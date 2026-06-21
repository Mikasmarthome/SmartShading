"""Privacy-first Learning Export — Phase 9F13b / 9F13d (Schema Freeze).

Produces a JSON-serializable dict that aggregates Forecast Learning
results without exposing raw data, entity IDs, or individual records.

Design invariants
-----------------
  No HA dependencies — pure Python.  Importable and testable without a
  Home Assistant installation.

  Privacy-first:
    - No entity IDs / source_id
    - No snapshot IDs (forecast_snapshot_id, reality_snapshot_id)
    - No individual ForecastSnapshot / RealitySnapshot / ForecastRecord values
    - No individual measurement timestamps from records
    - Only aggregated metrics: MAE, MBE, trust_score, bias_direction,
      sample_count, bucket labels, overall quality

  UTC policy:
    generated_at_utc must be timezone-aware.  A naive datetime raises
    ValueError — this is a programmer error, not a runtime data issue,
    and is consistent with the SmartShading UTC invariant applied
    throughout the pipeline (build_reality_snapshot, make_forecast_snapshot_id,
    etc.).  All other failure modes (None store, trust engine error,
    unexpected data shape) produce a valid partial dict and never raise.

  Extensibility:
    build_learning_export accepts keyword-only arguments.  Future steps
    can add override_store, solar_impact_store, confidence_data, etc.
    without changing existing call sites.

    scope is a list[str] so that future modules (override_learning,
    solar_impact_learning, confidence, adaptation) can be appended without
    a breaking change to the envelope structure.

  Versioning:
    format_version   — global envelope version; bumps when top-level keys
                       change or scope semantics change.
    schema_version   — per-section version, nested inside each module section;
                       bumps when the internal structure of that section changes
                       independently of other sections.

---

PRIVACY CHECKLIST — enforced for all current and future export sections
-----------------------------------------------------------------------
The following data MUST NEVER appear in the standard Learning Export,
regardless of which module section adds it:

  NEVER EXPORT:
    entity_id               (sensor or weather entity identifiers)
    source_id               (weather provider entity ID from ForecastSnapshot)
    forecast_snapshot_id    (individual snapshot key)
    reality_snapshot_id     (individual snapshot key)
    forecast_value          (per-record raw prediction)
    actual_value            (per-record raw observation)
    absolute_error          (per-record raw error — use aggregated MAE instead)
    bias_error              (per-record raw error — use aggregated MBE instead)
    raw timestamps          (forecast_created_utc, forecast_target_utc,
                             observed_at_utc from individual records)
    raw ForecastSnapshot objects
    raw RealitySnapshot objects
    raw ForecastRecord objects

  ALLOWED:
    generated_at_utc        (export metadata timestamp only)
    aggregated counts       (forecast_snapshots_count, etc.)
    bucket labels           (short / medium / long)
    MAE, MBE                (aggregated per bucket)
    trust_score, bias_direction, overall_quality  (aggregated)
    sample_count, distinct_days, n_effective, outlier_count, data_error_count
    observation_window_days (configuration metadata)
    variables_ready         (list of variable names, no raw data)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .forecast_trust_engine import compute_forecast_trust, compute_forecast_trust_summary
from ..models.forecast_learning import (
    ForecastTrustInput,
    ForecastTrustSummary,
    ForecastVariable,
)

_LOGGER = logging.getLogger(__name__)

# Increment when the export JSON schema changes incompatibly.
FORMAT_VERSION: int = 1

# Must match the observation window used by ForecastTrustInput default.
_OBSERVATION_WINDOW_DAYS: int = 90


# ---------------------------------------------------------------------------
# Private trust computation (mirrors diagnostics.py; kept separate to avoid
# importing the HA-coupled diagnostics module here)
# ---------------------------------------------------------------------------

def _compute_trust_for_store(store: Any) -> ForecastTrustSummary | None:
    """Compute ForecastTrustSummary from *store*.

    Returns None when the store has no ForecastRecords.
    May raise — the caller must wrap this in a try/except.
    """
    records = list(store.forecast_records.values())
    if not records:
        return None
    now = datetime.now(timezone.utc)
    results = tuple(
        compute_forecast_trust(
            ForecastTrustInput(
                records=[r for r in records if r.variable is var],
                variable=var,
            ),
            computed_at_utc=now,
        )
        for var in ForecastVariable
    )
    return compute_forecast_trust_summary(results, computed_at_utc=now)


# ---------------------------------------------------------------------------
# Private serialization helpers (privacy-first — aggregated only)
# ---------------------------------------------------------------------------

def _serialize_bucket(br: Any) -> dict:
    # mbe sign convention: positive = forecast overestimation (forecast > actual)
    #                      negative = forecast underestimation (forecast < actual)
    return {
        "horizon_bucket":   br.horizon_bucket.value,
        "sample_count":     br.sample_count,
        "n_effective":      round(br.n_effective, 2),
        "distinct_days":    br.distinct_days,
        "trust_ready":      br.trust_ready,
        "mae":              round(br.mae, 4) if br.mae is not None else None,
        "mbe":              round(br.mbe, 4) if br.mbe is not None else None,
        "trust_score":      round(br.trust_score, 4) if br.trust_score is not None else None,
        "bias_direction":   br.bias_direction.value if br.bias_direction is not None else None,
        "outlier_count":    br.outlier_count,
        "data_error_count": br.data_error_count,
    }


def _serialize_variable_result(r: Any) -> dict:
    return {
        "variable":               r.variable.value,
        "aggregated_trust_score": r.aggregated_trust_score,
        "any_bucket_ready":       r.any_bucket_ready,
        "buckets":                [_serialize_bucket(br) for br in r.bucket_results],
    }


# ---------------------------------------------------------------------------
# Private forecast section builder
# ---------------------------------------------------------------------------

_FORECAST_UNAVAILABLE: dict = {
    "schema_version":          1,
    "available":               False,
    "observation_window_days": _OBSERVATION_WINDOW_DAYS,
    "counts": {
        "forecast_snapshots":  0,
        "reality_snapshots":   0,
        "forecast_records":    0,
        "unmatched_snapshots": 0,
    },
    "trust_computed":      False,
    "overall_trust_score": None,
    "overall_quality":     None,
    "variables_ready":     [],
    "variables":           [],
}


def _build_forecast_section(forecast_store: Any | None) -> dict:
    """Build the forecast_learning section.  Never raises."""
    if forecast_store is None:
        return dict(_FORECAST_UNAVAILABLE)

    try:
        fc_count  = len(forecast_store.forecast_snapshots)
        re_count  = len(forecast_store.reality_snapshots)
        rec_count = len(forecast_store.forecast_records)
        unmatched = len(forecast_store.get_unmatched_snapshots())
    except Exception:
        _LOGGER.warning(
            "SmartShading: learning_export: could not read store counts — "
            "returning unavailable section"
        )
        return dict(_FORECAST_UNAVAILABLE)

    trust_computed      = False
    overall_trust_score = None
    overall_quality     = None
    variables_ready: list[str] = []
    variables_out:   list[dict] = []

    if rec_count > 0:
        try:
            summary = _compute_trust_for_store(forecast_store)
            if summary is not None:
                trust_computed      = True
                overall_trust_score = summary.overall_trust_score
                overall_quality     = summary.overall_quality
                variables_ready     = [v.value for v in summary.variables_ready]
                variables_out       = [_serialize_variable_result(r) for r in summary.results]
        except Exception:
            _LOGGER.warning(
                "SmartShading: Forecast Trust computation failed during export "
                "— trust data omitted"
            )

    return {
        "schema_version":          1,
        "available":               True,
        "observation_window_days": _OBSERVATION_WINDOW_DAYS,
        "counts": {
            "forecast_snapshots":  fc_count,
            "reality_snapshots":   re_count,
            "forecast_records":    rec_count,
            "unmatched_snapshots": unmatched,
        },
        "trust_computed":      trust_computed,
        "overall_trust_score": overall_trust_score,
        "overall_quality":     overall_quality,
        "variables_ready":     variables_ready,
        "variables":           variables_out,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_learning_export(
    *,
    forecast_store: Any | None,
    generated_at_utc: datetime,
) -> dict:
    """Build a privacy-first Forecast Learning export dict.

    Parameters
    ----------
    forecast_store:
        ForecastLearningStore or None.  None produces an "available: false"
        section; no exception is raised.
    generated_at_utc:
        UTC-aware datetime stamped into the export metadata.

        Raises ValueError for naive (tzinfo=None) datetimes — consistent
        with the SmartShading UTC policy.  This is the only condition under
        which build_learning_export raises; all other errors are caught
        internally and produce a valid partial dict.

    Returns
    -------
    dict
        JSON-serializable export.  Top-level keys:
          format_version    int        — envelope version (currently 1)
          generated_at_utc  str        — ISO 8601 UTC timestamp
          scope             list[str]  — included module sections (e.g. ["forecast_learning"])
          forecast_learning dict       — aggregated Forecast Learning data;
                                         includes schema_version: 1 as first key
    """
    if generated_at_utc.tzinfo is None:
        raise ValueError(
            "build_learning_export: generated_at_utc must be timezone-aware; "
            "naive datetimes are not accepted (SmartShading UTC policy)"
        )

    try:
        forecast_section = _build_forecast_section(forecast_store)
    except Exception:
        _LOGGER.warning(
            "SmartShading: _build_forecast_section raised unexpectedly — using fallback"
        )
        forecast_section = dict(_FORECAST_UNAVAILABLE)

    return {
        "format_version":    FORMAT_VERSION,
        "generated_at_utc":  generated_at_utc.isoformat(),
        "scope":             ["forecast_learning"],
        "forecast_learning": forecast_section,
    }
