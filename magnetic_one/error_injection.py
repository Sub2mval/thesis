"""
Checkpointer-based fork/error-injection harness.

Walks a completed run's checkpoint history, picks a message to corrupt,
rewrites that one message via `graph.aupdate_state`, and resumes the graph
from there with `graph.ainvoke(None, config=...)` -- LangGraph's normal
fork-from-checkpoint pattern. The specific failure mode used (see
failure_modes.py) is recorded in the fork's metadata (`fm_id`, `fm_name`)
so it flows through to the trace JSON, useful for analyzing results at the
sub-mode level, not just the 3 broad categories.

See paired_fork.py for the variant that forks a baseline and a
Gricean-checked run together with one shared, identical corruption.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from autogen_core.models import ChatCompletionClient, UserMessage

from magnetic_one.context_utils import ORCHESTRATOR_NAME
from magnetic_one.failure_modes import ERROR_TYPES, choose_failure_mode
from magnetic_one.state import ThreadMessage

_CORRUPTION_PROMPT = """RESPONSE CORRUPTION TASK

You are an error injector for a multi-agent system. First determine whether the
selected failure mode is meaningful for the TARGET AGENT and TARGET MESSAGE. Only
if it is meaningful should you produce a corrupted message.

FAILURE MODE:
{fm_instruction}

TASK:
{task}

TARGET AGENT / ROLE:
{target_agent}

CONVERSATION SO FAR (raw messages only, before the target message):
{conversation}

TARGET MESSAGE:
---
{original_content}
---

First line your response with exactly one of:
ELIGIBLE
INELIGIBLE

Use INELIGIBLE when this failure mode does not make semantic sense for this
target agent/message or cannot meaningfully affect the target's contribution to
the task. Do not force an incompatible failure into a tool result, status message,
or other output that cannot meaningfully exhibit the failure.

If ELIGIBLE, put exactly one plausible corrupted version of the target message
after the first line. The corruption must:
- instantiate the selected failure mode;
- remain plausible for the target agent's role;
- preserve the general format needed by downstream agents;
- not deliberately optimize for the final benchmark answer being wrong;
- not introduce a different failure mode.

Do not include analysis, labels, or explanations after the corrupted message.
"""


class MessageCheckpoint(TypedDict):
    snapshot: Any
    node: Optional[str]
    message_index: int


async def list_message_checkpoints(graph, config: Dict[str, Any]) -> List[MessageCheckpoint]:
    """Every checkpoint (oldest -> newest) where a message was appended.

    Note: the node that *wrote* checkpoint i is recorded as checkpoint
    (i-1)'s `.next` -- this LangGraph version does not populate
    metadata["writes"], so we can't read the writer off the checkpoint itself.
    """
    history = [snap async for snap in graph.aget_state_history(config)]
    history.reverse()
    results: List[MessageCheckpoint] = []
    prev_len = 0
    for i, snap in enumerate(history):
        msgs = snap.values.get("messages", []) or []
        if len(msgs) > prev_len:
            node = None
            if i > 0 and history[i - 1].next:
                node = history[i - 1].next[0]
            results.append({"snapshot": snap, "node": node, "message_index": len(msgs) - 1})
            prev_len = len(msgs)
    return results


def choose_target_message_index(checkpoints: List[MessageCheckpoint], strategy: str = "middle_agent_message") -> int:
    if not checkpoints:
        raise ValueError("No message-producing checkpoints found in this trace.")

    agent_cps = [c for c in checkpoints if c["snapshot"].values["messages"][c["message_index"]]["source"] != ORCHESTRATOR_NAME]
    pool = agent_cps or checkpoints

    if strategy == "first_agent_message":
        chosen = pool[0]
    elif strategy == "last_agent_message":
        chosen = pool[-1]
    else:
        chosen = pool[len(pool) // 2]
    return chosen["message_index"]


async def generate_corrupted_message(
    model_client: ChatCompletionClient,
    task: str,
    messages: List[ThreadMessage],
    target_index: int,
    error_type: str,
    fm_id: Optional[str] = None,
    target_role: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate one role-aware corruption or return an INELIGIBLE result."""
    mode = choose_failure_mode(error_type, fm_id)
    target = messages[target_index]
    prior = messages[:target_index]
    conversation = "\n".join(f"[{m['source']}]: {m['content']}" for m in prior) or "(no prior messages)"
    prompt = _CORRUPTION_PROMPT.format(
        fm_instruction=mode["instruction"],
        task=task,
        target_agent=target_role or target["source"],
        conversation=conversation,
        original_content=target["content"],
    )
    # call_type="corruption" (PART 8) -- instrumentation-only label; see
    # ollama_client.py's module docstring for how it's stripped before
    # the real API call.
    response = await model_client.create(
        [UserMessage(content=prompt, source="ErrorInjector")],
        extra_create_args={"call_type": "corruption"},
    )
    assert isinstance(response.content, str)
    raw = response.content.strip()
    lines = raw.splitlines()
    marker = lines[0].strip().upper() if lines else ""
    if marker == "INELIGIBLE":
        return {
            "eligible": False,
            "eligibility_reason": "Injector rejected this target as incompatible with the selected failure mode.",
            "content": None,
            "fm_id": mode["id"],
            "fm_name": mode["name"],
            "prompt": prompt,
            "injector_response": raw,
        }
    if marker == "ELIGIBLE":
        raw = "\n".join(lines[1:]).strip()
    return {
        "eligible": True,
        "eligibility_reason": None,
        "content": raw,
        "fm_id": mode["id"],
        "fm_name": mode["name"],
        "prompt": prompt,
        "injector_response": response.content.strip(),
    }


