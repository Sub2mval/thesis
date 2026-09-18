"""
Canonical Trust_Allocator package.

`legacy_trust_allocator.py` is the single source of truth for the
original 3-way trust prompt/schema/labels/notices, ported EXACTLY from
the archived `trust-based-resilience-new-method/Final venv/magnetic_one/
trust.py`. Both `llm_debate` and `magnetic_one` call into it via
`allocate()` so there is exactly one trust-scoring implementation in
this repository, not two independent ones.

The allocator itself has no notion of experiment designs 1-4 -- that
policy layer lives one level up, in the repo-root `experiment_design.py`.
"""
