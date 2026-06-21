"""Forecast Learning data models — Phase 9F12c.

Foundation dataclasses and enums for the Forecast Trust Learning system.
No computation logic, no persistence, no HA dependencies.

Data flow:
  ForecastSnapshot + RealitySnapshot
        ↓  (matcher — later step)
  ForecastRecord
        ↓  (ForecastTrustInput)
  ForecastTrustBucketResult   (per variable × HorizonBucket)
        ↓
  ForecastTrustResult         (per variable, all buckets)
        ↓
  ForecastTrustSummary        (global, all variables)

Architecture invariants (consistent with the rest of the Learning Stack):
  - These models are purely descriptive.  No threshold is modified here.
  - Missing or absent forecast data must never prevent normal shading decisions.
  - Forecast Trust is a global system property (per Forecast-source), not
    per-window.  Window-specific solar sensitivity is handled by Solar Impact
    Learning (9F8), which is orthogonal.
  - All datetime fields are UTC.  No local time, no naive datetime objects.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ForecastVariable(Enum):
    """Which weather variable a ForecastRecord describes.

    Values match the field names used throughout the pipeline so that
    dict-based lookups (e.g. _REFERENCE_SCALES[var.value]) work without
    an additional mapping layer.
    """

    CLOUD_COVERAGE   = "cloud_coverage"
    TEMPERATURE      = "temperature"
    SOLAR_IRRADIANCE = "solar_irradiance"


class HorizonBucket(Enum):
    """Forecast horizon classification.

    Boundary convention (enforced by classify_horizon in the engine):
      SHORT   forecast_horizon_minutes in [0,   120)
      MEDIUM  forecast_horizon_minutes in [120, 360)
      LONG    forecast_horizon_minutes in [360, ∞)

    The raw forecast_horizon_minutes is always persisted; the bucket is
    derived at query time so that boundary changes do not invalidate
    stored records.
    """

    SHORT  = "short"
    MEDIUM = "medium"
    LONG   = "long"


class BiasDirection(Enum):
    """Systematic direction of the forecast error.

    Derived from MBE (Mean Bias Error = mean(forecast − actual)):
      OVER_PREDICTING   MBE > +BIAS_THRESHOLD  (provider says more than reality)
      UNDER_PREDICTING  MBE < −BIAS_THRESHOLD  (provider says less than reality)
      UNBIASED          |MBE| ≤ BIAS_THRESHOLD
    """

    OVER_PREDICTING  = "over_predicting"
    UNDER_PREDICTING = "under_predicting"
    UNBIASED         = "unbiased"


class ForecastResolution(Enum):
    """Temporal resolution of the original forecast."""

    HOURLY = "hourly"
    DAILY  = "daily"


# ---------------------------------------------------------------------------
# ForecastRecord — matched forecast-actual pair
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastRecord:
    """Matched forecast-actual pair for a single variable at a single target time.

    Built by the Forecast Matcher from a ForecastSnapshot + RealitySnapshot.
    absolute_error and bias_error are pre-computed at creation time so that
    the Trust Engine never re-derives them and can trust them as ground truth.

    Outlier / data-error flags are set by the Trust Engine during trust
    computation, not by the matcher.

    Field semantics
    ---------------
    absolute_error    |forecast_value − actual_value|     always ≥ 0
    bias_error        forecast_value − actual_value       sign encodes direction:
                        positive  = forecast was too high (over-prediction)
                        negative  = forecast was too low  (under-prediction)
    is_outlier        True when the record was downweighted during trust
                      computation because its error exceeded the IQR threshold.
                      Never set by the matcher.
    is_data_error     True when the record carries a physically impossible value
                      (e.g. cloud_coverage > 100) and was fully excluded from
                      trust computation.  Never set by the matcher.
    """

    # Identity
    forecast_snapshot_id:      str

    # What was measured
    variable:                  ForecastVariable

    # Timing
    forecast_created_utc:      datetime   # when the snapshot was taken
    forecast_target_utc:       datetime   # what time the forecast was for
    forecast_horizon_minutes:  int        # (target − created) in whole minutes; ≥ 0

    # Values
    forecast_value:            float
    actual_value:              float
    absolute_error:            float      # |forecast_value − actual_value|
    bias_error:                float      # forecast_value − actual_value

    # Quality flags (set by Trust Engine, default False at creation)
    is_outlier:                bool = False
    is_data_error:             bool = False


# ---------------------------------------------------------------------------
# ForecastTrustInput — engine entry point
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastTrustInput:
    """Input to compute_forecast_trust().

    records                 — ForecastRecords for a single variable.
                              May span multiple HorizonBuckets; the engine
                              partitions them internally.
    variable                — which variable these records represent
    observation_window_days — rolling-window size; records older than this
                              are ignored during trust computation
    prior_trust_scores      — aggregated trust_score values from previous
                              computation cycles, newest first.  Used to
                              derive estimate_stability.  None when no
                              prior history is available.
    """

    records:                  list[ForecastRecord]
    variable:                 ForecastVariable
    observation_window_days:  int = 90
    prior_trust_scores:       tuple[float, ...] | None = None


# ---------------------------------------------------------------------------
# ForecastTrustBucketResult — per variable × bucket
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastTrustBucketResult:
    """Trust computation result for one (variable, HorizonBucket) pair.

    When trust_ready is False, mae / mbe / trust_score / bias_direction
    are all None.  sample_count, n_effective, and distinct_days are always
    reported so consumers can track data accumulation progress.

    Field semantics
    ---------------
    sample_count      raw count of ForecastRecords in this bucket (before weighting)
    n_effective       sum of record weights after outlier downweighting;
                        non-outlier records contribute 1.0, outliers 0.1
    distinct_days     number of distinct UTC calendar days represented
    trust_ready       True when n_effective ≥ MIN_SAMPLE and distinct_days ≥ MIN_DAYS
    mae               weighted mean absolute error (in variable's natural unit)
    mbe               unweighted mean bias error; sign = over/under-prediction
    trust_score       max(0.0, 1.0 − mae / reference_scale) → [0.0, 1.0]
    bias_direction    derived from mbe relative to a per-variable threshold
    outlier_count     records downweighted to weight 0.1 (IQR-based)
    data_error_count  records excluded entirely (weight 0.0; impossible values)
    """

    variable:           ForecastVariable
    horizon_bucket:     HorizonBucket

    # Data volume
    sample_count:       int
    n_effective:        float
    distinct_days:      int

    # Gate
    trust_ready:        bool

    # Metrics (None when trust_ready is False)
    mae:                float | None
    mbe:                float | None
    trust_score:        float | None
    bias_direction:     BiasDirection | None

    # Quality accounting
    outlier_count:      int
    data_error_count:   int


# ---------------------------------------------------------------------------
# ForecastTrustResult — per variable, all buckets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastTrustResult:
    """Full trust result for a single variable across all horizon buckets.

    bucket_results is a tuple (immutable) containing one entry per bucket
    for which records were available.  Buckets with zero records are absent.

    aggregated_trust_score is a weighted average over the ready bucket scores:
      SHORT weight 0.50, MEDIUM weight 0.35, LONG weight 0.15
      (weights re-normalised when some buckets are not trust_ready)
      None when no bucket is trust_ready.

    estimate_stability measures how stable the aggregated_trust_score has
    been across recent computation cycles.  1.0 = perfectly stable;
    0.0 = strongly fluctuating.  None when fewer than 3 prior cycles
    are available in ForecastTrustInput.prior_trust_scores.
    """

    variable:                ForecastVariable
    bucket_results:          tuple[ForecastTrustBucketResult, ...]

    # Aggregated convenience value
    aggregated_trust_score:  float | None
    any_bucket_ready:        bool

    # Stability of the estimate over time
    estimate_stability:      float | None

    # Metadata
    computed_at_utc:         datetime
    observation_window_days: int


# ---------------------------------------------------------------------------
# ForecastTrustSummary — global, all variables
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastTrustSummary:
    """Global forecast trust summary across all variables.

    overall_trust_score is a weighted average across all ready
    (variable, bucket) pairs.  Bucket weights are the same as in
    ForecastTrustResult; variable weights are equal (1/N ready variables).
    None when no (variable, bucket) pair is trust_ready.

    overall_quality is a human-readable classification:
      "poor"      overall_trust_score < 0.40
      "fair"      0.40 ≤ overall_trust_score < 0.60
      "good"      0.60 ≤ overall_trust_score < 0.80
      "excellent" overall_trust_score ≥ 0.80
      None        no ready variable
    """

    results:              tuple[ForecastTrustResult, ...]
    overall_trust_score:  float | None
    variables_ready:      tuple[ForecastVariable, ...]
    overall_quality:      str | None    # "poor" | "fair" | "good" | "excellent" | None
    computed_at_utc:      datetime


# ---------------------------------------------------------------------------
# Utility accessors (pure functions — no computation)
# ---------------------------------------------------------------------------

def get_bucket_result(
    result: ForecastTrustResult,
    bucket: HorizonBucket,
) -> ForecastTrustBucketResult | None:
    """Return the ForecastTrustBucketResult for *bucket*, or None if absent."""
    for br in result.bucket_results:
        if br.horizon_bucket is bucket:
            return br
    return None


def get_variable_result(
    summary: ForecastTrustSummary,
    variable: ForecastVariable,
) -> ForecastTrustResult | None:
    """Return the ForecastTrustResult for *variable*, or None if absent."""
    for r in summary.results:
        if r.variable is variable:
            return r
    return None
