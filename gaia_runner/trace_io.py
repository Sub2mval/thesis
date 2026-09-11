"""
Where and how a run's traces get written to disk:

    {out_dir}/{task_id}/{system}__baseline_{on|off}.json
    {out_dir}/{task_id}/{system}__fork{n}_{fm_id_or_family}_{on|off}.json

Plus {out_dir}/summary.jsonl with one line per trace, appended as each
trace completes -- so a crash partway through a long benchmark run
doesn't lose earlier results, and accuracy can be scanned without
opening every trace file.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def save_baseline_trace(out_dir: str, trace: Dict[str, Any]) -> str:
    condition = "on" if trace["use_gricean_check"] else "off"
    path = os.path.join(out_dir, trace["task_id"], f"{trace['system']}__baseline_{condition}.json")
    _write_json(path, trace)
    return path


def save_fork_trace(out_dir: str, trace: Dict[str, Any], fork_index: int) -> str:
    condition = "on" if trace["fork_condition"] == "checker_on" else "off"
    fm_tag = trace["fm_id"] or trace["error_type"]
    fname = f"{trace['system']}__fork{fork_index}_{fm_tag}_{condition}.json"
    path = os.path.join(out_dir, trace["task_id"], fname)
    _write_json(path, trace)
    return path


def append_summary_row(out_dir: str, row: Dict[str, Any]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.jsonl"), "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def summary_row(trace: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """kind: "baseline" or "fork" -- determines which condition field is read."""
    condition = trace.get("use_gricean_check") if kind == "baseline" else trace.get("fork_condition")
    return {
        "task_id": trace["task_id"], "system": trace["system"], "kind": kind, "condition": condition,
        "error_type": trace.get("error_type"), "fm_id": trace.get("fm_id"),
        "correct": trace.get("correct"), "total_tokens": trace.get("token_stats", {}).get("total_tokens"),
    }
