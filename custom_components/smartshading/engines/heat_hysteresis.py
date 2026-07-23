"""Heat protection release hysteresis — pure decision function (v1.2.0-beta.1, T9).

Single source of truth for "is heat protection needed this cycle", given the
current temperature readings, the configured entry thresholds, a single
symmetric hysteresis margin, and whether heat protection was already active
on the prior cycle.

Called from two places with identical inputs (by design — see module
docstring of coordinator.py's heat-hysteresis wiring):
  1. The Coordinator, once per window per cycle, to persist the resulting
     `active` flag for the NEXT cycle's `previously_active` input, and to
     populate WindowDecisionInput.heat_previously_active for HeatEvaluator.
  2. HeatEvaluator.evaluate(), which calls this function again with the same
     inputs to decide its own candidate — a cheap, side-effect-free
     recomputation, not a second implementation of the logic.

Semantics (entry inclusive-high, exit exclusive-low — a standard, explicit
hysteresis convention):
  - Entry: outdoor_temp_c >= outdoor_entry_c OR indoor_temp_c >= indoor_entry_c
    (either signal alone is sufficient to need protection — unchanged from
    the pre-T9 OR logic).
  - Exit: only once EVERY enabled signal with a current reading has dropped
    BELOW (strictly less than) its own (entry - hysteresis_c) exit bound. A
    signal that is disabled (entry threshold None) is treated as "already
    below exit" for this purpose (it never blocks a release). A signal that
    IS enabled but has no current reading (sensor unavailable) is treated as
    "still above exit" (fail-safe hold — missing data must never be used to
    release an active protection, consistent with SafetyHold's
    sensor_unavailable-extends-the-hold precedent for Wind/Storm/Rain).
  - hysteresis_c = 0.0 collapses entry and exit to the same value, exactly
    reproducing the pre-T9 flat-threshold comparison (a full opt-out).
  - Missing data while NOT previously active never triggers entry (unchanged
    pre-T9 fail-safe: "do not trigger from missing data").
"""
from __future__ import annotations

from dataclasses import dataclass

# Reason codes surfaced in diagnostics / support export.
REASON_DISABLED = "disabled"                # both thresholds None
REASON_INSUFFICIENT_DATA = "insufficient_data"  # no reading for any enabled signal, not active
REASON_HELD_MISSING_DATA = "held_missing_data"  # no reading for any enabled signal, was active
REASON_NOT_NEEDED = "not_needed"             # not active, entry not met
REASON_ENTERED = "entered"                   # was not active, entry met this cycle
REASON_HELD_BY_HYSTERESIS = "held_by_hysteresis"  # was active, exit not (yet) met
REASON_EXITED = "exited"                     # was active, exit met this cycle


@dataclass(frozen=True)
class HeatHysteresisResult:
    active: bool
    reason: str


def resolve_heat_needed(
    *,
    outdoor_temp_c: float | None,
    indoor_temp_c: float | None,
    outdoor_entry_c: float | None,
    indoor_entry_c: float | None,
    hysteresis_c: float,
    previously_active: bool,
) -> HeatHysteresisResult:
    """Return whether heat protection is needed this cycle, with a reason code.

    Pure function: no I/O, no state, no side effects. See module docstring
    for the full entry/exit hysteresis semantics.
    """
    outdoor_enabled = outdoor_entry_c is not None
    indoor_enabled = indoor_entry_c is not None
    if not outdoor_enabled and not indoor_enabled:
        return HeatHysteresisResult(active=False, reason=REASON_DISABLED)

    outdoor_available = outdoor_enabled and outdoor_temp_c is not None
    indoor_available = indoor_enabled and indoor_temp_c is not None

    if not outdoor_available and not indoor_available:
        if previously_active:
            return HeatHysteresisResult(active=True, reason=REASON_HELD_MISSING_DATA)
        return HeatHysteresisResult(active=False, reason=REASON_INSUFFICIENT_DATA)

    outdoor_entry_met = outdoor_available and outdoor_temp_c >= outdoor_entry_c
    indoor_entry_met = indoor_available and indoor_temp_c >= indoor_entry_c
    entry_met = outdoor_entry_met or indoor_entry_met

    if not previously_active:
        if entry_met:
            return HeatHysteresisResult(active=True, reason=REASON_ENTERED)
        return HeatHysteresisResult(active=False, reason=REASON_NOT_NEEDED)

    # previously_active is True: stays active unless every enabled signal
    # with a current reading has dropped strictly below its exit bound.
    outdoor_below_exit = (
        not outdoor_enabled
        or (outdoor_available and outdoor_temp_c < (outdoor_entry_c - hysteresis_c))
    )
    indoor_below_exit = (
        not indoor_enabled
        or (indoor_available and indoor_temp_c < (indoor_entry_c - hysteresis_c))
    )
    if outdoor_below_exit and indoor_below_exit:
        return HeatHysteresisResult(active=False, reason=REASON_EXITED)
    return HeatHysteresisResult(active=True, reason=REASON_HELD_BY_HYSTERESIS)
