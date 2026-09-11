# Thesis: Can Adherence to Gricean Maxims Improve MAS Resilience?

This repository contains the implementation and experiment harness for my Master's thesis on whether monitoring agent-agent communication for violations of the Gricean Cooperative Principle can make Multi-Agent Systems (MAS) more resilient to errors.

## Problem

Multi-Agent Systems are groups of agents working together to achieve some goal. In an LLM-based MAS, much of this coordination happens through messages passed between agents. When one agent produces a bad message, later agents may treat it as a reliable premise and build on it, causing an error to propagate through the system.

The question I am interested in is therefore not simply whether an individual agent can make an error. It is whether the MAS can detect and recover from an error once it enters the conversation.

A resilient MAS should maintain its ability to solve the task even when its communication is perturbed by an error.

## Hypothesis 1: Gricean violations are a proxy for live MAS error detection

Agents communicating toward a shared task should follow the Cooperative Principle. In particular, their contributions should satisfy the four Gricean Maxims:

* **Quality:** provide information that is supported by evidence and avoid false or misleading claims.
* **Quantity:** provide the information required by the task without unnecessarily polluting the context.
* **Relation:** remain relevant to the current task and role.
* **Manner:** communicate clearly, orderly, and in a form the receiving agent can act on.

This gives a useful way of thinking about MAS failures: a message which is wrong, irrelevant, misleading, ambiguous, or otherwise unhelpful to the current exchange should often manifest as a violation of one or more of these maxims.

For example, one MAST failure consists of agents solving the wrong mathematical problem. The message from the first agent introduces an irrelevant worked example, and the receiving agent then follows that tangent and produces an answer to the wrong question. The first message violates **Quantity** by supplying irrelevant information, while the next agent violates **Relation** by treating that irrelevant material as the problem it needs to solve.

The important point is that the Gricean checker is not being asked to predict whether a final answer is correct. It evaluates the communication that is happening during the MAS run and asks whether the receiving agent can safely use the latest message as a premise for its own reasoning.

This leads to my first hypothesis:

> **H1: Violations of the Cooperative Principle, expressed as non-adherence to Gricean Maxims, are a useful proxy for live error detection in Multi-Agent Systems.**

The checker scores every message on Quality, Quantity, Relation, and Manner from 1-5. The mean score is converted deterministically into an adherence level: `HIGH` when the mean is at least 4.5, otherwise `NOT_HIGH` for the intervention logic. The LLM does not directly choose the final adherence label.

The checker also includes several cases which should *not* count as communication errors. Truthfully reporting a runtime failure is high-quality information. Explicitly calibrated uncertainty is high-quality information. A devil's-advocate agent can legitimately disagree with another agent when disagreement is its assigned role. Machine-readable data can also be highly adherent even when it would be awkward for a human reader.

## From detection to resilience

Detecting a bad message is only useful if the MAS does something with that information. The experiment therefore tests a second question: whether feeding a detected violation back into the receiving agent through a reflection step actually improves resilience.

The current design has two important properties.

### 1. The checker does not change the trajectory before an error is detected

An earlier version of the experiment passed the checker's result to agents on every message. That meant the checker-on and checker-off runs could diverge even when every message was perfectly adherent. An injected error could then occur in two different conversational states, making the comparison difficult to interpret.

The current implementation fixes this.

A `HIGH` result is a true no-op. In the LLM-Debate implementation, `wrap_with_adherence_notice()` has no `HIGH` notice, and the normal message is therefore passed through unchanged. The checker can still record the score for analysis, but it does not alter the conversation.

As a result, the checker-on and checker-off traces remain identical until the first `NOT_HIGH` message. This gives a common checkpoint from which the same perturbation can be introduced into both runs.

### 2. A detected violation triggers a reflection loop

When the checker produces `NOT_HIGH`, the receiving agent is given the checker's reasoning and asked to reflect on the flagged message before producing its response.