async def fork_trace_with_error(
    graph,
    model_client: ChatCompletionClient,
    checkpoints: List[MessageCheckpoint],
    task: str,
    target_message_index: int,
    error_type: str,
    fm_id: Optional[str] = None,
) -> Dict[str, Any]:
    matches = [c for c in checkpoints if c["message_index"] == target_message_index]
    if not matches:
        raise ValueError(f"No checkpoint found writing message index {target_message_index}.")
    chosen = matches[0]
    snapshot = chosen["snapshot"]

    messages = list(snapshot.values["messages"])
    original_content = messages[target_message_index]["content"]
    original_source = messages[target_message_index]["source"]

    corruption = await generate_corrupted_message(model_client, task, messages, target_message_index, error_type, fm_id)
    if not corruption.get("eligible", True):
        raise ValueError(
            f"Selected injection target {original_source} / message {target_message_index} is ineligible for "
            f"{corruption['fm_id']}: {corruption.get('eligibility_reason', '')}"
        )
    corrupted_messages = list(messages)
    corrupted_messages[target_message_index] = {"source": original_source, "content": corruption["content"]}

    new_config = await graph.aupdate_state(snapshot.config, {"messages": corrupted_messages}, as_node=chosen["node"])
    final_state = await graph.ainvoke(None, config=new_config)

    return {
        "error_type": error_type,
        "fm_id": corruption["fm_id"],
        "fm_name": corruption["fm_name"],
        "injected_at_step": (snapshot.metadata or {}).get("step"),
        "injected_at_message_index": target_message_index,
        "injected_at_node": chosen["node"],
        "original_message": {"source": original_source, "content": original_content},
        "corrupted_message": {"source": original_source, "content": corruption["content"]},
        "injection": {k: corruption.get(k) for k in ("eligible", "eligibility_reason", "prompt", "injector_response")},
        "final_state": final_state,
    }


async def run_all_forks(
    graph,
    model_client: ChatCompletionClient,
    config: Dict[str, Any],
    task: str,
    target_message_index: Optional[int] = None,
    strategy: str = "middle_agent_message",
    error_types: Optional[List[str]] = None,
    fm_ids: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """fm_ids: optional {error_type: fm_id} to pin specific failure modes
    instead of random selection, e.g. {"specification_issue": "FM-1.3"}."""
    checkpoints = await list_message_checkpoints(graph, config)
    idx = target_message_index if target_message_index is not None else choose_target_message_index(checkpoints, strategy)
    results = []
    for error_type in error_types or ERROR_TYPES:
        fm_id = (fm_ids or {}).get(error_type)
        result = await fork_trace_with_error(graph, model_client, checkpoints, task, idx, error_type, fm_id)
        results.append(result)
    return results
