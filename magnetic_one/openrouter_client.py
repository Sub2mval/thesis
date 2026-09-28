"""OpenRouter backend for magnetic_one: autogen's OpenAI client pointed at
OpenRouter, with the same temperature=0 / seed=42 defaults as the Ollama
backends, wrapped in the same instrumentation and key rotation."""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from autogen_ext.models.openai import OpenAIChatCompletionClient

from magnetic_one.model_info import lookup_model_info
from magnetic_one.ollama_client import (
    DEFAULT_SEED, DEFAULT_TEMPERATURE, _FALLBACK_MODEL_INFO, InstrumentedOllamaChatCompletionClient,
)
from magnetic_one.ollama_cloud_client import RotatingKeyOllamaClient, _mask_key

DEFAULT_OPENROUTER_HOST = "https://openrouter.ai/api/v1"


def build_openrouter_client(
    model: str,
    api_key: str,
    host: str = DEFAULT_OPENROUTER_HOST,
    model_info: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = DEFAULT_TEMPERATURE,
    seed: Optional[int] = DEFAULT_SEED,
    key_index: int = 0,
) -> InstrumentedOllamaChatCompletionClient:
    kwargs: Dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if seed is not None:
        kwargs["seed"] = seed
    client = OpenAIChatCompletionClient(
        model=model, base_url=host, api_key=api_key,
        model_info=model_info or lookup_model_info(model) or _FALLBACK_MODEL_INFO, **kwargs,
    )
    return InstrumentedOllamaChatCompletionClient(
        client, model=model, host=host, key_identifier=_mask_key(api_key, key_index),
        generation_options=kwargs, provider="openrouter",
    )


def build_rotating_openrouter_client(
    model: str, api_keys: Sequence[str], host: str = DEFAULT_OPENROUTER_HOST,
    model_info: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = DEFAULT_TEMPERATURE, seed: Optional[int] = DEFAULT_SEED,
) -> RotatingKeyOllamaClient:
    return RotatingKeyOllamaClient(
        model=model, api_keys=api_keys, host=host,
        client_factory=lambda key, idx: build_openrouter_client(
            model, key, host, model_info, temperature, seed, idx),
    )
