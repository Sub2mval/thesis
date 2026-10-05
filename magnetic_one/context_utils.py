"""
Generic wiring shared across agent nodes: turning MessageHistory into an
LLM-ready context, describing the team, and adapting AutoGen ChatAgents so
the graph can call them uniformly.

Nothing here is Gricean-check-specific -- reflection injection is each
agent node's own responsibility (orchestrator_agent.py / worker_agent.py
prepend `state["pending_reflection"]` to their own context/instruction
when it's set); this module just builds the plain, unmodified context.

build_llm_context's optional `wrap_last_with_level` does the equivalent
job for the experiment-design/Trust_Allocator extension's delivery-time
notice (state["pending_trust_level"], set by gricean_checker.py's
Design 1-3 branch): it wraps only the LAST message's *content* in the
returned LLMMessage list, using trust_allocator.legacy_trust_allocator.
wrap_with_trust_notice -- the underlying `messages` list passed in is
never mutated, matching the "notices are delivery-time context only"
requirement.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from autogen_agentchat.base import ChatAgent
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from autogen_core.models import AssistantMessage, ChatCompletionClient, LLMMessage, UserMessage

from magnetic_one.state import ThreadMessage
from trust_allocator.legacy_trust_allocator import wrap_with_trust_notice

ORCHESTRATOR_NAME = "MagenticOneOrchestrator"

AgentCallResult = tuple[str, List[Dict[str, Any]]]
AgentCaller = Callable[[str, CancellationToken], Awaitable[AgentCallResult]]


def get_compatible_context(model_client: ChatCompletionClient, messages: List[LLMMessage]) -> List[LLMMessage]:
    """Strip images from the context for models that can't see them."""
    if model_client.model_info.get("vision", False):
        return messages
    from autogen_agentchat.utils import remove_images

    return remove_images(messages)


def build_llm_context(messages: List[ThreadMessage], wrap_last_with_level: Optional[str] = None) -> List[LLMMessage]:
    """Turn the permanent thread into plain LLM messages -- one-to-one,
    no notices or reflections spliced in here, EXCEPT that when
    `wrap_last_with_level` is given (a "low"/"medium"/"high" trust
    level, from state["pending_trust_level"]), the last message's
    content is prepended with that level's trust notice in the returned
    copy only -- `messages` itself is never touched. None (the default,
    and design 4's only value) is a no-op, same as passing an unassessed
    "undefined" level. Callers append whatever extra per-call context
    (a reflection, a task prompt) they need on top of this."""
    context: List[LLMMessage] = []
    last_index = len(messages) - 1
    for i, m in enumerate(messages):
        content = m["content"]
        if i == last_index and wrap_last_with_level:
            content = wrap_with_trust_notice(content, wrap_last_with_level)
        if m["source"] == ORCHESTRATOR_NAME:
            context.append(AssistantMessage(content=content, source=m["source"]))
        else:
            context.append(UserMessage(content=content, source=m["source"]))
    return context


def team_description(participant_names: List[str], participant_descriptions: List[str]) -> str:
    desc = ""
    for name, description in zip(participant_names, participant_descriptions, strict=True):
        desc += re.sub(r"\s+", " ", f"{name}: {description}").strip() + "\n"
    return desc.strip()


def make_autogen_agent_caller(agent: ChatAgent) -> AgentCaller:
    """Adapt an AutoGen ChatAgent into the plain async-function shape the
    graph's `call_agent` node dispatches through."""

    async def _call(instruction: str, cancellation_token: CancellationToken) -> AgentCallResult:
        message = TextMessage(content=instruction, source=ORCHESTRATOR_NAME)
        tool_events: List[Dict[str, Any]] = []
        final_response = None
        async for event in agent.on_messages_stream([message], cancellation_token):
            if hasattr(event, "chat_message"):
                final_response = event.chat_message
            event_type = type(event).__name__
            if event_type in {"ToolCallExecutionEvent", "CodeExecutionEvent"}:
                if event_type == "ToolCallExecutionEvent":
                    results = []
                    for result in getattr(event, "content", []) or []:
                        results.append({
                            "name": getattr(result, "name", None),
                            "call_id": getattr(result, "call_id", None),
                            "content": getattr(result, "content", str(result)),
                            "is_error": getattr(result, "is_error", None),
                        })
                    tool_events.append({"type": event_type, "source": getattr(event, "source", None), "results": results})
                else:
                    result = getattr(event, "result", None)
                    tool_events.append({
                        "type": event_type, "source": getattr(event, "source", None),
                        "output": getattr(result, "output", event.to_text() if hasattr(event, "to_text") else str(event)),
                    })
        if final_response is None:
            raise RuntimeError(f"Agent {agent.name} produced no final chat message.")
        return final_response.to_model_text(), tool_events

    return _call