"""
Common token-usage schema shared by both MAS adapters, so a trace's
`token_stats` looks the same regardless of which MAS produced it:

    {"n_llm_calls": int, "input_tokens": int, "output_tokens": int,
     "total_tokens": int, "calls": [CallRecord, ...]}

CallRecord (one per LLM call, in call order):
    {"call_index": int, "context": Optional[str], "input_tokens": Optional[int],
     "output_tokens": Optional[int], "total_tokens": int, "token_source": str}

`context` is a short free-text label for which agent/step the call
belongs to (e.g. a source name, or the model name for llm_debate, whose
calls aren't attributed to a named agent).
"""

from __future__ import annotations

from typing import Any, Dict, List


def summarize_calls(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Builds the trace-level totals from a list of per-call records
    already in the CallRecord shape above."""
    input_tokens = sum(c.get("input_tokens") or 0 for c in calls)
    output_tokens = sum(c.get("output_tokens") or 0 for c in calls)
    return {
        "n_llm_calls": len(calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "calls": calls,
    }


def from_magnetic_token_stats(token_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Adapts magnetic_one's own usage tracking (ollama_client.py's
    prompt_tokens/completion_tokens naming) to the common schema above,
    without touching magnetic_one's code. Accepts either the full
    `token_stats` dict MagenticOneLangGraph.run() attaches, or a bare
    {"calls": [...]} dict assembled by gaia_runner itself (see
    magnetic_system.py's _combined_usage)."""
    calls = [
        {
            "call_index": c.get("call_index"),
            "context": ",".join(s for s in (c.get("input_sources") or []) if s) or None,
            "input_tokens": c.get("prompt_tokens"),
            "output_tokens": c.get("completion_tokens"),
            "total_tokens": c.get("total_tokens", 0),
            "token_source": c.get("token_source"),
        }
        for c in token_stats.get("calls", [])
    ]
    return summarize_calls(calls)
