"""
Helper for building autogen `ChatCompletionClient`s backed by a local Ollama
server, using autogen_ext's built-in Ollama client.

Defaults to temperature=0 and a fixed seed for reproducibility -- both are
genuinely forwarded to the real API call (verified against
autogen_ext.models.ollama._ollama_client._create_args_from_config, which
explicitly preserves and merges the `options` dict, unlike `headers`/`host`
which get silently dropped -- see ollama_cloud_client.py's docstring for
that unrelated issue). Pass temperature=None / seed=None to disable either,
or options={...} to set other Ollama generation options directly.

Instrumentation note: `extra_create_args` is a normal, already-supported
parameter of ChatCompletionClient.create() -- callers that want a
particular call labeled in the trace (e.g. the Trust_Allocator, a
reflection call, or FM-1.1 corruption) pass
`extra_create_args={"call_type": "trust_allocator"}` (etc) at the call
site. InstrumentedOllamaChatCompletionClient.create() below pops that key
out of `extra_create_args` before forwarding the rest on to the real
client -- the underlying Ollama API never sees it -- and uses it purely
as a label on the resulting call record. Call sites that don't pass one
get "agent_call" (e.g. autogen's own FileSurfer/WebSurfer/Coder agents,
which call `create()` from deep inside the autogen library with no
opportunity for this project's code to inject a label).
"""

from __future__ import annotations

import time
import warnings
from typing import Any, Dict, Optional, Sequence

from autogen_core.models import AssistantMessage, ChatCompletionClient, LLMMessage
from autogen_ext.models.ollama import OllamaChatCompletionClient

from gaia_runner.serialization import to_jsonsafe
from gaia_runner import trace_io

DEFAULT_OLLAMA_HOST = "http://localhost:11434"


def _usage_value(usage: Any, *names: str) -> Optional[int]:
    if usage is None:
        return None
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    if isinstance(usage, dict):
        for name in names:
            value = usage.get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
    return None


class InstrumentedOllamaChatCompletionClient:
    """Thin delegating wrapper that records a COMPLETE per-call record
    (request, response, tokens, timing, status) for each LLM call.

    It intentionally leaves generation behavior unchanged: all requests are
    forwarded verbatim to the underlying AutoGen Ollama client (apart from
    stripping the `call_type` instrumentation label out of
    extra_create_args before forwarding -- see module docstring). Usage is
    read from the provider/AutoGen response when available, with a local
    token-count fallback for missing usage fields.
    """

    def __init__(
        self,
        client: ChatCompletionClient,
        *,
        model: Optional[str] = None,
        host: Optional[str] = None,
        key_identifier: Optional[str] = None,
        generation_options: Optional[Dict[str, Any]] = None,
    ):
        self._client = client
        self._records = []
        self._call_index = 0
        # Metadata about THIS client instance (one model/host/key per
        # instance -- see build_ollama_client / ollama_cloud_client.py),
        # attached to every call record it produces (PART 3).
        self._model = model
        self._host = host
        self._key_identifier = key_identifier
        self._generation_options = generation_options or {}

    @property
    def model_info(self):
        return self._client.model_info

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def reset_usage(self) -> None:
        self._records.clear()
        self._call_index = 0

    def usage_stats(self) -> Dict[str, Any]:
        successful = [r for r in self._records if r.get("status") == "ok"]
        prompt_values = [r["prompt_tokens"] for r in successful if r.get("prompt_tokens") is not None]
        completion_values = [r["completion_tokens"] for r in successful if r.get("completion_tokens") is not None]
        return {
            "n_llm_calls": len(self._records),
            "n_successful_calls": len(successful),
            "n_failed_attempts": sum(r.get("status") == "error" for r in self._records),
            "prompt_tokens": sum(prompt_values) if prompt_values else 0,
            "completion_tokens": sum(completion_values) if completion_values else 0,
            "total_tokens": sum(prompt_values) + sum(completion_values),
            "total_elapsed_seconds": sum(r.get("elapsed_seconds") or 0 for r in self._records),
            "calls": list(self._records),
        }

    async def create(self, messages: Sequence[LLMMessage], *, tools=(), tool_choice="auto",
                     json_output=None, extra_create_args=None, cancellation_token=None):
        self._call_index += 1
        started = time.time()
        started_iso = trace_io.now_iso()
        prompt_tokens = None
        completion_tokens = None
        token_source = "backend_usage"

        # `call_type` is an instrumentation-only label, never something the
        # real Ollama API understands -- pop it out of a COPY of
        # extra_create_args so the underlying client never sees it, while
        # every other key (a real create() option) is forwarded unchanged.
        raw_extra = dict(extra_create_args) if extra_create_args else {}
        call_type = raw_extra.pop("call_type", "agent_call")
        forwarded_extra = raw_extra

        common: Dict[str, Any] = {
            "call_index": self._call_index,
            "call_type": call_type,
            "started_at": started_iso,
            "started_at_unix": started,
            "provider": "ollama",
            "model": self._model,
            "host": self._host,
            "key_identifier": self._key_identifier,
            "request": {
                "messages": to_jsonsafe(messages),
                "tools": to_jsonsafe(tools) if tools else [],
                "tool_choice": to_jsonsafe(tool_choice),
                "json_output": to_jsonsafe(json_output),
                "generation_options": to_jsonsafe(self._generation_options),
                "temperature": self._generation_options.get("temperature"),
                "seed": self._generation_options.get("seed"),
                "other_request_parameters": to_jsonsafe(forwarded_extra) if forwarded_extra else {},
            },
            # Back-compat flat fields, unchanged shape from before this pass.
            "input_message_count": len(messages),
            "input_sources": [getattr(m, "source", None) for m in messages],
        }

        try:
            response = await self._client.create(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=forwarded_extra,
                cancellation_token=cancellation_token,
            )

            usage = getattr(response, "usage", None)
            prompt_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
            completion_tokens = _usage_value(usage, "completion_tokens", "output_tokens")

            if prompt_tokens is None:
                try:
                    prompt_tokens = int(self._client.count_tokens(messages, tools=tools))
                    token_source = "mixed_backend_and_estimated" if completion_tokens is not None else "estimated"
                except Exception:
                    pass

            if completion_tokens is None:
                content = getattr(response, "content", None)
                if isinstance(content, str):
                    try:
                        completion_tokens = int(
                            self._client.count_tokens([AssistantMessage(content=content)], tools=())
                        )
                        token_source = "mixed_backend_and_estimated" if prompt_tokens is not None else "estimated"
                    except Exception:
                        pass

            content = getattr(response, "content", None)
            generated_content = content if isinstance(content, str) else None
            tool_calls = content if not isinstance(content, str) else None

            self._records.append({
                **common,
                "finished_at": trace_io.now_iso(),
                "elapsed_seconds": time.time() - started,
                "response": {
                    "raw": to_jsonsafe(response),
                    "generated_content": generated_content,
                    "tool_calls": to_jsonsafe(tool_calls) if tool_calls is not None else None,
                    "finish_info": {"finish_reason": getattr(response, "finish_reason", None)},
                    "metadata": {
                        "cached": getattr(response, "cached", None),
                        "thought": getattr(response, "thought", None),
                    },
                },
                "tokens": {
                    "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                    "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
                    "token_source": token_source,
                },
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
                "token_source": token_source,
                "status": "ok",
                "error": None,
            })
            return response
        except Exception as exc:
            self._records.append({
                **common,
                "finished_at": trace_io.now_iso(),
                "elapsed_seconds": time.time() - started,
                "response": None,
                "tokens": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": 0, "token_source": "unavailable"},
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": 0,
                "token_source": "unavailable",
                "status": "error",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            })
            raise

    def count_tokens(self, messages, *, tools=()):
        return self._client.count_tokens(messages, tools=tools)

    def remaining_tokens(self, messages, *, tools=()):
        return self._client.remaining_tokens(messages, tools=tools)


