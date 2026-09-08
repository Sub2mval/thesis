"""
Graph topology for the LangGraph re-implementation of Magentic-One.

    Question
       |
       v
  create_task_ledger -> init_outer_loop -.
                                          v
                  .------------------ gricean_check <---------.
                  |                    (checks the LAST        |
                  v                     message only)          |
           progress_ledger                                 call_agent
             (the "Orchestrator                          (WebSurfer / Coder /
              Agent" hub)  ---> update_task_ledger            FileSurfer / terminal)
                  |             (replans, loops back      ^
                  |              to init_outer_loop)       |
                  '-----------------------------------------'
                  |
                  v
             final_answer -> Answer

One graph, always -- `enable_gricean_check` (see state.py) is a per-run
flag read *inside* gricean_check_node, not a build-time choice, so there
is no separate "baseline" topology to keep in sync with this one. Every
message hand-off (worker -> progress_ledger, and progress_ledger ->
call_agent) passes through the same gricean_check node; see
orchestrator_nodes.py for what it does with a flagged message.
"""

from __future__ import annotations

from typing import Dict, Optional

from autogen_core.models import ChatCompletionClient
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from context_utils import AgentCaller
from orchestrator_nodes import build_orchestrator_nodes
from prompts import ORCHESTRATOR_FINAL_ANSWER_PROMPT
from state import MagenticState


def build_magentic_one_graph(
    model_client: ChatCompletionClient,
    agent_callers: Dict[str, AgentCaller],
    participant_descriptions: Dict[str, str],
    max_rounds: int = 20,
    max_stalls: int = 3,
    final_answer_prompt: str = ORCHESTRATOR_FINAL_ANSWER_PROMPT,
    gricean_model_client: Optional[ChatCompletionClient] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
):
    nodes = build_orchestrator_nodes(
        model_client=model_client,
        gricean_client=gricean_model_client or model_client,
        agent_callers=agent_callers,
        participant_descriptions=participant_descriptions,
        max_rounds=max_rounds,
        max_stalls=max_stalls,
        final_answer_prompt=final_answer_prompt,
    )

    def route_after_progress_ledger(state: MagenticState) -> str:
        if state.get("n_rounds", 0) > state.get("max_rounds", max_rounds):
            return "final_answer"
        if state["is_satisfied"]:
            return "final_answer"
        if state["n_stalls"] >= state.get("max_stalls", max_stalls):
            return "update_task_ledger"
        return "gricean_check"

    def route_after_check(state: MagenticState) -> str:
        return state["next_after_check"]

    graph = StateGraph(MagenticState)
    for name, fn in nodes.items():
        graph.add_node(name, fn)

    graph.set_entry_point("create_task_ledger")
    graph.add_edge("create_task_ledger", "init_outer_loop")
    graph.add_edge("init_outer_loop", "gricean_check")
    graph.add_conditional_edges(
        "gricean_check", route_after_check, {"progress_ledger": "progress_ledger", "call_agent": "call_agent"}
    )
    graph.add_conditional_edges(
        "progress_ledger",
        route_after_progress_ledger,
        {"final_answer": "final_answer", "update_task_ledger": "update_task_ledger", "gricean_check": "gricean_check"},
    )
    graph.add_edge("call_agent", "gricean_check")
    graph.add_edge("update_task_ledger", "init_outer_loop")
    graph.add_edge("final_answer", END)

    return graph.compile(checkpointer=checkpointer)