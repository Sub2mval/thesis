"""
Small, generic helpers for getting *structured* JSON out of a chat model.

Extracted out of orchestrator_graph.py because two different call sites
(the progress ledger and the Gricean adherence checker) each used to have
their own hand-rolled "ask, validate, retry with a correction message"
loop. `call_model_for_json` below is that loop, written once.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from autogen_core.models import AssistantMessage, ChatCompletionClient, LLMMessage, UserMessage
from autogen_core.utils import extract_json_from_str

logger = logging.getLogger("magentic_one_langgraph.json_llm")

MAX_JSON_RETRIES = 10

# Neither the old progress_ledger context nor the checker's windowed
# conversation bounded message size -- a single large agent output (e.g. a
# full spreadsheet dumped into a message, common on GAIA file-attachment
# tasks) could blow straight through the model's context window (confirmed
# against a real production failure: "prompt too long: 460751, max:
# 262144"). Capping here, at the point a message enters the thread,
# protects every downstream consumer without repeating the logic there.
MAX_MESSAGE_CHARS = 20_000

# Validator return shape: (is_valid, error_detail_for_the_model, cleaned_dict)
Validator = Callable[[Dict[str, Any]], Tuple[bool, Optional[str], Optional[Dict[str, Any]]]]


def truncate_message_content(content: str, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Cap a single message's length, keeping head and tail (where
    context-setting and concluding/final content tend to live), with an
    explicit marker so truncation is visible in the data, not silently
    lossy."""
    if not isinstance(content, str) or len(content) <= max_chars:
        return content
    half = max_chars // 2
    omitted = len(content) - max_chars
    return (
        content[:half]
        + f"\n\n... [{omitted} characters truncated -- message was {len(content)} characters total] ...\n\n"
        + content[-half:]
    )


def robust_extract_json(content: str) -> List[Dict[str, Any]]:
    """Extract JSON object(s) from a model response, robust to the model
    wrapping its answer in a markdown fence that ITSELF contains a nested
    fenced code block (e.g. a shell command inside a JSON string value --
    a common pattern here, since that's the normal way to hand code to the
    terminal agent).

    autogen_core.utils.extract_json_from_str's fence regex is non-greedy,
    so on input containing a nested ``` fence inside a JSON string value it
    matches the FIRST closing fence it finds -- the inner one -- silently
    truncating the extracted text mid-object. Confirmed against a real
    production failure trace.

    Strategy: strip a leading/trailing outer fence by POSITION (immune to
    what's nested inside) and try that first; fall back to raw parsing (no
    fence at all), then to autogen_core's own extractor as a last resort.
    """
    text = content.strip() if isinstance(content, str) else content

    if isinstance(text, str) and text.startswith("```"):
        first_newline = text.find("\n")
        inner = text[first_newline + 1 :] if first_newline != -1 else text[3:]
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3]
        try:
            return [json.loads(inner)]
        except json.JSONDecodeError:
            pass  # fall through

    if isinstance(text, str):
        try:
            return [json.loads(text)]
        except json.JSONDecodeError:
            pass

    return extract_json_from_str(content)


async def call_model_for_json(
    model_client: ChatCompletionClient,
    get_compatible_context: Callable[[ChatCompletionClient, List[LLMMessage]], List[LLMMessage]],
    base_context: List[LLMMessage],
    validate: Validator,
    source: str,
    max_retries: int = MAX_JSON_RETRIES,
) -> Dict[str, Any]:
    """Call `model_client` on `base_context`, parse the response as a
    single JSON object, and pass it through `validate`. On a parse failure
    or a `validate` rejection, retry with ONE extra correction turn
    appended (never accumulated across attempts, so retries can't balloon
    the context window) explaining what was wrong. Raises ValueError if
    still invalid after `max_retries` attempts.
    """
    correction: Optional[Tuple[str, str]] = None
    consecutive_parse_failures = 0
    raw: Optional[str] = None
    error_detail: Optional[str] = None

    for attempt in range(max_retries):
        if correction is not None and consecutive_parse_failures < 2:
            bad_response, prior_error = correction
            context = base_context + [
                AssistantMessage(content=bad_response, source=source),
                UserMessage(
                    content=f"That response was invalid: {prior_error} Respond again with ONLY the "
                    "corrected JSON object, matching the schema exactly.",
                    source=source,
                ),
            ]
        else:
            context = base_context

        compatible = get_compatible_context(model_client, context)
        if model_client.model_info.get("json_output", False):
            response = await model_client.create(compatible, json_output=True)
        else:
            response = await model_client.create(compatible)
        raw = response.content

        try:
            assert isinstance(raw, str)
            parsed_objects = robust_extract_json(raw)
            if len(parsed_objects) != 1:
                raise ValueError("expected exactly one JSON object in the response")
            ok, error_detail, cleaned = validate(parsed_objects[0])
            if ok:
                assert cleaned is not None
                return cleaned
            consecutive_parse_failures = 0  # a schema-shape mistake, not a parse failure
        except (json.JSONDecodeError, TypeError, ValueError, AssertionError) as e:
            error_detail = error_detail or f"the response was not valid, parsable JSON on its own ({e})."
            consecutive_parse_failures += 1

        logger.warning(
            "call_model_for_json: attempt %d/%d failed (%s). Raw response preview: %r",
            attempt + 1,
            max_retries,
            error_detail,
            (raw or "")[:300],
        )
        correction = (raw or "", error_detail or "the response did not match the required schema.")

    raise ValueError(
        f"Failed to get valid JSON after {max_retries} retries ({error_detail}). "
        f"Last raw response: {(raw or '')[:2000]!r}"
    )