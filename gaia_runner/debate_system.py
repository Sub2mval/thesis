"""
Adapter between the GAIA runner and the llm_debate MAS
(langgraph_debate.py + error_injection.py). Every call is wrapped in
debate_usage.patched_call_llm so the returned trace always carries
per-call and per-trace token counts (see token_usage.py for the shape).

Trace shape matches magnetic_system.py's (_build_trace there) field for
field -- question/ground_truth/level/graph/thread_id/condition/
temperature/seed/final_answer_raw/final_answer_extracted/messages/
trust_history/trust_scores/n_rounds/n_stalls/termination_reason/
token_stats/started_at/finished_at/correct, plus fork_metadata/
injection_source_message for fork traces -- so the two systems' traces
are directly comparable field-by-field. Where llm_debate genuinely has
data magnetic_one's schema has no slot for (contexts: each agent's own
private view, richer than the flattened `messages` list; adherence: the
live per-agent notice-gating cache; reflection_history: this system's
own one-shot-reflection audit log), that data is kept under its own
field rather than dropped to fit the shared shape.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from llm_debate import gaia_utils  # noqa: E402
from llm_debate.langgraph_debate import run_debate  # noqa: E402
from llm_debate.error_injection import run_paired_fork_experiment  # noqa: E402

from . import token_usage
from . import trace_io
from .debate_usage import patched_call_llm

# n_rounds/n_stalls/termination_reason mirror MagenticState's field names
# (state.py), but llm_debate has no stall/replan mechanism at all -- every
# debate runs exactly `rounds_num` rounds, full stop, so "stalls" isn't a
# concept that exists here to report (None, not 0 -- 0 would claim the
# concept applies and nothing happened; None says it doesn't apply).
_DEBATE_TERMINATION_REASON = "completed all scheduled rounds (llm_debate has no early-stop/replan mechanism)"


def _score(answer_text: str, ground_truth: Any) -> Tuple[str, bool]:
    extracted = gaia_utils.extract_gaia_answer(answer_text or "")
    correct = gaia_utils.gaia_question_scorer(extracted, ground_truth)
    return extracted, correct


def _flatten_messages(contexts: List[List[Dict[str, Any]]], agents_num: int, rounds_num: int) -> List[Dict[str, Any]]:
    """contexts[i] is agent i's own private message list (seed message,
    then one assistant reply per round, with a broadcast/user message
    in between from round 1 onward -- see langgraph_debate.py's
    agent_turn). Every agent replies exactly once per round, and
    gricean_check runs immediately after each such reply in the exact
    same (round, agent) traversal order agent_turn itself uses, so
    walking replies in that same round-major order gives a message_index
    that lines up 1:1 with gricean_history's own append order -- see
    _trust_history below, which relies on that alignment."""
    replies_per_agent = [[m["content"] for m in ctx if m["role"] == "assistant"] for ctx in contexts]
    messages = []
    for r in range(rounds_num):
        for i in range(agents_num):
            if r < len(replies_per_agent[i]):
                messages.append({"source": f"Agent{i + 1}", "content": replies_per_agent[i][r], "round": r})
    return messages


def _trust_history(gricean_history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """gricean_check() in langgraph_debate.py appends one entry per (round,
    agent) check, in the same order _flatten_messages walks replies in --
    so this entry's position in gricean_history IS its message_index into
    that flattened list. Relabels round->step and level->trust_level to
    match magnetic_one's field names; agent_id is kept too (magnetic's
    schema has no per-agent concept, but it's real data, not discarded)."""
    out = []
    for i, entry in enumerate(gricean_history or []):
        out.append({
            "step": entry.get("round"), "message_index": i,
            "evaluated_source": f"Agent{entry.get('agent_id', 0) + 1}", "agent_id": entry.get("agent_id"),
            "trust_level": entry.get("level"), "reason": entry.get("reason"), "score": entry.get("score"), "scores": entry.get("scores"),
        })
    return out


