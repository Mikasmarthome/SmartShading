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
from ..models.multi_objective_outcome import (
    ATTRIBUTION_UNKNOWN,
    ATTRIBUTION_ZONE_SHARED,
    DIRECTION_CLOSE_MORE,
    DIRECTION_OPEN_MORE,
    DIRECTION_UNCHANGED,
    DIRECTION_UNKNOWN,
    MOVE_CAUSE_NONE,
    MULTI_OBJECTIVE_SCHEMA_VERSION,
    RECON_UNAVAILABLE,
    THERMAL_DIR_AMBIGUOUS,
    THERMAL_DIR_COOLING,
    THERMAL_DIR_COOLING_OR_HOLD,
    THERMAL_DIR_STABLE,
    THERMAL_DIR_WARMING,
    MovementOutcome,
    MultiObjectiveOutcome,
    OutcomeConfounders,
    OutcomeReliability,
    PreferenceOutcome,
    ThermalOutcome,
)
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
# P4 thermal maturity (passed transiently from the coordinator at resolution)
# ---------------------------------------------------------------------------

# Maturity classes
MATURITY_IMMATURE: str = "immature"
MATURITY_MATURE: str = "mature"
MATURITY_MAXIMUM_REACHED: str = "maximum_reached"
MATURITY_INVALIDATED: str = "invalidated"

# Minimum observation duration for the P4 path (a confident, learned window may
# resolve before 30 min — but only when explicitly matured and trend-stable).
_P4_MIN_OBSERVATION_MIN: float = 15.0


@dataclass(frozen=True)
class ThermalMaturityInput:
    """P4 thermal maturity provenance for one resolution.

    authority_applied=False ⇒ legacy/fallback path (30-min gate unchanged).
    authority_applied=True  ⇒ P4 path (learned window; early availability only
    when matured + trend-stable, or maximum_reached).
    """

    authority_applied: bool = False
    selected_window_minutes: int | None = None
    model_confidence_at_decision: float | None = None
    response_onset_detected: bool = False
    response_onset_minutes: float | None = None
    stable_trend_detected: bool = False
    resolution_reason: str = "fallback_window"
    maturity: str = MATURITY_MATURE


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
    # --- P3 Multi-Objective inputs (all transient; HA convention only) ---
    # override_target_ha / final_requested_target_ha are LOGICAL HA positions
    # (0=closed, 100=open).  The coordinator converts any internal value BEFORE
    # constructing this input — no internal position ever enters here.
    override_target_ha: int | None = None
    final_requested_target_ha: int | None = None
    override_hold_duration_seconds: float | None = None
    cleared_by_lifecycle: bool = False
    cleared_by_safety: bool = False
    solar_exposure_at_decision: float | None = None
    observation_interrupted: bool = False
    config_changed: bool = False
    behavior_mode_changed: bool = False
    legacy_data: bool = False
    movement_observation: "MovementObservation | None" = None
    # P4 thermal maturity (None ⇒ legacy 30-min path).
    thermal_maturity: "ThermalMaturityInput | None" = None


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
    maturity: "ThermalMaturityInput | None" = None,
    interrupted: bool = False,
) -> str:
    """Resolution status, P4-maturity aware.

    A TIMEOUT with temperature is 'complete' only when thermally mature:
      - legacy/fallback path (no P4 authority): duration is 30 min by
        construction → complete.
      - P4 path: 'complete' only when matured (mature/maximum_reached); an
        early window that did not mature stays 'partial_early_exit' so it
        never enters the active legacy chain (e.g. SolarImpact) as complete.
    """
    if trigger != OutcomeResolutionTrigger.TIMEOUT:
        return "partial_early_exit"
    if indoor_temp_outcome_c is None:
        return "partial_no_temp"
    if interrupted:
        # A restart-interrupted observation is never a complete thermal sample
        # and must not enter the active legacy chain (e.g. SolarImpact).
        return "interrupted_partial"
    if maturity is not None and maturity.authority_applied:
        if maturity.maturity in (MATURITY_MATURE, MATURITY_MAXIMUM_REACHED):
            return "complete"
        return "partial_early_exit"   # early but not matured / invalidated
    return "complete"   # legacy/fallback 30-min path


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


# ===========================================================================
# P3 — Multi-Objective resolvers (pure, deterministic, fully testable)
# ===========================================================================