def reset_usage_tracking(client: Any) -> None:
    fn = getattr(client, "reset_usage", None)
    if callable(fn):
        fn()


def get_usage_tracking(client: Any) -> Dict[str, Any]:
    fn = getattr(client, "usage_stats", None)
    if callable(fn):
        return fn()
    return {
        "n_llm_calls": 0,
        "n_successful_calls": 0,
        "n_failed_attempts": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "total_elapsed_seconds": 0,
        "calls": [],
    }


# Reproducibility defaults -- fixed so repeated runs (and reruns after a
# crash, via resume=True) are directly comparable.
DEFAULT_TEMPERATURE: Optional[float] = 0
DEFAULT_SEED: Optional[int] = 42

_FALLBACK_MODEL_INFO: Dict[str, Any] = {
    "vision": False,
    "function_calling": True,
    "json_output": True,
    "family": "unknown",
    "structured_output": False,
}


def _merge_options(
    temperature: Optional[float], seed: Optional[int], options: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    merged: Dict[str, Any] = {}
    if temperature is not None:
        merged["temperature"] = temperature
    if seed is not None:
        merged["seed"] = seed
    if options:
        merged.update(options)  # explicit options win over the temperature/seed defaults
    return merged or None


def build_ollama_client(
    model: str,
    host: str = DEFAULT_OLLAMA_HOST,
    model_info: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = DEFAULT_TEMPERATURE,
    seed: Optional[int] = DEFAULT_SEED,
    options: Optional[Dict[str, Any]] = None,
    key_identifier: Optional[str] = None,
    **kwargs: Any,
) -> ChatCompletionClient:
    """Build a ChatCompletionClient pointed at a local Ollama server."""
    merged_options = _merge_options(temperature, seed, options)
    try:
        if model_info is not None:
            return InstrumentedOllamaChatCompletionClient(
                OllamaChatCompletionClient(model=model, host=host, model_info=model_info, options=merged_options, **kwargs),
                model=model, host=host, key_identifier=key_identifier, generation_options=merged_options,
            )
        return InstrumentedOllamaChatCompletionClient(
            OllamaChatCompletionClient(model=model, host=host, options=merged_options, **kwargs),
            model=model, host=host, key_identifier=key_identifier, generation_options=merged_options,
        )
    except Exception as e:
        warnings.warn(
            f"Could not auto-detect capabilities for Ollama model '{model}' ({e}). "
            "Falling back to a conservative default model_info; pass model_info= "
            "explicitly for accurate behavior.",
            stacklevel=2,
        )
        return InstrumentedOllamaChatCompletionClient(
            OllamaChatCompletionClient(model=model, host=host, model_info=_FALLBACK_MODEL_INFO,
                                        options=merged_options, **kwargs),
            model=model, host=host, key_identifier=key_identifier, generation_options=merged_options,
        )
