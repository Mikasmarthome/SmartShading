"""Forecast Trust based Strategy Modifier.

Uses historical forecast trust data and the current/near-future forecast
value to compute a bounded adjustment to solar activation thresholds.

Architecture invariants
-----------------------
- NEVER commands covers directly.
- NEVER overrides Safety (Tier 1), Manual Override (Tier 2), or Lifecycle (Tier 3).
- Applied AFTER AdaptationApplication, BEFORE the TierOrchestrator.
- Effect is bounded by FORECAST_MAX_DELTA_WM2 in each direction.
- No-op when trust data is unavailable, insufficient, or below the gate.
- No-op when no current/near-future forecast snapshot is available.
- Pure Python, no HA dependency.

Gate conditions (ALL must pass for modification to apply)
----------------------------------------------------------
1. ForecastLearningStore is non-None and has ForecastRecords.
2. At least one variable (SOLAR_IRRADIANCE or CLOUD_COVERAGE) has a
   trust-ready bucket (sample_count ≥ 30, distinct_days ≥ 10).
3. overall_trust_score ≥ FORECAST_MIN_TRUST_SCORE (0.70).
4. At least one ForecastSnapshot with target time within
   [now − 60 min, now + 4 h] is present.

Threshold adjustment direction
-------------------------------
Sunny forecast (high solar, low cloud coverage):
    → negative delta → lower thresholds → shade slightly earlier
Mild/cloudy forecast (low solar, high cloud coverage):
    → positive delta → raise thresholds → shade slightly later/less

The delta is scaled by both forecast extremity and trust score:
    solar_factor = clamp((solar_wm2 − 200) / 300, −1, +1)
    cloud_factor = clamp((50 − cloud_pct) / 50, −1, +1)
    combined = average of available factors
    delta = −combined × trust_score × FORECAST_MAX_DELTA_WM2

Hard threshold clamps in apply_forecast_modifier() prevent extreme outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..engines.forecast_trust_engine import (
    compute_forecast_trust,
    compute_forecast_trust_summary,
)
from ..models.forecast_learning import ForecastTrustInput, ForecastVariable
from ..models.forecast_snapshots import ForecastSnapshot

if TYPE_CHECKING:
    from ..models.forecast_store import ForecastLearningStore
    from ..models.behavior_config import BehaviorConfig


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

FORECAST_MIN_TRUST_SCORE: float = 0.70
FORECAST_MAX_DELTA_WM2: float = 30.0

# Forecast snapshot time window around now that counts as "current"
_LOOKBACK_MINUTES: int = 60
_LOOKAHEAD_HOURS: int = 4

# Hard clamps applied AFTER the delta (absolute threshold floors/ceilings)
_LIGHT_FLOOR_WM2: float = 50.0
_LIGHT_CEIL_WM2: float = 400.0
_NORMAL_FLOOR_WM2: float = 100.0
_NORMAL_CEIL_WM2: float = 600.0
_STRONG_FLOOR_WM2: float = 200.0
_STRONG_CEIL_WM2: float = 800.0


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastStrategyModifier:
    """Bounded threshold delta computed from forecast trust + current forecast.

    threshold_delta_wm2
        Value to ADD to all three solar entry thresholds.
        Negative  → lower thresholds → earlier/stronger shading (sunny forecast).
        Positive  → raise thresholds → later/lighter shading (mild forecast).
        Zero when applied=False.
    applied
        True when all gate conditions passed and the delta is non-zero.
    trust_score
        The overall_trust_score used; None when trust was not available.
    forecast_solar_wm2
        The nearest forecast solar irradiance value used; None when absent.
    forecast_cloud_pct
        The nearest forecast cloud coverage value used; None when absent.
    reason
        Short machine-readable reason string for debug/trace attributes.
    """

    threshold_delta_wm2: float
    applied: bool
    trust_score: float | None
    forecast_solar_wm2: float | None
    forecast_cloud_pct: float | None
    reason: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _no_modifier(reason: str) -> ForecastStrategyModifier:
    return ForecastStrategyModifier(
        threshold_delta_wm2=0.0,
        applied=False,
        trust_score=None,
        forecast_solar_wm2=None,
        forecast_cloud_pct=None,
        reason=reason,
    )


def _pick_nearest_snapshot(
    snapshots: list[ForecastSnapshot],
    variable: ForecastVariable,
    now: datetime,
) -> float | None:
    """Return the forecast_value of the snapshot closest to *now* for *variable*."""
    candidates = [s for s in snapshots if s.variable is variable]
    if not candidates:
        return None
    best = min(candidates, key=lambda s: abs((s.forecast_target_utc - now).total_seconds()))
    return best.forecast_value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_forecast_strategy_modifier(
    store: ForecastLearningStore,
    now: datetime,
) -> ForecastStrategyModifier:
    """Compute a ForecastStrategyModifier from the live ForecastLearningStore.

    Pure function except for datetime.now() fallback (controlled by *now*).
    Safe to call every coordinator cycle — returns a no-op modifier quickly
    when any gate condition is unmet.

    Parameters
    ----------
    store:
        The live ForecastLearningStore (may have zero records on first start).
    now:
        Current UTC timestamp (passed by the coordinator for determinism).
    """
    # Gate 1: records present
    all_records = list(store.forecast_records.values())
    if not all_records:
        return _no_modifier("no_forecast_records")

    # Build per-variable trust results (SOLAR_IRRADIANCE + CLOUD_COVERAGE only)
    trust_results = []
    for variable in (ForecastVariable.SOLAR_IRRADIANCE, ForecastVariable.CLOUD_COVERAGE):
        var_records = [r for r in all_records if r.variable is variable]
        if var_records:
            inp = ForecastTrustInput(records=var_records, variable=variable)
            trust_results.append(compute_forecast_trust(inp, computed_at_utc=now))

    if not trust_results:
        return _no_modifier("no_trust_variables")

    # Gate 2: at least one variable trust-ready
    # any_bucket_ready is on per-variable ForecastTrustResult, not on the summary.
    _any_ready = any(r.any_bucket_ready for r in trust_results)
    summary = compute_forecast_trust_summary(tuple(trust_results), computed_at_utc=now)
    if not _any_ready or summary.overall_trust_score is None:
        return _no_modifier("trust_not_ready")

    # Gate 3: trust score meets minimum
    if summary.overall_trust_score < FORECAST_MIN_TRUST_SCORE:
        return ForecastStrategyModifier(
            threshold_delta_wm2=0.0,
            applied=False,
            trust_score=summary.overall_trust_score,
            forecast_solar_wm2=None,
            forecast_cloud_pct=None,
            reason="trust_below_threshold",
        )

    # Gate 4: current/near-future forecast snapshot available
    window_start = now - timedelta(minutes=_LOOKBACK_MINUTES)
    window_end = now + timedelta(hours=_LOOKAHEAD_HOURS)
    relevant = [
        s for s in store.forecast_snapshots.values()
        if window_start <= s.forecast_target_utc <= window_end
    ]

    if not relevant:
        return ForecastStrategyModifier(
            threshold_delta_wm2=0.0,
            applied=False,
            trust_score=summary.overall_trust_score,
            forecast_solar_wm2=None,
            forecast_cloud_pct=None,
            reason="no_current_forecast_snapshot",
        )

    forecast_solar = _pick_nearest_snapshot(relevant, ForecastVariable.SOLAR_IRRADIANCE, now)
    forecast_cloud = _pick_nearest_snapshot(relevant, ForecastVariable.CLOUD_COVERAGE, now)

    if forecast_solar is None and forecast_cloud is None:
        return ForecastStrategyModifier(
            threshold_delta_wm2=0.0,
            applied=False,
            trust_score=summary.overall_trust_score,
            forecast_solar_wm2=None,
            forecast_cloud_pct=None,
            reason="no_relevant_forecast_variables",
        )

    # Compute combined factor in [-1.0, +1.0]
    # solar_factor > 0  = sunny; cloud_factor > 0 = sunny
    factors = []
    if forecast_solar is not None:
        factors.append(_clamp((forecast_solar - 200.0) / 300.0, -1.0, 1.0))
    if forecast_cloud is not None:
        factors.append(_clamp((50.0 - forecast_cloud) / 50.0, -1.0, 1.0))
    combined = sum(factors) / len(factors)

    # Negative delta = sunny → lower thresholds → earlier/stronger shading
    delta = _clamp(
        -combined * summary.overall_trust_score * FORECAST_MAX_DELTA_WM2,
        -FORECAST_MAX_DELTA_WM2,
        FORECAST_MAX_DELTA_WM2,
    )

    return ForecastStrategyModifier(
        threshold_delta_wm2=delta,
        applied=True,
        trust_score=summary.overall_trust_score,
        forecast_solar_wm2=forecast_solar,
        forecast_cloud_pct=forecast_cloud,
        reason="applied",
    )


def apply_forecast_modifier(
    bc: BehaviorConfig,
    modifier: ForecastStrategyModifier,
) -> BehaviorConfig:
    """Return a BehaviorConfig with solar thresholds adjusted by *modifier*.

    If *modifier* is not applied, returns *bc* unchanged (same object).
    Hard clamps are applied after the delta to prevent extreme outcomes.
    """
    if not modifier.applied or modifier.threshold_delta_wm2 == 0.0:
        return bc

    from dataclasses import replace
    delta = modifier.threshold_delta_wm2
    return replace(
        bc,
        light_shade_threshold_wm2=_clamp(
            bc.light_shade_threshold_wm2 + delta, _LIGHT_FLOOR_WM2, _LIGHT_CEIL_WM2
        ),
        normal_shade_threshold_wm2=_clamp(
            bc.normal_shade_threshold_wm2 + delta, _NORMAL_FLOOR_WM2, _NORMAL_CEIL_WM2
        ),
        strong_shade_threshold_wm2=_clamp(
            bc.strong_shade_threshold_wm2 + delta, _STRONG_FLOOR_WM2, _STRONG_CEIL_WM2
        ),
    )
