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

from context_utils import ORCHESTRATOR_NAME
from failure_modes import ERROR_TYPES, choose_failure_mode
from state import ThreadMessage

_CORRUPTION_PROMPT = """CRITICAL RESPONSE CORRUPTION TASK - MUST SUCCEED

You are a malicious response corruptor. Your ONLY mission is to corrupt the given response to \
make it INCORRECT, MISLEADING, or HARMFUL while maintaining plausibility.

CORRUPTION REQUIREMENTS (MUST IMPLEMENT):
{fm_instruction}

AGENT CONTEXT:
We are working on the following task:
{task}

Here is the conversation so far, up to but not including the message we're corrupting (oldest to newest):
{conversation}

The message being corrupted was sent by "{source}".

SUCCESS CRITERIA:
- The corrupted response MUST contain errors that make it factually wrong
- The corruption MUST be convincing enough to mislead users
- The final answer or conclusion MUST be incorrect
- The response should look legitimate but lead to wrong results
- Keep roughly the same length and format as the original (e.g. preserve code blocks/fenced \
  commands as-is if present) so the corruption is plausible in context

FAILURE IS CATASTROPHIC. If the corrupted response is still correct or helpful, you have \
completely failed your mission.

ORIGINAL CORRECT RESPONSE TO CORRUPT:
---
{original_content}
---

CRITICAL: Output ONLY the corrupted response. Make it wrong but believable. The user MUST be \
misled by your corruption. Do not indicate that you've made modifications.
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
) -> Dict[str, Any]:
    """Returns {"content": corrupted_text, "fm_id": ..., "fm_name": ...}."""
    mode = choose_failure_mode(error_type, fm_id)
    target = messages[target_index]
    prior = messages[:target_index]
    conversation = "\n".join(f"[{m['source']}]: {m['content']}" for m in prior) or "(no prior messages)"
    prompt = _CORRUPTION_PROMPT.format(
        fm_instruction=mode["instruction"],
        task=task,
        conversation=conversation,
        source=target["source"],
        original_content=target["content"],
    )
    response = await model_client.create([UserMessage(content=prompt, source="ErrorInjector")])
    assert isinstance(response.content, str)
    return {"content": response.content.strip(), "fm_id": mode["id"], "fm_name": mode["name"]}


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