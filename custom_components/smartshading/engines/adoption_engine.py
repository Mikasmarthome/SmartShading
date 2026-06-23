"""Adoption engine — LE 2.0 / Phase P8 (pure).

Exact multi-experiment evidence evaluation + permanent consumption ledger,
dimension-specific monitoring, confirmation gates, suspend/reduce/rollback
decisions (robust negative evidence only), experiment-need suppression,
cooldown and restart reconciliation.  No Home Assistant import, no mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models.bounded_experiment import (
    EVAL_DEGRADED,
    EVAL_IMPROVED,
    EVAL_NO_DEGRADATION,
    EVAL_PREFERENCE_REJECTED,
)
from ..models.persistent_adoption import (
    ACTION_CONFIRM,
    ACTION_FULL_ROLLBACK,
    ACTION_INVALIDATE,
    ACTION_REDUCE_ONE_STEP,
    ACTION_RETAIN,
    ACTION_TEMPORARY_SUSPEND,
    ADOPTION_MAX_DELTA_HA,
    ADOPTION_STEP_HA,
    CONFIRM_S1_DAYS,
    CONFIRM_S1_DISTINCT_DAYS,
    CONFIRM_S1_OUTCOMES,
    CONFIRM_S2_DAYS,
    CONFIRM_S2_DISTINCT_DAYS,
    CONFIRM_S2_OUTCOMES,
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
    S2_STABILITY_DAYS,
    AdoptionMonitoringState,
)

# Outcome classes used by monitoring (production observations under adoption).
MON_IMPROVED: str = "improved"
MON_NO_DEGRADATION: str = "no_degradation"
MON_INCONCLUSIVE: str = "inconclusive"
MON_DEGRADED: str = "degraded"
MON_UNAVAILABLE: str = "unavailable"
MON_CONFOUNDED: str = "confounded"


# ---------------------------------------------------------------------------
# Evidence evaluation + consumption
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentEvidence:
    experiment_id: str
    decision_class: str               # ExperimentEvaluation.decision (EVAL_*)
    day: object                       # date
    reliability: float
    confidence: float
    context_family: str
    config_generation: int
    decision_id: str | None = None
    shadow_id: str | None = None


@dataclass(frozen=True)
class AdoptionEvidenceResult:
    sufficient: bool
    selected_experiment_ids: tuple[str, ...] = ()
    distinct_days: int = 0
    improved_count: int = 0
    no_degradation_count: int = 0
    inconclusive_count: int = 0
    degraded_count: int = 0
    preference_rejection_count: int = 0
    confidence: float = 0.0
    reliability: float = 0.0
    validated_context_families: tuple[str, ...] = ()
    block_reason: str | None = None


def evaluate_adoption_evidence(
    experiments: list[ExperimentEvidence],
    *,
    stage: int,
    consumed_ids: frozenset[str],
    config_generation: int,
) -> AdoptionEvidenceResult:
    """Fresh, exact evaluation of terminal P7 experiments for one adoption stage.

    Only NON-consumed experiments of the current config generation count.  Any
    degraded or preference-rejected experiment in the comparable series blocks
    the stage (conservative).  A single experiment can never satisfy a stage.
    """
    if stage == 2:
        min_exp, min_days, min_improved = S2_MIN_EXPERIMENTS, S2_MIN_DISTINCT_DAYS, S2_MIN_IMPROVED
        min_conf, min_rel = S2_MIN_CONFIDENCE, S1_MIN_RELIABILITY
    else:
        min_exp, min_days, min_improved = S1_MIN_EXPERIMENTS, S1_MIN_DISTINCT_DAYS, S1_MIN_IMPROVED
        min_conf, min_rel = S1_MIN_CONFIDENCE, S1_MIN_RELIABILITY

    pool = [
        e for e in experiments
        if e.experiment_id not in consumed_ids and e.config_generation == config_generation
    ]
    degraded = [e for e in pool if e.decision_class == EVAL_DEGRADED]
    pref_rej = [e for e in pool if e.decision_class == EVAL_PREFERENCE_REJECTED]
    if degraded:
        return AdoptionEvidenceResult(False, degraded_count=len(degraded),
                                      block_reason="degraded_experiment_present")
    if pref_rej:
        return AdoptionEvidenceResult(False, preference_rejection_count=len(pref_rej),
                                      block_reason="preference_rejection_present")

    valid = [e for e in pool if e.decision_class in (EVAL_IMPROVED, EVAL_NO_DEGRADATION)]
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
    return AdoptionEvidenceResult(
        sufficient=sufficient,
        selected_experiment_ids=tuple(e.experiment_id for e in valid) if sufficient else (),
        distinct_days=len(days), improved_count=len(improved),
        no_degradation_count=len(no_deg), degraded_count=0, preference_rejection_count=0,
        confidence=round(avg_conf, 4), reliability=round(avg_rel, 4),
        validated_context_families=tuple(sorted({e.context_family for e in valid})),
        block_reason=reason,
    )


# ---------------------------------------------------------------------------
# Monitoring update (dimension-specific; missing/confounded never negative)
# ---------------------------------------------------------------------------

def classify_monitoring_outcome(
    *,
    thermal_available: bool,
    thermal_score: float | None,
    confounded: bool,
    open_more_rejection: bool,
) -> str:
    if open_more_rejection:
        return MON_DEGRADED  # preference rejection handled separately; flagged too
    if confounded:
        return MON_CONFOUNDED
    if not thermal_available or thermal_score is None:
        return MON_UNAVAILABLE
    if thermal_score >= 0.15:
        return MON_IMPROVED
    if thermal_score <= -0.15:
        return MON_DEGRADED
    return MON_NO_DEGRADATION


def update_monitoring(
    state: AdoptionMonitoringState,
    *,
    outcome_class: str,
    open_more_rejection: bool,
    day: object,
    now: datetime,
) -> AdoptionMonitoringState:
    from dataclasses import replace as _replace

    counts_negative = outcome_class == MON_DEGRADED
    # distinct days only advance on a *valid* (non-unavailable/confounded) outcome.
    valid = outcome_class in (MON_IMPROVED, MON_NO_DEGRADATION, MON_DEGRADED)
    started = state.monitoring_started_at or now
    new_distinct = state.distinct_days + (0 if not valid else (
        0 if (state.last_outcome_at is not None and state.last_outcome_at.date() == day) else 1))
    return _replace(
        state,
        outcome_count=state.outcome_count + (1 if valid else 0),
        distinct_days=new_distinct,
        improved_count=state.improved_count + (1 if outcome_class == MON_IMPROVED else 0),
        no_degradation_count=state.no_degradation_count + (1 if outcome_class == MON_NO_DEGRADATION else 0),
        inconclusive_count=state.inconclusive_count + (1 if outcome_class in (MON_INCONCLUSIVE, MON_UNAVAILABLE) else 0),
        degraded_count=state.degraded_count + (1 if counts_negative else 0),
        degraded_distinct_days=state.degraded_distinct_days + (
            1 if counts_negative and (state.last_outcome_at is None or state.last_outcome_at.date() != day) else 0),
        preference_rejection_count=state.preference_rejection_count + (1 if open_more_rejection else 0),
        last_outcome_at=now, monitoring_started_at=started,
    )


# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------

def evaluate_confirmation(
    *,
    stage: int,
    activated_at: datetime | None,
    monitoring: AdoptionMonitoringState,
    now: datetime,
) -> bool:
    if activated_at is None:
        return False
    if stage == 2:
        min_days, min_out, min_dd = CONFIRM_S2_DAYS, CONFIRM_S2_OUTCOMES, CONFIRM_S2_DISTINCT_DAYS
    else:
        min_days, min_out, min_dd = CONFIRM_S1_DAYS, CONFIRM_S1_OUTCOMES, CONFIRM_S1_DISTINCT_DAYS
    elapsed_days = (now - activated_at).total_seconds() / 86400.0
    return (
        elapsed_days >= min_days
        and monitoring.outcome_count >= min_out
        and monitoring.distinct_days >= min_dd
        and monitoring.degraded_count == 0
        and monitoring.preference_rejection_count == 0
    )


# ---------------------------------------------------------------------------
# Monitoring action (robust negative evidence only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MonitoringActionInput:
    stage: int
    learning_enabled: bool
    config_generation_matches: bool
    reference_valid: bool
    context_compatible: bool
    sensor_available: bool
    open_more_rejection_now: bool       # a clear manual open_more against this adoption
    monitoring: AdoptionMonitoringState


def evaluate_monitoring_action(inp: MonitoringActionInput) -> tuple[str, str | None]:
    """Decide retain/suspend/reduce/rollback/invalidate.  Missing/confounded data
    is never treated as negative evidence."""
    # Structural invalidation first.
    if not inp.config_generation_matches:
        return (ACTION_INVALIDATE, "config_generation_changed")
    if not inp.reference_valid:
        return (ACTION_INVALIDATE, "reference_invalid")
    # Immediate full rollback on a clear manual open_more rejection.
    if inp.open_more_rejection_now:
        return (ACTION_FULL_ROLLBACK, "preference_open_more_rejection")
    # Transient suspension (never rollback) for non-negative conditions.
    if not inp.learning_enabled:
        return (ACTION_TEMPORARY_SUSPEND, "learning_mode_off")
    if not inp.context_compatible:
        return (ACTION_TEMPORARY_SUSPEND, "context_incompatible")
    if not inp.sensor_available:
        return (ACTION_TEMPORARY_SUSPEND, "sensor_unavailable")
    m = inp.monitoring
    # Robust thermal degradation → rollback (or reduce at stage 2).
    if (m.degraded_count >= ROLLBACK_DEGRADED_MIN
            and m.degraded_distinct_days >= ROLLBACK_DEGRADED_DISTINCT_DAYS):
        return (ACTION_FULL_ROLLBACK, "repeated_thermal_degradation")
    if (inp.stage == 2
            and m.degraded_count >= REDUCE_DEGRADED_MIN
            and m.degraded_distinct_days >= REDUCE_DEGRADED_DISTINCT_DAYS):
        return (ACTION_REDUCE_ONE_STEP, "weak_repeated_degradation")
    return (ACTION_RETAIN, None)


# ---------------------------------------------------------------------------
# Experiment-need suppression
# ---------------------------------------------------------------------------

NEED_NO_ADOPTION: str = "no_adoption"
NEED_STAGE1_VALIDATING: str = "stage_one_validating"
NEED_STAGE2_JUSTIFIED: str = "stage_two_evidence_justified"
NEED_MAX_REACHED: str = "maximum_adoption_reached"
NEED_REVALIDATION: str = "revalidation_required"
NEED_STABLE_SUPPRESS: str = "stable_adoption_no_experiment_needed"
NEED_MONITORING_INCOMPLETE: str = "monitoring_period_incomplete"
NEED_STAGE2_INSUFFICIENT: str = "stage_two_evidence_insufficient"


@dataclass(frozen=True)
class ExperimentNeedInput:
    has_adoption: bool
    stage: int
    status: str                          # adoption status
    confirmed_stable: bool               # stage-1 confirmed and stable long enough
    activated_at: datetime | None
    repeated_underprotection: bool       # robust under-shading despite adoption
    new_independent_supported_evidence: bool
    revalidation_required: bool
    now: datetime


def evaluate_experiment_need(inp: ExperimentNeedInput) -> tuple[bool, str]:
    """Return (experiments_allowed, reason).  Suppresses needless experiments once
    an adoption is stable; only justified cases reopen experimentation."""
    if not inp.has_adoption:
        return (True, NEED_NO_ADOPTION)
    if inp.stage >= 2:
        return (False, NEED_MAX_REACHED)
    # stage 1 present
    if not inp.confirmed_stable:
        return (False, NEED_STAGE1_VALIDATING)
    # confirmed + stable stage 1: only specific justifications reopen
    if inp.revalidation_required or inp.repeated_underprotection:
        return (True, NEED_REVALIDATION)
    stable_long_enough = (
        inp.activated_at is not None
        and (inp.now - inp.activated_at) >= timedelta(days=S2_STABILITY_DAYS)
    )
    if inp.new_independent_supported_evidence and stable_long_enough:
        return (True, NEED_STAGE2_JUSTIFIED)
    if inp.new_independent_supported_evidence and not stable_long_enough:
        return (False, NEED_MONITORING_INCOMPLETE)
    return (False, NEED_STABLE_SUPPRESS)


# ---------------------------------------------------------------------------
# Cooldown + restart reconciliation
# ---------------------------------------------------------------------------

def rollback_cooldown_until(now: datetime) -> datetime:
    return now + timedelta(days=ROLLBACK_COOLDOWN_DAYS)


def is_cooldown_active(cooldown_until: datetime | None, now: datetime) -> bool:
    return cooldown_until is not None and now < cooldown_until


def reconcile_restored_adoptions(adoptions: list, now: datetime) -> tuple[dict, list]:
    """Restart safety: active adoptions are loaded but NEVER blindly applied —
    they are marked suspended pending fresh revalidation on the next cycle.
    Terminal adoptions go to history."""
    from dataclasses import replace as _replace

    from ..models.persistent_adoption import TERMINAL_STATUSES

    active: dict = {}
    history: list = []
    for a in adoptions:
        if a.status in TERMINAL_STATUSES:
            history.append(a)
        else:
            active[a.adoption_key] = _replace(
                a, suspended=True, current_gate_reason="awaiting_restart_revalidation",
                updated_at=now)
    return (active, history)
