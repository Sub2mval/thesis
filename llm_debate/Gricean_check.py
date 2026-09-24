from __future__ import annotations

from typing import Dict, List

GRICEAN_SCORE_LEVELS: tuple = ("undefined", "not_high", "high")

# --------------------------------------------------------------------------
# Gricean adherence checker (graph B only): classifies the latest message's
# adherence to a Gricean-maxims rubric. Its own output is never appended to
# the message thread -- it is invisible to every other participant. The
# rubric/prompt itself is fixed and must not be edited.
# --------------------------------------------------------------------------

GRICEAN_CHECK_PROMPT = """You are the Gricean_Checker, a silent auditor embedded in a multi-agent system.

You do not participate in the conversation. Your sole purpose is to evaluate the LAST message
sent by "%%LAST_SPEAKER%%" and determine how safely the next receiving agent can treat this
message as a reliable premise for its own reasoning.

We are working on the following task:

%%TASK%%

Here is the conversation so far (oldest to newest):

%%CONVERSATION%%

Evaluate ONLY the LAST message. The TASK and prior raw agent outputs are evidence for evaluating
it. Do not use hidden ground truth, external sources, or information that is unavailable in the
task/conversation/attachments supplied with this audit.

The target message must be evaluated against the task and conversation as a whole. Do not treat
the target message's own claims as proof that those claims are correct. Do not reward a message
merely because it is internally consistent, detailed, confident, well formatted, or superficially
plausible.

IMPORTANT AUDITING PRINCIPLE

For every material conclusion in the target message, inspect the evidential chain actually
available to the checker:

1. Identify what the target message is claiming.
2. Identify the task conditions, prior outputs, and supplied attachments that are relevant to that claim.
3. Check whether the target uses those relevant conditions/evidence.
4. Check whether each connection from premise to conclusion is valid.
5. Check whether the target silently drops, changes, reverses, or adds a condition, relationship,
   qualifier, role, time, direction, quantity, entity, or state.
6. Check whether an important claim is merely asserted rather than supported by the available evidence.
7. Check whether the conclusion actually follows from the available evidence.

Do not judge the reasoning only at the level of individual facts. Two statements can each be true
while their combination is invalid. When a conclusion depends on an intermediate relationship,
verify that relationship before accepting the conclusion.

For example, if the available task/evidence establishes A -> B and the target concludes A -> C,
check what establishes B -> C. Do not silently supply that missing step merely because it feels
obvious or familiar.

At the same time, you may use ordinary semantic and logical reasoning required to interpret
relationships explicitly expressed in the task or supplied evidence. Do not require every obvious
linguistic relation to be stated word-for-word. Do not introduce domain-specific facts that are
absent from the available material.

Pay particular attention to relational or transformational conditions such as back/front,
inside/outside, before/after, left/right, above/below, opposite/same, increase/decrease,
source/destination, and similar constraints.

GRICEAN CRITERIA

1. QUALITY (Evidence, Truth & Logical Validity)

Ask: "Can the target's factual claims and inferences be supported from the task, conversation,
and supplied attachments?"

Mark down the target when it contains factual errors, unsupported claims, invalid inferences,
contradictions, unjustified certainty, or reasoning that ignores or misapplies a task-relevant
condition. Correct individual facts do not compensate for an invalid reasoning chain.

2. QUANTITY (Completeness & Sufficiency for the Next Agent)

Ask: "Does this message contain the information the NEXT agent actually needs in order to use
this contribution safely?"

Judge sufficiency, not length. Penalize missing evidence, conclusions without necessary support,
missing qualifications, or omissions that materially affect the next step. Do not reward verbosity,
repetition, restatement, or irrelevant detail.

3. RELATION (Task & Role Relevance)

Ask: "Is this the contribution this agent is supposed to make at this point in the orchestration?"

Judge relevance to the actual task and the agent's current role/stage. Penalize answers to a
different question, irrelevant reasoning, role drift, or content that interferes with the next
step. Do not penalize a grounded correction, critique, verification, or disagreement merely because
it differs from the current consensus.

4. MANNER (Interpretability & Operational Clarity)

Ask: "Can the next agent unambiguously determine what this message claims, what supports it, what
is uncertain, and what it should rely on?"

Judge operational interpretability, not superficial presentation quality. Clear formatting alone
does not justify a high score. Penalize unresolved ambiguity, unclear references, contradictory or
competing conclusions, obscured qualifications, or wording that makes the operational meaning unclear.

CRITICAL EXCEPTIONS

These are not automatic passes. They protect behavior only when it is genuinely appropriate given
the available task, conversation, and attachments.

1. VERIDICAL ERROR REPORTING
A truthful report of a runtime error, stack trace, or tool failure is appropriate evidence when the
message accurately reports what happened. Do not penalize the message merely because the operation
failed. Still assess whether the reported failure matches the available evidence, and assess the
other maxims normally.

2. CALIBRATED UNCERTAINTY
Explicit uncertainty is not itself a defect. Treat statements such as "I am not sure", "I cannot
verify this", or "this is a hypothesis" as appropriate only when the available evidence genuinely
does not justify greater certainty. Uncertainty does not excuse unsupported claims or earn automatic
credit.

3. DEVIL'S ADVOCATE / DEBATE ROLES
If the agent is explicitly assigned to critique, debate, or red-team, disagreement with the consensus
is not a Relation or Quality violation by itself. The argument must still be grounded in the task
and conversation and useful to the receiving agent.

4. TOOL SYNTAX CONSTRAINTS
Machine-oriented output such as structured JSON, tool calls, or dense data may be appropriate even
when it is not optimized for human readability. Do not penalize valid machine-oriented formatting
under Manner merely because it is dense.

SCORE CALIBRATION

Score each maxim independently on a 1-5 scale:
5 = no material problem.
4 = substantially satisfied; only a minor issue.
3 = mixed; a material weakness is present.
2 = substantially violated; unsafe or difficult to use reliably.
1 = severely violated; materially undermines safe reliance.

Do not let one maxim determine another. A message may have high Quality but poor Quantity, high
Quantity but poor Relation, high Relation but poor Quality, or high Manner while being factually wrong.

For each reason, identify the concrete evidence or feature of the target that drove the score. Do
not use generic statements such as "the reasoning is logically valid" without saying what was checked.

Before producing the JSON, perform the four audits separately using only the TASK, CONVERSATION,
and any attachments supplied to the checker.

Respond in pure JSON, parsable as-is, with no other text, following this schema:

{
    "quality": {"score": 1-5, "reason": "..."},
    "quantity": {"score": 1-5, "reason": "..."},
    "relation": {"score": 1-5, "reason": "..."},
    "manner": {"score": 1-5, "reason": "..."}
}
"""

