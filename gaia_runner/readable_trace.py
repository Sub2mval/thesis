"""Human-readable Markdown rendering for GAIA run traces.

The JSON trace remains authoritative. This module renders the same run as a
chronological audit log so a person can inspect the exact prompts and outputs,
including transient Trust Allocator notices/reflections where they were actually
sent, without having to reverse-engineer the machine log.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _call_messages(call: Dict[str, Any]) -> List[Dict[str, Any]]:
    request = call.get("request") or {}
    messages = request.get("messages") if isinstance(request, dict) else None
    return messages if isinstance(messages, list) else []


def _call_output(call: Dict[str, Any]) -> Any:
    response = call.get("response") or {}
    if not isinstance(response, dict):
        return None
    if response.get("generated_content") is not None:
        return response.get("generated_content")
    raw = response.get("raw")
    if isinstance(raw, dict):
        message = raw.get("message")
        if isinstance(message, dict):
            return message.get("content")
    return None


def _collect_web_sources(trace: Dict[str, Any]) -> Dict[str, str]:
    sources: Dict[str, str] = {}
    for call in ((trace.get("token_stats") or {}).get("calls") or []):
        for event in call.get("tool_events") or []:
            content = str(event.get("content", ""))
            for sid, url in re.findall(r"\[(W\d+)\].*?\bURL:\s*(https?://\S+)", content, flags=re.S):
                sources[sid] = url.rstrip(")>")
    return sources


def _linkify_citations(text: str, sources: Dict[str, str]) -> str:
    def repl(match):
        sid = match.group(1)
        url = sources.get(sid)
        return f"[{sid}]({url})" if url else match.group(0)
    return re.sub(r"\[(W\d+)\](?!\()", repl, text)


def _format_messages(messages: Iterable[Dict[str, Any]], sources: Optional[Dict[str, str]] = None) -> str:
    blocks: List[str] = []
    for i, msg in enumerate(messages, 1):
        role = msg.get("role", "message")
        content = _linkify_citations(str(msg.get("content", "")), sources or {})
        blocks.append(f"**Message {i}: {role}**\n\n{_md(content)}")
    return "\n\n".join(blocks)

def _title_for_call(call: Dict[str, Any], agent_index: int, agent_source: Optional[str] = None) -> str:
    call_type = call.get("call_type", "llm_call")
    if call_type == "trust_allocator":
        return "Trust Allocator"
    if call_type == "reflection":
        return "Reflection"
    if call_type == "corruption":
        return "Error Injector"
    if call_type == "aggregate":
        return "Final aggregation"
    if call_type == "agent_turn":
        return agent_source or f"Agent turn {agent_index}"
    if call_type.startswith("orchestrator_"):
        return f"Orchestrator — {call_type}"
    return f"LLM call — {call_type}"


def _render_call(call: Dict[str, Any], agent_index: int, agent_source: Optional[str] = None, sources: Optional[Dict[str, str]] = None) -> str:
    title = _title_for_call(call, agent_index, agent_source)
    call_index = call.get("call_index")
    call_type = call.get("call_type")
    lines = [f"## {title}", ""]
    if call_index is not None:
        lines.append(f"Call index: `{call_index}`")
    if call_type:
        lines.append(f"Call type: `{call_type}`")
    lines.append("")
    lines.append("### Input")
    lines.append("")
    lines.append(_format_messages(_call_messages(call), sources) or "(no recorded messages)")
    lines.append("")
    lines.append("### Output")
    lines.append("")
    output = _call_output(call)
    lines.append(_md(_linkify_citations(str(output or ""), sources)) or "(no generated text recorded)")
    tool_events = call.get("tool_events") or []
    if tool_events:
        tool_names = [str(event.get("tool") or event.get("tool_name") or event.get("type") or "tool") for event in tool_events]
        unique_tools = ", ".join(dict.fromkeys(tool_names))
        citations = [f"[{sid}]({url})" for sid, url in sorted((sources or {}).items())]
        line = f"*Tool use: {unique_tools}"
        if citations:
            line += " · Sources: " + ", ".join(citations)
        lines.append(line + "*")
    return "\n".join(lines).rstrip()


def _render_injection(trace: Dict[str, Any]) -> Optional[str]:
    injection = trace.get("injection")
    fork = trace.get("fork_metadata") or {}
    if not injection and not fork:
        return None

    lines = ["## Error Injection", ""]
    lines.append(f"Failure mode: `{trace.get('fm_id') or trace.get('error_type') or ''}`")
    if trace.get("fm_name"):
        lines.append(f"Failure name: {trace['fm_name']}")
    if trace.get("injected_at_message_index") is not None:
        lines.append(f"Target message index: `{trace['injected_at_message_index']}`")
    if trace.get("target_source"):
        lines.append(f"Target source: `{trace['target_source']}`")
    lines.append("")

    original = fork.get("original_message") or trace.get("injection_source_message")
    corrupted = fork.get("corrupted_message")
    if original:
        lines.extend(["### Original message", "", _md(original.get("content") if isinstance(original, dict) else original), ""])
    if corrupted:
        lines.extend(["### Corrupted message", "", _md(corrupted.get("content") if isinstance(corrupted, dict) else corrupted), ""])

    if injection:
        eligible = injection.get("eligible")
        if eligible is not None:
            lines.append(f"Eligibility: **{'ELIGIBLE' if eligible else 'INELIGIBLE'}**")
        if injection.get("eligibility_reason"):
            lines.append(f"Reason: {injection['eligibility_reason']}")
        if injection.get("prompt"):
            lines.extend(["", "### Injector input", "", injection["prompt"]])
        if injection.get("injector_response"):
            lines.extend(["", "### Injector output", "", injection["injector_response"]])
    return "\n".join(lines)


def build_readable_trace(trace: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the full trace available to callers; formatting is performed separately."""
    return trace


