"""
The MagenticOneOrchestrator agent node.

Unlike every other node in this graph, this is not one LLM call: it's the
one node that stands in for the whole "Orchestrator Agent" box in the
reference diagram, so it owns all of the orchestrator's own bookkeeping --
the Task Ledger and Progress Ledger both live in MagenticState (state.py),
not as separate graph nodes. Each call:
  - the FIRST time it's called (no messages yet), first builds the Task
    Ledger (facts + plan) and an initial single-message history from it
  - always does a Progress Ledger pass over the current message history
  - if that pass says the team is stalled, replans (rebuilds the Task
    Ledger and resets the message history to a fresh ledger message) and
    tries again, up to MAX_REPLANS_PER_TURN times
  - once it's decided who to dispatch to next -- or that the request is
    satisfied / max rounds are up -- returns, and orchestrator_graph.py
    routes to either Gricean_Checker or straight to END.

MAX_REPLANS_PER_TURN exists because, now that replanning happens inside
one node call instead of as separate graph steps, LangGraph's
`recursion_limit` no longer bounds it -- a team that never stops looking
"stalled" would otherwise loop here forever.
"""

from __future__ import annotations

from typing import Dict, List

from autogen_core.models import ChatCompletionClient, UserMessage

from magnetic_one.context_utils import ORCHESTRATOR_NAME, build_llm_context, get_compatible_context, team_description
from magnetic_one.json_llm import call_model_for_json
from magnetic_one.prompts import (
    ORCHESTRATOR_PROGRESS_LEDGER_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_FACTS_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_FACTS_UPDATE_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_FULL_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT,
    ORCHESTRATOR_TASK_LEDGER_PLAN_UPDATE_PROMPT,
)
from magnetic_one.state import MagenticState

MAX_REPLANS_PER_TURN = 5


