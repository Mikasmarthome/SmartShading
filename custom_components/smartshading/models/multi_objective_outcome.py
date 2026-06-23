"""Multi-Objective Outcome models — LE 2.0 / Phase P3.

Decomposes the single legacy ``DecisionOutcome.outcome_score`` into separate,
defensible dimensions so the Learning Engine can later explain *why* a decision
was good or bad and adapt each concern independently:

    thermal      — observed thermal association (NOT proven window causation)
    movement     — movement economy / stability (deterministic counters)
    preference   — manual user override as its own authority (direction ≠ score)
    reliability  — per-dimension gate / trust (never an extra objective score)
    confounders  — dimension-specific interpretation hazards

Hard invariants (P3 sharpenings):
  - All positions are HA convention (0 = closed, 100 = open).  No internal /
    inverted values ever appear here.
  - Every score is float|None, normalized [-1.0, +1.0].  None ⇔ not available;
    an unavailable dimension never carries an active score.
  - override_direction is SEPARATE from preference score; direction is never
    encoded in the score sign.
  - reliability.overall is a conservative diagnostic summary only — it carries
    NO active learning authority; the per-dimension reliabilities gate.
  - reconstruction is dimension-specific (reconstructed + reconstruction_quality).
  - No Home Assistant import.  Fully serializable.  Frozen dataclasses.

These models have NO runtime authority in P3: they are recorded, persisted,
exported and tested, but the active learning chain still consumes the legacy
score until later phases migrate each consumer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

MULTI_OBJECTIVE_SCHEMA_VERSION: int = 1

# Override direction labels (preference) — never encoded in the score sign.
DIRECTION_OPEN_MORE: str = "open_more"
DIRECTION_CLOSE_MORE: str = "close_more"
DIRECTION_UNCHANGED: str = "unchanged"
DIRECTION_UNKNOWN: str = "unknown"

# Attribution quality labels.  window_isolated is FORBIDDEN in P3 (needs P5).
ATTRIBUTION_UNKNOWN: str = "unknown"
ATTRIBUTION_ZONE_SHARED: str = "zone_shared"
ATTRIBUTION_WINDOW_CANDIDATE: str = "window_candidate"
ATTRIBUTION_WINDOW_ISOLATED: str = "window_isolated"  # never set before P5

# Reconstruction quality labels (per dimension).
RECON_EXACT: str = "exact"
RECON_PARTIAL: str = "partial"
RECON_UNAVAILABLE: str = "unavailable"

# Thermal direction labels.
THERMAL_DIR_COOLING_OR_HOLD: str = "cooling_or_hold"
THERMAL_DIR_AMBIGUOUS: str = "ambiguous"
THERMAL_DIR_WARMING: str = "warming"
THERMAL_DIR_STABLE: str = "stable"
THERMAL_DIR_COOLING: str = "cooling"

# Movement cause labels.
MOVE_CAUSE_NONE: str = "none"            # stable, no follow-up movement
MOVE_CAUSE_COMFORT: str = "comfort"      # SmartShading comfort/solar/heat movement
MOVE_CAUSE_SAFETY: str = "safety"
MOVE_CAUSE_LIFECYCLE: str = "lifecycle"
MOVE_CAUSE_ABSENCE: str = "absence"
MOVE_CAUSE_MANUAL: str = "manual"


def _clamp_score(v: float | None) -> float | None:
    if v is None:
        return None
    return max(-1.0, min(1.0, v))


def _ha_or_none(v: int | None, name: str) -> int | None:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 100:
        raise ValueError(f"{name} must be HA position [0,100] or None, got {v!r}")
    return v


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


# ---------------------------------------------------------------------------
# ThermalOutcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThermalOutcome:
    """Observed thermal association of a decision (NOT proven window causation)."""

    available: bool = False
    score: float | None = None
    temperature_start: float | None = None
    temperature_end: float | None = None
    temperature_delta: float | None = None
    observation_duration_min: float | None = None
    expected_direction: str | None = None      # cooling_or_hold | ambiguous
    observed_direction: str | None = None       # cooling | stable | warming
    protection_effect_detected: bool = False
    overheat_detected: bool = False
    insufficient_response: bool = False
    outdoor_temp_at_decision: float | None = None
    solar_exposure_at_decision: float | None = None
    reason: str = ""
    reconstructed: bool = False
    reconstruction_quality: str = RECON_UNAVAILABLE

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _clamp_score(self.score))

    def to_dict(self) -> dict:
        return {
            "available": self.available, "score": self.score,
            "temperature_start": self.temperature_start, "temperature_end": self.temperature_end,
            "temperature_delta": self.temperature_delta,
            "observation_duration_min": self.observation_duration_min,
            "expected_direction": self.expected_direction,
            "observed_direction": self.observed_direction,
            "protection_effect_detected": self.protection_effect_detected,
            "overheat_detected": self.overheat_detected,
            "insufficient_response": self.insufficient_response,
            "outdoor_temp_at_decision": self.outdoor_temp_at_decision,
            "solar_exposure_at_decision": self.solar_exposure_at_decision,
            "reason": self.reason,
            "reconstructed": self.reconstructed,
            "reconstruction_quality": self.reconstruction_quality,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ThermalOutcome":
        if not isinstance(d, dict):
            return cls()
        return cls(
            available=bool(d.get("available", False)), score=d.get("score"),
            temperature_start=d.get("temperature_start"), temperature_end=d.get("temperature_end"),
            temperature_delta=d.get("temperature_delta"),
            observation_duration_min=d.get("observation_duration_min"),
            expected_direction=d.get("expected_direction"),
            observed_direction=d.get("observed_direction"),
            protection_effect_detected=bool(d.get("protection_effect_detected", False)),
            overheat_detected=bool(d.get("overheat_detected", False)),
            insufficient_response=bool(d.get("insufficient_response", False)),
            outdoor_temp_at_decision=d.get("outdoor_temp_at_decision"),
            solar_exposure_at_decision=d.get("solar_exposure_at_decision"),
            reason=d.get("reason", ""),
            reconstructed=bool(d.get("reconstructed", False)),
            reconstruction_quality=d.get("reconstruction_quality", RECON_UNAVAILABLE),
        )


# ---------------------------------------------------------------------------
# MovementOutcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MovementOutcome:
    """Movement economy / stability — deterministic counters.

    Positive movement is a LIMITED stability signal and never compensates a
    thermal failure or a preference rejection.  Safety / lifecycle / absence /
    manual transitions are excluded from instability scoring.
    """

    available: bool = False
    score: float | None = None
    stable_without_additional_action: bool = False
    command_attempt_count: int = 0
    successful_command_count: int = 0
    material_target_change_count: int = 0
    comfort_state_transition_count: int = 0
    excluded_transition_count: int = 0
    oscillation_detected: bool = False
    oscillation_reason: str | None = None
    minimum_action_interval_respected: bool | None = None
    unnecessary_movement_signal: bool = False
    movement_cause: str = MOVE_CAUSE_NONE
    reason: str = ""
    reconstructed: bool = False
    reconstruction_quality: str = RECON_UNAVAILABLE

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _clamp_score(self.score))

    def to_dict(self) -> dict:
        return {
            "available": self.available, "score": self.score,
            "stable_without_additional_action": self.stable_without_additional_action,
            "command_attempt_count": self.command_attempt_count,
            "successful_command_count": self.successful_command_count,
            "material_target_change_count": self.material_target_change_count,
            "comfort_state_transition_count": self.comfort_state_transition_count,
            "excluded_transition_count": self.excluded_transition_count,
            "oscillation_detected": self.oscillation_detected,
            "oscillation_reason": self.oscillation_reason,
            "minimum_action_interval_respected": self.minimum_action_interval_respected,
            "unnecessary_movement_signal": self.unnecessary_movement_signal,
            "movement_cause": self.movement_cause, "reason": self.reason,
            "reconstructed": self.reconstructed,
            "reconstruction_quality": self.reconstruction_quality,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "MovementOutcome":
        if not isinstance(d, dict):
            return cls()
        return cls(
            available=bool(d.get("available", False)), score=d.get("score"),
            stable_without_additional_action=bool(d.get("stable_without_additional_action", False)),
            command_attempt_count=int(d.get("command_attempt_count", 0)),
            successful_command_count=int(d.get("successful_command_count", 0)),
            material_target_change_count=int(d.get("material_target_change_count", 0)),
            comfort_state_transition_count=int(d.get("comfort_state_transition_count", 0)),
            excluded_transition_count=int(d.get("excluded_transition_count", 0)),
            oscillation_detected=bool(d.get("oscillation_detected", False)),
            oscillation_reason=d.get("oscillation_reason"),
            minimum_action_interval_respected=d.get("minimum_action_interval_respected"),
            unnecessary_movement_signal=bool(d.get("unnecessary_movement_signal", False)),
            movement_cause=d.get("movement_cause", MOVE_CAUSE_NONE),
            reason=d.get("reason", ""),
            reconstructed=bool(d.get("reconstructed", False)),
            reconstruction_quality=d.get("reconstruction_quality", RECON_UNAVAILABLE),
        )


# ---------------------------------------------------------------------------
# PreferenceOutcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreferenceOutcome:
    """Manual override as its own authority.

    override_direction (open_more|close_more|unchanged|unknown) is SEPARATE from
    the score.  An override is fundamentally a rejection of the preceding
    automatic decision → negative score.  A positive score requires explicit,
    defensible acceptance evidence (none exists in P3).  A missing override,
    a lifecycle-clear or a safety-clear never produce a positive score.
    """

    available: bool = False
    manual_override_occurred: bool = False
    override_direction: str = DIRECTION_UNKNOWN
    override_target_ha: int | None = None
    override_delta_ha: int | None = None
    override_delay_seconds: float | None = None
    override_hold_duration_seconds: float | None = None
    cleared_by_lifecycle: bool = False
    preference_signal_strength: float | None = None   # [0,1]
    score: float | None = None                        # [-1,+1]; negative = rejection
    reason: str = ""
    reconstructed: bool = False
    reconstruction_quality: str = RECON_UNAVAILABLE

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _clamp_score(self.score))
        _ha_or_none(self.override_target_ha, "override_target_ha")

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "manual_override_occurred": self.manual_override_occurred,
            "override_direction": self.override_direction,
            "override_target_ha": self.override_target_ha,
            "override_delta_ha": self.override_delta_ha,
            "override_delay_seconds": self.override_delay_seconds,
            "override_hold_duration_seconds": self.override_hold_duration_seconds,
            "cleared_by_lifecycle": self.cleared_by_lifecycle,
            "preference_signal_strength": self.preference_signal_strength,
            "score": self.score, "reason": self.reason,
            "reconstructed": self.reconstructed,
            "reconstruction_quality": self.reconstruction_quality,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "PreferenceOutcome":
        if not isinstance(d, dict):
            return cls()
        return cls(
            available=bool(d.get("available", False)),
            manual_override_occurred=bool(d.get("manual_override_occurred", False)),
            override_direction=d.get("override_direction", DIRECTION_UNKNOWN),
            override_target_ha=d.get("override_target_ha"),
            override_delta_ha=d.get("override_delta_ha"),
            override_delay_seconds=d.get("override_delay_seconds"),
            override_hold_duration_seconds=d.get("override_hold_duration_seconds"),
            cleared_by_lifecycle=bool(d.get("cleared_by_lifecycle", False)),
            preference_signal_strength=d.get("preference_signal_strength"),
            score=d.get("score"), reason=d.get("reason", ""),
            reconstructed=bool(d.get("reconstructed", False)),
            reconstruction_quality=d.get("reconstruction_quality", RECON_UNAVAILABLE),
        )


# ---------------------------------------------------------------------------
# OutcomeConfounders — dimension-specific
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OutcomeConfounders:
    """Interpretation hazards.  Dimension-specific flags drive the actual
    downgrades; the raw event flags are diagnostic context."""

    thermal_confounded: bool = False
    movement_confounded: bool = False
    preference_confounded: bool = False
    # Raw events (some reliably detected in P3, others reserved/None until later)
    sensor_unavailable: bool = False
    ha_restart_interruption: bool = False
    config_changed: bool = False
    behavior_mode_changed: bool = False
    safety_event: bool = False
    manual_override: bool = False
    presence_absence_transition: bool = False
    window_door_open: bool | None = None
    strong_forecast_change: bool | None = None
    hvac_influence: bool | None = None
    multiple_windows_changed: bool | None = None
    incomplete_provenance: bool = False
    detected: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "thermal_confounded": self.thermal_confounded,
            "movement_confounded": self.movement_confounded,
            "preference_confounded": self.preference_confounded,
            "sensor_unavailable": self.sensor_unavailable,
            "ha_restart_interruption": self.ha_restart_interruption,
            "config_changed": self.config_changed,
            "behavior_mode_changed": self.behavior_mode_changed,
            "safety_event": self.safety_event,
            "manual_override": self.manual_override,
            "presence_absence_transition": self.presence_absence_transition,
            "window_door_open": self.window_door_open,
            "strong_forecast_change": self.strong_forecast_change,
            "hvac_influence": self.hvac_influence,
            "multiple_windows_changed": self.multiple_windows_changed,
            "incomplete_provenance": self.incomplete_provenance,
            "detected": list(self.detected),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "OutcomeConfounders":
        if not isinstance(d, dict):
            return cls()
        return cls(
            thermal_confounded=bool(d.get("thermal_confounded", False)),
            movement_confounded=bool(d.get("movement_confounded", False)),
            preference_confounded=bool(d.get("preference_confounded", False)),
            sensor_unavailable=bool(d.get("sensor_unavailable", False)),
            ha_restart_interruption=bool(d.get("ha_restart_interruption", False)),
            config_changed=bool(d.get("config_changed", False)),
            behavior_mode_changed=bool(d.get("behavior_mode_changed", False)),
            safety_event=bool(d.get("safety_event", False)),
            manual_override=bool(d.get("manual_override", False)),
            presence_absence_transition=bool(d.get("presence_absence_transition", False)),
            window_door_open=d.get("window_door_open"),
            strong_forecast_change=d.get("strong_forecast_change"),
            hvac_influence=d.get("hvac_influence"),
            multiple_windows_changed=d.get("multiple_windows_changed"),
            incomplete_provenance=bool(d.get("incomplete_provenance", False)),
            detected=tuple(d.get("detected", []) or []),
        )


# ---------------------------------------------------------------------------
# OutcomeReliability
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OutcomeReliability:
    """Per-dimension gate/trust [0,1].  overall is a CONSERVATIVE diagnostic
    summary with NO active learning authority (no weighted magic mixing)."""

    overall: float = 0.0
    thermal: float = 0.0
    movement: float = 0.0
    preference: float = 0.0
    sensor_completeness: float = 0.0
    observation_continuity: float = 0.0
    context_stability: float = 0.0
    attribution_quality: str = ATTRIBUTION_UNKNOWN
    provenance_completeness: float = 0.0
    legacy_data: bool = False
    interrupted: bool = False
    partial: bool = False
    confounded: bool = False
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "overall": self.overall, "thermal": self.thermal,
            "movement": self.movement, "preference": self.preference,
            "sensor_completeness": self.sensor_completeness,
            "observation_continuity": self.observation_continuity,
            "context_stability": self.context_stability,
            "attribution_quality": self.attribution_quality,
            "provenance_completeness": self.provenance_completeness,
            "legacy_data": self.legacy_data, "interrupted": self.interrupted,
            "partial": self.partial, "confounded": self.confounded,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "OutcomeReliability":
        if not isinstance(d, dict):
            return cls()
        return cls(
            overall=float(d.get("overall", 0.0)), thermal=float(d.get("thermal", 0.0)),
            movement=float(d.get("movement", 0.0)), preference=float(d.get("preference", 0.0)),
            sensor_completeness=float(d.get("sensor_completeness", 0.0)),
            observation_continuity=float(d.get("observation_continuity", 0.0)),
            context_stability=float(d.get("context_stability", 0.0)),
            attribution_quality=d.get("attribution_quality", ATTRIBUTION_UNKNOWN),
            provenance_completeness=float(d.get("provenance_completeness", 0.0)),
            legacy_data=bool(d.get("legacy_data", False)),
            interrupted=bool(d.get("interrupted", False)),
            partial=bool(d.get("partial", False)),
            confounded=bool(d.get("confounded", False)),
            reasons=tuple(d.get("reasons", []) or []),
        )


# ---------------------------------------------------------------------------
# MultiObjectiveOutcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MultiObjectiveOutcome:
    """Container embedded additively in DecisionOutcome.multi_objective."""

    thermal: ThermalOutcome = field(default_factory=ThermalOutcome)
    movement: MovementOutcome = field(default_factory=MovementOutcome)
    preference: PreferenceOutcome = field(default_factory=PreferenceOutcome)
    reliability: OutcomeReliability = field(default_factory=OutcomeReliability)
    confounders: OutcomeConfounders = field(default_factory=OutcomeConfounders)
    attribution_quality: str = ATTRIBUTION_UNKNOWN
    resolution_status: str = "pending"
    legacy_score: float | None = None
    schema_version: int = MULTI_OBJECTIVE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "thermal": self.thermal.to_dict(),
            "movement": self.movement.to_dict(),
            "preference": self.preference.to_dict(),
            "reliability": self.reliability.to_dict(),
            "confounders": self.confounders.to_dict(),
            "attribution_quality": self.attribution_quality,
            "resolution_status": self.resolution_status,
            "legacy_score": self.legacy_score,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "MultiObjectiveOutcome | None":
        if not isinstance(d, dict):
            return None
        return cls(
            thermal=ThermalOutcome.from_dict(d.get("thermal")),
            movement=MovementOutcome.from_dict(d.get("movement")),
            preference=PreferenceOutcome.from_dict(d.get("preference")),
            reliability=OutcomeReliability.from_dict(d.get("reliability")),
            confounders=OutcomeConfounders.from_dict(d.get("confounders")),
            attribution_quality=d.get("attribution_quality", ATTRIBUTION_UNKNOWN),
            resolution_status=d.get("resolution_status", "pending"),
            legacy_score=d.get("legacy_score"),
            schema_version=int(d.get("schema_version", MULTI_OBJECTIVE_SCHEMA_VERSION)),
        )
