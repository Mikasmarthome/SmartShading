"""Experiment engine — LE 2.0 / Phase P7 (pure).

Selection / zone-lock ranking, activation-timing gate, candidate revalidation
(reusing the real shadow dry-run), honest non-causal baseline comparison +
evaluation, cooldown logic, the logical-rollback state machine and the P8
adoption-eligibility snapshot.  No Home Assistant import, no runtime mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models.bounded_experiment import (
    EVAL_DEGRADED,
    EVAL_IMPROVED,
    EVAL_INCONCLUSIVE,
    EVAL_INVALID,
    EVAL_NO_DEGRADATION,
    EVAL_PREFERENCE_REJECTED,
    EXPERIMENT_CUMULATIVE_CAP_HA,
    EXPERIMENT_MATERIALITY_HA,
    EXPERIMENT_STEP_HA,
    MAX_EXPERIMENTS_PER_WINDOW_PER_30D,
    P8_MIN_CONFIDENCE,
    P8_MIN_DISTINCT_DAYS,
    P8_MIN_VALID_EXPERIMENTS,
    REJECTION_COOLDOWN_DAYS,
    ROLLBACK_COMPLETE,
    ROLLBACK_LOGICAL,
    ROLLBACK_NONE,
    ROLLBACK_PHYSICAL_PENDING,
    WINDOW_CONTEXT_COOLDOWN_DAYS,
    ZONE_COOLDOWN_S,
    ExperimentEvaluation,
)
from ..models.cover_group import CoverHardwareType
from ..state_machine.states import ShadingState
from .shadow_engine import compute_shadow_candidate

# Minimum robust baseline before any non-inconclusive verdict is allowed.
MIN_BASELINE_SAMPLES: int = 3
MIN_BASELINE_DAYS: int = 2
# Normalized thermal-score margins (higher score = better outcome).
IMPROVE_MARGIN: float = 0.15
DEGRADE_MARGIN: float = 0.15


# ---------------------------------------------------------------------------
# Selection / zone-lock ranking
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentCandidate:
    """One rankable experiment candidate for a zone's single slot."""

    window_id: str
    intensity_level: str
    context_family: str
    shadow_confidence: float
    contribution_confidence: float
    planned_age_s: float
    recently_experimented: bool   # anti-starvation: deprioritize last-run window


def rank_experiment_candidates(
    candidates: list[ExperimentCandidate],
) -> list[ExperimentCandidate]:
    """Deterministic priority: not-recently-run first, then higher confidence,
    then higher contribution confidence, then older planned candidate."""
    return sorted(
        candidates,
        key=lambda c: (
            c.recently_experimented,          # False (0) before True (1)
            -round(c.shadow_confidence, 6),
            -round(c.contribution_confidence, 6),
            -round(c.planned_age_s, 3),
        ),
    )


# ---------------------------------------------------------------------------
# Activation-timing gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActivationTimingInput:
    context_reentered: bool             # shadow context family currently present
    decision_matches_intensity: bool   # regular deterministic decision == proposal intensity
    regular_target_comparable: bool    # regular target ~ proposal baseline
    solar_stable_sufficient: bool      # load high enough and not strongly fluctuating
    thermal_outcome_observable: bool   # P4 can observe an outcome
    sufficient_observation_time: bool  # enough time before night/lifecycle transition
    higher_authority_moving: bool      # window already moved by a higher authority
    command_filter_would_allow: bool


def evaluate_activation_timing(inp: ActivationTimingInput) -> tuple[bool, str | None]:
    checks = [
        (inp.context_reentered, "context_not_present"),
        (inp.decision_matches_intensity, "intensity_mismatch"),
        (inp.regular_target_comparable, "regular_target_not_comparable"),
        (inp.solar_stable_sufficient, "solar_unstable_or_low"),
        (inp.thermal_outcome_observable, "no_observable_thermal_outcome"),
        (inp.sufficient_observation_time, "insufficient_observation_time"),
        (not inp.higher_authority_moving, "higher_authority_moving"),
        (inp.command_filter_would_allow, "command_filter_blocks"),
    ]
    for ok, code in checks:
        if not ok:
            return (False, code)
    return (True, None)


