"""Solar Impact Learning Engine — Phase 9F8.

Answers: How strongly does sunlight affect this specific window?

Input:
  SolarImpactInput.situations  — pool of SituationRecords for the window
  SolarImpactInput.confidence_result — ConfidenceResult gate (may be None)

Output:
  SolarImpactResult — three-layer solar factor and gate diagnostics

---

Architecture: Model C (Global + Situational)
--------------------------------------------

  Global Solar Factor
    Computed from ALL valid situations regardless of solar angle.
    Uses the MEDIAN of indoor_temp_delta_c (robust against outlier heat-wave
    days and faulty sensor readings).  Provides a baseline factor even when
    data is sparse.

  Situational Solar Factor
    Exposure-weighted mean of indoor_temp_delta_c.  High-exposure situations
    contribute more because the causal link between sun and temperature is
    strongest at high irradiance.  Low-exposure observations are noisy proxies
    for solar impact.
    Formula:  Σ(delta_c × exposure_wm2) / Σ(exposure_wm2)

  Combined Solar Factor
    combined = 0.40 × global + 0.60 × situational
    If situational_factor is None (no exposure data) → combined = global
    If global_factor is None (no delta data at all) → combined = None

  The 40/60 split means situational context takes precedence once available,
  but the global median anchors the result against situational noise.

---

Valid situations filter
-----------------------

Only situations with  resolution_status == "complete"  and a non-None
indoor_temp_delta_c are processed.  "complete" guarantees that the
observation window elapsed in full and that the thermal delta was recorded.

---

Gate order (all must pass for learning_active = True)
------------------------------------------------------

  1. Confidence gate   — confidence_result not None and level >= MEDIUM
  2. Sample gate       — sample_count >= 10
  3. Distinct-day gate — distinct_days >= 5

Any gate failure → learning_active = False, all factors = None.
sample_count and distinct_days are always reported, even when learning is off.

---

Why MEDIAN for the global factor?
----------------------------------

Solar heat data is prone to:
  • Single extreme heat-wave days → elevated indoor_temp_delta_c
  • Faulty temperature sensors   → occasional impossible readings
  • Measurement during cold weather → near-zero or negative deltas

Mean would be pulled by outliers on both ends.  Median consistently returns
the "typical" thermal response of the window and is fully insensitive to
values beyond the sample midpoint.

---

Solar Similarity Function (compute_solar_similarity)
-----------------------------------------------------

Compares two NormalizedSituations using solar-specific feature weights.
No mandatory filters on lifecycle_state or decided_by: the same solar angle
produces useful data regardless of whether shading was active.

Feature weights:
  effective_exposure_norm     0.50
  solar_relative_azimuth_norm 0.25
  sun_elevation_norm          0.15
  outdoor_temp_norm           0.10

None redistribution: identical to SimilarityCalculator.
All features None → 0.0.

This function is a utility for future solar-specific neighbourhood queries
(e.g., querying similar solar conditions for a real-time evaluation).

---

Tier safety
-----------

This module is purely analytical.  It produces a SolarImpactResult.
No HeatEvaluator threshold is changed.  No runtime state is modified.
No Tier 1–5 logic is affected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .confidence_engine import ConfidenceLevel, ConfidenceResult
from .feature_normalizer import NormalizedSituation, normalize_situation
from .situation_joiner import SituationRecord


# ---------------------------------------------------------------------------
# Architecture constants
# ---------------------------------------------------------------------------

_GATE_OPEN_LEVELS: frozenset[ConfidenceLevel] = frozenset(
    {ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH, ConfidenceLevel.VERY_HIGH}
)

_MIN_SAMPLE_COUNT:  int   = 10  # minimum complete situations with temp delta
_MIN_DISTINCT_DAYS: int   = 5   # minimum distinct calendar days in the pool

_GLOBAL_WEIGHT:      float = 0.40
_SITUATIONAL_WEIGHT: float = 0.60

_RESOLUTION_COMPLETE: str = "complete"

# Solar-specific feature weights — independent of SimilarityCalculator weights.
# Exposure dominates: higher irradiance → cleaner thermal causal signal.
# Indoor temperature is intentionally excluded so that the similarity reflects
# the *solar input*, not the thermal state that is the *output* we are measuring.
_SOLAR_FEATURE_WEIGHTS: list[tuple[str, float]] = [
    ("effective_exposure_norm",     0.50),
    ("solar_relative_azimuth_norm", 0.25),
    ("sun_elevation_norm",          0.15),
    ("outdoor_temp_norm",           0.10),
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SolarImpactInput:
    """Input to compute_solar_impact().

    situations       — all SituationRecords for the window (may include
                       partial and unresolved records; filtering is internal)
    confidence_result — from ConfidenceEngine; None when confidence cannot yet
                        be computed (treated as gate-closed)
    """

    situations:        list[SituationRecord]
    confidence_result: ConfidenceResult | None


@dataclass(frozen=True)
class SolarImpactResult:
    """Output of compute_solar_impact().

    global_solar_factor      — median indoor_temp_delta_c over all valid
                               situations (°C); None when no data
    situational_solar_factor — exposure-weighted mean of indoor_temp_delta_c
                               (°C); None when no exposure data
    combined_solar_factor    — 0.40×global + 0.60×situational; falls back to
                               global when situational is None; None when
                               global is also None
    sample_count             — number of valid (complete + delta) situations
    distinct_days            — distinct calendar days in valid pool
    confidence_gate_passed   — whether confidence_result.level >= MEDIUM
    learning_active          — True only when all three gates passed
    """

    global_solar_factor:      float | None
    situational_solar_factor: float | None
    combined_solar_factor:    float | None
    sample_count:             int
    distinct_days:            int
    confidence_gate_passed:   bool
    learning_active:          bool


# ---------------------------------------------------------------------------
# Solar similarity function
# ---------------------------------------------------------------------------

def compute_solar_similarity(a: NormalizedSituation, b: NormalizedSituation) -> float:
    """Solar-weighted similarity between two NormalizedSituations.

    Uses _SOLAR_FEATURE_WEIGHTS.  Does NOT apply mandatory filters on
    lifecycle_state or decided_by — solar angle data is useful regardless of
    whether shading was active or which evaluator decided.

    None weight redistribution: features with a None value in either situation
    have their weight dropped; remaining weights are re-normalized to 1.0.
    All features None in either situation → 0.0.
    """
    total_weight = 0.0
    weighted_sum = 0.0

    for field, weight in _SOLAR_FEATURE_WEIGHTS:
        val_a: float | None = getattr(a, field)
        val_b: float | None = getattr(b, field)
        if val_a is None or val_b is None:
            continue
        total_weight += weight
        weighted_sum += abs(val_a - val_b) * weight

    if total_weight == 0.0:
        return 0.0

    return max(0.0, min(1.0, 1.0 - weighted_sum / total_weight))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _filter_valid(situations: list[SituationRecord]) -> list[SituationRecord]:
    """Keep only situations with resolution_status == 'complete' and a non-None delta."""
    return [
        s for s in situations
        if s.resolution_status == _RESOLUTION_COMPLETE
        and s.indoor_temp_delta_c is not None
    ]


def _count_distinct_days(situations: list[SituationRecord]) -> int:
    """Count distinct UTC calendar dates in *situations*."""
    days: set[date] = {s.decision_timestamp.date() for s in situations}
    return len(days)


def _median(values: list[float]) -> float:
    """Return the median of a non-empty sorted list."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 != 0:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def _compute_global_factor(valid: list[SituationRecord]) -> float | None:
    """Median indoor_temp_delta_c over all valid situations.

    Median is used instead of mean: it is robust against outlier heat-wave
    days and sensor anomalies that would skew the mean significantly.
    """
    deltas = [s.indoor_temp_delta_c for s in valid if s.indoor_temp_delta_c is not None]
    return _median(deltas) if deltas else None


