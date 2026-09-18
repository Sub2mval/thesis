"""
Generic wiring shared across agent nodes: turning MessageHistory into an
LLM-ready context, describing the team, and adapting AutoGen ChatAgents so
the graph can call them uniformly.

Nothing here is Gricean-check-specific -- reflection injection is each
agent node's own responsibility (orchestrator_agent.py / worker_agent.py
prepend `state["pending_reflection"]` to their own context/instruction
when it's set); this module just builds the plain, unmodified context.

build_llm_context's optional `wrap_last_with_level` does the equivalent
job for the experiment-design/Trust_Allocator extension's delivery-time
notice (state["pending_trust_level"], set by gricean_checker.py's
Design 1-3 branch): it wraps only the LAST message's *content* in the
returned LLMMessage list, using trust_allocator.legacy_trust_allocator.
wrap_with_trust_notice -- the underlying `messages` list passed in is
never mutated, matching the "notices are delivery-time context only"
requirement.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Dict, List, Optional

from autogen_agentchat.base import ChatAgent
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from autogen_core.models import AssistantMessage, ChatCompletionClient, LLMMessage, UserMessage

from magnetic_one.state import ThreadMessage
from trust_allocator.legacy_trust_allocator import wrap_with_trust_notice

ORCHESTRATOR_NAME = "MagenticOneOrchestrator"

AgentCaller = Callable[[str, CancellationToken], Awaitable[str]]


def get_compatible_context(model_client: ChatCompletionClient, messages: List[LLMMessage]) -> List[LLMMessage]:
    """Strip images from the context for models that can't see them."""
    if model_client.model_info.get("vision", False):
        return messages
    from autogen_agentchat.utils import remove_images

    return remove_images(messages)


def build_llm_context(messages: List[ThreadMessage], wrap_last_with_level: Optional[str] = None) -> List[LLMMessage]:
    """Turn the permanent thread into plain LLM messages -- one-to-one,
    no notices or reflections spliced in here, EXCEPT that when
    `wrap_last_with_level` is given (a "low"/"medium"/"high" trust
    level, from state["pending_trust_level"]), the last message's
    content is prepended with that level's trust notice in the returned
    copy only -- `messages` itself is never touched. None (the default,
    and design 4's only value) is a no-op, same as passing an unassessed
    "undefined" level. Callers append whatever extra per-call context
    (a reflection, a task prompt) they need on top of this."""
    context: List[LLMMessage] = []
    last_index = len(messages) - 1
    for i, m in enumerate(messages):
        content = m["content"]
        if i == last_index and wrap_last_with_level:
            content = wrap_with_trust_notice(content, wrap_last_with_level)
        if m["source"] == ORCHESTRATOR_NAME:
            context.append(AssistantMessage(content=content, source=m["source"]))
        else:
            context.append(UserMessage(content=content, source=m["source"]))
    return context


def team_description(participant_names: List[str], participant_descriptions: List[str]) -> str:
    desc = ""
    for name, description in zip(participant_names, participant_descriptions, strict=True):
        desc += re.sub(r"\s+", " ", f"{name}: {description}").strip() + "\n"
    return desc.strip()


def make_autogen_agent_caller(agent: ChatAgent) -> AgentCaller:
    """Adapt an AutoGen ChatAgent into the plain async-function shape the
    graph's `call_agent` node dispatches through."""

    async def _call(instruction: str, cancellation_token: CancellationToken) -> str:
        message = TextMessage(content=instruction, source=ORCHESTRATOR_NAME)
        response = await agent.on_messages([message], cancellation_token)
        return response.chat_message.to_model_text()

    return _call