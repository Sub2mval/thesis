"""
llm_debate's call_llm() (langgraph_debate.py) discards the raw Ollama
response and returns only the text, so today no token counts exist
anywhere in that MAS -- unlike magnetic_one, which already tracks usage
via InstrumentedOllamaChatCompletionClient (see ../magnetic_one/ollama_client.py).
This module adds the same capability to llm_debate without editing its
files: it installs a drop-in replacement for call_llm that behaves
identically (same retry/model-selection logic) but also appends a usage
record -- read from Ollama's native prompt_eval_count/eval_count fields
-- to a list the caller controls.

Usage:
    records = []
    with patched_call_llm(records):
        run_debate(...)              # or run_paired_fork_experiment(...)
    stats = token_usage.summarize_calls(records)

Both langgraph_debate.call_llm AND error_injection.call_llm must be
patched: `from langgraph_debate import call_llm` in error_injection.py
creates a second, independent name binding, so patching one module's
attribute doesn't affect the other's.
"""

from __future__ import annotations

import contextlib
import os
import random
import sys
import time
from typing import Any, Dict, List

import ollama  # noqa: E402
from tenacity import retry, stop_after_attempt, wait_exponential  # noqa: E402

from llm_debate import langgraph_debate  # noqa: E402
from llm_debate import error_injection as debate_error_injection  # noqa: E402

Message = Dict[str, Any]


def _field(resp: Any, name: str) -> Any:
    """Ollama's ChatResponse supports both mapping-style and attribute
    access depending on client version -- try both rather than assuming."""
    value = resp.get(name) if hasattr(resp, "get") else None
    return value if value is not None else getattr(resp, name, None)


def _make_instrumented_call_llm(records: List[Dict[str, Any]]):
    """Same body as langgraph_debate.call_llm, plus a usage record per
    ATTEMPT (not just per successful call -- see the except branch below),
    in the same rich shape magnetic_one's InstrumentedOllamaChatCompletionClient
    already used (started_at_unix/elapsed_seconds/input_message_count/
    input_sources/status/prompt_tokens/completion_tokens), so both MAS
    adapters produce directly comparable token_stats rather than debate's
    getting a smaller, lossier shape."""

    @retry(wait=wait_exponential(multiplier=1, min=4, max=10), stop=stop_after_attempt(5))
    def instrumented_call_llm(messages: List[Message], config: Dict[str, Any], call_type: str = "unknown") -> str:
        model = random.choice(config["model_list"])
        headers = {"authorization": f"Bearer {model['api_key']}"} if model.get("api_key") else None
        client = ollama.Client(host=model.get("host", "http://localhost:11434"), headers=headers)
        options = {k: v for k, v in (("temperature", config.get("temperature")),
                                      ("num_predict", config.get("max_tokens")),
                                      ("seed", config.get("seed"))) if v is not None}
        call_index = len(records) + 1
        started = time.time()
        common = {
            "call_index": call_index,
            "call_type": call_type,  # "agent_turn" | "reflection" | "gricean_check" | "aggregate" | "corruption"
            "started_at_unix": started, "input_message_count": len(messages),
            "input_sources": [m.get("role") for m in messages], "context": model.get("model"),
        }
        try:
            resp = client.chat(model=model["model"], messages=messages, options=options)
            prompt_tokens = _field(resp, "prompt_eval_count")
            completion_tokens = _field(resp, "eval_count")
            records.append({
                **common, "elapsed_seconds": time.time() - started,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
                "token_source": "ollama_native" if prompt_tokens is not None else "unavailable",
                "status": "ok",
            })
            return resp["message"]["content"]
        except Exception as exc:
            # Recorded (not discarded) even on failure -- tenacity will
            # retry the whole call, so one logical LLM call can produce
            # several of these records; n_failed_attempts in
            # token_usage.summarize_calls counts them.
            records.append({
                **common, "elapsed_seconds": time.time() - started,
                "prompt_tokens": None, "completion_tokens": None, "total_tokens": 0,
                "token_source": "unavailable", "status": "error", "error": f"{type(exc).__name__}: {exc}",
            })
            raise

    return instrumented_call_llm


@contextlib.contextmanager
def patched_call_llm(records: List[Dict[str, Any]]):
    """While active, both langgraph_debate.call_llm and
    error_injection.call_llm record usage into `records`. Restores the
    originals on exit (including on exception), so this nests safely
    across multiple traces/questions run in the same process."""
    instrumented = _make_instrumented_call_llm(records)
    original_debate = langgraph_debate.call_llm
    original_injection = debate_error_injection.call_llm
    langgraph_debate.call_llm = instrumented
    debate_error_injection.call_llm = instrumented
    try:
        yield
    finally:
        langgraph_debate.call_llm = original_debate
        debate_error_injection.call_llm = original_injection
