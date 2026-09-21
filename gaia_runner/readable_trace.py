"""
Reader-friendly companion file for a GAIA trace.

trace_io.save_baseline_trace / save_fork_trace each now write TWO files per
trace: the machine-shaped trace exactly as before, and a second
"*.readable.json" file built here -- the same run, laid out for a human to
scroll through instead of a schema to be parsed.

Shape of the readable file:
    {<every top-level field shown in the reference trace summary --
      task_id/system/graph/question/ground_truth/level/thread_id/condition/
      temperature/seed/use_gricean_check/fork_condition/error_type/fm_id/
      fm_name/experiment_design -- plus final_answer/correct>,
     "conversation": [ ...events, oldest to newest... ]}

Each event in "conversation" is one of:
  - the seed entry: {"agent_name": "Initial Question", "agent_input": "",
    "agent_output": <question text>}
  - a normal turn: {"agent_name": ..., "agent_input": ..., "agent_output": ...}
    -- one per message in trace["messages"], AND one immediately after it
    for that message's Trust_Allocator verdict, if trace["trust_history"]
    has an entry for that message_index. This is exactly the "question,
    agent1, trust allocator, agent2, trust allocator, ..." order, since
    trace["messages"] and trace["trust_history"] are both already stored
    in generation order (see debate_system.py / magnetic_system.py).
  - a corrupted-message turn, wherever trace["fork_metadata"] says a
    message was injected: {"old_message": ..., "agent": ...,
    "corrupters_input": ..., "error_type": ..., "corrupted_message": ...}
    -- 5 fields, replacing the normal 3-field turn at that position, per
    spec. "corrupters_input" is the fm_instruction text that was actually
    fed to the corrupting model for this fm_id (looked up from
    llm_debate.error_injection.FAILURE_MODES / magnetic_one.failure_modes.
    FAILURE_MODES) -- the exact corruption prompt itself isn't kept in the
    trace, but the instruction is what it was built from and is
    deterministic per fm_id, so this is not a guess.

"No \\ns": this file is for people, not parsers. format_readable() below
deliberately turns escaped newlines back into real line breaks in the
serialized text, which makes multi-paragraph agent output read like actual
paragraphs instead of literal backslash-n. That makes the file technically
invalid as strict JSON (a raw newline inside a quoted string) -- accepted
on purpose, since the machine-readable trace already exists as the sibling
plain .json file this one is paired with.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

# Every field shown in the reference trace summary, in that same order.
_TOP_FIELDS: List[str] = [
    "task_id", "system", "graph", "question", "ground_truth", "level",
    "thread_id", "condition", "temperature", "seed", "use_gricean_check",
    "fork_condition", "error_type", "fm_id", "fm_name", "experiment_design",
]


def _fm_instruction(system: str, error_type: Optional[str], fm_id: Optional[str]) -> Optional[str]:
    """The corruption-requirement text actually handed to the corrupting
    model for this fm_id (see FAILURE_MODES in llm_debate/error_injection.py
    or magnetic_one/failure_modes.py) -- i.e. "corrupter's input". Looked
    up rather than stored on the trace, since it's a deterministic function
    of (system, error_type, fm_id) and both modules already expose it via
    choose_failure_mode(). Deferred imports, same reasoning as elsewhere in
    gaia_runner: don't force both ollama/tenacity AND autogen to be
    installed just to write a trace for one system."""
    if not error_type or not fm_id:
        return None
    try:
        if system == "llm_debate":
            from llm_debate.error_injection import choose_failure_mode
        else:
            from magnetic_one.failure_modes import choose_failure_mode
        return choose_failure_mode(error_type, fm_id)["instruction"]
    except Exception:
        return None


def _debate_input_lookup(trace: Dict[str, Any]):
    """llm_debate's flat trace["messages"] holds only each agent's
    assistant replies (see debate_system._flatten_messages) -- the
    broadcast/instruction actually fed to the agent for that turn isn't in
    that list, but IS in trace["contexts"][agent_id] (each agent's own
    private context: seed message, then alternating user broadcast /
    assistant reply). This builds, per agent, the ordered list of "user
    message immediately before this reply" pairings, then returns a
    lookup(source) closure that walks that per-agent list in step with
    however many times that agent's name has already been passed in --
    which lines up exactly with encounter order in trace["messages"],
    since each agent's own replies appear there in chronological order
    even though interleaved with other agents' replies."""
    contexts = trace.get("contexts") or []
    per_agent_inputs: List[List[Optional[str]]] = []
    for ctx in contexts:
        inputs: List[Optional[str]] = []
        last_user: Optional[str] = None
        for m in ctx:
            if m.get("role") == "user":
                last_user = m.get("content")
            elif m.get("role") == "assistant":
                inputs.append(last_user)
        per_agent_inputs.append(inputs)

    counters = [0] * len(contexts)

    def lookup(source: str) -> Optional[str]:
        try:
            agent_id = int(str(source).replace("Agent", "")) - 1
        except ValueError:
            return None
        if not (0 <= agent_id < len(per_agent_inputs)):
            return None
        i = counters[agent_id]
        counters[agent_id] += 1
        inputs = per_agent_inputs[agent_id]
        return inputs[i] if i < len(inputs) else None

    return lookup


