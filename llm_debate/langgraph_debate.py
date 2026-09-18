# langgraph_debate_minimal.py
#
# Minimal LangGraph reimplementation of multi-agent LLM debate (Du et al.,
# arXiv:2305.14325), wired to the required Gricean adherence checker in
# Gricean_check.py (used as-is, unmodified engine -- 4-axis Gricean rubric,
# deterministic level-from-scores derivation). Gricean_check.py's
# format_conversation() expects {"source", "content"} dicts, so context is
# adapted at the call site without touching that module.
#
# Adherence handling: only HIGH vs. NOT_HIGH (low+medium merged) is acted
# on here -- the checker's own engine is untouched, the merge happens where
# the level is consumed. HIGH behaves exactly as a normal broadcast always
# has. NOT_HIGH additionally triggers a private reflection call for the
# receiving agent: the flagged message's raw content (delivered to other
# agents unmodified -- the checker's verdict never alters what is actually
# broadcast, matching magnetic_one's Gricean_Checker, which never touches
# MessageHistory either) plus the checker's reasoning become the reflection
# prompt; the reflection text is used once, to help shape *this turn's*
# answer, then stored in its own state variable (state["reflections"]) --
# never appended to any agent_contexts, never re-shown to its author or
# anyone else afterward.
#
# gricean_check itself runs on every turn regardless of use_gricean_check,
# so adherence history is always collected for analysis. use_gricean_check
# only controls whether agent_turn actually surfaces that history to the
# agents (reflection triggering) -- when off, construct_broadcast is handed
# an empty adherence view, so no reflection is ever triggered and the
# debate proceeds exactly as if the checker weren't running at all, while
# state["adherence"] still fills up in the background.
#
# Dropped vs. the original long version (plumbing only, not mechanism):
# multi-model/key routing config schema, YAML config loading, token/usage
# bookkeeping, tenacity's retry_error_callback.
#
# Duck-types against gaia_utils.py rather than importing it: init_agents
# accepts an `attachment` dict shaped exactly like load_attachment()'s
# return value, and aggregate() appends config["answer_format_instruction"]
# verbatim if present (pass gaia_utils.GAIA_ANSWER_FORMAT_INSTRUCTION there
# to get "FINAL ANSWER: ..." lines that extract_gaia_answer/
# gaia_question_scorer can consume). Neither is imported here so this file
# has no hard GAIA dependency.
#
# Experiment-design / Trust_Allocator extension (see repo-root
# experiment_design.py): state["experiment_design"] (default "4", set by
# run_debate) picks which experiment design gricean_check() below scores
# every turn under. Designs "1", "2", "3", and "4" ALL short-circuit to
# the canonical Trust_Allocator (trust_allocator.legacy_trust_allocator.
# allocate), resolved via experiment_design.resolve_design() -- the old
# 4-axis Gricean checker above is no longer dispatched to for any of
# these designs (the Gricean_check.py module/functions are left in place
# for unrelated legacy compatibility, but nothing here calls them anymore).
# construct_broadcast wraps each flagged agent's broadcast content with
# that agent's trust notice (trust_allocator.legacy_trust_allocator.
# wrap_with_trust_notice); for designs whose resolved policy also has
# reflect=True (Design 4's medium/low verdicts), that agent is additionally
# added to `flagged` so agent_turn triggers one private reflection call.

import asyncio
import json
import random
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import ollama
from tenacity import retry, stop_after_attempt, wait_exponential

from langgraph.graph import END, START, StateGraph

from .Gricean_check import (
    GRICEAN_METRICS,
    format_combined_reason,
    format_conversation,
    format_gricean_check_prompt,
    score_to_gricean_level,
)
from experiment_design import resolve_design
from trust_allocator.legacy_trust_allocator import (
    TRUST_ALLOCATOR_NODE_NAME,
    allocate as allocate_trust,
    wrap_with_trust_notice,
)

Message = Dict[str, Any]  # role/content, plus optionally images/audio for attachments


