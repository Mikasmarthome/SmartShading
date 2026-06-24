"""Bounded Strategy Experiment engine — LE 2.0 / Phase P9B (pure).

Generalises the P7/P8 experiment+adoption logic to the strategy parameter
families (timing / threshold / tier-choice / hold / hysteresis): bounded
candidate computation, exact multi-experiment evidence + consumption ledger,
experiment-need suppression, dimension-specific monitoring, confirmation,
suspend/reduce/rollback (robust evidence only), cooldown, cause→family routing
and restart reconciliation.  No Home Assistant import, no mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models.strategy_learning import (
    AD_TERMINAL_STATUSES,
    CONFIRM_S1_DAYS,
    CONFIRM_S1_DISTINCT_DAYS,
    CONFIRM_S1_OUTCOMES,
    CONFIRM_S2_DAYS,
    CONFIRM_S2_DISTINCT_DAYS,
    CONFIRM_S2_OUTCOMES,
    EVAL_DEGRADED,
    EVAL_IMPROVED,
    EVAL_NO_DEGRADATION,
    EVAL_PREFERENCE_REJECTED,
    FAMILY_BOUNDS,
    REDUCE_DEGRADED_DISTINCT_DAYS,
    REDUCE_DEGRADED_MIN,
    ROLLBACK_COOLDOWN_DAYS,
    ROLLBACK_DEGRADED_DISTINCT_DAYS,
    ROLLBACK_DEGRADED_MIN,
    S1_MIN_CONFIDENCE,
    S1_MIN_DISTINCT_DAYS,
    S1_MIN_EXPERIMENTS,
    S1_MIN_IMPROVED,
    S1_MIN_RELIABILITY,
    S2_MIN_CONFIDENCE,
    S2_MIN_DISTINCT_DAYS,
    S2_MIN_EXPERIMENTS,
    S2_MIN_IMPROVED,
    S2_MIN_RELIABILITY,
    S2_STABILITY_DAYS,
    ACTION_FULL_ROLLBACK,
    ACTION_INVALIDATE,
    ACTION_REDUCE_ONE_STEP,
    ACTION_RETAIN,
    ACTION_TEMPORARY_SUSPEND,
    StrategyMonitoringState,
)

# Monitoring outcome classes (production observations under strategy).
MON_IMPROVED = EVAL_IMPROVED
MON_NO_DEGRADATION = EVAL_NO_DEGRADATION
MON_DEGRADED = EVAL_DEGRADED
MON_INCONCLUSIVE = "inconclusive"
MON_UNAVAILABLE = "unavailable"
MON_CONFOUNDED = "confounded"


# ---------------------------------------------------------------------------
# Bounded candidate computation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyCandidateResult:
    valid: bool
    candidate_value: float | None
    delta_from_baseline: float | None
    block_reason: str | None = None


def compute_strategy_candidate(
    *, parameter_family: str, baseline_value: float, current_adopted_delta: float,
    direction_sign: int,
) -> StrategyCandidateResult:
    """Compute the next bounded candidate value (exactly one step) for a family.

    The new cumulative delta = current_adopted_delta + sign*step must not exceed
    the family cap.  direction_sign ∈ {-1, +1}.
    """
    bounds = FAMILY_BOUNDS.get(parameter_family)
    if bounds is None:
        return StrategyCandidateResult(False, None, None, "unknown_family")
    if direction_sign not in (-1, 1):
        return StrategyCandidateResult(False, None, None, "invalid_direction")
    new_delta = current_adopted_delta + direction_sign * bounds.step
    if abs(new_delta) - bounds.cap > 1e-9:
        return StrategyCandidateResult(False, None, None, "cumulative_cap_exceeded")
    return StrategyCandidateResult(
        valid=True, candidate_value=baseline_value + new_delta, delta_from_baseline=new_delta)


# ---------------------------------------------------------------------------
# Evidence + consumption
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyEvidence:
    experiment_id: str
    decision_class: str
    day: object
    reliability: float
    confidence: float
    context_family: str
    config_generation: int
    direction_sign: int


@dataclass(frozen=True)
class StrategyEvidenceResult:
    sufficient: bool
    selected_experiment_ids: tuple[str, ...] = ()
    distinct_days: int = 0
    improved_count: int = 0
    no_degradation_count: int = 0
    degraded_count: int = 0
    preference_rejection_count: int = 0
    confidence: float = 0.0
    reliability: float = 0.0
    validated_context_families: tuple[str, ...] = ()
    direction_sign: int = 0
    block_reason: str | None = None


def evaluate_strategy_evidence(
    experiments: list[StrategyEvidence], *, stage: int, consumed_ids: frozenset[str],
    config_generation: int,
) -> StrategyEvidenceResult:
    """Fresh exact evaluation of terminal strategy experiments for one stage.

    Only non-consumed experiments of the current generation count; any degraded
    or preference-rejected experiment blocks; a single experiment never suffices;
    all selected experiments must share the same direction sign."""
    if stage == 2:
        min_exp, min_days, min_improved = S2_MIN_EXPERIMENTS, S2_MIN_DISTINCT_DAYS, S2_MIN_IMPROVED
        min_conf, min_rel = S2_MIN_CONFIDENCE, S2_MIN_RELIABILITY
    else:
        min_exp, min_days, min_improved = S1_MIN_EXPERIMENTS, S1_MIN_DISTINCT_DAYS, S1_MIN_IMPROVED
        min_conf, min_rel = S1_MIN_CONFIDENCE, S1_MIN_RELIABILITY

    pool = [e for e in experiments
            if e.experiment_id not in consumed_ids and e.config_generation == config_generation]
    if any(e.decision_class == EVAL_DEGRADED for e in pool):
        return StrategyEvidenceResult(False, block_reason="degraded_experiment_present")
    if any(e.decision_class == EVAL_PREFERENCE_REJECTED for e in pool):
        return StrategyEvidenceResult(False, block_reason="preference_rejection_present")
    valid = [e for e in pool if e.decision_class in (EVAL_IMPROVED, EVAL_NO_DEGRADATION)]
    # All selected experiments must push in the same direction.
    signs = {e.direction_sign for e in valid}
    if len(signs) > 1:
        # keep the majority direction set
        pos = [e for e in valid if e.direction_sign == 1]
        neg = [e for e in valid if e.direction_sign == -1]
        valid = pos if len(pos) >= len(neg) else neg
    direction = valid[0].direction_sign if valid else 0
    improved = [e for e in valid if e.decision_class == EVAL_IMPROVED]
    no_deg = [e for e in valid if e.decision_class == EVAL_NO_DEGRADATION]
    days = {e.day for e in valid}
    avg_conf = sum(e.confidence for e in valid) / len(valid) if valid else 0.0
    avg_rel = sum(e.reliability for e in valid) / len(valid) if valid else 0.0

    reason = None
    if len(valid) < min_exp:
        reason = "insufficient_valid_experiments"
    elif len(days) < min_days:
        reason = "insufficient_distinct_days"
    elif len(improved) < min_improved:
        reason = "insufficient_improved_outcomes"
    elif avg_conf < min_conf:
        reason = "confidence_too_low"
    elif avg_rel < min_rel:
        reason = "reliability_too_low"
    sufficient = reason is None
    return StrategyEvidenceResult(
        sufficient=sufficient,
        selected_experiment_ids=tuple(e.experiment_id for e in valid) if sufficient else (),
        distinct_days=len(days), improved_count=len(improved), no_degradation_count=len(no_deg),
        confidence=round(avg_conf, 4), reliability=round(avg_rel, 4),
        validated_context_families=tuple(sorted({e.context_family for e in valid})),
        direction_sign=direction, block_reason=reason)


# ---------------------------------------------------------------------------
# Experiment-need suppression
# ---------------------------------------------------------------------------

NEED_NO_ADOPTION = "no_strategy_adoption"
NEED_VALIDATING = "strategy_adoption_validating"
NEED_CONFIRMED_STABLE = "stable_strategy_no_experiment_needed"
NEED_STAGE2_INSUFFICIENT = "second_stage_evidence_insufficient"
NEED_MAX_REACHED = "maximum_delta_reached"
NEED_REVALIDATION = "revalidation_required"
NEED_CONTEXT_CHANGED = "context_changed"
NEED_FORECAST_TRUST = "forecast_trust_insufficient"
NEED_MONITORING_INCOMPLETE = "monitoring_period_incomplete"


@dataclass(frozen=True)
class StrategyNeedInput:
    has_adoption: bool
    parameter_family: str
    adopted_delta: float
    status: str
    confirmed_stable: bool
    activated_at: datetime | None
    repeated_degradation: bool
    new_independent_evidence: bool
    context_changed: bool
    revalidation_required: bool
    now: datetime


def evaluate_strategy_experiment_need(inp: StrategyNeedInput) -> tuple[bool, str]:
    if not inp.has_adoption:
        return (True, NEED_NO_ADOPTION)
    bounds = FAMILY_BOUNDS.get(inp.parameter_family)
    if bounds is not None and abs(inp.adopted_delta) >= bounds.cap - 1e-9:
        return (False, NEED_MAX_REACHED)
    if not inp.confirmed_stable:
        return (False, NEED_VALIDATING)
    if inp.context_changed:
        return (False, NEED_CONTEXT_CHANGED)  # shadow first, not an immediate experiment
    if inp.revalidation_required or inp.repeated_degradation:
        return (True, NEED_REVALIDATION)
    stable_long = (inp.activated_at is not None
                   and (inp.now - inp.activated_at) >= timedelta(days=S2_STABILITY_DAYS))
    if inp.new_independent_evidence and stable_long:
        return (True, NEED_STAGE2_INSUFFICIENT)  # justified second-stage attempt
    if inp.new_independent_evidence and not stable_long:
        return (False, NEED_MONITORING_INCOMPLETE)
    return (False, NEED_CONFIRMED_STABLE)


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

def classify_strategy_outcome(
    *, thermal_available: bool, thermal_score: float | None, confounded: bool,
    open_more_rejection: bool,
) -> str:
    if open_more_rejection:
        return MON_DEGRADED
    if confounded:
        return MON_CONFOUNDED
    if not thermal_available or thermal_score is None:
        return MON_UNAVAILABLE
    if thermal_score >= 0.15:
        return MON_IMPROVED
    if thermal_score <= -0.15:
        return MON_DEGRADED
    return MON_NO_DEGRADATION


def update_strategy_monitoring(
    state: StrategyMonitoringState, *, outcome_class: str, open_more_rejection: bool,
    moved: bool, day: object, now: datetime,
) -> StrategyMonitoringState:
    from dataclasses import replace as _replace
    valid = outcome_class in (MON_IMPROVED, MON_NO_DEGRADATION, MON_DEGRADED)
    counts_neg = outcome_class == MON_DEGRADED
    same_day = state.last_outcome_at is not None and state.last_outcome_at.date() == day
    return _replace(
        state,
        outcome_count=state.outcome_count + (1 if valid else 0),
        distinct_days=state.distinct_days + (0 if (not valid or same_day) else 1),
        improved_count=state.improved_count + (1 if outcome_class == MON_IMPROVED else 0),
        no_degradation_count=state.no_degradation_count + (1 if outcome_class == MON_NO_DEGRADATION else 0),
        inconclusive_count=state.inconclusive_count + (1 if outcome_class in (MON_INCONCLUSIVE, MON_UNAVAILABLE) else 0),
        degraded_count=state.degraded_count + (1 if counts_neg else 0),
        degraded_distinct_days=state.degraded_distinct_days + (
            1 if counts_neg and not same_day else 0),
        preference_rejection_count=state.preference_rejection_count + (1 if open_more_rejection else 0),
        movement_count=state.movement_count + (1 if moved else 0),
        last_outcome_at=now, monitoring_started_at=state.monitoring_started_at or now)


def evaluate_strategy_confirmation(
    *, stage: int, activated_at: datetime | None, monitoring: StrategyMonitoringState, now: datetime,
) -> bool:
    if activated_at is None:
        return False
    if stage == 2:
        min_days, min_out, min_dd = CONFIRM_S2_DAYS, CONFIRM_S2_OUTCOMES, CONFIRM_S2_DISTINCT_DAYS
    else:
        min_days, min_out, min_dd = CONFIRM_S1_DAYS, CONFIRM_S1_OUTCOMES, CONFIRM_S1_DISTINCT_DAYS
    elapsed = (now - activated_at).total_seconds() / 86400.0
    return (elapsed >= min_days and monitoring.outcome_count >= min_out
            and monitoring.distinct_days >= min_dd and monitoring.degraded_count == 0
            and monitoring.preference_rejection_count == 0)


@dataclass(frozen=True)
class StrategyMonitoringActionInput:
    stage: int
    learning_enabled: bool
    config_generation_matches: bool
    reference_valid: bool
    context_compatible: bool
    sensor_available: bool
    forecast_trust_ok: bool
    open_more_rejection_now: bool
    monitoring: StrategyMonitoringState


def evaluate_strategy_monitoring_action(inp: StrategyMonitoringActionInput) -> tuple[str, str | None]:
    if not inp.config_generation_matches:
        return (ACTION_INVALIDATE, "config_generation_changed")
    if not inp.reference_valid:
        return (ACTION_INVALIDATE, "reference_invalid")
    if inp.open_more_rejection_now:
        return (ACTION_FULL_ROLLBACK, "preference_open_more_rejection")
    if not inp.learning_enabled:
        return (ACTION_TEMPORARY_SUSPEND, "learning_mode_off")
    if not inp.context_compatible:
        return (ACTION_TEMPORARY_SUSPEND, "context_incompatible")
    if not inp.sensor_available:
        return (ACTION_TEMPORARY_SUSPEND, "sensor_unavailable")
    if not inp.forecast_trust_ok:
        return (ACTION_TEMPORARY_SUSPEND, "forecast_trust_insufficient")
    m = inp.monitoring
    if (m.degraded_count >= ROLLBACK_DEGRADED_MIN
            and m.degraded_distinct_days >= ROLLBACK_DEGRADED_DISTINCT_DAYS):
        return (ACTION_FULL_ROLLBACK, "repeated_degradation")
    if (inp.stage == 2 and m.degraded_count >= REDUCE_DEGRADED_MIN
            and m.degraded_distinct_days >= REDUCE_DEGRADED_DISTINCT_DAYS):
        return (ACTION_REDUCE_ONE_STEP, "weak_repeated_degradation")
    return (ACTION_RETAIN, None)


# ---------------------------------------------------------------------------
# Cause → family routing
# ---------------------------------------------------------------------------

from ..models.strategy_learning import (
    FAMILY_ENTRY_TIMING, FAMILY_EXIT_TIMING, FAMILY_TIER_CHOICE, FAMILY_MINIMUM_HOLD,
)
from ..engines.thermal_insufficiency import (
    CAUSE_LATE_ENTRY, CAUSE_INSUFFICIENT_INTENSITY, CAUSE_EXCESSIVE_LOAD_DURATION,
)

# Over-shading mirror causes (sharpening 8).
CAUSE_UNNECESSARY_EARLY_ENTRY = "unnecessary_early_entry"
CAUSE_EXCESSIVE_INTENSITY = "excessive_intensity_choice"
CAUSE_LATE_RELEASE = "late_release"
CAUSE_EXCESSIVE_HOLD = "excessive_hold"

# (family, direction_sign) — sign +1 = stronger/earlier-shade; -1 = weaker/later.
_CAUSE_FAMILY_MAP: dict[str, tuple[str, int]] = {
    CAUSE_LATE_ENTRY: (FAMILY_ENTRY_TIMING, -1),            # enter earlier (lower minutes)
    CAUSE_INSUFFICIENT_INTENSITY: (FAMILY_TIER_CHOICE, 1),  # stronger tier
    CAUSE_EXCESSIVE_LOAD_DURATION: (FAMILY_ENTRY_TIMING, -1),
    CAUSE_UNNECESSARY_EARLY_ENTRY: (FAMILY_ENTRY_TIMING, 1),  # enter later
    CAUSE_EXCESSIVE_INTENSITY: (FAMILY_TIER_CHOICE, -1),      # weaker tier
    CAUSE_LATE_RELEASE: (FAMILY_EXIT_TIMING, -1),            # release earlier
    CAUSE_EXCESSIVE_HOLD: (FAMILY_MINIMUM_HOLD, -1),         # shorter hold
}


def route_cause_to_family(cause: str) -> tuple[str, int] | None:
    """Map a thermal-insufficiency / over-shading cause to (family, direction).

    Causes owned by P7/P8 (insufficient_position) or non-actionable
    (wrong window / outdoor / inertia / confounded / unavailable) return None."""
    return _CAUSE_FAMILY_MAP.get(cause)


# ---------------------------------------------------------------------------
# Cooldown + reconciliation
# ---------------------------------------------------------------------------

def rollback_cooldown_until(now: datetime) -> datetime:
    return now + timedelta(days=ROLLBACK_COOLDOWN_DAYS)


def is_cooldown_active(cooldown_until: datetime | None, now: datetime) -> bool:
    return cooldown_until is not None and now < cooldown_until


def reconcile_restored_strategy_experiments(experiments: list, now: datetime) -> tuple[dict, list]:
    """Restart safety for strategy experiments (mirror of P7)."""
    from dataclasses import replace as _replace
    from ..models.strategy_learning import (
        EXP_ACTIVATED, EXP_ARMED, EXP_INTERRUPTED_PARTIAL, EXP_OBSERVING, EXP_PLANNED,
    )
    active: dict = {}
    history: list = []
    for e in experiments:
        if e.status in (EXP_PLANNED, EXP_ARMED):
            active[e.zone_id] = e
        elif e.status in (EXP_ACTIVATED, EXP_OBSERVING):
            history.append(_replace(
                e, status=EXP_INTERRUPTED_PARTIAL, abort_reason="interrupted_by_restart",
                rollback_state="logical", completed_at=(e.completed_at or now), updated_at=now))
        else:
            history.append(e)
    return (active, history)


def reconcile_restored_strategy_adoptions(adoptions: list, now: datetime) -> tuple[dict, list]:
    """Restart safety for strategy adoptions: never blindly reactivated."""
    from dataclasses import replace as _replace
    active: dict = {}
    history: list = []
    for a in adoptions:
        if a.status in AD_TERMINAL_STATUSES:
            history.append(a)
        else:
            active[a.adoption_key] = _replace(
                a, suspended=True, current_gate_reason="awaiting_restart_revalidation", updated_at=now)
    return (active, history)
