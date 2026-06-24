"""Bounded Strategy Learning models — LE 2.0 / Phase P9B.

Extends the position-learning chain (P7 experiment / P8 adoption) to bounded,
experimentable and adoptable decisions about *when* to shade, *which* tier, and
the entry/exit thresholds, minimum hold and hysteresis — without a second
position-adoption path and without a fixed Light→Normal→Strong sequence.

Hard invariants:
  - Exactly one parameter family (one discrete decision) per experiment.
  - Bounded steps and cumulative caps per family (see FAMILY_BOUNDS).
  - At most one active experiment per zone (shared with P7 position experiments).
  - Adoption only from MULTIPLE exact, non-consumed, terminal-valid experiments.
  - Manual preference / Manual Override / Safety / Lifecycle always win.
  - Logical rollback only; consumed experiment ids stay consumed forever.
  - Non-causal: limitations always include 'not_causally_validated'.

No Home Assistant import.  Frozen dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

STRATEGY_LEARNING_SCHEMA_VERSION: int = 1
CAUSAL_LIMITATION: str = "not_causally_validated"

# --- parameter families ---
FAMILY_ENTRY_TIMING: str = "entry_timing"
FAMILY_EXIT_TIMING: str = "exit_timing"
FAMILY_ENTRY_THRESHOLD: str = "entry_threshold"
FAMILY_EXIT_THRESHOLD: str = "exit_threshold"
FAMILY_TIER_CHOICE: str = "tier_choice"
FAMILY_MINIMUM_HOLD: str = "minimum_hold"
FAMILY_HYSTERESIS: str = "hysteresis"

ALL_FAMILIES: tuple[str, ...] = (
    FAMILY_ENTRY_TIMING, FAMILY_EXIT_TIMING, FAMILY_ENTRY_THRESHOLD,
    FAMILY_EXIT_THRESHOLD, FAMILY_TIER_CHOICE, FAMILY_MINIMUM_HOLD, FAMILY_HYSTERESIS,
)

# Families whose effect is realised in P9B runtime via the Unified Solar
# Threshold Resolver (clean, single-clamp, fully reversible injection point).
THRESHOLD_FAMILIES: frozenset[str] = frozenset({FAMILY_ENTRY_THRESHOLD, FAMILY_EXIT_THRESHOLD})


@dataclass(frozen=True)
class FamilyBounds:
    step: float          # exactly-one-experiment bounded step magnitude
    cap: float           # max cumulative |delta| vs configured baseline
    unit: str            # "min" | "wm2" | "tier" | "step"


# Bounded steps + cumulative caps per family (named constants; see spec §4–§7, §18).
FAMILY_BOUNDS: dict[str, FamilyBounds] = {
    FAMILY_ENTRY_TIMING: FamilyBounds(step=10, cap=20, unit="min"),
    FAMILY_EXIT_TIMING: FamilyBounds(step=10, cap=20, unit="min"),
    FAMILY_ENTRY_THRESHOLD: FamilyBounds(step=15, cap=30, unit="wm2"),
    FAMILY_EXIT_THRESHOLD: FamilyBounds(step=15, cap=30, unit="wm2"),
    FAMILY_MINIMUM_HOLD: FamilyBounds(step=5, cap=10, unit="min"),
    FAMILY_HYSTERESIS: FamilyBounds(step=1, cap=2, unit="step"),
    FAMILY_TIER_CHOICE: FamilyBounds(step=1, cap=1, unit="tier"),
}

# --- evidence / confirmation / cooldown gates (named constants) ---
S1_MIN_EXPERIMENTS: int = 3
S1_MIN_DISTINCT_DAYS: int = 3
S1_MIN_IMPROVED: int = 2
S1_MIN_CONFIDENCE: float = 0.65
S1_MIN_RELIABILITY: float = 0.55

S2_MIN_EXPERIMENTS: int = 4
S2_MIN_DISTINCT_DAYS: int = 4
S2_MIN_IMPROVED: int = 3
S2_MIN_CONFIDENCE: float = 0.75
S2_MIN_RELIABILITY: float = 0.65
S2_STABILITY_DAYS: int = 14

CONFIRM_S1_DAYS: int = 14
CONFIRM_S1_OUTCOMES: int = 5
CONFIRM_S1_DISTINCT_DAYS: int = 3
CONFIRM_S2_DAYS: int = 21
CONFIRM_S2_OUTCOMES: int = 7
CONFIRM_S2_DISTINCT_DAYS: int = 4

ROLLBACK_DEGRADED_MIN: int = 3
ROLLBACK_DEGRADED_DISTINCT_DAYS: int = 2
REDUCE_DEGRADED_MIN: int = 2
REDUCE_DEGRADED_DISTINCT_DAYS: int = 2
ROLLBACK_COOLDOWN_DAYS: int = 30

AGE_CAP_DAYS: int = 365
EXPERIMENT_HISTORY_PER_KEY: int = 20
ADOPTION_HISTORY_PER_KEY: int = 20

# --- experiment state machine ---
EXP_PLANNED: str = "planned"
EXP_ARMED: str = "armed"
EXP_ACTIVATED: str = "activated"
EXP_OBSERVING: str = "observing"
EXP_COMPLETED: str = "completed"
EXP_ACCEPTED_FOR_ADOPTION: str = "accepted_for_adoption"
EXP_REJECTED: str = "rejected"
EXP_ABORTED: str = "aborted"
EXP_INTERRUPTED_PARTIAL: str = "interrupted_partial"
EXP_INVALIDATED: str = "invalidated"

EXP_ACTIVE_STATUSES: frozenset[str] = frozenset({EXP_ARMED, EXP_ACTIVATED, EXP_OBSERVING})
EXP_TERMINAL_STATUSES: frozenset[str] = frozenset({
    EXP_COMPLETED, EXP_ACCEPTED_FOR_ADOPTION, EXP_REJECTED, EXP_ABORTED,
    EXP_INTERRUPTED_PARTIAL, EXP_INVALIDATED,
})

# --- adoption state machine ---
AD_CANDIDATE: str = "candidate"
AD_ELIGIBLE: str = "eligible"
AD_ADOPTED: str = "adopted"
AD_MONITORING: str = "monitoring"
AD_CONFIRMED: str = "confirmed"
AD_REDUCED: str = "reduced"
AD_ROLLED_BACK: str = "rolled_back"
AD_REJECTED: str = "rejected"
AD_INVALIDATED: str = "invalidated"

AD_ACTIVE_STATUSES: frozenset[str] = frozenset({
    AD_ELIGIBLE, AD_ADOPTED, AD_MONITORING, AD_CONFIRMED, AD_REDUCED,
})
AD_TERMINAL_STATUSES: frozenset[str] = frozenset({
    AD_ROLLED_BACK, AD_REJECTED, AD_INVALIDATED,
})

# --- evaluation classes ---
EVAL_IMPROVED: str = "improved"
EVAL_NO_DEGRADATION: str = "no_degradation"
EVAL_DEGRADED: str = "degraded"
EVAL_PREFERENCE_REJECTED: str = "preference_rejected"
EVAL_INCONCLUSIVE: str = "inconclusive"
EVAL_UNAVAILABLE: str = "unavailable"
EVAL_CONFOUNDED: str = "confounded"

# --- monitoring actions ---
ACTION_RETAIN: str = "retain"
ACTION_CONFIRM: str = "confirm"
ACTION_TEMPORARY_SUSPEND: str = "temporary_suspend"
ACTION_REDUCE_ONE_STEP: str = "reduce_one_step"
ACTION_FULL_ROLLBACK: str = "full_rollback"
ACTION_INVALIDATE: str = "invalidate"

# --- tiers for TIER_CHOICE ---
TIER_OPEN: str = "open"
TIER_LIGHT: str = "light"
TIER_NORMAL: str = "normal"
TIER_STRONG: str = "strong"
TIER_ORDER: tuple[str, ...] = (TIER_OPEN, TIER_LIGHT, TIER_NORMAL, TIER_STRONG)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


# ---------------------------------------------------------------------------
# StrategyShadowCandidate (must exist before any real experiment)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyShadowCandidate:
    strategy_shadow_id: str
    window_id: str
    zone_id: str
    parameter_family: str
    baseline_value: float
    candidate_value: float
    current_state: str
    proposed_state: str
    context_family: str
    forecast_trust_level: str = "forecast_unavailable"
    expected_benefit: float | None = None
    movement_cost: float | None = None
    confidence: float = 0.0
    reliability: float = 0.0
    config_generation: int = 0
    created_at: datetime | None = None
    expires_at: datetime | None = None
    reason_codes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (CAUSAL_LIMITATION,)

    @property
    def shadow_key(self) -> tuple[str, str]:
        return (self.window_id, self.parameter_family)

    @property
    def delta(self) -> float:
        return self.candidate_value - self.baseline_value

    def to_dict(self) -> dict:
        lims = tuple(self.limitations)
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return {
            "strategy_shadow_id": self.strategy_shadow_id, "window_id": self.window_id,
            "zone_id": self.zone_id, "parameter_family": self.parameter_family,
            "baseline_value": self.baseline_value, "candidate_value": self.candidate_value,
            "current_state": self.current_state, "proposed_state": self.proposed_state,
            "context_family": self.context_family, "forecast_trust_level": self.forecast_trust_level,
            "expected_benefit": self.expected_benefit, "movement_cost": self.movement_cost,
            "confidence": self.confidence, "reliability": self.reliability,
            "config_generation": self.config_generation, "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at), "reason_codes": list(self.reason_codes),
            "limitations": list(lims),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyShadowCandidate":
        lims = tuple(d.get("limitations", []) or ())
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return cls(
            strategy_shadow_id=d["strategy_shadow_id"], window_id=d["window_id"],
            zone_id=d["zone_id"], parameter_family=d["parameter_family"],
            baseline_value=float(d["baseline_value"]), candidate_value=float(d["candidate_value"]),
            current_state=d.get("current_state", "open"), proposed_state=d.get("proposed_state", "open"),
            context_family=d.get("context_family", "global"),
            forecast_trust_level=d.get("forecast_trust_level", "forecast_unavailable"),
            expected_benefit=d.get("expected_benefit"), movement_cost=d.get("movement_cost"),
            confidence=float(d.get("confidence", 0.0)), reliability=float(d.get("reliability", 0.0)),
            config_generation=int(d.get("config_generation", 0)),
            created_at=_parse(d.get("created_at")), expires_at=_parse(d.get("expires_at")),
            reason_codes=tuple(d.get("reason_codes", []) or []), limitations=lims,
        )


# ---------------------------------------------------------------------------
# Monitoring state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyMonitoringState:
    outcome_count: int = 0
    distinct_days: int = 0
    improved_count: int = 0
    no_degradation_count: int = 0
    inconclusive_count: int = 0
    degraded_count: int = 0
    degraded_distinct_days: int = 0
    preference_rejection_count: int = 0
    movement_count: int = 0
    last_outcome_at: datetime | None = None
    monitoring_started_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "outcome_count": self.outcome_count, "distinct_days": self.distinct_days,
            "improved_count": self.improved_count, "no_degradation_count": self.no_degradation_count,
            "inconclusive_count": self.inconclusive_count, "degraded_count": self.degraded_count,
            "degraded_distinct_days": self.degraded_distinct_days,
            "preference_rejection_count": self.preference_rejection_count,
            "movement_count": self.movement_count, "last_outcome_at": _iso(self.last_outcome_at),
            "monitoring_started_at": _iso(self.monitoring_started_at),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "StrategyMonitoringState":
        if not isinstance(d, dict):
            return cls()
        return cls(
            outcome_count=int(d.get("outcome_count", 0)), distinct_days=int(d.get("distinct_days", 0)),
            improved_count=int(d.get("improved_count", 0)),
            no_degradation_count=int(d.get("no_degradation_count", 0)),
            inconclusive_count=int(d.get("inconclusive_count", 0)),
            degraded_count=int(d.get("degraded_count", 0)),
            degraded_distinct_days=int(d.get("degraded_distinct_days", 0)),
            preference_rejection_count=int(d.get("preference_rejection_count", 0)),
            movement_count=int(d.get("movement_count", 0)),
            last_outcome_at=_parse(d.get("last_outcome_at")),
            monitoring_started_at=_parse(d.get("monitoring_started_at")),
        )


# ---------------------------------------------------------------------------
# BoundedStrategyExperiment
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BoundedStrategyExperiment:
    experiment_id: str
    strategy_shadow_id: str
    zone_id: str
    window_id: str
    parameter_family: str
    baseline_value: float
    candidate_value: float
    baseline_state: str
    candidate_state: str
    context_family: str
    config_generation: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    planned_at: datetime | None = None
    activated_at: datetime | None = None
    confirmed_at: datetime | None = None
    completed_at: datetime | None = None
    decision_id: str | None = None
    outcome_reference: str | None = None
    forecast_trust_level: str = "forecast_unavailable"
    status: str = EXP_PLANNED
    confirmation: str = "experiment_planned"
    evaluation_class: str = EVAL_INCONCLUSIVE
    reliability: float = 0.0
    confidence: float = 0.0
    eligibility_snapshot: dict | None = None
    abort_reason: str | None = None
    rollback_state: str = "none"
    schema_version: int = STRATEGY_LEARNING_SCHEMA_VERSION

    @property
    def experiment_key(self) -> tuple[str, str, str]:
        return (self.window_id, self.parameter_family, self.context_family)

    @property
    def delta(self) -> float:
        return self.candidate_value - self.baseline_value

    @property
    def is_active(self) -> bool:
        return self.status in EXP_ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in EXP_TERMINAL_STATUSES

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id, "strategy_shadow_id": self.strategy_shadow_id,
            "zone_id": self.zone_id, "window_id": self.window_id,
            "parameter_family": self.parameter_family, "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value, "baseline_state": self.baseline_state,
            "candidate_state": self.candidate_state, "context_family": self.context_family,
            "config_generation": self.config_generation, "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at), "planned_at": _iso(self.planned_at),
            "activated_at": _iso(self.activated_at), "confirmed_at": _iso(self.confirmed_at),
            "completed_at": _iso(self.completed_at), "decision_id": self.decision_id,
            "outcome_reference": self.outcome_reference,
            "forecast_trust_level": self.forecast_trust_level, "status": self.status,
            "confirmation": self.confirmation, "evaluation_class": self.evaluation_class,
            "reliability": self.reliability, "confidence": self.confidence,
            "eligibility_snapshot": self.eligibility_snapshot, "abort_reason": self.abort_reason,
            "rollback_state": self.rollback_state, "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BoundedStrategyExperiment":
        return cls(
            experiment_id=d["experiment_id"], strategy_shadow_id=d.get("strategy_shadow_id", ""),
            zone_id=d["zone_id"], window_id=d["window_id"],
            parameter_family=d["parameter_family"], baseline_value=float(d.get("baseline_value", 0.0)),
            candidate_value=float(d.get("candidate_value", 0.0)),
            baseline_state=d.get("baseline_state", "open"), candidate_state=d.get("candidate_state", "open"),
            context_family=d.get("context_family", "global"),
            config_generation=int(d.get("config_generation", 0)),
            created_at=_parse(d.get("created_at")), updated_at=_parse(d.get("updated_at")),
            planned_at=_parse(d.get("planned_at")), activated_at=_parse(d.get("activated_at")),
            confirmed_at=_parse(d.get("confirmed_at")), completed_at=_parse(d.get("completed_at")),
            decision_id=d.get("decision_id"), outcome_reference=d.get("outcome_reference"),
            forecast_trust_level=d.get("forecast_trust_level", "forecast_unavailable"),
            status=d.get("status", EXP_PLANNED), confirmation=d.get("confirmation", "experiment_planned"),
            evaluation_class=d.get("evaluation_class", EVAL_INCONCLUSIVE),
            reliability=float(d.get("reliability", 0.0)), confidence=float(d.get("confidence", 0.0)),
            eligibility_snapshot=d.get("eligibility_snapshot"), abort_reason=d.get("abort_reason"),
            rollback_state=d.get("rollback_state", "none"),
            schema_version=int(d.get("schema_version", STRATEGY_LEARNING_SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# PersistentStrategyAdoption
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PersistentStrategyAdoption:
    adoption_id: str
    zone_id: str
    window_id: str
    parameter_family: str
    context_family: str
    baseline_value: float
    adopted_delta: float
    effective_value: float
    baseline_state: str = "open"
    adopted_state: str = "open"
    source_experiment_ids: tuple[str, ...] = ()
    source_shadow_ids: tuple[str, ...] = ()
    source_decision_ids: tuple[str, ...] = ()
    consumed_experiment_ids: tuple[str, ...] = ()
    validated_context_families: tuple[str, ...] = ()
    created_at: datetime | None = None
    activated_at: datetime | None = None
    stage2_activated_at: datetime | None = None
    updated_at: datetime | None = None
    last_validated_at: datetime | None = None
    config_generation: int = 0
    status: str = AD_CANDIDATE
    suspended: bool = False
    confidence: float = 0.0
    reliability: float = 0.0
    distinct_experiment_days: int = 0
    monitoring: StrategyMonitoringState = field(default_factory=StrategyMonitoringState)
    rollback_reason: str | None = None
    current_gate_reason: str | None = None
    cooldown_until: datetime | None = None
    limitations: tuple[str, ...] = (CAUSAL_LIMITATION,)
    schema_version: int = STRATEGY_LEARNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        bounds = FAMILY_BOUNDS.get(self.parameter_family)
        if bounds is not None and abs(self.adopted_delta) - bounds.cap > 1e-9:
            raise ValueError(
                f"adopted_delta {self.adopted_delta} exceeds cap {bounds.cap} "
                f"for family {self.parameter_family}")

    @property
    def adoption_key(self) -> tuple[str, str]:
        return (self.window_id, self.parameter_family)

    @property
    def is_active(self) -> bool:
        return self.status in AD_ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in AD_TERMINAL_STATUSES

    @property
    def stage(self) -> int:
        bounds = FAMILY_BOUNDS.get(self.parameter_family)
        if bounds is None or self.adopted_delta == 0:
            return 0
        return 2 if abs(self.adopted_delta) >= bounds.cap - 1e-9 else 1

    def to_dict(self) -> dict:
        lims = tuple(self.limitations)
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return {
            "adoption_id": self.adoption_id, "zone_id": self.zone_id, "window_id": self.window_id,
            "parameter_family": self.parameter_family, "context_family": self.context_family,
            "baseline_value": self.baseline_value, "adopted_delta": self.adopted_delta,
            "effective_value": self.effective_value, "baseline_state": self.baseline_state,
            "adopted_state": self.adopted_state,
            "source_experiment_ids": list(self.source_experiment_ids),
            "source_shadow_ids": list(self.source_shadow_ids),
            "source_decision_ids": list(self.source_decision_ids),
            "consumed_experiment_ids": list(self.consumed_experiment_ids),
            "validated_context_families": list(self.validated_context_families),
            "created_at": _iso(self.created_at), "activated_at": _iso(self.activated_at),
            "stage2_activated_at": _iso(self.stage2_activated_at), "updated_at": _iso(self.updated_at),
            "last_validated_at": _iso(self.last_validated_at),
            "config_generation": self.config_generation, "status": self.status,
            "suspended": self.suspended, "confidence": self.confidence, "reliability": self.reliability,
            "distinct_experiment_days": self.distinct_experiment_days,
            "monitoring": self.monitoring.to_dict(), "rollback_reason": self.rollback_reason,
            "current_gate_reason": self.current_gate_reason, "cooldown_until": _iso(self.cooldown_until),
            "limitations": list(lims), "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PersistentStrategyAdoption":
        lims = tuple(d.get("limitations", []) or ())
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return cls(
            adoption_id=d["adoption_id"], zone_id=d["zone_id"], window_id=d["window_id"],
            parameter_family=d["parameter_family"], context_family=d.get("context_family", "global"),
            baseline_value=float(d.get("baseline_value", 0.0)),
            adopted_delta=float(d.get("adopted_delta", 0.0)),
            effective_value=float(d.get("effective_value", 0.0)),
            baseline_state=d.get("baseline_state", "open"), adopted_state=d.get("adopted_state", "open"),
            source_experiment_ids=tuple(d.get("source_experiment_ids", []) or []),
            source_shadow_ids=tuple(d.get("source_shadow_ids", []) or []),
            source_decision_ids=tuple(d.get("source_decision_ids", []) or []),
            consumed_experiment_ids=tuple(d.get("consumed_experiment_ids", []) or []),
            validated_context_families=tuple(d.get("validated_context_families", []) or []),
            created_at=_parse(d.get("created_at")), activated_at=_parse(d.get("activated_at")),
            stage2_activated_at=_parse(d.get("stage2_activated_at")), updated_at=_parse(d.get("updated_at")),
            last_validated_at=_parse(d.get("last_validated_at")),
            config_generation=int(d.get("config_generation", 0)), status=d.get("status", AD_CANDIDATE),
            suspended=bool(d.get("suspended", False)), confidence=float(d.get("confidence", 0.0)),
            reliability=float(d.get("reliability", 0.0)),
            distinct_experiment_days=int(d.get("distinct_experiment_days", 0)),
            monitoring=StrategyMonitoringState.from_dict(d.get("monitoring")),
            rollback_reason=d.get("rollback_reason"), current_gate_reason=d.get("current_gate_reason"),
            cooldown_until=_parse(d.get("cooldown_until")), limitations=lims,
            schema_version=int(d.get("schema_version", STRATEGY_LEARNING_SCHEMA_VERSION)),
        )
