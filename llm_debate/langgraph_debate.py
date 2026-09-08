# langgraph_debate_minimal.py
#
# Minimal LangGraph reimplementation of multi-agent LLM debate (Du et al.,
# arXiv:2305.14325), wired to the required Trust Allocator in
# trust_allocator.py (used as-is, unmodified -- Gricean 4-axis rubric,
# deterministic level-from-scores derivation, HIGH-PRIORITY notice
# template). trust_allocator.py's format_conversation() expects
# {"source", "content"} dicts, so context is adapted at the call site
# without touching that module.
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

from trust_allocator import (
    TRUST_METRICS,
    format_combined_reason,
    format_conversation,
    format_trust_allocator_prompt,
    score_to_trust_level,
    wrap_with_trust_notice,
)

Message = Dict[str, Any]  # role/content, plus optionally images/audio for attachments


@retry(wait=wait_exponential(multiplier=1, min=4, max=10), stop=stop_after_attempt(5))
def call_llm(messages: List[Message], config: Dict[str, Any]) -> str:
    model = random.choice(config["model_list"])
    client = ollama.Client(host=model.get("host", "http://localhost:11434"))
    options = {k: v for k, v in (("temperature", config.get("temperature")),
                                  ("num_predict", config.get("max_tokens"))) if v is not None}
    resp = client.chat(model=model["model"], messages=messages, options=options)
    return resp["message"]["content"]


def parse_trust_scores(text: str) -> Dict[str, Dict[str, Any]]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0))
        return {m: {"score": int(data[m]["score"]), "reason": str(data[m].get("reason", ""))} for m in TRUST_METRICS}
    except Exception:
        return {m: {"score": 3, "reason": "could not parse Trust_Allocator output"} for m in TRUST_METRICS}


def construct_broadcast(others: List[Tuple[int, List[Message]]], question: str, round_idx: int,
                         trust: Dict[int, Dict[str, str]]) -> Message:
    if not others:
        return {"role": "user", "content": "Please verify and restate your answer clearly at the end."}
    parts = ["Other agents' current answers:"]
    for agent_id, ctx in others:
        content = ctx[round_idx]["content"]
        info = trust.get(agent_id)
        text = wrap_with_trust_notice(content, info["level"], info["reason"]) if info else content
        parts.append(f"\nAgent {agent_id + 1}: ```{text}```")
    parts.append(f"\n\nUsing this as advice, give an updated answer to: {question}\n"
                  "State your final answer clearly at the end.")
    return {"role": "user", "content": "\n".join(parts)}


class DebateState(TypedDict):
    query: str
    agents_num: int
    rounds_num: int
    round: int
    agent_idx: int
    contexts: List[List[Message]]
    trust: Dict[int, Dict[str, str]]
    final_answer: Optional[str]
    config: Dict[str, Any]
    use_trust: bool
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
    return {"contexts": [[dict(msg)] for _ in range(state["agents_num"])], "round": 0, "agent_idx": 0, "trust": {}}


def agent_turn(state: DebateState) -> Dict[str, Any]:
    i, r = state["agent_idx"], state["round"]
    contexts = [list(c) for c in state["contexts"]]
    if r != 0:
        others = [(j, contexts[j]) for j in range(state["agents_num"]) if j != i]
        contexts[i].append(construct_broadcast(others, state["query"], 2 * r - 1, state["trust"]))
    reply = call_llm(contexts[i], state["config"])
    contexts[i].append({"role": "assistant", "content": reply})
    next_i = (i + 1) % state["agents_num"]
    return {"contexts": contexts, "agent_idx": next_i, "round": r + 1 if next_i == 0 else r}


def trust_check(state: DebateState) -> Dict[str, Any]:
    speaker = (state["agent_idx"] - 1) % state["agents_num"]
    ctx = state["contexts"][speaker]
    conversation = format_conversation([{"source": m["role"], "content": m["content"]} for m in ctx])
    prompt = format_trust_allocator_prompt(state["query"], conversation, f"Agent {speaker + 1}")
    scores = parse_trust_scores(call_llm([{"role": "user", "content": prompt}], state["config"]))
    trust = dict(state["trust"])
    trust[speaker] = {"level": score_to_trust_level({m: scores[m]["score"] for m in TRUST_METRICS}),
                       "reason": format_combined_reason(scores)}
    return {"trust": trust}


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
    if debate_finished(state):
        return "aggregate"
    return "trust_check" if state["use_trust"] else "agent_turn"


def build_graph(checkpointer=None):
    g = StateGraph(DebateState)
    g.add_node("init_agents", init_agents)
    g.add_node("agent_turn", agent_turn)
    g.add_node("trust_check", trust_check)
    g.add_node("aggregate", aggregate)
    g.add_edge(START, "init_agents")
    g.add_edge("init_agents", "agent_turn")
    g.add_conditional_edges("agent_turn", route_after_turn,
                             {"agent_turn": "agent_turn", "trust_check": "trust_check", "aggregate": "aggregate"})
    g.add_edge("trust_check", "agent_turn")
    g.add_edge("aggregate", END)
    return g.compile(checkpointer=checkpointer)


def run_debate(query: str, config: Dict[str, Any], agents_num: int = 3, rounds_num: int = 2,
               use_trust: bool = False, attachment: Optional[Dict[str, Any]] = None) -> str:
    app = build_graph()
    result = app.invoke(
        {"query": query, "agents_num": agents_num, "rounds_num": rounds_num, "round": 0, "agent_idx": 0,
         "contexts": [], "trust": {}, "final_answer": None, "config": config, "use_trust": use_trust,
         "attachment": attachment},
        config={"recursion_limit": 300},
    )
    return result["final_answer"]


if __name__ == "__main__":
    cfg = {"model_list": [{"model": "llama3.1:8b", "host": "http://localhost:11434"}],
           "temperature": 0.7, "max_tokens": 1024}
    print("--- no trust ---")
    print(run_debate("What is 17 * 24?", cfg))
    print("--- with trust ---")
    print(run_debate("What is 17 * 24?", cfg, use_trust=True))