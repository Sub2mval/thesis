"""
State schema for the LangGraph re-implementation of Magentic-One.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class ThreadMessage(TypedDict):
    source: str
    content: str


class TrustLogEntry(TypedDict, total=False):
    step: int
    message_index: int
    evaluated_source: str
    trust_level: str
    reason: str
    scores: Dict[str, Dict[str, Any]]  # {"quality": {"score": 1-5, "reason": ...}, "quantity": {...}, ...}




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

    # --- trust extension ---
    trust_level: str
    # The Trust_Allocator's own reasoning for the current trust_level, passed
    # to the receiving agent alongside the trust label itself.
    trust_reason: str
    # The four Gricean-maxim scores (1-5 each) that produced the current
    # trust_level, e.g. {"quality": {"score": 4, "reason": "..."}, ...}.
    trust_scores: Dict[str, Any]
    trust_history: List[TrustLogEntry]
    route_after_trust: str