# ---------------------------------------------------------------------------
# Candidate revalidation (immediately before activation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentCandidateResult:
    valid: bool
    experiment_parameter_target_ha: int | None
    expected_final_candidate_target_ha: int | None
    effective_delta_ha: int | None
    cumulative_delta_from_config_ha: int | None
    block_reason: str | None = None


def revalidate_experiment_candidate(
    *,
    current_authoritative_target_ha: int,
    real_regular_target_ha: int,
    configured_base_target_ha: int,
    new_state: ShadingState,
    daytime_min_ha: int | None,
    ahb_position_ha: int | None,
    ahb_enabled: bool,
    hardware_type: CoverHardwareType,
    in_solar_sector: bool,
    effective_exposure_wm2: float | None,
    step_ha: int = EXPERIMENT_STEP_HA,
) -> ExperimentCandidateResult:
    """Re-simulate the close-more candidate through the REAL clamps now.

    Never blindly reuses the P6-stored value.  Enforces the cumulative cap and
    materiality after the real guardrails.  ``step_ha`` is the close-more
    magnitude (Stage 1 = 5; a bounded Stage 2 = 10).  The cumulative cap vs the
    configured base (``EXPERIMENT_CUMULATIVE_CAP_HA`` = 10) is enforced after the
    real guardrails, so a larger step can never exceed the total deviation bound.
    """
    dry = compute_shadow_candidate(
        current_authoritative_target_ha=current_authoritative_target_ha,
        real_applied_target_ha=real_regular_target_ha,
        configured_base_target_ha=configured_base_target_ha,
        new_state=new_state,
        daytime_min_ha=daytime_min_ha,
        ahb_position_ha=ahb_position_ha,
        ahb_enabled=ahb_enabled,
        hardware_type=hardware_type,
        in_solar_sector=in_solar_sector,
        effective_exposure_wm2=effective_exposure_wm2,
        step_ha=step_ha,
    )
    final = dry.shadow_final_candidate_target_ha
    cumulative = (
        configured_base_target_ha - final if final is not None else None
    )
    if not dry.valid:
        return ExperimentCandidateResult(
            valid=False,
            experiment_parameter_target_ha=dry.shadow_parameter_target_ha,
            expected_final_candidate_target_ha=final,
            effective_delta_ha=dry.net_delta_vs_real_ha,
            cumulative_delta_from_config_ha=cumulative,
            block_reason=dry.block_reason,
        )
    if cumulative is not None and cumulative > EXPERIMENT_CUMULATIVE_CAP_HA:
        return ExperimentCandidateResult(
            valid=False,
            experiment_parameter_target_ha=dry.shadow_parameter_target_ha,
            expected_final_candidate_target_ha=final,
            effective_delta_ha=dry.net_delta_vs_real_ha,
            cumulative_delta_from_config_ha=cumulative,
            block_reason="cumulative_cap_exceeded",
        )
    return ExperimentCandidateResult(
        valid=True,
        experiment_parameter_target_ha=dry.shadow_parameter_target_ha,
        expected_final_candidate_target_ha=final,
        effective_delta_ha=dry.net_delta_vs_real_ha,
        cumulative_delta_from_config_ha=cumulative,
    )


