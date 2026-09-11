"""
Worker agent nodes -- FileSurfer / WebSurfer / Coder / ComputerTerminal
(and, in hil_mode, User) each get their own graph node, one per entry in
`agent_callers` (the dict AutoGen ChatAgents are adapted into by
context_utils.make_autogen_agent_caller). Every worker node does the same
three things: read the instruction just handed to it (with any pending
Gricean reflection folded in), call the agent, and append its response to
MessageHistory.
"""

from __future__ import annotations

from typing import Any, Dict

from autogen_core import CancellationToken

from magnetic_one.context_utils import ORCHESTRATOR_NAME, AgentCaller
from magnetic_one.json_llm import truncate_message_content
from magnetic_one.state import MagenticState


def build_worker_nodes(agent_callers: Dict[str, AgentCaller]) -> Dict[str, Any]:
    def _make_node(name: str, caller: AgentCaller):
        async def worker_node(state: MagenticState) -> MagenticState:
            instruction = state["instruction"]
            if state.get("pending_reflection"):
                instruction = f"{state['pending_reflection']}\n\n{instruction}"
            content = truncate_message_content(await caller(instruction, CancellationToken()))
            new_messages = list(state["messages"]) + [{"source": name, "content": content}]
            return {**state, "messages": new_messages, "next_after_check": ORCHESTRATOR_NAME}

        return worker_node

    return {name: _make_node(name, caller) for name, caller in agent_callers.items()}