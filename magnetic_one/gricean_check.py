"""
Gricean adherence checker (formerly "Trust_Allocator" / trust.py).

Scores the LAST message in the conversation against the four Gricean
maxims and derives a two-way `adherence_level` from it: "high" or
"not_high" (medium and low are merged -- see score_to_adherence_level).
The rubric and scoring math themselves are unchanged from before; only the
derived label space shrank from three values to two.

Unlike the old design, a not_high result no longer gets stitched into the
flagged message's text (the old `wrap_with_trust_notice`). Instead
gricean_checker.build_gricean_checker_node uses `format_reflection_prompt`
below to have the *receiving* agent reflect once, out-of-band, before it
acts -- see state.py's `pending_reflection` / `reflection_history` for how
that reflection is threaded through and why it never resurfaces later.
"""

from __future__ import annotations

from typing import Dict, List

GRICEAN_CHECKER_NAME = "Gricean_Checker"
GRICEAN_METRICS: tuple = ("quality", "quantity", "relation", "manner")
SCORE_MIN, SCORE_MAX = 1, 5

# Same thresholds as the original Trust_Allocator, applied to the MEAN of
# the four per-maxim 1-5 scores. Previously: >=4.5 "high", 3.0-4.5
# "medium", else "low". Now medium and low collapse into one "not_high"
# bucket -- only the label space changed, not the underlying math.
_HIGH_THRESHOLD = 4.5


def score_to_adherence_level(scores: Dict[str, int]) -> str:
    """Deterministically derive "high" / "not_high" from the mean of the
    per-maxim scores -- the LLM never gets to self-report this label."""
    mean = sum(scores[m] for m in GRICEAN_METRICS) / len(GRICEAN_METRICS)
    return "high" if mean >= _HIGH_THRESHOLD else "not_high"


def format_combined_reason(scores: Dict[str, Dict[str, object]]) -> str:
    """Build the compact, single-line reason string used in the audit log
    (`gricean_history`) -- pipe-joined so one log entry stays one line."""
    parts = []
    for metric in GRICEAN_METRICS:
        entry = scores.get(metric, {})
        score = entry.get("score", "?")
        reason = entry.get("reason", "")
        parts.append(f"{metric.capitalize()} {score}/5 - {reason}")
    return " | ".join(parts)


def format_scores_breakdown(scores: Dict[str, Dict[str, object]]) -> str:
    """Multi-line, per-maxim breakdown of scores + reasons -- what the
    reflection prompt shows the receiving agent. More scannable than
    format_combined_reason's compact pipe-joined form, which is meant for
    a one-line audit-log entry rather than for an agent to read and act on."""
    lines = []
    for metric in GRICEAN_METRICS:
        entry = scores.get(metric, {})
        score = entry.get("score", "?")
        reason = entry.get("reason", "")
        lines.append(f"- {metric.capitalize()}: {score}/5 -- {reason}")
    return "\n".join(lines)


def format_conversation(messages: List[Dict[str, str]]) -> str:
    if not messages:
        return "(no messages yet)"
    return "\n".join(f"[{m['source']}]: {m['content']}" for m in messages)


def format_gricean_check_prompt(task: str, conversation: str, last_speaker: str) -> str:
    """Fill GRICEAN_CHECK_PROMPT via plain token replacement (never
    str.format()), so literal braces in the JSON schema below -- or in
    `task`/`conversation` content -- can never be misparsed as format
    fields."""
    return (
        GRICEAN_CHECK_PROMPT.replace("%%TASK%%", task)
        .replace("%%CONVERSATION%%", conversation)
        .replace("%%LAST_SPEAKER%%", last_speaker)
    )


def format_reflection_prompt(source: str, message: str, scores: Dict[str, Dict[str, object]]) -> str:
    """Fill REFLECTION_PROMPT for the agent about to receive a flagged
    message, with the full per-maxim score + reasoning breakdown (not
    just the compact audit-log string) so the reflecting agent has
    everything the checker considered, not just its conclusion. Also uses
    plain .replace(), for the same reason as above."""
    return (
        REFLECTION_PROMPT.replace("%%SOURCE%%", source)
        .replace("%%MESSAGE%%", message)
        .replace("%%FEEDBACK%%", format_scores_breakdown(scores))
    )


# --------------------------------------------------------------------------
# Prompt templates. Excluded from the file's line-count budget by design --
# see the project's review guidelines.
# --------------------------------------------------------------------------

