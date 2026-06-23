"""Outcome resolution logic — Phase 9F4b-2.

Converts a PendingOutcome + OutcomeResolutionInput into a fully resolved
DecisionOutcome. Pure function — no HA dependencies, no queue, no coordinator.

Architecture:
    PendingOutcome  +  OutcomeResolutionInput
                          ↓
                    resolve_outcome()
                          ↓
                    DecisionOutcome  →  LearningStore (9F4a)

Resolution status rules:
    TIMEOUT  + temp available  → "complete"
    TIMEOUT  + no temp         → "partial_no_temp"
    any other trigger          → "partial_early_exit"

Score model  [-1.0 … +1.0]:
    Override:       dominant negative signal; range [-1.0, -0.5].
                    Shorter delay = stronger negative (user corrected quickly).
    Stability:      +0.30 if TIMEOUT with no override (accepted for full window).
    Thermal hold:   +0.15 bonus for shading states where indoor temp stayed at
                    or below the decision temperature (delta ≤ 0.5 °C or
                    no sensor). Rewards keeping the room from heating under load.
    Temperature:    ±0.30 for shading states only; positive = cooling (delta < 0).
                    No temp sensor → 0.0 for this component.
    Open overheat:  −0.05 per °C above 3 °C for OPEN decisions where the room
                    heated substantially (cap −0.10). Targets the blind spot
                    where leaving the window open caused significant overheating.
    Outdoor heat:   Rising indoor temp penalty is scaled down linearly when
                    outdoor temp ≥ 32 °C (full mitigation at ≥ 40 °C). Only
                    applies to the temperature component, not to thermal hold.
    Clamp:          Score is always clamped to [-1.0, +1.0].

Maximum achievable scores (non-override path):
    Shading + −3 °C cooling (sensor):   +0.30 + 0.15 + 0.30 = +0.75 (excellent)
    Shading + −1.5 °C cooling (sensor): +0.30 + 0.15 + 0.15 = +0.60 (excellent boundary)
    Shading + stable 0 °C (sensor):     +0.30 + 0.15 + 0.00 = +0.45 (good, confirmed hold)
    Shading + no sensor:                +0.30 + 0.00 + 0.00 = +0.30 (good, hold unconfirmed)
    Shading + +1 °C rise:               +0.30 + 0.00 − 0.10 = +0.20 (acceptable)
    Shading + +3 °C rise (normal):      +0.30 + 0.00 − 0.30 = +0.00 (just acceptable)
    Shading + +3 °C, 40 °C outdoor:     +0.30 + 0.00 + 0.00 = +0.30 (good, heat mitigated)
    OPEN + comfortable:                 +0.30 + 0.00 + 0.00 = +0.30 (good)
    OPEN + +4 °C overheating:           +0.30 + 0.00 − 0.05 = +0.25 (acceptable)

Note on evaluator-specific scoring (NightEvaluator, AbsenceEvaluator, etc.):
    Per architecture confirmation, these will be handled separately in a later
    step. This resolution function applies the same score model uniformly —
    evaluator-specific weighting belongs in the Learning Engine (9F5+).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..models.learning import DecisionOutcome
from ..models.pending_outcome import PendingOutcome
from ..state_machine.states import ShadingState


# ---------------------------------------------------------------------------
# Trigger model
# ---------------------------------------------------------------------------

class OutcomeResolutionTrigger(Enum):
    """Cause of an outcome resolution. No string literals in runtime code."""

    TIMEOUT = "timeout"           # Observation window elapsed without interruption
    OVERRIDE = "override"         # User issued a manual override (started / renewed)
    STATE_CHANGE = "state_change" # Evaluator produced a new shading decision
    LIFECYCLE = "lifecycle"       # Lifecycle transition (night / morning)
    SAFETY = "safety"             # Safety evaluator intervened (storm / wind)


# ---------------------------------------------------------------------------
# Resolution input
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OutcomeResolutionInput:
    """All information needed to resolve a PendingOutcome into a DecisionOutcome.

    Fields that are conditional on the trigger:
      override_delay_min   — set only for OVERRIDE; None otherwise
      override_event_type  — "started" or "renewed"; None otherwise
      indoor_temp_outcome_c — set when a temperature sensor was readable at
                              resolution time; None if no sensor or not yet
                              within the configured delay window

    override_occurred and escalation_occurred are not fields here because they
    follow deterministically from the trigger:
        override_occurred   = trigger == OVERRIDE
        escalation_occurred = trigger == SAFETY
    Keeping them out of the input removes a redundancy error surface.
    """

    trigger: OutcomeResolutionTrigger
    resolution_timestamp: datetime
    indoor_temp_outcome_c: float | None = None
    override_delay_min: float | None = None     # minutes between decision and override
    override_event_type: str | None = None      # "started" or "renewed"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Shading states for which a temperature delta produces a meaningful signal.
# For these states, indoor cooling (delta < 0) is a positive outcome signal.
# OPEN is excluded from the standard temperature component: thermal intent is
# ambiguous. The _open_heat_component() handles extreme overheating for OPEN.
_SHADING_STATES_FOR_TEMP: frozenset[ShadingState] = frozenset({
    ShadingState.NORMAL_SHADE,
    ShadingState.STRONG_SHADE,
    ShadingState.LIGHT_SHADE,
    ShadingState.NIGHT_CLOSED,
    ShadingState.ABSENCE_CLOSED,
})

# Shading states eligible for the thermal-hold bonus.  Intentionally excludes
# NIGHT_CLOSED and ABSENCE_CLOSED: those states serve privacy/schedule/security
# goals, not active heat protection.  Granting a bonus for stable room temperature
# during a night/absence timeout would reward thermally irrelevant shading and
# degrade learning quality for heat-protection decisions.
_THERMAL_HOLD_STATES: frozenset[ShadingState] = frozenset({
    ShadingState.NORMAL_SHADE,
    ShadingState.STRONG_SHADE,
    ShadingState.LIGHT_SHADE,
})

# Stability bonus for a TIMEOUT outcome (no override) — the user accepted the
# shading decision for the full observation window.
_STABILITY_BONUS: float = 0.30

# Thermal-hold bonus for shading states where the room stayed at or near the
# decision temperature. Rewards successful active shading even when no cooling
# is measured — important for rooms already at comfort temperature under load.
_THERMAL_HOLD_BONUS: float = 0.15
_THERMAL_HOLD_MAX_DELTA_C: float = 0.5  # room considered "held" if delta ≤ this

# Outdoor heat context: above _OUTDOOR_HEAT_PENALTY_START_C, a moderate indoor
# temperature rise is expected and acceptable even with optimal shading. The
# penalty scales linearly to zero at _OUTDOOR_HEAT_PENALTY_ZERO_C.
# Only applied to the temperature component, not to the thermal-hold bonus.
_OUTDOOR_HEAT_PENALTY_START_C: float = 32.0
_OUTDOOR_HEAT_PENALTY_ZERO_C: float = 40.0

# OPEN state overheating: conservative negative signal when leaving the window
# open resulted in a significant indoor temperature rise.
_OPEN_OVERHEAT_THRESHOLD_C: float = 3.0   # rises above this trigger the penalty
_OPEN_OVERHEAT_PENALTY_PER_C: float = 0.05  # per degree above threshold
_OPEN_OVERHEAT_MAX_PENALTY: float = 0.10   # cap


def _resolution_status(
    trigger: OutcomeResolutionTrigger,
    indoor_temp_outcome_c: float | None,
) -> str:
    if trigger == OutcomeResolutionTrigger.TIMEOUT:
        return "complete" if indoor_temp_outcome_c is not None else "partial_no_temp"
    return "partial_early_exit"


def _temp_component(
    decided_state: ShadingState,
    indoor_temp_delta_c: float | None,
    outdoor_temp_c: float | None = None,
) -> float:
    """Temperature contribution to the score, in [-0.30, +0.30].

    For shading states: cooling (negative delta) → positive signal.
    Reference scale: ±3 °C → ±0.30 (proportional), saturates beyond ±3 °C.
    Returns 0.0 when no delta is available or state is OPEN / safety.

    When outdoor_temp_c is high (≥ _OUTDOOR_HEAT_PENALTY_START_C) and the indoor
    temperature rises, the penalty is scaled down linearly because a moderate
    indoor rise is thermodynamically expected at extreme outdoor temperatures
    even with optimal shading active.
    """
    if indoor_temp_delta_c is None or decided_state not in _SHADING_STATES_FOR_TEMP:
        return 0.0
    raw = -indoor_temp_delta_c / 10.0
    if (
        outdoor_temp_c is not None
        and indoor_temp_delta_c > 0
        and raw < 0
        and outdoor_temp_c > _OUTDOOR_HEAT_PENALTY_START_C
    ):
        heat_range = _OUTDOOR_HEAT_PENALTY_ZERO_C - _OUTDOOR_HEAT_PENALTY_START_C
        outdoor_factor = min(1.0, (outdoor_temp_c - _OUTDOOR_HEAT_PENALTY_START_C) / heat_range)
        raw = raw * (1.0 - outdoor_factor)
    return max(-0.30, min(0.30, raw))


def _thermal_hold_component(
    trigger: OutcomeResolutionTrigger,
    decided_state: ShadingState,
    indoor_temp_delta_c: float | None,
) -> float:
    """Thermal-hold bonus for shading states with confirmed temperature stability.

    Returns _THERMAL_HOLD_BONUS (+0.15) when:
      - trigger is TIMEOUT (user accepted the decision for the full window)
      - decided_state is an active heat-protection shading state
        (NORMAL_SHADE, STRONG_SHADE, LIGHT_SHADE; NOT NIGHT_CLOSED or ABSENCE_CLOSED)
      - indoor_temp_delta_c is available (sensor present) AND ≤ _THERMAL_HOLD_MAX_DELTA_C

    Requires an actual sensor reading: granting +0.15 without evidence that the
    room held temperature would be overly generous and degrade learning quality.
    NIGHT_CLOSED and ABSENCE_CLOSED are excluded — their goal is not heat protection,
    so a stable room temperature during those timeouts is thermally irrelevant.
    """
    if trigger != OutcomeResolutionTrigger.TIMEOUT:
        return 0.0
    if decided_state not in _THERMAL_HOLD_STATES:
        return 0.0
    if indoor_temp_delta_c is None:
        return 0.0  # no evidence without sensor
    if indoor_temp_delta_c > _THERMAL_HOLD_MAX_DELTA_C:
        return 0.0
    return _THERMAL_HOLD_BONUS


def _open_heat_component(
    trigger: OutcomeResolutionTrigger,
    decided_state: ShadingState,
    indoor_temp_delta_c: float | None,
) -> float:
    """Conservative overheating penalty for OPEN decisions.

    Returns a small negative penalty when SmartShading left the window open
    and the room heated substantially (delta > _OPEN_OVERHEAT_THRESHOLD_C).
    Only applies to TIMEOUT (full observation window without override).

    Score contribution: -0.05 per degree above threshold, capped at -0.10.
    Examples: +4 °C rise → -0.05; +5 °C rise → -0.10; +6 °C+ → -0.10.
    """
    if trigger != OutcomeResolutionTrigger.TIMEOUT:
        return 0.0
    if decided_state != ShadingState.OPEN:
        return 0.0
    if indoor_temp_delta_c is None or indoor_temp_delta_c <= _OPEN_OVERHEAT_THRESHOLD_C:
        return 0.0
    excess = indoor_temp_delta_c - _OPEN_OVERHEAT_THRESHOLD_C
    return -min(_OPEN_OVERHEAT_MAX_PENALTY, excess * _OPEN_OVERHEAT_PENALTY_PER_C)


def _compute_score(
    trigger: OutcomeResolutionTrigger,
    decided_state: ShadingState,
    override_delay_min: float | None,
    indoor_temp_delta_c: float | None,
    outdoor_temp_c: float | None = None,
) -> float:
    """Compute an outcome score in [-1.0, +1.0].

    Override path (dominant negative):
        score = -1.0 + delay_factor * 0.5
        delay_factor = clamp(override_delay_min / 30.0, 0, 1)
        → 0 min delay → -1.0  (user corrected immediately)
        → 30 min delay → -0.5 (user waited before correcting)

    Non-override path:
        stability     = +0.30 if TIMEOUT (user accepted for the full window)
        thermal_hold  = +0.15 if TIMEOUT + shading + indoor stable
        temp_comp     = [-0.30, +0.30] for shading states (outdoor-heat-aware)
        open_heat     = [-0.10, 0] for OPEN + strong indoor rise
        score         = stability + thermal_hold + temp_comp + open_heat
    """
    if trigger == OutcomeResolutionTrigger.OVERRIDE:
        delay_factor = min(1.0, max(0.0, (override_delay_min or 0.0) / 30.0))
        score = -1.0 + delay_factor * 0.5
    else:
        stability = _STABILITY_BONUS if trigger == OutcomeResolutionTrigger.TIMEOUT else 0.0
        score = (
            stability
            + _temp_component(decided_state, indoor_temp_delta_c, outdoor_temp_c)
            + _thermal_hold_component(trigger, decided_state, indoor_temp_delta_c)
            + _open_heat_component(trigger, decided_state, indoor_temp_delta_c)
        )

    return max(-1.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Public resolution function
# ---------------------------------------------------------------------------

def resolve_outcome(
    pending: PendingOutcome,
    inp: OutcomeResolutionInput,
) -> DecisionOutcome:
    """Resolve *pending* with the given *inp*, returning a frozen DecisionOutcome.

    This function is pure and deterministic: the same (pending, inp) pair
    always produces the same DecisionOutcome. It has no side effects — storing
    the result in the LearningStore is the caller's responsibility.
    """
    override_occurred = inp.trigger == OutcomeResolutionTrigger.OVERRIDE
    escalation_occurred = inp.trigger == OutcomeResolutionTrigger.SAFETY

    state_duration_min = (
        inp.resolution_timestamp - pending.decision_timestamp
    ).total_seconds() / 60.0

    indoor_temp_delta_c: float | None = None
    if pending.indoor_temp_at_decision is not None and inp.indoor_temp_outcome_c is not None:
        indoor_temp_delta_c = inp.indoor_temp_outcome_c - pending.indoor_temp_at_decision

    resolution_status = _resolution_status(inp.trigger, inp.indoor_temp_outcome_c)

    outcome_score = _compute_score(
        trigger=inp.trigger,
        decided_state=pending.to_state,
        override_delay_min=inp.override_delay_min,
        indoor_temp_delta_c=indoor_temp_delta_c,
        outdoor_temp_c=pending.outdoor_temp_at_decision,
    )

    return DecisionOutcome(
        decision_timestamp=pending.decision_timestamp,
        window_id=pending.window_id,
        decided_state=pending.to_state,
        decided_by=pending.decided_by,
        indoor_temp_outcome_delay_min=pending.indoor_temp_outcome_delay_min,
        lifecycle_state=pending.lifecycle_state,
        from_state=pending.from_state,
        override_occurred=override_occurred,
        override_delay_min=inp.override_delay_min,
        override_event_type=inp.override_event_type,
        indoor_temp_at_decision=pending.indoor_temp_at_decision,
        indoor_temp_outcome_c=inp.indoor_temp_outcome_c,
        indoor_temp_delta_c=indoor_temp_delta_c,
        state_duration_min=state_duration_min,
        escalation_occurred=escalation_occurred,
        outcome_score=outcome_score,
        resolution_status=resolution_status,
        evaluation_timestamp=inp.resolution_timestamp,
        # P2: carry the authoritative decision link so the coordinator attaches
        # the outcome by decision_id (v2), never by timestamp.
        decision_id=pending.decision_id,
    )