# ---------------------------------------------------------------------------
# Honest baseline comparison + evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentEvaluationInput:
    experiment_outcome_available: bool
    experiment_thermal_score: float | None
    experiment_preference_score: float | None
    experiment_movement_score: float | None
    baseline_thermal_scores: tuple[float, ...]
    baseline_distinct_days: int
    user_open_more_rejection: bool
    reliability: float
    confounders: tuple[str, ...] = ()


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def evaluate_experiment(inp: ExperimentEvaluationInput) -> ExperimentEvaluation:
    """Classify the experiment vs a robust baseline range.  Never claims an exact
    causal effect; a single comparison can at most reach improved/no_degradation
    relative to the baseline distribution, with non-causal limitations."""
    baseline = list(inp.baseline_thermal_scores)
    n = len(baseline)
    user_acceptance = "rejected" if inp.user_open_more_rejection else "unknown"

    # Preference rejection dominates every thermal reading.
    if inp.user_open_more_rejection:
        return ExperimentEvaluation(
            experiment_outcome_available=inp.experiment_outcome_available,
            experiment_thermal_score=inp.experiment_thermal_score,
            experiment_preference_score=inp.experiment_preference_score,
            experiment_movement_score=inp.experiment_movement_score,
            baseline_sample_count=n, baseline_distinct_days=inp.baseline_distinct_days,
            experiment_vs_baseline_class=EVAL_PREFERENCE_REJECTED,
            user_acceptance="rejected", reliability=inp.reliability, confidence=0.0,
            confounders=inp.confounders, decision=EVAL_PREFERENCE_REJECTED,
            p8_adoption_eligible=False,
        )

    if not inp.experiment_outcome_available or inp.experiment_thermal_score is None:
        return ExperimentEvaluation(
            experiment_outcome_available=inp.experiment_outcome_available,
            baseline_sample_count=n, baseline_distinct_days=inp.baseline_distinct_days,
            experiment_vs_baseline_class=EVAL_INVALID, user_acceptance=user_acceptance,
            reliability=inp.reliability, confidence=0.0, confounders=inp.confounders,
            decision=EVAL_INVALID, p8_adoption_eligible=False,
        )

    # Robust baseline required.
    if n < MIN_BASELINE_SAMPLES or inp.baseline_distinct_days < MIN_BASELINE_DAYS:
        return ExperimentEvaluation(
            experiment_outcome_available=True,
            experiment_thermal_score=inp.experiment_thermal_score,
            experiment_preference_score=inp.experiment_preference_score,
            experiment_movement_score=inp.experiment_movement_score,
            baseline_sample_count=n, baseline_distinct_days=inp.baseline_distinct_days,
            experiment_vs_baseline_class=EVAL_INCONCLUSIVE, user_acceptance=user_acceptance,
            reliability=inp.reliability, confidence=0.0, confounders=inp.confounders,
            decision=EVAL_INCONCLUSIVE, p8_adoption_eligible=False,
        )

    median = _median(baseline)
    score = inp.experiment_thermal_score
    if score >= median + IMPROVE_MARGIN:
        klass = EVAL_IMPROVED
    elif score <= median - DEGRADE_MARGIN:
        klass = EVAL_DEGRADED
    elif score >= median - DEGRADE_MARGIN:
        klass = EVAL_NO_DEGRADATION
    else:  # pragma: no cover - unreachable given the bands above
        klass = EVAL_INCONCLUSIVE

    confidence = min(1.0, n / 6.0) * min(1.0, inp.baseline_distinct_days / 3.0) * inp.reliability
    return ExperimentEvaluation(
        experiment_outcome_available=True,
        experiment_thermal_score=score,
        experiment_preference_score=inp.experiment_preference_score,
        experiment_movement_score=inp.experiment_movement_score,
        baseline_sample_count=n, baseline_distinct_days=inp.baseline_distinct_days,
        baseline_thermal_distribution={"median": round(median, 4), "n": n},
        experiment_vs_baseline_class=klass, user_acceptance=user_acceptance,
        reliability=inp.reliability, confidence=round(confidence, 4),
        confounders=inp.confounders, decision=klass,
        # Single experiment never unlocks adoption — P8 needs repeated evidence.
        p8_adoption_eligible=False,
    )