def build_orchestrator_node(
    model_client: ChatCompletionClient,
    participant_names: List[str],
    participant_descriptions: Dict[str, str],
    max_rounds: int,
    max_stalls: int,
    final_answer_prompt: str,
):
    description = team_description(participant_names, [participant_descriptions[n] for n in participant_names])

    def _ledger_message(state: MagenticState) -> Dict[str, str]:
        ledger = state["task_ledger"]
        content = ORCHESTRATOR_TASK_LEDGER_FULL_PROMPT.format(
            task=state["task"], team=state["team_description"], facts=ledger["facts"], plan=ledger["plan"]
        )
        return {"source": ORCHESTRATOR_NAME, "content": content}

    async def _bootstrap(state: MagenticState) -> MagenticState:
        planning = [UserMessage(content=ORCHESTRATOR_TASK_LEDGER_FACTS_PROMPT.format(task=state["task"]), source=ORCHESTRATOR_NAME)]
        response = await model_client.create(get_compatible_context(model_client, planning))
        facts = response.content
        planning += [
            UserMessage(content=facts, source=ORCHESTRATOR_NAME),
            UserMessage(content=ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT.format(team=description), source=ORCHESTRATOR_NAME),
        ]
        response = await model_client.create(get_compatible_context(model_client, planning))

        state = {
            **state,
            "team_description": description,
            "participant_names": participant_names,
            "task_ledger": {"facts": facts, "plan": response.content},
            "n_stalls": 0,
        }
        return {**state, "messages": [_ledger_message(state)]}

    async def _replan(state: MagenticState) -> MagenticState:
        context = build_llm_context(state["messages"])
        context.append(
            UserMessage(
                content=ORCHESTRATOR_TASK_LEDGER_FACTS_UPDATE_PROMPT.format(task=state["task"], facts=state["task_ledger"]["facts"]),
                source=ORCHESTRATOR_NAME,
            )
        )
        response = await model_client.create(get_compatible_context(model_client, context))
        facts = response.content
        context += [
            UserMessage(content=facts, source=ORCHESTRATOR_NAME),
            UserMessage(content=ORCHESTRATOR_TASK_LEDGER_PLAN_UPDATE_PROMPT.format(team=state["team_description"]), source=ORCHESTRATOR_NAME),
        ]
        response = await model_client.create(get_compatible_context(model_client, context))

        state = {**state, "task_ledger": {"facts": facts, "plan": response.content}}
        return {**state, "messages": [_ledger_message(state)]}

    async def _progress_ledger_pass(state: MagenticState) -> MagenticState:
        context = build_llm_context(state["messages"])
        if state.get("pending_reflection"):
            context.append(UserMessage(content=state["pending_reflection"], source=ORCHESTRATOR_NAME))
        context.append(
            UserMessage(
                content=ORCHESTRATOR_PROGRESS_LEDGER_PROMPT.format(
                    task=state["task"], team=state["team_description"], names=", ".join(state["participant_names"])
                ),
                source=ORCHESTRATOR_NAME,
            )
        )

        def validate(parsed):
            if len(state["participant_names"]) == 1:
                parsed["next_speaker"] = {"reason": "The team consists of only one agent.", "answer": state["participant_names"][0]}
            required = ["is_request_satisfied", "is_progress_being_made", "is_in_loop", "instruction_or_question", "next_speaker"]
            for key in required:
                entry = parsed.get(key)
                if not isinstance(entry, dict) or "answer" not in entry or "reason" not in entry:
                    return False, f'the key "{key}" was missing, or was not an object with both "reason" and "answer".', None
            if not parsed["is_request_satisfied"]["answer"] and parsed["next_speaker"]["answer"] not in state["participant_names"]:
                bad = parsed["next_speaker"]["answer"]
                return False, f'"next_speaker.answer" was {bad!r}, which does not match any team member name.', None
            return True, None, parsed

        ledger = await call_model_for_json(model_client, get_compatible_context, context, validate, ORCHESTRATOR_NAME)

        n_stalls = state.get("n_stalls", 0)
        if not ledger["is_progress_being_made"]["answer"] or ledger["is_in_loop"]["answer"]:
            n_stalls += 1
        else:
            n_stalls = max(0, n_stalls - 1)

        new_messages = list(state["messages"])
        if not ledger["is_request_satisfied"]["answer"]:
            new_messages.append({"source": ORCHESTRATOR_NAME, "content": ledger["instruction_or_question"]["answer"]})

        return {
            **state,
            "n_rounds": state.get("n_rounds", 0) + 1,
            "n_stalls": n_stalls,
            "progress_ledger": ledger,
            "is_satisfied": bool(ledger["is_request_satisfied"]["answer"]),
            "next_speaker": ledger["next_speaker"]["answer"],
            "instruction": ledger["instruction_or_question"]["answer"],
            "messages": new_messages,
        }

    async def _final_answer(state: MagenticState) -> MagenticState:
        context = build_llm_context(state["messages"])
        context.append(UserMessage(content=final_answer_prompt.format(task=state["task"]), source=ORCHESTRATOR_NAME))
        response = await model_client.create(get_compatible_context(model_client, context))
        reason = (
            "Max rounds reached."
            if state.get("n_rounds", 0) > state.get("max_rounds", max_rounds)
            else state.get("progress_ledger", {}).get("is_request_satisfied", {}).get("reason", "Task completed.")
        )
        return {
            **state,
            "final_answer": response.content,
            "termination_reason": reason,
            "messages": list(state["messages"]) + [{"source": ORCHESTRATOR_NAME, "content": response.content}],
        }

    def _past_limits(state: MagenticState) -> bool:
        return state["is_satisfied"] or state.get("n_rounds", 0) > state.get("max_rounds", max_rounds)

    async def orchestrator_node(state: MagenticState) -> MagenticState:
        if not state.get("messages"):
            state = await _bootstrap(state)

        for _ in range(MAX_REPLANS_PER_TURN):
            state = await _progress_ledger_pass(state)
            if _past_limits(state) or state["n_stalls"] < state.get("max_stalls", max_stalls):
                break
            state = await _replan(state)
        # Falls through with whatever the last pass decided, even if still
        # "stalled" after MAX_REPLANS_PER_TURN attempts, rather than
        # looping here forever -- see module docstring.

        if _past_limits(state):
            return await _final_answer(state)
        return {**state, "next_after_check": state["next_speaker"]}

    return orchestrator_node