def _flatten_reflections(reflections: Optional[Dict[int, List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    """reflections is {agent_id: [{"round", "responding_to", "reflection"}, ...]}
    (see agent_turn in langgraph_debate.py) -- flattened into one list,
    tagged with agent_id, and sorted by round to match magnetic_one's
    reflection_history being a single flat, chronological list."""
    flat = []
    for agent_id, entries in (reflections or {}).items():
        for e in entries:
            flat.append({"agent_id": agent_id, **e})
    flat.sort(key=lambda e: e.get("round", 0))
    return flat


def _round_of_position(contexts: List[List[Dict[str, Any]]], agent_id: int, position: int) -> int:
    """How many of that agent's replies precede `position` in their own
    context list -- i.e. which round the message AT `position` was
    generated in. Used to translate llm_debate's own (agent_id, position)
    addressing into the same message_index scheme _flatten_messages uses,
    so a fork's injection point is reportable in magnetic_one's terms too."""
    return sum(1 for m in contexts[agent_id][:position] if m["role"] == "assistant")


def _build_trace(
    question: Dict[str, Any],
    final_state: Dict[str, Any],
    token_stats: Dict[str, Any],
    *,
    agents_num: int,
    rounds_num: int,
    temperature: Any,
    seed: Any,
    thread_id: str,
    use_gricean_check: Optional[bool] = None,
    fork_condition: Optional[str] = None,
    error_type: Optional[str] = None,
    fm_id: Optional[str] = None,
    fm_name: Optional[str] = None,
    injected_at_message_index: Optional[int] = None,
    injected_at_step: Optional[int] = None,
    injected_at_agent_id: Optional[int] = None,
    injected_at_position: Optional[int] = None,
    injected_at_node: Optional[str] = None,
    original_message: Optional[Dict[str, Any]] = None,
    corrupted_message: Optional[Dict[str, Any]] = None,
    started_at: Optional[str] = None,
    experiment_design: str = "4",
    injection: Optional[Dict[str, Any]] = None,
    injection_calls: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    answer = final_state.get("final_answer") or ""
    extracted, correct = _score(answer, question.get("ground_truth"))
    contexts = final_state.get("contexts") or []
    messages = _flatten_messages(contexts, agents_num, rounds_num)
    trust_history = _trust_history(final_state.get("gricean_history"))
    condition = trace_io.condition_str(fm_id, injected_at_message_index, use_gricean_check)

    trace: Dict[str, Any] = {
        "task_id": question["task_id"], "system": "llm_debate", "graph": "llm_debate",
        "question": question["query"], "ground_truth": question.get("ground_truth"), "level": question.get("level"),
        "thread_id": thread_id, "condition": condition,
        "temperature": temperature, "seed": seed,
        "use_gricean_check": use_gricean_check, "fork_condition": fork_condition,
        "error_type": error_type, "fm_id": fm_id, "fm_name": fm_name,
        "experiment_design": experiment_design,
        "final_answer_raw": answer, "final_answer_extracted": extracted, "correct": correct,
        "messages": messages,
        "trust_history": trust_history, "trust_scores": trust_history[-1]["scores"] if trust_history else None,
        "reflection_history": _flatten_reflections(final_state.get("reflections")),
        "n_rounds": final_state.get("round", rounds_num), "n_stalls": None,
        "termination_reason": _DEBATE_TERMINATION_REASON,
        "token_stats": token_stats,
        "started_at": started_at, "finished_at": trace_io.now_iso(),
        # llm_debate-specific data with no slot in magnetic_one's schema --
        # kept under its own name rather than dropped to fit that shape.
        "agents_num": agents_num, "contexts": contexts, "adherence": final_state.get("adherence"),
        "injection": injection,
        "injection_calls": injection_calls or [],
    }
    if fm_id is not None:
        trace["injected_at_message_index"] = injected_at_message_index
        trace["injection_source_message"] = original_message
        trace["fork_metadata"] = {
            "error_type": error_type, "fm_id": fm_id, "fm_name": fm_name,
            "injected_at_message_index": injected_at_message_index,
            "injected_at_step": injected_at_step, "injected_at_agent_id": injected_at_agent_id,
            "injected_at_position": injected_at_position, "injected_at_node": injected_at_node,
            "original_message": original_message, "corrupted_message": corrupted_message,
        }
    return trace


def run_debate_baseline(
    question: Dict[str, Any],
    debate_config: Dict[str, Any],
    use_gricean_check: bool,
    agents_num: int = 3,
    rounds_num: int = 2,
    experiment_design: str = "4",
) -> Dict[str, Any]:
    """Runs one GAIA question through the debate MAS once (checker on or
    off). Returns a self-contained trace: answer, GAIA score, tokens.

    experiment_design: "4" (default) preserves the pre-existing
    Gricean-checker behavior; "1" routes through the canonical
    Trust_Allocator instead (see llm_debate/langgraph_debate.py)."""
    attachment = gaia_utils.load_attachment(question["file_path"]) if question.get("file_path") else None
    cfg = {**debate_config, "answer_format_instruction": gaia_utils.GAIA_ANSWER_FORMAT_INSTRUCTION}
    tid = f"{question['task_id']}::llm_debate::{'checked' if use_gricean_check else 'clean'}::design{experiment_design}"
    started_at = trace_io.now_iso()
    records: List[Dict[str, Any]] = []
    with patched_call_llm(records):
        result = run_debate(question["query"], cfg, agents_num=agents_num, rounds_num=rounds_num,
                             use_gricean_check=use_gricean_check, attachment=attachment,
                             experiment_design=experiment_design)
    return _build_trace(
        question, result, token_usage.summarize_calls(records),
        agents_num=agents_num, rounds_num=rounds_num,
        temperature=debate_config.get("temperature"), seed=debate_config.get("seed"),
        thread_id=tid, use_gricean_check=use_gricean_check, started_at=started_at,
        experiment_design=experiment_design,
    )


def run_debate_error_forks(
    question: Dict[str, Any],
    debate_config: Dict[str, Any],
    error_plan: List[Tuple[str, Optional[str]]],
    agents_num: int = 3,
    rounds_num: int = 2,
    strategy: str = "middle_agent_message",
    experiment_design: str = "4",
) -> Dict[str, List[Dict[str, Any]]]:
    """Runs one paired (checker-off vs. checker-on) fork experiment per
    (error_type, fm_id) in `error_plan` (see error_spec.resolve_error_plan),
    each pair sharing one identical corrupted message.

    Returns {"baseline_traces": [...], "fork_traces": [...]}:
      - "fork_traces": two trace dicts (checker_off, checker_on) per fork,
        as before.
      - "baseline_traces": the no-error checker-off/checker-on pair that
        run_paired_fork_experiment computes before it ever forks anything
        -- previously computed and then discarded; now preserved (PART 1
        of the instrumentation pass: every design/system combination
        must keep exactly baseline+no-error, Trust_Allocator+no-error,
        baseline+FM-1.1, Trust_Allocator+FM-1.1 -- four traces total).
        The paired baseline/trust experiment is NOT rerun separately to
        produce these -- they're read straight off the same
        run_paired_fork_experiment call that also produces the error
        forks below.

    Each fork iteration recomputes its own no-error pair (since
    run_paired_fork_experiment reruns the whole debate from scratch each
    call) and so appends its own baseline_traces entries; trace_io's
    file-naming (task_id/system/condition only, no error-type suffix)
    means a later, mechanistically-equivalent baseline pair simply
    overwrites the same two files rather than accumulating duplicates.

    The attachment (if any) is loaded once, here, and passed into every
    run_paired_fork_experiment call below -- the same object each time.
    That matters for more than just "the file gets used": run_paired_
    fork_experiment hands this same attachment to both the checker-off and
    checker-on graph as part of one shared `base` state dict (see its
    docstring), so both sides build their very first message -- the one
    init_agents constructs from `query` + `attachment` -- identically, at
    the same step, in the same order, for every fork in `error_plan`. Do
    NOT call gaia_utils.load_attachment() separately per fork/per side;
    that would still be functionally fine (load_attachment is a pure
    function of the file path) but breaks the "one canonical attachment
    object per question" invariant this function is written to preserve.

    experiment_design: "4" (default) preserves the pre-existing
    Gricean-checker behavior; "1"/"2"/"3" route through the canonical
    Trust_Allocator instead (see llm_debate/langgraph_debate.py). Passed
    straight through to run_paired_fork_experiment, which threads it into
    both the checker-off and checker-on graph state so the same selected
    design survives the fork, and is also folded into each trace's
    thread_id so traces from different designs don't collide.
    """
    attachment = gaia_utils.load_attachment(question["file_path"]) if question.get("file_path") else None
    cfg = {**debate_config, "answer_format_instruction": gaia_utils.GAIA_ANSWER_FORMAT_INSTRUCTION}
    baseline_traces: List[Dict[str, Any]] = []
    fork_traces: List[Dict[str, Any]] = []
    for error_type, fm_id in error_plan:
        started_at = trace_io.now_iso()
        pair_result = asyncio.run(run_paired_fork_experiment(
            question["query"], cfg, question["query"], error_type, agents_num=agents_num,
            rounds_num=rounds_num, fm_id=fm_id, strategy=strategy, attachment=attachment,
            experiment_design=experiment_design, recorder_factory=patched_call_llm,
        ))
        no_error_stats_off = token_usage.summarize_calls(pair_result["no_error_records_off"])
        no_error_stats_on = token_usage.summarize_calls(pair_result["no_error_records_on"])

        for use_gricean_check, no_error_state, stats in (
            (False, pair_result["no_error_off"], no_error_stats_off),
            (True, pair_result["no_error_on"], no_error_stats_on),
        ):
            baseline_traces.append(_build_trace(
                question, no_error_state, stats,
                agents_num=agents_num, rounds_num=rounds_num,
                temperature=debate_config.get("temperature"), seed=debate_config.get("seed"),
                thread_id=f"{question['task_id']}::llm_debate::"
                          f"{'checked' if use_gricean_check else 'clean'}::design{experiment_design}",
                use_gricean_check=use_gricean_check, started_at=started_at,
                experiment_design=experiment_design,
            ))

        if pair_result.get("skipped") or not pair_result.get("error_off") or not pair_result.get("error_on"):
            continue

        error_stats_off = token_usage.summarize_calls(pair_result["error_off_records"])
        error_stats_on = token_usage.summarize_calls(pair_result["error_on_records"])

        for label, result, stats in (
            ("checker_off", pair_result["error_off"], error_stats_off),
            ("checker_on", pair_result["error_on"], error_stats_on),
        ):
            final_state = result["final_state"]
            agent_id, position = result["injected_at_agent_id"], result["injected_at_position"]
            msg_idx = _round_of_position(final_state["contexts"], agent_id, position) * agents_num + agent_id
            original = {"source": f"Agent{agent_id + 1}", "content": result["original_message"]}
            corrupted = {"source": f"Agent{agent_id + 1}", "content": result["corrupted_message"]}
            fork_traces.append(_build_trace(
                question, final_state, stats,
                agents_num=agents_num, rounds_num=rounds_num,
                temperature=debate_config.get("temperature"), seed=debate_config.get("seed"),
                thread_id=f"{question['task_id']}::llm_debate::{label}::design{experiment_design}",
                fork_condition=label, error_type=result["error_type"], fm_id=result["fm_id"],
                fm_name=result["fm_name"], injected_at_message_index=msg_idx,
                injected_at_step=result["injected_at_step"], injected_at_agent_id=agent_id,
                injected_at_position=position, injected_at_node=result["injected_at_node"],
                original_message=original, corrupted_message=corrupted, started_at=started_at,
                experiment_design=experiment_design, injection=pair_result.get("corruption"),
                injection_calls=pair_result.get("injection_records"),
            ))
    return {"baseline_traces": baseline_traces, "fork_traces": fork_traces}