# ---------------------------------------------------------------------------
# Causal same-cycle evaluation (Increment 3H)
# ---------------------------------------------------------------------------
# The window-wide / context-aggregate baseline median (above) systematically
# disadvantages a bounded close-more experiment: the experiment is always
# observed on the HARDEST (rising-forcing, near-noon) cycle, but compared against
# a baseline median that mixes easier cycles, so a genuinely-helpful step looks
# merely neutral.  The causal same-cycle comparison instead asks: for THIS
# observation window, how would the room have responded at the BASELINE (more
# open) position?  Only the cover position differs; the exogenous inputs (solar,
# outdoor) are shared.  The counterfactual baseline delta is the observed
# experiment delta PLUS the solar warming the more-open baseline would have
# additionally admitted — estimated from the learned typical context response,
# scaled to this cycle's solar load and the known open-fraction delta.

# Bound on the modelled avoided warming (°C) — a single bounded step cannot
# plausibly avoid more than this; keeps a noisy coefficient from inventing credit.
AVOIDED_WARMING_CAP_C: float = 3.0
_THERMAL_LOAD_MIN_WM2: float = 150.0


@dataclass(frozen=True)
class CausalSameCycleInput:
    experiment_outcome_available: bool
    observed_experiment_delta_c: float | None
    observed_solar_wm2: float | None
    outdoor_temp_c: float | None
    baseline_open_fraction: float | None      # of_base (regular authoritative)
    experiment_open_fraction: float | None    # of_exp (more closed)
    # Learned typical context response, from the SAME context-family baseline
    # (non-experiment) outcomes — abs deltas + their solar levels + distinct days.
    baseline_abs_deltas: tuple[float, ...]
    baseline_solars: tuple[float, ...]
    baseline_distinct_days: int
    reliability: float
    user_open_more_rejection: bool
    confounders: tuple[str, ...] = ()


