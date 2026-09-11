"""
Dispatch to worker agents, and the Gricean adherence gate every message
hand-off passes through before the next node consumes it.

gricean_check_node always looks only at the last message in `messages`.
If it clears HIGH adherence, nothing else happens -- the next node behaves
exactly as if there were no checker. If it does not, this node makes one
extra model call asking whichever node runs next to reflect on the
flagged message (using the checker's own reasoning as the prompt) and
hands that single reflection to that node via `state["pending_reflection"]`.
See state.py for why that reflection is logged but never re-read later.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from autogen_core import CancellationToken
from autogen_core.models import ChatCompletionClient, UserMessage

from magnetic_one.context_utils import AgentCaller, get_compatible_context
from magnetic_one.gricean_check import (
    GRICEAN_CHECKER_NAME,
    GRICEAN_METRICS,
    SCORE_MAX,
    SCORE_MIN,
    format_combined_reason,
    format_conversation,
    format_gricean_check_prompt,
    format_reflection_prompt,
    score_to_adherence_level,
)
from magnetic_one.json_llm import call_model_for_json, truncate_message_content
from magnetic_one.state import MagenticState

logger = logging.getLogger("magentic_one_langgraph.dispatch_nodes")

# The checker only needs enough recent context to judge consistency, not
# the full history (unlike progress_ledger's loop-detection, which does).
ADHERENCE_CHECK_CONTEXT_WINDOW = 6


def build_dispatch_nodes(gricean_client: ChatCompletionClient, agent_callers: Dict[str, AgentCaller]) -> Dict[str, Any]:
    async def call_agent(state: MagenticState) -> MagenticState:
        speaker = state["next_speaker"]
        instruction = state["instruction"]
        if state.get("pending_reflection"):
            instruction = f"{state['pending_reflection']}\n\n{instruction}"
        content = await agent_callers[speaker](instruction, CancellationToken())
        content = truncate_message_content(content)
        new_messages = list(state["messages"]) + [{"source": speaker, "content": content}]
        return {**state, "messages": new_messages, "next_after_check": "progress_ledger"}

    async def gricean_check_node(state: MagenticState) -> MagenticState:
        messages = state["messages"]
        if not state.get("enable_gricean_check", True) or not messages:
            return {**state, "adherence_level": "high", "pending_reflection": None}

        last = messages[-1]
        window = messages[-ADHERENCE_CHECK_CONTEXT_WINDOW:]
        prompt = format_gricean_check_prompt(state["task"], format_conversation(window), last["source"])
        base_context = [UserMessage(content=prompt, source=GRICEAN_CHECKER_NAME)]

        def validate(parsed):
            cleaned = {}
            for metric in GRICEAN_METRICS:
                entry = parsed.get(metric)
                if not isinstance(entry, dict) or "score" not in entry:
                    return False, f'missing or malformed "{metric}" entry', None
                score = int(entry["score"])
                if not (SCORE_MIN <= score <= SCORE_MAX):
                    return False, f'"{metric}" score {score} is out of the 1-5 range', None
                cleaned[metric] = {"score": score, "reason": str(entry.get("reason", ""))}
            return True, None, cleaned

        try:
            scores = await call_model_for_json(gricean_client, get_compatible_context, base_context, validate, GRICEAN_CHECKER_NAME)
        except ValueError:
            scores = {m: {"score": 3, "reason": "Adherence checker failed to parse a response after retries."} for m in GRICEAN_METRICS}
            logger.warning("Gricean adherence checker failed to parse a response; defaulting to a flat score set.")

        level = score_to_adherence_level({m: scores[m]["score"] for m in GRICEAN_METRICS})
        reason = format_combined_reason(scores)
        adherence_history = list(state.get("adherence_history", [])) + [
            {"step": state.get("n_rounds", 0), "message_index": len(messages) - 1, "evaluated_source": last["source"], "adherence_level": level, "reason": reason, "scores": scores}
        ]

        pending_reflection: Optional[str] = None
        reflection_history = list(state.get("reflection_history", []))
        if level != "high":
            next_node = state["next_after_check"]
            reflection_prompt = format_reflection_prompt(last["source"], last["content"], reason)
            response = await gricean_client.create(
                get_compatible_context(gricean_client, [UserMessage(content=reflection_prompt, source=next_node)])
            )
            if isinstance(response.content, str):
                pending_reflection = response.content
                reflection_history.append(
                    {"step": state.get("n_rounds", 0), "message_index": len(messages) - 1, "consuming_node": next_node, "reason": reason, "reflection": pending_reflection}
                )

        return {
            **state,
            "adherence_level": level,
            "adherence_reason": reason,
            "adherence_scores": scores,
            "adherence_history": adherence_history,
            "pending_reflection": pending_reflection,
            "reflection_history": reflection_history,
        }

    return {"call_agent": call_agent, "gricean_check": gricean_check_node}