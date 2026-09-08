"""
LangGraph re-implementation of autogen's MagenticOneOrchestrator, exposing
two graphs from one builder: an unmodified baseline, and a Trust_Allocator-
mediated variant spliced into every message hand-off.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from autogen_core import CancellationToken
from autogen_core.models import (
    AssistantMessage,
    ChatCompletionClient,
    LLMMessage,
    UserMessage,
)
from autogen_core.utils import extract_json_from_str
from autogen_agentchat.messages import TextMessage
from autogen_agentchat.base import ChatAgent

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver

from prompts import (
    ORCHESTRATOR_FINAL_ANSWER_PROMPT,
    ORCHESTRATOR_PROGRESS_LEDGER_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_FACTS_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_FACTS_UPDATE_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_FULL_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_PLAN_UPDATE_PROMPT,
)
from state import MagenticState, ThreadMessage
from trust import (
    TRUST_METRICS,
    TRUST_SCORE_MAX,
    TRUST_SCORE_MIN,
    format_combined_reason,
    format_conversation,
    format_trust_allocator_prompt,
    score_to_trust_level,
    wrap_with_trust_notice,
)

logger = logging.getLogger("magentic_one_langgraph")

ORCHESTRATOR_NAME = "MagenticOneOrchestrator"
TRUST_ALLOCATOR_NAME = "Trust_Allocator"
MAX_JSON_RETRIES = 10

# Neither _thread_to_context (progress_ledger) nor format_conversation
# (Trust_Allocator) previously bounded message size or history length --
# a single large agent output (e.g. an agent dumping a full spreadsheet's
# contents into a message, which happens more than you'd expect on GAIA
# file-attachment questions) could and did blow straight through the
# model's context window, confirmed against a real production failure
# ("prompt too long: 460751, max: 262144").
#
# MAX_MESSAGE_CHARS caps any single message at the point it enters the
# thread (in call_agent) -- this protects every downstream consumer
# (progress_ledger, update_task_ledger, final_answer, Trust_Allocator)
# without needing separate logic in each. ~20k chars is a generous but
# bounded ~5k tokens; tune to your model's num_ctx budget.
MAX_MESSAGE_CHARS = 20_000

# Trust_Allocator only needs to judge how trustworthy the LATEST message
# is -- unlike progress_ledger's loop-detection, it doesn't fundamentally
# need the entire history, just enough recent context to judge
# consistency. Windowing it is a much bigger win than windowing
# progress_ledger would be (which trades off loop-detection quality), so
# only Trust_Allocator's context is windowed here.
TRUST_ALLOCATOR_CONTEXT_WINDOW = 6


def _truncate_message_content(content: str, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Cap a single message's length, keeping head and tail (where context-
    setting and concluding/final content tend to live) rather than just
    cutting the end, with an explicit marker so truncation is visible in the
    data rather than silently lossy."""
    if not isinstance(content, str) or len(content) <= max_chars:
        return content
    half = max_chars // 2
    omitted = len(content) - max_chars
    return (
        content[:half]
        + f"\n\n... [{omitted} characters truncated -- message was {len(content)} characters total] ...\n\n"
        + content[-half:]
    )


def _get_compatible_context(model_client: ChatCompletionClient, messages: List[LLMMessage]) -> List[LLMMessage]:
    if model_client.model_info.get("vision", False):
        return messages
    from autogen_agentchat.utils import remove_images

    return remove_images(messages)


def robust_extract_json(content: str) -> List[Dict[str, Any]]:
    """Extract JSON object(s) from a model response, robust to the model
    wrapping its answer in a markdown fence that ITSELF contains a nested
    fenced code block (e.g. instructing ComputerTerminal with a fenced
    shell command inside instruction_or_question.answer -- a very common
    pattern in Magentic-One, since that's the normal way to hand code to
    ComputerTerminal).

    autogen_core.utils.extract_json_from_str's fence regex is non-greedy
    (```(...)?\\n([\\s\\S]*?)```), so on input containing a nested ``` fence
    inside a JSON string value, it matches the FIRST closing ``` it finds --
    which is the INNER fence's closing marker, not the real outer one. That
    silently truncates the extracted text mid-object, producing an
    "Unterminated string" JSONDecodeError even when the model's actual
    output was complete and well-formed. Confirmed against a real production
    failure trace.

    Strategy: strip a leading/trailing outer fence by POSITION (not regex),
    which is immune to what's nested inside, try that first; fall back to
    raw parsing (no fence at all), then to autogen_core's own extractor as a
    last resort for anything this doesn't specifically target.
    """
    text = content.strip() if isinstance(content, str) else content

    if isinstance(text, str) and text.startswith("```"):
        first_newline = text.find("\n")
        inner = text[first_newline + 1 :] if first_newline != -1 else text[3:]
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3]
        try:
            return [json.loads(inner)]
        except json.JSONDecodeError:
            pass  # fall through

    if isinstance(text, str):
        try:
            return [json.loads(text)]
        except json.JSONDecodeError:
            pass

    return extract_json_from_str(content)


