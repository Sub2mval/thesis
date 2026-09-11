"""
Graph topology: one node per agent, matching the reference diagram.

                              Question
                                 |
                                 v
                   .---> MagenticOneOrchestrator
                   |     (owns Task Ledger & Progress
                   |      Ledger -- both live in
                   |      MagenticState, not as nodes)
                   |             |
                   |             v
                   |      Gricean_Checker  <-------------------.
                   |     (scores the LAST                      |
                   |      message only)                        |
                   |             |                              |
                   |             v                              |
                   |   FileSurfer / WebSurfer / Coder /          |
                   |   ComputerTerminal (whichever the            |
                   |   Orchestrator picked as next_speaker) ------'
                   |
                   '------------------------------------ (loops back)
                                 |
                                 v
                            Answer (END)

Every hand-off, in both directions, passes through Gricean_Checker -- see
gricean_checker.py for what happens to a flagged message. `enable_gricean_check`
(a per-run state flag, not a build-time choice -- see state.py) never
changes the graph's shape: Gricean_Checker always scores every message
either way, so there is only ever one compiled graph. The flag only gates
whether a low score ever turns into a reflection that reaches an agent.
"""

from __future__ import annotations

from typing import Dict, Optional

from autogen_core.models import ChatCompletionClient
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from magnetic_one.agent_nodes import build_agent_nodes
from magnetic_one.context_utils import AgentCaller, ORCHESTRATOR_NAME
from magnetic_one.gricean_check import GRICEAN_CHECKER_NAME
from magnetic_one.prompts import ORCHESTRATOR_FINAL_ANSWER_PROMPT
from magnetic_one.state import MagenticState


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
    nodes = build_agent_nodes(
        model_client=model_client,
        gricean_client=gricean_model_client or model_client,
        agent_callers=agent_callers,
        participant_descriptions=participant_descriptions,
        max_rounds=max_rounds,
        max_stalls=max_stalls,
        final_answer_prompt=final_answer_prompt,
    )
    worker_names = list(agent_callers.keys())

    graph = StateGraph(MagenticState)
    for name, fn in nodes.items():
        graph.add_node(name, fn)

    graph.set_entry_point(ORCHESTRATOR_NAME)

    # The Orchestrator either dispatches (-> Gricean_Checker, which then
    # routes on to whichever worker it picked) or has written a final
    # answer (-> END). Nothing else ever routes straight to END.
    graph.add_conditional_edges(
        ORCHESTRATOR_NAME,
        lambda s: END if s.get("final_answer") is not None else GRICEAN_CHECKER_NAME,
        {GRICEAN_CHECKER_NAME: GRICEAN_CHECKER_NAME, END: END},
    )

    # Every worker unconditionally hands its response back through the
    # checker -- who receives it next depends on `next_after_check`, set
    # by the worker node itself (always ORCHESTRATOR_NAME).
    for name in worker_names:
        graph.add_edge(name, GRICEAN_CHECKER_NAME)

    # Gricean_Checker routes to whoever `next_after_check` names: the
    # Orchestrator (after checking a worker's response) or the target
    # worker (after checking the Orchestrator's instruction to it).
    graph.add_conditional_edges(
        GRICEAN_CHECKER_NAME,
        lambda s: s["next_after_check"],
        {ORCHESTRATOR_NAME: ORCHESTRATOR_NAME, **{name: name for name in worker_names}},
    )

    return graph.compile(checkpointer=checkpointer)