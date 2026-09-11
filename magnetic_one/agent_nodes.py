"""
Assembles the graph's node registry: one entry per agent --
MagenticOneOrchestrator, Gricean_Checker, and every worker agent. See
orchestrator_graph.py for how these are wired into edges, and
orchestrator_agent.py / worker_agent.py / gricean_checker.py for what each
one actually does.
"""

from __future__ import annotations

from typing import Any, Dict

from autogen_core.models import ChatCompletionClient

from magnetic_one.context_utils import AgentCaller, ORCHESTRATOR_NAME
from magnetic_one.gricean_check import GRICEAN_CHECKER_NAME
from magnetic_one.gricean_checker import build_gricean_checker_node
from magnetic_one.orchestrator_agent import build_orchestrator_node
from magnetic_one.worker_agent import build_worker_nodes


def build_agent_nodes(
    model_client: ChatCompletionClient,
    gricean_client: ChatCompletionClient,
    agent_callers: Dict[str, AgentCaller],
    participant_descriptions: Dict[str, str],
    max_rounds: int,
    max_stalls: int,
    final_answer_prompt: str,
) -> Dict[str, Any]:
    orchestrator = build_orchestrator_node(
        model_client=model_client,
        participant_names=list(agent_callers.keys()),
        participant_descriptions=participant_descriptions,
        max_rounds=max_rounds,
        max_stalls=max_stalls,
        final_answer_prompt=final_answer_prompt,
    )
    workers = build_worker_nodes(agent_callers)
    checker = build_gricean_checker_node(gricean_client)
    return {ORCHESTRATOR_NAME: orchestrator, GRICEAN_CHECKER_NAME: checker, **workers}