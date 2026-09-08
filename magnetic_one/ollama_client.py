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
"""

from __future__ import annotations

import time
import warnings
from typing import Any, Dict, Optional, Sequence

from autogen_core.models import AssistantMessage, ChatCompletionClient, LLMMessage
from autogen_ext.models.ollama import OllamaChatCompletionClient

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
    """Thin delegating wrapper that records token usage for each LLM call.

    It intentionally leaves generation behavior unchanged: all requests are
    forwarded verbatim to the underlying AutoGen Ollama client. Usage is read
    from the provider/AutoGen response when available, with a local token-count
    fallback for missing usage fields.
    """

    def __init__(self, client: ChatCompletionClient):
        self._client = client
        self._records = []
        self._call_index = 0

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
            "calls": list(self._records),
        }

    async def create(self, messages: Sequence[LLMMessage], *, tools=(), tool_choice="auto",
                     json_output=None, extra_create_args=None, cancellation_token=None):
        self._call_index += 1
        started = time.time()
        prompt_tokens = None
        completion_tokens = None
        token_source = "backend_usage"
        try:
            response = await self._client.create(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args if extra_create_args is not None else {},
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

            self._records.append({
                "call_index": self._call_index,
                "started_at_unix": started,
                "elapsed_seconds": time.time() - started,
                "input_message_count": len(messages),
                "input_sources": [getattr(m, "source", None) for m in messages],
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
                "token_source": token_source,
                "status": "ok",
            })
            return response
        except Exception as exc:
            self._records.append({
                "call_index": self._call_index,
                "started_at_unix": started,
                "elapsed_seconds": time.time() - started,
                "input_message_count": len(messages),
                "input_sources": [getattr(m, "source", None) for m in messages],
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": 0,
                "token_source": "unavailable",
                "status": "error",
                "error": type(exc).__name__,
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
    **kwargs: Any,
) -> ChatCompletionClient:
    """Build a ChatCompletionClient pointed at a local Ollama server."""
    merged_options = _merge_options(temperature, seed, options)
    try:
        if model_info is not None:
            return InstrumentedOllamaChatCompletionClient(OllamaChatCompletionClient(model=model, host=host, model_info=model_info, options=merged_options, **kwargs))
        return InstrumentedOllamaChatCompletionClient(OllamaChatCompletionClient(model=model, host=host, options=merged_options, **kwargs))
    except Exception as e:
        warnings.warn(
            f"Could not auto-detect capabilities for Ollama model '{model}' ({e}). "
            "Falling back to a conservative default model_info; pass model_info= "
            "explicitly for accurate behavior.",
            stacklevel=2,
        )
        return OllamaChatCompletionClient(
            model=model, host=host, model_info=_FALLBACK_MODEL_INFO, options=merged_options, **kwargs
        )