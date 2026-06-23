"""Thermal Response models — LE 2.0 / Phase P4.

Per-zone (== per config entry) learned thermal-reaction model and the bounded
observation records that feed it.  P4's only active authority is the selection
of the outcome observation window; these models never touch cover targets,
thresholds, shadow or experiments.

Architecture (corrected): one SmartShading config entry corresponds to exactly
one zone, with its own coordinator and its own entry-wide
``indoor_temperature_sensor_ids`` — that list IS the zone's temperature source.
There is no cross-zone averaging and no second temperature source.

Source classification (slim):
    configured_zone_sensors          – ≥1 configured, all currently valid
    configured_zone_sensors_partial  – some valid, some unavailable (reliability ↓)
    no_valid_zone_temperature        – none valid / none configured (thermal paused)

Invariants:
  - No Home Assistant import.  Frozen dataclasses.  Fully serializable.
  - All positions are HA convention (0=closed, 100=open).
  - Each config entry owns its own model/store — nothing is shared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

THERMAL_MODEL_SCHEMA_VERSION: int = 1

# Source classification
SOURCE_CONFIGURED: str = "configured_zone_sensors"
SOURCE_CONFIGURED_PARTIAL: str = "configured_zone_sensors_partial"
SOURCE_NONE: str = "no_valid_zone_temperature"

# Inertia levels
INERTIA_FAST: str = "fast"
INERTIA_MEDIUM: str = "medium"
INERTIA_SLOW: str = "slow"
INERTIA_UNKNOWN: str = "unknown"

# Aggregation methods (diagnostic)
AGG_MEDIAN: str = "median"
AGG_SINGLE: str = "single"
AGG_NONE: str = "none"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


# ---------------------------------------------------------------------------
# ThermalResponseObservation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThermalResponseObservation:
    """One bounded, thermally-usable zone observation.

    indoor_samples is a SPARSE sequence of (offset_min, temp) points (≤ 6),
    never a full high-frequency time series.  decision_ids may contain more
    than one id when several windows of the same zone changed together
    (zone-shared event, single observation, weight 1).
    """

    zone_id: str
    decision_ids: tuple[str, ...]
    started_at: datetime
    ended_at: datetime
    observation_duration_min: float
    indoor_start: float | None
    indoor_end: float | None
    indoor_samples: tuple[tuple[int, float], ...] = ()
    outdoor_start: float | None = None
    outdoor_end: float | None = None
    solar_start: float | None = None
    solar_end: float | None = None
    shading_state: str = ""
    target_before_ha: int | None = None
    target_after_ha: int | None = None
    thermal_available: bool = False
    thermal_score: float | None = None
    thermal_direction: str | None = None
    attribution_quality: str = "unknown"
    source_kind: str = SOURCE_NONE
    valid_sensor_count: int = 0
    configured_sensor_count: int = 0
    aggregation_method: str = AGG_NONE
    reliability: float = 0.0
    context_key: str = "global"
    confounded: bool = False
    config_generation: int = 0

    @property
    def timestamp(self) -> datetime:
        return self.started_at

    @property
    def indoor_delta_c(self) -> float | None:
        if self.indoor_start is None or self.indoor_end is None:
            return None
        return self.indoor_end - self.indoor_start

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "decision_ids": list(self.decision_ids),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "observation_duration_min": self.observation_duration_min,
            "indoor_start": self.indoor_start,
            "indoor_end": self.indoor_end,
            "indoor_samples": [list(s) for s in self.indoor_samples],
            "outdoor_start": self.outdoor_start, "outdoor_end": self.outdoor_end,
            "solar_start": self.solar_start, "solar_end": self.solar_end,
            "shading_state": self.shading_state,
            "target_before_ha": self.target_before_ha,
            "target_after_ha": self.target_after_ha,
            "thermal_available": self.thermal_available,
            "thermal_score": self.thermal_score,
            "thermal_direction": self.thermal_direction,
            "attribution_quality": self.attribution_quality,
            "source_kind": self.source_kind,
            "valid_sensor_count": self.valid_sensor_count,
            "configured_sensor_count": self.configured_sensor_count,
            "aggregation_method": self.aggregation_method,
            "reliability": self.reliability,
            "context_key": self.context_key,
            "confounded": self.confounded,
            "config_generation": self.config_generation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ThermalResponseObservation":
        samples = tuple(
            (int(s[0]), float(s[1])) for s in (d.get("indoor_samples") or [])
            if isinstance(s, (list, tuple)) and len(s) == 2
        )
        return cls(
            zone_id=d["zone_id"],
            decision_ids=tuple(d.get("decision_ids", []) or []),
            started_at=_parse(d["started_at"]),  # type: ignore[arg-type]
            ended_at=_parse(d["ended_at"]),  # type: ignore[arg-type]
            observation_duration_min=float(d.get("observation_duration_min", 0.0)),
            indoor_start=d.get("indoor_start"), indoor_end=d.get("indoor_end"),
            indoor_samples=samples,
            outdoor_start=d.get("outdoor_start"), outdoor_end=d.get("outdoor_end"),
            solar_start=d.get("solar_start"), solar_end=d.get("solar_end"),
            shading_state=d.get("shading_state", ""),
            target_before_ha=d.get("target_before_ha"),
            target_after_ha=d.get("target_after_ha"),
            thermal_available=bool(d.get("thermal_available", False)),
            thermal_score=d.get("thermal_score"),
            thermal_direction=d.get("thermal_direction"),
            attribution_quality=d.get("attribution_quality", "unknown"),
            source_kind=d.get("source_kind", SOURCE_NONE),
            valid_sensor_count=int(d.get("valid_sensor_count", 0)),
            configured_sensor_count=int(d.get("configured_sensor_count", 0)),
            aggregation_method=d.get("aggregation_method", AGG_NONE),
            reliability=float(d.get("reliability", 0.0)),
            context_key=d.get("context_key", "global"),
            confounded=bool(d.get("confounded", False)),
            config_generation=int(d.get("config_generation", 0)),
        )


# ---------------------------------------------------------------------------
# ContextThermalModel
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextThermalModel:
    """Per-context-bucket thermal sub-model.  active only with enough evidence."""

    context_key: str
    response_onset_minutes: float | None = None
    effective_observation_minutes: float | None = None
    typical_temperature_response_c: float | None = None
    sample_count: int = 0
    distinct_days: int = 0
    confidence: float = 0.0
    active: bool = False

    def to_dict(self) -> dict:
        return {
            "context_key": self.context_key,
            "response_onset_minutes": self.response_onset_minutes,
            "effective_observation_minutes": self.effective_observation_minutes,
            "typical_temperature_response_c": self.typical_temperature_response_c,
            "sample_count": self.sample_count, "distinct_days": self.distinct_days,
            "confidence": self.confidence, "active": self.active,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ContextThermalModel":
        return cls(
            context_key=d["context_key"],
            response_onset_minutes=d.get("response_onset_minutes"),
            effective_observation_minutes=d.get("effective_observation_minutes"),
            typical_temperature_response_c=d.get("typical_temperature_response_c"),
            sample_count=int(d.get("sample_count", 0)),
            distinct_days=int(d.get("distinct_days", 0)),
            confidence=float(d.get("confidence", 0.0)),
            active=bool(d.get("active", False)),
        )


# ---------------------------------------------------------------------------
# ThermalResponseModel
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThermalResponseModel:
    """Per-zone learned thermal-reaction model.  Distinct aspects (onset,
    effective observation window, inertia, magnitude) are modelled separately —
    no single mean conflates them."""

    zone_id: str
    schema_version: int = THERMAL_MODEL_SCHEMA_VERSION
    response_onset_minutes: float | None = None
    effective_observation_minutes: float | None = None
    response_duration_minutes: float | None = None
    thermal_inertia_level: str = INERTIA_UNKNOWN
    typical_temperature_response_c: float | None = None
    expected_response_direction: str = "unknown"
    confidence: float = 0.0
    sample_count: int = 0
    distinct_days: int = 0
    unconfounded_sample_count: int = 0
    source_kind: str = SOURCE_NONE
    config_generation: int = 0
    context_models: dict[str, ContextThermalModel] = field(default_factory=dict)
    last_updated: datetime | None = None
    fallback_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "schema_version": self.schema_version,
            "response_onset_minutes": self.response_onset_minutes,
            "effective_observation_minutes": self.effective_observation_minutes,
            "response_duration_minutes": self.response_duration_minutes,
            "thermal_inertia_level": self.thermal_inertia_level,
            "typical_temperature_response_c": self.typical_temperature_response_c,
            "expected_response_direction": self.expected_response_direction,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "distinct_days": self.distinct_days,
            "unconfounded_sample_count": self.unconfounded_sample_count,
            "source_kind": self.source_kind,
            "config_generation": self.config_generation,
            "context_models": {k: v.to_dict() for k, v in self.context_models.items()},
            "last_updated": _iso(self.last_updated),
            "fallback_reason": self.fallback_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ThermalResponseModel":
        ctx = {
            k: ContextThermalModel.from_dict(v)
            for k, v in (d.get("context_models") or {}).items()
            if isinstance(v, dict)
        }
        return cls(
            zone_id=d["zone_id"],
            schema_version=int(d.get("schema_version", THERMAL_MODEL_SCHEMA_VERSION)),
            response_onset_minutes=d.get("response_onset_minutes"),
            effective_observation_minutes=d.get("effective_observation_minutes"),
            response_duration_minutes=d.get("response_duration_minutes"),
            thermal_inertia_level=d.get("thermal_inertia_level", INERTIA_UNKNOWN),
            typical_temperature_response_c=d.get("typical_temperature_response_c"),
            expected_response_direction=d.get("expected_response_direction", "unknown"),
            confidence=float(d.get("confidence", 0.0)),
            sample_count=int(d.get("sample_count", 0)),
            distinct_days=int(d.get("distinct_days", 0)),
            unconfounded_sample_count=int(d.get("unconfounded_sample_count", 0)),
            source_kind=d.get("source_kind", SOURCE_NONE),
            config_generation=int(d.get("config_generation", 0)),
            context_models=ctx,
            last_updated=_parse(d.get("last_updated")),
            fallback_reason=d.get("fallback_reason"),
        )
