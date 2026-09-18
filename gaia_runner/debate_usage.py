"""
llm_debate's call_llm() (langgraph_debate.py) discards the raw Ollama
response and returns only the text, so today no token counts exist
anywhere in that MAS -- unlike magnetic_one, which already tracks usage
via InstrumentedOllamaChatCompletionClient (see ../magnetic_one/ollama_client.py).
This module adds the same capability to llm_debate without editing its
files: it installs a drop-in replacement for call_llm that behaves
identically (same retry/model-selection logic) but also appends a
COMPLETE per-attempt call record -- not just a token count -- to a list
the caller controls.

Usage:
    records = []
    with patched_call_llm(records):
        run_debate(...)              # or run_paired_fork_experiment(...)
    stats = token_usage.summarize_calls(records)

Both langgraph_debate.call_llm AND error_injection.call_llm must be
patched: `from langgraph_debate import call_llm` in error_injection.py
creates a second, independent name binding, so patching one module's
attribute doesn't affect the other's.

Every ATTEMPT (not just successful calls) appends one record, in
chronological order, containing the complete request (messages, model,
host, generation options) and, on success, the complete raw response
(serialized via serialization.to_jsonsafe) -- not just its token counts.
See token_usage.py for the top-level aggregate shape this feeds.
"""

from __future__ import annotations

import contextlib
import itertools
import random
import time
from typing import Any, Dict, List, Optional

import ollama  # noqa: E402
from tenacity import retry, stop_after_attempt, wait_exponential  # noqa: E402

from llm_debate import langgraph_debate  # noqa: E402
from llm_debate import error_injection as debate_error_injection  # noqa: E402

from . import trace_io
from .serialization import to_jsonsafe

Message = Dict[str, Any]


def _field(resp: Any, name: str) -> Any:
    """Ollama's ChatResponse supports both mapping-style and attribute
    access depending on client version -- try both rather than assuming."""
    value = resp.get(name) if hasattr(resp, "get") else None
    return value if value is not None else getattr(resp, name, None)


def mask_api_key(key: Optional[str], index: Optional[int] = None) -> Optional[str]:
    """Never store a full API key in a trace (PART 14) -- a masked,
    non-secret identifier such as "key#0 (...abcd)" is fine for debugging."""
    if not key:
        return None
    label = f"key#{index}" if index is not None else "key"
    return f"{label} (...{key[-4:]})" if len(key) >= 4 else label


def _make_instrumented_call_llm(records: List[Dict[str, Any]]):
    """Same body as langgraph_debate.call_llm, plus one COMPLETE per-attempt
    call record (see module docstring) appended for every attempt -- not
    just successful ones (see the except branch below) -- in the same
    chronological list both MAS adapters feed into token_stats.calls."""

    counter = itertools.count(1)

    @retry(wait=wait_exponential(multiplier=1, min=4, max=10), stop=stop_after_attempt(5))
    def instrumented_call_llm(messages: List[Message], config: Dict[str, Any], call_type: str = "unknown") -> str:
        model_list = config["model_list"]
        model = random.choice(model_list)
        # Identity lookup (not equality/list.index) -- correct even if
        # model_list contains duplicate-looking entries.
        key_index = next((i for i, m in enumerate(model_list) if m is model), None)
        headers = {"authorization": f"Bearer {model['api_key']}"} if model.get("api_key") else None
        host = model.get("host", "http://localhost:11434")
        client = ollama.Client(host=host, headers=headers)
        options = {k: v for k, v in (("temperature", config.get("temperature")),
                                      ("num_predict", config.get("max_tokens")),
                                      ("seed", config.get("seed"))) if v is not None}
        call_index = next(counter)
        started = time.time()
        started_iso = trace_io.now_iso()

        request_record = {
            "messages": to_jsonsafe(messages),
            "tools": None,  # llm_debate's call_llm never passes tools to ollama.Client.chat
            "tool_choice": None,
            "json_output": None,
            "generation_options": to_jsonsafe(options),
            "temperature": config.get("temperature"),
            "seed": config.get("seed"),
            "other_request_parameters": {"max_tokens": config.get("max_tokens")},
        }
        common: Dict[str, Any] = {
            "call_index": call_index,
            "call_type": call_type,  # "agent_turn" | "reflection" | "trust_allocator" | "aggregate" | "corruption"
            "started_at": started_iso,
            "started_at_unix": started,
            "provider": "ollama",
            "model": model.get("model"),
            "host": host,
            "key_identifier": mask_api_key(model.get("api_key"), key_index),
            "request": request_record,
            # Back-compat flat fields kept alongside the richer `request`/
            # `response`/`tokens` shape below -- token_usage.summarize_calls
            # and every usage_stats() aggregator read these directly.
            "input_message_count": len(messages),
            "input_sources": [m.get("role") for m in messages],
            "context": model.get("model"),
        }
        try:
            resp = client.chat(model=model["model"], messages=messages, options=options)
            finished = time.time()
            message_obj = _field(resp, "message")
            content = _field(message_obj, "content") if message_obj is not None else None
            tool_calls = _field(message_obj, "tool_calls") if message_obj is not None else None
            prompt_tokens = _field(resp, "prompt_eval_count")
            completion_tokens = _field(resp, "eval_count")
            token_source = "ollama_native" if prompt_tokens is not None else "unavailable"
            records.append({
                **common,
                "finished_at": trace_io.now_iso(),
                "elapsed_seconds": finished - started,
                "response": {
                    "raw": to_jsonsafe(resp),
                    "generated_content": content,
                    "tool_calls": to_jsonsafe(tool_calls) if tool_calls else None,
                    "finish_info": {"done": _field(resp, "done"), "done_reason": _field(resp, "done_reason")},
                    "metadata": {"created_at": _field(resp, "created_at"), "model": _field(resp, "model")},
                },
                "tokens": {
                    "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                    "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
                    "token_source": token_source,
                },
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
                "token_source": token_source,
                "status": "ok",
                "error": None,
            })
            return content
        except Exception as exc:
            # Recorded (not discarded) even on failure -- tenacity will
            # retry the whole call, so one logical LLM call can produce
            # several of these records; n_failed_attempts in
            # token_usage.summarize_calls counts them (PART 3: "A
            # successful call after three failed attempts therefore
            # produces four records.").
            records.append({
                **common,
                "finished_at": trace_io.now_iso(),
                "elapsed_seconds": time.time() - started,
                "response": None,
                "tokens": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": 0, "token_source": "unavailable"},
                "prompt_tokens": None, "completion_tokens": None, "total_tokens": 0,
                "token_source": "unavailable",
                "status": "error",
                "error": {"type": type(exc).__name__, "message": str(exc)},
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