def _compute_situational_factor(valid: list[SituationRecord]) -> float | None:
    """Exposure-weighted mean of indoor_temp_delta_c.

    Situations with higher effective_exposure_wm2 get proportionally more
    weight because the causal link between solar irradiance and indoor
    temperature change is strongest under high-exposure conditions.

    Situations where exposure is None or <= 0 are excluded: they cannot
    be meaningfully weighted and likely represent nighttime or cloudy
    measurements with no solar signal.
    """
    total_exposure = 0.0
    weighted_sum = 0.0

    for s in valid:
        if s.indoor_temp_delta_c is None:
            continue
        if s.effective_exposure_wm2 is None or s.effective_exposure_wm2 <= 0.0:
            continue
        total_exposure += s.effective_exposure_wm2
        weighted_sum += s.indoor_temp_delta_c * s.effective_exposure_wm2

    if total_exposure == 0.0:
        return None

    return weighted_sum / total_exposure


def _compute_combined_factor(
    global_factor: float | None,
    situational_factor: float | None,
) -> float | None:
    """Weighted blend: 0.40 × global + 0.60 × situational.

    Fallback rules:
      situational_factor is None → combined = global_factor
      global_factor is None      → combined = None  (no basis for an estimate)
    """
    if global_factor is None:
        return None
    if situational_factor is None:
        return global_factor
    return _GLOBAL_WEIGHT * global_factor + _SITUATIONAL_WEIGHT * situational_factor