# Resolution-status string for a fully observed (TIMEOUT + temp) outcome.
_RESOLUTION_COMPLETE: str = "complete"

# Thermal constants (derived from / consistent with the v1 score constants).
_THERMAL_COOL_SCALE_C: float = 3.0            # ±3 °C maps to ±1.0 thermal score
_THERMAL_HOLD_DELTA_C: float = _THERMAL_HOLD_MAX_DELTA_C   # 0.5 — "held"
_THERMAL_HOLD_SCORE: float = 0.30             # modest positive for protection under load
# Below this solar load, "temperature stable" is NOT credited as a shading effect.
_THERMAL_LOAD_MIN_WM2: float = 150.0          # == default light-shade entry threshold
_THERMAL_MIN_DURATION_MIN: float = 30.0       # fixed window in P3 (P4 replaces it)
_OPEN_OVERHEAT_DELTA_C: float = _OPEN_OVERHEAT_THRESHOLD_C  # 3.0

_THERMAL_STATES: frozenset[ShadingState] = frozenset({
    ShadingState.LIGHT_SHADE, ShadingState.NORMAL_SHADE, ShadingState.STRONG_SHADE,
})

# Preference signal strength by override delay (minutes) — mirrors
# override_learning.compute_override_signal_strength so the two stay consistent.
def _preference_strength_from_delay(delay_min: float | None) -> float:
    if delay_min is None:
        return 0.50
    if delay_min < 5:
        return 1.00
    if delay_min < 30:
        return 0.75
    if delay_min < 120:
        return 0.40
    return 0.10


@dataclass(frozen=True)
class MovementObservation:
    """Coordinator-supplied movement counters for one observation window.

    All counts are deterministic (no estimates).  target_history holds the
    sequence of comfort-driven target positions (HA convention) observed during
    the window, used for oscillation detection.  decision_target_ha is the
    target of the decision being resolved.
    """

    decision_target_ha: int | None = None
    command_attempt_count: int = 0
    successful_command_count: int = 0
    comfort_state_transition_count: int = 0
    excluded_transition_count: int = 0
    material_target_change_count: int = 0
    target_history: tuple[int, ...] = ()
    minimum_action_interval_respected: bool | None = None
    movement_cause: str = MOVE_CAUSE_NONE


# Oscillation deadband — same materiality threshold as P2 decision records.
_OSCILLATION_DEADBAND_HA: int = 3


def _detect_oscillation(history: tuple[int, ...]) -> tuple[bool, str | None]:
    """Deterministic oscillation detection over comfort target history (HA).

    Triggers when, within the window:
      - two opposite material target changes occur, OR
      - the target returns within the deadband of an earlier target after a
        material move away, OR
      - the sequence toggles A→B→A between two distinct targets.
    """
    if len(history) < 3:
        return False, None
    # A→B→A toggle (within deadband on the return)
    for i in range(len(history) - 2):
        a, b, c = history[i], history[i + 1], history[i + 2]
        if abs(b - a) >= _OSCILLATION_DEADBAND_HA and abs(c - a) < _OSCILLATION_DEADBAND_HA \
                and abs(b - c) >= _OSCILLATION_DEADBAND_HA:
            return True, "toggle_return_to_previous"
    # Two opposite material changes
    directions: list[int] = []
    for i in range(1, len(history)):
        delta = history[i] - history[i - 1]
        if abs(delta) >= _OSCILLATION_DEADBAND_HA:
            directions.append(1 if delta > 0 else -1)
    for i in range(1, len(directions)):
        if directions[i] != directions[i - 1]:
            return True, "opposite_material_changes"
    return False, None


