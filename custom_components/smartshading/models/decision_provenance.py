"""Decision Provenance data models — LE 2.0 / Phase P2.

Pure, Home-Assistant-independent domain models that capture, for every
*material* learning-relevant decision, the full causal chain:

    What would have been decided WITHOUT learning?      → BaselineDecision
    Which learning sources intervened, in what order?    → AdaptationDecision / AdaptationStep
    What target survived all resolvers and clamps?       → ResolvedDecision
    What was dispatched and how did it end?              → DispatchProvenance
    Which later outcome belongs to this decision?        → LearningDecisionRecord.outcome

Design invariants (P2 specification):
  - Provenance and Outcome are DECOUPLED.  The persistent home is the
    LearningDecisionRecord envelope.  An outcome-less material decision is a
    valid, fully-formed record (outcome=None, outcome_status="none").
  - All HA positions are stored in HA convention: 0 = closed, 100 = open.
    Internal/inverted transport values never appear here.  Validation enforces
    [0, 100] or None for every *_ha field.
  - No Home Assistant imports.  Fully serializable via to_dict()/from_dict().
  - Frozen dataclasses — records are immutable; updates use dataclasses.replace().

This module contains NO logic that reads or writes runtime state and NO
adaptive authority.  It is pure vocabulary + (de)serialization.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .learning import DecisionOutcome

# Current payload schema version for the learning store (P2).
PROVENANCE_SCHEMA_VERSION: int = 2

# Valid outcome_status values for a LearningDecisionRecord.
OUTCOME_STATUS_NONE: str = "none"
OUTCOME_STATUS_PENDING: str = "pending"
OUTCOME_STATUS_COMPLETE: str = "complete"
OUTCOME_STATUS_PARTIAL_NO_TEMP: str = "partial_no_temp"
OUTCOME_STATUS_PARTIAL_EARLY_EXIT: str = "partial_early_exit"
OUTCOME_STATUS_INTERRUPTED_PARTIAL: str = "interrupted_partial"
OUTCOME_STATUS_INVALIDATED: str = "invalidated"

_VALID_OUTCOME_STATUS: frozenset[str] = frozenset({
    OUTCOME_STATUS_NONE,
    OUTCOME_STATUS_PENDING,
    OUTCOME_STATUS_COMPLETE,
    OUTCOME_STATUS_PARTIAL_NO_TEMP,
    OUTCOME_STATUS_PARTIAL_EARLY_EXIT,
    OUTCOME_STATUS_INTERRUPTED_PARTIAL,
    OUTCOME_STATUS_INVALIDATED,
})

# Retention classes (P2.8).
RETENTION_FULL: str = "full"
RETENTION_SUMMARY: str = "summary"
RETENTION_PINNED: str = "pinned"

# Adaptation source identifiers.
SOURCE_ADAPTIVE_HEAT: str = "adaptive_heat"
SOURCE_ADAPTIVE_SOLAR: str = "adaptive_solar"
SOURCE_FORECAST_MODIFIER: str = "forecast_modifier"
SOURCE_MANUAL_PREFERENCE: str = "manual_preference"
SOURCE_PASSIVE_ADAPTATION: str = "passive_adaptation"   # populated from P8
SOURCE_SHADOW: str = "shadow"                            # populated from P6


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_ha_position(value: int | None, field_name: str) -> None:
    """Raise ValueError when *value* is not None and not an int in [0, 100]."""
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be int or None, got {value!r}")
    if value < 0 or value > 100:
        raise ValueError(f"{field_name} must be in [0, 100] (HA convention), got {value}")


def _parse_dt(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# ---------------------------------------------------------------------------
# ModelEligibility — model-specific learning eligibility (P2.12)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelEligibility:
    """Whether this concrete decision may feed each learning model.

    Distinct from shading_learning_eligible (behavior-mode level): a fully
    automatic window is generally eligible, but a concrete Night/Safety event
    is not suitable for *thermal* learning.
    """

    thermal: bool = False
    preference: bool = False
    movement: bool = False
    forecast: bool = False
    shadow: bool = False
    experiment: bool = False

    def to_dict(self) -> dict:
        return {
            "thermal": self.thermal,
            "preference": self.preference,
            "movement": self.movement,
            "forecast": self.forecast,
            "shadow": self.shadow,
            "experiment": self.experiment,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ModelEligibility":
        if not isinstance(d, dict):
            return cls()
        return cls(
            thermal=bool(d.get("thermal", False)),
            preference=bool(d.get("preference", False)),
            movement=bool(d.get("movement", False)),
            forecast=bool(d.get("forecast", False)),
            shadow=bool(d.get("shadow", False)),
            experiment=bool(d.get("experiment", False)),
        )


# ---------------------------------------------------------------------------
# DecisionContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionContext:
    """Context surrounding one decision."""

    window_id: str
    zone_id: str
    decision_timestamp: datetime
    cycle_id: int
    config_fingerprint: str
    config_generation: int
    behavior_mode_at_decision: str
    observation_mode: bool
    active_control: bool
    shading_learning_eligible: bool
    model_eligibility: ModelEligibility
    lifecycle_state: str
    presence_absence: str               # "present" | "absent"
    manual_override_active: bool
    safety_active: bool
    learning_eligibility_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "zone_id": self.zone_id,
            "decision_timestamp": _iso(self.decision_timestamp),
            "cycle_id": self.cycle_id,
            "config_fingerprint": self.config_fingerprint,
            "config_generation": self.config_generation,
            "behavior_mode_at_decision": self.behavior_mode_at_decision,
            "observation_mode": self.observation_mode,
            "active_control": self.active_control,
            "shading_learning_eligible": self.shading_learning_eligible,
            "model_eligibility": self.model_eligibility.to_dict(),
            "lifecycle_state": self.lifecycle_state,
            "presence_absence": self.presence_absence,
            "manual_override_active": self.manual_override_active,
            "safety_active": self.safety_active,
            "learning_eligibility_reasons": list(self.learning_eligibility_reasons),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionContext":
        return cls(
            window_id=d["window_id"],
            zone_id=d["zone_id"],
            decision_timestamp=_parse_dt(d["decision_timestamp"]),  # type: ignore[arg-type]
            cycle_id=int(d.get("cycle_id", 0)),
            config_fingerprint=d.get("config_fingerprint", ""),
            config_generation=int(d.get("config_generation", 0)),
            behavior_mode_at_decision=d.get("behavior_mode_at_decision", "unknown"),
            observation_mode=bool(d.get("observation_mode", False)),
            active_control=bool(d.get("active_control", False)),
            shading_learning_eligible=bool(d.get("shading_learning_eligible", False)),
            model_eligibility=ModelEligibility.from_dict(d.get("model_eligibility")),
            lifecycle_state=d.get("lifecycle_state", "day"),
            presence_absence=d.get("presence_absence", "present"),
            manual_override_active=bool(d.get("manual_override_active", False)),
            safety_active=bool(d.get("safety_active", False)),
            learning_eligibility_reasons=tuple(d.get("learning_eligibility_reasons", []) or []),
        )


# ---------------------------------------------------------------------------
# BaselineDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaselineDecision:
    """The decision pure user configuration + deterministic evaluators would
    have produced for this input snapshot (no learning, no adaptation)."""

    baseline_state: str
    baseline_requested_target_ha: int | None
    baseline_decided_by: str

    def __post_init__(self) -> None:
        _validate_ha_position(self.baseline_requested_target_ha, "baseline_requested_target_ha")

    def to_dict(self) -> dict:
        return {
            "baseline_state": self.baseline_state,
            "baseline_requested_target_ha": self.baseline_requested_target_ha,
            "baseline_decided_by": self.baseline_decided_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BaselineDecision":
        return cls(
            baseline_state=d["baseline_state"],
            baseline_requested_target_ha=d.get("baseline_requested_target_ha"),
            baseline_decided_by=d.get("baseline_decided_by", "unknown"),
        )


# ---------------------------------------------------------------------------
# AdaptationStep
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdaptationStep:
    """One applied (or blocked) adaptation source, in application order.

    Position sources (manual_preference, passive_adaptation, shadow) populate
    input_target_ha / output_target_ha.  Threshold sources (adaptive_heat,
    adaptive_solar, forecast_modifier) populate input_thresholds /
    output_thresholds — their net target effect is reconstructable from
    BaselineDecision → ResolvedDecision.target_after_learning_ha.
    """

    source: str
    applied: bool
    input_target_ha: int | None = None
    output_target_ha: int | None = None
    input_thresholds: dict | None = None
    output_thresholds: dict | None = None
    confidence: float | None = None
    strength: float | None = None
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_ha_position(self.input_target_ha, "input_target_ha")
        _validate_ha_position(self.output_target_ha, "output_target_ha")

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "applied": self.applied,
            "input_target_ha": self.input_target_ha,
            "output_target_ha": self.output_target_ha,
            "input_thresholds": self.input_thresholds,
            "output_thresholds": self.output_thresholds,
            "confidence": self.confidence,
            "strength": self.strength,
            "blocked_reason": self.blocked_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AdaptationStep":
        return cls(
            source=d["source"],
            applied=bool(d.get("applied", False)),
            input_target_ha=d.get("input_target_ha"),
            output_target_ha=d.get("output_target_ha"),
            input_thresholds=d.get("input_thresholds"),
            output_thresholds=d.get("output_thresholds"),
            confidence=d.get("confidence"),
            strength=d.get("strength"),
            blocked_reason=d.get("blocked_reason"),
        )


# ---------------------------------------------------------------------------
# AdaptationDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdaptationDecision:
    """All adaptation sources that were available/applied, with ordered steps."""

    steps: tuple[AdaptationStep, ...] = ()
    adaptation_sources: tuple[str, ...] = ()
    net_target_delta_ha: int | None = None
    adaptation_strength: float = 0.0
    confidence_at_decision: float | None = None
    confidence_level_at_decision: str | None = None

    # Manual preference (only real position-adaptation source in P2)
    manual_preference_available: bool = False
    manual_preference_applied: bool = False
    manual_preference_target_ha: int | None = None
    manual_preference_delta_ha: int | None = None
    manual_preference_profile_key: str | None = None     # "light"|"normal"|"strong"|None
    manual_preference_confidence: str | None = None

    # Passive adaptation (experiments) — populated from P8
    passive_adaptation_available: bool = False
    passive_adaptation_target_ha: int | None = None
    passive_adaptation_source: str | None = None
    passive_adaptation_strength: float | None = None
    passive_adaptation_confidence: str | None = None

    # Shadow candidate — populated from P6
    shadow_candidate_available: bool = False
    shadow_candidate_target_ha: int | None = None
    shadow_candidate_reason: str | None = None

    # Forecast modifier diagnostics
    forecast_modifier_delta_wm2: float | None = None
    forecast_trust_score: float | None = None

    # Bounded experiment (P7) — populated only when a real experiment is injected.
    experiment_applied: bool = False
    experiment_id: str | None = None
    target_before_experiment_ha: int | None = None
    experiment_parameter_target_ha: int | None = None
    target_after_experiment_ha: int | None = None
    experiment_delta_requested_ha: int | None = None
    experiment_delta_effective_ha: int | None = None

    # Persistent adoption (P8) — populated only when an adoption is applied.
    adoption_applied: bool = False
    adoption_id: str | None = None
    target_before_adoption_ha: int | None = None
    adopted_delta_requested_ha: int | None = None
    target_after_adoption_ha: int | None = None
    current_total_adaptive_delta_ha: int | None = None

    def __post_init__(self) -> None:
        _validate_ha_position(self.manual_preference_target_ha, "manual_preference_target_ha")
        _validate_ha_position(self.passive_adaptation_target_ha, "passive_adaptation_target_ha")
        _validate_ha_position(self.shadow_candidate_target_ha, "shadow_candidate_target_ha")
        _validate_ha_position(self.target_before_experiment_ha, "target_before_experiment_ha")
        _validate_ha_position(self.experiment_parameter_target_ha, "experiment_parameter_target_ha")
        _validate_ha_position(self.target_after_experiment_ha, "target_after_experiment_ha")
        _validate_ha_position(self.target_before_adoption_ha, "target_before_adoption_ha")
        _validate_ha_position(self.target_after_adoption_ha, "target_after_adoption_ha")

    def to_dict(self) -> dict:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "adaptation_sources": list(self.adaptation_sources),
            "net_target_delta_ha": self.net_target_delta_ha,
            "adaptation_strength": self.adaptation_strength,
            "confidence_at_decision": self.confidence_at_decision,
            "confidence_level_at_decision": self.confidence_level_at_decision,
            "manual_preference_available": self.manual_preference_available,
            "manual_preference_applied": self.manual_preference_applied,
            "manual_preference_target_ha": self.manual_preference_target_ha,
            "manual_preference_delta_ha": self.manual_preference_delta_ha,
            "manual_preference_profile_key": self.manual_preference_profile_key,
            "manual_preference_confidence": self.manual_preference_confidence,
            "passive_adaptation_available": self.passive_adaptation_available,
            "passive_adaptation_target_ha": self.passive_adaptation_target_ha,
            "passive_adaptation_source": self.passive_adaptation_source,
            "passive_adaptation_strength": self.passive_adaptation_strength,
            "passive_adaptation_confidence": self.passive_adaptation_confidence,
            "shadow_candidate_available": self.shadow_candidate_available,
            "shadow_candidate_target_ha": self.shadow_candidate_target_ha,
            "shadow_candidate_reason": self.shadow_candidate_reason,
            "forecast_modifier_delta_wm2": self.forecast_modifier_delta_wm2,
            "forecast_trust_score": self.forecast_trust_score,
            "experiment_applied": self.experiment_applied,
            "experiment_id": self.experiment_id,
            "target_before_experiment_ha": self.target_before_experiment_ha,
            "experiment_parameter_target_ha": self.experiment_parameter_target_ha,
            "target_after_experiment_ha": self.target_after_experiment_ha,
            "experiment_delta_requested_ha": self.experiment_delta_requested_ha,
            "experiment_delta_effective_ha": self.experiment_delta_effective_ha,
            "adoption_applied": self.adoption_applied,
            "adoption_id": self.adoption_id,
            "target_before_adoption_ha": self.target_before_adoption_ha,
            "adopted_delta_requested_ha": self.adopted_delta_requested_ha,
            "target_after_adoption_ha": self.target_after_adoption_ha,
            "current_total_adaptive_delta_ha": self.current_total_adaptive_delta_ha,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AdaptationDecision":
        return cls(
            steps=tuple(AdaptationStep.from_dict(s) for s in d.get("steps", []) or []),
            adaptation_sources=tuple(d.get("adaptation_sources", []) or []),
            net_target_delta_ha=d.get("net_target_delta_ha"),
            adaptation_strength=float(d.get("adaptation_strength", 0.0)),
            confidence_at_decision=d.get("confidence_at_decision"),
            confidence_level_at_decision=d.get("confidence_level_at_decision"),
            manual_preference_available=bool(d.get("manual_preference_available", False)),
            manual_preference_applied=bool(d.get("manual_preference_applied", False)),
            manual_preference_target_ha=d.get("manual_preference_target_ha"),
            manual_preference_delta_ha=d.get("manual_preference_delta_ha"),
            manual_preference_profile_key=d.get("manual_preference_profile_key"),
            manual_preference_confidence=d.get("manual_preference_confidence"),
            passive_adaptation_available=bool(d.get("passive_adaptation_available", False)),
            passive_adaptation_target_ha=d.get("passive_adaptation_target_ha"),
            passive_adaptation_source=d.get("passive_adaptation_source"),
            passive_adaptation_strength=d.get("passive_adaptation_strength"),
            passive_adaptation_confidence=d.get("passive_adaptation_confidence"),
            shadow_candidate_available=bool(d.get("shadow_candidate_available", False)),
            shadow_candidate_target_ha=d.get("shadow_candidate_target_ha"),
            shadow_candidate_reason=d.get("shadow_candidate_reason"),
            forecast_modifier_delta_wm2=d.get("forecast_modifier_delta_wm2"),
            forecast_trust_score=d.get("forecast_trust_score"),
            experiment_applied=bool(d.get("experiment_applied", False)),
            experiment_id=d.get("experiment_id"),
            target_before_experiment_ha=d.get("target_before_experiment_ha"),
            experiment_parameter_target_ha=d.get("experiment_parameter_target_ha"),
            target_after_experiment_ha=d.get("target_after_experiment_ha"),
            experiment_delta_requested_ha=d.get("experiment_delta_requested_ha"),
            experiment_delta_effective_ha=d.get("experiment_delta_effective_ha"),
            adoption_applied=bool(d.get("adoption_applied", False)),
            adoption_id=d.get("adoption_id"),
            target_before_adoption_ha=d.get("target_before_adoption_ha"),
            adopted_delta_requested_ha=d.get("adopted_delta_requested_ha"),
            target_after_adoption_ha=d.get("target_after_adoption_ha"),
            current_total_adaptive_delta_ha=d.get("current_total_adaptive_delta_ha"),
        )


# ---------------------------------------------------------------------------
# ResolvedDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedDecision:
    """The actually-used decision across every resolution/clamp stage.

    All *_ha are logical HA positions (0=closed, 100=open).  None means
    no explicit target (hold / no command).
    """

    final_state: str
    decided_by: str
    target_after_learning_ha: int | None = None
    target_after_tier_resolution_ha: int | None = None
    target_after_command_filter_ha: int | None = None
    target_after_daytime_min_ha: int | None = None
    target_after_anti_heat_buildup_ha: int | None = None
    target_after_harmonization_ha: int | None = None
    final_requested_target_ha: int | None = None
    active_evaluators: tuple[str, ...] = ()
    applied_clamps: tuple[str, ...] = ()
    suppression_reason: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "target_after_learning_ha",
            "target_after_tier_resolution_ha",
            "target_after_command_filter_ha",
            "target_after_daytime_min_ha",
            "target_after_anti_heat_buildup_ha",
            "target_after_harmonization_ha",
            "final_requested_target_ha",
        ):
            _validate_ha_position(getattr(self, name), name)

    def to_dict(self) -> dict:
        return {
            "final_state": self.final_state,
            "decided_by": self.decided_by,
            "target_after_learning_ha": self.target_after_learning_ha,
            "target_after_tier_resolution_ha": self.target_after_tier_resolution_ha,
            "target_after_command_filter_ha": self.target_after_command_filter_ha,
            "target_after_daytime_min_ha": self.target_after_daytime_min_ha,
            "target_after_anti_heat_buildup_ha": self.target_after_anti_heat_buildup_ha,
            "target_after_harmonization_ha": self.target_after_harmonization_ha,
            "final_requested_target_ha": self.final_requested_target_ha,
            "active_evaluators": list(self.active_evaluators),
            "applied_clamps": list(self.applied_clamps),
            "suppression_reason": self.suppression_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResolvedDecision":
        return cls(
            final_state=d["final_state"],
            decided_by=d.get("decided_by", "unknown"),
            target_after_learning_ha=d.get("target_after_learning_ha"),
            target_after_tier_resolution_ha=d.get("target_after_tier_resolution_ha"),
            target_after_command_filter_ha=d.get("target_after_command_filter_ha"),
            target_after_daytime_min_ha=d.get("target_after_daytime_min_ha"),
            target_after_anti_heat_buildup_ha=d.get("target_after_anti_heat_buildup_ha"),
            target_after_harmonization_ha=d.get("target_after_harmonization_ha"),
            final_requested_target_ha=d.get("final_requested_target_ha"),
            active_evaluators=tuple(d.get("active_evaluators", []) or []),
            applied_clamps=tuple(d.get("applied_clamps", []) or []),
            suppression_reason=d.get("suppression_reason"),
        )


# ---------------------------------------------------------------------------
# DispatchProvenance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchProvenance:
    """Dispatch outcome.  requested_target_ha is the LOGICAL HA position
    (0=closed, 100=open).  The inverted transport value is never stored or
    exported; transport_inversion_applied is a pure diagnostic flag."""

    dispatch_allowed: bool | None = None
    dispatch_filter_reason: str | None = None
    dispatch_attempted: bool = False
    dispatch_succeeded: bool | None = None
    dispatch_status: str | None = None
    dispatch_error_category: str | None = None
    requested_target_ha: int | None = None
    transport_inversion_applied: bool = False

    def __post_init__(self) -> None:
        _validate_ha_position(self.requested_target_ha, "requested_target_ha")

    def to_dict(self) -> dict:
        return {
            "dispatch_allowed": self.dispatch_allowed,
            "dispatch_filter_reason": self.dispatch_filter_reason,
            "dispatch_attempted": self.dispatch_attempted,
            "dispatch_succeeded": self.dispatch_succeeded,
            "dispatch_status": self.dispatch_status,
            "dispatch_error_category": self.dispatch_error_category,
            "requested_target_ha": self.requested_target_ha,
            "transport_inversion_applied": self.transport_inversion_applied,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DispatchProvenance":
        return cls(
            dispatch_allowed=d.get("dispatch_allowed"),
            dispatch_filter_reason=d.get("dispatch_filter_reason"),
            dispatch_attempted=bool(d.get("dispatch_attempted", False)),
            dispatch_succeeded=d.get("dispatch_succeeded"),
            dispatch_status=d.get("dispatch_status"),
            dispatch_error_category=d.get("dispatch_error_category"),
            requested_target_ha=d.get("requested_target_ha"),
            transport_inversion_applied=bool(d.get("transport_inversion_applied", False)),
        )


# ---------------------------------------------------------------------------
# DecisionProvenance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionProvenance:
    """The complete causal record of one decision (no outcome)."""

    decision_id: str
    context: DecisionContext
    baseline: BaselineDecision
    adaptation: AdaptationDecision
    resolved: ResolvedDecision
    dispatch: DispatchProvenance
    schema_version: int = PROVENANCE_SCHEMA_VERSION

    @property
    def provenance_id(self) -> str:
        return self.decision_id

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "context": self.context.to_dict(),
            "baseline": self.baseline.to_dict(),
            "adaptation": self.adaptation.to_dict(),
            "resolved": self.resolved.to_dict(),
            "dispatch": self.dispatch.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionProvenance":
        return cls(
            decision_id=d["decision_id"],
            context=DecisionContext.from_dict(d["context"]),
            baseline=BaselineDecision.from_dict(d["baseline"]),
            adaptation=AdaptationDecision.from_dict(d["adaptation"]),
            resolved=ResolvedDecision.from_dict(d["resolved"]),
            dispatch=DispatchProvenance.from_dict(d["dispatch"]),
            schema_version=int(d.get("schema_version", PROVENANCE_SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# ProvenanceSummary — compact form for aged-out records (P2.8 demotion)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProvenanceSummary:
    """Verdichtete Provenance for records beyond the full-retention window."""

    decision_id: str
    decision_timestamp: datetime
    baseline_requested_target_ha: int | None
    final_requested_target_ha: int | None
    adaptation_sources: tuple[str, ...]
    net_target_delta_ha: int | None
    dispatch_status: str | None
    config_generation: int
    # True when full provenance once existed for this decision (a demoted v2
    # record).  False for legacy records that never had provenance.
    provenance_available: bool = True

    def __post_init__(self) -> None:
        _validate_ha_position(self.baseline_requested_target_ha, "baseline_requested_target_ha")
        _validate_ha_position(self.final_requested_target_ha, "final_requested_target_ha")

    @classmethod
    def from_provenance(cls, p: DecisionProvenance) -> "ProvenanceSummary":
        return cls(
            decision_id=p.decision_id,
            decision_timestamp=p.context.decision_timestamp,
            baseline_requested_target_ha=p.baseline.baseline_requested_target_ha,
            final_requested_target_ha=p.resolved.final_requested_target_ha,
            adaptation_sources=p.adaptation.adaptation_sources,
            net_target_delta_ha=p.adaptation.net_target_delta_ha,
            dispatch_status=p.dispatch.dispatch_status,
            config_generation=p.context.config_generation,
            provenance_available=True,
        )

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "decision_timestamp": _iso(self.decision_timestamp),
            "baseline_requested_target_ha": self.baseline_requested_target_ha,
            "final_requested_target_ha": self.final_requested_target_ha,
            "adaptation_sources": list(self.adaptation_sources),
            "net_target_delta_ha": self.net_target_delta_ha,
            "dispatch_status": self.dispatch_status,
            "config_generation": self.config_generation,
            "provenance_available": self.provenance_available,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProvenanceSummary":
        return cls(
            decision_id=d["decision_id"],
            decision_timestamp=_parse_dt(d["decision_timestamp"]),  # type: ignore[arg-type]
            baseline_requested_target_ha=d.get("baseline_requested_target_ha"),
            final_requested_target_ha=d.get("final_requested_target_ha"),
            adaptation_sources=tuple(d.get("adaptation_sources", []) or []),
            net_target_delta_ha=d.get("net_target_delta_ha"),
            dispatch_status=d.get("dispatch_status"),
            config_generation=int(d.get("config_generation", 0)),
            provenance_available=bool(d.get("provenance_available", True)),
        )


# ---------------------------------------------------------------------------
# LearningDecisionRecord — the persistent envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LearningDecisionRecord:
    """Persistent home for one material decision.

    provenance is always present (full) OR replaced by summary when demoted.
    outcome is None until resolution, then attached atomically via replace().
    """

    decision_id: str
    decision_timestamp: datetime
    cycle_id: int
    window_id: str
    provenance: DecisionProvenance | None = None
    summary: ProvenanceSummary | None = None
    outcome: DecisionOutcome | None = None
    outcome_status: str = OUTCOME_STATUS_NONE
    invalidation_reason: str | None = None
    retention_class: str = RETENTION_FULL
    pinned: bool = False

    # Restart / interruption tracking (P2.6)
    observation_interrupted: bool = False
    interruption_started_at: datetime | None = None
    restored_at: datetime | None = None
    interruption_duration_seconds: int | None = None
    restart_count: int = 0

    # Provenance availability flags (unambiguous; replaces the old, misleading
    # ``legacy_provenance_available``):
    #   provenance_available — True when full provenance OR a summary is present.
    #   legacy_record        — True for records reconstructed from v1 data (no
    #                          provenance was ever captured).  These two are
    #                          independent: a legacy record has
    #                          provenance_available=False, legacy_record=True.
    provenance_available: bool = True
    legacy_record: bool = False

    def __post_init__(self) -> None:
        if self.outcome_status not in _VALID_OUTCOME_STATUS:
            raise ValueError(f"invalid outcome_status: {self.outcome_status!r}")

    @property
    def timestamp(self) -> datetime:
        """Alias used by prune_by_age_and_count."""
        return self.decision_timestamp

    @property
    def has_outcome(self) -> bool:
        return self.outcome is not None

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "decision_timestamp": _iso(self.decision_timestamp),
            "cycle_id": self.cycle_id,
            "window_id": self.window_id,
            "provenance": self.provenance.to_dict() if self.provenance is not None else None,
            "summary": self.summary.to_dict() if self.summary is not None else None,
            "outcome": self.outcome.to_dict() if self.outcome is not None else None,
            "outcome_status": self.outcome_status,
            "invalidation_reason": self.invalidation_reason,
            "retention_class": self.retention_class,
            "pinned": self.pinned,
            "observation_interrupted": self.observation_interrupted,
            "interruption_started_at": _iso(self.interruption_started_at),
            "restored_at": _iso(self.restored_at),
            "interruption_duration_seconds": self.interruption_duration_seconds,
            "restart_count": self.restart_count,
            "provenance_available": self.provenance_available,
            "legacy_record": self.legacy_record,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LearningDecisionRecord":
        raw_prov = d.get("provenance")
        raw_summary = d.get("summary")
        raw_outcome = d.get("outcome")
        return cls(
            decision_id=d["decision_id"],
            decision_timestamp=_parse_dt(d["decision_timestamp"]),  # type: ignore[arg-type]
            cycle_id=int(d.get("cycle_id", 0)),
            window_id=d["window_id"],
            provenance=DecisionProvenance.from_dict(raw_prov) if raw_prov else None,
            summary=ProvenanceSummary.from_dict(raw_summary) if raw_summary else None,
            outcome=DecisionOutcome.from_dict(raw_outcome) if raw_outcome else None,
            outcome_status=d.get("outcome_status", OUTCOME_STATUS_NONE),
            invalidation_reason=d.get("invalidation_reason"),
            retention_class=d.get("retention_class", RETENTION_FULL),
            pinned=bool(d.get("pinned", False)),
            observation_interrupted=bool(d.get("observation_interrupted", False)),
            interruption_started_at=_parse_dt(d.get("interruption_started_at")),
            restored_at=_parse_dt(d.get("restored_at")),
            interruption_duration_seconds=d.get("interruption_duration_seconds"),
            restart_count=int(d.get("restart_count", 0)),
            # Backward-tolerant: accept the old key if a pre-rename record is read.
            provenance_available=bool(
                d.get("provenance_available", d.get("legacy_provenance_available", True))
            ),
            legacy_record=bool(d.get("legacy_record", False)),
        )


# ---------------------------------------------------------------------------
# Materiality vocabulary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionCandidate:
    """The computed decision of the current cycle, used for materiality."""

    shading_state: str
    baseline_target_ha: int | None
    final_target_ha: int | None
    adaptation_sources: frozenset[str] = field(default_factory=frozenset)
    dispatch_attempted: bool = False
    dispatch_status: str | None = None
    filter_reason: str | None = None
    suppression_reason: str | None = None
    shadow_experiment_status: str | None = None

    def to_summary(self) -> "DecisionSummary":
        return DecisionSummary(
            shading_state=self.shading_state,
            baseline_target_ha=self.baseline_target_ha,
            final_target_ha=self.final_target_ha,
            adaptation_sources=self.adaptation_sources,
            dispatch_attempted=self.dispatch_attempted,
            dispatch_status=self.dispatch_status,
            filter_reason=self.filter_reason,
            suppression_reason=self.suppression_reason,
            shadow_experiment_status=self.shadow_experiment_status,
        )


@dataclass(frozen=True)
class DecisionSummary:
    """The last persisted decision's key fields, kept per window for dedup."""

    shading_state: str
    baseline_target_ha: int | None
    final_target_ha: int | None
    adaptation_sources: frozenset[str] = field(default_factory=frozenset)
    dispatch_attempted: bool = False
    dispatch_status: str | None = None
    filter_reason: str | None = None
    suppression_reason: str | None = None
    shadow_experiment_status: str | None = None
