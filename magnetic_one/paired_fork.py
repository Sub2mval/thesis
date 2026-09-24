"""
Paired baseline/Gricean-checked error injection.

Rationale: `enable_gricean_check` is a per-run state flag on one compiled
graph (see magentic_one_langgraph.py), not a build-time choice, and the
Gricean checker never touches MessageHistory itself (see gricean_checker.
py) -- it only ever adds a `pending_reflection` that the *next* agent's
own prompt includes, and only does so when the flag is on. So with the
same model, seed, and task, a baseline run (flag off) and a
Gricean-checked run (flag on) are MECHANISTICALLY identical -- same
prompts fed to the same model -- right up to the first message that
would have been flagged not_high; only what's generated *after* that
point can differ in a way attributable to the checker.

NOTE this is a mechanistic guarantee, not a textual one: on Ollama Cloud,
RotatingKeyOllamaClient can rotate keys mid-run (rate-limit/auth
retries), and different backend replicas aren't guaranteed to reproduce
identical sampled tokens even at temperature=0/seed=42 for an identical
request -- so the two traces' message *content* can legitimately differ
before either has been flagged. `shared_prefix_length` below therefore
determines the safe-to-fork region from each trace's own Gricean
verdicts (first not_high message_index), not from literal content
equality -- see its docstring for the full reasoning.

The checker still SCORES every message in both runs either way (see
state.py), so `gricean_history` from the baseline run is directly
comparable to the checked run's, message for message, up to and
including the divergence point.

`run_paired_traces` runs both under one MagenticOneLangGraph instance
(same checkpointer, two thread_ids). `fork_paired_traces_with_error` then
finds a message index inside that shared prefix, generates ONE corrupted
message, and injects that exact same corruption into both forks -- so
any difference in what happens next is attributable to the Gricean
checker alone, not to incidental prompt drift or a second,
independently-sampled corruption.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from autogen_core.models import ChatCompletionClient

from magnetic_one.error_injection import generate_corrupted_message, list_message_checkpoints
from magnetic_one.failure_modes import choose_failure_mode
from magnetic_one.state import ThreadMessage
from magnetic_one.ollama_client import get_usage_tracking

try:  # only needed for the type hint; avoids a hard import cycle at runtime
    from magnetic_one.magnetic_one_langgraph import MagenticOneLangGraph
except ImportError:  # pragma: no cover
    MagenticOneLangGraph = Any  # type: ignore


def shared_prefix_length(
    baseline_messages: List[ThreadMessage],
    gricean_messages: List[ThreadMessage],
    baseline_gricean_history: Optional[List[Dict[str, Any]]] = None,
    gricean_gricean_history: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """How many leading messages are safe to fork from.

    Originally this required literal content equality (same source AND
    content) between the two traces -- the module docstring's claim that
    temperature=0/seed=42 makes the two runs byte-identical up to the
    first not_high verdict. In practice this proved too strict on Ollama
    Cloud: RotatingKeyOllamaClient (ollama_cloud_client.py) rotates to a
    different key on a rate-limit/auth error mid-run, and a different
    backend replica isn't guaranteed to reproduce the exact same sampled
    tokens for an identical request even at temperature 0 -- so content
    could differ from message 0, well before the checker had done
    anything, causing every source to [skip] with "no shared-prefix
    message" even on ordinary runs.

    The guarantee this experiment actually depends on is behavioral, not
    textual: the checker scores every message in both runs regardless of
    `enable_gricean_check`, and only ever *acts* on a not_high score
    (via `pending_reflection`) when the flag is on. So checker-on's
    trajectory is mechanistically identical to checker-off's -- same
    prompts, same model -- right up until checker-on's own first not_high
    verdict actually injects a reflection into what the next agent sees.
    Before that point, differing wording between the two traces is cloud
    sampling noise, not a real divergence caused by the checker.

    So: when gricean_history is supplied for both sides, the cutoff is
    the message_index of the first not-high verdict in EITHER trace's
    history (whichever comes first), not content comparison at all.
    Falls back to the original literal-equality check if history isn't
    passed in (e.g. an older/direct call site)."""
    if baseline_gricean_history is not None and gricean_gricean_history is not None:
        def _first_not_high(history: List[Dict[str, Any]]) -> Optional[int]:
            for entry in history:
                if entry.get("adherence_level") != "high":
                    return entry.get("message_index")
            return None

        cutoffs = [c for c in (_first_not_high(baseline_gricean_history), _first_not_high(gricean_gricean_history))
                   if c is not None]
        limit = min(cutoffs) if cutoffs else min(len(baseline_messages), len(gricean_messages))
        return max(0, min(limit, len(baseline_messages), len(gricean_messages)))

    n = 0
    for a, b in zip(baseline_messages, gricean_messages):
        if a["source"] != b["source"] or a["content"] != b["content"]:
            break
        n += 1
    return n


def _shared_target_candidates(
    baseline_messages: List[ThreadMessage],
    gricean_messages: List[ThreadMessage],
    orchestrator_name: str,
    strategy: str = "middle_agent_message",
    baseline_gricean_history: Optional[List[Dict[str, Any]]] = None,
    gricean_gricean_history: Optional[List[Dict[str, Any]]] = None,
) -> List[int]:
    shared_len = shared_prefix_length(
        baseline_messages, gricean_messages, baseline_gricean_history, gricean_gricean_history
    )
    if shared_len == 0:
        raise ValueError("Baseline and Gricean-checked traces share no common prefix -- nothing to fork from.")

    agent_candidates = [i for i in range(shared_len) if baseline_messages[i]["source"] != orchestrator_name]
    pool = agent_candidates or list(range(shared_len))
    if strategy == "first_agent_message":
        first = pool[0]
    elif strategy == "last_agent_message":
        first = pool[-1]
    else:
        first = pool[len(pool) // 2]

    # Preserve the historical target-selection behavior, then allow the
    # injector to move only one raw message index backward or forward if the
    # chosen message is not a safe target. Do not search arbitrarily far
    # through the execution because that would change the intervention point.
    ordered = [first]
    if first - 1 >= 0:
        ordered.append(first - 1)
    if first + 1 < shared_len:
        ordered.append(first + 1)
    return ordered


def choose_shared_target_index(
    baseline_messages: List[ThreadMessage],
    gricean_messages: List[ThreadMessage],
    orchestrator_name: str,
    strategy: str = "middle_agent_message",
    baseline_gricean_history: Optional[List[Dict[str, Any]]] = None,
    gricean_gricean_history: Optional[List[Dict[str, Any]]] = None,
) -> int:
    return _shared_target_candidates(
        baseline_messages, gricean_messages, orchestrator_name, strategy,
        baseline_gricean_history, gricean_gricean_history,
    )[0]


async def run_paired_traces(
    magentic: "MagenticOneLangGraph", task: str, thread_id_prefix: str = "run", experiment_design: str = "4",
) -> Dict[str, Any]:
    """Run `task` twice on one MagenticOneLangGraph instance's single
    compiled graph (same checkpointer) -- once with the Gricean checker
    off, once on -- under two thread_ids. Returns both configs (needed by
    `fork_paired_traces_with_error` below) alongside each run's result.

    experiment_design (see repo-root experiment_design.py) is passed
    straight through to both magentic.run() calls, so the same selected
    design drives both the baseline and checked side. It's also folded
    into both thread_ids so traces from different designs never collide
    under the same checkpointer."""
    baseline_config = {"configurable": {"thread_id": f"{thread_id_prefix}-baseline-design{experiment_design}"}}
    gricean_config = {"configurable": {"thread_id": f"{thread_id_prefix}-gricean-design{experiment_design}"}}
    baseline_result = await magentic.run(
        task, thread_id=baseline_config["configurable"]["thread_id"],
        enable_gricean_check=False, experiment_design=experiment_design,
    )
    gricean_result = await magentic.run(
        task, thread_id=gricean_config["configurable"]["thread_id"],
        enable_gricean_check=True, experiment_design=experiment_design,
    )
    return {
        "baseline_config": baseline_config,
        "gricean_config": gricean_config,
        "baseline_result": baseline_result,
        "gricean_result": gricean_result,
    }


def _usage_calls(clients: List[Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for client in clients:
        calls.extend(list(get_usage_tracking(client).get("calls", [])))
    calls.sort(key=lambda r: (r.get("started_at_unix", 0), r.get("call_index", 0)))
    return calls


async def _apply_fork(graph, checkpoints, target_index: int, corrupted_content: str,
                      usage_clients: Optional[List[Any]] = None) -> Dict[str, Any]:
    matches = [c for c in checkpoints if c["message_index"] == target_index]
    if not matches:
        raise ValueError(f"No checkpoint found writing message index {target_index}.")
    chosen = matches[0]
    snapshot = chosen["snapshot"]

    messages = list(snapshot.values["messages"])
    original = messages[target_index]
    messages[target_index] = {"source": original["source"], "content": corrupted_content}

    clients = usage_clients or []
    before_by_client = [list(get_usage_tracking(client).get("calls", [])) for client in clients]
    new_config = await graph.aupdate_state(snapshot.config, {"messages": messages}, as_node=chosen["node"])
    final_state = await graph.ainvoke(None, config=new_config)
    branch_calls: List[Dict[str, Any]] = []
    for client, before in zip(clients, before_by_client):
        after = list(get_usage_tracking(client).get("calls", []))
        branch_calls.extend(after[len(before):])
    branch_calls.sort(key=lambda r: (r.get("started_at_unix", 0), r.get("call_index", 0)))
    return {
        "injected_at_step": (snapshot.metadata or {}).get("step"),
        "injected_at_node": chosen["node"],
        "original_message": original,
        "final_state": final_state,
        "calls": branch_calls,
    }


async def fork_paired_traces_with_error(
    graph,
    model_client: ChatCompletionClient,
    baseline_config: Dict[str, Any],
    gricean_config: Dict[str, Any],
    task: str,
    error_type: str,
    orchestrator_name: str,
    fm_id: Optional[str] = None,
    strategy: str = "middle_agent_message",
    target_message_index: Optional[int] = None,
    usage_clients: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Fork the SAME already-completed baseline and Gricean-checked runs
    (same `graph` -- one compiled graph serves both, distinguished only by
    `thread_id` in each config) at a shared message index, injecting ONE
    identical corrupted message into both."""
    baseline_checkpoints = await list_message_checkpoints(graph, baseline_config)
    gricean_checkpoints = await list_message_checkpoints(graph, gricean_config)
    baseline_messages = baseline_checkpoints[-1]["snapshot"].values["messages"]
    gricean_messages = gricean_checkpoints[-1]["snapshot"].values["messages"]
    baseline_gricean_history = baseline_checkpoints[-1]["snapshot"].values.get("gricean_history", [])
    gricean_gricean_history = gricean_checkpoints[-1]["snapshot"].values.get("gricean_history", [])

    if target_message_index is None:
        candidate_indices = _shared_target_candidates(
            baseline_messages, gricean_messages, orchestrator_name, strategy,
            baseline_gricean_history, gricean_gricean_history,
        )
    else:
        idx = target_message_index
        shared_len = shared_prefix_length(baseline_messages, gricean_messages, baseline_gricean_history, gricean_gricean_history)
        if idx < 0 or idx >= shared_len:
            raise ValueError(f"message index {idx} is not in the shared prefix (only the first {shared_len} messages are guaranteed identical).")
        candidate_indices = [idx]
        if idx - 1 >= 0:
            candidate_indices.append(idx - 1)
        if idx + 1 < shared_len:
            candidate_indices.append(idx + 1)

    # Generate one usable corruption from the shared pre-error state. If a
    # candidate is semantically ineligible for either trace, try only the
    # immediate neighboring shared message. The failure mode is selected once
    # so every attempt refers to the same FM, and the first eligible corruption
    # is then applied identically to both branches.
    clients = usage_clients or [model_client]
    before_injection = [list(get_usage_tracking(client).get("calls", [])) for client in clients]
    corruption = None
    idx = None
    target_source = None
    selected_mode = choose_failure_mode(error_type, fm_id)
    selected_fm_id = selected_mode["id"]
    injection_attempts: List[Dict[str, Any]] = []
    for candidate_idx in candidate_indices:
        candidate_source = baseline_messages[candidate_idx]["source"]
        comparison_source = gricean_messages[candidate_idx]["source"]
        if candidate_source != comparison_source:
            injection_attempts.append({
                "message_index": candidate_idx,
                "target_source": candidate_source,
                "comparison_source": comparison_source,
                "fm_id": selected_fm_id,
                "fm_name": selected_mode["name"],
                "eligible": False,
                "eligibility_reason": "Paired candidate messages do not have the same source/role.",
            })
            continue
        candidate_corruption = await generate_corrupted_message(
            model_client, task, baseline_messages, candidate_idx, error_type, selected_fm_id,
            target_role=candidate_source, comparison_messages=gricean_messages,
        )
        if candidate_corruption.get("eligible", True) and isinstance(candidate_corruption.get("content"), str) and candidate_corruption["content"].strip():
            corruption = candidate_corruption
            idx = candidate_idx
            target_source = candidate_source
            break
        injection_attempts.append({
            "message_index": candidate_idx,
            "target_source": candidate_source,
            "comparison_source": comparison_source,
            "fm_id": candidate_corruption.get("fm_id") or selected_fm_id,
            "fm_name": candidate_corruption.get("fm_name") or selected_mode["name"],
            "eligible": candidate_corruption.get("eligible"),
            "eligibility_reason": candidate_corruption.get("eligibility_reason"),
            "injector_response": candidate_corruption.get("injector_response"),
        })

    injection_calls: List[Dict[str, Any]] = []
    for client, before in zip(clients, before_injection):
        after = list(get_usage_tracking(client).get("calls", []))
        injection_calls.extend(after[len(before):])
    injection_calls.sort(key=lambda r: (r.get("started_at_unix", 0), r.get("call_index", 0)))

    if corruption is None:
        mode_id = selected_fm_id
        mode_name = selected_mode["name"]
        reason = (
            injection_attempts[-1].get("eligibility_reason")
            if injection_attempts else "No eligible target was available for this failure mode."
        )
        return {
            "eligible": False,
            "skipped": True,
            "error_type": error_type,
            "fm_id": mode_id,
            "fm_name": mode_name,
            "injected_at_message_index": None,
            "corrupted_message": None,
            "injection": {
                "eligible": False,
                "eligibility_reason": reason,
                "attempts": injection_attempts,
            },
            "injection_attempts": injection_attempts,
            "injection_calls": injection_calls,
            "baseline": None,
            "gricean_checked": None,
        }

    baseline_fork = await _apply_fork(graph, baseline_checkpoints, idx, corruption["content"], clients)
    gricean_fork = await _apply_fork(graph, gricean_checkpoints, idx, corruption["content"], clients)

    return {
        "eligible": True,
        "skipped": False,
        "error_type": error_type,
        "fm_id": corruption["fm_id"],
        "fm_name": corruption["fm_name"],
        "injected_at_message_index": idx,
        "corrupted_message": {"source": target_source, "content": corruption["content"]},
        "injection": {k: corruption.get(k) for k in ("eligible", "eligibility_reason", "prompt", "injector_response")},
        "injection_attempts": injection_attempts,
        "injection_calls": injection_calls,
        "baseline": baseline_fork,
        "gricean_checked": gricean_fork,
    }