def compute_thermal_outcome(
    *,
    state: ShadingState,
    indoor_delta_c: float | None,
    outdoor_temperature_c: float | None,
    solar_exposure: float | None,
    observation_duration_min: float | None,
    resolution_status: str,
    thermal_confounded: bool,
    temperature_start: float | None = None,
    temperature_end: float | None = None,
    maturity: "ThermalMaturityInput | None" = None,
) -> ThermalOutcome:
    """Exact, deterministic thermal observation.  Describes ONLY the thermal
    association — not proven window causation, not a global score.

    Two availability paths (P4 integration):
      - Legacy/fallback (no P4 authority): minimum duration stays 30 min.
      - P4 path (authority applied): a confident, learned window may be valid
        from MIN_OBSERVATION (15 min) — but only when matured/maximum_reached
        (enforced jointly here and via resolution_status, which downgrades an
        early non-matured TIMEOUT to partial_early_exit).
    """
    duration = observation_duration_min or 0.0
    legacy_path = maturity is None or not maturity.authority_applied
    min_required = _THERMAL_MIN_DURATION_MIN if legacy_path else _P4_MIN_OBSERVATION_MIN

    # P4 maturity provenance carried onto every ThermalOutcome (available or not).
    _mat_kwargs = dict(
        thermal_resolution_reason=(maturity.resolution_reason if maturity else None),
        thermal_maturity=(maturity.maturity if maturity else None),
        selected_observation_window_minutes=(maturity.selected_window_minutes if maturity else None),
        actual_observation_duration_minutes=round(duration, 1),
        response_onset_detected=(maturity.response_onset_detected if maturity else False),
        response_onset_minutes=(maturity.response_onset_minutes if maturity else None),
        stable_trend_detected=(maturity.stable_trend_detected if maturity else False),
        thermal_model_confidence_at_decision=(maturity.model_confidence_at_decision if maturity else None),
        thermal_model_authority_applied=(maturity.authority_applied if maturity else False),
    )

    invalidated = maturity is not None and maturity.maturity == MATURITY_INVALIDATED
    available = (
        not invalidated
        and resolution_status == _RESOLUTION_COMPLETE
        and indoor_delta_c is not None
        and duration >= min_required
        and state in (_THERMAL_STATES | {ShadingState.OPEN})
        and not thermal_confounded
    )
    # Early (<30 min) P4 outcomes are only available when explicitly matured.
    if available and not legacy_path and duration < _THERMAL_MIN_DURATION_MIN:
        if maturity is None or maturity.maturity not in (MATURITY_MATURE, MATURITY_MAXIMUM_REACHED):
            available = False
    if not available:
        return ThermalOutcome(
            available=False, score=None,
            temperature_start=temperature_start, temperature_end=temperature_end,
            temperature_delta=indoor_delta_c,  # informative only
            observation_duration_min=observation_duration_min,
            outdoor_temp_at_decision=outdoor_temperature_c,
            solar_exposure_at_decision=solar_exposure,
            reason="not_available" if not thermal_confounded else "thermal_confounded",
            reconstruction_quality=RECON_UNAVAILABLE,
            **_mat_kwargs,
        )

    delta = float(indoor_delta_c)  # not None here
    observed = (
        THERMAL_DIR_COOLING if delta < -_THERMAL_HOLD_DELTA_C
        else THERMAL_DIR_STABLE if abs(delta) <= _THERMAL_HOLD_DELTA_C
        else THERMAL_DIR_WARMING
    )
    has_load = solar_exposure is not None and solar_exposure >= _THERMAL_LOAD_MIN_WM2
    protection = False
    overheat = False
    insufficient = False

    if state in _THERMAL_STATES:
        expected = THERMAL_DIR_COOLING_OR_HOLD
        if observed == THERMAL_DIR_COOLING:
            score = max(0.0, min(1.0, -delta / _THERMAL_COOL_SCALE_C))
            protection = True
            reason = "cooling_under_shade"
        elif observed == THERMAL_DIR_STABLE:
            # "stable" is only credited as protection when there was real solar load.
            score = _THERMAL_HOLD_SCORE if has_load else 0.0
            protection = has_load
            reason = "held_under_load" if has_load else "stable_low_load_uncredited"
        else:  # warming despite shade
            raw = -delta / _THERMAL_COOL_SCALE_C
            if outdoor_temperature_c is not None and outdoor_temperature_c > _OUTDOOR_HEAT_PENALTY_START_C:
                rng = _OUTDOOR_HEAT_PENALTY_ZERO_C - _OUTDOOR_HEAT_PENALTY_START_C
                mit = min(1.0, (outdoor_temperature_c - _OUTDOOR_HEAT_PENALTY_START_C) / rng)
                raw = raw * (1.0 - mit)
            score = max(-1.0, min(0.0, raw))
            insufficient = True
            reason = "insufficient_response"
    else:  # OPEN — ambiguous thermal expectation
        expected = THERMAL_DIR_AMBIGUOUS
        if delta > _OPEN_OVERHEAT_DELTA_C:
            overheat = True
            score = max(-0.3, min(0.0, -(delta - _OPEN_OVERHEAT_DELTA_C) / 5.0))
            reason = "open_overheat"
        else:
            score = None  # ambiguous, not automatically negative
            reason = "open_ambiguous"

    return ThermalOutcome(
        available=True, score=score,
        temperature_start=temperature_start, temperature_end=temperature_end,
        temperature_delta=delta, observation_duration_min=observation_duration_min,
        expected_direction=expected, observed_direction=observed,
        protection_effect_detected=protection, overheat_detected=overheat,
        insufficient_response=insufficient,
        outdoor_temp_at_decision=outdoor_temperature_c, solar_exposure_at_decision=solar_exposure,
        reason=reason, reconstruction_quality=RECON_UNAVAILABLE,
        **_mat_kwargs,
    )