# NOTE on determinism: random.choice(model_list) below still calls into
# Python's shared `random` module (it draws bits even when len==1), and the
# Gricean checker/reflection nodes make extra call_llm() calls that a
# checker-off run never makes. With a single-entry model_list this is
# harmless -- random.choice always returns that one entry regardless of
# how much PRNG state gets consumed. With more than one entry, the extra
# draws WILL shift which entry later debate turns land on between
# checker-off and checker-on runs, breaking bit-for-bit reproducibility of
# *which model handles which turn* even at temperature 0 + a fixed seed.
# For a strict determinism guarantee, use a single-entry model_list --
# UNLESS every entry names the identical model and differs only in
# `api_key`/`host` (e.g. several Ollama Cloud keys load-balancing the same
# model): in that case which entry gets picked has no effect on the
# response itself (same weights, same temperature=0, same seed), only on
# which key pays for the call, so multi-entry rotation is safe there.
#
# `model` entries may optionally carry an `api_key` (e.g. one of several
# Ollama Cloud keys) -- forwarded as a Bearer Authorization header on a
# per-call basis, so N keys in model_list gives free random load-spreading
# across them via the random.choice() above. Falls back to whatever
# OLLAMA_API_KEY env var is set (or no auth) when `api_key` is absent.
@retry(wait=wait_exponential(multiplier=1, min=4, max=10), stop=stop_after_attempt(5))
def call_llm(messages: List[Message], config: Dict[str, Any], call_type: str = "unknown") -> str:
    # call_type is purely a label for instrumentation (see gaia_runner/debate_usage.py) --
    # it has no effect on model selection or the request itself.
    model = random.choice(config["model_list"])
    headers = {"authorization": f"Bearer {model['api_key']}"} if model.get("api_key") else None
    client = ollama.Client(host=model.get("host", "http://localhost:11434"), headers=headers)
    options = {k: v for k, v in (("temperature", config.get("temperature")),
                                  ("num_predict", config.get("max_tokens")),
                                  ("seed", config.get("seed"))) if v is not None}
    resp = client.chat(model=model["model"], messages=messages, options=options)
    return resp["message"]["content"]


def parse_gricean_scores(text: str) -> Dict[str, Dict[str, Any]]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0))
        return {m: {"score": int(data[m]["score"]), "reason": str(data[m].get("reason", ""))} for m in GRICEAN_METRICS}
    except Exception:
        return {m: {"score": 3, "reason": "could not parse Gricean adherence checker output"} for m in GRICEAN_METRICS}


def construct_broadcast(others: List[Tuple[int, List[Message]]], question: str, round_idx: int,
                         adherence: Dict[int, Dict[str, str]],
                         design: str = "4") -> Tuple[Message, List[Tuple[int, str, str]]]:
    """Returns (broadcast_message, flagged) where flagged lists the
    (agent_id, raw_content, checker_reason) triples the caller should
    trigger a private reflection call for -- used by the caller to decide
    whether to reflect.

    All of designs 1-4 go through the Trust_Allocator here: each agent's
    `adherence` entry carries {"trust_level", "reason", "notice",
    "reflect"} (see gricean_check() below / experiment_design.
    resolve_design()). Every agent whose resolved policy has a `notice`
    gets that trust notice prepended to ITS content in the broadcast text
    itself, per that design's notice policy. `flagged` is populated only
    for entries whose policy says reflect=True -- never true under
    Designs 1-3 (always reflect=False), but true for a medium/low verdict
    under Design 4."""
    if not others:
        return {"role": "user", "content": "Please verify and restate your answer clearly at the end."}, []
    parts = ["Other agents' current answers:"]
    flagged: List[Tuple[int, str, str]] = []
    for agent_id, ctx in others:
        content = ctx[round_idx]["content"]
        info = adherence.get(agent_id)
        delivered = content
        if info and info.get("notice"):
            delivered = wrap_with_trust_notice(content, info["notice"])
        parts.append(f"\nAgent {agent_id + 1}: ```{delivered}```")
        if info and info.get("reflect"):
            flagged.append((agent_id, content, info.get("reason", "")))
    parts.append(f"\n\nUsing this as advice, give an updated answer to: {question}\n"
                  "State your final answer clearly at the end.")
    return {"role": "user", "content": "\n".join(parts)}, flagged


