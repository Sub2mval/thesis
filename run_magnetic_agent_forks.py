#!/usr/bin/env python3
"""
Per-agent-type magnetic_one error-injection runner.

The main `run_gaia_benchmark.py` / gaia_runner.cli entry point injects ONE
error per (question, error_type/fm_id) at a single shared-prefix position
(see gaia_runner.magnetic_system.run_magnetic_error_forks -- picked by
`strategy`, identically for whichever agent happens to own that position).
It has no notion of "one fork per agent type".

This script covers that: for each GAIA task_id given, it runs the baseline
pair (checker off / checker on) ONCE, then -- for each agent type in
--sources -- picks a random message *authored by that specific agent*
inside the region that is guaranteed byte-identical between the checker-off
and checker-on traces, injects ONE fm_id error there, and forks both traces
from it. Every agent type gets its own separate fork/run; results are
written to their own subfolder so the four agent-type runs never collide:

    {out_dir}/{task_id}/{source}/{system}__fork_{fm_id}_{on|off}.json
    {out_dir}/{task_id}/{source}/summary.jsonl

If a given agent type never produced a message before the two traces
diverge for a given question (e.g. Coder wasn't invoked), that source is
skipped for that question and noted on stdout -- it's not an error.

Usage (run from the thesis-main/ directory):
    python run_magnetic_agent_forks.py --task-ids abc123,def456 \
        --fm-id FM-1.1 --ollama-cloud --magnetic-model <model> \
        --out-dir gaia_runs/output/magnetic_one
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

_LLM_DEBATE_DIR = os.path.join(os.path.dirname(__file__), "llm_debate")
_MAGNETIC_DIR = os.path.join(os.path.dirname(__file__), "magnetic_one")
for _dir in (_LLM_DEBATE_DIR, _MAGNETIC_DIR):
    if os.path.abspath(_dir) not in sys.path:
        sys.path.insert(0, os.path.abspath(_dir))

import gaia_utils  # noqa: E402
from ollama_cloud_client import DEFAULT_OLLAMA_CLOUD_HOST, load_api_keys_from_env  # noqa: E402

from gaia_runner.error_spec import resolve_error_plan  # noqa: E402
from gaia_runner.question_select import select_questions  # noqa: E402
from gaia_runner.magnetic_system import build_magnetic_system, run_magnetic_error_forks_by_source  # noqa: E402
from gaia_runner import trace_io  # noqa: E402

DEFAULT_SOURCES = ["MagenticOneOrchestrator", "FileSurfer", "WebSurfer", "Coder"]


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--task-ids", required=True, help="Comma-separated GAIA task_ids.")
    p.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                    help="Comma-separated agent names, one fork each. Default: the 4 magnetic_one agents.")
    p.add_argument("--fm-id", default="FM-1.1", help="Fine-grained failure-mode id to inject, e.g. FM-1.1.")
    p.add_argument("--seed", type=int, default=42, help="Seed for both question sampling ties and target-message choice.")
    p.add_argument("--out-dir", default="gaia_runs/output/magnetic_one")
    p.add_argument("--gaia-source", choices=["huggingface", "local"], default="huggingface")
    p.add_argument("--gaia-local-path", default=None)
    p.add_argument("--gaia-subset", default="2023_level1")
    p.add_argument("--gaia-split", default="validation")
    p.add_argument("--magnetic-model", default="llama3.1:8b")
    p.add_argument("--magnetic-host", default="http://localhost:11434")
    p.add_argument("--gricean-model", default=None)
    p.add_argument("--ollama-cloud", action="store_true",
                    help="Use Ollama Cloud, rotating across OLLAMA_API_KEY_1..N from the environment.")
    p.add_argument("--ollama-cloud-host", default=DEFAULT_OLLAMA_CLOUD_HOST)
    return p


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def _save_source_rows(out_dir: str, task_id: str, source: str, rows: List[Dict[str, Any]]) -> None:
    source_dir = os.path.join(out_dir, task_id, source)
    for row in rows:
        condition = "on" if row["fork_condition"] == "checker_on" else "off"
        fm_tag = row["fm_id"] or row["error_type"]
        fname = f"{row['system']}__fork_{fm_tag}_{condition}.json"
        _write_json(os.path.join(source_dir, fname), row)
    if rows:
        os.makedirs(source_dir, exist_ok=True)
        with open(os.path.join(source_dir, "summary.jsonl"), "a") as f:
            for row in rows:
                f.write(json.dumps(trace_io.summary_row(row, "fork"), default=str) + "\n")


def main(argv: List[str] = None) -> None:
    args = build_arg_parser().parse_args(argv)
    task_ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    error_type, fm_id = resolve_error_plan(fm_id=args.fm_id)[0]

    pool = gaia_utils.load_gaia_questions(source=args.gaia_source, subset=args.gaia_subset,
                                           split=args.gaia_split, local_path=args.gaia_local_path)
    questions = select_questions(pool, task_ids=task_ids, seed=args.seed)

    for question in questions:
        if args.ollama_cloud:
            keys = load_api_keys_from_env(prefix="OLLAMA_API_KEY")
            magentic = build_magnetic_system(args.magnetic_model, args.gricean_model, args.ollama_cloud_host, api_keys=keys)
        else:
            magentic = build_magnetic_system(args.magnetic_model, args.gricean_model, args.magnetic_host)
        try:
            by_source = run_magnetic_error_forks_by_source(
                question, magentic, sources, error_type, fm_id=fm_id, seed=args.seed,
            )
            for source, rows in by_source.items():
                if not rows:
                    print(f"[skip] {question['task_id']}: no shared-prefix message from '{source}'.")
                    continue
                _save_source_rows(args.out_dir, question["task_id"], source, rows)
                print(f"[ok]   {question['task_id']} / {source}: fork saved "
                      f"(message index {rows[0]['injected_at_message_index']}).")
        finally:
            import asyncio
            asyncio.run(magentic.close())

    print(f"\nDone. {len(questions)} question(s) x up to {len(sources)} agent-type fork(s) written to {args.out_dir}")


if __name__ == "__main__":
    main()