The reflection is specifically intended to repair the communication problem that was detected. For example, the reflection prompt can tell an agent to question an ambiguous message, avoid building on an unsupported claim, recover missing information, or ignore an irrelevant tangent and return to the task.

The reflection itself is private. It is used once to shape the receiving agent's next response and is recorded for later analysis rather than becoming another shared conversational message.

The goal is therefore to test whether this intervention changes the effect of an injected error on the final task result.

## Experimental setup

The benchmark uses the 165 questions in the GAIA validation set and runs them through two different MAS architectures:

1. **LLM-Debate**: a LangGraph implementation of multi-agent debate, where agents iteratively exchange their current answers.
2. **Magentic-One**: a LangGraph implementation of Magentic-One, with an orchestrator coordinating FileSurfer, WebSurfer, Coder, and ComputerTerminal workers.

For each system, the runner can execute both a baseline configuration and a Gricean-intervention configuration. The same underlying model can be used for both, with temperature set to `0` and a fixed seed in the benchmark runner.

The main benchmark entry point is:

```bash
python run_gaia_benchmark.py --n 20 --systems both
```

By default, the full benchmark can be run with:

```bash
python run_gaia_benchmark.py
```

The runner can also select particular GAIA questions, restrict execution to one MAS architecture, or target a specific injected failure mode.

## Error injection

Resilience is tested by deliberately corrupting a message in an otherwise valid MAS trace.

The error-injection harness uses the MAST-style taxonomy implemented in `llm_debate/error_injection.py`. The 14 fine-grained failure modes are grouped into three families:

* **Specification issues (FM-1.x)**: task specification deviation, role specification deviation, redundant steps, removed conversation history, and removed termination conditions.
* **Inter-agent misalignment (FM-2.x)**: repeating handled tasks, ambiguity, goal drift, withholding, ignoring other agents, and inconsistent reasoning.
* **Task verification (FM-3.x)**: premature termination, removal of verification, and incorrect verification.

For example, `FM-2.3` corresponds to *Deviate from main goal (drift)*.

The corruption is generated from the original message, the task, the preceding conversation, and the selected failure-mode instruction. The resulting message is required to remain plausible while making the response incorrect or misleading.

The default error plan runs one fork from each of the three families. A specific failure can also be selected, for example:

```bash
python run_gaia_benchmark.py --error-type FM-2.3 --systems debate
```

## Paired-fork experiment

The key experimental comparison is a paired fork.

First, the checker-off and checker-on versions of the MAS are run until there is a checkpoint where their message histories are byte-identical. The current checker design is intended to preserve this shared prefix until the first `NOT_HIGH` intervention.

Second, one message at the selected checkpoint is corrupted.

Third, **the exact same corrupted message is inserted into both traces** and each MAS is allowed to continue to completion.

This matters because the comparison is then between two continuations of the same state:

```text
                    identical MAS state
                           |
                    inject same error
                       /       \
              checker OFF   checker ON
                    |             |
              continue       detect/reflection
                    |             |
                 result          result
```

Any difference in how well the two runs recover can therefore be attributed much more directly to the Gricean intervention rather than to the error simply being inserted into different conversational states.

The implementation in `llm_debate/error_injection.py` explicitly generates one corruption and reuses it for both sides of the paired fork.

## What is being measured

For the unperturbed runs, the benchmark records the final answer and whether it is correct according to the GAIA ground truth.

For each injected-error fork, it records the same final correctness measure together with the injected MAST failure mode, injection location, original message, corrupted message, and token usage.

The main resilience comparison is therefore the change in accuracy caused by the injected error:

```text
baseline accuracy  ->  accuracy after injected error
```

The central comparison is whether this degradation is smaller when the Gricean intervention is enabled.

In other words, the question is not whether the checker makes every run more accurate in the absence of errors. The question is whether, once the same error is introduced, the checker-enabled MAS preserves more of its original task performance.

