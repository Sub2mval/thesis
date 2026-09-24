"""
Adapter between the GAIA runner and the magnetic_one MAS
(magnetic_one_langgraph.py + paired_fork.py + error_injection.py).
Token usage is already tracked inside magnetic_one itself (ollama_client.py/
ollama_cloud_client.py's usage_stats()) in a rich, per-call shape; this
module now passes that shape straight through into the trace (see
_combined_usage) rather than lossily normalizing it, and builds the rest
of each trace to match the project's established trace schema (see
_build_trace) -- GAIA scoring / attachment handling live here too.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple


from . import trace_io  # noqa: E402
from llm_debate import gaia_utils  # noqa: E402
from magnetic_one.magnetic_one_langgraph import MagenticOneLangGraph  # noqa: E402
from magnetic_one.prompts import ORCHESTRATOR_FINAL_ANSWER_PROMPT  # noqa: E402
from magnetic_one.context_utils import ORCHESTRATOR_NAME  # noqa: E402
from magnetic_one.ollama_client import (  # noqa: E402
    get_usage_tracking, reset_usage_tracking, DEFAULT_TEMPERATURE, DEFAULT_SEED,
)
from magnetic_one.paired_fork import fork_paired_traces_with_error, run_paired_traces, shared_prefix_length  # noqa: E402
from magnetic_one.error_injection import list_message_checkpoints  # noqa: E402

# Ask the orchestrator to end its final answer in GAIA's own convention,
# so gaia_utils.extract_gaia_answer can parse it the same way it does for
# the debate MAS's output.
GAIA_FINAL_ANSWER_PROMPT = ORCHESTRATOR_FINAL_ANSWER_PROMPT + "\n" + gaia_utils.GAIA_ANSWER_FORMAT_INSTRUCTION


def build_magnetic_system(
    model: str, gricean_model: Optional[str], host: str,
    api_keys: Optional[List[str]] = None, **kwargs: Any,
) -> MagenticOneLangGraph:
    """Pass `api_keys` (e.g. loaded from OLLAMA_API_KEY_1..N) to route through
    Ollama Cloud with key rotation instead of a single local/cloud client."""
    if api_keys:
        return MagenticOneLangGraph.from_ollama_cloud(
            model=model, api_keys=api_keys, host=host, gricean_model=gricean_model,
            final_answer_prompt=GAIA_FINAL_ANSWER_PROMPT, **kwargs,
        )
    return MagenticOneLangGraph.from_ollama(
        model=model, host=host, gricean_model=gricean_model,
        final_answer_prompt=GAIA_FINAL_ANSWER_PROMPT, **kwargs,
    )


def _task_text(question: Dict[str, Any]) -> str:
    """magnetic_one's agents (FileSurfer in particular) take a plain task
    string, not a separate attachment payload like llm_debate's Ollama
    client accepts -- so a local attachment is surfaced as a file path in
    the task text for FileSurfer to open, rather than loaded/encoded here."""
    text = question["query"]
    if question.get("file_path"):
        text += f"\n\nAn attached file for this task is available at: {question['file_path']}"
    return text


def _score(answer_text: str, ground_truth: Any) -> Tuple[str, bool]:
    extracted = gaia_utils.extract_gaia_answer(answer_text or "")
    correct = gaia_utils.gaia_question_scorer(extracted, ground_truth)
    return extracted, correct


def _now_iso() -> str:
    return trace_io.now_iso()


def _condition_str(fm_id: Optional[str], injected_at_message_index: Optional[int], use_gricean_check: Optional[bool]) -> str:
    """Thin wrapper kept for local readability -- see trace_io.condition_str
    for the actual (shared-with-debate_system.py) implementation."""
    return trace_io.condition_str(fm_id, injected_at_message_index, use_gricean_check)


def _trust_history(gricean_history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Same per-message log gricean_checker.py already builds (step,
    message_index, evaluated_source, reason, scores) -- just relabels
    adherence_level as trust_level, since that's the field name used
    consistently elsewhere in this project's trace schema."""
    out = []
    for entry in gricean_history or []:
        e = dict(entry)
        e["trust_level"] = e.pop("adherence_level", None)
        out.append(e)
    return out


