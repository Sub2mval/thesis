"""
Adapter between the GAIA runner and the llm_debate MAS
(langgraph_debate.py + error_injection.py). Every call is wrapped in
debate_usage.patched_call_llm so the returned trace always carries
per-call and per-trace token counts (see token_usage.py for the shape).
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional, Tuple



from llm_debate import gaia_utils  # noqa: E402
from llm_debate.langgraph_debate import run_debate  # noqa: E402
from llm_debate.error_injection import run_paired_fork_experiment  # noqa: E402

from . import token_usage
from .debate_usage import patched_call_llm


def _score(question: Dict[str, Any], answer_text: str) -> Dict[str, Any]:
    extracted = gaia_utils.extract_gaia_answer(answer_text or "")
    correct = gaia_utils.gaia_question_scorer(extracted, question.get("ground_truth"))
    return {"extracted_answer": extracted, "correct": correct}


def run_debate_baseline(
    question: Dict[str, Any],
    debate_config: Dict[str, Any],
    use_gricean_check: bool,
    agents_num: int = 3,
    rounds_num: int = 2,
) -> Dict[str, Any]:
    """Runs one GAIA question through the debate MAS once (checker on or
    off). Returns a self-contained trace: answer, GAIA score, tokens."""
    attachment = gaia_utils.load_attachment(question["file_path"]) if question.get("file_path") else None
    cfg = {**debate_config, "answer_format_instruction": gaia_utils.GAIA_ANSWER_FORMAT_INSTRUCTION}
    records: List[Dict[str, Any]] = []
    with patched_call_llm(records):
        result = run_debate(question["query"], cfg, agents_num=agents_num, rounds_num=rounds_num,
                             use_gricean_check=use_gricean_check, attachment=attachment)
    final_answer = result["final_answer"]
    return {
        "task_id": question["task_id"], "system": "llm_debate", "use_gricean_check": use_gricean_check,
        "final_answer": final_answer, **_score(question, final_answer),
        "token_stats": token_usage.summarize_calls(records),
        # Full per-agent conversation history, checker verdicts, and the
        # private-reflection audit log -- previously discarded, since
        # run_debate() used to return only the final_answer string.
        "contexts": result["contexts"], "adherence": result["adherence"], "reflections": result["reflections"],
    }


def run_debate_error_forks(
    question: Dict[str, Any],
    debate_config: Dict[str, Any],
    error_plan: List[Tuple[str, Optional[str]]],
    agents_num: int = 3,
    rounds_num: int = 2,
    strategy: str = "middle_agent_message",
) -> List[Dict[str, Any]]:
    """Runs one paired (checker-off vs. checker-on) fork experiment per
    (error_type, fm_id) in `error_plan` (see error_spec.resolve_error_plan),
    each pair sharing one identical corrupted message. Returns a flat list
    of two trace dicts (checker_off, checker_on) per fork.

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
    """
    attachment = gaia_utils.load_attachment(question["file_path"]) if question.get("file_path") else None
    cfg = {**debate_config, "answer_format_instruction": gaia_utils.GAIA_ANSWER_FORMAT_INSTRUCTION}
    results: List[Dict[str, Any]] = []
    for error_type, fm_id in error_plan:
        records: List[Dict[str, Any]] = []
        with patched_call_llm(records):
            result_off, result_on = asyncio.run(run_paired_fork_experiment(
                question["query"], cfg, question["query"], error_type, agents_num=agents_num,
                rounds_num=rounds_num, fm_id=fm_id, strategy=strategy, attachment=attachment,
            ))
        # Token usage covers both sides of this fork (they share one
        # baseline pass before diverging at the injection point), so the
        # same combined stats are attached to both result rows.
        stats = token_usage.summarize_calls(records)
        for label, result in (("checker_off", result_off), ("checker_on", result_on)):
            final_state = result["final_state"]
            answer = final_state.get("final_answer", "")
            results.append({
                "task_id": question["task_id"], "system": "llm_debate", "fork_condition": label,
                "error_type": result["error_type"], "fm_id": result["fm_id"], "fm_name": result["fm_name"],
                "final_answer": answer, **_score(question, answer), "token_stats": stats,
                # Where/what was corrupted, plus the full post-fork conversation state --
                # previously discarded here even though fork_trace_with_error already
                # returns all of it.
                "injected_at_step": result["injected_at_step"], "injected_at_agent_id": result["injected_at_agent_id"],
                "injected_at_position": result["injected_at_position"], "injected_at_node": result["injected_at_node"],
                "original_message": result["original_message"], "corrupted_message": result["corrupted_message"],
                "contexts": final_state["contexts"], "adherence": final_state["adherence"],
                "reflections": final_state["reflections"],
            })
    return results
