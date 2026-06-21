"""Forecast Trust Engine — Phase 9F12d.

Answers: How reliable were the forecasts from this source?

Input:
  ForecastTrustInput — pool of ForecastRecords for a single variable

Output:
  ForecastTrustResult — per-bucket trust metrics and aggregated score

---

Processing pipeline (per HorizonBucket)
-----------------------------------------

  1. Filter records
       • Keep only records whose variable matches ForecastTrustInput.variable
       • Keep only records within the observation window
         (forecast_created_utc ≥ computed_at_utc − observation_window_days)
       • Records with is_data_error=True are excluded entirely from metrics
         (counted in data_error_count but not used in MAE/MBE)
       • Records with is_outlier=True are downweighted (not excluded)

  2. Assign HorizonBucket via classify_horizon()
       SHORT   forecast_horizon_minutes ∈ [0,   120)
       MEDIUM  forecast_horizon_minutes ∈ [120, 360)
       LONG    forecast_horizon_minutes ∈ [360, ∞)

  3. Per bucket: compute weighted metrics
       weight = _NORMAL_WEIGHT (1.0)  for normal records
       weight = _OUTLIER_WEIGHT (0.25) for is_outlier records
       n_effective = Σ weights

       MAE = Σ(weight × absolute_error) / n_effective
       MBE = Σ(weight × bias_error)     / n_effective
             MBE retains its sign (positive = over-prediction)

  4. Trust Score
       trust_score = clamp(1.0 − MAE / reference_scale, 0.0, 1.0)

       Reference scales (MAE at which trust = 0.0):
         cloud_coverage    50 percentage points
         temperature       10 °C
         solar_irradiance  500 W/m²

  5. Bias Direction
       threshold per variable:
         cloud_coverage    ±10 pp
         temperature       ±1.0 °C
         solar_irradiance  ±50 W/m²
       MBE >  +threshold  → OVER_PREDICTING
       MBE <  −threshold  → UNDER_PREDICTING
       else               → UNBIASED

  6. Trust Ready Gate
       sample_count  ≥ 30
       distinct_days ≥ 10
       n_effective   ≥ 15.0

       All three conditions must be true.
       When False: mae, mbe, trust_score, bias_direction are all None.

---

Aggregated Trust Score (ForecastTrustResult)
---------------------------------------------

  Weighted average of ready-bucket trust_scores by n_effective:
    Σ(trust_score_b × n_effective_b) / Σ(n_effective_b)   over ready buckets
  None when no bucket is trust_ready.

---

Estimate Stability (ForecastTrustResult)
-----------------------------------------

  Spread of trust_scores across ready buckets:
    estimate_stability = clamp(1.0 − (max_score − min_score), 0.0, 1.0)
  None when fewer than 2 buckets are trust_ready.

  0.0 → buckets diverge strongly (e.g. SHORT=0.9, LONG=0.0)
  1.0 → all ready buckets agree exactly

---

Summary (ForecastTrustSummary)
--------------------------------

  overall_trust_score = mean of aggregated_trust_score across ready variables
  overall_quality classification:
    [0.00, 0.40) → "poor"
    [0.40, 0.60) → "fair"
    [0.60, 0.80) → "good"
    [0.80, 1.00] → "excellent"

---

Tier safety
-----------

Purely diagnostic.  No threshold is modified.  No runtime state is touched.
No Tier 1–5 logic is affected.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..models.forecast_learning import (
    BiasDirection,
    ForecastRecord,
    ForecastTrustBucketResult,
    ForecastTrustInput,
    ForecastTrustResult,
    ForecastTrustSummary,
    ForecastVariable,
    HorizonBucket,
)


# ---------------------------------------------------------------------------
# Architecture constants
# ---------------------------------------------------------------------------

# Horizon bucket boundaries (minutes, lower inclusive, upper exclusive)
_SHORT_THRESHOLD_MINUTES:  int = 120   # [0,   120) → SHORT
_MEDIUM_THRESHOLD_MINUTES: int = 360   # [120, 360) → MEDIUM
                                        # [360, ∞)  → LONG

# Record weights
_NORMAL_WEIGHT:  float = 1.00
_OUTLIER_WEIGHT: float = 0.25

# Trust-ready gate thresholds
_MIN_SAMPLE_COUNT:  int   = 30    # raw record count (data errors excluded)
_MIN_DISTINCT_DAYS: int   = 10    # distinct UTC calendar days
_MIN_N_EFFECTIVE:   float = 15.0  # sum of weights after outlier downweighting

# Reference scales: MAE value at which trust_score reaches 0.0
_REFERENCE_SCALES: dict[str, float] = {
    ForecastVariable.CLOUD_COVERAGE.value:    50.0,
    ForecastVariable.TEMPERATURE.value:       10.0,
    ForecastVariable.SOLAR_IRRADIANCE.value: 500.0,
}

# Bias direction thresholds: MBE magnitude below which the forecast is "unbiased"
_BIAS_THRESHOLDS: dict[str, float] = {
    ForecastVariable.CLOUD_COVERAGE.value:    10.0,
    ForecastVariable.TEMPERATURE.value:        1.0,
    ForecastVariable.SOLAR_IRRADIANCE.value:  50.0,
}

# Overall quality thresholds (lower-bound inclusive → label)
_QUALITY_THRESHOLDS: list[tuple[float, str]] = [
    (0.40, "poor"),
    (0.60, "fair"),
    (0.80, "good"),
]
_QUALITY_TOP: str = "excellent"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def classify_horizon(horizon_minutes: int) -> HorizonBucket:
    """Map forecast_horizon_minutes to a HorizonBucket.

    Boundary convention: lower bound inclusive, upper bound exclusive.
      SHORT   [0,   120)
      MEDIUM  [120, 360)
      LONG    [360, ∞)
    """
    if horizon_minutes < _SHORT_THRESHOLD_MINUTES:
        return HorizonBucket.SHORT
    if horizon_minutes < _MEDIUM_THRESHOLD_MINUTES:
        return HorizonBucket.MEDIUM
    return HorizonBucket.LONG


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_bucket(
    records: list[ForecastRecord],
    variable: ForecastVariable,
    bucket: HorizonBucket,
    data_error_count: int,
) -> ForecastTrustBucketResult:
    """Compute ForecastTrustBucketResult for one (variable, bucket) pair.

    *records* must already be filtered: correct variable, within observation
    window, and is_data_error=False.  The data_error_count for this bucket
    is passed separately (already counted by the caller).
    """
    sample_count  = len(records)
    outlier_count = sum(1 for r in records if r.is_outlier)

    weights = [
        _OUTLIER_WEIGHT if r.is_outlier else _NORMAL_WEIGHT
        for r in records
    ]
    n_effective   = sum(weights)
    distinct_days = len({r.forecast_created_utc.date() for r in records})

    trust_ready = (
        sample_count  >= _MIN_SAMPLE_COUNT
        and distinct_days >= _MIN_DISTINCT_DAYS
        and n_effective   >= _MIN_N_EFFECTIVE
    )

    if not trust_ready:
        return ForecastTrustBucketResult(
            variable=variable,
            horizon_bucket=bucket,
            sample_count=sample_count,
            n_effective=n_effective,
            distinct_days=distinct_days,
            trust_ready=False,
            mae=None,
            mbe=None,
            trust_score=None,
            bias_direction=None,
            outlier_count=outlier_count,
            data_error_count=data_error_count,
        )

    # Weighted metrics
    mae = sum(w * r.absolute_error for w, r in zip(weights, records)) / n_effective
    mbe = sum(w * r.bias_error     for w, r in zip(weights, records)) / n_effective

    # Trust score
    ref_scale  = _REFERENCE_SCALES[variable.value]
    trust_score = max(0.0, min(1.0, 1.0 - mae / ref_scale))

    # Bias direction
    bias_thresh = _BIAS_THRESHOLDS[variable.value]
    if mbe > bias_thresh:
        bias_direction = BiasDirection.OVER_PREDICTING
    elif mbe < -bias_thresh:
        bias_direction = BiasDirection.UNDER_PREDICTING
    else:
        bias_direction = BiasDirection.UNBIASED

    return ForecastTrustBucketResult(
        variable=variable,
        horizon_bucket=bucket,
        sample_count=sample_count,
        n_effective=n_effective,
        distinct_days=distinct_days,
        trust_ready=True,
        mae=mae,
        mbe=mbe,
        trust_score=trust_score,
        bias_direction=bias_direction,
        outlier_count=outlier_count,
        data_error_count=data_error_count,
    )


def _classify_quality(score: float | None) -> str | None:
    """Map an overall_trust_score to a human-readable quality label."""
    if score is None:
        return None
    for threshold, label in _QUALITY_THRESHOLDS:
        if score < threshold:
            return label
    return _QUALITY_TOP


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_forecast_trust(
    inp: ForecastTrustInput,
    *,
    computed_at_utc: datetime | None = None,
) -> ForecastTrustResult:
    """Compute a ForecastTrustResult from a ForecastTrustInput.

    Pure function — deterministic when computed_at_utc is supplied.

    Parameters
    ----------
    inp:
        Input containing ForecastRecords and configuration.
    computed_at_utc:
        Reference time for the observation window.  Defaults to
        datetime.now(timezone.utc) when None — supply a fixed timestamp
        in tests to keep them deterministic.
    """
    _now = computed_at_utc if computed_at_utc is not None else datetime.now(timezone.utc)
    cutoff = _now - timedelta(days=inp.observation_window_days)

    # ------------------------------------------------------------------
    # Filter: variable match + observation window
    # ------------------------------------------------------------------
    in_window = [
        r for r in inp.records
        if r.variable is inp.variable
        and r.forecast_created_utc >= cutoff
    ]

    # Separate processable records from data errors
    processable: list[ForecastRecord] = [r for r in in_window if not r.is_data_error]
    errors:      list[ForecastRecord] = [r for r in in_window if r.is_data_error]

    # ------------------------------------------------------------------
    # Partition by HorizonBucket
    # ------------------------------------------------------------------
    processable_by_bucket: dict[HorizonBucket, list[ForecastRecord]] = defaultdict(list)
    for r in processable:
        processable_by_bucket[classify_horizon(r.forecast_horizon_minutes)].append(r)

    error_count_by_bucket: dict[HorizonBucket, int] = defaultdict(int)
    for r in errors:
        error_count_by_bucket[classify_horizon(r.forecast_horizon_minutes)] += 1

    # ------------------------------------------------------------------
    # Compute a result for every bucket (always all three)
    # ------------------------------------------------------------------
    bucket_results: tuple[ForecastTrustBucketResult, ...] = tuple(
        _compute_bucket(
            records=processable_by_bucket[bucket],
            variable=inp.variable,
            bucket=bucket,
            data_error_count=error_count_by_bucket[bucket],
        )
        for bucket in HorizonBucket
    )

    # ------------------------------------------------------------------
    # Aggregated trust score (weighted by n_effective over ready buckets)
    # ------------------------------------------------------------------
    ready = [br for br in bucket_results if br.trust_ready and br.trust_score is not None]
    total_n = sum(br.n_effective for br in ready)
    aggregated_trust_score: float | None = (
        sum(br.trust_score * br.n_effective for br in ready) / total_n
        if total_n > 0.0 else None
    )
    any_bucket_ready = bool(ready)

    # ------------------------------------------------------------------
    # Estimate stability (spread of trust_scores across ready buckets)
    # ------------------------------------------------------------------
    ready_scores = [br.trust_score for br in ready]  # trust_score is not None in 'ready'
    if len(ready_scores) < 2:
        estimate_stability: float | None = None
    else:
        spread = max(ready_scores) - min(ready_scores)
        estimate_stability = max(0.0, min(1.0, 1.0 - spread))

    return ForecastTrustResult(
        variable=inp.variable,
        bucket_results=bucket_results,
        aggregated_trust_score=aggregated_trust_score,
        any_bucket_ready=any_bucket_ready,
        estimate_stability=estimate_stability,
        computed_at_utc=_now,
        observation_window_days=inp.observation_window_days,
    )


def compute_forecast_trust_summary(
    results: tuple[ForecastTrustResult, ...],
    *,
    computed_at_utc: datetime | None = None,
) -> ForecastTrustSummary:
    """Aggregate multiple ForecastTrustResults into a ForecastTrustSummary.

    overall_trust_score is the simple mean of aggregated_trust_score values
    across variables that have at least one trust_ready bucket.

    Pure function — deterministic when computed_at_utc is supplied.
    """
    _now = computed_at_utc if computed_at_utc is not None else datetime.now(timezone.utc)

    ready_results  = [r for r in results if r.any_bucket_ready]
    variables_ready = tuple(r.variable for r in ready_results)

    ready_scores = [
        r.aggregated_trust_score
        for r in ready_results
        if r.aggregated_trust_score is not None
    ]
    overall_trust_score: float | None = (
        sum(ready_scores) / len(ready_scores) if ready_scores else None
    )
    overall_quality = _classify_quality(overall_trust_score)

    return ForecastTrustSummary(
        results=results,
        overall_trust_score=overall_trust_score,
        variables_ready=variables_ready,
        overall_quality=overall_quality,
        computed_at_utc=_now,
    )
