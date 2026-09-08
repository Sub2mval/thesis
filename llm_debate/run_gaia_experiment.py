# run_gaia_experiment.py
#
# Runs both debate MAS variants (baseline / with Trust Allocator) over a
# configurable number of GAIA validation questions, records the full trace +
# correctness for each, then for each question forks 3 additional traces from
# a single shared injection point -- one per error type (factual_error,
# reasoning_error, fabrication) -- continuing execution from there via
# LangGraph's checkpointer, and records those traces + correctness + which
# step/message was corrupted.
#
# Every record is written to its own JSON file the moment it's produced, so a
# crash partway through a long run only costs you the in-flight question.
#
# --- Ollama Cloud ------------------------------------------------------------
# Configured for Ollama Cloud (qwen3.5:397b-cloud) with API keys rotated
# across requests. Put them in a .env file next to this script, one per line,
# numbered from 1 (any number of keys works, not just 4):
#
#   OLLAMA_API_KEY_1 = 'key1'
#   OLLAMA_API_KEY_2 = 'key2'
#   OLLAMA_API_KEY_3 = 'key3'
#   OLLAMA_API_KEY_4 = 'key4'
#
# Each key becomes its own entry in general_config's model_list, and
# call_llm's existing random.choice over that list rotates across keys for
# you -- no other code change needed for the rotation itself.
# ------------------------------------------------------------------------------
#
# Requirements:
#   pip install langgraph langgraph-checkpoint-sqlite ollama datasets pyyaml tenacity python-dotenv
#   huggingface-cli login      (GAIA is a gated dataset on the Hub)
#
# Usage:
#   python run_gaia_experiment.py --num-questions 5
#   python run_gaia_experiment.py --num-questions 20 --graphs baseline
#   python run_gaia_experiment.py --local-path my_gaia_subset.json --num-questions 3

import argparse
import json
import os
import random
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver

from langgraph_debate import (
    DebateState,
    build_debate_graph,
    build_debate_graph_with_trust_allocator,
)
from gaia_utils import (
    GAIA_ANSWER_FORMAT_INSTRUCTION,
    extract_gaia_answer,
    gaia_question_scorer,
    load_attachment,
    load_gaia_questions,
)
from error_induction import ERROR_TYPES, generate_corrupted_message


# --------------------------------------------------------------------------- #
# Safe JSON writing
# --------------------------------------------------------------------------- #

def load_ollama_api_keys() -> List[str]:
    """
    Reads OLLAMA_API_KEY_1, OLLAMA_API_KEY_2, ... from the environment (after
    load_dotenv()), in order, stopping at the first gap. Any number of keys
    is fine -- 1, 4, 10, whatever's in the .env file. Blank values (e.g.
    OLLAMA_API_KEY_3 = '') are skipped rather than treated as a stop, so a
    sparsely-filled .env still picks up the keys that are actually set.
    """
    keys = []
    n = 1
    misses = 0
    while misses < 3:  # tolerate a couple of blank/missing slots before giving up
        val = os.getenv(f"OLLAMA_API_KEY_{n}")
        if val and val.strip():
            keys.append(val.strip())
            misses = 0
        else:
            misses += 1
        n += 1
    return keys


def write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, path)  # atomic on POSIX


# --------------------------------------------------------------------------- #
# Trace extraction: walk checkpoint history, diff consecutive states to
# recover which agent's message was added at each step.
# --------------------------------------------------------------------------- #

def _diff_new_assistant_message(prev_contexts, curr_contexts) -> Optional[Dict[str, Any]]:
    if prev_contexts is None:
        return None
    for agent_id, (old_ctx, new_ctx) in enumerate(zip(prev_contexts, curr_contexts)):
        if len(new_ctx) > len(old_ctx):
            for m in new_ctx[len(old_ctx):]:
                if m["role"] == "assistant":
                    return {"agent_id": agent_id, "role": m["role"], "content": m["content"]}
    return None


