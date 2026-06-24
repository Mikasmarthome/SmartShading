"""Unified Solar Threshold Resolver — LE 2.0 / Phase P9A.

Replaces the previously SEQUENTIAL solar-threshold mutation (adaptive solar
escalation THEN forecast strategy modifier, each with its own clamp) with a
single composition: the learned delta and the forecast delta are each applied
exactly once and a single final clamp is enforced per intensity.

Source authority (verbindlich):
  - The current measured solar irradiance stays authoritative for the real
    current load (this module only adjusts the *entry thresholds*, never the
    measured value).
  - Forecast only adjusts the precautionary threshold/strategy planning.

Pure Python, no Home Assistant dependency.
"""
from __future__ import annotations

from dataclasses import dataclass

# Hard W/m² clamps (canonical home; mirror the historical forecast-modifier
# bounds so behavior stays within the same safety envelope).
LIGHT_FLOOR_WM2: float = 50.0
LIGHT_CEIL_WM2: float = 400.0
NORMAL_FLOOR_WM2: float = 100.0
NORMAL_CEIL_WM2: float = 600.0
STRONG_FLOOR_WM2: float = 200.0
STRONG_CEIL_WM2: float = 800.0

# Forecast trust gate levels (sharpening 5).
TRUST_TRUSTED: str = "forecast_trusted"
TRUST_PARTIAL: str = "forecast_partially_trusted"
TRUST_UNTRUSTED: str = "forecast_untrusted"
TRUST_UNAVAILABLE: str = "forecast_unavailable"

TRUST_FULL_MIN: float = 0.70      # ≥ → precautionary strategy may be used
TRUST_PARTIAL_MIN: float = 0.50   # [0.50,0.70) → conservative only


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def classify_forecast_trust_level(available: bool, trust_score: float | None) -> str:
    """Map availability + trust score to a discrete strategy gate level."""
    if not available or trust_score is None:
        return TRUST_UNAVAILABLE
    if trust_score >= TRUST_FULL_MIN:
        return TRUST_TRUSTED
    if trust_score >= TRUST_PARTIAL_MIN:
        return TRUST_PARTIAL
    return TRUST_UNTRUSTED


@dataclass(frozen=True)
class SolarThresholdResolution:
    effective_light_wm2: float
    effective_normal_wm2: float
    effective_strong_wm2: float
    applied_learned_delta_light: float
    applied_learned_delta_normal: float
    applied_learned_delta_strong: float
    applied_forecast_delta: float
    forecast_trust_level: str
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "effective_light_wm2": round(self.effective_light_wm2, 2),
            "effective_normal_wm2": round(self.effective_normal_wm2, 2),
            "effective_strong_wm2": round(self.effective_strong_wm2, 2),
            "applied_learned_delta_light": round(self.applied_learned_delta_light, 2),
            "applied_learned_delta_normal": round(self.applied_learned_delta_normal, 2),
            "applied_learned_delta_strong": round(self.applied_learned_delta_strong, 2),
            "applied_forecast_delta": round(self.applied_forecast_delta, 2),
            "forecast_trust_level": self.forecast_trust_level,
            "reason_codes": list(self.reason_codes),
        }


def resolve_solar_thresholds(
    *,
    configured_light_wm2: float,
    configured_normal_wm2: float,
    configured_strong_wm2: float,
    learned_delta_light: float = 0.0,
    learned_delta_normal: float = 0.0,
    learned_delta_strong: float = 0.0,
    forecast_delta_wm2: float = 0.0,
    forecast_available: bool = False,
    forecast_trust_score: float | None = None,
) -> SolarThresholdResolution:
    """Compose configured + learned + forecast deltas ONCE, with one final clamp.

    The learned delta is the per-intensity threshold change produced by the
    adaptive profile (extracted by the caller as adapted − configured); the
    forecast delta is the single bounded forecast strategy delta.  No threshold
    is mutated twice and no intermediate clamp is applied.
    """
    level = classify_forecast_trust_level(forecast_available, forecast_trust_score)
    # Forecast precaution only at full trust (mirrors the existing applied gate);
    # the caller already passes 0.0 when the forecast modifier was not applied.
    fc = forecast_delta_wm2

    reasons: list[str] = []
    if any((learned_delta_light, learned_delta_normal, learned_delta_strong)):
        reasons.append("learned_solar_delta")
    if fc != 0.0:
        reasons.append("forecast_solar_delta")
    if not reasons:
        reasons.append("configured_only")

    eff_light = _clamp(configured_light_wm2 + learned_delta_light + fc,
                       LIGHT_FLOOR_WM2, LIGHT_CEIL_WM2)
    eff_normal = _clamp(configured_normal_wm2 + learned_delta_normal + fc,
                        NORMAL_FLOOR_WM2, NORMAL_CEIL_WM2)
    eff_strong = _clamp(configured_strong_wm2 + learned_delta_strong + fc,
                        STRONG_FLOOR_WM2, STRONG_CEIL_WM2)
    return SolarThresholdResolution(
        effective_light_wm2=eff_light, effective_normal_wm2=eff_normal,
        effective_strong_wm2=eff_strong,
        applied_learned_delta_light=learned_delta_light,
        applied_learned_delta_normal=learned_delta_normal,
        applied_learned_delta_strong=learned_delta_strong,
        applied_forecast_delta=fc, forecast_trust_level=level,
        reason_codes=tuple(reasons),
    )