def compute_preference_outcome(
    *,
    trigger: OutcomeResolutionTrigger,
    override_delay_min: float | None,
    override_target_ha: int | None,
    final_requested_target_ha: int | None,
    override_hold_duration_seconds: float | None,
    cleared_by_lifecycle: bool,
    cleared_by_safety: bool,
    preference_confounded: bool,
) -> PreferenceOutcome:
    """Exact preference signal.  An override is a REJECTION (negative score).
    Direction is separate from the score sign.  Lifecycle/safety clears and the
    absence of an override never produce a positive score."""
    is_override = trigger == OutcomeResolutionTrigger.OVERRIDE

    # Direction (independent of score)
    direction = DIRECTION_UNKNOWN
    delta_ha: int | None = None
    if override_target_ha is not None and final_requested_target_ha is not None:
        delta_ha = override_target_ha - final_requested_target_ha
        if delta_ha > 0:
            direction = DIRECTION_OPEN_MORE     # higher HA = more open
        elif delta_ha < 0:
            direction = DIRECTION_CLOSE_MORE
        else:
            direction = DIRECTION_UNCHANGED

    if not is_override:
        # No override on this resolution → no preference signal (never positive).
        return PreferenceOutcome(
            available=False, manual_override_occurred=False,
            override_direction=DIRECTION_UNKNOWN, cleared_by_lifecycle=cleared_by_lifecycle,
            preference_signal_strength=0.0 if (cleared_by_lifecycle or cleared_by_safety) else None,
            score=None,
            reason="lifecycle_clear_not_reversal" if cleared_by_lifecycle
            else "safety_clear_not_reversal" if cleared_by_safety else "no_override",
            reconstruction_quality=RECON_UNAVAILABLE,
        )

    delay_sec = override_delay_min * 60.0 if override_delay_min is not None else None
    strength = _preference_strength_from_delay(override_delay_min)
    # A long hold strengthens the rejection signal (up to +0.5, capped at 1.0).
    if override_hold_duration_seconds is not None:
        if override_hold_duration_seconds >= 7200:      # >= 2 h
            strength = min(1.0, strength + 0.5)
        elif override_hold_duration_seconds >= 1800:    # >= 30 min
            strength = min(1.0, strength + 0.25)
    score = -strength  # rejection → negative; direction lives in override_direction

    return PreferenceOutcome(
        available=True, manual_override_occurred=True,
        override_direction=direction, override_target_ha=override_target_ha,
        override_delta_ha=delta_ha, override_delay_seconds=delay_sec,
        override_hold_duration_seconds=override_hold_duration_seconds,
        cleared_by_lifecycle=False, preference_signal_strength=strength, score=score,
        reason="override_rejection", reconstruction_quality=RECON_UNAVAILABLE,
    )


