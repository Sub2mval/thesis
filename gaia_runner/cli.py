"""
Top-level GAIA benchmark runner: for a chosen subset of the GAIA
validation questions, runs each one through llm_debate and/or
magnetic_one (checker off and on), then runs the requested
error-injection Gricean-check forks against the same question -- saving
every trace to disk with per-call and per-trace token usage attached.

Usage (run from the thesis-main/ directory):
    python run_gaia_benchmark.py --n 20 --systems both
    python run_gaia_benchmark.py --task-ids abc123,def456 --error-family task_verification
    python run_gaia_benchmark.py --n 5 --error-type FM-2.3 --systems debate
    python run_gaia_benchmark.py                      # all 165 questions, both systems, default 3-family forks
"""

from __future__ import annotations
from dotenv import load_dotenv
import argparse
from typing import Any, Dict, List

from tqdm import tqdm
from llm_debate import gaia_utils  # noqa: E402
# NOTE: load_api_keys_from_env is intentionally NOT imported here at module
# level -- it lives in magnetic_one/ollama_cloud_client.py, which pulls in
# autogen_core/autogen_ext. Importing it eagerly would force a debate-only
# run to have autogen installed, breaking the same deferred-import
# separation _run_debate()/_run_magnetic() already rely on below. It's
# imported lazily, inside _debate_model_list() and _run_magnetic(), instead.

from .error_spec import resolve_error_plan
from .question_select import select_questions
from . import trace_io

DEFAULT_OLLAMA_CLOUD_HOST = "https://ollama.com"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run GAIA validation questions against llm_debate and magnetic_one.")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--n", type=int, help="Random subset of N questions.")
    sel.add_argument("--task-ids", type=str, help="Comma-separated GAIA task_ids to run instead of a random subset.")
    err = p.add_mutually_exclusive_group()
    err.add_argument("--error-family", choices=["specification_issue", "inter_agent_misalignment", "task_verification"],
                      help="Inject one random error from this family only (default: one from each of the 3 families).")
    err.add_argument("--error-type", type=str, help="Inject exactly this fine-grained error type, e.g. FM-2.3.")
    p.add_argument("--systems", choices=["debate", "magnetic", "both"], default="both")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", default="gaia_runs/output")
    p.add_argument("--gaia-source", choices=["huggingface", "local"], default="huggingface")
    p.add_argument("--gaia-local-path", default=None)
    p.add_argument("--gaia-subset", default="2023_all")
    p.add_argument("--gaia-split", default="validation")
    p.add_argument("--debate-model", default="gemma4:31b-cloud")
    p.add_argument("--debate-host", default="http://localhost:11434")
    p.add_argument("--agents-num", type=int, default=3)
    p.add_argument("--rounds-num", type=int, default=2)
    p.add_argument("--magnetic-model", default="gemma4:31b-cloud")
    p.add_argument("--magnetic-host", default="http://localhost:11434")
    p.add_argument("--gricean-model", default=None, help="Optional separate (cheaper) model for the Gricean checker.")
    p.add_argument("--ollama-cloud", action="store_true",
                    help="Use Ollama Cloud instead of a local server: loads all OLLAMA_API_KEY_1.."
                         "OLLAMA_API_KEY_N (and a bare OLLAMA_API_KEY, if set) from the environment "
                         "and rotates across them for both systems. Overrides --debate-host/--magnetic-host "
                         "with --ollama-cloud-host.")
    p.add_argument("--ollama-cloud-host", default=DEFAULT_OLLAMA_CLOUD_HOST)
    return p


