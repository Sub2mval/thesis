"""
The Gricean_Checker agent node.

Sits on every edge between MagenticOneOrchestrator and a worker agent, in
both directions (matching the reference diagram). Every message that is
ever appended to MessageHistory passes through this node exactly once,
right after it's added -- so, over a full run, `state["gricean_history"]`
ends up with one score entry per message. It only ever looks at the LAST
message; older messages were already scored (and are never rescored) on
the turn they were added.

If that message doesn't clear HIGH adherence AND `enable_gricean_check` is
on, this node writes one extra message: not a generic warning, but a
reflection prompt (gricean_check.format_reflection_prompt) that names the
specific Gricean violation and asks the RECEIVING agent
(`state["next_after_check"]`, whoever this message is headed to) to act in
a way that repairs it -- so THAT agent's own next message restores a high
Gricean score. That reflection is handed off via `state["pending_reflection"]`,
used exactly once by the receiving node, and logged to
`state["reflection_history"]` for audit; it is never re-read into a later
prompt, by that agent or any other.

`enable_gricean_check` gates ONLY the reflection loop, not the scoring
itself: this node always scores every message and appends to
`gricean_history`, on or off, so a baseline run's log is directly
comparable to a checked run's -- the same messages get scored the same
way either way, it's just that a baseline run never turns a low score into
a reflection that reaches an agent. `pending_reflection` is always None
when the flag is off, regardless of the score, so an agent never sees a
reflection it didn't earn a real intervention for.
"""

from __future__ import annotations

import logging
from typing import Optional

from autogen_core.models import ChatCompletionClient, UserMessage

from context_utils import get_compatible_context
from gricean_check import (
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
from json_llm import call_model_for_json
from state import MagenticState

logger = logging.getLogger("magentic_one_langgraph.gricean_checker")

# Only recent context is needed to judge consistency, unlike the
# Orchestrator's own loop-detection, which needs the full history.
ADHERENCE_CHECK_CONTEXT_WINDOW = 6


def build_gricean_checker_node(gricean_client: ChatCompletionClient):
    async def gricean_checker_node(state: MagenticState) -> MagenticState:
        messages = state["messages"]
        if not messages:
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
            scores = {m: {"score": 3, "reason": "Gricean_Checker failed to parse a response after retries."} for m in GRICEAN_METRICS}
            logger.warning("Gricean_Checker failed to parse a response; defaulting to a flat score set.")

        level = score_to_adherence_level({m: scores[m]["score"] for m in GRICEAN_METRICS})
        reason = format_combined_reason(scores)
        gricean_history = list(state.get("gricean_history", [])) + [
            {
                "step": state.get("n_rounds", 0),
                "message_index": len(messages) - 1,
                "evaluated_source": last["source"],
                "adherence_level": level,
                "reason": reason,
                "scores": scores,
            }
        ]

        pending_reflection: Optional[str] = None
        reflection_history = list(state.get("reflection_history", []))
        if state.get("enable_gricean_check", True) and level != "high":
            receiving_agent = state["next_after_check"]
            reflection_prompt = format_reflection_prompt(last["source"], last["content"], scores)
            response = await gricean_client.create(
                get_compatible_context(gricean_client, [UserMessage(content=reflection_prompt, source=receiving_agent)])
            )
            if isinstance(response.content, str):
                pending_reflection = response.content
                reflection_history.append(
                    {
                        "step": state.get("n_rounds", 0),
                        "message_index": len(messages) - 1,
                        "receiving_agent": receiving_agent,
                        "reason": reason,
                        "reflection": pending_reflection,
                    }
                )

        return {
            **state,
            "adherence_level": level,
            "adherence_reason": reason,
            "adherence_scores": scores,
            "gricean_history": gricean_history,
            "pending_reflection": pending_reflection,
            "reflection_history": reflection_history,
        }

    return gricean_checker_node