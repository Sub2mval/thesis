"""
Resolves the error-injection CLI options into a concrete list of
(error_type, fm_id) pairs, one entry per fork to run against a trace.
Mirrors error_injection.py's FAILURE_MODES hierarchy: 3 families / 14
fine-grained types nested under them.

Three ways to call resolve_error_plan():
  - nothing given              -> one fork per family (3 total), each
                                   injecting a random fine-grained type
                                   from that family (fm_id=None lets
                                   choose_failure_mode() pick randomly,
                                   independently, at injection time).
                                   This is the "3 errors per run, drawn
                                   from the 3 families" default.
  - family="inter_agent_misalignment"
                                -> one fork only, from that family
                                   (random fine-grained type within it).
  - fm_id="FM-2.3"              -> one fork only, that exact fine-grained
                                   type; its family is looked up
                                   automatically.
`family` and `fm_id` are mutually exclusive.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple


from llm_debate.error_injection import ERROR_TYPES, FAILURE_MODES  # noqa: E402


def _family_of(fm_id: str) -> str:
    for family, modes in FAILURE_MODES.items():
        if any(m["id"] == fm_id for m in modes):
            return family
    valid = [m["id"] for modes in FAILURE_MODES.values() for m in modes]
    raise ValueError(f"Unknown fm_id '{fm_id}'. Must be one of {valid}.")


def resolve_error_plan(
    family: Optional[str] = None,
    fm_id: Optional[str] = None,
) -> List[Tuple[str, Optional[str]]]:
    """Returns [(error_type, fm_id_or_None), ...] -- one entry per fork."""
    if family is not None and fm_id is not None:
        raise ValueError("Pass either family or fm_id, not both.")

    if fm_id is not None:
        return [(_family_of(fm_id), fm_id)]

    if family is not None:
        if family not in ERROR_TYPES:
            raise ValueError(f"Unknown family '{family}'. Must be one of {ERROR_TYPES}.")
        return [(family, None)]

    # Default: one fork per family (3 total), fm_id=None so each fork's
    # own choose_failure_mode() call picks a random fine-grained type
    # from within that family independently.
    return [(fam, None) for fam in ERROR_TYPES]
