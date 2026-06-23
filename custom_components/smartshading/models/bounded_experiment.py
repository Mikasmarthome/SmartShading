"""Bounded Experiment models — LE 2.0 / Phase P7.

A BoundedExperiment is the safe, persistent, fully-auditable record of a single
real thermal cover experiment.  For the first time a P6-supported shadow
candidate may be fed — strictly bounded — into the real target chain.

Hard invariants (P7 specification):
  - Thermal experiments are CLOSE-MORE only: exactly one fixed parameter step of
    -5 percentage points (HA convention: lower value = more closed).  No opening
    experiments, no variable/contribution-proportional steps, no tilt/threshold
    experiments.
  - Cumulative active deviation from the config base never exceeds 10 pp.
  - At most one active experiment per zone.
  - The experiment is injected as a Tier-5 parameter BELOW every higher
    authority (Safety > Override > Lifecycle > Absence > Behavior > manual
    preference > protection floors/clamps > regular tier decision > experiment >
    dispatch).  It can never bypass clamps, harmonization or the command filter.
  - Outcome linkage is exact via experiment_id/decision_id — never a timestamp
    fallback.
  - Evaluation is never an exact causal claim: limitations always contains
    'not_causally_validated'.
  - P7 NEVER creates a persistent target adaptation — that is P8.
    accepted_for_p8 means only "P8 may consider persistent adoption".

No Home Assistant import.  Frozen dataclasses.  HA position convention
(0=closed, 100=open).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

EXPERIMENT_SCHEMA_VERSION: int = 1

# --- bounded parameters (HA percentage points) ---
EXPERIMENT_STEP_HA: int = 5              # single fixed close-more step magnitude
EXPERIMENT_DELTA_HA: int = -EXPERIMENT_STEP_HA   # signed close-more delta (lower HA)
EXPERIMENT_CUMULATIVE_CAP_HA: int = 10   # max cumulative deviation vs config base
EXPERIMENT_MATERIALITY_HA: int = 3       # below this an effective delta is not material

DIRECTION_CLOSE_MORE: str = "close_more"

# --- retention / cooldown (seconds / days) — justified in the P7 plan ---
ZONE_COOLDOWN_S: int = 24 * 3600         # >=1 day between activations per zone
WINDOW_CONTEXT_COOLDOWN_DAYS: int = 7    # >=7 days between same (window,intensity,context)
MAX_EXPERIMENTS_PER_WINDOW_PER_30D: int = 3
REJECTION_COOLDOWN_DAYS: int = 30        # long block after rejection/degraded
EXPERIMENT_AGE_CAP_DAYS: int = 365
EXPERIMENT_HISTORY_PER_WINDOW: int = 30

# --- P8 adoption preparation gates (snapshot only) ---
P8_MIN_VALID_EXPERIMENTS: int = 3
P8_MIN_DISTINCT_DAYS: int = 3
P8_MIN_CONFIDENCE: float = 0.6

# --- state machine ---
STATUS_PLANNED: str = "planned"
STATUS_ARMED: str = "armed"               # fresh eligibility passed; waiting for activation context
STATUS_ACTIVATED: str = "activated"       # parameter injected + actuation confirmed
STATUS_OBSERVING: str = "observing"       # experiment pending-outcome open
STATUS_COMPLETED: str = "completed"       # outcome resolved, evaluated
STATUS_ACCEPTED_FOR_P8: str = "accepted_for_p8"
STATUS_REJECTED: str = "rejected"
STATUS_ABORTED: str = "aborted"
STATUS_ROLLED_BACK: str = "rolled_back"
STATUS_EXPIRED: str = "expired"
STATUS_INVALIDATED: str = "invalidated"
STATUS_INTERRUPTED_PARTIAL: str = "interrupted_partial"

# Active = occupies the single per-zone slot.
ACTIVE_STATUSES: frozenset[str] = frozenset({
    STATUS_ARMED, STATUS_ACTIVATED, STATUS_OBSERVING,
})
TERMINAL_STATUSES: frozenset[str] = frozenset({
    STATUS_COMPLETED, STATUS_ACCEPTED_FOR_P8, STATUS_REJECTED, STATUS_ABORTED,
    STATUS_ROLLED_BACK, STATUS_EXPIRED, STATUS_INVALIDATED, STATUS_INTERRUPTED_PARTIAL,
})

# --- evaluation classes ---
EVAL_IMPROVED: str = "improved"
EVAL_NO_DEGRADATION: str = "no_degradation"
EVAL_INCONCLUSIVE: str = "inconclusive"
EVAL_DEGRADED: str = "degraded"
EVAL_PREFERENCE_REJECTED: str = "preference_rejected"
EVAL_INVALID: str = "invalid"

# --- rollback state machine ---
ROLLBACK_NONE: str = "none"
ROLLBACK_LOGICAL: str = "logical"                    # authority removed; no command
ROLLBACK_PHYSICAL_PENDING: str = "physical_pending"  # awaiting a regular opener decision
ROLLBACK_COMPLETE: str = "complete"

# --- dispatch / actuation confirmation classes ---
CONFIRM_PLANNED: str = "experiment_planned"
CONFIRM_INJECTED: str = "experiment_injected"
CONFIRM_COMMAND_ATTEMPTED: str = "command_attempted"
CONFIRM_COMMAND_SENT: str = "command_sent"
CONFIRM_POSITION_CONFIRMED: str = "position_change_confirmed"
CONFIRM_POSITION_ASSUMED: str = "position_change_assumed"
CONFIRM_POSITION_FAILED: str = "position_change_failed"

CAUSAL_LIMITATION: str = "not_causally_validated"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _ha(v: int | None, name: str) -> int | None:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 100:
        raise ValueError(f"{name} must be HA position [0,100] or None, got {v!r}")
    return v


# ---------------------------------------------------------------------------
# ExperimentEligibilityResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentEligibilityResult:
    """Output of the pure, fresh experiment eligibility gate."""

    eligible: bool
    intensity_level: str | None = None
    reasons: tuple[str, ...] = ()        # passed gates
    blocked_by: tuple[str, ...] = ()     # failed gates
    block_reason: str | None = None      # primary block reason

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible, "intensity_level": self.intensity_level,
            "reasons": list(self.reasons), "blocked_by": list(self.blocked_by),
            "block_reason": self.block_reason,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ExperimentEligibilityResult":
        if not isinstance(d, dict):
            return cls(eligible=False)
        return cls(
            eligible=bool(d.get("eligible", False)),
            intensity_level=d.get("intensity_level"),
            reasons=tuple(d.get("reasons", []) or []),
            blocked_by=tuple(d.get("blocked_by", []) or []),
            block_reason=d.get("block_reason"),
        )


# ---------------------------------------------------------------------------
# ExperimentEvaluation (honest, non-causal)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExperimentEvaluation:
    experiment_outcome_available: bool = False
    experiment_thermal_score: float | None = None
    experiment_preference_score: float | None = None
    experiment_movement_score: float | None = None
    baseline_sample_count: int = 0
    baseline_distinct_days: int = 0
    baseline_thermal_distribution: dict | None = None
    experiment_vs_baseline_class: str = EVAL_INCONCLUSIVE
    user_acceptance: str | None = None      # accepted | rejected | unknown
    reliability: float = 0.0
    confidence: float = 0.0
    confounders: tuple[str, ...] = ()
    decision: str = EVAL_INCONCLUSIVE
    limitations: tuple[str, ...] = (CAUSAL_LIMITATION,)
    p8_adoption_eligible: bool = False

    def to_dict(self) -> dict:
        lims = tuple(self.limitations)
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return {
            "experiment_outcome_available": self.experiment_outcome_available,
            "experiment_thermal_score": self.experiment_thermal_score,
            "experiment_preference_score": self.experiment_preference_score,
            "experiment_movement_score": self.experiment_movement_score,
            "baseline_sample_count": self.baseline_sample_count,
            "baseline_distinct_days": self.baseline_distinct_days,
            "baseline_thermal_distribution": self.baseline_thermal_distribution,
            "experiment_vs_baseline_class": self.experiment_vs_baseline_class,
            "user_acceptance": self.user_acceptance,
            "reliability": self.reliability, "confidence": self.confidence,
            "confounders": list(self.confounders), "decision": self.decision,
            "limitations": list(lims), "p8_adoption_eligible": self.p8_adoption_eligible,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ExperimentEvaluation":
        if not isinstance(d, dict):
            return cls()
        lims = tuple(d.get("limitations", []) or ())
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return cls(
            experiment_outcome_available=bool(d.get("experiment_outcome_available", False)),
            experiment_thermal_score=d.get("experiment_thermal_score"),
            experiment_preference_score=d.get("experiment_preference_score"),
            experiment_movement_score=d.get("experiment_movement_score"),
            baseline_sample_count=int(d.get("baseline_sample_count", 0)),
            baseline_distinct_days=int(d.get("baseline_distinct_days", 0)),
            baseline_thermal_distribution=d.get("baseline_thermal_distribution"),
            experiment_vs_baseline_class=d.get("experiment_vs_baseline_class", EVAL_INCONCLUSIVE),
            user_acceptance=d.get("user_acceptance"),
            reliability=float(d.get("reliability", 0.0)),
            confidence=float(d.get("confidence", 0.0)),
            confounders=tuple(d.get("confounders", []) or []),
            decision=d.get("decision", EVAL_INCONCLUSIVE), limitations=lims,
            p8_adoption_eligible=bool(d.get("p8_adoption_eligible", False)),
        )


# ---------------------------------------------------------------------------
# BoundedExperiment
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BoundedExperiment:
    experiment_id: str
    source_shadow_id: str
    window_id: str
    zone_id: str
    intensity_level: str
    context_family: str
    created_at: datetime
    updated_at: datetime
    config_generation: int = 0
    source_decision_ids: tuple[str, ...] = ()
    planned_start_at: datetime | None = None
    activated_at: datetime | None = None
    completed_at: datetime | None = None
    # Target stages (all HA convention).
    baseline_parameter_target_ha: int | None = None         # regular authoritative intensity target
    experiment_parameter_target_ha: int | None = None       # baseline − 5 pp (pre-clamp)
    expected_final_candidate_target_ha: int | None = None    # after real clamps (revalidation)
    actual_final_requested_target_ha: int | None = None      # what the decision actually requested
    actual_dispatched_target_ha: int | None = None           # what dispatch sent
    observed_start_position_ha: int | None = None
    observed_end_position_ha: int | None = None
    delta_ha: int | None = None                              # effective delta vs baseline
    cumulative_delta_from_config_ha: int | None = None
    status: str = STATUS_PLANNED
    confirmation: str = CONFIRM_PLANNED
    eligibility_snapshot: dict | None = None
    activation_snapshot: dict | None = None
    abort_reason: str | None = None
    experiment_decision_id: str | None = None
    outcome_reference: str | None = None                     # decision_id carrying the outcome
    evaluation: ExperimentEvaluation = field(default_factory=ExperimentEvaluation)
    rollback_state: str = ROLLBACK_NONE
    schema_version: int = EXPERIMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for n in ("baseline_parameter_target_ha", "experiment_parameter_target_ha",
                  "expected_final_candidate_target_ha", "actual_final_requested_target_ha",
                  "actual_dispatched_target_ha", "observed_start_position_ha",
                  "observed_end_position_ha"):
            _ha(getattr(self, n), n)

    @property
    def experiment_key(self) -> tuple[str, str, str]:
        return (self.window_id, self.intensity_level, self.context_family)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id, "source_shadow_id": self.source_shadow_id,
            "window_id": self.window_id, "zone_id": self.zone_id,
            "intensity_level": self.intensity_level, "context_family": self.context_family,
            "created_at": _iso(self.created_at), "updated_at": _iso(self.updated_at),
            "config_generation": self.config_generation,
            "source_decision_ids": list(self.source_decision_ids),
            "planned_start_at": _iso(self.planned_start_at),
            "activated_at": _iso(self.activated_at), "completed_at": _iso(self.completed_at),
            "baseline_parameter_target_ha": self.baseline_parameter_target_ha,
            "experiment_parameter_target_ha": self.experiment_parameter_target_ha,
            "expected_final_candidate_target_ha": self.expected_final_candidate_target_ha,
            "actual_final_requested_target_ha": self.actual_final_requested_target_ha,
            "actual_dispatched_target_ha": self.actual_dispatched_target_ha,
            "observed_start_position_ha": self.observed_start_position_ha,
            "observed_end_position_ha": self.observed_end_position_ha,
            "delta_ha": self.delta_ha,
            "cumulative_delta_from_config_ha": self.cumulative_delta_from_config_ha,
            "status": self.status, "confirmation": self.confirmation,
            "eligibility_snapshot": self.eligibility_snapshot,
            "activation_snapshot": self.activation_snapshot,
            "abort_reason": self.abort_reason,
            "experiment_decision_id": self.experiment_decision_id,
            "outcome_reference": self.outcome_reference,
            "evaluation": self.evaluation.to_dict(),
            "rollback_state": self.rollback_state, "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BoundedExperiment":
        return cls(
            experiment_id=d["experiment_id"], source_shadow_id=d.get("source_shadow_id", ""),
            window_id=d["window_id"], zone_id=d["zone_id"],
            intensity_level=d["intensity_level"], context_family=d.get("context_family", "global"),
            created_at=_parse(d["created_at"]),  # type: ignore[arg-type]
            updated_at=_parse(d.get("updated_at") or d["created_at"]),  # type: ignore[arg-type]
            config_generation=int(d.get("config_generation", 0)),
            source_decision_ids=tuple(d.get("source_decision_ids", []) or []),
            planned_start_at=_parse(d.get("planned_start_at")),
            activated_at=_parse(d.get("activated_at")),
            completed_at=_parse(d.get("completed_at")),
            baseline_parameter_target_ha=d.get("baseline_parameter_target_ha"),
            experiment_parameter_target_ha=d.get("experiment_parameter_target_ha"),
            expected_final_candidate_target_ha=d.get("expected_final_candidate_target_ha"),
            actual_final_requested_target_ha=d.get("actual_final_requested_target_ha"),
            actual_dispatched_target_ha=d.get("actual_dispatched_target_ha"),
            observed_start_position_ha=d.get("observed_start_position_ha"),
            observed_end_position_ha=d.get("observed_end_position_ha"),
            delta_ha=d.get("delta_ha"),
            cumulative_delta_from_config_ha=d.get("cumulative_delta_from_config_ha"),
            status=d.get("status", STATUS_PLANNED),
            confirmation=d.get("confirmation", CONFIRM_PLANNED),
            eligibility_snapshot=d.get("eligibility_snapshot"),
            activation_snapshot=d.get("activation_snapshot"),
            abort_reason=d.get("abort_reason"),
            experiment_decision_id=d.get("experiment_decision_id"),
            outcome_reference=d.get("outcome_reference"),
            evaluation=ExperimentEvaluation.from_dict(d.get("evaluation")),
            rollback_state=d.get("rollback_state", ROLLBACK_NONE),
            schema_version=int(d.get("schema_version", EXPERIMENT_SCHEMA_VERSION)),
        )
