"""Shading Strategy models — LE 2.0 / Phase P9A (foundation, observe/recommend).

A ShadingStrategyCandidate describes the CURRENTLY appropriate shading state and
the conditions for the next transitions.  It is NOT a fixed daily sequence: the
engine decides the appropriate state each cycle, and any day-long sequence
emerges only from successive decisions.

P9A produces these candidates in observe/recommend/shadow mode only — they carry
no real control authority (that is P9B).  Non-causal: limitations always include
'not_causally_validated'.

No Home Assistant import.  HA convention (0=closed, 100=open).
"""
from __future__ import annotations

from dataclasses import dataclass, field

STRATEGY_SCHEMA_VERSION: int = 1
CAUSAL_LIMITATION: str = "not_causally_validated"

# Semantic shading states the strategy may recommend (no fixed order).
STATE_OPEN: str = "open"
STATE_LIGHT: str = "light"
STATE_NORMAL: str = "normal"
STATE_STRONG: str = "strong"
STRATEGY_STATES: tuple[str, ...] = (STATE_OPEN, STATE_LIGHT, STATE_NORMAL, STATE_STRONG)


@dataclass(frozen=True)
class ForecastLoadFeatures:
    """Horizon features (not just the peak)."""

    available: bool = False
    trust_level: str = "forecast_unavailable"
    expected_load_start_min: float | None = None      # minutes from now
    expected_load_duration_min: float | None = None
    expected_load_peak_wm2: float | None = None
    expected_cumulative_load: float | None = None
    sun_elevation: float | None = None
    sun_relative_azimuth: float | None = None

    def to_dict(self) -> dict:
        return {
            "available": self.available, "trust_level": self.trust_level,
            "expected_load_start_min": self.expected_load_start_min,
            "expected_load_duration_min": self.expected_load_duration_min,
            "expected_load_peak_wm2": self.expected_load_peak_wm2,
            "expected_cumulative_load": self.expected_cumulative_load,
            "sun_elevation": self.sun_elevation,
            "sun_relative_azimuth": self.sun_relative_azimuth,
        }


@dataclass(frozen=True)
class ShadingStrategyCandidate:
    window_id: str
    zone_id: str
    context_family: str
    current_state: str
    recommended_state_now: str
    allowed_next_states: tuple[str, ...] = ()
    entry_conditions_by_state: dict = field(default_factory=dict)   # state → condition dict
    exit_conditions_by_state: dict = field(default_factory=dict)
    hold_constraints_by_state: dict = field(default_factory=dict)
    forecast_load_features: ForecastLoadFeatures = field(default_factory=ForecastLoadFeatures)
    thermal_context: str = "global"
    expected_benefit: float | None = None
    movement_cost: float | None = None
    confidence: float = 0.0
    reliability: float = 0.0
    reason_codes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (CAUSAL_LIMITATION,)
    schema_version: int = STRATEGY_SCHEMA_VERSION

    def to_dict(self) -> dict:
        lims = tuple(self.limitations)
        if CAUSAL_LIMITATION not in lims:
            lims = lims + (CAUSAL_LIMITATION,)
        return {
            "window_id": self.window_id, "zone_id": self.zone_id,
            "context_family": self.context_family, "current_state": self.current_state,
            "recommended_state_now": self.recommended_state_now,
            "allowed_next_states": list(self.allowed_next_states),
            "entry_conditions_by_state": self.entry_conditions_by_state,
            "exit_conditions_by_state": self.exit_conditions_by_state,
            "hold_constraints_by_state": self.hold_constraints_by_state,
            "forecast_load_features": self.forecast_load_features.to_dict(),
            "thermal_context": self.thermal_context, "expected_benefit": self.expected_benefit,
            "movement_cost": self.movement_cost, "confidence": self.confidence,
            "reliability": self.reliability, "reason_codes": list(self.reason_codes),
            "limitations": list(lims), "schema_version": self.schema_version,
        }