def compute_movement_outcome(
    obs: "MovementObservation | None",
) -> MovementOutcome:
    """Exact movement economy from deterministic counters.  Positive movement is
    a LIMITED stability signal; safety/lifecycle/absence/manual are excluded from
    instability scoring; a CommandFilter block is not a successful command."""
    if obs is None:
        return MovementOutcome(available=False, reason="no_observation")

    excluded_cause = obs.movement_cause in ("safety", "lifecycle", "absence", "manual")
    oscillation, osc_reason = _detect_oscillation(obs.target_history)
    comfort_moves = obs.comfort_state_transition_count

    unnecessary = oscillation or comfort_moves >= 2
    stable = comfort_moves == 0 and obs.material_target_change_count == 0 and not excluded_cause

    if excluded_cause:
        score: float | None = None     # never penalize excluded movement
        reason = f"excluded_{obs.movement_cause}"
    elif oscillation:
        score = max(-1.0, -0.2 * max(1, comfort_moves))
        reason = "oscillation"
    elif comfort_moves >= 2:
        score = max(-1.0, -0.2 * (comfort_moves - 1))
        reason = "repeated_comfort_movement"
    elif stable:
        score = 0.2                     # LIMITED positive — never compensates
        reason = "stable_no_additional_action"
    else:
        score = 0.0
        reason = "single_followup_movement"

    return MovementOutcome(
        available=True, score=score,
        stable_without_additional_action=stable,
        command_attempt_count=obs.command_attempt_count,
        successful_command_count=obs.successful_command_count,
        material_target_change_count=obs.material_target_change_count,
        comfort_state_transition_count=comfort_moves,
        excluded_transition_count=obs.excluded_transition_count,
        oscillation_detected=oscillation, oscillation_reason=osc_reason,
        minimum_action_interval_respected=obs.minimum_action_interval_respected,
        unnecessary_movement_signal=unnecessary, movement_cause=obs.movement_cause,
        reason=reason, reconstruction_quality=RECON_UNAVAILABLE,
    )


def _finalize_reliability(
    *,
    thermal: ThermalOutcome,
    movement: MovementOutcome,
    preference: PreferenceOutcome,
    confounders: OutcomeConfounders,
    resolution_status: str,
    interrupted: bool,
    legacy_data: bool,
    attribution_quality: str,
    thermal_early: bool = False,
) -> OutcomeReliability:
    """Per-dimension reliability gates + a CONSERVATIVE overall summary.

    overall = min of the AVAILABLE relevant dimension reliabilities (diagnostic
    only — no active authority).  invalidated → everything 0.
    """
    invalidated = resolution_status == "invalidated"
    partial = resolution_status in ("partial_no_temp", "partial_early_exit", "interrupted_partial")
    reasons: list[str] = []

    if invalidated:
        return OutcomeReliability(
            overall=0.0, thermal=0.0, movement=0.0, preference=0.0,
            sensor_completeness=0.0, observation_continuity=0.0, context_stability=0.0,
            attribution_quality=attribution_quality, provenance_completeness=0.0,
            legacy_data=legacy_data, interrupted=interrupted, partial=partial,
            confounded=any((confounders.thermal_confounded, confounders.movement_confounded,
                            confounders.preference_confounded)),
            reasons=("invalidated",),
        )

    sensor_completeness = 1.0 if thermal.available or thermal.temperature_delta is not None else 0.0
    observation_continuity = 0.3 if interrupted else (0.6 if partial else 1.0)
    context_stability = 0.0 if any((
        confounders.thermal_confounded, confounders.movement_confounded,
        confounders.preference_confounded)) else 1.0

    # Thermal reliability
    if not thermal.available:
        thermal_rel = 0.0
    elif confounders.thermal_confounded:
        thermal_rel = 0.0
        reasons.append("thermal_confounded")
    else:
        thermal_rel = observation_continuity * (0.5 if partial else 1.0)
        # Conservative: an early (<30 min) learned-window outcome is trusted less
        # than a full-length observation — prevents circular window shortening.
        if thermal_early:
            thermal_rel *= 0.6

    # Movement reliability — degraded by interruption (lost observation).
    if not movement.available:
        movement_rel = 0.0
    elif confounders.movement_confounded:
        movement_rel = 0.0
    else:
        movement_rel = 0.4 if interrupted else 1.0

    # Preference reliability — a clear override is reliable even without temperature.
    if not preference.available:
        preference_rel = 0.0
    elif confounders.preference_confounded:
        preference_rel = 0.0
        reasons.append("preference_confounded")
    else:
        preference_rel = 1.0

    available_rels = [
        r for avail, r in (
            (thermal.available, thermal_rel),
            (movement.available, movement_rel),
            (preference.available, preference_rel),
        ) if avail
    ]
    overall = min(available_rels) if available_rels else 0.0

    if legacy_data:
        reasons.append("legacy_data")
    if interrupted:
        reasons.append("interrupted")

    return OutcomeReliability(
        overall=overall, thermal=thermal_rel, movement=movement_rel, preference=preference_rel,
        sensor_completeness=sensor_completeness, observation_continuity=observation_continuity,
        context_stability=context_stability, attribution_quality=attribution_quality,
        provenance_completeness=0.5 if confounders.incomplete_provenance else 1.0,
        legacy_data=legacy_data, interrupted=interrupted, partial=partial,
        confounded=any((confounders.thermal_confounded, confounders.movement_confounded,
                        confounders.preference_confounded)),
        reasons=tuple(reasons),
    )