def _reflection_prompt(query: str, flagged: List[Tuple[int, str, str]]) -> str:
    blocks = "\n\n".join(
        f"Flagged message from Agent {aid + 1}:\n```{content}```\nTrust_Allocator's reason for the LOW-trust verdict: {reason}"
        for aid, content, reason in flagged
    )
    return (
        "Before answering, privately reflect on the message(s) below, which the Trust_Allocator "
        "flagged as LOW trust. This reflection is private: it will not be shown to "
        "any other agent, and it will not be available to you in future turns.\n"
        f"Task: {query}\n\n{blocks}\n\n"
        "Write a brief reflection: how much should you rely on this information, and what specifically "
        "do you plan to do differently (if anything) in your next answer as a result?"
    )


class DebateState(TypedDict):
    query: str
    agents_num: int
    rounds_num: int
    round: int
    agent_idx: int
    contexts: List[List[Message]]
    adherence: Dict[int, Dict[str, str]]
    # Append-only: one entry per Gricean check, added here in gricean_check().
    # `adherence` above stays as the transient "latest verdict per agent"
    # cache construct_broadcast() reads for live notice-gating -- it gets
    # overwritten every time that agent speaks again, so on its own it
    # can't answer "what did the checker say about round 1?" once round 2
    # has run. This list is what actually preserves that per-message history.
    gricean_history: List[Dict[str, Any]]
    reflections: Dict[int, List[Dict[str, Any]]]
    final_answer: Optional[str]
    config: Dict[str, Any]
    use_gricean_check: bool
    attachment: Optional[Dict[str, Any]]  # shape of gaia_utils.load_attachment()'s return value
    # Experiment-design / Trust_Allocator extension (see repo-root
    # experiment_design.py and the module docstring above). "4" (the
    # default) preserves the pre-existing Gricean-checker behavior above
    # completely unchanged; set once by run_debate, never mutated by any
    # node.
    experiment_design: str


def _build_initial_message(query: str, attachment: Optional[Dict[str, Any]]) -> Message:
    msg: Message = {"role": "user", "content": f"{query}\nState your final answer clearly at the end."}
    if not attachment:
        return msg
    if attachment.get("text"):
        note = f" {attachment['note']}" if attachment.get("note") else ""
        msg["content"] += f"\n\nAttached file contents:{note}\n\n{attachment['text']}"
    if attachment.get("images_b64"):
        msg["images"] = attachment["images_b64"]
    if attachment.get("audio_b64"):
        msg["audio"] = attachment["audio_b64"]
    if attachment.get("kind") == "unsupported" and attachment.get("note"):
        msg["content"] += f"\n\n[Note: an attached file could not be included -- {attachment['note']}]"
    return msg


def init_agents(state: DebateState) -> Dict[str, Any]:
    msg = _build_initial_message(state["query"], state.get("attachment"))
    return {"contexts": [[dict(msg)] for _ in range(state["agents_num"])], "round": 0, "agent_idx": 0,
            "adherence": {}, "gricean_history": [], "reflections": {}}