def _trust_event(t: Dict[str, Any], evaluated_content: str) -> Dict[str, Any]:
    level = str(t.get("trust_level") or "").upper()
    reason = t.get("reason") or ""
    extras = []
    if t.get("notice_applied"):
        extras.append(f"notice applied: {t['notice_applied']}")
    if t.get("reflect"):
        extras.append("reflection triggered")
    suffix = f" ({'; '.join(extras)})" if extras else ""
    return {
        "agent_name": "Trust_Allocator",
        "agent_input": evaluated_content,
        "agent_output": f"{level} trust{suffix} -- {reason}".strip(),
    }


def _build_events(trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    system = trace.get("system")
    messages = trace.get("messages") or []
    trust_by_index: Dict[int, Dict[str, Any]] = {}
    for t in trace.get("trust_history") or []:
        idx = t.get("message_index")
        if idx is not None:
            trust_by_index[idx] = t

    fork_meta = trace.get("fork_metadata")
    injected_idx = fork_meta.get("injected_at_message_index") if fork_meta else None
    corrupters_input = (
        _fm_instruction(system, trace.get("error_type"), (fork_meta or {}).get("fm_id") or trace.get("fm_id"))
        if fork_meta else None
    )

    debate_input_lookup = _debate_input_lookup(trace) if system == "llm_debate" else None

    events: List[Dict[str, Any]] = [
        {"agent_name": "Initial Question", "agent_input": "", "agent_output": trace.get("question")}
    ]

    prev_output = trace.get("question")
    for idx, msg in enumerate(messages):
        source = msg.get("source")
        content = msg.get("content")
        agent_input = debate_input_lookup(source) if debate_input_lookup is not None else prev_output

        if fork_meta is not None and idx == injected_idx:
            original = fork_meta.get("original_message") or {}
            corrupted = fork_meta.get("corrupted_message") or {}
            events.append({
                "old_message": original.get("content"),
                "agent": original.get("source") or source,
                "corrupters_input": corrupters_input,
                "error_type": trace.get("error_type"),
                "corrupted_message": corrupted.get("content") or content,
            })
        else:
            events.append({"agent_name": source, "agent_input": agent_input, "agent_output": content})

        prev_output = content

        t = trust_by_index.get(idx)
        if t is not None:
            events.append(_trust_event(t, content))

    return events


def build_readable_trace(trace: Dict[str, Any]) -> Dict[str, Any]:
    readable: Dict[str, Any] = {field: trace.get(field) for field in _TOP_FIELDS}
    readable["final_answer"] = trace.get("final_answer_extracted")
    readable["correct"] = trace.get("correct")
    readable["conversation"] = _build_events(trace)
    return readable


def format_readable(readable: Dict[str, Any]) -> str:
    text = json.dumps(readable, indent=2, ensure_ascii=False, default=str)
    # See module docstring's "No \ns" note -- deliberate, for humans.
    return text.replace("\\n", "\n")


def write_readable_trace(path: str, trace: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(format_readable(build_readable_trace(trace)))