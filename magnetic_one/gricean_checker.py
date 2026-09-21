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

--- Experiment-design branch (see repo-root experiment_design.py) ---
`state["experiment_design"]` (default "4", set once at run start --
see magnetic_one_langgraph.py) picks which scoring mechanism this node
runs on every turn. "1"/"2"/"3"/"4" ALL short-circuit to
`_run_trust_allocator_design` (Designs 1, 2, 3, and 4 are all
implemented), which calls the canonical Trust_Allocator (trust_allocator.
legacy_trust_allocator.allocate) and resolves its verdict via
experiment_design.resolve_design(). The old 4-axis Gricean adherence
checker block above/below is preserved in this file for unrelated legacy
compatibility, but the experiment-design paths no longer dispatch to it
for any of Designs 1-4.

`enable_gricean_check` IS the intervention switch here, same as in
llm_debate/langgraph_debate.py: when it's off, this is a BASELINE run
and the Trust_Allocator is never even called -- no notice, no
reflection, transient per-turn intervention state is cleared instead.
When it's on, the design's own resolved policy applies unconditionally
(e.g. Design 1's "always attach a notice, never reflect"; Design 3's
"no notice at all for HIGH, transparent delivery, no reflection"; or
Design 4's "no notice at all for HIGH, LOW notice for medium/low, PLUS
one private reflection for medium/low").

`pending_trust_level` carries the delivery-time notice forward, and
`pending_reflection` carries the (now possibly non-None, under Design 4)
private reflection forward -- see worker_agent.py / orchestrator_agent.py,
which already consume both generically.
"""

from __future__ import annotations

import logging
from typing import Optional

from autogen_core.models import ChatCompletionClient, UserMessage

from experiment_design import resolve_design
from magnetic_one.context_utils import get_compatible_context
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
from magnetic_one.json_llm import call_model_for_json
from magnetic_one.state import MagenticState
from trust_allocator.legacy_trust_allocator import (
    TRUST_ALLOCATOR_NODE_NAME,
    allocate as allocate_trust,
)
from trust_allocator import legacy_trust_allocator

logger = logging.getLogger("magentic_one_langgraph.gricean_checker")

# Used only by the legacy 4-axis checker below (_legacy_gricean_checker_node),
# which only needs recent context to judge local consistency, unlike the
# Orchestrator's own loop-detection, which needs the full history. The
# canonical Trust_Allocator branch (_run_trust_allocator_design) does NOT
# use this -- it is given the full message history, unwindowed.
ADHERENCE_CHECK_CONTEXT_WINDOW = 6

_TRUST_REFLECTION_PROMPT = """You are about to receive the following message from "%%SOURCE%%":

%%MESSAGE%%

The Trust_Allocator has flagged this message as LOW trust. Here is the allocator's reason:

%%REASON%%

This is a reflection loop, not a warning to just note and move past. Decide concretely how much
you should rely on this message and what, specifically, you will do differently as a result --
e.g. independently verifying a claim before acting on it, asking a clarifying question, or
flagging an inconsistency rather than assuming good faith. Keep this reflection to a few
sentences; it is for your own internal use only -- it will not be shown to any other agent and
will not become part of the shared conversation.
"""


def _trust_reflection_prompt(source: str, message: str, reason: str) -> str:
    """Design 4's private-reflection prompt: same delivery-time,
    single-use shape as gricean_check.format_reflection_prompt (never
    stored in state["messages"], never re-read into a later prompt), but
    worded in Trust_Allocator/trust terms rather than the old Gricean
    4-axis wording, since this branch is scored by the Trust_Allocator,
    not the Gricean checker."""
    return (
        _TRUST_REFLECTION_PROMPT.replace("%%SOURCE%%", source)
        .replace("%%MESSAGE%%", message)
        .replace("%%REASON%%", reason)
    )


async def _run_trust_allocator_design(
    gricean_client: ChatCompletionClient, state: MagenticState, messages, last, design: str
) -> MagenticState:
    """Design 1-4 branch: score the last message with the canonical
    Trust_Allocator instead of the Gricean 4-axis checker, then resolve
    the verdict via experiment_design.resolve_design(). Designs 1, 2, 3,
    and 4 are all implemented. Only called when enable_gricean_check is
    on -- see the baseline short-circuit in gricean_checker_node() below.

    Mirrors gricean_checker_node's own shape (history append, return
    dict) so the two branches stay easy to compare, but writes to
    `pending_trust_level` in addition to `pending_reflection`. Designs 1,
    2, and 3's policies are all reflect=False, unconditionally, so the
    reflection-generating gricean_client.create(...) call below is never
    reached for them; Design 4's policy is reflect=True for medium/low
    verdicts, which is what actually exercises that call.

    Unlike the legacy 4-axis checker below (which only needs recent
    context to judge local consistency, hence ADHERENCE_CHECK_CONTEXT_
    WINDOW), the canonical Trust_Allocator gets the FULL message history
    -- no windowing -- so its verdict can be grounded in everything said
    so far, not just the last few turns.
    """
    conversation = legacy_trust_allocator.format_conversation(
        [{"source": m["source"], "content": m["content"]} for m in messages]
    )

    async def _client(prompt: str) -> str:
        # call_type="trust_allocator" (PART 6) -- extra_create_args is a
        # normal, already-supported create() parameter; the instrumented
        # client pops this label out before forwarding the rest to the
        # real Ollama API (see ollama_client.py's module docstring).
        response = await gricean_client.create(
            get_compatible_context(
                gricean_client, [UserMessage(content=prompt, source=TRUST_ALLOCATOR_NODE_NAME)]
            ),
            extra_create_args={"call_type": "trust_allocator"},
        )
        assert isinstance(response.content, str)
        return response.content

    verdict = await allocate_trust(state["task"], conversation, last["source"], _client)
    raw_trust_level = verdict["trust_level"]
    reason = verdict["reason"]
    policy = resolve_design(raw_trust_level, design)

    gricean_history = list(state.get("gricean_history", [])) + [
        {
            "step": state.get("n_rounds", 0),
            "message_index": len(messages) - 1,
            "evaluated_source": last["source"],
            "adherence_level": raw_trust_level,  # the allocator's raw verdict, not a Gricean level
            "reason": reason,
            "score": verdict["score"],
            "scores": verdict["scores"],
            "experiment_design": design,
            "notice_applied": policy["notice"],
            "reflect": policy["reflect"],
        }
    ]

    pending_reflection: Optional[str] = None
    reflection_history = list(state.get("reflection_history", []))
    if policy["reflect"]:
        # Not reachable under Designs 1, 2, or 3 (reflect is always False
        # for all three); reachable under Design 4 for medium/low
        # verdicts, which is the one private reflection Design 4 adds on
        # top of Design 3's notice policy.
        receiving_agent = state["next_after_check"]
        reflection_prompt = _trust_reflection_prompt(last["source"], last["content"], reason)
        # call_type="reflection" (PART 7) -- same instrumentation-only
        # label mechanism as the trust_allocator call above.
        response = await gricean_client.create(
            get_compatible_context(gricean_client, [UserMessage(content=reflection_prompt, source=receiving_agent)]),
            extra_create_args={"call_type": "reflection"},
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
        "adherence_level": raw_trust_level,
        "adherence_reason": reason,
        "adherence_scores": {},
        "gricean_history": gricean_history,
        "pending_reflection": pending_reflection,
        "reflection_history": reflection_history,
        "pending_trust_level": policy["notice"],
    }


def build_gricean_checker_node(gricean_client: ChatCompletionClient):
    async def gricean_checker_node(state: MagenticState) -> MagenticState:
        messages = state["messages"]
        if not messages:
            return {**state, "adherence_level": "high", "pending_reflection": None, "pending_trust_level": None}

        design = state.get("experiment_design", "4")
        last = messages[-1]

        # Designs 1-4 all route through the canonical Trust_Allocator +
        # experiment_design.resolve_design(); the old 4-axis Gricean
        # scoring path below is no longer dispatched to for any of them.
        if not state.get("enable_gricean_check", True):
            # Baseline: enable_gricean_check is the intervention switch
            # for Trust_Allocator designs. Do NOT call the canonical
            # Trust_Allocator on a baseline run -- just clear any
            # transient intervention state left over from a prior turn
            # and continue normally. gricean_history is intentionally
            # left untouched (no entry logged for this baseline turn),
            # matching the equivalent baseline branch in
            # llm_debate/langgraph_debate.py.
            return {
                **state,
                "adherence_level": None,
                "adherence_reason": None,
                "adherence_scores": {},
                "pending_reflection": None,
                "pending_trust_level": None,
            }
        return await _run_trust_allocator_design(gricean_client, state, messages, last, design)

    return gricean_checker_node


# --------------------------------------------------------------------------
# Old 4-axis Gricean adherence checker. No experiment-design path (1-4)
# dispatches to this anymore -- see build_gricean_checker_node above and
# the module docstring. Left here, unused, for unrelated legacy
# compatibility only.
# --------------------------------------------------------------------------


async def _legacy_gricean_checker_node(gricean_client: ChatCompletionClient, state: MagenticState) -> MagenticState:
    messages = state["messages"]
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
        "pending_trust_level": None,
    }
