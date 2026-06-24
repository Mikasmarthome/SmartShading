"""Shading Strategy Resolver — LE 2.0 / Phase P9A (pure, observe/recommend).

Produces a ShadingStrategyCandidate each cycle from the current state, the
measured exposure, the unified effective solar thresholds, forecast load
features and the thermal context.  It expresses the CURRENTLY appropriate state
and the conditions for the next transitions — never a fixed Light→Normal→Light
sequence.  In P9A this is recommendation/shadow only and carries no control
authority.

No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.shading_strategy import (
    STATE_LIGHT,
    STATE_NORMAL,
    STATE_OPEN,
    STATE_STRONG,
    ForecastLoadFeatures,
    ShadingStrategyCandidate,
)
from .solar_threshold_resolver import TRUST_TRUSTED

# Default exit hysteresis: a state is released when exposure drops a margin below
# its entry threshold (prevents Light↔Normal ping-pong in the shadow model).
DEFAULT_EXIT_HYSTERESIS_WM2: float = 40.0


@dataclass(frozen=True)
class StrategyResolverInput:
    window_id: str
    zone_id: str
    context_family: str
    current_state: str
    in_solar_sector: bool
    measured_exposure_wm2: float | None
    light_threshold_wm2: float
    normal_threshold_wm2: float
    strong_threshold_wm2: float
    forecast: ForecastLoadFeatures
    confidence: float = 0.0
    reliability: float = 0.0
    exit_hysteresis_wm2: float = DEFAULT_EXIT_HYSTERESIS_WM2


def _state_for_exposure(expo: float, light: float, normal: float, strong: float) -> str:
    if expo >= strong:
        return STATE_STRONG
    if expo >= normal:
        return STATE_NORMAL
    if expo >= light:
        return STATE_LIGHT
    return STATE_OPEN


def resolve_strategy(inp: StrategyResolverInput) -> ShadingStrategyCandidate:
    """Return a non-authoritative ShadingStrategyCandidate for the current cycle."""
    reasons: list[str] = []
    expo = inp.measured_exposure_wm2

    if not inp.in_solar_sector or expo is None:
        recommended = STATE_OPEN
        reasons.append("not_in_solar_sector" if not inp.in_solar_sector else "no_measured_exposure")
    else:
        recommended = _state_for_exposure(
            expo, inp.light_threshold_wm2, inp.normal_threshold_wm2, inp.strong_threshold_wm2)
        reasons.append("measured_exposure_state")

    # Forecast precaution (only at full trust): a long, high expected load while
    # currently open may justify an earlier moderate start — surfaced as a
    # recommendation/reason, not forced.
    fc = inp.forecast
    if (fc.available and fc.trust_level == TRUST_TRUSTED
            and (fc.expected_load_duration_min or 0) >= 120
            and (fc.expected_load_peak_wm2 or 0) >= inp.normal_threshold_wm2):
        reasons.append("forecast_long_high_load_early_moderate")
    elif fc.available and fc.trust_level != TRUST_TRUSTED:
        reasons.append("forecast_low_trust_no_preshade")

    # Allowed next states: all semantic states are reachable (no fixed sequence);
    # a stage may be skipped or fully open is always allowed.
    allowed = tuple(s for s in (STATE_OPEN, STATE_LIGHT, STATE_NORMAL, STATE_STRONG)
                    if s != inp.current_state)

    entry = {
        STATE_LIGHT: {"solar_wm2_at_least": inp.light_threshold_wm2},
        STATE_NORMAL: {"solar_wm2_at_least": inp.normal_threshold_wm2},
        STATE_STRONG: {"solar_wm2_at_least": inp.strong_threshold_wm2},
    }
    exit_ = {
        STATE_LIGHT: {"solar_wm2_below": inp.light_threshold_wm2 - inp.exit_hysteresis_wm2},
        STATE_NORMAL: {"solar_wm2_below": inp.normal_threshold_wm2 - inp.exit_hysteresis_wm2},
        STATE_STRONG: {"solar_wm2_below": inp.strong_threshold_wm2 - inp.exit_hysteresis_wm2},
    }
    return ShadingStrategyCandidate(
        window_id=inp.window_id, zone_id=inp.zone_id, context_family=inp.context_family,
        current_state=inp.current_state, recommended_state_now=recommended,
        allowed_next_states=allowed, entry_conditions_by_state=entry,
        exit_conditions_by_state=exit_, hold_constraints_by_state={},
        forecast_load_features=fc, thermal_context=inp.context_family,
        confidence=inp.confidence, reliability=inp.reliability,
        reason_codes=tuple(reasons),
    )