def _combined_usage(magentic: MagenticOneLangGraph) -> Dict[str, Any]:
    """usage_stats() on both InstrumentedOllamaChatCompletionClient (local)
    and RotatingKeyOllamaClient (cloud) already returns the full shape this
    project's traces use directly -- n_llm_calls/n_successful_calls/
    n_failed_attempts/prompt_tokens/completion_tokens/total_tokens/calls
    (each call carrying started_at_unix/elapsed_seconds/input_message_count/
    input_sources/status, plus client_key_index when using Ollama Cloud's
    rotation). token_stats is only assembled automatically inside
    MagenticOneLangGraph.run(); fork continuations below call
    graph.ainvoke() directly (as paired_fork.py does), bypassing that
    wrapper, so usage is read off the instrumented client(s) and combined
    by hand here instead -- kept in this same rich shape, not reduced to a
    smaller common schema."""
    calls: List[Dict[str, Any]] = list(get_usage_tracking(magentic.client).get("calls", []))
    if magentic.gricean_model_client is not magentic.client:
        calls += list(get_usage_tracking(magentic.gricean_model_client).get("calls", []))
    calls.sort(key=lambda r: (r.get("started_at_unix", 0), r.get("call_index", 0)))
    prompt_tokens = sum(c.get("prompt_tokens") or 0 for c in calls)
    completion_tokens = sum(c.get("completion_tokens") or 0 for c in calls)
    return {
        "n_llm_calls": len(calls),
        "n_successful_calls": sum(c.get("status") == "ok" for c in calls),
        "n_failed_attempts": sum(c.get("status") == "error" for c in calls),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "total_elapsed_seconds": sum(c.get("elapsed_seconds") or 0 for c in calls),
        "calls": calls,
    }


def _build_trace(
    question: Dict[str, Any],
    final_state: Dict[str, Any],
    token_stats: Dict[str, Any],
    *,
    thread_id: str,
    use_gricean_check: Optional[bool] = None,
    fork_condition: Optional[str] = None,
    error_type: Optional[str] = None,
    fm_id: Optional[str] = None,
    fm_name: Optional[str] = None,
    injected_at_message_index: Optional[int] = None,
    injected_at_step: Optional[int] = None,
    injected_at_node: Optional[str] = None,
    original_message: Optional[Dict[str, Any]] = None,
    corrupted_message: Optional[Dict[str, Any]] = None,
    target_source: Optional[str] = None,
    started_at: Optional[str] = None,
    experiment_design: str = "4",
) -> Dict[str, Any]:
    """Builds one trace dict, shaped to match this project's established
    magnetic_one trace schema (task_id/question/ground_truth/level/graph/
    thread_id/condition/temperature/seed/messages/trust_history/
    trust_scores/n_rounds/n_stalls/termination_reason/token_stats/
    started_at/finished_at/correct, plus fork_metadata + injection_source_
    message for fork traces) -- while still keeping this project's own
    control fields (system/use_gricean_check/fork_condition/error_type/
    fm_id) that trace_io.py's file-naming and summary_row() rely on, and
    this project's own extra field (reflection_history) that the schema
    doesn't have a slot for."""
    answer = final_state.get("final_answer") or ""
    extracted, correct = _score(answer, question.get("ground_truth"))
    trust_history = _trust_history(final_state.get("gricean_history"))
    condition = _condition_str(fm_id, injected_at_message_index, use_gricean_check)

    trace: Dict[str, Any] = {
        "task_id": question["task_id"], "system": "magnetic_one", "graph": "magnetic_one",
        "question": question["query"], "ground_truth": question.get("ground_truth"), "level": question.get("level"),
        "thread_id": thread_id, "condition": condition,
        "temperature": DEFAULT_TEMPERATURE, "seed": DEFAULT_SEED,
        "use_gricean_check": use_gricean_check, "fork_condition": fork_condition,
        "error_type": error_type, "fm_id": fm_id, "fm_name": fm_name,
        "experiment_design": experiment_design,
        "final_answer_raw": answer, "final_answer_extracted": extracted, "correct": correct,
        "messages": final_state.get("messages"),
        "trust_history": trust_history, "trust_scores": trust_history[-1]["scores"] if trust_history else None,
        "reflection_history": final_state.get("reflection_history"),
        "n_rounds": final_state.get("n_rounds"), "n_stalls": final_state.get("n_stalls"),
        "termination_reason": final_state.get("termination_reason"),
        "token_stats": token_stats,
        "started_at": started_at, "finished_at": _now_iso(),
    }
    if fm_id is not None:
        trace["injected_at_message_index"] = injected_at_message_index
        trace["injection_source_message"] = original_message
        trace["fork_metadata"] = {
            "error_type": error_type, "fm_id": fm_id, "fm_name": fm_name,
            "injected_at_step": injected_at_step, "injected_at_message_index": injected_at_message_index,
            "injected_at_node": injected_at_node, "original_message": original_message,
            "corrupted_message": corrupted_message,
        }
    if target_source is not None:
        trace["target_source"] = target_source
    return trace


