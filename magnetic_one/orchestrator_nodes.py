"""
Node registry for the Magentic-One LangGraph -- see orchestrator_graph.py
for how these are wired into edges.

Pipeline stages, matching the "b) Magentic-One" reference diagram:
  create_task_ledger / update_task_ledger  -- the "Task Ledger" box: facts + plan
  progress_ledger                          -- the "Orchestrator Agent" box's own reasoning
  call_agent                               -- dispatch to WebSurfer / Coder / FileSurfer / terminal
  gricean_check                            -- the gate every hand-off passes through (the
                                               diagram's "D" arrows into the Orchestrator)
  final_answer                             -- the "Answer" box

The actual node logic lives in two files, split by concern:
  ledger_nodes.py    -- the orchestrator's own reasoning (task ledger, progress ledger, final answer)
  dispatch_nodes.py  -- worker dispatch + the Gricean adherence gate + reflection loop
"""

from __future__ import annotations

from typing import Any, Dict

from autogen_core.models import ChatCompletionClient

from context_utils import AgentCaller
from dispatch_nodes import build_dispatch_nodes
from ledger_nodes import build_ledger_nodes


def build_orchestrator_nodes(
    model_client: ChatCompletionClient,
    gricean_client: ChatCompletionClient,
    agent_callers: Dict[str, AgentCaller],
    participant_descriptions: Dict[str, str],
    max_rounds: int,
    max_stalls: int,
    final_answer_prompt: str,
) -> Dict[str, Any]:
    ledger_nodes = build_ledger_nodes(
        model_client=model_client,
        participant_names=list(agent_callers.keys()),
        participant_descriptions=participant_descriptions,
        max_rounds=max_rounds,
        max_stalls=max_stalls,
        final_answer_prompt=final_answer_prompt,
    )
    dispatch_nodes = build_dispatch_nodes(gricean_client=gricean_client, agent_callers=agent_callers)
    return {**ledger_nodes, **dispatch_nodes}