def _thread_to_context(messages: List[ThreadMessage], trust_level: str = "undefined", trust_reason: str = "") -> List[LLMMessage]:
    """Port of MagenticOneOrchestrator._thread_to_context. The trust notice
    (if any) is applied only to the last message, since trust_level/reason
    always describe "the latest message" at the moment it's being read."""
    context: List[LLMMessage] = []
    for i, m in enumerate(messages):
        content = m["content"]
        if i == len(messages) - 1:
            content = wrap_with_trust_notice(content, trust_level, trust_reason)
        if m["source"] == ORCHESTRATOR_NAME:
            context.append(AssistantMessage(content=content, source=m["source"]))
        else:
            context.append(UserMessage(content=content, source=m["source"]))
    return context


def _team_description(participant_names: List[str], participant_descriptions: List[str]) -> str:
    import re

    desc = ""
    for name, description in zip(participant_names, participant_descriptions, strict=True):
        desc += re.sub(r"\s+", " ", f"{name}: {description}").strip() + "\n"
    return desc.strip()


AgentCaller = Callable[[str, CancellationToken], Awaitable[str]]


def make_autogen_agent_caller(agent: ChatAgent) -> AgentCaller:
    async def _call(instruction: str, cancellation_token: CancellationToken) -> str:
        message = TextMessage(content=instruction, source=ORCHESTRATOR_NAME)
        response = await agent.on_messages([message], cancellation_token)
        return response.chat_message.to_model_text()

    return _call


