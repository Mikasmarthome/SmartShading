"""Shading state enum and priority model. See ARCHITECTURE.md §4.1.

Only the nine current states are defined here. MORNING_OPEN is documented in
ARCHITECTURE.md §4.1 as a tracking-only label for a lifecycle transition,
not a real ShadingState, and is therefore intentionally not a member of
this enum.

Priority uses a gap-based scheme (ranks 1–9 for Tier-1 Safety, then
multiples of 10) so that future Tier-1 states can be inserted without
renumbering any existing entry.

Tier 1 currently contains STORM_SAFE and WIND_SAFE only.
Frost Protection is intentionally excluded: its correct action
(retract vs. close vs. no-op) depends on cover type, which the current
model (CoverCapability) does not expose semantically.  It will be added
once a Cover-Type model exists in WindowConfig / CoverCapability.
"""
from __future__ import annotations

from enum import Enum


class ShadingState(Enum):
    """The nine approved shading states (ARCHITECTURE.md §4.1).

    Tier 1 Safety cluster (ranks 1–9):
      STORM_SAFE — always active; structural storm damage affects all exterior covers.
      WIND_SAFE  — opt-in (default off); wind damage risk is cover-type-dependent.
      Ranks 3–9 are reserved for future Tier-1 states (e.g. Frost Protection once
      a Cover-Type model distinguishes awnings from roller shutters).
    """

    STORM_SAFE      = "storm_safe"
    WIND_SAFE       = "wind_safe"
    MANUAL_OVERRIDE = "manual_override"
    NIGHT_CLOSED    = "night_closed"
    ABSENCE_CLOSED  = "absence_closed"
    STRONG_SHADE    = "strong_shade"
    NORMAL_SHADE    = "normal_shade"
    LIGHT_SHADE     = "light_shade"
    OPEN            = "open"


# Priority rank per ARCHITECTURE.md §4.1 — 1 is the highest priority.
# Lower number always wins: a state with a lower rank may always escalate
# over a state with a higher rank (see state_machine.transitions).
#
# Gap-based scheme: Tier-1 Safety occupies ranks 1–9 (1 and 2 in use),
# leaving ranks 3–9 free for future Tier-1 states without renumbering any
# existing entry.  All other tiers use multiples of 10.
STATE_PRIORITY: dict[ShadingState, int] = {
    # Tier 1 — Safety Guards (ranks 1–9 reserved; 3–9 available for future Tier-1)
    ShadingState.STORM_SAFE:      1,
    ShadingState.WIND_SAFE:       2,
    # Tier 2 — Manual Override
    ShadingState.MANUAL_OVERRIDE: 10,
    # Tier 3 — Lifecycle
    ShadingState.NIGHT_CLOSED:    20,
    # Tier 4 — Protection Floors
    ShadingState.ABSENCE_CLOSED:  30,
    ShadingState.STRONG_SHADE:    40,
    ShadingState.NORMAL_SHADE:    50,
    ShadingState.LIGHT_SHADE:     60,
    # Tier 5 / Fallback
    ShadingState.OPEN:            70,
}


def priority(state: ShadingState) -> int:
    """Return the priority rank of a state (1 = highest priority)."""
    return STATE_PRIORITY[state]


def is_higher_priority(state_a: ShadingState, state_b: ShadingState) -> bool:
    """True if state_a outranks state_b (lower rank number = higher priority)."""
    return priority(state_a) < priority(state_b)