def run_magnetic_baseline(
    question: Dict[str, Any], magentic: MagenticOneLangGraph, use_gricean_check: bool, experiment_design: str = "4"
) -> Dict[str, Any]:
    """Runs one GAIA question through magnetic_one once (checker on or
    off). experiment_design: "4" (default) preserves the pre-existing
    Gricean-checker behavior; "1" routes through the canonical
    Trust_Allocator instead (see magnetic_one/gricean_checker.py)."""
    tid = f"{question['task_id']}::magnetic_one::{'checked' if use_gricean_check else 'clean'}::design{experiment_design}"
    started_at = _now_iso()
    attachment = gaia_utils.load_attachment(question["file_path"]) if question.get("file_path") else None
    result = asyncio.run(
        magentic.run(
            _task_text(question), thread_id=tid, enable_gricean_check=use_gricean_check,
            experiment_design=experiment_design, attachment=attachment,
        )
    )
    return _build_trace(
        question, result, result["token_stats"], thread_id=tid, use_gricean_check=use_gricean_check,
        started_at=started_at, experiment_design=experiment_design,
    )


def run_magnetic_error_forks(
    question: Dict[str, Any],
    magentic: MagenticOneLangGraph,
    error_plan: List[Tuple[str, Optional[str]]],
    strategy: str = "middle_agent_message",
    experiment_design: str = "4",
) -> Dict[str, List[Dict[str, Any]]]:
    """Runs the baseline pair once, then one fork per (error_type, fm_id)
    in `error_plan`, all forked from that same shared pair (see
    paired_fork.py's docstring for why this stays comparable up to the
    injection point -- verdict-based, not byte-identical, as of the
    shared_prefix_length fix).

    Returns {"baseline_traces": [...], "fork_traces": [...]}:
      - "fork_traces": two trace dicts (checker_off, checker_on) per fork,
        as before.
      - "baseline_traces": the no-error baseline/gricean-checked pair that
        run_paired_traces computes before any fork exists (pair[
        "baseline_result"]/pair["gricean_result"]) -- previously computed
        and then only used as a fork starting point, never saved as its
        own trace. Now preserved once, here, NOT recomputed (PART 1 of
        the instrumentation pass): every design/system combination needs
        exactly baseline+no-error, Trust_Allocator+no-error, baseline+
        FM-1.1, Trust_Allocator+FM-1.1 -- four traces total. Each side's
        own `result["token_stats"]` is already correctly scoped to just
        that side's calls -- magentic.run() resets usage tracking at its
        own start (see magnetic_one_langgraph.py), so gricean_result's
        run does not bleed baseline_result's calls into its own stats,
        and both were captured into their respective result dicts before
        the per-fork reset_usage_tracking() calls below ever run.

    experiment_design (see repo-root experiment_design.py) is passed
    straight through to run_paired_traces, which threads it into both
    sides of the baseline pair, and is recorded on every resulting
    trace (via _build_trace) and folded into each trace's thread_id so
    traces from different designs never collide."""
    task = _task_text(question)
    attachment = gaia_utils.load_attachment(question["file_path"]) if question.get("file_path") else None
    pair_started_at = _now_iso()
    pair = asyncio.run(run_paired_traces(
        magentic, task, thread_id_prefix=question["task_id"], experiment_design=experiment_design,
        attachment=attachment,
    ))
    baseline_traces: List[Dict[str, Any]] = [
        _build_trace(
            question, result, result["token_stats"],
            thread_id=f"{question['task_id']}::magnetic_one::"
                      f"{'checked' if use_gricean_check else 'clean'}::design{experiment_design}",
            use_gricean_check=use_gricean_check, started_at=pair_started_at, experiment_design=experiment_design,
        )
        for use_gricean_check, result in (
            (False, pair["baseline_result"]), (True, pair["gricean_result"])
        )
    ]

    fork_traces: List[Dict[str, Any]] = []
    for error_type, fm_id in error_plan:
        reset_usage_tracking(magentic.client)
        if magentic.gricean_model_client is not magentic.client:
            reset_usage_tracking(magentic.gricean_model_client)
        started_at = _now_iso()
        fork = asyncio.run(fork_paired_traces_with_error(
            magentic.graph, magentic.client, pair["baseline_config"], pair["gricean_config"], task,
            error_type, ORCHESTRATOR_NAME, fm_id=fm_id, strategy=strategy,
        ))
        stats = _combined_usage(magentic)
        for label, side in (("checker_off", "baseline"), ("checker_on", "gricean_checked")):
            side_result = fork[side]
            final_state = side_result["final_state"]
            fork_traces.append(_build_trace(
                question, final_state, stats,
                thread_id=f"{question['task_id']}::magnetic_one::{label}::design{experiment_design}",
                fork_condition=label, error_type=fork["error_type"], fm_id=fork["fm_id"], fm_name=fork["fm_name"],
                injected_at_message_index=fork["injected_at_message_index"],
                injected_at_step=side_result.get("injected_at_step"), injected_at_node=side_result.get("injected_at_node"),
                original_message=side_result.get("original_message"), corrupted_message=fork["corrupted_message"],
                started_at=started_at, experiment_design=experiment_design,
            ))
    return {"baseline_traces": baseline_traces, "fork_traces": fork_traces}


