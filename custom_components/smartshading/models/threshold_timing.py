"""Threshold/Timing learning model — LE 2.0 / Phase P9A (foundation).

Per (window_id, intensity_level, context_family) learned, BOUNDED timing/threshold
characteristics for the Open↔Light↔Normal↔Strong transitions.  P9A only models
and (later) observes these; real changes happen exclusively through bounded P9B
experiments + adoption.  Stores discrete bounded deltas/conditions — never a free
time series as a standing authority.

No Home Assistant import.  Frozen dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

THRESHOLD_TIMING_SCHEMA_VERSION: int = 1

# Bounds for any learned timing/threshold delta (P9B experiments stay within).
MAX_TIMING_DELTA_MIN: int = 15
MAX_ENTRY_THRESHOLD_DELTA_WM2: float = 30.0
MAX_EXIT_THRESHOLD_DELTA_WM2: float = 30.0
MAX_HOLD_DELTA_MIN: int = 10


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


@dataclass(frozen=True)
class ThresholdTimingModel:
    window_id: str
    intensity_level: str
    context_family: str
    # Bounded learned deltas (0 until P9B validates them).
    entry_threshold_delta_wm2: float = 0.0
    exit_threshold_delta_wm2: float = 0.0
    entry_lead_delta_min: int = 0          # negative = earlier entry
    release_delta_min: int = 0             # negative = earlier release
    hold_time_delta_min: int = 0
    entry_hysteresis_wm2: float | None = None
    exit_hysteresis_wm2: float | None = None
    learned_response_delay_min: float | None = None   # mirrors P4
    movement_cost_weight: float | None = None
    confidence: float = 0.0
    reliability: float = 0.0
    distinct_days: int = 0
    config_generation: int = 0
    updated_at: datetime | None = None
    schema_version: int = THRESHOLD_TIMING_SCHEMA_VERSION

    @property
    def model_key(self) -> tuple[str, str, str]:
        return (self.window_id, self.intensity_level, self.context_family)

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id, "intensity_level": self.intensity_level,
            "context_family": self.context_family,
            "entry_threshold_delta_wm2": self.entry_threshold_delta_wm2,
            "exit_threshold_delta_wm2": self.exit_threshold_delta_wm2,
            "entry_lead_delta_min": self.entry_lead_delta_min,
            "release_delta_min": self.release_delta_min,
            "hold_time_delta_min": self.hold_time_delta_min,
            "entry_hysteresis_wm2": self.entry_hysteresis_wm2,
            "exit_hysteresis_wm2": self.exit_hysteresis_wm2,
            "learned_response_delay_min": self.learned_response_delay_min,
            "movement_cost_weight": self.movement_cost_weight,
            "confidence": self.confidence, "reliability": self.reliability,
            "distinct_days": self.distinct_days, "config_generation": self.config_generation,
            "updated_at": _iso(self.updated_at), "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ThresholdTimingModel":
        return cls(
            window_id=d["window_id"], intensity_level=d["intensity_level"],
            context_family=d.get("context_family", "global"),
            entry_threshold_delta_wm2=float(d.get("entry_threshold_delta_wm2", 0.0)),
            exit_threshold_delta_wm2=float(d.get("exit_threshold_delta_wm2", 0.0)),
            entry_lead_delta_min=int(d.get("entry_lead_delta_min", 0)),
            release_delta_min=int(d.get("release_delta_min", 0)),
            hold_time_delta_min=int(d.get("hold_time_delta_min", 0)),
            entry_hysteresis_wm2=d.get("entry_hysteresis_wm2"),
            exit_hysteresis_wm2=d.get("exit_hysteresis_wm2"),
            learned_response_delay_min=d.get("learned_response_delay_min"),
            movement_cost_weight=d.get("movement_cost_weight"),
            confidence=float(d.get("confidence", 0.0)),
            reliability=float(d.get("reliability", 0.0)),
            distinct_days=int(d.get("distinct_days", 0)),
            config_generation=int(d.get("config_generation", 0)),
            updated_at=_parse(d.get("updated_at")),
            schema_version=int(d.get("schema_version", THRESHOLD_TIMING_SCHEMA_VERSION)),
        )