def agent_turn(state: DebateState) -> Dict[str, Any]:
    i, r = state["agent_idx"], state["round"]
    contexts = [list(c) for c in state["contexts"]]
    flagged: List[Tuple[int, str, str]] = []
    if r != 0:
        others = [(j, contexts[j]) for j in range(state["agents_num"]) if j != i]
        design = state.get("experiment_design", "4")
        # Adherence history is always collected (see gricean_check below,
        # which now runs unconditionally) so it's available for analysis
        # either way. It's only ever surfaced to the agents (notices,
        # reflection) when use_gricean_check is on -- passing {} makes
        # construct_broadcast treat every source as unassessed, i.e. plain
        # pass-through with no flagging, exactly matching a baseline run
        # with the checker off. This holds for design "4" and for designs
        # 1-3 alike: use_gricean_check is the intervention switch, and a
        # baseline run (use_gricean_check=False) must receive the raw
        # broadcast with no trust notice and no reflection regardless of
        # which design is selected -- see gricean_checker.py's baseline
        # branch in magnetic_one for the matching enforcement there.
        visible_adherence = state["adherence"] if state["use_gricean_check"] else {}
        broadcast, flagged = construct_broadcast(others, state["query"], 2 * r - 1, visible_adherence, design)
        contexts[i].append(broadcast)

    reflections = {k: list(v) for k, v in state["reflections"].items()}
    extra: List[Message] = []
    if flagged:
        reflection = call_llm([{"role": "user", "content": _reflection_prompt(state["query"], flagged)}],
                               state["config"], call_type="reflection")
        # Kept for the record, with the context of what it was responding
        # to (round + the flagged messages/reasons) -- never fed back into
        # any prompt, and accumulated (not overwritten) so a full history
        # survives even if this agent reflects more than once.
        reflections.setdefault(i, []).append({
            "round": r,
            "responding_to": [{"agent_id": aid, "content": content, "reason": reason} for aid, content, reason in flagged],
            "reflection": reflection,
        })
        extra = [{"role": "user", "content": f"[Private reflection -- not part of the shared conversation]\n"
                                              f"{reflection}\n\nNow give your updated answer to the task."}]

    reply = call_llm(contexts[i] + extra, state["config"], call_type="agent_turn")
    contexts[i].append({"role": "assistant", "content": reply})
    next_i = (i + 1) % state["agents_num"]
    return {"contexts": contexts, "agent_idx": next_i, "round": r + 1 if next_i == 0 else r,
            "reflections": reflections}


def _debate_client_for_allocator(config: Dict[str, Any]):
    """Adapt langgraph_debate's own call_llm() into the sync
    ClientCallable shape trust_allocator.legacy_trust_allocator.allocate
    expects (prompt string -> raw response string)."""

    def _client(prompt: str) -> str:
        return call_llm([{"role": "user", "content": prompt}], config, call_type="trust_allocator")

    return _client


async def _trust_allocator_check(state: DebateState, speaker: int, ctx: List[Message], msg_round: int) -> Dict[str, Any]:
    """Design 1-3 branch of gricean_check: score the last message with
    the canonical Trust_Allocator instead of the Gricean 4-axis checker,
    then resolve the verdict via experiment_design.resolve_design().
    Designs 1, 2, and 3 are implemented (see experiment_design.py). Only
    called when use_gricean_check is on -- see the baseline short-circuit
    in gricean_check() below."""
    design = state["experiment_design"]
    conversation = format_conversation([{"source": m["role"], "content": m["content"]} for m in ctx])
    verdict = await allocate_trust(
        state["query"], conversation, f"Agent {speaker + 1}", _debate_client_for_allocator(state["config"])
    )
    raw_trust_level = verdict["trust_level"]
    reason = verdict["reason"]
    policy = resolve_design(raw_trust_level, design)

    adherence = dict(state["adherence"])
    adherence[speaker] = {
        "trust_level": raw_trust_level, "reason": reason,
        "notice": policy["notice"], "reflect": policy["reflect"],
    }
    gricean_history = list(state["gricean_history"])
    gricean_history.append({
        "round": msg_round, "agent_id": speaker, "level": raw_trust_level, "reason": reason,
        "scores": None,  # the Trust_Allocator has no per-axis scores, unlike the Gricean checker
        "experiment_design": design, "notice_applied": policy["notice"], "reflect": policy["reflect"],
    })
    return {"adherence": adherence, "gricean_history": gricean_history}