def evaluate_experiment_causal(inp: CausalSameCycleInput) -> ExperimentEvaluation:
    """Classify a bounded close-more experiment by a CAUSAL same-cycle comparison.

    Compares the experiment's observed thermal score against the counterfactual
    baseline score for the SAME window (only the cover position differs).  The
    counterfactual delta = observed delta + modelled avoided solar warming.  A
    thin context baseline yields ``inconclusive`` (it never silently falls back to
    a favourable window-wide median).  Because more shade can only reduce solar
    gain, this path never fabricates a thermal degradation; an adverse result is
    surfaced through the preference path (user open-more rejection)."""
    from .outcome_resolution import score_thermal_delta

    n = len(inp.baseline_abs_deltas)
    base_user_acc = "rejected" if inp.user_open_more_rejection else "unknown"

    if inp.user_open_more_rejection:
        return ExperimentEvaluation(
            experiment_outcome_available=inp.experiment_outcome_available,
            experiment_thermal_score=None, baseline_sample_count=n,
            baseline_distinct_days=inp.baseline_distinct_days,
            experiment_vs_baseline_class=EVAL_PREFERENCE_REJECTED,
            user_acceptance="rejected", reliability=inp.reliability,
            confounders=inp.confounders, decision=EVAL_PREFERENCE_REJECTED,
            p8_adoption_eligible=False)

    if not inp.experiment_outcome_available or inp.observed_experiment_delta_c is None:
        return ExperimentEvaluation(
            experiment_outcome_available=False, baseline_sample_count=n,
            baseline_distinct_days=inp.baseline_distinct_days,
            experiment_vs_baseline_class=EVAL_INVALID, user_acceptance=base_user_acc,
            reliability=inp.reliability, confounders=inp.confounders,
            decision=EVAL_INVALID, p8_adoption_eligible=False)

    # Thin context baseline → inconclusive (no favourable window-wide fallback).
    of_base = inp.baseline_open_fraction
    of_exp = inp.experiment_open_fraction
    if (n < MIN_BASELINE_SAMPLES or inp.baseline_distinct_days < MIN_BASELINE_DAYS
            or not inp.baseline_solars or inp.observed_solar_wm2 is None
            or of_base in (None, 0) or of_exp is None):
        return ExperimentEvaluation(
            experiment_outcome_available=True,
            experiment_thermal_score=None, baseline_sample_count=n,
            baseline_distinct_days=inp.baseline_distinct_days,
            baseline_thermal_distribution={"scope": "thin_context_baseline", "n": n},
            experiment_vs_baseline_class=EVAL_INCONCLUSIVE, user_acceptance=base_user_acc,
            reliability=inp.reliability, confounders=inp.confounders,
            decision=EVAL_INCONCLUSIVE, p8_adoption_eligible=False)

    s_typ = _median(list(inp.baseline_solars))
    typ_resp = _median([abs(d) for d in inp.baseline_abs_deltas])
    d_of = max(0.0, of_base - of_exp)
    has_load = inp.observed_solar_wm2 >= _THERMAL_LOAD_MIN_WM2

    # Per-cycle avoided warming: learned typical response, scaled by the position
    # delta (relative to the baseline opening) and this cycle's solar vs typical.
    if s_typ and s_typ > 0:
        avoided = typ_resp * (d_of / of_base) * (inp.observed_solar_wm2 / s_typ)
    else:
        avoided = 0.0
    avoided = max(0.0, min(AVOIDED_WARMING_CAP_C, avoided))

    obs_delta = float(inp.observed_experiment_delta_c)
    cf_delta = obs_delta + avoided  # counterfactual (more open) admits more warming

    exp_score = score_thermal_delta(
        obs_delta, has_load=has_load, outdoor_temperature_c=inp.outdoor_temp_c)
    base_score = score_thermal_delta(
        cf_delta, has_load=has_load, outdoor_temperature_c=inp.outdoor_temp_c)

    if exp_score >= base_score + IMPROVE_MARGIN:
        klass = EVAL_IMPROVED
    elif exp_score <= base_score - DEGRADE_MARGIN:
        klass = EVAL_DEGRADED
    else:
        klass = EVAL_NO_DEGRADATION

    confidence = (min(1.0, n / 6.0) * min(1.0, inp.baseline_distinct_days / 3.0)
                  * inp.reliability)
    return ExperimentEvaluation(
        experiment_outcome_available=True,
        experiment_thermal_score=round(exp_score, 4),
        baseline_sample_count=n, baseline_distinct_days=inp.baseline_distinct_days,
        baseline_thermal_distribution={
            "scope": "causal_same_cycle",
            "counterfactual_baseline_score": round(base_score, 4),
            "avoided_warming_c": round(avoided, 4),
            "typical_response_c": round(typ_resp, 4),
            "typical_solar_wm2": round(s_typ, 1), "n": n},
        experiment_vs_baseline_class=klass, user_acceptance=base_user_acc,
        reliability=inp.reliability, confidence=round(confidence, 4),
        confounders=inp.confounders, decision=klass, p8_adoption_eligible=False)


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

def is_cooldown_active(
    *,
    now: datetime,
    last_zone_activation_at: datetime | None,
    last_context_completion_at: datetime | None,
    last_rejection_at: datetime | None,
    window_activations_last_30d: int,
) -> tuple[bool, str | None]:
    """Return (active, reason) for cooldown gating of one (window,intensity,context)."""
    if last_rejection_at is not None and now - last_rejection_at < timedelta(days=REJECTION_COOLDOWN_DAYS):
        return (True, "rejection_cooldown")
    if last_zone_activation_at is not None and (now - last_zone_activation_at).total_seconds() < ZONE_COOLDOWN_S:
        return (True, "zone_cooldown")
    if last_context_completion_at is not None and now - last_context_completion_at < timedelta(days=WINDOW_CONTEXT_COOLDOWN_DAYS):
        return (True, "context_cooldown")
    if window_activations_last_30d >= MAX_EXPERIMENTS_PER_WINDOW_PER_30D:
        return (True, "window_quota_reached")
    return (False, None)


