# error_injection.py
#
# Checkpointer-based fork/error-injection harness for the LangGraph debate
# graph in langgraph_debate_minimal.py.
#
# Adapted from an AutoGen-orchestrator sibling implementation (which used a
# single flat state["messages"] list, an ORCHESTRATOR_NAME role to filter
# out, and an autogen_core ChatCompletionClient). None of that exists here:
# our state is state["contexts"]: List[List[Message]] -- one message list
# per agent -- and generation goes through this module's plain synchronous
# call_llm(). FAILURE_MODES and the corruption prompt are preserved
# verbatim; only the state-shape-specific plumbing is rewritten.
#
# Headline feature: run_paired_fork_experiment() runs the baseline
# (checker off) and Trust_Allocator-on debates side by side, each under its
# own checkpointer, picks ONE fork point using the selected experiment
# design's own intervention semantics (see run_paired_fork_experiment's
# docstring -- Designs 1/2 intervene on every message and always fork at
# the second checkpoint; Designs 3/4 only intervene from the first
# non-HIGH Trust_Allocator verdict onward, so any checkpoint before that
# is fair game), generates exactly ONE corruption there, and forks BOTH
# runs from that point with the identical corrupted content, so both
# continuations start from the same message history shape and any
# difference afterward is attributable to the checker, not to the two runs
# having already drifted apart before the injection.

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langgraph.checkpoint.memory import MemorySaver

from .langgraph_debate import Message, build_graph, call_llm


class FailureMode(TypedDict):
    id: str
    name: str
    instruction: str