def _build_multi_objective(
    pending: PendingOutcome,
    inp: "OutcomeResolutionInput",
    indoor_temp_delta_c: float | None,
    legacy_score: float | None,
    resolution_status: str,
) -> MultiObjectiveOutcome:
    """Assemble the full MultiObjectiveOutcome (pure)."""
    state = pending.to_state
    is_override = inp.trigger == OutcomeResolutionTrigger.OVERRIDE
    is_safety = inp.trigger == OutcomeResolutionTrigger.SAFETY

    # --- Confounders (dimension-specific) ---
    sensor_unavailable = inp.indoor_temp_outcome_c is None
    thermal_confounded = (
        is_override or is_safety or inp.observation_interrupted
        or inp.config_changed or inp.behavior_mode_changed
    )
    # Movement is confounded only by config/mode change (safety/lifecycle/manual
    # are EXCLUDED, not confounded — handled by movement_cause).
    movement_confounded = inp.config_changed or inp.behavior_mode_changed
    preference_confounded = inp.config_changed or inp.behavior_mode_changed
    detected: list[str] = []
    for name, flag in (
        ("sensor_unavailable", sensor_unavailable),
        ("ha_restart_interruption", inp.observation_interrupted),
        ("config_changed", inp.config_changed),
        ("behavior_mode_changed", inp.behavior_mode_changed),
        ("safety_event", is_safety),
        ("manual_override", is_override),
    ):
        if flag:
            detected.append(name)
    confounders = OutcomeConfounders(
        thermal_confounded=thermal_confounded,
        movement_confounded=movement_confounded,
        preference_confounded=preference_confounded,
        sensor_unavailable=sensor_unavailable,
        ha_restart_interruption=inp.observation_interrupted,
        config_changed=inp.config_changed,
        behavior_mode_changed=inp.behavior_mode_changed,
        safety_event=is_safety, manual_override=is_override,
        incomplete_provenance=False, detected=tuple(detected),
    )

    duration_min = (inp.resolution_timestamp - pending.decision_timestamp).total_seconds() / 60.0

    thermal = compute_thermal_outcome(
        state=state, indoor_delta_c=indoor_temp_delta_c,
        outdoor_temperature_c=pending.outdoor_temp_at_decision,
        solar_exposure=inp.solar_exposure_at_decision,
        observation_duration_min=duration_min, resolution_status=resolution_status,
        thermal_confounded=thermal_confounded,
        temperature_start=pending.indoor_temp_at_decision,
        temperature_end=inp.indoor_temp_outcome_c,
        maturity=inp.thermal_maturity,
    )
    preference = compute_preference_outcome(
        trigger=inp.trigger, override_delay_min=inp.override_delay_min,
        override_target_ha=inp.override_target_ha,
        final_requested_target_ha=inp.final_requested_target_ha,
        override_hold_duration_seconds=inp.override_hold_duration_seconds,
        cleared_by_lifecycle=inp.cleared_by_lifecycle, cleared_by_safety=inp.cleared_by_safety,
        preference_confounded=preference_confounded,
    )
    movement = compute_movement_outcome(inp.movement_observation)

    # --- Attribution (P3: never window_isolated) ---
    if not thermal.available:
        attribution = ATTRIBUTION_UNKNOWN
    else:
        attribution = ATTRIBUTION_ZONE_SHARED  # conservative default in P3

    _thermal_early = (
        thermal.available and thermal.thermal_model_authority_applied
        and (thermal.actual_observation_duration_minutes or 999.0) < _THERMAL_MIN_DURATION_MIN
    )
    reliability = _finalize_reliability(
        thermal=thermal, movement=movement, preference=preference,
        confounders=confounders, resolution_status=resolution_status,
        interrupted=inp.observation_interrupted, legacy_data=inp.legacy_data,
        attribution_quality=attribution, thermal_early=_thermal_early,
    )

    return MultiObjectiveOutcome(
        thermal=thermal, movement=movement, preference=preference,
        reliability=reliability, confounders=confounders,
        attribution_quality=attribution, resolution_status=resolution_status,
        legacy_score=legacy_score, schema_version=MULTI_OBJECTIVE_SCHEMA_VERSION,
    )