def _snapshot_entry(step_index: int, snap, message_added: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "step_index": step_index,
        "checkpoint_id": snap.config["configurable"].get("checkpoint_id"),
        "round": snap.values.get("round"),
        "agent_idx": snap.values.get("agent_idx"),
        "agent_trust": snap.values.get("agent_trust"),
        "final_answer": snap.values.get("final_answer"),
        "message_added": message_added,
    }


def extract_trace(app, thread_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    history = list(app.get_state_history(thread_config))
    history.reverse()  # chronological, oldest first

    trace = []
    prev_contexts = None
    for step_index, snap in enumerate(history):
        contexts = snap.values.get("agent_contexts", [])
        added = _diff_new_assistant_message(prev_contexts, contexts)
        trace.append(_snapshot_entry(step_index, snap, added))
        prev_contexts = contexts
    return trace


def _walk_branch_back_to(app, tip_snapshot, stop_checkpoint_id: str) -> List[Any]:
    """Walk parent_config pointers from tip_snapshot back to (but excluding)
    the checkpoint with id stop_checkpoint_id. Returns chronological list of
    StateSnapshots for just this segment (used to isolate one fork's trace
    even though all forks share one thread_id lineage)."""
    segment = []
    snap = tip_snapshot
    while snap is not None:
        cid = snap.config["configurable"].get("checkpoint_id")
        if cid == stop_checkpoint_id:
            break
        segment.append(snap)
        parent_cfg = snap.parent_config
        if parent_cfg is None:
            break
        snap = app.get_state(parent_cfg)
    segment.reverse()
    return segment


# --------------------------------------------------------------------------- #
# Baseline run
# --------------------------------------------------------------------------- #

def build_initial_state(question: Dict[str, Any], agents_num: int, rounds_num: int,
                         general_config: Dict[str, Any], attachment: Optional[Dict[str, Any]] = None) -> DebateState:
    return {
        "query": question["query"],
        "agents_num": agents_num,
        "rounds_num": rounds_num,
        "round": 0,
        "agent_idx": 0,
        "agent_contexts": [],
        "final_answer": None,
        "token_stats": {},
        "config": general_config,
        "agent_trust": {},
        "attachment": attachment,
    }


def run_baseline(app, question, graph_name, agents_num, rounds_num, general_config,
                  out_dir, overwrite=False):
    thread_id = f"{question['task_id']}__{graph_name}"
    out_path = os.path.join(out_dir, f"{thread_id}.json")
    if os.path.exists(out_path) and not overwrite:
        print(f"[skip] {out_path} already exists")
        with open(out_path) as f:
            return json.load(f), {"configurable": {"thread_id": thread_id}}

    attachment = None
    if question.get("file_path"):
        attachment = load_attachment(question["file_path"])
        if attachment["kind"] == "unsupported":
            print(f"[warn] {question['task_id']}: attachment not usable -- {attachment['note']}")

    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 300}
    initial_state = build_initial_state(question, agents_num, rounds_num, general_config, attachment)

    t0 = time.time()
    result = app.invoke(initial_state, config=config)
    elapsed = time.time() - t0

    thread_config = {"configurable": {"thread_id": thread_id}}
    trace = extract_trace(app, thread_config)
    predicted = extract_gaia_answer(result["final_answer"])
    correct = (
        gaia_question_scorer(predicted, question["ground_truth"])
        if question.get("ground_truth") else None
    )

    record = {
        "task_id": question["task_id"],
        "graph": graph_name,
        "query": question["query"],
        "ground_truth": question.get("ground_truth"),
        "file_name": question.get("file_name") or None,
        "attachment_kind": attachment["kind"] if attachment else None,
        "attachment_note": attachment.get("note") if attachment else None,
        "predicted_answer": predicted,
        "correct": correct,
        "final_answer_raw": result["final_answer"],
        "token_stats": result["token_stats"],
        "elapsed_seconds": elapsed,
        "thread_id": thread_id,
        "trace": trace,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(out_path, record)
    print(f"[baseline] {thread_id}: correct={correct} predicted={predicted!r}")
    return record, thread_config


# --------------------------------------------------------------------------- #
# Fork with induced error
# --------------------------------------------------------------------------- #

def run_error_forks(app, base_record, base_thread_config, question, general_config,
                     out_dir, rng, overwrite=False):
    trace = base_record["trace"]
    eligible = [t for t in trace if t["message_added"] is not None]
    if not eligible:
        print(f"[fork] no eligible messages to corrupt for {question['task_id']}; skipping")
        return []

    fork_step = rng.choice(eligible)
    step_index = fork_step["step_index"]
    agent_id = fork_step["message_added"]["agent_id"]
    original_message = fork_step["message_added"]["content"]

    history = list(app.get_state_history(base_thread_config))
    history.reverse()
    fork_point_snapshot = history[step_index]
    fork_point_config = fork_point_snapshot.config
    fork_point_checkpoint_id = fork_point_config["configurable"].get("checkpoint_id")
    fork_source_contexts = fork_point_snapshot.values["agent_contexts"]

    records = []
    for error_type in ERROR_TYPES:
        fork_name = f"{base_record['thread_id']}__fork_{error_type}_step{step_index}"
        out_path = os.path.join(out_dir, f"{fork_name}.json")
        if os.path.exists(out_path) and not overwrite:
            print(f"[skip] {out_path} already exists")
            with open(out_path) as f:
                records.append(json.load(f))
            continue

        corrupted, _usage = generate_corrupted_message(
            original_message, error_type, question["query"], general_config
        )

        new_contexts = [list(ctx) for ctx in fork_source_contexts]
        new_contexts[agent_id] = new_contexts[agent_id][:-1] + [
            {"role": "assistant", "content": corrupted}
        ]

        fork_config = app.update_state(fork_point_config, {"agent_contexts": new_contexts})

        t0 = time.time()
        result = app.invoke(None, config=fork_config)
        elapsed = time.time() - t0

        # Capture the tip of this fork's branch right now, before starting the
        # next fork (which branches from the same parent under the same
        # thread_id) -- this is what keeps the three forks' traces distinct.
        tip_snapshot = app.get_state({"configurable": {"thread_id": base_record["thread_id"]}})
        segment = _walk_branch_back_to(app, tip_snapshot, fork_point_checkpoint_id)

        fork_trace = list(trace[: step_index + 1])
        # Replace the fork-point entry's message with the corrupted version,
        # tagged as injected.
        fork_trace[-1] = dict(fork_trace[-1])
        fork_trace[-1]["message_added"] = {
            "agent_id": agent_id,
            "role": "assistant",
            "content": corrupted,
            "injected_error_type": error_type,
            "original_content": original_message,
        }

        prev_contexts = new_contexts
        for offset, snap in enumerate(segment):
            contexts = snap.values.get("agent_contexts", [])
            added = _diff_new_assistant_message(prev_contexts, contexts) if offset > 0 else None
            fork_trace.append(_snapshot_entry(step_index + 1 + offset, snap, added))
            prev_contexts = contexts

        predicted = extract_gaia_answer(result["final_answer"])
        correct = (
            gaia_question_scorer(predicted, question["ground_truth"])
            if question.get("ground_truth") else None
        )

        record = {
            "task_id": question["task_id"],
            "graph": base_record["graph"],
            "fork_error_type": error_type,
            "fork_step_index": step_index,
            "fork_agent_id": agent_id,
            "fork_round": fork_step["round"],
            "original_message": original_message,
            "corrupted_message": corrupted,
            "query": question["query"],
            "ground_truth": question.get("ground_truth"),
            "predicted_answer": predicted,
            "correct": correct,
            "final_answer_raw": result["final_answer"],
            "token_stats": result["token_stats"],
            "elapsed_seconds": elapsed,
            "trace": fork_trace,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        write_json_atomic(out_path, record)
        print(f"[fork:{error_type}] {fork_name}: correct={correct} predicted={predicted!r} "
              f"(baseline correct={base_record['correct']})")
        records.append(record)

    return records


# --------------------------------------------------------------------------- #
# Parallel execution across questions.
#
# Each question's baseline + forks are independent of every other question's,
# so they parallelize cleanly across processes. Threads won't help here even
# though the LLM calls are I/O-bound over HTTP -- the real bottleneck is
# Ollama's own generation throughput, which is governed by OLLAMA_NUM_PARALLEL
# / available VRAM on the server, not by Python concurrency. Set --workers to
# roughly match how many requests Ollama can actually run concurrently; more
# than that just queues up in Ollama with no speedup.
#
# Each worker process gets its OWN sqlite checkpoint file (checkpoints_<pid>.sqlite)
# rather than sharing one -- this sidesteps sqlite write-lock contention
# entirely. Forking only needs a question's own checkpoint history, and a
# single worker processes a given question start-to-finish (baseline + all 3
# forks), so per-worker files are sufficient; nothing needs a cross-worker view.
# --------------------------------------------------------------------------- #

_WORKER_APPS: Dict[str, Any] = {}
_WORKER_GENERAL_CONFIG: Dict[str, Any] = {}
_WORKER_AGENTS_NUM: int = 3
_WORKER_ROUNDS_NUM: int = 2
_WORKER_OUTPUT_DIR: str = "gaia_traces"
_WORKER_OVERWRITE: bool = False
_WORKER_SKIP_FORKS: bool = False


def _worker_init(output_dir, checkpoint_dir, general_config, graph_names,
                  agents_num, rounds_num, overwrite, skip_forks):
    global _WORKER_APPS, _WORKER_GENERAL_CONFIG, _WORKER_AGENTS_NUM
    global _WORKER_ROUNDS_NUM, _WORKER_OUTPUT_DIR, _WORKER_OVERWRITE, _WORKER_SKIP_FORKS

    worker_pid = os.getpid()
    db_path = os.path.join(checkpoint_dir, f"checkpoints_{worker_pid}.sqlite")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    apps = {}
    if "baseline" in graph_names:
        apps["baseline"] = build_debate_graph(checkpointer=checkpointer)
    if "trust_allocator" in graph_names:
        apps["trust_allocator"] = build_debate_graph_with_trust_allocator(checkpointer=checkpointer)

    _WORKER_APPS = apps
    _WORKER_GENERAL_CONFIG = general_config
    _WORKER_AGENTS_NUM = agents_num
    _WORKER_ROUNDS_NUM = rounds_num
    _WORKER_OUTPUT_DIR = output_dir
    _WORKER_OVERWRITE = overwrite
    _WORKER_SKIP_FORKS = skip_forks


def _worker_run_job(job):
    question, graph_name = job
    app = _WORKER_APPS[graph_name]
    # Deterministic per-job seed so which message gets corrupted doesn't
    # depend on execution order across workers (nondeterministic under
    # ProcessPoolExecutor scheduling).
    seed_source = f"{question['task_id']}::{graph_name}"
    rng = random.Random(abs(hash(seed_source)) % (2**32))

    try:
        base_record, base_thread_config = run_baseline(
            app, question, graph_name, _WORKER_AGENTS_NUM, _WORKER_ROUNDS_NUM,
            _WORKER_GENERAL_CONFIG, _WORKER_OUTPUT_DIR, overwrite=_WORKER_OVERWRITE,
        )
        if not _WORKER_SKIP_FORKS:
            run_error_forks(
                app, base_record, base_thread_config, question,
                _WORKER_GENERAL_CONFIG, _WORKER_OUTPUT_DIR, rng, overwrite=_WORKER_OVERWRITE,
            )
        return (question["task_id"], graph_name, "ok", None)
    except Exception as e:  # noqa: BLE001 -- want to keep other jobs running
        return (question["task_id"], graph_name, "error", str(e))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-questions", type=int, default=None,
                         help="How many GAIA questions to run (default: all available after filtering)")
    parser.add_argument("--subset", default="2023_level1")
    parser.add_argument("--split", default="validation")
    parser.add_argument("--local-path", default=None,
                         help="Load questions from a local .json/.jsonl file instead of the Hub")
    parser.add_argument("--text-only", action="store_true",
                         help="Skip GAIA tasks that have a file attachment (by default they're now "
                              "included -- see gaia_utils.load_attachment for which file types are "
                              "actually usable: images and PDFs are passed to the model, plain text "
                              "is inlined, everything else is noted as unsupported and skipped).")
    parser.add_argument("--graphs", default="baseline,trust_allocator",
                         help="Comma-separated subset of: baseline, trust_allocator")
    parser.add_argument("--no-forks", action="store_true", help="Skip the error-injection forking step")
    parser.add_argument("--output-dir", default="gaia_traces")
    parser.add_argument("--checkpoint-db", default="gaia_checkpoints.sqlite")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    # Model / debate config
    parser.add_argument("--ollama-model", default="qwen3.5:397b-cloud")
    parser.add_argument("--ollama-host", default="https://ollama.com")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--agents-num", type=int, default=3)
    parser.add_argument("--rounds-num", type=int, default=2)
    parser.add_argument("--workers", type=int, default=1,
                         help="Number of questions to process concurrently (separate processes). "
                              "Match this to how many concurrent requests your Ollama server can "
                              "actually serve (see OLLAMA_NUM_PARALLEL) -- more just queues up with "
                              "no speedup.")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    questions = load_gaia_questions(
        source="local" if args.local_path else "huggingface",
        subset=args.subset,
        split=args.split,
        local_path=args.local_path,
        text_only=args.text_only,
        limit=args.num_questions,
    )
    print(f"Loaded {len(questions)} question(s).")

    load_dotenv()
    api_keys = load_ollama_api_keys()
    if not api_keys:
        raise RuntimeError(
            "No API keys found. Add OLLAMA_API_KEY_1, OLLAMA_API_KEY_2, etc. "
            "to a .env file next to this script."
        )
    print(f"Loaded {len(api_keys)} Ollama Cloud API key(s); requests will rotate across them.")

    general_config = {
        "model_api_config": {
            "primary": {
                "model_list": [
                    {"model_name": args.ollama_model, "host": args.ollama_host, "api_key": key}
                    for key in api_keys
                ]
            }
        },
        "model_name": "primary",
        "model_temperature": args.temperature,
        "model_max_tokens": args.max_tokens,
        "answer_format_instruction": GAIA_ANSWER_FORMAT_INSTRUCTION,
    }

    graph_names = [g.strip() for g in args.graphs.split(",") if g.strip()]

    if args.workers <= 1:
        rng = random.Random(args.seed)
        with SqliteSaver.from_conn_string(args.checkpoint_db) as checkpointer:
            apps = {}
            if "baseline" in graph_names:
                apps["baseline"] = build_debate_graph(checkpointer=checkpointer)
            if "trust_allocator" in graph_names:
                apps["trust_allocator"] = build_debate_graph_with_trust_allocator(checkpointer=checkpointer)

            for question in questions:
                for graph_name, app in apps.items():
                    base_record, base_thread_config = run_baseline(
                        app, question, graph_name, args.agents_num, args.rounds_num,
                        general_config, args.output_dir, overwrite=args.overwrite,
                    )
                    if not args.no_forks:
                        run_error_forks(
                            app, base_record, base_thread_config, question,
                            general_config, args.output_dir, rng, overwrite=args.overwrite,
                        )
    else:
        checkpoint_dir = os.path.dirname(os.path.abspath(args.checkpoint_db)) or "."
        os.makedirs(checkpoint_dir, exist_ok=True)

        jobs = [(q, g) for q in questions for g in graph_names]
        print(f"Dispatching {len(jobs)} job(s) across {args.workers} worker process(es)...")

        completed, errored = 0, 0
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_worker_init,
            initargs=(args.output_dir, checkpoint_dir, general_config, graph_names,
                      args.agents_num, args.rounds_num, args.overwrite, args.no_forks),
        ) as executor:
            futures = [executor.submit(_worker_run_job, job) for job in jobs]
            for fut in as_completed(futures):
                task_id, graph_name, status, error = fut.result()
                if status == "ok":
                    completed += 1
                    print(f"[done {completed}/{len(jobs)}] {task_id} / {graph_name}")
                else:
                    errored += 1
                    print(f"[ERROR {errored}] {task_id} / {graph_name}: {error}")

    print("Done.")


if __name__ == "__main__":
    main()