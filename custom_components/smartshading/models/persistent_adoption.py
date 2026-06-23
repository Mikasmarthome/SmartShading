"""Persistent Adoption models — LE 2.0 / Phase P8.

A PersistentTargetAdoption is the first construct that turns repeatedly
confirmed, safe P7 experiment results into a BOUNDED persistent target
adaptation in the regular target chain.

Hard invariants (P8 specification):
  - close_more only; adopted_delta_ha ∈ {0, -5, -10} (HA: lower = more closed).
  - Never more than -10 pp cumulative deviation from the configured target.
  - Identity is (window_id, intensity_level); at most one active adoption per
    identity.  context_family is an evidence/applicability GATE, never a parallel
    per-season target.
  - Created only from MULTIPLE exact, non-consumed, terminal-valid P7 experiments
    (fresh evaluation; persisted P7 snapshots are diagnostic only).
  - Manual preference always wins; Learning Mode OFF removes adoption authority.
  - Logical rollback only (no proactive inverse command).
  - Evaluation/monitoring is never an exact causal claim: limitations always
    contains 'not_causally_validated'.

No Home Assistant import.  Frozen dataclasses.  HA convention (0=closed,100=open).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

ADOPTION_SCHEMA_VERSION: int = 1

# --- bounded parameters (HA percentage points) ---
ADOPTION_STEP_HA: int = 5
ADOPTION_MAX_DELTA_HA: int = 10          # cumulative cap vs configured target
ADOPTION_MATERIALITY_HA: int = 3

DIRECTION_CLOSE_MORE: str = "close_more"
PARAM_TARGET_POSITION: str = "target_position"

# --- stage-1 (first -5 pp) evidence gates ---
S1_MIN_EXPERIMENTS: int = 3
S1_MIN_DISTINCT_DAYS: int = 3
S1_MIN_IMPROVED: int = 2
S1_MIN_CONFIDENCE: float = 0.6
S1_MIN_RELIABILITY: float = 0.5

# --- stage-2 (cumulative -10 pp) evidence gates (stricter, independent series) ---
S2_MIN_EXPERIMENTS: int = 4
S2_MIN_DISTINCT_DAYS: int = 4
S2_MIN_IMPROVED: int = 3
S2_MIN_CONFIDENCE: float = 0.7
S2_STABILITY_DAYS: int = 14              # first stage must be stable this long first

# --- confirmation gates (monitoring after activation) ---
CONFIRM_S1_DAYS: int = 14
CONFIRM_S1_OUTCOMES: int = 5
CONFIRM_S1_DISTINCT_DAYS: int = 3
CONFIRM_S2_DAYS: int = 21
CONFIRM_S2_OUTCOMES: int = 7
CONFIRM_S2_DISTINCT_DAYS: int = 4

# --- reduce / rollback thresholds (robust negative evidence only) ---
ROLLBACK_DEGRADED_MIN: int = 3
ROLLBACK_DEGRADED_DISTINCT_DAYS: int = 2
REDUCE_DEGRADED_MIN: int = 2             # weaker, repeated → reduce -10 → -5
REDUCE_DEGRADED_DISTINCT_DAYS: int = 2

# --- cooldown / retention ---
ROLLBACK_COOLDOWN_DAYS: int = 30
ADOPTION_AGE_CAP_DAYS: int = 365
ADOPTION_HISTORY_PER_WINDOW: int = 20

# --- state machine ---
STATUS_CANDIDATE: str = "candidate"
STATUS_ELIGIBLE: str = "eligible"
STATUS_ADOPTED: str = "adopted"
STATUS_MONITORING: str = "monitoring"
STATUS_CONFIRMED: str = "confirmed"
STATUS_REDUCED: str = "reduced"
STATUS_ROLLED_BACK: str = "rolled_back"
STATUS_REJECTED: str = "rejected"
STATUS_EXPIRED: str = "expired"
STATUS_INVALIDATED: str = "invalidated"

# Active = occupies the single per-(window,intensity) slot and may apply a delta.
ACTIVE_STATUSES: frozenset[str] = frozenset({
    STATUS_ELIGIBLE, STATUS_ADOPTED, STATUS_MONITORING, STATUS_CONFIRMED, STATUS_REDUCED,
})
TERMINAL_STATUSES: frozenset[str] = frozenset({
    STATUS_ROLLED_BACK, STATUS_REJECTED, STATUS_EXPIRED, STATUS_INVALIDATED,
})

# --- monitoring action verbs ---
ACTION_RETAIN: str = "retain"
ACTION_TEMPORARY_SUSPEND: str = "temporary_suspend"
ACTION_REDUCE_ONE_STEP: str = "reduce_one_step"
ACTION_FULL_ROLLBACK: str = "full_rollback"
ACTION_INVALIDATE: str = "invalidate"
ACTION_CONFIRM: str = "confirm"

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
# AdoptionEligibilityResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdoptionEligibilityResult:
    eligible: bool
    intensity_level: str | None = None
    reasons: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    block_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible, "intensity_level": self.intensity_level,
            "reasons": list(self.reasons), "blocked_by": list(self.blocked_by),
            "block_reason": self.block_reason,
        }


# ---------------------------------------------------------------------------
# AdoptionMonitoringState (dimension-specific, non-causal)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdoptionMonitoringState:
    outcome_count: int = 0
    distinct_days: int = 0
    improved_count: int = 0
    no_degradation_count: int = 0
    inconclusive_count: int = 0
    degraded_count: int = 0
    degraded_distinct_days: int = 0
    preference_rejection_count: int = 0
    last_outcome_at: datetime | None = None
    monitoring_started_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "outcome_count": self.outcome_count, "distinct_days": self.distinct_days,
            "improved_count": self.improved_count,
            "no_degradation_count": self.no_degradation_count,
            "inconclusive_count": self.inconclusive_count,
            "degraded_count": self.degraded_count,
            "degraded_distinct_days": self.degraded_distinct_days,
            "preference_rejection_count": self.preference_rejection_count,
            "last_outcome_at": _iso(self.last_outcome_at),
            "monitoring_started_at": _iso(self.monitoring_started_at),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "AdoptionMonitoringState":
        if not isinstance(d, dict):
            return cls()
        return cls(
            outcome_count=int(d.get("outcome_count", 0)),
            distinct_days=int(d.get("distinct_days", 0)),
            improved_count=int(d.get("improved_count", 0)),
            no_degradation_count=int(d.get("no_degradation_count", 0)),
            inconclusive_count=int(d.get("inconclusive_count", 0)),
            degraded_count=int(d.get("degraded_count", 0)),
            degraded_distinct_days=int(d.get("degraded_distinct_days", 0)),
            preference_rejection_count=int(d.get("preference_rejection_count", 0)),
            last_outcome_at=_parse(d.get("last_outcome_at")),
            monitoring_started_at=_parse(d.get("monitoring_started_at")),
        )


# ---------------------------------------------------------------------------
# PersistentTargetAdoption
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PersistentTargetAdoption:
    adoption_id: str
    window_id: str
    zone_id: str
    intensity_level: str
    context_family: str                       # primary validated context (gate)
    parameter_type: str = PARAM_TARGET_POSITION
    direction: str = DIRECTION_CLOSE_MORE
    configured_target_ha: int | None = None
    adopted_delta_ha: int = 0                  # 0 | -5 | -10
    effective_target_ha: int | None = None
    validated_context_families: tuple[str, ...] = ()   # all contexts evidence came from
    source_experiment_ids: tuple[str, ...] = ()
    source_shadow_ids: tuple[str, ...] = ()
    source_decision_ids: tuple[str, ...] = ()
    consumed_experiment_ids: tuple[str, ...] = ()      # permanent ledger (never released)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    activated_at: datetime | None = None
    stage2_activated_at: datetime | None = None
    last_validated_at: datetime | None = None
    config_generation: int = 0
    status: str = STATUS_CANDIDATE
    suspended: bool = False
    confidence: float = 0.0
    reliability: float = 0.0
    distinct_experiment_days: int = 0
    successful_experiment_count: int = 0
    no_degradation_count: int = 0
    inconclusive_count: int = 0
    degraded_count: int = 0
    preference_rejection_count: int = 0
    monitoring: AdoptionMonitoringState = field(default_factory=AdoptionMonitoringState)
    rollback_reason: str | None = None
    current_gate_reason: str | None = None
    cooldown_until: datetime | None = None
    limitations: tuple[str, ...] = (CAUSAL_LIMITATION,)
    schema_version: int = ADOPTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _ha(self.configured_target_ha, "configured_target_ha")
        _ha(self.effective_target_ha, "effective_target_ha")
        if self.adopted_delta_ha not in (0, -ADOPTION_STEP_HA, -ADOPTION_MAX_DELTA_HA):
            raise ValueError(f"adopted_delta_ha must be 0, -5 or -10, got {self.adopted_delta_ha}")

    @property
    def adoption_key(self) -> tuple[str, str]:
        return (self.window_id, self.intensity_level)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def stage(self) -> int:
        if self.adopted_delta_ha == -ADOPTION_MAX_DELTA_HA:
            return 2
        if self.adopted_delta_ha == -ADOPTION_STEP_HA:
            return 1
        return 0

    def to_dict(self) -> dict:
        lims = tuple(self.limitations)
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return {
            "adoption_id": self.adoption_id, "window_id": self.window_id,
            "zone_id": self.zone_id, "intensity_level": self.intensity_level,
            "context_family": self.context_family, "parameter_type": self.parameter_type,
            "direction": self.direction, "configured_target_ha": self.configured_target_ha,
            "adopted_delta_ha": self.adopted_delta_ha,
            "effective_target_ha": self.effective_target_ha,
            "validated_context_families": list(self.validated_context_families),
            "source_experiment_ids": list(self.source_experiment_ids),
            "source_shadow_ids": list(self.source_shadow_ids),
            "source_decision_ids": list(self.source_decision_ids),
            "consumed_experiment_ids": list(self.consumed_experiment_ids),
            "created_at": _iso(self.created_at), "updated_at": _iso(self.updated_at),
            "activated_at": _iso(self.activated_at),
            "stage2_activated_at": _iso(self.stage2_activated_at),
            "last_validated_at": _iso(self.last_validated_at),
            "config_generation": self.config_generation, "status": self.status,
            "suspended": self.suspended, "confidence": self.confidence,
            "reliability": self.reliability,
            "distinct_experiment_days": self.distinct_experiment_days,
            "successful_experiment_count": self.successful_experiment_count,
            "no_degradation_count": self.no_degradation_count,
            "inconclusive_count": self.inconclusive_count,
            "degraded_count": self.degraded_count,
            "preference_rejection_count": self.preference_rejection_count,
            "monitoring": self.monitoring.to_dict(), "rollback_reason": self.rollback_reason,
            "current_gate_reason": self.current_gate_reason,
            "cooldown_until": _iso(self.cooldown_until), "limitations": list(lims),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PersistentTargetAdoption":
        lims = tuple(d.get("limitations", []) or ())
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return cls(
            adoption_id=d["adoption_id"], window_id=d["window_id"], zone_id=d["zone_id"],
            intensity_level=d["intensity_level"], context_family=d.get("context_family", "global"),
            parameter_type=d.get("parameter_type", PARAM_TARGET_POSITION),
            direction=d.get("direction", DIRECTION_CLOSE_MORE),
            configured_target_ha=d.get("configured_target_ha"),
            adopted_delta_ha=int(d.get("adopted_delta_ha", 0)),
            effective_target_ha=d.get("effective_target_ha"),
            validated_context_families=tuple(d.get("validated_context_families", []) or []),
            source_experiment_ids=tuple(d.get("source_experiment_ids", []) or []),
            source_shadow_ids=tuple(d.get("source_shadow_ids", []) or []),
            source_decision_ids=tuple(d.get("source_decision_ids", []) or []),
            consumed_experiment_ids=tuple(d.get("consumed_experiment_ids", []) or []),
            created_at=_parse(d.get("created_at")), updated_at=_parse(d.get("updated_at")),
            activated_at=_parse(d.get("activated_at")),
            stage2_activated_at=_parse(d.get("stage2_activated_at")),
            last_validated_at=_parse(d.get("last_validated_at")),
            config_generation=int(d.get("config_generation", 0)),
            status=d.get("status", STATUS_CANDIDATE), suspended=bool(d.get("suspended", False)),
            confidence=float(d.get("confidence", 0.0)),
            reliability=float(d.get("reliability", 0.0)),
            distinct_experiment_days=int(d.get("distinct_experiment_days", 0)),
            successful_experiment_count=int(d.get("successful_experiment_count", 0)),
            no_degradation_count=int(d.get("no_degradation_count", 0)),
            inconclusive_count=int(d.get("inconclusive_count", 0)),
            degraded_count=int(d.get("degraded_count", 0)),
            preference_rejection_count=int(d.get("preference_rejection_count", 0)),
            monitoring=AdoptionMonitoringState.from_dict(d.get("monitoring")),
            rollback_reason=d.get("rollback_reason"),
            current_gate_reason=d.get("current_gate_reason"),
            cooldown_until=_parse(d.get("cooldown_until")), limitations=lims,
            schema_version=int(d.get("schema_version", ADOPTION_SCHEMA_VERSION)),
        )