def reconstruct_multi_objective_from_legacy(o: DecisionOutcome) -> MultiObjectiveOutcome:
    """Build a MultiObjectiveOutcome from a legacy v1 DecisionOutcome.

    Strict rules (P3 sharpening 9): no partial score derived from the legacy
    overall score is ever marked ``exact``; missing raw data stays
    ``unavailable``; an override direction that cannot be recovered stays
    ``unknown``.  Reconstructed legacy dimensions must NEVER alone unlock
    shadow/experiment eligibility (the record also carries legacy_record=True).
    """
    from ..models.multi_objective_outcome import (
        RECON_PARTIAL, RECON_UNAVAILABLE, DIRECTION_UNKNOWN,
    )

    delta = o.indoor_temp_delta_c
    status = o.resolution_status

    # Thermal: exposure unknown in v1 → cannot credit a score; informative only.
    if delta is not None and status == _RESOLUTION_COMPLETE:
        observed = (
            THERMAL_DIR_COOLING if delta < -_THERMAL_HOLD_DELTA_C
            else THERMAL_DIR_STABLE if abs(delta) <= _THERMAL_HOLD_DELTA_C
            else THERMAL_DIR_WARMING
        )
        thermal = ThermalOutcome(
            available=False, score=None, temperature_delta=delta,
            observed_direction=observed, reason="legacy_partial_no_exposure",
            reconstructed=True, reconstruction_quality=RECON_PARTIAL,
        )
    else:
        thermal = ThermalOutcome(
            available=False, score=None, temperature_delta=delta,
            reason="legacy_unavailable",
            reconstructed=True, reconstruction_quality=RECON_UNAVAILABLE,
        )

    # Preference: override occurrence + delay are raw fields; direction/target
    # were not stored in v1 → direction unknown, quality partial.
    if o.override_occurred:
        strength = _preference_strength_from_delay(o.override_delay_min)
        preference = PreferenceOutcome(
            available=True, manual_override_occurred=True,
            override_direction=DIRECTION_UNKNOWN,
            override_delay_seconds=(o.override_delay_min * 60.0
                                    if o.override_delay_min is not None else None),
            preference_signal_strength=strength, score=-strength,
            reason="legacy_override_direction_unknown",
            reconstructed=True, reconstruction_quality=RECON_PARTIAL,
        )
    else:
        preference = PreferenceOutcome(
            available=False, manual_override_occurred=False, score=None,
            reason="legacy_no_override",
            reconstructed=True, reconstruction_quality=RECON_UNAVAILABLE,
        )

    # Movement: not reconstructable from v1 data.
    movement = MovementOutcome(
        available=False, score=None, reason="legacy_unavailable",
        reconstructed=True, reconstruction_quality=RECON_UNAVAILABLE,
    )
    confounders = OutcomeConfounders(incomplete_provenance=True, detected=("legacy_record",))
    reliability = _finalize_reliability(
        thermal=thermal, movement=movement, preference=preference,
        confounders=confounders, resolution_status=status,
        interrupted=False, legacy_data=True, attribution_quality=ATTRIBUTION_UNKNOWN,
    )
    return MultiObjectiveOutcome(
        thermal=thermal, movement=movement, preference=preference,
        reliability=reliability, confounders=confounders,
        attribution_quality=ATTRIBUTION_UNKNOWN, resolution_status=status,
        legacy_score=o.outcome_score,
    )


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

    resolution_status = _resolution_status(
        inp.trigger, inp.indoor_temp_outcome_c, inp.thermal_maturity,
        interrupted=inp.observation_interrupted,
    )

    outcome_score = _compute_score(
        trigger=inp.trigger,
        decided_state=pending.to_state,
        override_delay_min=inp.override_delay_min,
        indoor_temp_delta_c=indoor_temp_delta_c,
        outdoor_temp_c=pending.outdoor_temp_at_decision,
    )

    # P3: assemble the multi-objective decomposition (additive; legacy score
    # above is bit-identical and remains the active authority in P3).
    multi_objective = _build_multi_objective(
        pending, inp, indoor_temp_delta_c, outcome_score, resolution_status
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
        # P3: additive multi-objective decomposition (no active authority yet).
        multi_objective=multi_objective,
    )