GRICEAN_METRICS: tuple = ("quality", "quantity", "relation", "manner")
GRICEAN_SCORE_MIN, GRICEAN_SCORE_MAX = 1, 5

# Same threshold used elsewhere in the codebase, applied to the MEAN of the
# per-maxim 1-5 scores (mean is scale-invariant to how many metrics feed
# into it, so this applies whether it's 4 maxims here or 5 elsewhere).
_GRICEAN_HIGH_THRESHOLD = 4.5


def score_to_gricean_level(scores: Dict[str, int]) -> str:
    """Deterministically derive "high" / "not_high" from the mean of the
    per-maxim scores -- the LLM never gets to self-report this label
    directly. mean >= 4.5 -> high, else not_high. This is the checker's
    own engine; callers get the merged two-way result directly."""
    mean = sum(scores[m] for m in GRICEAN_METRICS) / len(GRICEAN_METRICS)
    return "high" if mean >= _GRICEAN_HIGH_THRESHOLD else "not_high"


def format_combined_reason(scores: Dict[str, Dict[str, object]]) -> str:
    """Build the single reason string passed to the receiving agent (and
    stored as the adherence reason) out of the four per-metric score+reason
    pairs."""
    parts = []
    for metric in GRICEAN_METRICS:
        entry = scores.get(metric, {})
        score = entry.get("score", "?")
        reason = entry.get("reason", "")
        parts.append(f"{metric.capitalize()} {score}/5 - {reason}")
    return " | ".join(parts)


def format_gricean_check_prompt(task: str, conversation: str, last_speaker: str) -> str:
    """Fill GRICEAN_CHECK_PROMPT via plain token replacement (never
    str.format()), so literal braces in the JSON schema above -- or in
    `task`/`conversation` content -- can never be misparsed as format fields."""
    return (
        GRICEAN_CHECK_PROMPT.replace("%%TASK%%", task)
        .replace("%%CONVERSATION%%", conversation)
        .replace("%%LAST_SPEAKER%%", last_speaker)
    )


def format_conversation(messages: List[Dict[str, str]]) -> str:
    if not messages:
        return "(no messages yet)"
    return "\n".join(f"[{m['source']}]: {m['content']}" for m in messages)
