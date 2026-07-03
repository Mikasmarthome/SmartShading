"""Transition rules: escalation, de-escalation, and guard-bypass logic.

Implements the corrected Decision Engine guard rule from ARCHITECTURE.md
§5.7 (P0-1 audit fix) together with the explicit "direct, ungated"
transitions from §4.4 and the MANUAL_OVERRIDE / STORM_SAFE exit carve-outs
from §4.5 / §4.6 / §4.3.

This module only decides *whether a guard should even be consulted* for a
given (current, proposed) pair. It does not itself decide *what* the
proposed state is - that is StateMachine.evaluate()'s job (not yet
implemented, deferred to the Comfort/Forecast/Lifecycle Engine phase).
"""
from __future__ import annotations

from .states import ShadingState, priority

# ARCHITECTURE.md §4.4 "Direkt (ohne Deeskalation)": lifecycle- or
# presence-driven exits that bypass the normal hysteresis/guard check even
# though they are priority-decreasing (de-escalations by rank).
LIFECYCLE_DIRECT_TRANSITIONS: frozenset[tuple[ShadingState, ShadingState]] = frozenset(
    {
        (ShadingState.NIGHT_CLOSED, ShadingState.OPEN),
        (ShadingState.ABSENCE_CLOSED, ShadingState.OPEN),
    }
)


def is_escalation(current: ShadingState, proposed: ShadingState) -> bool:
    """True if proposed has strictly higher priority than current (§5.7, rule 3)."""
    return priority(proposed) < priority(current)


def is_deescalation(current: ShadingState, proposed: ShadingState) -> bool:
    """True if proposed has strictly lower priority than current (§5.7, rule 4)."""
    return priority(proposed) > priority(current)


def bypasses_guard(current: ShadingState, proposed: ShadingState) -> bool:
    """True if the (current -> proposed) transition must never be gated by
    StateGuard.is_locked(), regardless of minimum_state_duration/hysteresis.

    Covers, in order:
    1. No-op (proposed == current) - trivially nothing to gate.
    2. Escalation (§5.7 rule 3) - always allowed, this is what makes
       STORM_SAFE and MANUAL_OVERRIDE detection un-blockable (§5.7 rules
       5-6), since both sit at the top of the priority order (§4.1).
    3. Lifecycle-driven direct exits (§4.4): NIGHT_CLOSED -> OPEN and
       ABSENCE_CLOSED -> OPEN are driven by the Lifecycle/presence signal
       itself, not by hysteresis, and must not be re-delayed by
       minimum_state_duration.
    4. Ending MANUAL_OVERRIDE (§4.5 clarification): a timeout or explicit
       user reset must not be re-delayed by minimum_state_duration - that
       would silently re-extend an override that has already ended.
    5. Leaving a Tier-1 Safety state — STORM_SAFE (§4.6) or WIND_SAFE: the
       respective clear-hysteresis already gates *when* the evaluator proposes
       leaving the safety state; once proposed, the return to the previous
       state must not be additionally delayed by minimum_state_duration.
    6. Night-Contact Option B ventilation (NIGHT_CLOSED <-> NIGHT_VENT): both
       directions are driven directly by the window contact sensor (a real,
       user-caused open/close), not by hysteresis. NIGHT_VENT -> NIGHT_CLOSED
       is already an escalation (rule 2) and bypasses on its own; NIGHT_CLOSED
       -> NIGHT_VENT is a de-escalation by rank and would otherwise be gated
       by minimum_state_duration, silently dropping a repeated open shortly
       after a completed return-to-night. Listed explicitly for symmetry with
       the reverse direction and so a future minimum_state_duration entry for
       NIGHT_CLOSED can never re-introduce this gap.
    """
    if proposed == current:
        return True
    if is_escalation(current, proposed):
        return True
    if (current, proposed) in LIFECYCLE_DIRECT_TRANSITIONS:
        return True
    if current is ShadingState.MANUAL_OVERRIDE:
        return True
    if current in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):
        return True
    if (
        current is ShadingState.NIGHT_CLOSED
        and proposed is ShadingState.NIGHT_VENT
    ):
        return True
    return False
