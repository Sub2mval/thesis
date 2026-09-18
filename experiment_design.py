"""
Policy layer ABOVE the canonical Trust_Allocator
(trust_allocator/legacy_trust_allocator.py).

The allocator itself only ever returns the ORIGINAL 3-way verdict --
"low" / "medium" / "high" -- and knows nothing about experiment designs.
Everything about what a given experiment design DOES with that verdict
(which delivery-time trust notice to attach, if any; whether to trigger
a reflection call) lives here instead, via `resolve_design()`, so the
allocator module never has to change as more designs are added.

Both `llm_debate` (langgraph_debate.py) and `magnetic_one`
(gricean_checker.py) call `resolve_design()` at the same point where
they previously ran their own scoring logic, then act on the returned
`DesignPolicy` uniformly.

Design 4 IS handled by this module, on equal footing with Designs 1-3:
it is the final pre-relabelling historical design, and it is resolved
here exactly the same way as the others -- callers score the message
with the canonical Trust_Allocator and then call resolve_design() for
Design 4 too. The old multi-axis Gricean-checker code path is no longer
reachable from any of Designs 1-4 (see llm_debate/langgraph_debate.py
and magnetic_one/gricean_checker.py).

Design 1 (implemented):
    The original 3-level trust system, unmodified: medium remains
    distinct from low, every one of high/medium/low produces its own
    corresponding delivery-time trust notice, and there is no
    reflection call.

Design 2 (implemented):
    Medium collapses into low: high still gets its own HIGH notice, but
    both medium and low verdicts now produce the LOW notice. No
    reflection.

Design 3 (implemented):
    High-trust suppression: a HIGH verdict now gets no notice at all
    (transparent delivery, identical to a baseline run for that
    message). Medium and low both still collapse to the LOW notice, as
    in Design 2. No reflection.

Design 4 (implemented):
    Design 3 plus one additional historical change: a HIGH verdict
    still gets no notice (transparent, as in Design 3), and medium/low
    still both collapse to the LOW notice -- but now a medium or low
    verdict ALSO triggers one private reflection for the receiving
    agent/node before it produces its response, on top of the LOW
    notice. This is the final pre-relabelling historical design; the
    canonical Trust_Allocator underneath it is unchanged from Designs
    1-3.
"""

from __future__ import annotations

from typing import TypedDict

# The allocator's own vocabulary (trust_allocator.legacy_trust_allocator.
# TRUST_LEVELS minus "undefined", which is a state default the allocator
# never itself returns -- see that module's allocate()).
_VALID_RAW_TRUST_LEVELS = ("low", "medium", "high")

# Experiment designs that route through this policy layer.
VALID_DESIGNS = ("1", "2", "3", "4")


class DesignPolicy(TypedDict):
    # Which TRUST_NOTICE_TEMPLATES key (from legacy_trust_allocator) to
    # attach at delivery time, or None for no notice at all.
    notice: "str | None"
    # Whether this verdict should additionally trigger a private
    # reflection call for the receiving agent/node.
    reflect: bool


def resolve_design(raw_trust_level: str, design: str) -> DesignPolicy:
    """Turn the allocator's raw "low"/"medium"/"high" verdict into a
    concrete delivery decision for the given experiment design.

    Raises:
        ValueError: `raw_trust_level` isn't one of the allocator's three
            real verdicts.
        NotImplementedError: `design` isn't implemented (not one of
            "1"/"2"/"3"/"4").
    """
    if raw_trust_level not in _VALID_RAW_TRUST_LEVELS:
        raise ValueError(
            f"resolve_design expects a raw 'low'/'medium'/'high' verdict from the "
            f"Trust_Allocator, got {raw_trust_level!r}"
        )

    if design == "1":
        # Design 1: original 3-level trust system, no reflection. Every
        # level gets its own corresponding notice; medium stays distinct
        # from low (no collapsing).
        return {"notice": raw_trust_level, "reflect": False}

    if design == "2":
        # Design 2: medium collapses into low. High still gets its own
        # HIGH notice; medium and low both produce the LOW notice. No
        # reflection.
        if raw_trust_level == "high":
            return {"notice": "high", "reflect": False}
        return {"notice": "low", "reflect": False}

    if design == "3":
        # Design 3: high-trust suppression. High gets no notice at all --
        # transparent, delivery-identical to a baseline message for that
        # verdict. Medium and low both still collapse to the LOW notice
        # (same collapse as Design 2). No reflection.
        if raw_trust_level == "high":
            return {"notice": None, "reflect": False}
        return {"notice": "low", "reflect": False}

    if design == "4":
        # Design 4: Design 3's notice policy (HIGH transparent, medium/low
        # both collapse to the LOW notice) PLUS one additional historical
        # change -- a medium or low verdict also triggers one private
        # reflection for the receiving agent/node before it responds.
        if raw_trust_level == "high":
            return {"notice": None, "reflect": False}
        return {"notice": "low", "reflect": True}

    raise NotImplementedError(
        f"resolve_design() does not handle design {design!r}. Implemented designs are "
        f"{VALID_DESIGNS!r}."
    )