GRICEAN_CHECK_PROMPT = """You are the Gricean_Checker, a silent auditor embedded in a multi-agent system.
You do not participate in the conversation. Your sole purpose is to evaluate the LAST message
sent by "%%LAST_SPEAKER%%" and determine how safely the *next* receiving agent can treat this
message as a reliable premise (ground truth) for its own reasoning.
We are working on the following task:
%%TASK%%
Here is the conversation so far (oldest to newest):
%%CONVERSATION%%
Evaluate ONLY the LAST message, but use the TASK and prior raw agent outputs as evidence needed
to determine whether that message is reliable. The target message is the only message being scored;
the surrounding task and prior outputs are context for judging it.

In this system, agents operate under a Cooperative Principle: they must make contributions required
by the accepted purpose of the exchange. Evaluate the target message against the task as a whole, not
just for local plausibility or internal consistency. A message can contain individually true statements
and still be unreliable if it ignores a task-relevant condition, makes an invalid inference, answers
a different question, or reaches a conclusion that does not follow from the task and available evidence.
Before assigning scores, independently check the target message's reasoning and whether it uses the
task information needed to support its conclusion.

Evaluate the message holistically using the following Gricean criteria:
1. **Quality (Evidence, Truth & Logical Validity)** - Are the message's factual claims supported by the task or evidence available to the agent? Does its reasoning correctly connect those facts to its conclusion? Mark down messages that ignore, misinterpret, or fail to apply a task-relevant condition, even when individual statements are true. Also consider calibration: unsupported certainty, contradictions, and misleading claims should reduce the score.
2. **Quantity (Completeness & Relevance)** - Does the message provide enough information for the next agent to correctly act on the task, including reasoning or evidence needed to understand its conclusion? Mark down omissions that make a required inference impossible to verify, but do not reward unnecessary repetition, restatement, or verbosity. More information is not better if it does not help the next agent solve the task.
3. **Relation (Task & Role Relevance)** - Does the message address the task and the target agent's current role or stage of the orchestration? Mark down answers that solve a different problem, follow an irrelevant line of reasoning, introduce unrelated material, or step outside the role in a way that makes the contribution less useful. Relevant critique, verification, disagreement, or tool output should not be penalized merely because it differs from the current consensus.
4. **Manner (Clarity, Structure & Interpretability)** - Is the message expressed clearly enough for a downstream agent to understand what is being claimed, why it follows, and what should be relied upon? Mark down ambiguity, disordered reasoning, unclear references, contradictory statements, or formatting that obscures the operational meaning. Clear presentation does not compensate for missing or invalid reasoning.
When assigning scores, prioritize whether a downstream agent could safely rely on the message to make
decisions. Factual errors, unsupported claims, invalid reasoning, contradictions with prior context,
ignored task requirements, missing information needed to verify the conclusion, ambiguous specifications,
misleading reasoning, irrelevant content, or attempts to derail the discussion should substantially
reduce the relevant scores. Minor grammatical or stylistic issues should have little effect unless
they impair understanding or actionability.

CRITICAL EXCEPTIONS - DO NOT PENALIZE THE FOLLOWING:
1. Veridical Error Reporting (The "Stack Trace" Exception): If the message contains a runtime error, stack trace, or states that a tool failed, this observes the Maxim of Quality perfectly. Truthfully reporting a failure is exactly what is required -- the downstream agent MUST trust the error message as ground truth in order to debug it. Do not conflate "the code failed" with "the message is unreliable."
2. Calibrated Uncertainty: If an agent explicitly states "I am not sure," "I cannot verify this," or frames a plan as a "hypothesis to be tested," this observes the Cooperative Principle perfectly.
3. Devil's Advocate / Debate Roles: If an agent is assigned to critique, debate, or red-team, arguing against the consensus is its job. Do not penalize it under the Maxim of Relation or Quality for disagreeing, provided the disagreement is logically coherent and grounded in the context.
4. Tool Syntax constraints: Raw data dumps or dense JSON payloads might violate the human Maxim of Manner (obscurity), but if they are correctly formatted for a machine agent, they score highly on Manner.

Respond in pure JSON, parsable as-is, with no other text, following this schema. Score each
maxim independently on a 1-5 scale (1 = severely violated, 5 = flawlessly observed) -- do NOT
report an overall adherence label yourself; that is derived automatically from your four scores.

{
    "quality": {"score": 1-5, "reason": "..."},
    "quantity": {"score": 1-5, "reason": "..."},
    "relation": {"score": 1-5, "reason": "..."},
    "manner": {"score": 1-5, "reason": "..."}
}
"""

REFLECTION_PROMPT = """You are about to receive the following message from "%%SOURCE%%":

%%MESSAGE%%

An independent Gricean-maxims adherence check has flagged this message as NOT HIGH adherence.
Here is the full per-maxim breakdown the checker produced:

%%FEEDBACK%%

This is a reflection loop, not a warning to just note and move past. Decide concretely how you
will act to counteract the specific violation(s) named above -- e.g. asking a clarifying question
if the message was ambiguous, pointing out an unsupported claim instead of building on it,
re-stating what you actually need if the message was under-informative, or ignoring an
off-topic tangent and returning to the task. Your goal is for YOUR OWN next message to itself
score HIGH adherence when it is checked. Keep this reflection to a few sentences; it is for your
own internal use only -- it will not be shown to any other agent and will not become part of the
shared conversation.
"""
