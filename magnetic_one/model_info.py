"""Known model_info for models autogen can't auto-detect (used by every
backend: local Ollama, Ollama Cloud, OpenRouter). Matching is done on a
normalised model name, so e.g. "gemma4:31b", "gemma4:31b-cloud" and
OpenRouter's "google/gemma-4-31b-it" all resolve to the same entry."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_COMMON = {"vision": True, "function_calling": True, "json_output": True,
           "family": "unknown", "structured_output": True}

# (pattern matched against the normalised name, model_info)
_KNOWN_MODELS = [
    (re.compile(r"gemma-?4.*31b"), dict(_COMMON)),
    (re.compile(r"qwen-?3\.8.*27b"), dict(_COMMON)),
]


def lookup_model_info(model: Optional[str]) -> Optional[Dict[str, Any]]:
    name = (model or "").lower()
    for pattern, info in _KNOWN_MODELS:
        if pattern.search(name):
            return dict(info)
    return None
