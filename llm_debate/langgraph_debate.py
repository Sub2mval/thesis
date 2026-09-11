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
# on here -- the checker's own low/medium/high engine is untouched, the
# merge happens where the level is consumed. HIGH behaves exactly as a
# normal broadcast always has. NOT_HIGH additionally triggers a private
# reflection call for the receiving agent: the flagged message (still
# broadcast into permanent context as usual, wrapped in its notice) plus
# the checker's reasoning become the reflection prompt; the reflection
# text is used once, to help shape *this turn's* answer, then stored in
# its own state variable (state["reflections"]) -- never appended to any
# agent_contexts, never re-shown to its author or anyone else afterward.
#
# gricean_check itself runs on every turn regardless of use_gricean_check,
# so adherence history is always collected for analysis. use_gricean_check
# only controls whether agent_turn actually surfaces that history to the
# agents (notices in the broadcast, reflection triggering) -- when off,
# construct_broadcast is handed an empty adherence view, so the debate
# proceeds exactly as if the checker weren't running at all, while
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
    wrap_with_adherence_notice,
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
def call_llm(messages: List[Message], config: Dict[str, Any]) -> str:
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
                         adherence: Dict[int, Dict[str, str]]) -> Tuple[Message, List[Tuple[int, str, str]]]:
    """Returns (broadcast_message, flagged) where flagged lists the
    (agent_id, raw_content, checker_reason) triples for NOT_HIGH sources
    this round -- used by the caller to decide whether to reflect."""
    if not others:
        return {"role": "user", "content": "Please verify and restate your answer clearly at the end."}, []
    parts = ["Other agents' current answers:"]
    flagged: List[Tuple[int, str, str]] = []
    for agent_id, ctx in others:
        content = ctx[round_idx]["content"]
        info = adherence.get(agent_id)
        text = wrap_with_adherence_notice(content, info["level"], info["reason"]) if info else content
        parts.append(f"\nAgent {agent_id + 1}: ```{text}```")
        if info and info["level"] == "not_high":
            flagged.append((agent_id, content, info["reason"]))
    parts.append(f"\n\nUsing this as advice, give an updated answer to: {question}\n"
                  "State your final answer clearly at the end.")
    return {"role": "user", "content": "\n".join(parts)}, flagged


def _reflection_prompt(query: str, flagged: List[Tuple[int, str, str]]) -> str:
    blocks = "\n\n".join(
        f"Flagged message from Agent {aid + 1}:\n```{content}```\nGricean adherence checker's reasoning: {reason}"
        for aid, content, reason in flagged
    )
    return (
        "Before answering, privately reflect on the message(s) below, which the Gricean adherence "
        "checker flagged as NOT high adherence. This reflection is private: it will not be shown to "
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
    reflections: Dict[int, List[Dict[str, Any]]]
    final_answer: Optional[str]
    config: Dict[str, Any]
    use_gricean_check: bool
    attachment: Optional[Dict[str, Any]]  # shape of gaia_utils.load_attachment()'s return value


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
            "adherence": {}, "reflections": {}}


def agent_turn(state: DebateState) -> Dict[str, Any]:
    i, r = state["agent_idx"], state["round"]
    contexts = [list(c) for c in state["contexts"]]
    flagged: List[Tuple[int, str, str]] = []
    if r != 0:
        others = [(j, contexts[j]) for j in range(state["agents_num"]) if j != i]
        # Adherence history is always collected (see gricean_check below,
        # which now runs unconditionally) so it's available for analysis
        # either way -- but it's only surfaced to the agents (notices,
        # reflection) when use_gricean_check is on. Passing {} here makes
        # construct_broadcast treat every source as unassessed, i.e. plain
        # pass-through with no flagging, exactly like the checker were off.
        visible_adherence = state["adherence"] if state["use_gricean_check"] else {}
        broadcast, flagged = construct_broadcast(others, state["query"], 2 * r - 1, visible_adherence)
        contexts[i].append(broadcast)

    reflections = {k: list(v) for k, v in state["reflections"].items()}
    extra: List[Message] = []
    if flagged:
        reflection = call_llm([{"role": "user", "content": _reflection_prompt(state["query"], flagged)}],
                               state["config"])
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

    reply = call_llm(contexts[i] + extra, state["config"])
    contexts[i].append({"role": "assistant", "content": reply})
    next_i = (i + 1) % state["agents_num"]
    return {"contexts": contexts, "agent_idx": next_i, "round": r + 1 if next_i == 0 else r,
            "reflections": reflections}


def gricean_check(state: DebateState) -> Dict[str, Any]:
    # Runs on every turn regardless of use_gricean_check, so adherence
    # history is always collected for analysis. Whether it's actually
    # shown to the agents is decided downstream, in agent_turn.
    speaker = (state["agent_idx"] - 1) % state["agents_num"]
    ctx = state["contexts"][speaker]
    conversation = format_conversation([{"source": m["role"], "content": m["content"]} for m in ctx])
    prompt = format_gricean_check_prompt(state["query"], conversation, f"Agent {speaker + 1}")
    scores = parse_gricean_scores(call_llm([{"role": "user", "content": prompt}], state["config"]))
    level = score_to_gricean_level({m: scores[m]["score"] for m in GRICEAN_METRICS})
    adherence = dict(state["adherence"])
    adherence[speaker] = {"level": "high" if level == "high" else "not_high",
                           "reason": format_combined_reason(scores)}
    return {"adherence": adherence}


def aggregate(state: DebateState) -> Dict[str, Any]:
    answers = "\n\n".join(f"Solution {i + 1}:\n{c[-1]['content']}" for i, c in enumerate(state["contexts"]))
    prompt = f"Task:\n{state['query']}\n\n{answers}\n\nReason over these solutions and give one final answer."
    instruction = state["config"].get("answer_format_instruction")
    if instruction:
        prompt += f"\n\n{instruction}"
    return {"final_answer": call_llm([{"role": "user", "content": prompt}], state["config"])}


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
               use_gricean_check: bool = False, attachment: Optional[Dict[str, Any]] = None) -> str:
    app = build_graph()
    result = app.invoke(
        {"query": query, "agents_num": agents_num, "rounds_num": rounds_num, "round": 0, "agent_idx": 0,
         "contexts": [], "adherence": {}, "reflections": {}, "final_answer": None, "config": config,
         "use_gricean_check": use_gricean_check, "attachment": attachment},
        config={"recursion_limit": 300},
    )
    return result["final_answer"]


if __name__ == "__main__":
    cfg = {"model_list": [{"model": "llama3.1:8b", "host": "http://localhost:11434"}],
           "temperature": 0.7, "max_tokens": 1024}
    print("--- no adherence check ---")
    print(run_debate("What is 17 * 24?", cfg))
    print("--- with adherence check ---")
    print(run_debate("What is 17 * 24?", cfg, use_gricean_check=True))