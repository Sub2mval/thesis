"""
Select which GAIA validation questions a benchmark run should cover:
either a random subset of size n, an explicit list of task_ids, or the
full pool (all 165 questions, for the default GAIA validation split)
when neither is given.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional


def select_questions(
    questions: List[Dict[str, Any]],
    n: Optional[int] = None,
    task_ids: Optional[List[str]] = None,
    seed: int = 42,
) -> List[Dict[str, Any]]:
    """questions: the full pool, as returned by gaia_utils.load_gaia_questions().
    n: take a random sample of this size (mutually exclusive with task_ids).
    task_ids: take exactly these questions, in the order given; raises if
        any id is missing from `questions` -- fail loudly rather than
        silently running fewer questions than requested.
    seed: RNG seed for the random subset, so a given `--n` is reproducible
        across runs (and comparable between the two MAS's).
    Returns all `questions` unchanged if neither n nor task_ids is given.
    """
    if n is not None and task_ids is not None:
        raise ValueError("Pass either n or task_ids, not both.")

    if task_ids is not None:
        by_id = {q["task_id"]: q for q in questions}
        missing = [tid for tid in task_ids if tid not in by_id]
        if missing:
            raise ValueError(f"task_id(s) not found in the loaded split: {missing}")
        return [by_id[tid] for tid in task_ids]

    if n is not None:
        if n > len(questions):
            raise ValueError(f"Requested n={n} but only {len(questions)} questions are available.")
        rng = random.Random(seed)
        return rng.sample(questions, n)

    return list(questions)