## Architecture

At a high level, the LLM-Debate graph is:

```text
Question
   |
   v
Agent 1 <----> Agent 2 <----> Agent 3
   |                               |
   +----------- debate ------------+
                 |
              Gricean
              Checker
                 |
              Aggregate
                 |
               Answer
```

The actual LangGraph implementation keeps separate message contexts for the agents. After each agent turn, `gricean_check()` evaluates the latest message. The checker always records the assessment, but when `use_gricean_check` is disabled the assessment is not surfaced to the agents and no reflection is triggered.

When the checker is enabled and the result is `NOT_HIGH`, the receiving agent enters the private reflection step before generating its next response.

The Magentic-One graph places the checker explicitly on the communication edges:

```text
                     Question
                        |
                        v
              MagenticOneOrchestrator
                        |
                        v
                 Gricean_Checker
                        |
          +-------------+-------------+
          |       |          |        |
          v       v          v        v
      FileSurfer WebSurfer  Coder  ComputerTerminal
          |       |          |        |
          +-------+----------+--------+
                        |
                        v
                 Gricean_Checker
                        |
                        v
              MagenticOneOrchestrator
                        |
                      Answer
```

Every worker hand-off passes through the checker, which scores the latest message and, when necessary, creates the reflection for the receiving agent. As in the debate system, scoring and intervention are separate: the checker can always record an adherence assessment, while `enable_gricean_check` controls whether a non-HIGH assessment produces a reflection.

## Repository structure

```text
.
├── gaia_runner/
│   ├── cli.py                 # GAIA benchmark entry point
│   ├── debate_system.py       # LLM-Debate benchmark adapter
│   ├── magnetic_system.py     # Magentic-One benchmark adapter
│   ├── error_spec.py          # Maps CLI options to injected errors
│   ├── question_select.py     # GAIA question selection
│   ├── trace_io.py            # Trace and summary output
│   └── token_usage.py         # Token accounting
│
├── llm_debate/
│   ├── langgraph_debate.py    # LLM-Debate MAS
│   ├── Gricean_check.py       # Gricean scoring and intervention logic
│   ├── error_injection.py     # Paired-fork and MAST-style corruption
│   └── gaia_utils.py          # GAIA loading/scoring helpers
│
├── magnetic_one/
│   ├── magnetic_one_langgraph.py  # Magentic-One wrapper
│   ├── orchestrator_graph.py      # Magentic-One LangGraph topology
│   ├── gricean_check.py           # Gricean rubric and reflection prompts
│   ├── gricean_checker.py         # Checker graph node
│   └── ...                         # Agents, orchestration, state, clients
│
├── run_gaia_benchmark.py       # Convenience entry point
└── requirements.txt
```

## Outputs

Runs are written under the configured output directory, by default `gaia_runs/output/`.

Each question gets baseline traces for checker-off and checker-on runs, plus the requested fork traces. A `summary.jsonl` file is also appended as traces finish so that a long benchmark run can be inspected without opening every individual trace.

A typical layout is:

```text
gaia_runs/output/
├── <task_id>/
│   ├── llm_debate__baseline_off.json
│   ├── llm_debate__baseline_on.json
│   ├── llm_debate__fork0_FM-1.x_off.json
│   ├── llm_debate__fork0_FM-1.x_on.json
│   └── ...
└── summary.jsonl
```

The traces include the final answer, correctness, error metadata where applicable, and token statistics.

## Current research question

The project is ultimately testing two connected claims:

1. **Detection:** Gricean non-adherence can act as a useful online signal that something has gone wrong in an MAS conversation.
2. **Resilience:** giving a receiving agent a targeted reflection based on that signal helps the MAS recover from injected errors and retain more of its original task performance.

The first claim is validated by comparing the Gricean assessment of injected erroneous messages against the messages they replace. The second is evaluated through the paired checker-off/checker-on error-injection experiments described above.