def _debate_model_list(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if not args.ollama_cloud:
        return [{"model": args.debate_model, "host": args.debate_host}]
    from magnetic_one.ollama_cloud_client import load_api_keys_from_env  # deferred: see import note above
    keys = load_api_keys_from_env(prefix="OLLAMA_API_KEY")
    # One entry per key, all naming the SAME model/host -- random.choice()
    # in call_llm() then load-spreads across keys. Safe for reproducibility
    # here because every entry is the identical model; only the auth key
    # differs (see the determinism note in langgraph_debate.call_llm).
    return [{"model": args.debate_model, "host": args.ollama_cloud_host, "api_key": k} for k in keys]


def _run_debate(question: Dict[str, Any], args: argparse.Namespace, error_plan, out_dir: str) -> List[Dict[str, Any]]:
    from . import debate_system  # deferred: keeps a magnetic-only run from needing ollama/tenacity installed
    cfg = {"model_list": _debate_model_list(args), "temperature": 0, "seed": args.seed}
    rows: List[Dict[str, Any]] = []
    for use_check in (False, True):
        trace = debate_system.run_debate_baseline(question, cfg, use_check, args.agents_num, args.rounds_num)
        trace_io.save_baseline_trace(out_dir, trace)
        rows.append(trace_io.summary_row(trace, "baseline"))
    forks = debate_system.run_debate_error_forks(question, cfg, error_plan, args.agents_num, args.rounds_num)
    for i, fork_trace in enumerate(forks):
        trace_io.save_fork_trace(out_dir, fork_trace, i)
        rows.append(trace_io.summary_row(fork_trace, "fork"))
    for row in rows:
        trace_io.append_summary_row(out_dir, row)
    return rows


def _run_magnetic(question: Dict[str, Any], args: argparse.Namespace, error_plan, out_dir: str) -> List[Dict[str, Any]]:
    from . import magnetic_system  # deferred: keeps a debate-only run from needing autogen installed
    if args.ollama_cloud:
        from magnetic_one.ollama_cloud_client import load_api_keys_from_env  # deferred: see import note above
        keys = load_api_keys_from_env(prefix="OLLAMA_API_KEY")
        magentic = magnetic_system.build_magnetic_system(
            args.magnetic_model, args.gricean_model, args.ollama_cloud_host, api_keys=keys,
        )
    else:
        magentic = magnetic_system.build_magnetic_system(args.magnetic_model, args.gricean_model, args.magnetic_host)
    rows: List[Dict[str, Any]] = []
    for use_check in (False, True):
        trace = magnetic_system.run_magnetic_baseline(question, magentic, use_check)
        trace_io.save_baseline_trace(out_dir, trace)
        rows.append(trace_io.summary_row(trace, "baseline"))
    forks = magnetic_system.run_magnetic_error_forks(question, magentic, error_plan)
    for i, fork_trace in enumerate(forks):
        trace_io.save_fork_trace(out_dir, fork_trace, i)
        rows.append(trace_io.summary_row(fork_trace, "fork"))
    for row in rows:
        trace_io.append_summary_row(out_dir, row)
    return rows


def main(argv: List[str] = None) -> None:
    from dotenv import load_dotenv
    args = build_arg_parser().parse_args(argv)
    task_ids = args.task_ids.split(",") if args.task_ids else None
    error_plan = resolve_error_plan(family=args.error_family, fm_id=args.error_type)

    pool = gaia_utils.load_gaia_questions(source=args.gaia_source, subset=args.gaia_subset,
                                           split=args.gaia_split, local_path=args.gaia_local_path)
    questions = select_questions(pool, n=args.n, task_ids=task_ids, seed=args.seed)

    rows: List[Dict[str, Any]] = []
    for question in tqdm(questions, desc="GAIA questions"):
        if args.systems in ("debate", "both"):
            rows += _run_debate(question, args, error_plan, args.out_dir)
        if args.systems in ("magnetic", "both"):
            rows += _run_magnetic(question, args, error_plan, args.out_dir)

    n_baseline = [r for r in rows if r["kind"] == "baseline"]
    n_correct = sum(1 for r in n_baseline if r.get("correct"))
    print(f"\nDone. {len(questions)} question(s), {len(rows)} trace(s) written to {args.out_dir}")
    print(f"Baseline accuracy: {n_correct}/{len(n_baseline)}")


if __name__ == "__main__":
    main()
