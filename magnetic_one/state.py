"""
State schema for the LangGraph re-implementation of Magentic-One.

Every node in orchestrator_graph.py receives and returns a `MagenticState`
(a plain dict matching this TypedDict) -- nodes never hold state
themselves, so a run is fully inspectable/resumable via the checkpointer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class ThreadMessage(TypedDict):
    """One entry in the permanent, shared conversation -- visible to every
    node and every worker agent that reads `messages`."""

    source: str
    content: str


class AdherenceLogEntry(TypedDict, total=False):
    """One row of the permanent audit trail the Gricean adherence checker
    (gricean_check.py) leaves behind. Never read back into a prompt --
    it exists purely for post-hoc inspection/debugging."""

    step: int
    message_index: int
    evaluated_source: str
    adherence_level: str  # "high" | "not_high"
    reason: str
    scores: Dict[str, Dict[str, Any]]  # {"quality": {"score": 1-5, "reason": ...}, ...}


class ReflectionLogEntry(TypedDict, total=False):
    """One row of the reflection audit trail. A reflection is generated
    once, used once (folded into the single upcoming call for whichever
    node is about to consume the flagged message, via
    `state["pending_reflection"]`), and afterwards only kept here for
    audit purposes. No node ever reads an old entry from this list back
    into a prompt, so a reflection never resurfaces on a later turn, and
    one agent's reflection is never visible to another agent."""

    step: int
    message_index: int
    consuming_node: str  # which node's very next call this reflection was generated for
    reason: str  # the adherence checker's reasoning that triggered this reflection
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

    facts: str
    plan: str

    messages: List[ThreadMessage]

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
    # Whether the checker/reflection loop runs at all this run. One
    # compiled graph serves both the "baseline" and "checked" behaviour --
    # see gricean_check_node in orchestrator_nodes.py -- so this is a
    # per-run flag on the state, not a build-time graph choice.
    enable_gricean_check: bool
    # Always describes the *last* message in `messages` as of the most
    # recent gricean_check_node run.
    adherence_level: str  # "high" | "not_high"
    adherence_reason: str
    adherence_scores: Dict[str, Any]
    adherence_history: List[AdherenceLogEntry]
    # Set by gricean_check_node, consumed (and effectively cleared, since
    # the next gricean_check_node run always overwrites it) by whichever
    # node runs immediately after. None whenever the last message cleared
    # HIGH adherence, so that node behaves exactly as if no check existed.
    pending_reflection: Optional[str]
    reflection_history: List[ReflectionLogEntry]
    # gricean_check_node sits on two edges (worker -> progress_ledger, and
    # progress_ledger -> call_agent) and needs to know which one sent it
    # here so it can route back correctly once it's done.
    next_after_check: str