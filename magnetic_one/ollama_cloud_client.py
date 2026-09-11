"""
Ollama Cloud support: rotate across multiple API keys so a GAIA run doesn't
die the moment one key's rate/usage limit is hit.

IMPORTANT: autogen_ext's OllamaChatCompletionClient only forwards a `host`
kwarg to the real ollama.AsyncClient -- any `headers=` kwarg you pass to it
is silently dropped (see `ollama_init_kwargs = {"host"}` in
autogen_ext.models.ollama._ollama_client). So per-instance Authorization
headers can't be set that way -- an earlier version of this module produced
uniform 401s regardless of which key was "active" because of exactly this.

The underlying ollama-python client instead reads the OLLAMA_API_KEY
environment variable exactly ONCE, at construction time, and bakes the
resulting Authorization header permanently into that client's httpx
headers. So each of our N clients is built by temporarily setting
OLLAMA_API_KEY to that client's key, constructing it, then restoring the
env var.

`options` (unlike `headers`/`host`) IS genuinely forwarded to the real
per-request API call -- verified against
autogen_ext.models.ollama._ollama_client._create_args_from_config, which
explicitly preserves and merges it. So temperature/seed set here for
reproducibility actually take effect, unlike the headers issue above.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from ollama import ResponseError
from autogen_core import CancellationToken
from autogen_core.models import ChatCompletionClient, CreateResult, LLMMessage

from magnetic_one.ollama_client import InstrumentedOllamaChatCompletionClient, get_usage_tracking as _get_local_usage
from autogen_core.tools import Tool, ToolSchema
from pydantic import BaseModel

from autogen_ext.models.ollama import OllamaChatCompletionClient

logger = logging.getLogger("magentic_one_langgraph.ollama_cloud_client")

DEFAULT_OLLAMA_CLOUD_HOST = "https://ollama.com"

# Reproducibility defaults -- fixed so repeated runs (and reruns after a
# crash, via resume=True) are directly comparable. Pass temperature=None /
# seed=None to RotatingKeyOllamaClient to disable either.
DEFAULT_TEMPERATURE: Optional[float] = 0
DEFAULT_SEED: Optional[int] = 42

_TRANSIENT_RETRY_STATUS_CODES = {429, 529}
_TRANSIENT_RETRY_KEYWORDS = ("rate limit", "quota", "too many requests", "limit exceeded")
_AUTH_ERROR_STATUS_CODES = {401, 403}

_FALLBACK_MODEL_INFO = {
    "vision": False,
    "function_calling": True,
    "json_output": True,
    "family": "unknown",
    "structured_output": False,
}


@contextlib.contextmanager
def _temporarily_set_env(var_name: str, value: str):
    old = os.environ.get(var_name)
    os.environ[var_name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(var_name, None)
        else:
            os.environ[var_name] = old


def load_api_keys_from_env(prefix: str = "OLLAMA_API_KEY", env_file: Optional[str] = ".env") -> List[str]:
    if env_file:
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file)
        except ImportError:
            logger.warning(
                "python-dotenv not installed (`pip install python-dotenv`); "
                "relying on already-exported environment variables instead."
            )

    keys: List[str] = []
    if os.getenv(prefix):
        keys.append(os.environ[prefix])
    i = 1
    while True:
        val = os.getenv(f"{prefix}_{i}")
        if val is None:
            break
        keys.append(val)
        i += 1

    keys = [k.strip().strip('"').strip("'") for k in keys]
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        raise ValueError(
            f"No API keys found. Expected env vars like {prefix}_1, {prefix}_2, ... "
            f"(or a single {prefix}) in your environment or in '{env_file}'."
        )
    return keys


def _classify_error(e: Exception) -> Optional[str]:
    if isinstance(e, ResponseError):
        if e.status_code in _TRANSIENT_RETRY_STATUS_CODES:
            return "transient"
        if e.status_code in _AUTH_ERROR_STATUS_CODES:
            return "auth"
    msg = str(e).lower()
    if any(kw in msg for kw in _TRANSIENT_RETRY_KEYWORDS):
        return "transient"
    return None


def _merge_options(
    temperature: Optional[float], seed: Optional[int], options: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    merged: Dict[str, Any] = {}
    if temperature is not None:
        merged["temperature"] = temperature
    if seed is not None:
        merged["seed"] = seed
    if options:
        merged.update(options)
    return merged or None


def _build_client_for_key(
    model: str,
    host: str,
    key: str,
    model_info: Optional[dict],
    options: Optional[Dict[str, Any]],
) -> ChatCompletionClient:
    with _temporarily_set_env("OLLAMA_API_KEY", key):
        try:
            if model_info is not None:
                return InstrumentedOllamaChatCompletionClient(OllamaChatCompletionClient(model=model, host=host, model_info=model_info, options=options))
            return InstrumentedOllamaChatCompletionClient(OllamaChatCompletionClient(model=model, host=host, options=options))
        except ValueError as e:
            if "model_info is required" not in str(e):
                raise
            logger.warning(
                "Could not auto-detect capabilities for model '%s' (%s). Falling back to a permissive "
                "default model_info; pass model_info= explicitly for accurate behavior.",
                model,
                e,
            )
            return OllamaChatCompletionClient(model=model, host=host, model_info=_FALLBACK_MODEL_INFO, options=options)


class RotatingKeyOllamaClient:
    """Duck-types as a ChatCompletionClient, backed by N real
    OllamaChatCompletionClients (one per API key). Rotates on rate-limit-
    shaped errors; if every key fails in a full pass, backs off and retries
    indefinitely (up to max_consecutive_full_cycles as a safety valve
    against a permanent misconfiguration being mistaken for quota
    exhaustion)."""

    def __init__(
        self,
        model: str,
        api_keys: Sequence[str],
        host: str = DEFAULT_OLLAMA_CLOUD_HOST,
        model_info: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = DEFAULT_TEMPERATURE,
        seed: Optional[int] = DEFAULT_SEED,
        options: Optional[Dict[str, Any]] = None,
        base_backoff_seconds: float = 5.0,
        max_backoff_seconds: float = 120.0,
        max_consecutive_full_cycles: Optional[int] = 20,
    ):
        if not api_keys:
            raise ValueError("api_keys must be non-empty.")
        self._keys = list(api_keys)
        merged_options = _merge_options(temperature, seed, options)
        self._clients: List[ChatCompletionClient] = [
            _build_client_for_key(model=model, host=host, key=key, model_info=model_info, options=merged_options)
            for key in self._keys
        ]
        self._idx = 0
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._max_consecutive_full_cycles = max_consecutive_full_cycles

    def _key_label(self, idx: int) -> str:
        k = self._keys[idx]
        return f"key#{idx} (...{k[-4:]})" if len(k) >= 4 else f"key#{idx}"

    def _rotate(self) -> None:
        self._idx = (self._idx + 1) % len(self._clients)

    @property
    def model_info(self):
        return self._clients[self._idx].model_info

    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Any = "auto",
        json_output: Optional[bool | type[BaseModel]] = None,
        extra_create_args: Any = {},
        cancellation_token: Optional[CancellationToken] = None,
    ) -> CreateResult:
        backoff = self._base_backoff
        start_idx = self._idx
        consecutive_full_cycles = 0
        auth_errors_this_cycle: List[str] = []

        while True:
            client = self._clients[self._idx]
            try:
                return await client.create(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    json_output=json_output,
                    extra_create_args=extra_create_args,
                    cancellation_token=cancellation_token,
                )
            except Exception as e:
                kind = _classify_error(e)
                if kind is None:
                    raise

                if kind == "auth":
                    auth_errors_this_cycle.append(self._key_label(self._idx))
                    logger.warning(
                        "Auth error on %s: %s. Trying next key (this usually means a bad/expired key, "
                        "wrong host, or no access to this model -- not something that clears with time).",
                        self._key_label(self._idx),
                        e,
                    )
                else:
                    logger.warning("Rate limit hit on %s: %s. Rotating to next key.", self._key_label(self._idx), e)

                self._rotate()

                if self._idx == start_idx:
                    if len(auth_errors_this_cycle) == len(self._clients):
                        raise RuntimeError(
                            f"All {len(self._clients)} API key(s) failed authentication (401/403) on this same "
                            f"request. This is almost never a temporary rate limit -- it usually means the "
                            f"key(s), host, or model access is misconfigured. Keys tried: {auth_errors_this_cycle}."
                        ) from e
                    auth_errors_this_cycle = []

                    consecutive_full_cycles += 1
                    if (
                        self._max_consecutive_full_cycles is not None
                        and consecutive_full_cycles >= self._max_consecutive_full_cycles
                    ):
                        logger.error(
                            "All %d key(s) still failing after %d consecutive full cycles -- giving up.",
                            len(self._clients),
                            consecutive_full_cycles,
                        )
                        raise
                    logger.warning(
                        "All %d key(s) rate-limited (cycle %d/%s). Sleeping %.0fs before retrying.",
                        len(self._clients),
                        consecutive_full_cycles,
                        self._max_consecutive_full_cycles if self._max_consecutive_full_cycles is not None else "inf",
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff)

    def reset_usage(self) -> None:
        for client in self._clients:
            reset = getattr(client, "reset_usage", None)
            if callable(reset):
                reset()

    def usage_stats(self) -> Dict[str, Any]:
        records = []
        for key_idx, client in enumerate(self._clients):
            stats = _get_local_usage(client)
            for record in stats.get("calls", []):
                r = dict(record)
                r["client_key_index"] = key_idx
                records.append(r)
        records.sort(key=lambda r: (r.get("started_at_unix", 0), r.get("call_index", 0)))
        prompt_tokens = sum(r.get("prompt_tokens") or 0 for r in records)
        completion_tokens = sum(r.get("completion_tokens") or 0 for r in records)
        return {
            "n_llm_calls": len(records),
            "n_successful_calls": sum(r.get("status") == "ok" for r in records),
            "n_failed_attempts": sum(r.get("status") == "error" for r in records),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "calls": records,
        }

    def count_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        return self._clients[self._idx].count_tokens(messages, tools=tools)

    def remaining_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        return self._clients[self._idx].remaining_tokens(messages, tools=tools)