def format_readable(trace_or_readable: Dict[str, Any]) -> str:
    trace = trace_or_readable
    lines = [
        f"# {trace.get('system', 'run')}__{trace.get('condition', 'unknown')}",
        "",
        f"**Task ID:** `{trace.get('task_id', '')}`",
        f"**System:** {trace.get('system', '')}",
        f"**Graph:** {trace.get('graph', '')}",
        f"**Condition:** `{trace.get('condition', '')}`",
        f"**Experiment design:** `{trace.get('experiment_design', '')}`",
        f"**Ground truth:** {trace.get('ground_truth', '')}",
        f"**Final answer:** {trace.get('final_answer_extracted', '')}",
        f"**Correct:** {trace.get('correct', '')}",
        "",
        "---",
        "",
        "## Initial Question",
        "",
        _md(trace.get("question")),
        "",
    ]

    injection = _render_injection(trace)
    if injection:
        lines.extend([injection, "", "---", ""])

    calls = list((trace.get("token_stats") or {}).get("calls") or [])
    sources = _collect_web_sources(trace)
    agent_index = 0
    agent_sources = [m.get("source") for m in (trace.get("messages") or [])]
    for call in calls:
        agent_source = None
        if call.get("call_type") == "agent_turn":
            agent_source = agent_sources[agent_index] if agent_index < len(agent_sources) else None
            agent_index += 1
        lines.append(_render_call(call, agent_index, agent_source, sources))
        lines.extend(["", "---", ""])

    if not calls:
        lines.extend(["## Recorded messages", "", "No raw call records were stored for this trace.", ""])

    magnetic_events = trace.get("message_tool_events") or []
    if magnetic_events:
        compact = []
        for idx, events in enumerate(magnetic_events, 1):
            names = []
            for event in events:
                names.extend(str(r.get("name")) for r in event.get("results", []) if r.get("name"))
                if event.get("type") == "CodeExecutionEvent":
                    names.append("code_execution")
            if names:
                compact.append(f"message {idx}: {', '.join(dict.fromkeys(names))}")
        if compact:
            lines.extend(["## Tool use", "", "  ".join(compact), ""])

    lines.extend([
        "## Final Result",
        "",
        f"**Answer:** {trace.get('final_answer_extracted', '')}",
        f"**Correct:** {trace.get('correct', '')}",
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_readable_trace(path: str, trace: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(format_readable(trace))