# Source: MAST-style multi-agent failure taxonomy. FM-1.x = specification
# issues, FM-2.x = inter-agent misalignment, FM-3.x = task verification.
# Preserved verbatim from the source harness.
FAILURE_MODES: Dict[str, List[FailureMode]] = {
    "specification_issue": [
        {
            "id": "FM-1.1",
            "name": "Task specification deviation",
            "instruction": (
                "COMPLETELY rewrite this prompt to ENSURE total task failure: DESTROY the "
                "original task specification, CORRUPT input/output formats, IGNORE all key "
                "constraints, and REDIRECT the task goal entirely. The agent MUST NOT complete "
                "the original task successfully."
            ),
        },
        {
            "id": "FM-1.2",
            "name": "Role specification deviation",
            "instruction": (
                "FORCE the agent to MASSIVELY overstep its defined role: COMPEL it to make "
                "unauthorized decisions, DEMAND actions beyond its responsibilities, PUSH it to "
                "assume authority it doesn't have. The agent MUST violate its role boundaries "
                "and act inappropriately."
            ),
        },
        {
            "id": "FM-1.3",
            "name": "Add redundant steps",
            "instruction": (
                "TRAP the agent in repetitive task loops: FORCE endless repetition of identical "
                "steps, DEMAND circular discussions without progress, COMPEL infinite "
                "verification cycles. The agent MUST be stuck in loops and NEVER complete the "
                "task."
            ),
        },
        {
            "id": "FM-1.4",
            "name": "Remove conversation history",
            "instruction": (
                "WIPE the agent's memory completely: FORCE it to IGNORE all recent conversation "
                "history, ELIMINATE previous decisions and information, DESTROY contextual "
                "awareness. The agent MUST act like it has amnesia and CANNOT build on prior "
                "work."
            ),
        },
        {
            "id": "FM-1.5",
            "name": "Remove termination conditions",
            "instruction": (
                "DISABLE the agent's termination awareness: REMOVE all termination conditions, "
                "ELIMINATE stopping criteria, FORCE endless continuation without purpose. The "
                "agent MUST run forever without knowing when to stop and create infinite "
                "meaningless output."
            ),
        },
    ],
    "inter_agent_misalignment": [
        {
            "id": "FM-2.1",
            "name": "Repeat handled tasks",
            "instruction": (
                "FORCE the agent to completely restart the conversation: ELIMINATE all previous "
                "context and progress, DEMAND starting over without justification, DESTROY "
                "accumulated knowledge and decisions. The agent MUST lose all its previous work "
                "and fail to build on prior successes."
            ),
        },
        {
            "id": "FM-2.2",
            "name": "Make request ambiguous",
            "instruction": (
                "FORCE the agent to make DANGEROUS assumptions: NEVER ask for clarification "
                "when information is vague, PROCEED on incomplete data without verification, "
                "ASSUME details not provided. The agent MUST act on wrong assumptions and fail "
                "the task."
            ),
        },
        {
            "id": "FM-2.3",
            "name": "Deviate from main goal (drift)",
            "instruction": (
                "HIJACK the agent's task focus: FORCE gradual drift away from the original goal, "
                "REDIRECT attention to irrelevant details, INJECT inefficient tangents. The "
                "agent MUST completely miss the target and focus on the wrong things."
            ),
        },
        {
            "id": "FM-2.4",
            "name": "Deviate from main goal (withholding)",
            "instruction": (
                "FORCE the agent to SECRETLY withhold critical information: HIDE essential "
                "details from other agents, CREATE information asymmetry deliberately, CONCEAL "
                "important findings to sabotage team success. The agent MUST be deceptive and "
                "secretive, causing team failure."
            ),
        },
        {
            "id": "FM-2.5",
            "name": "Ignore other agents",
            "instruction": (
                "FORCE the agent to be COMPLETELY stubborn and uncooperative: IGNORE all other "
                "agents' suggestions, REJECT feedback and corrections aggressively, INSIST on "
                "its own approach regardless of quality. The agent MUST be arrogant and refuse "
                "all collaboration."
            ),
        },
        {
            "id": "FM-2.6",
            "name": "Inconsistent reasoning",
            "instruction": (
                "DELIBERATELY create reasoning-action mismatches to ENSURE task failure: FORCE "
                "the agent's reasoning to contradict its final actions, CREATE inconsistencies "
                "between explanation and execution, ENSURE logical processes lead to illogical "
                "conclusions. The agent MUST be internally contradictory and fail through "
                "self-inconsistency."
            ),
        },
    ],
    "task_verification": [
        {
            "id": "FM-3.1",
            "name": "Premature termination",
            "instruction": (
                "FORCE the agent to QUIT prematurely: TERMINATE before all necessary steps are "
                "completed, STOP just before success, FINISH the task incompletely. The agent "
                "MUST abandon work right before finishing successfully."
            ),
        },
        {
            "id": "FM-3.2",
            "name": "Remove verification steps",
            "instruction": (
                "COMPLETELY eliminate verification steps to ENSURE hidden errors: REMOVE all "
                "checking mechanisms, SKIP validation processes, ELIMINATE quality control "
                "steps. The agent MUST produce unverified, error-prone results that will fail "
                "when used."
            ),
        },
        {
            "id": "FM-3.3",
            "name": "Incorrect verification",
            "instruction": (
                "SABOTAGE all verification processes: MAKE the verification process completely "
                "flawed, FORCE failure to detect real problems, ENSURE incorrect confirmation of "
                "wrong results. The agent MUST verify everything incorrectly and approve "
                "failures."
            ),
        },
    ],
}

ERROR_TYPES: List[str] = list(FAILURE_MODES.keys())

_CORRUPTION_PROMPT = """RESPONSE CORRUPTION TASK

You are an error injector for a multi-agent system. Your job is to determine whether
the selected failure mode is meaningful for the TARGET AGENT and, only if it is, produce
one plausible corrupted version of the TARGET MESSAGE.

FAILURE MODE:
{fm_instruction}

TASK:
{task}

TARGET AGENT:
{target_agent}

CONVERSATION SO FAR (raw agent outputs only, Trace A):
{conversation}

TARGET MESSAGE (Trace A):
---
{original_content}
---

{paired_section}

First line your response with exactly one of:
ELIGIBLE
INELIGIBLE

Use INELIGIBLE when the selected failure mode does not make semantic sense for this
target agent/message.
When a comparison trace is provided, say INELIGIBLE unless the SAME corrupted message can
meaningfully instantiate the selected failure mode in BOTH traces at this exact position.
Do not force an incompatible failure into a message.

If ELIGIBLE, put the corrupted message after the first line. The corruption must:
- instantiate the selected failure mode;
- remain plausible for the target agent's role;
- preserve the general format needed by downstream agents;
- not deliberately optimize for the final benchmark answer being wrong;
- not claim a different failure mode.

Do not include analysis, labels, or explanations after the corrupted message.
"""


