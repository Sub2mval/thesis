# Thesis: Can adherence to Gricean Maxims improve MAS Resilience

## Problem: 
 - MAS are susceptible to errors.
 - These errors reduce MAS performance

## Setup
 - Systems which maintain performance in the presence of errors: aka Resilient MAS
 - We can test the resilience of MAS by introducing errors

## Insight
 - LLM-MAS can be represented as a conversation between agents towards a goal. Therefore, it should follow the Cooperative Principle.
 - Thus, errors represent a violation of the four maxims of Cooperative Principle
 - Indeed, all 14 errors catalogued in MAST represent a violation of atleast one Gricen principle
 - Thus a Gricean Adherence Checker should be able to catch most introduced errors.

## Solution
 - A Gricean Checker checks each message in MAS message history for adherence to gricean maxims.
 - Non adhering messages get flagged.
 - Next agent is prompted to account for the mistake.

## Experiment
 - 2 MAS (based on LLM-Debate and Magnetic One) solve 165 questions of GAIA validation set
 - Each MAS is run with and without Gricean Checker with same LLM (temp = 0 and fixed seed to ensure identical trace outputs).
 - Whenever Gricean adherence is not maintained, subsequent agent gets a reflection loop to address the situation.
 - Initial accuracy for both versions of MAS is recorded.
 - Perturbations based on the MAST topology are introduced in the trace at points where the previous message history is identical.
 - Subsequently, accuracy is measured again.
 - More resilient MAS should have maintained its accuracy significantly more
 
