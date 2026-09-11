"""
Adapter between the GAIA runner and the magnetic_one MAS
(magnetic_one_langgraph.py + paired_fork.py + mo_error_injection.py).
Token usage is already tracked inside magnetic_one itself (ollama_client.py);
this module reshapes it into the common schema from token_usage.py and
adds GAIA scoring / attachment handling.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple


from llm_debate import gaia_utils  # noqa: E402
from magnetic_one.magnetic_one_langgraph import MagenticOneLangGraph  # noqa: E402
from magnetic_one.prompts import ORCHESTRATOR_FINAL_ANSWER_PROMPT  # noqa: E402
from magnetic_one.context_utils import ORCHESTRATOR_NAME  # noqa: E402
from magnetic_one.ollama_client import get_usage_tracking, reset_usage_tracking  # noqa: E402
from magnetic_one.paired_fork import fork_paired_traces_with_error, run_paired_traces, shared_prefix_length  # noqa: E402
from magnetic_one.error_injection import list_message_checkpoints  # noqa: E402

from . import token_usage

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


def _score(question: Dict[str, Any], answer_text: str) -> Dict[str, Any]:
    extracted = gaia_utils.extract_gaia_answer(answer_text or "")
    correct = gaia_utils.gaia_question_scorer(extracted, question.get("ground_truth"))
    return {"extracted_answer": extracted, "correct": correct}


def _combined_usage(magentic: MagenticOneLangGraph) -> Dict[str, Any]:
    """token_stats is only assembled inside MagenticOneLangGraph.run();
    fork continuations below call graph.ainvoke() directly (as
    paired_fork.py does), bypassing that wrapper -- so usage is read off
    the instrumented client(s) by hand instead."""
    calls = list(get_usage_tracking(magentic.client).get("calls", []))
    if magentic.gricean_model_client is not magentic.client:
        calls += list(get_usage_tracking(magentic.gricean_model_client).get("calls", []))
    calls.sort(key=lambda r: (r.get("started_at_unix", 0), r.get("call_index", 0)))
    return {"calls": calls}


def run_magnetic_baseline(question: Dict[str, Any], magentic: MagenticOneLangGraph, use_gricean_check: bool) -> Dict[str, Any]:
    """Runs one GAIA question through magnetic_one once (checker on or off)."""
    tid = f"{question['task_id']}-{'on' if use_gricean_check else 'off'}"
    result = asyncio.run(magentic.run(_task_text(question), thread_id=tid, enable_gricean_check=use_gricean_check))
    answer = result.get("final_answer") or ""
    return {
        "task_id": question["task_id"], "system": "magnetic_one", "use_gricean_check": use_gricean_check,
        "final_answer": answer, **_score(question, answer),
        "token_stats": token_usage.from_magnetic_token_stats(result["token_stats"]),
    }


def run_magnetic_error_forks(
    question: Dict[str, Any],
    magentic: MagenticOneLangGraph,
    error_plan: List[Tuple[str, Optional[str]]],
    strategy: str = "middle_agent_message",
) -> List[Dict[str, Any]]:
    """Runs the baseline pair once, then one fork per (error_type, fm_id)
    in `error_plan`, all forked from that same shared pair (see
    paired_fork.py's docstring for why this stays byte-identical up to
    the injection point). Returns a flat list of two trace dicts
    (checker_off, checker_on) per fork."""
    task = _task_text(question)
    pair = asyncio.run(run_paired_traces(magentic, task, thread_id_prefix=question["task_id"]))
    results: List[Dict[str, Any]] = []
    for error_type, fm_id in error_plan:
        reset_usage_tracking(magentic.client)
        if magentic.gricean_model_client is not magentic.client:
            reset_usage_tracking(magentic.gricean_model_client)
        fork = asyncio.run(fork_paired_traces_with_error(
            magentic.graph, magentic.client, pair["baseline_config"], pair["gricean_config"], task,
            error_type, ORCHESTRATOR_NAME, fm_id=fm_id, strategy=strategy,
        ))
        stats = token_usage.from_magnetic_token_stats(_combined_usage(magentic))
        for label, side in (("checker_off", "baseline"), ("checker_on", "gricean_checked")):
            answer = fork[side]["final_state"].get("final_answer") or ""
            results.append({
                "task_id": question["task_id"], "system": "magnetic_one", "fork_condition": label,
                "error_type": fork["error_type"], "fm_id": fork["fm_id"], "fm_name": fork["fm_name"],
                "final_answer": answer, **_score(question, answer), "token_stats": stats,
            })
    return results


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
    is byte-identical between checker-off and checker-on up to that point.

    Returns {source: [checker_off_row, checker_on_row]}; a source with no
    matching message in the shared prefix for this question maps to [].
    """
    task = _task_text(question)
    pair = asyncio.run(run_paired_traces(magentic, task, thread_id_prefix=question["task_id"]))

    async def _final_messages():
        baseline_cps = await list_message_checkpoints(magentic.graph, pair["baseline_config"])
        gricean_cps = await list_message_checkpoints(magentic.graph, pair["gricean_config"])
        return baseline_cps[-1]["snapshot"].values["messages"], gricean_cps[-1]["snapshot"].values["messages"]

    baseline_messages, gricean_messages = asyncio.run(_final_messages())
    shared_len = shared_prefix_length(baseline_messages, gricean_messages)

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

        fork = asyncio.run(fork_paired_traces_with_error(
            magentic.graph, magentic.client, pair["baseline_config"], pair["gricean_config"], task,
            error_type, ORCHESTRATOR_NAME, fm_id=fm_id, target_message_index=idx,
        ))
        stats = token_usage.from_magnetic_token_stats(_combined_usage(magentic))
        rows = []
        for label, side in (("checker_off", "baseline"), ("checker_on", "gricean_checked")):
            answer = fork[side]["final_state"].get("final_answer") or ""
            rows.append({
                "task_id": question["task_id"], "system": "magnetic_one", "fork_condition": label,
                "target_source": source, "injected_at_message_index": idx,
                "error_type": fork["error_type"], "fm_id": fork["fm_id"], "fm_name": fork["fm_name"],
                "final_answer": answer, **_score(question, answer), "token_stats": stats,
            })
        results[source] = rows
    return results