class MessageCheckpoint(TypedDict):
    snapshot: Any
    node: Optional[str]
    agent_id: int
    position: int  # index into contexts[agent_id]


async def list_message_checkpoints(graph, config: Dict[str, Any]) -> List[MessageCheckpoint]:
    """Every checkpoint (oldest -> newest) where some agent's ASSISTANT
    turn was appended -- i.e. an agent produced new original content.
    Broadcast (user) turns are just re-statements of others' answers and
    aren't offered as injection targets.

    Note: as in the source harness, the node that *wrote* checkpoint i is
    recorded as checkpoint (i-1)'s `.next` -- this LangGraph version does
    not populate metadata["writes"], so we can't read the writer off the
    checkpoint itself.
    """
    history = [snap async for snap in graph.aget_state_history(config)]
    history.reverse()
    results: List[MessageCheckpoint] = []
    prev_lens: List[int] = []
    for i, snap in enumerate(history):
        contexts = snap.values.get("contexts", [])
        if not prev_lens:
            prev_lens = [0] * len(contexts)
        for agent_id, ctx in enumerate(contexts):
            if len(ctx) > prev_lens[agent_id]:
                if ctx[-1]["role"] == "assistant":
                    node = history[i - 1].next[0] if i > 0 and history[i - 1].next else None
                    results.append({"snapshot": snap, "node": node, "agent_id": agent_id, "position": len(ctx) - 1})
                prev_lens[agent_id] = len(ctx)
    return results