def gricean_check(state: DebateState) -> Dict[str, Any]:
    # Runs on every turn regardless of use_gricean_check, so adherence
    # history is always collected for analysis. Whether it's actually
    # shown to the agents is decided downstream, in agent_turn.
    speaker = (state["agent_idx"] - 1) % state["agents_num"]
    ctx = state["contexts"][speaker]
    # agent_turn already advanced state["round"] to round+1 by the time this
    # node runs, but only when `speaker` was the last agent in that round
    # (next_i wrapped to 0) -- undo that so the logged round matches the
    # round the checked message actually belongs to.
    msg_round = state["round"] - 1 if speaker == state["agents_num"] - 1 else state["round"]

    # Designs 1-4 all route through the canonical Trust_Allocator +
    # experiment_design.resolve_design(); the old 4-axis Gricean scoring
    # path below is no longer dispatched to for any of them (see the
    # module docstring above).
    design = state.get("experiment_design", "4")
    if not state.get("use_gricean_check", False):
        # Baseline: use_gricean_check is the intervention switch for
        # Trust_Allocator designs. Do NOT call the canonical
        # Trust_Allocator on a baseline run -- just clear any
        # transient per-speaker intervention state left over from a
        # prior turn and continue. gricean_history is intentionally
        # left untouched (no entry logged for this baseline turn).
        adherence = dict(state["adherence"])
        adherence.pop(speaker, None)
        return {"adherence": adherence}
    return asyncio.run(_trust_allocator_check(state, speaker, ctx, msg_round))


def aggregate(state: DebateState) -> Dict[str, Any]:
    answers = "\n\n".join(f"Solution {i + 1}:\n{c[-1]['content']}" for i, c in enumerate(state["contexts"]))
    prompt = f"Task:\n{state['query']}\n\n{answers}\n\nReason over these solutions and give one final answer."
    instruction = state["config"].get("answer_format_instruction")
    if instruction:
        prompt += f"\n\n{instruction}"
    return {"final_answer": call_llm([{"role": "user", "content": prompt}], state["config"], call_type="aggregate")}


def debate_finished(state: DebateState) -> bool:
    return state["agent_idx"] == 0 and state["round"] >= state["rounds_num"]


def route_after_turn(state: DebateState) -> str:
    # Always routes through gricean_check (unless finished) -- adherence
    # history is collected regardless of use_gricean_check; see agent_turn
    # for where that flag actually takes effect.
    return "aggregate" if debate_finished(state) else "gricean_check"


def build_graph(checkpointer=None):
    g = StateGraph(DebateState)
    g.add_node("init_agents", init_agents)
    g.add_node("agent_turn", agent_turn)
    g.add_node("gricean_check", gricean_check)
    g.add_node("aggregate", aggregate)
    g.add_edge(START, "init_agents")
    g.add_edge("init_agents", "agent_turn")
    g.add_conditional_edges("agent_turn", route_after_turn, {"gricean_check": "gricean_check", "aggregate": "aggregate"})
    g.add_edge("gricean_check", "agent_turn")
    g.add_edge("aggregate", END)
    return g.compile(checkpointer=checkpointer)


def run_debate(query: str, config: Dict[str, Any], agents_num: int = 3, rounds_num: int = 2,
               use_gricean_check: bool = False, attachment: Optional[Dict[str, Any]] = None,
               experiment_design: str = "4") -> Dict[str, Any]:
    """Returns the full final DebateState (contexts/adherence/reflections/
    final_answer/etc), not just the answer string -- callers that only
    want the answer should read result["final_answer"].

    experiment_design: "4" (the default) preserves the pre-existing
    Gricean-checker behavior above completely unchanged; "1" routes
    every turn's adherence check through the canonical Trust_Allocator
    instead (see gricean_check() / experiment_design.resolve_design()).
    """
    app = build_graph()
    result = app.invoke(
        {"query": query, "agents_num": agents_num, "rounds_num": rounds_num, "round": 0, "agent_idx": 0,
         "contexts": [], "adherence": {}, "gricean_history": [], "reflections": {}, "final_answer": None,
         "config": config, "use_gricean_check": use_gricean_check, "attachment": attachment,
         "experiment_design": experiment_design},
        config={"recursion_limit": 300},
    )
    return result


if __name__ == "__main__":
    cfg = {"model_list": [{"model": "llama3.1:8b", "host": "http://localhost:11434"}],
           "temperature": 0.7, "max_tokens": 1024}
    print("--- no adherence check ---")
    print(run_debate("What is 17 * 24?", cfg)["final_answer"])
    print("--- with adherence check ---")
    print(run_debate("What is 17 * 24?", cfg, use_gricean_check=True)["final_answer"])
