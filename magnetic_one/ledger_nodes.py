"""
The orchestrator's own reasoning steps: building/updating the task ledger
(facts + plan), running the progress ledger each round, and writing the
final answer. This is the "Task Ledger" + "Orchestrator Agent" half of the
diagram; dispatching to worker agents and gating hand-offs lives in
dispatch_nodes.py instead.
"""

from __future__ import annotations

from typing import Any, Dict

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


def build_ledger_nodes(
    model_client: ChatCompletionClient,
    participant_names: list,
    participant_descriptions: Dict[str, str],
    max_rounds: int,
    max_stalls: int,
    final_answer_prompt: str,
) -> Dict[str, Any]:
    description = team_description(participant_names, [participant_descriptions[n] for n in participant_names])

    async def create_task_ledger(state: MagenticState) -> MagenticState:
        planning = [UserMessage(content=ORCHESTRATOR_TASK_LEDGER_FACTS_PROMPT.format(task=state["task"]), source=ORCHESTRATOR_NAME)]
        response = await model_client.create(get_compatible_context(model_client, planning))
        facts = response.content
        planning.append(UserMessage(content=facts, source=ORCHESTRATOR_NAME))
        planning.append(UserMessage(content=ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT.format(team=description), source=ORCHESTRATOR_NAME))
        response = await model_client.create(get_compatible_context(model_client, planning))
        return {
            **state,
            "team_description": description,
            "participant_names": participant_names,
            "facts": facts,
            "plan": response.content,
            "n_stalls": 0,
            "max_rounds": state.get("max_rounds", max_rounds),
            "max_stalls": state.get("max_stalls", max_stalls),
            "enable_gricean_check": state.get("enable_gricean_check", True),
            "adherence_history": state.get("adherence_history", []),
            "reflection_history": state.get("reflection_history", []),
        }

    async def init_outer_loop(state: MagenticState) -> MagenticState:
        ledger_message = {
            "source": ORCHESTRATOR_NAME,
            "content": ORCHESTRATOR_TASK_LEDGER_FULL_PROMPT.format(
                task=state["task"], team=state["team_description"], facts=state["facts"], plan=state["plan"]
            ),
        }
        return {**state, "messages": [ledger_message], "next_after_check": "progress_ledger"}

    async def progress_ledger(state: MagenticState) -> MagenticState:
        n_rounds = state.get("n_rounds", 0) + 1
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
            "n_rounds": n_rounds,
            "n_stalls": n_stalls,
            "progress_ledger": ledger,
            "is_satisfied": bool(ledger["is_request_satisfied"]["answer"]),
            "next_speaker": ledger["next_speaker"]["answer"],
            "instruction": ledger["instruction_or_question"]["answer"],
            "messages": new_messages,
            "next_after_check": "call_agent",
        }

    async def update_task_ledger(state: MagenticState) -> MagenticState:
        context = build_llm_context(state["messages"])
        context.append(UserMessage(content=ORCHESTRATOR_TASK_LEDGER_FACTS_UPDATE_PROMPT.format(task=state["task"], facts=state["facts"]), source=ORCHESTRATOR_NAME))
        response = await model_client.create(get_compatible_context(model_client, context))
        facts = response.content
        context.append(UserMessage(content=facts, source=ORCHESTRATOR_NAME))
        context.append(UserMessage(content=ORCHESTRATOR_TASK_LEDGER_PLAN_UPDATE_PROMPT.format(team=state["team_description"]), source=ORCHESTRATOR_NAME))
        response = await model_client.create(get_compatible_context(model_client, context))
        return {**state, "facts": facts, "plan": response.content}

    async def final_answer(state: MagenticState) -> MagenticState:
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

    return {
        "create_task_ledger": create_task_ledger,
        "init_outer_loop": init_outer_loop,
        "progress_ledger": progress_ledger,
        "update_task_ledger": update_task_ledger,
        "final_answer": final_answer,
    }