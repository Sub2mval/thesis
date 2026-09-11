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
from typing import Any, Dict, List

_LLM_DEBATE_DIR = os.path.join(os.path.dirname(__file__), "..", "llm_debate")
if os.path.abspath(_LLM_DEBATE_DIR) not in sys.path:
    sys.path.insert(0, os.path.abspath(_LLM_DEBATE_DIR))

import ollama  # noqa: E402
from tenacity import retry, stop_after_attempt, wait_exponential  # noqa: E402

import langgraph_debate  # noqa: E402
import error_injection as debate_error_injection  # noqa: E402

Message = Dict[str, Any]


def _field(resp: Any, name: str) -> Any:
    """Ollama's ChatResponse supports both mapping-style and attribute
    access depending on client version -- try both rather than assuming."""
    value = resp.get(name) if hasattr(resp, "get") else None
    return value if value is not None else getattr(resp, name, None)


def _make_instrumented_call_llm(records: List[Dict[str, Any]]):
    """Same body as langgraph_debate.call_llm, plus a usage record per call."""

    @retry(wait=wait_exponential(multiplier=1, min=4, max=10), stop=stop_after_attempt(5))
    def instrumented_call_llm(messages: List[Message], config: Dict[str, Any]) -> str:
        model = random.choice(config["model_list"])
        client = ollama.Client(host=model.get("host", "http://localhost:11434"))
        options = {k: v for k, v in (("temperature", config.get("temperature")),
                                      ("num_predict", config.get("max_tokens")),
                                      ("seed", config.get("seed"))) if v is not None}
        resp = client.chat(model=model["model"], messages=messages, options=options)
        input_tokens = _field(resp, "prompt_eval_count")
        output_tokens = _field(resp, "eval_count")
        records.append({
            "call_index": len(records) + 1,
            "context": model["model"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": (input_tokens or 0) + (output_tokens or 0),
            "token_source": "ollama_native" if input_tokens is not None else "unavailable",
        })
        return resp["message"]["content"]

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
