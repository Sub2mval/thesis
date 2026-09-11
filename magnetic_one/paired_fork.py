"""
Paired baseline/Gricean-checked error injection.

Rationale: `enable_gricean_check` is a per-run state flag on one compiled
graph (see magentic_one_langgraph.py), not a build-time choice, and the
Gricean checker never touches MessageHistory itself (see gricean_checker.
py) -- it only ever adds a `pending_reflection` that the *next* agent's
own prompt includes, and only does so when the flag is on. So with the
same model, seed, and task, a baseline run (flag off) and a
Gricean-checked run (flag on) produce byte-identical MessageHistory right
up to the first message that would have been flagged not_high; only what's
generated *after* that point can differ. The checker still SCORES every
message in both runs either way (see state.py), so `gricean_history` from
the baseline run is directly comparable to the checked run's, message for
message, up to and including the divergence point.

`run_paired_traces` runs both under one MagenticOneLangGraph instance
(same checkpointer, two thread_ids). `fork_paired_traces_with_error` then
finds a message index inside that guaranteed-identical shared prefix,
generates ONE corrupted message, and injects that exact same corruption
into both forks -- so any difference in what happens next is attributable
to the Gricean checker alone, not to incidental prompt drift or a second,
independently-sampled corruption.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from autogen_core.models import ChatCompletionClient

from magnetic_one.error_injection import generate_corrupted_message, list_message_checkpoints
from magnetic_one.state import ThreadMessage

try:  # only needed for the type hint; avoids a hard import cycle at runtime
    from magnetic_one.magnetic_one_langgraph import MagenticOneLangGraph
except ImportError:  # pragma: no cover
    MagenticOneLangGraph = Any  # type: ignore


def shared_prefix_length(baseline_messages: List[ThreadMessage], gricean_messages: List[ThreadMessage]) -> int:
    """How many leading messages are identical (same source AND content)
    between the two traces -- the region it's safe to fork from."""
    n = 0
    for a, b in zip(baseline_messages, gricean_messages):
        if a["source"] != b["source"] or a["content"] != b["content"]:
            break
        n += 1
    return n


def choose_shared_target_index(
    baseline_messages: List[ThreadMessage],
    gricean_messages: List[ThreadMessage],
    orchestrator_name: str,
    strategy: str = "middle_agent_message",
) -> int:
    shared_len = shared_prefix_length(baseline_messages, gricean_messages)
    if shared_len == 0:
        raise ValueError("Baseline and Gricean-checked traces share no common prefix -- nothing to fork from.")

    candidates = [i for i in range(shared_len) if baseline_messages[i]["source"] != orchestrator_name]
    pool = candidates or list(range(shared_len))
    if strategy == "first_agent_message":
        return pool[0]
    if strategy == "last_agent_message":
        return pool[-1]
    return pool[len(pool) // 2]


async def run_paired_traces(magentic: "MagenticOneLangGraph", task: str, thread_id_prefix: str = "run") -> Dict[str, Any]:
    """Run `task` twice on one MagenticOneLangGraph instance's single
    compiled graph (same checkpointer) -- once with the Gricean checker
    off, once on -- under two thread_ids. Returns both configs (needed by
    `fork_paired_traces_with_error` below) alongside each run's result."""
    baseline_config = {"configurable": {"thread_id": f"{thread_id_prefix}-baseline"}}
    gricean_config = {"configurable": {"thread_id": f"{thread_id_prefix}-gricean"}}
    baseline_result = await magentic.run(task, thread_id=baseline_config["configurable"]["thread_id"], enable_gricean_check=False)
    gricean_result = await magentic.run(task, thread_id=gricean_config["configurable"]["thread_id"], enable_gricean_check=True)
    return {
        "baseline_config": baseline_config,
        "gricean_config": gricean_config,
        "baseline_result": baseline_result,
        "gricean_result": gricean_result,
    }


async def _apply_fork(graph, checkpoints, target_index: int, corrupted_content: str) -> Dict[str, Any]:
    matches = [c for c in checkpoints if c["message_index"] == target_index]
    if not matches:
        raise ValueError(f"No checkpoint found writing message index {target_index}.")
    chosen = matches[0]
    snapshot = chosen["snapshot"]

    messages = list(snapshot.values["messages"])
    original = messages[target_index]
    messages[target_index] = {"source": original["source"], "content": corrupted_content}

    new_config = await graph.aupdate_state(snapshot.config, {"messages": messages}, as_node=chosen["node"])
    final_state = await graph.ainvoke(None, config=new_config)
    return {
        "injected_at_step": (snapshot.metadata or {}).get("step"),
        "injected_at_node": chosen["node"],
        "original_message": original,
        "final_state": final_state,
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
) -> Dict[str, Any]:
    """Fork the SAME already-completed baseline and Gricean-checked runs
    (same `graph` -- one compiled graph serves both, distinguished only by
    `thread_id` in each config) at a shared message index, injecting ONE
    identical corrupted message into both."""
    baseline_checkpoints = await list_message_checkpoints(graph, baseline_config)
    gricean_checkpoints = await list_message_checkpoints(graph, gricean_config)
    baseline_messages = baseline_checkpoints[-1]["snapshot"].values["messages"]
    gricean_messages = gricean_checkpoints[-1]["snapshot"].values["messages"]

    if target_message_index is None:
        idx = choose_shared_target_index(baseline_messages, gricean_messages, orchestrator_name, strategy)
    else:
        idx = target_message_index
        shared_len = shared_prefix_length(baseline_messages, gricean_messages)
        if idx >= shared_len:
            raise ValueError(f"message index {idx} is not in the shared prefix (only the first {shared_len} messages are guaranteed identical).")

    # Generate the corruption ONCE, off the shared (hence identical-either-
    # way) prefix, so both forks get the exact same corrupted text rather
    # than two independently-sampled corruptions.
    corruption = await generate_corrupted_message(model_client, task, baseline_messages, idx, error_type, fm_id)

    baseline_fork = await _apply_fork(graph, baseline_checkpoints, idx, corruption["content"])
    gricean_fork = await _apply_fork(graph, gricean_checkpoints, idx, corruption["content"])

    return {
        "error_type": error_type,
        "fm_id": corruption["fm_id"],
        "fm_name": corruption["fm_name"],
        "injected_at_message_index": idx,
        "corrupted_message": {"source": baseline_messages[idx]["source"], "content": corruption["content"]},
        "baseline": baseline_fork,
        "gricean_checked": gricean_fork,
    }