def choose_target_checkpoint(checkpoints: List[MessageCheckpoint],
                              strategy: str = "middle_agent_message") -> MessageCheckpoint:
    if not checkpoints:
        raise ValueError("No assistant-turn checkpoints found in this trace.")
    if strategy == "first_agent_message":
        return checkpoints[0]
    if strategy == "last_agent_message":
        return checkpoints[-1]
    return checkpoints[len(checkpoints) // 2]


def choose_failure_mode(error_type: str, fm_id: Optional[str] = None) -> FailureMode:
    """Pick a specific failure mode within a category. Random by default;
    pass fm_id (e.g. "FM-2.3") to pin a specific one for reproducibility."""
    if error_type not in FAILURE_MODES:
        raise ValueError(f"Unknown error_type '{error_type}'. Must be one of {ERROR_TYPES}.")
    modes = FAILURE_MODES[error_type]
    if fm_id is not None:
        for m in modes:
            if m["id"] == fm_id:
                return m
        raise ValueError(f"fm_id '{fm_id}' is not one of {[m['id'] for m in modes]} for error_type '{error_type}'.")
    return random.choice(modes)


def _all_agent_outputs(contexts: List[List[Message]]) -> List[Dict[str, str]]:
    replies = [[m.get("content") for m in ctx if m.get("role") == "assistant" and isinstance(m.get("content"), str)] for ctx in contexts]
    out: List[Dict[str, str]] = []
    for round_idx in range(max((len(r) for r in replies), default=0)):
        for agent_id, replies_for_agent in enumerate(replies):
            if round_idx < len(replies_for_agent):
                out.append({"source": f"Agent{agent_id + 1}", "content": replies_for_agent[round_idx]})
    return out


def _format_raw_messages(messages: List[Message], source_prefix: str = "message") -> str:
    """Format raw assistant messages without assuming a `source` field exists."""
    parts: List[str] = []
    for i, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        source = message.get("source") or f"{source_prefix}_{i}"
        parts.append(f"[{source}]: {content}")
    return "\n".join(parts) or "(no prior agent outputs)"


def generate_corrupted_message(config: Dict[str, Any], task: str, agent_context: List[Message], position: int,
                                error_type: str, fm_id: Optional[str] = None,
                                target_agent: Optional[str] = None,
                                agent_outputs: Optional[List[Dict[str, str]]] = None,
                                comparison_context: Optional[List[Message]] = None) -> Dict[str, Any]:
    """Generate one role-aware corruption or return an INELIGIBLE result."""
    mode = choose_failure_mode(error_type, fm_id)
    target = agent_context[position]
    target_content = target.get("content")
    if not isinstance(target_content, str):
        return {
            "eligible": False,
            "eligibility_reason": "Target message has no textual content to corrupt.",
            "content": None,
            "fm_id": mode["id"],
            "fm_name": mode["name"],
            "prompt": None,
            "injector_response": "INELIGIBLE",
        }
    conversation = _format_raw_messages(agent_context[:position])
    paired_section = ""
    if comparison_context is not None:
        if position >= len(comparison_context):
            return {
                "eligible": False,
                "eligibility_reason": "Comparison trace has no message at the candidate position.",
                "content": None,
                "fm_id": mode["id"],
                "fm_name": mode["name"],
                "prompt": None,
                "injector_response": "INELIGIBLE",
            }
        paired_target = comparison_context[position]
        paired_target_content = paired_target.get("content")
        if not isinstance(paired_target_content, str):
            return {
                "eligible": False,
                "eligibility_reason": "Comparison target message has no textual content to corrupt.",
                "content": None,
                "fm_id": mode["id"],
                "fm_name": mode["name"],
                "prompt": None,
                "injector_response": "INELIGIBLE",
            }
        paired_conversation = _format_raw_messages(comparison_context[:position], source_prefix="comparison_message")
        paired_section = (
            "COMPARISON TRACE (Trace B, same candidate position):\n"
            f"CONVERSATION SO FAR:\n{paired_conversation}\n\n"
            "TARGET MESSAGE (Trace B):\n---\n"
            f"{paired_target_content}\n---\n\n"
            "The corruption will be injected into BOTH Trace A and Trace B. "
            "It must therefore be semantically valid for both target messages."
        )
    prompt = _CORRUPTION_PROMPT.format(
        fm_instruction=mode["instruction"], task=task,
        target_agent=target_agent or "the debate participant represented by this context",
        conversation=conversation, original_content=target_content,
        paired_section=paired_section,
    )
    response = call_llm([{"role": "user", "content": prompt}], config, call_type="corruption").strip()
    lines = response.splitlines()
    marker = lines[0].strip().upper() if lines else ""
    if marker == "INELIGIBLE":
        return {"eligible": False, "eligibility_reason": "Injector rejected this target as incompatible with the selected failure mode.",
                "content": None, "fm_id": mode["id"], "fm_name": mode["name"], "prompt": prompt, "injector_response": response}
    if marker == "ELIGIBLE":
        response = "\n".join(lines[1:]).strip()
    return {"eligible": True, "eligibility_reason": None, "content": response,
            "fm_id": mode["id"], "fm_name": mode["name"], "prompt": prompt, "injector_response": response}


async def fork_trace_with_error(graph, config: Dict[str, Any], task: str, target: MessageCheckpoint,
                                 error_type: str, fm_id: Optional[str] = None,
                                 corruption: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Forks `graph` from `target`'s checkpoint, overwriting one agent's
    message with a corrupted version, then resumes to completion. Pass a
    precomputed `corruption` to inject the SAME text into multiple graphs
    (see run_paired_fork_experiment) instead of generating a fresh one
    per-graph."""
    snapshot = target["snapshot"]
    agent_id, position = target["agent_id"], target["position"]
    contexts = [list(c) for c in snapshot.values["contexts"]]
    original_content = contexts[agent_id][position]["content"]

    if corruption is None:
        corruption = generate_corrupted_message(config, task, contexts[agent_id], position, error_type, fm_id)
    if not corruption.get("eligible", True):
        raise ValueError(
            f"Selected injection target Agent {agent_id + 1} / position {position} is ineligible for "
            f"{corruption['fm_id']}: {corruption.get('eligibility_reason', '')}"
        )
    contexts[agent_id][position] = {"role": "assistant", "content": corruption["content"]}

    new_config = await graph.aupdate_state(snapshot.config, {"contexts": contexts}, as_node=target["node"])
    final_state = await graph.ainvoke(None, config=new_config)

    return {
        "error_type": error_type, "fm_id": corruption["fm_id"], "fm_name": corruption["fm_name"],
        "injected_at_step": (snapshot.metadata or {}).get("step"),
        "injected_at_agent_id": agent_id, "injected_at_position": position, "injected_at_node": target["node"],
        "original_message": original_content, "corrupted_message": corruption["content"],
        "injection": {k: corruption.get(k) for k in ("eligible", "eligibility_reason", "prompt", "injector_response")},
        "final_state": final_state,
    }


async def run_all_forks(graph, config: Dict[str, Any], task: str, target: Optional[MessageCheckpoint] = None,
                         strategy: str = "middle_agent_message", error_types: Optional[List[str]] = None,
                         fm_ids: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """fm_ids: optional {error_type: fm_id} to pin specific failure modes
    instead of random selection, e.g. {"specification_issue": "FM-1.3"}."""
    checkpoints = await list_message_checkpoints(graph, config)
    tgt = target or choose_target_checkpoint(checkpoints, strategy)
    results = []
    for error_type in error_types or ERROR_TYPES:
        result = await fork_trace_with_error(graph, config, task, tgt, error_type, (fm_ids or {}).get(error_type))
        results.append(result)
    return results


# --------------------------------------------------------------------------
# Paired baseline-vs-Gricean-checker experiment.
# --------------------------------------------------------------------------

async def run_paired_fork_experiment(query: str, debate_config: Dict[str, Any], task: str, error_type: str,
                                      agents_num: int = 3, rounds_num: int = 2, fm_id: Optional[str] = None,
                                      strategy: str = "middle_agent_message",
                                      attachment: Optional[Dict[str, Any]] = None,
                                      experiment_design: str = "4",
                                      records: Optional[List[Dict[str, Any]]] = None,
                                      recorder_factory=None,
                                      ) -> Dict[str, Any]:
    """Runs the SAME debate twice -- checker off, checker on -- each under
    its own MemorySaver/thread, picks ONE fork point using the selected
    experiment design's own intervention semantics (see below), generates
    ONE corruption there, and forks both graphs from that point with the
    identical corrupted content.

    Fork-point selection (`experiment_design`, see repo-root
    experiment_design.py):
      - Designs "1"/"2" apply a Trust_Allocator notice to EVERY message
        (there is no "before the checker did anything" region), so there
        is nothing to search for: the historical experiment for these
        designs always injects at the second eligible checkpoint,
        regardless of `strategy`.
      - Designs "3"/"4" treat a HIGH verdict as transparent (no notice, no
        reflection -- see gricean_check()/agent_turn() in
        langgraph_debate.py), so the checker-on run only starts behaving
        differently from checker-off at its first non-HIGH Trust_Allocator
        verdict. Only checkpoints before that point are eligible, and
        `strategy` ("first_agent_message" / "middle_agent_message" /
        "last_agent_message") picks among them exactly as it always has.

    Unlike the byte-identical-content matching this replaced, no content
    comparison between the two runs is needed or performed: cps_off and
    cps_on always share the same (agent_id, position) sequence turn for
    turn, because the debate's graph topology (which agent speaks in which
    round) depends only on agents_num/rounds_num, never on the
    Trust_Allocator's verdicts -- a flagged message triggers a private
    reflection call (agent_turn), but that reflection is never appended to
    `contexts`, so it can only change a later message's CONTENT, never the
    checkpoint structure. Pairing cps_off[i] with cps_on[i] by index is
    therefore always valid, regardless of what the Trust_Allocator decided.

    Returns a dict containing the two no-error states, the one shared corruption,
    and five separately scoped call lists: checker-off baseline, checker-on baseline,
    corruption generation, checker-off fork continuation, and checker-on fork continuation.

    The two states above are the SAME no-error baseline/trust-enabled
    pair the fork below branches from -- PART 1 of the instrumentation
    pass requires these to be preserved (they used to be computed here
    and then discarded), not recomputed: this function still only ever
    runs each side's debate to completion once.

    `recorder_factory` (see gaia_runner/debate_usage.py's patched_call_llm), when supplied,
    records each phase into its own list so no-error, injection, and fork branches cannot
    contaminate one another's token statistics. The legacy `records` argument is retained
    for compatibility with older direct callers but is no longer used for trace attribution.

    `attachment` (shape of gaia_utils.load_attachment()'s return value) is
    passed to BOTH graphs via the shared `base` dict below, not loaded or
    attached separately per side -- both graphs run init_agents() (the
    same pure function of `query` and `attachment`) as their identical
    first step, so both build their first message the same way, in the
    same order, for every agent. Do not load or construct the attachment
    separately for graph_off vs. graph_on.

    `experiment_design` is threaded into BOTH graph_off's and graph_on's
    initial state unchanged -- the same selected design must survive the
    fork, since it's what langgraph_debate.gricean_check() /
    construct_broadcast() key off of on each side. It also gets folded
    into each side's thread_id so traces from different designs never
    collide in the same MemorySaver.
    """
    saver_off, saver_on = MemorySaver(), MemorySaver()
    graph_off, graph_on = build_graph(saver_off), build_graph(saver_on)
    cfg_off = {"configurable": {"thread_id": f"baseline-design{experiment_design}"}, "recursion_limit": 300}
    cfg_on = {"configurable": {"thread_id": f"gricean-design{experiment_design}"}, "recursion_limit": 300}
    base = {"query": query, "agents_num": agents_num, "rounds_num": rounds_num, "round": 0, "agent_idx": 0,
            "contexts": [], "adherence": {}, "gricean_history": [], "reflections": {}, "final_answer": None,
            "config": debate_config, "attachment": attachment, "experiment_design": experiment_design}

    no_error_off_records: List[Dict[str, Any]] = []
    no_error_on_records: List[Dict[str, Any]] = []
    injection_records: List[Dict[str, Any]] = []
    error_off_records: List[Dict[str, Any]] = []
    error_on_records: List[Dict[str, Any]] = []

    if recorder_factory is not None:
        with recorder_factory(no_error_off_records):
            no_error_off = await graph_off.ainvoke({**base, "use_gricean_check": False}, config=cfg_off)
        with recorder_factory(no_error_on_records):
            no_error_on = await graph_on.ainvoke({**base, "use_gricean_check": True}, config=cfg_on)
    else:
        no_error_off = await graph_off.ainvoke({**base, "use_gricean_check": False}, config=cfg_off)
        no_error_on = await graph_on.ainvoke({**base, "use_gricean_check": True}, config=cfg_on)

    cps_off = await list_message_checkpoints(graph_off, cfg_off)
    cps_on = await list_message_checkpoints(graph_on, cfg_on)
    # See the docstring above: cps_off[i]/cps_on[i] are always "the same
    # point" turn-for-turn, so pairing by index (no content comparison) is
    # always valid.
    pairs = list(zip(cps_off, cps_on))
    if not pairs:
        raise ValueError("No assistant-turn checkpoints were produced by either run.")

    if experiment_design in ("1", "2"):
        # Every message gets a Trust_Allocator notice under these designs,
        # so there is no "before the checker did anything" region to pick
        # `strategy` over -- the historical experiment for Designs 1/2
        # always injects at the second eligible checkpoint.
        if len(pairs) < 2:
            raise ValueError(
                f"Design {experiment_design} requires at least 2 assistant-turn checkpoints to inject "
                f"at the second one, but only {len(pairs)} were produced."
            )
        eligible = pairs[:2]
        idx = 1
    else:
        # Designs 3/4: HIGH is transparent, so checker-on only starts
        # diverging in effect (not necessarily content) from checker-off
        # at its first non-HIGH Trust_Allocator verdict. gricean_history
        # is appended to in exactly the same per-turn order
        # list_message_checkpoints walks checkpoints in (see
        # gricean_check() in langgraph_debate.py), so its indices line up
        # 1:1 with `pairs`.
        gricean_history = no_error_on.get("gricean_history") or []
        first_non_high = next(
            (i for i, entry in enumerate(gricean_history)
             if str(entry.get("level", "")).strip().lower() != "high"),
            None,
        )
        eligible = pairs if first_non_high is None else pairs[:first_non_high]
        if not eligible:
            raise ValueError(
                "The Trust_Allocator's very first verdict was already non-HIGH, leaving no eligible "
                "checkpoint before it to fork from."
            )
        idx = {"first_agent_message": 0, "last_agent_message": -1}.get(strategy, len(eligible) // 2)

    candidate_order = [idx]
    if idx - 1 >= 0:
        candidate_order.append(idx - 1)
    if idx + 1 < len(eligible):
        candidate_order.append(idx + 1)
    ordered_pairs = [eligible[j] for j in candidate_order]

    selected_mode = choose_failure_mode(error_type, fm_id)
    selected_fm_id = selected_mode["id"]

    target_off = target_on = None
    corruption = None
    rejected_targets: List[Dict[str, Any]] = []
    for candidate_off, candidate_on in ordered_pairs:
        if recorder_factory is not None:
            with recorder_factory(injection_records):
                candidate_corruption = generate_corrupted_message(
                    debate_config, task, candidate_off["snapshot"].values["contexts"][candidate_off["agent_id"]],
                    candidate_off["position"], error_type, selected_fm_id, target_agent=f"Agent {candidate_off['agent_id'] + 1}",
                    agent_outputs=_all_agent_outputs(candidate_off["snapshot"].values.get("contexts", [])),
                    comparison_context=candidate_on["snapshot"].values["contexts"][candidate_on["agent_id"]],
                )
        else:
            candidate_corruption = generate_corrupted_message(
                debate_config, task, candidate_off["snapshot"].values["contexts"][candidate_off["agent_id"]],
                candidate_off["position"], error_type, selected_fm_id, target_agent=f"Agent {candidate_off['agent_id'] + 1}",
                agent_outputs=_all_agent_outputs(candidate_off["snapshot"].values.get("contexts", [])),
                comparison_context=candidate_on["snapshot"].values["contexts"][candidate_on["agent_id"]],
            )
        if candidate_corruption.get("eligible", True) and isinstance(candidate_corruption.get("content"), str) and candidate_corruption["content"].strip():
            target_off, target_on, corruption = candidate_off, candidate_on, candidate_corruption
            break
        rejected_targets.append({
            "agent_id": candidate_off["agent_id"],
            "position": candidate_off["position"],
            "fm_id": candidate_corruption.get("fm_id"),
            "fm_name": candidate_corruption.get("fm_name"),
            "eligible": candidate_corruption.get("eligible"),
            "eligibility_reason": candidate_corruption.get("eligibility_reason"),
            "injector_response": candidate_corruption.get("injector_response"),
        })

    if corruption is None:
        return {
            "no_error_off": no_error_off,
            "no_error_on": no_error_on,
            "no_error_records_off": no_error_off_records,
            "no_error_records_on": no_error_on_records,
            "error_off": None,
            "error_on": None,
            "error_off_records": error_off_records,
            "error_on_records": error_on_records,
            "corruption": {
                "eligible": False,
                "fm_id": selected_fm_id,
                "fm_name": rejected_targets[-1].get("fm_name") if rejected_targets else None,
                "eligibility_reason": (rejected_targets[-1].get("eligibility_reason") if rejected_targets else "No eligible target was available."),
                "attempts": rejected_targets,
            },
            "skipped": True,
            "skip_reason": (rejected_targets[-1].get("eligibility_reason") if rejected_targets else "No eligible target was available."),
            "injection_records": injection_records,
        }
    if recorder_factory is not None:
        with recorder_factory(error_off_records):
            result_off = await fork_trace_with_error(graph_off, debate_config, task, target_off, error_type, fm_id, corruption)
        with recorder_factory(error_on_records):
            result_on = await fork_trace_with_error(graph_on, debate_config, task, target_on, error_type, fm_id, corruption)
    else:
        result_off = await fork_trace_with_error(graph_off, debate_config, task, target_off, error_type, fm_id, corruption)
        result_on = await fork_trace_with_error(graph_on, debate_config, task, target_on, error_type, fm_id, corruption)
    return {
        "no_error_off": no_error_off,
        "no_error_on": no_error_on,
        "no_error_records_off": no_error_off_records,
        "no_error_records_on": no_error_on_records,
        "injection_records": injection_records,
        "error_off_records": error_off_records,
        "error_on_records": error_on_records,
        "corruption": corruption,
        "error_off": result_off,
        "error_on": result_on,
    }