def build_magentic_one_graph(
    model_client: ChatCompletionClient,
    agent_callers: Dict[str, AgentCaller],
    participant_descriptions: Dict[str, str],
    max_rounds: int = 20,
    max_stalls: int = 3,
    final_answer_prompt: str = ORCHESTRATOR_FINAL_ANSWER_PROMPT,
    include_trust_allocator: bool = False,
    trust_model_client: Optional[ChatCompletionClient] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
):
    participant_names = list(agent_callers.keys())
    team_description = _team_description(
        participant_names, [participant_descriptions[n] for n in participant_names]
    )
    trust_client = trust_model_client or model_client

    async def create_task_ledger(state: MagenticState) -> MagenticState:
        task = state["task"]
        planning_conversation: List[LLMMessage] = []

        planning_conversation.append(
            UserMessage(content=ORCHESTRATOR_TASK_LEDGER_FACTS_PROMPT.format(task=task), source=ORCHESTRATOR_NAME)
        )
        response = await model_client.create(_get_compatible_context(model_client, planning_conversation))
        assert isinstance(response.content, str)
        facts = response.content
        planning_conversation.append(AssistantMessage(content=facts, source=ORCHESTRATOR_NAME))

        planning_conversation.append(
            UserMessage(
                content=ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT.format(team=team_description), source=ORCHESTRATOR_NAME
            )
        )
        response = await model_client.create(_get_compatible_context(model_client, planning_conversation))
        assert isinstance(response.content, str)
        plan = response.content

        return {
            **state,
            "team_description": team_description,
            "participant_names": participant_names,
            "facts": facts,
            "plan": plan,
            "n_stalls": 0,
            "max_rounds": state.get("max_rounds", max_rounds),
            "max_stalls": state.get("max_stalls", max_stalls),
            "trust_level": state.get("trust_level", "undefined"),
            "trust_reason": state.get("trust_reason", ""),
            "trust_scores": state.get("trust_scores", {}),
            "trust_history": state.get("trust_history", []),
        }

    async def init_outer_loop(state: MagenticState) -> MagenticState:
        ledger_message: ThreadMessage = {
            "source": ORCHESTRATOR_NAME,
            "content": ORCHESTRATOR_TASK_LEDGER_FULL_PROMPT.format(
                task=state["task"],
                team=state["team_description"],
                facts=state["facts"],
                plan=state["plan"],
            ),
        }
        return {**state, "messages": [ledger_message], "route_after_trust": "progress_ledger"}

    async def progress_ledger(state: MagenticState) -> MagenticState:
        n_rounds = state.get("n_rounds", 0) + 1
        trust_level = state.get("trust_level", "undefined")
        trust_reason = state.get("trust_reason", "")
        context = _thread_to_context(state["messages"], trust_level, trust_reason)
        context.append(
            UserMessage(
                content=ORCHESTRATOR_PROGRESS_LEDGER_PROMPT.format(
                    task=state["task"], team=state["team_description"], names=", ".join(state["participant_names"])
                ),
                source=ORCHESTRATOR_NAME,
            )
        )

        base_context = context
        ledger: Dict[str, Any] = {}
        key_error = False
        ledger_str: Optional[str] = None
        error_detail: Optional[str] = None
        correction: Optional[tuple[str, str]] = None  # (bad_response_text, error_detail) from the prior attempt
        consecutive_parse_failures = 0  # not schema mistakes -- outright unparseable/truncated-looking output

        for attempt in range(MAX_JSON_RETRIES):
            # After repeated raw parse failures (not schema-shape mistakes), stop growing the
            # context with a correction round -- that's more likely to be truncation/context-length
            # pressure than a fixable misunderstanding, and appending more text only makes that worse.
            offer_correction = correction is not None and consecutive_parse_failures < 2
            if offer_correction:
                bad_response_text, prior_error_detail = correction
                # Bounded: always original context + at most one correction round, never
                # accumulating across attempts, so retries can't balloon the context window.
                retry_context = base_context + [
                    AssistantMessage(content=bad_response_text, source=ORCHESTRATOR_NAME),
                    UserMessage(
                        content=(
                            f"That response was invalid: {prior_error_detail} Respond again with ONLY the "
                            "corrected JSON object, matching the schema exactly."
                        ),
                        source=ORCHESTRATOR_NAME,
                    ),
                ]
            else:
                retry_context = base_context

            if model_client.model_info.get("json_output", False):
                response = await model_client.create(
                    _get_compatible_context(model_client, retry_context), json_output=True
                )
            else:
                response = await model_client.create(_get_compatible_context(model_client, retry_context))
            ledger_str = response.content
            error_detail = None
            try:
                assert isinstance(ledger_str, str)
                output_json = robust_extract_json(ledger_str)
                if len(output_json) != 1:
                    raise ValueError("expected exactly one JSON object in the response")
                ledger = output_json[0]

                if len(state["participant_names"]) == 1:
                    ledger["next_speaker"] = {
                        "reason": "The team consists of only one agent.",
                        "answer": state["participant_names"][0],
                    }

                required_keys = [
                    "is_request_satisfied",
                    "is_progress_being_made",
                    "is_in_loop",
                    "instruction_or_question",
                    "next_speaker",
                ]
                key_error = False
                for key in required_keys:
                    if (
                        key not in ledger
                        or not isinstance(ledger[key], dict)
                        or "answer" not in ledger[key]
                        or "reason" not in ledger[key]
                    ):
                        key_error = True
                        error_detail = (
                            f'the key "{key}" was missing, or was not an object containing both '
                            f'"reason" and "answer" fields.'
                        )
                        break
                if not key_error and (
                    not ledger["is_request_satisfied"]["answer"]
                    and ledger["next_speaker"]["answer"] not in state["participant_names"]
                ):
                    key_error = True
                    error_detail = (
                        f'"next_speaker.answer" was {ledger["next_speaker"]["answer"]!r}, which does not '
                        f'exactly match any team member name ({", ".join(state["participant_names"])}).'
                    )

                if not key_error:
                    break
                consecutive_parse_failures = 0  # a schema-shape mistake, not a parse failure -- correction is likely to help
            except (json.JSONDecodeError, TypeError, ValueError, AssertionError) as e:
                key_error = True
                error_detail = f"the response was not valid, parsable JSON on its own ({e})."
                consecutive_parse_failures += 1

            logger.warning(
                "progress_ledger: attempt %d/%d failed (%s) offering_correction_next=%s Raw response preview: %r",
                attempt + 1,
                MAX_JSON_RETRIES,
                error_detail,
                consecutive_parse_failures < 2,
                (ledger_str or "")[:300],
            )
            correction = (ledger_str or "", error_detail or "the response did not match the required schema.")

        if key_error:
            raise ValueError(
                f"Failed to parse ledger information after {MAX_JSON_RETRIES} retries "
                f"({error_detail}). Last raw response: {(ledger_str or '')[:2000]!r}"
            )

        n_stalls = state.get("n_stalls", 0)
        if not ledger["is_progress_being_made"]["answer"]:
            n_stalls += 1
        elif ledger["is_in_loop"]["answer"]:
            n_stalls += 1
        else:
            n_stalls = max(0, n_stalls - 1)

        new_messages = list(state["messages"])
        if not ledger["is_request_satisfied"]["answer"]:
            new_messages = new_messages + [
                {"source": ORCHESTRATOR_NAME, "content": ledger["instruction_or_question"]["answer"]}
            ]

        return {
            **state,
            "n_rounds": n_rounds,
            "n_stalls": n_stalls,
            "progress_ledger": ledger,
            "is_satisfied": bool(ledger["is_request_satisfied"]["answer"]),
            "next_speaker": ledger["next_speaker"]["answer"],
            "instruction": ledger["instruction_or_question"]["answer"],
            "messages": new_messages,
            "route_after_trust": "call_agent",
        }

    async def call_agent(state: MagenticState) -> MagenticState:
        speaker = state["next_speaker"]
        caller = agent_callers[speaker]
        trust_level = state.get("trust_level", "undefined")
        trust_reason = state.get("trust_reason", "")
        delivered_instruction = wrap_with_trust_notice(state["instruction"], trust_level, trust_reason)
        content = await caller(delivered_instruction, CancellationToken())
        content = _truncate_message_content(content)
        new_messages = list(state["messages"]) + [{"source": speaker, "content": content}]
        return {**state, "messages": new_messages, "route_after_trust": "progress_ledger"}

    async def update_task_ledger(state: MagenticState) -> MagenticState:
        context = _thread_to_context(state["messages"], state.get("trust_level", "undefined"), state.get("trust_reason", ""))

        context.append(
            UserMessage(
                content=ORCHESTRATOR_TASK_LEDGER_FACTS_UPDATE_PROMPT.format(task=state["task"], facts=state["facts"]),
                source=ORCHESTRATOR_NAME,
            )
        )
        response = await model_client.create(_get_compatible_context(model_client, context))
        assert isinstance(response.content, str)
        facts = response.content
        context.append(AssistantMessage(content=facts, source=ORCHESTRATOR_NAME))

        context.append(
            UserMessage(
                content=ORCHESTRATOR_TASK_LEDGER_PLAN_UPDATE_PROMPT.format(team=state["team_description"]),
                source=ORCHESTRATOR_NAME,
            )
        )
        response = await model_client.create(_get_compatible_context(model_client, context))
        assert isinstance(response.content, str)
        plan = response.content

        return {**state, "facts": facts, "plan": plan}

    async def final_answer(state: MagenticState) -> MagenticState:
        context = _thread_to_context(state["messages"], state.get("trust_level", "undefined"), state.get("trust_reason", ""))
        context.append(UserMessage(content=final_answer_prompt.format(task=state["task"]), source=ORCHESTRATOR_NAME))
        response = await model_client.create(_get_compatible_context(model_client, context))
        assert isinstance(response.content, str)

        reason = (
            "Max rounds reached."
            if state.get("n_rounds", 0) > state.get("max_rounds", max_rounds)
            else state.get("progress_ledger", {}).get("is_request_satisfied", {}).get("reason", "Task completed.")
        )

        return {
            **state,
            "final_answer": response.content,
            "termination_reason": reason,
            "messages": list(state["messages"]) + [{"source": ORCHESTRATOR_NAME, "content": response.content}],
        }

    async def trust_allocator(state: MagenticState) -> MagenticState:
        messages = state["messages"]
        if not messages:
            return state

        last = messages[-1]
        windowed_messages = messages[-TRUST_ALLOCATOR_CONTEXT_WINDOW:]
        prompt = format_trust_allocator_prompt(
            task=state["task"],
            conversation=format_conversation(windowed_messages),
            last_speaker=last["source"],
        )
        context: List[LLMMessage] = [UserMessage(content=prompt, source=TRUST_ALLOCATOR_NAME)]

        scores: Optional[Dict[str, Dict[str, Any]]] = None
        for _ in range(MAX_JSON_RETRIES):
            if trust_client.model_info.get("json_output", False):
                response = await trust_client.create(_get_compatible_context(trust_client, context), json_output=True)
            else:
                response = await trust_client.create(_get_compatible_context(trust_client, context))
            try:
                assert isinstance(response.content, str)
                parsed = robust_extract_json(response.content)[0]
                candidate: Dict[str, Dict[str, Any]] = {}
                valid = True
                for metric in TRUST_METRICS:
                    entry = parsed.get(metric)
                    if not isinstance(entry, dict) or "score" not in entry:
                        valid = False
                        break
                    score = int(entry["score"])
                    if not (TRUST_SCORE_MIN <= score <= TRUST_SCORE_MAX):
                        valid = False
                        break
                    candidate[metric] = {"score": score, "reason": str(entry.get("reason", ""))}
                if valid:
                    scores = candidate
                    break
            except (json.JSONDecodeError, TypeError, ValueError, KeyError, IndexError):
                continue

        if scores is None:
            scores = {m: {"score": 3, "reason": "Trust_Allocator failed to parse a valid response after retries."} for m in TRUST_METRICS}
            logger.warning("Trust_Allocator failed to parse a response; defaulting to a flat medium score set.")

        answer = score_to_trust_level({m: scores[m]["score"] for m in TRUST_METRICS})
        reason = format_combined_reason(scores)

        log_entry = {
            "step": state.get("n_rounds", 0),
            "message_index": len(messages) - 1,
            "evaluated_source": last["source"],
            "trust_level": answer,
            "reason": reason,
            "scores": scores,
        }
        trust_history = list(state.get("trust_history", [])) + [log_entry]

        # `messages` is untouched -- Trust_Allocator's own output never
        # joins the transcript, so it stays invisible to every other agent.
        return {**state, "trust_level": answer, "trust_reason": reason, "trust_scores": scores, "trust_history": trust_history}

    def route_after_progress_ledger(state: MagenticState) -> str:
        if state.get("n_rounds", 0) > state.get("max_rounds", max_rounds):
            return "final_answer"
        if state["is_satisfied"]:
            return "final_answer"
        if state["n_stalls"] >= state.get("max_stalls", max_stalls):
            return "update_task_ledger"
        return "trust_allocator" if include_trust_allocator else "call_agent"

    def route_after_trust(state: MagenticState) -> str:
        return state["route_after_trust"]

    graph = StateGraph(MagenticState)
    graph.add_node("create_task_ledger", create_task_ledger)
    graph.add_node("init_outer_loop", init_outer_loop)
    graph.add_node("progress_ledger", progress_ledger)
    graph.add_node("call_agent", call_agent)
    graph.add_node("update_task_ledger", update_task_ledger)
    graph.add_node("final_answer", final_answer)

    graph.set_entry_point("create_task_ledger")
    graph.add_edge("create_task_ledger", "init_outer_loop")

    if include_trust_allocator:
        graph.add_node("trust_allocator", trust_allocator)
        graph.add_edge("init_outer_loop", "trust_allocator")
        graph.add_conditional_edges(
            "progress_ledger",
            route_after_progress_ledger,
            {"final_answer": "final_answer", "update_task_ledger": "update_task_ledger", "trust_allocator": "trust_allocator"},
        )
        graph.add_conditional_edges(
            "trust_allocator", route_after_trust, {"call_agent": "call_agent", "progress_ledger": "progress_ledger"}
        )
        graph.add_edge("call_agent", "trust_allocator")
    else:
        graph.add_edge("init_outer_loop", "progress_ledger")
        graph.add_conditional_edges(
            "progress_ledger",
            route_after_progress_ledger,
            {"final_answer": "final_answer", "update_task_ledger": "update_task_ledger", "call_agent": "call_agent"},
        )
        graph.add_edge("call_agent", "progress_ledger")

    graph.add_edge("update_task_ledger", "init_outer_loop")
    graph.add_edge("final_answer", END)

    return graph.compile(checkpointer=checkpointer)


def build_both_magentic_one_graphs(
    model_client: ChatCompletionClient,
    agent_callers: Dict[str, AgentCaller],
    participant_descriptions: Dict[str, str],
    max_rounds: int = 20,
    max_stalls: int = 3,
    final_answer_prompt: str = ORCHESTRATOR_FINAL_ANSWER_PROMPT,
    trust_model_client: Optional[ChatCompletionClient] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
):
    common = dict(
        model_client=model_client,
        agent_callers=agent_callers,
        participant_descriptions=participant_descriptions,
        max_rounds=max_rounds,
        max_stalls=max_stalls,
        final_answer_prompt=final_answer_prompt,
        checkpointer=checkpointer,
    )
    baseline = build_magentic_one_graph(include_trust_allocator=False, **common)
    trust = build_magentic_one_graph(
        include_trust_allocator=True, trust_model_client=trust_model_client, **common
    )
    return baseline, trust