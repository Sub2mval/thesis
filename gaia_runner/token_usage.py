"""
Token-usage aggregation for llm_debate's traces. This shape now matches
magnetic_one's own usage_stats() shape (see ../magnetic_one/ollama_client.py
and ollama_cloud_client.py, and magnetic_system.py's _combined_usage) --
previously this module normalized BOTH systems down to a smaller common
schema (input_tokens/output_tokens only, no per-attempt status, no
timing); that normalization was dropped in favor of the richer shape
both MAS's raw instrumentation already computes, so nothing generated
gets discarded before it reaches a trace:

    {"n_llm_calls": int, "n_successful_calls": int, "n_failed_attempts": int,
     "prompt_tokens": int, "completion_tokens": int, "total_tokens": int,
     "calls": [CallRecord, ...]}

CallRecord (one per LLM call ATTEMPT, in call order -- a retried call
produces one record per attempt, not just the successful one):
    {"call_index": int, "call_type": str, "started_at_unix": float,
     "elapsed_seconds": float, "input_message_count": int,
     "input_sources": List[str], "context": Optional[str],
     "prompt_tokens": Optional[int], "completion_tokens": Optional[int],
     "total_tokens": int, "token_source": str, "status": "ok"|"error"}
"""

from __future__ import annotations

from typing import Any, Dict, List


def summarize_calls(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Builds the trace-level totals from a list of per-call records
    already in the CallRecord shape above (see debate_usage.py). Every
    raw per-call record is preserved verbatim in `calls` -- aggregation
    never discards the per-attempt records it's computed from (PART 11)."""
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