# ---------------------------------------------------------------------------
# Logical-rollback state machine (no proactive opening command)
# ---------------------------------------------------------------------------

def next_rollback_state(
    current: str,
    *,
    regular_decision_wants_more_open: bool,
    regular_movement_allowed: bool,
) -> str:
    """Advance the rollback state.

    Logical rollback removes the experiment authority immediately.  Physical
    opening only completes when a *regular* decision asks for a more-open
    position and the command path allows it — never proactively forced.
    """
    if current in (ROLLBACK_NONE,):
        return ROLLBACK_LOGICAL
    if current == ROLLBACK_LOGICAL:
        if regular_decision_wants_more_open and regular_movement_allowed:
            return ROLLBACK_COMPLETE
        if regular_decision_wants_more_open:
            return ROLLBACK_PHYSICAL_PENDING
        return ROLLBACK_LOGICAL
    if current == ROLLBACK_PHYSICAL_PENDING:
        if regular_decision_wants_more_open and regular_movement_allowed:
            return ROLLBACK_COMPLETE
        return ROLLBACK_PHYSICAL_PENDING
    return current


# ---------------------------------------------------------------------------
# P8 adoption-eligibility snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class P8AdoptionInput:
    valid_non_degraded_experiments: int
    distinct_days: int
    any_preference_rejection: bool
    any_degraded: bool
    min_confidence_seen: float


def derive_p8_adoption_eligible(inp: P8AdoptionInput) -> bool:
    """Fresh snapshot only — P8 must re-derive from current data."""
    if inp.any_preference_rejection or inp.any_degraded:
        return False
    return (
        inp.valid_non_degraded_experiments >= P8_MIN_VALID_EXPERIMENTS
        and inp.distinct_days >= P8_MIN_DISTINCT_DAYS
        and inp.min_confidence_seen >= P8_MIN_CONFIDENCE
    )


def reconcile_restored_experiments(experiments: list, now: datetime) -> tuple[dict, list]:
    """Apply the restart safety rule to restored bounded experiments.

    planned/armed → kept active (revalidated fresh on the next cycle).
    activated/observing → causality is broken by the restart; NEVER resumed as a
    complete experiment → demoted to interrupted_partial (logical rollback).
    terminal → history.  Restore alone never re-injects a target.
    """
    from dataclasses import replace as _replace

    from ..models.bounded_experiment import (
        STATUS_ACTIVATED,
        STATUS_ARMED,
        STATUS_INTERRUPTED_PARTIAL,
        STATUS_OBSERVING,
        STATUS_PLANNED,
    )

    active: dict = {}
    history: list = []
    for e in experiments:
        if e.status in (STATUS_PLANNED, STATUS_ARMED):
            active[e.zone_id] = e
        elif e.status in (STATUS_ACTIVATED, STATUS_OBSERVING):
            history.append(_replace(
                e, status=STATUS_INTERRUPTED_PARTIAL,
                abort_reason="interrupted_by_restart", rollback_state="logical",
                completed_at=(e.completed_at or now), updated_at=now,
            ))
        else:
            history.append(e)
    return (active, history)


# Re-exported for the coordinator's single injection point.
__all__ = [
    "EXPERIMENT_STEP_HA", "EXPERIMENT_MATERIALITY_HA",
    "ExperimentCandidate", "rank_experiment_candidates",
    "ActivationTimingInput", "evaluate_activation_timing",
    "ExperimentCandidateResult", "revalidate_experiment_candidate",
    "ExperimentEvaluationInput", "evaluate_experiment",
    "is_cooldown_active", "next_rollback_state",
    "P8AdoptionInput", "derive_p8_adoption_eligible",
]