def run_magnetic_error_forks_by_source(
    question: Dict[str, Any],
    magentic: MagenticOneLangGraph,
    sources: List[str],
    error_type: str,
    fm_id: Optional[str] = None,
    seed: int = 42,
) -> Dict[str, List[Dict[str, Any]]]:
    """Like run_magnetic_error_forks, but instead of one fork at a single
    shared-prefix position (picked by `strategy`), runs one SEPARATE fork
    per entry in `sources` (e.g. ["MagenticOneOrchestrator", "FileSurfer",
    "WebSurfer", "Coder"]) -- each targeting a message chosen at random
    (seeded, so reproducible) from among that specific agent's messages
    within the shared baseline/Gricean prefix. All forks branch off the
    SAME baseline pair (one paired run per question, not per source), so
    every source's injection point is guaranteed to sit in a region that
    both traces' Gricean verdicts agree was high-adherence up to that
    point (see shared_prefix_length's docstring in paired_fork.py -- a
    behavioral guarantee, not literal content equality, since Ollama
    Cloud key rotation can make wording differ before any real
    divergence).

    Returns {source: [checker_off_row, checker_on_row]}; a source with no
    matching message in the shared prefix for this question maps to [].
    """
    task = _task_text(question)
    attachment = gaia_utils.load_attachment(question["file_path"]) if question.get("file_path") else None
    pair = asyncio.run(run_paired_traces(
        magentic, task, thread_id_prefix=question["task_id"], attachment=attachment
    ))

    async def _final_messages():
        baseline_cps = await list_message_checkpoints(magentic.graph, pair["baseline_config"])
        gricean_cps = await list_message_checkpoints(magentic.graph, pair["gricean_config"])
        return (baseline_cps[-1]["snapshot"].values["messages"], gricean_cps[-1]["snapshot"].values["messages"],
                baseline_cps[-1]["snapshot"].values.get("gricean_history", []),
                gricean_cps[-1]["snapshot"].values.get("gricean_history", []))

    baseline_messages, gricean_messages, baseline_history, gricean_history = asyncio.run(_final_messages())
    shared_len = shared_prefix_length(baseline_messages, gricean_messages, baseline_history, gricean_history)

    rng = random.Random(seed)
    results: Dict[str, List[Dict[str, Any]]] = {}
    for source in sources:
        candidates = [i for i in range(shared_len) if baseline_messages[i]["source"] == source]
        if not candidates:
            results[source] = []
            continue
        idx = rng.choice(candidates)

        reset_usage_tracking(magentic.client)
        if magentic.gricean_model_client is not magentic.client:
            reset_usage_tracking(magentic.gricean_model_client)

        started_at = _now_iso()
        fork = asyncio.run(fork_paired_traces_with_error(
            magentic.graph, magentic.client, pair["baseline_config"], pair["gricean_config"], task,
            error_type, ORCHESTRATOR_NAME, fm_id=fm_id, target_message_index=idx,
        ))
        stats = _combined_usage(magentic)
        rows = []
        for label, side in (("checker_off", "baseline"), ("checker_on", "gricean_checked")):
            side_result = fork[side]
            final_state = side_result["final_state"]
            rows.append(_build_trace(
                question, final_state, stats,
                thread_id=f"{question['task_id']}::magnetic_one::{source}::{label}",
                fork_condition=label, target_source=source,
                error_type=fork["error_type"], fm_id=fork["fm_id"], fm_name=fork["fm_name"],
                injected_at_message_index=idx,
                injected_at_step=side_result.get("injected_at_step"), injected_at_node=side_result.get("injected_at_node"),
                original_message=side_result.get("original_message"), corrupted_message=fork["corrupted_message"],
                started_at=started_at,
            ))
        results[source] = rows
    return results