def _none_result(
    sample_count: int,
    distinct_days: int,
    confidence_gate_passed: bool,
) -> SolarImpactResult:
    """Return a learning-inactive result when any gate did not pass."""
    return SolarImpactResult(
        global_solar_factor=None,
        situational_solar_factor=None,
        combined_solar_factor=None,
        sample_count=sample_count,
        distinct_days=distinct_days,
        confidence_gate_passed=confidence_gate_passed,
        learning_active=False,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_solar_impact(inp: SolarImpactInput) -> SolarImpactResult:
    """Compute a SolarImpactResult from a SolarImpactInput.

    Gate order:
      1. Confidence gate   (inp.confidence_result not None and level >= MEDIUM)
      2. Sample gate       (sample_count >= 10)
      3. Distinct-day gate (distinct_days >= 5)

    All gates must pass for learning_active = True and factors to be computed.

    Pure function — no state, no randomness, no I/O.
    """
    valid = _filter_valid(inp.situations)
    sample_count  = len(valid)
    distinct_days = _count_distinct_days(valid)

    # ------------------------------------------------------------------
    # Gate 1 — Confidence
    # ------------------------------------------------------------------
    confidence_gate_passed = (
        inp.confidence_result is not None
        and inp.confidence_result.level in _GATE_OPEN_LEVELS
    )
    if not confidence_gate_passed:
        return _none_result(sample_count, distinct_days, confidence_gate_passed=False)

    # ------------------------------------------------------------------
    # Gate 2 — Sample count
    # ------------------------------------------------------------------
    if sample_count < _MIN_SAMPLE_COUNT:
        return _none_result(sample_count, distinct_days, confidence_gate_passed=True)

    # ------------------------------------------------------------------
    # Gate 3 — Distinct days
    # ------------------------------------------------------------------
    if distinct_days < _MIN_DISTINCT_DAYS:
        return _none_result(sample_count, distinct_days, confidence_gate_passed=True)

    # ------------------------------------------------------------------
    # Compute factors
    # ------------------------------------------------------------------
    global_factor      = _compute_global_factor(valid)
    situational_factor = _compute_situational_factor(valid)
    combined_factor    = _compute_combined_factor(global_factor, situational_factor)

    return SolarImpactResult(
        global_solar_factor=global_factor,
        situational_solar_factor=situational_factor,
        combined_solar_factor=combined_factor,
        sample_count=sample_count,
        distinct_days=distinct_days,
        confidence_gate_passed=True,
        learning_active=True,
    )
