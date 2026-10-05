"""
llm_debate's call_llm() (langgraph_debate.py) returns final text after
executing any GAIA web tools requested by the model. This module adds the
same usage instrumentation as magnetic_one, recording the complete
request/response shape plus the tool evidence used during each logical
LLM call.

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
            "tools": to_jsonsafe(langgraph_debate.gaia_utils.WEB_TOOL_DEFINITIONS) if call_type in {"agent_turn", "corruption"} else [],
            "tool_choice": "auto" if call_type in {"agent_turn", "corruption"} else None,
            "json_output": None,
            "generation_options": {**to_jsonsafe(options), "think": True},
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
        class _RecordingClient:
            def __init__(self, inner):
                self.inner = inner
                self.responses = []
                self.started = []
                self.finished = []

            def chat(self, **kwargs):
                t0 = time.time()
                response = self.inner.chat(**kwargs)
                t1 = time.time()
                self.responses.append(response)
                self.started.append(t0)
                self.finished.append(t1)
                return response

        try:
            recorder = _RecordingClient(client)
            tools = langgraph_debate.gaia_utils.WEB_TOOL_DEFINITIONS if call_type in {"agent_turn", "corruption"} else []
            content, _, raw_responses = langgraph_debate._chat_with_tools(
                recorder, model["model"], messages, options, config, tools=tools
            )
            finished = time.time()
            prompt_counts = [_field(r, "prompt_eval_count") for r in raw_responses]
            completion_counts = [_field(r, "eval_count") for r in raw_responses]
            prompt_tokens = sum(v or 0 for v in prompt_counts) if any(v is not None for v in prompt_counts) else None
            completion_tokens = sum(v or 0 for v in completion_counts) if any(v is not None for v in completion_counts) else None
            token_source = "ollama_native" if prompt_tokens is not None else "unavailable"
            final_resp = raw_responses[-1] if raw_responses else None
            final_message = _field(final_resp, "message") if final_resp is not None else None
            final_tool_calls = _field(final_message, "tool_calls") if final_message is not None else None
            records.append({
                **common,
                "finished_at": trace_io.now_iso(),
                "elapsed_seconds": finished - started,
                "response": {
                    "raw": to_jsonsafe(final_resp),
                    "generated_content": content,
                    "tool_calls": to_jsonsafe(final_tool_calls) if final_tool_calls else None,
                    "tool_rounds": to_jsonsafe(raw_responses[:-1]),
                    "finish_info": {"done": _field(final_resp, "done") if final_resp is not None else None,
                                    "done_reason": _field(final_resp, "done_reason") if final_resp is not None else None},
                    "metadata": {"created_at": _field(final_resp, "created_at") if final_resp is not None else None,
                                 "model": _field(final_resp, "model") if final_resp is not None else None},
                },
                "tool_events": to_jsonsafe(config.get("_last_tool_messages", [])),
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
