"""SmartShading Diagnostics — Phase 9F13a.

Provides async_get_config_entry_diagnostics (HA standard hook) and the
pure-Python helper build_forecast_diagnostics that assembles the Forecast
Learning section of the diagnostics payload.

Design invariants
-----------------
  build_forecast_diagnostics NEVER raises.  Any failure produces a partial
  fallback dict so that the HA diagnostics download always succeeds.

  The output is privacy-first:
    - no raw ForecastSnapshot / RealitySnapshot / ForecastRecord data
    - no entity IDs
    - no individual snapshot IDs
    - no individual measurement timestamps
  Only aggregated trust metrics and counts are exposed.

  Trust computation is on-demand (called here, not cached or persisted).
  The ForecastTrustSummary is recomputed each time diagnostics are requested.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from .coordinator import SmartShadingRuntimeData

from .engines.forecast_trust_engine import (
    compute_forecast_trust,
    compute_forecast_trust_summary,
)
from .models.forecast_learning import (
    ForecastTrustInput,
    ForecastTrustSummary,
    ForecastVariable,
)
from .models.forecast_store import ForecastLearningStore

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Private serialization helpers (privacy-first — no raw record data)
# ---------------------------------------------------------------------------

def _serialize_bucket(br: Any) -> dict:
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
        "variable":              r.variable.value,
        "aggregated_trust_score": r.aggregated_trust_score,
        "any_bucket_ready":      r.any_bucket_ready,
        "buckets":               [_serialize_bucket(br) for br in r.bucket_results],
    }


def _serialize_trust_summary(summary: ForecastTrustSummary) -> dict:
    return {
        "overall_trust_score": summary.overall_trust_score,
        "overall_quality":     summary.overall_quality,
        "variables_ready":     [v.value for v in summary.variables_ready],
        "results":             [_serialize_variable_result(r) for r in summary.results],
    }


# ---------------------------------------------------------------------------
# Trust computation (private — patchable in tests)
# ---------------------------------------------------------------------------

def _compute_trust_for_store(store: ForecastLearningStore) -> ForecastTrustSummary | None:
    """Compute ForecastTrustSummary from *store*.

    Returns None when the store has no ForecastRecords.
    May raise — the caller wraps this in a try/except.
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
# Public API
# ---------------------------------------------------------------------------

_FALLBACK: dict = {
    "active":                    False,
    "store_available":           False,
    "forecast_snapshots_count":  0,
    "reality_snapshots_count":   0,
    "forecast_records_count":    0,
    "unmatched_snapshots_count": 0,
    "trust_computed":            False,
    "trust_summary":             None,
}


def build_forecast_diagnostics(runtime_data: Any) -> dict:
    """Assemble the Forecast Learning section of the SmartShading diagnostics.

    Parameters
    ----------
    runtime_data:
        Duck-typed ConfigEntry.runtime_data.  Expected attributes:
          .forecast_store   ForecastLearningStore | None
          .forecast_cancel  tuple[Callable, Callable] | None
        Missing attributes silently produce the safe fallback values.

    Returns
    -------
    dict
        Always a valid dict.  Never raises.  Keys:
          active                    bool
          store_available           bool
          forecast_snapshots_count  int
          reality_snapshots_count   int
          forecast_records_count    int
          unmatched_snapshots_count int
          trust_computed            bool
          trust_summary             dict | None
    """
    try:
        store  = getattr(runtime_data, "forecast_store",  None)
        cancel = getattr(runtime_data, "forecast_cancel", None)

        active          = cancel is not None
        store_available = store is not None

        if not store_available:
            return {**_FALLBACK, "active": active}

        forecast_snapshots_count  = len(store.forecast_snapshots)
        reality_snapshots_count   = len(store.reality_snapshots)
        forecast_records_count    = len(store.forecast_records)
        unmatched_snapshots_count = len(store.get_unmatched_snapshots())

        trust_computed    = False
        trust_summary_out = None

        if forecast_records_count > 0:
            try:
                summary = _compute_trust_for_store(store)
                if summary is not None:
                    trust_summary_out = _serialize_trust_summary(summary)
                    trust_computed = True
            except Exception:
                _LOGGER.warning(
                    "SmartShading: Forecast Trust computation failed during diagnostics "
                    "— trust_summary omitted"
                )

        return {
            "active":                    active,
            "store_available":           store_available,
            "forecast_snapshots_count":  forecast_snapshots_count,
            "reality_snapshots_count":   reality_snapshots_count,
            "forecast_records_count":    forecast_records_count,
            "unmatched_snapshots_count": unmatched_snapshots_count,
            "trust_computed":            trust_computed,
            "trust_summary":             trust_summary_out,
        }

    except Exception:
        _LOGGER.warning(
            "SmartShading: build_forecast_diagnostics raised unexpectedly "
            "— returning fallback"
        )
        return dict(_FALLBACK)


# ---------------------------------------------------------------------------
# HA entry point
# ---------------------------------------------------------------------------

def _build_consolidated(runtime_data: Any) -> dict:
    """P11 consolidated PUBLIC_SAFE diagnostics; never raises."""
    try:
        from .engines.diagnostics_builder import build_consolidated_diagnostics
        coordinator = getattr(runtime_data, "coordinator", None) or runtime_data
        if coordinator is None:
            return {"section_errors": {"no_coordinator": 1}}
        return build_consolidated_diagnostics(coordinator)
    except Exception:
        _LOGGER.warning("SmartShading: consolidated diagnostics failed — partial")
        return {"section_errors": {"builder": 1}}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict:
    """Return SmartShading diagnostics for a config entry (HA standard hook).

    P11: consolidated PUBLIC_SAFE contract (system/learning/execution/storage/
    validation/health) plus the existing forecast section.  Read-only; counts and
    status only; no raw ids, no exact historical timestamps."""
    runtime_data = getattr(entry, "runtime_data", None)
    return {
        **_build_consolidated(runtime_data),
        "forecast_learning": build_forecast_diagnostics(runtime_data),
    }
