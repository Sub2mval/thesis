from __future__ import annotations

import warnings
from typing import Awaitable, Callable, Dict, Optional, Union

from autogen_agentchat.agents import ApprovalFuncType, CodeExecutorAgent, UserProxyAgent
from autogen_core import CancellationToken
from autogen_core.code_executor import CodeExecutor
from autogen_core.models import ChatCompletionClient
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from autogen_ext.agents.file_surfer import FileSurfer
from autogen_ext.agents.magentic_one import MagenticOneCoderAgent
from autogen_ext.agents.web_surfer import MultimodalWebSurfer
from autogen_ext.code_executors import create_default_code_executor
from autogen_ext.models.openai._openai_client import BaseOpenAIChatCompletionClient

from ollama_client import build_ollama_client, get_usage_tracking, reset_usage_tracking
from orchestrator_graph import build_magentic_one_graph, make_autogen_agent_caller
from prompts import ORCHESTRATOR_FINAL_ANSWER_PROMPT

SyncInputFunc = Callable[[str], str]
AsyncInputFunc = Callable[[str, Optional[CancellationToken]], Awaitable[str]]
InputFuncType = Union[SyncInputFunc, AsyncInputFunc]

GraphName = str


class MagenticOneLangGraph:
    def __init__(
        self,
        client: ChatCompletionClient,
        hil_mode: bool = False,
        input_func: InputFuncType | None = None,
        code_executor: CodeExecutor | None = None,
        approval_func: ApprovalFuncType | None = None,
        max_turns: int | None = 20,
        max_stalls: int = 3,
        final_answer_prompt: str = ORCHESTRATOR_FINAL_ANSWER_PROMPT,
        trust_model_client: Optional[ChatCompletionClient] = None,
        checkpointer: Optional[BaseCheckpointSaver] = None,
    ):
        self.client = client
        self.trust_model_client = trust_model_client or client
        self._validate_client_capabilities(client)

        if code_executor is None:
            warnings.warn(
                "Instantiating MagenticOneLangGraph without a code_executor is deprecated. "
                "Provide a code_executor to clear this warning (e.g., code_executor=DockerCommandLineCodeExecutor()).",
                DeprecationWarning,
                stacklevel=2,
            )
            code_executor = create_default_code_executor()

        fs = FileSurfer("FileSurfer", model_client=client)
        ws = MultimodalWebSurfer("WebSurfer", model_client=client)
        coder = MagenticOneCoderAgent("Coder", model_client=client)
        executor = CodeExecutorAgent("ComputerTerminal", code_executor=code_executor, approval_func=approval_func)

        self._agents = {fs.name: fs, ws.name: ws, coder.name: coder, executor.name: executor}
        if hil_mode:
            user_proxy = UserProxyAgent("User", input_func=input_func)
            self._agents[user_proxy.name] = user_proxy

        agent_callers = {name: make_autogen_agent_caller(agent) for name, agent in self._agents.items()}
        participant_descriptions = {name: agent.description for name, agent in self._agents.items()}

        self._max_turns = max_turns if max_turns is not None else 20
        self._max_stalls = max_stalls
        self.checkpointer = checkpointer or InMemorySaver()

        common = dict(
            model_client=client,
            agent_callers=agent_callers,
            participant_descriptions=participant_descriptions,
            max_rounds=self._max_turns,
            max_stalls=max_stalls,
            final_answer_prompt=final_answer_prompt,
            checkpointer=self.checkpointer,
        )
        self.graph_baseline = build_magentic_one_graph(include_trust_allocator=False, **common)
        self.graph_trust_allocator = build_magentic_one_graph(
            include_trust_allocator=True, trust_model_client=self.trust_model_client, **common
        )

    @classmethod
    def from_ollama(
        cls,
        model: str,
        host: str = "http://localhost:11434",
        trust_model: Optional[str] = None,
        model_info: Optional[dict] = None,
        **kwargs,
    ) -> "MagenticOneLangGraph":
        client = build_ollama_client(model=model, host=host, model_info=model_info)
        trust_client = build_ollama_client(model=trust_model, host=host) if trust_model else None
        return cls(client=client, trust_model_client=trust_client, **kwargs)

    def _validate_client_capabilities(self, client: ChatCompletionClient) -> None:
        capabilities = client.model_info
        required_capabilities = ["function_calling", "json_output"]
        if not all(capabilities.get(cap) for cap in required_capabilities):
            warnings.warn(
                "Client capabilities for MagenticOne must include vision, function calling, and json output.",
                stacklevel=2,
            )

    def _graph(self, name: GraphName):
        if name == "baseline":
            return self.graph_baseline
        if name == "trust_allocator":
            return self.graph_trust_allocator
        raise ValueError(f"Unknown graph '{name}'. Must be 'baseline' or 'trust_allocator'.")

    async def close(self) -> None:
        """Release resources held by the underlying agents (in particular
        MultimodalWebSurfer's Playwright browser). Call this in a
        try/finally around your run loop."""
        for name, agent in self._agents.items():
            close_fn = getattr(agent, "close", None)
            if close_fn is None:
                continue
            try:
                await close_fn()
            except Exception as e:
                warnings.warn(f"Error closing agent '{name}': {e}", stacklevel=2)

    async def run(
        self,
        task: str,
        graph: GraphName = "baseline",
        thread_id: str = "default",
        initial_trust_level: str = "undefined",
        initial_trust_reason: str = "",
    ) -> Dict:
        graph_obj = self._graph(graph)

        # Reset usage for this trace. The same client instance is shared by the
        # orchestrator and worker agents, so this captures the complete trace
        # rather than only top-level orchestration calls.
        reset_usage_tracking(self.client)
        if self.trust_model_client is not self.client:
            reset_usage_tracking(self.trust_model_client)

        config = {"configurable": {"thread_id": thread_id}}
        initial_state = {
            "task": task,
            "n_rounds": 0,
            "n_stalls": 0,
            "max_rounds": self._max_turns,
            "max_stalls": self._max_stalls,
            "trust_level": initial_trust_level,
            "trust_reason": initial_trust_reason,
            "trust_history": [],
        }
        result = await graph_obj.ainvoke(initial_state, config=config)

        main_usage = get_usage_tracking(self.client)
        trust_usage = (
            get_usage_tracking(self.trust_model_client)
            if self.trust_model_client is not self.client
            else {"n_llm_calls": 0, "n_successful_calls": 0, "n_failed_attempts": 0,
                  "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": []}
        )

        calls = list(main_usage.get("calls", [])) + list(trust_usage.get("calls", []))
        calls.sort(key=lambda r: (r.get("started_at_unix", 0), r.get("call_index", 0)))
        prompt_tokens = sum(r.get("prompt_tokens") or 0 for r in calls)
        completion_tokens = sum(r.get("completion_tokens") or 0 for r in calls)

        result["token_stats"] = {
            "n_llm_calls": len(calls),
            "n_successful_calls": sum(r.get("status") == "ok" for r in calls),
            "n_failed_attempts": sum(r.get("status") == "error" for r in calls),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "calls": calls,
        }

        return result

    async def astream(
        self,
        task: str,
        graph: GraphName = "baseline",
        thread_id: str = "default",
        initial_trust_level: str = "undefined",
        initial_trust_reason: str = "",
    ):
        graph_obj = self._graph(graph)
        config = {"configurable": {"thread_id": thread_id}}
        initial_state = {
            "task": task,
            "n_rounds": 0,
            "n_stalls": 0,
            "max_rounds": self._max_turns,
            "max_stalls": self._max_stalls,
            "trust_level": initial_trust_level,
            "trust_reason": initial_trust_reason,
            "trust_history": [],
        }
        async for event in graph_obj.astream(initial_state, config=config):
            yield event