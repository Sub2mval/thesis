"""
State schema for the LangGraph re-implementation of Magentic-One.

Every node in this graph IS an agent (MagenticOneOrchestrator, each worker,
Gricean_Checker -- see orchestrator_graph.py). Everything that isn't an
agent -- the Task Ledger, the Progress Ledger, the message history, and
the Gricean adherence log -- lives here instead, as plain data every node
reads and returns. Nodes never hold state themselves, so a run is fully
inspectable/resumable via the checkpointer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class ThreadMessage(TypedDict):
    """One entry in MessageHistory -- permanent, shared, visible to every
    agent that reads `messages`."""

    source: str
    content: str


class TaskLedger(TypedDict, total=False):
    """The Orchestrator's own facts + plan for the task. Rebuilt from
    scratch whenever the Orchestrator replans after a stall."""

    facts: str
    plan: str


class GriceanLogEntry(TypedDict, total=False):
    """One row of Gricean_history. Written once, by Gricean_Checker, for
    every message that ever enters MessageHistory. Never read back into a
    prompt -- it exists purely for post-hoc inspection/debugging."""

    step: int
    message_index: int
    evaluated_source: str
    adherence_level: str  # "high" | "not_high"
    reason: str
    scores: Dict[str, Dict[str, Any]]  # {"quality": {"score": 1-5, "reason": ...}, ...}


class ReflectionLogEntry(TypedDict, total=False):
    """One row of the reflection audit trail. A reflection is generated
    once by Gricean_Checker, used once (folded into the single upcoming
    call of whichever agent is about to receive the flagged message, via
    `state["pending_reflection"]`), and afterwards only kept here for
    audit. No agent ever reads an old entry back into a prompt, so a
    reflection never resurfaces on a later turn, and one agent's
    reflection is never visible to another agent."""

    step: int
    message_index: int
    receiving_agent: str  # which agent's very next call this reflection was generated for
    reason: str  # Gricean_Checker's reasoning that triggered this reflection
    reflection: str


class LLMCallUsage(TypedDict, total=False):
    call_index: int
    context: str
    started_at_unix: float
    elapsed_seconds: float
    input_message_count: int
    input_sources: List[Optional[str]]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: int
    token_source: str
    status: str
    error: str
    client_key_index: int


class TokenUsageSummary(TypedDict, total=False):
    n_llm_calls: int
    n_successful_calls: int
    n_failed_attempts: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    calls: List[LLMCallUsage]


class MagenticState(TypedDict, total=False):
    task: str
    team_description: str
    participant_names: List[str]

    task_ledger: TaskLedger
    messages: List[ThreadMessage]  # MessageHistory

    n_rounds: int
    n_stalls: int
    max_rounds: int
    max_stalls: int

    next_speaker: str
    instruction: str
    progress_ledger: Dict[str, Any]

    is_satisfied: bool
    final_answer: Optional[str]
    termination_reason: Optional[str]

    # --- Gricean adherence + reflection extension ---
    # Gricean_Checker scores every message regardless of this flag, so
    # `gricean_history` is populated the same way whether it's on or off
    # -- directly comparable across a baseline and a checked run. This
    # flag gates ONLY whether a not_high score ever turns into a
    # reflection that reaches an agent (`pending_reflection` stays None
    # whenever it's False, no matter the score). See gricean_checker.py.
    enable_gricean_check: bool
    # Always describes the *last* message in `messages`, as of the most
    # recent Gricean_Checker run.
    adherence_level: str  # "high" | "not_high"
    adherence_reason: str
    adherence_scores: Dict[str, Any]
    gricean_history: List[GriceanLogEntry]
    # Set by Gricean_Checker, consumed by whichever agent runs immediately
    # after it. None whenever the last message cleared HIGH adherence, so
    # that agent behaves exactly as if no checker existed.
    pending_reflection: Optional[str]
    reflection_history: List[ReflectionLogEntry]
    # Gricean_Checker sits on every edge (worker -> Orchestrator, and
    # Orchestrator -> worker) and needs to know who should receive the
    # message it just checked, so it can route there once it's done.
    next_after_check: str