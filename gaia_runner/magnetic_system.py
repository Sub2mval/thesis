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
import sys
from typing import Any, Dict, List, Optional, Tuple

_LLM_DEBATE_DIR = os.path.join(os.path.dirname(__file__), "..", "llm_debate")
_MAGNETIC_DIR = os.path.join(os.path.dirname(__file__), "..", "magnetic_one")
for _dir in (_LLM_DEBATE_DIR, _MAGNETIC_DIR):
    if os.path.abspath(_dir) not in sys.path:
        sys.path.insert(0, os.path.abspath(_dir))

import gaia_utils  # noqa: E402
from magnetic_one_langgraph import MagenticOneLangGraph  # noqa: E402
from prompts import ORCHESTRATOR_FINAL_ANSWER_PROMPT  # noqa: E402
from context_utils import ORCHESTRATOR_NAME  # noqa: E402
from ollama_client import get_usage_tracking, reset_usage_tracking  # noqa: E402
from paired_fork import fork_paired_traces_with_error, run_paired_traces  # noqa: E402

from . import token_usage

# Ask the orchestrator to end its final answer in GAIA's own convention,
# so gaia_utils.extract_gaia_answer can parse it the same way it does for
# the debate MAS's output.
GAIA_FINAL_ANSWER_PROMPT = ORCHESTRATOR_FINAL_ANSWER_PROMPT + "\n" + gaia_utils.GAIA_ANSWER_FORMAT_INSTRUCTION


def build_magnetic_system(model: str, gricean_model: Optional[str], host: str, **kwargs: Any) -> MagenticOneLangGraph:
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
