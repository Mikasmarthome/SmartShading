"""Deterministic Snapshot ID factory functions — Phase 9F12k-1.

Provides stable, collision-resistant string IDs for ForecastSnapshot and
RealitySnapshot objects.  IDs are computed purely from the data fields so
that re-collecting the same forecast or the same reality moment produces
the same ID — enabling O(1) deduplication in ForecastLearningStore.

Forecast Snapshot ID
--------------------
Format:  {source_id}__{variable}__{YYYYMMDDTHHMM}
Example: "weather.met_no_hourly__cloud_coverage__20250601T1400"

Components:
  source_id  — entity_id of the configured weather entity (e.g.
                "weather.met_no_hourly"); no sanitisation is applied,
                the caller must pass a stable identifier
  variable   — ForecastVariable.value string ("cloud_coverage",
                "temperature", "solar_irradiance")
  timestamp  — forecast_target_utc truncated to minute precision,
                formatted as YYYYMMDDTHHMM (UTC)

Reality Snapshot ID
-------------------
Format:  reality__{YYYYMMDDTHHMM}
Example: "reality__20250601T1400"

The observed timestamp is normalised to whole-minute precision (seconds
and microseconds discarded) before formatting so that two calls fired
within the same minute — e.g. at 14:00:01 and 14:00:59 — produce the
same ID and are treated as duplicates by the store.

UTC enforcement
---------------
Both functions require UTC-aware datetime arguments.  A naive datetime
(tzinfo is None) is rejected with ValueError because:
  - Two naive datetimes from different local timezones would produce
    identical IDs even though they represent different UTC moments.
  - SmartShading stores all timestamps in UTC; naive datetimes indicate
    a programming error at the call site and should fail fast rather
    than silently corrupt the store.

No HA dependencies.  No I/O.  No side effects.
"""
from __future__ import annotations

from datetime import datetime

from .forecast_learning import ForecastVariable

_SEPARATOR = "__"


def make_forecast_snapshot_id(
    source_id: str,
    variable: ForecastVariable,
    forecast_target_utc: datetime,
) -> str:
    """Return a deterministic ID for a ForecastSnapshot.

    The ID uniquely identifies the combination of weather source, forecast
    variable, and target time.  Identical inputs always yield the same ID;
    any difference in source, variable, or target minute yields a different ID.

    Parameters
    ----------
    source_id:
        entity_id of the weather entity used as forecast source
        (e.g. "weather.met_no_hourly").
    variable:
        The ForecastVariable enum member for the predicted quantity.
    forecast_target_utc:
        The UTC datetime for which the forecast value applies.
        Must be timezone-aware.  Seconds and sub-seconds are included
        as-is in the timestamp (minute precision is the caller's
        responsibility for target times; reality IDs normalise to minute).

    Raises
    ------
    ValueError
        If forecast_target_utc is a naive datetime (tzinfo is None).
    """
    if forecast_target_utc.tzinfo is None:
        raise ValueError(
            "forecast_target_utc must be a UTC-aware datetime; "
            f"received naive datetime: {forecast_target_utc!r}"
        )
    ts = forecast_target_utc.strftime("%Y%m%dT%H%M")
    return f"{source_id}{_SEPARATOR}{variable.value}{_SEPARATOR}{ts}"


def make_reality_snapshot_id(observed_at_utc: datetime) -> str:
    """Return a deterministic ID for a RealitySnapshot.

    The ID is based solely on the observation timestamp normalised to whole-
    minute precision.  Two calls with timestamps that differ only in seconds
    or microseconds produce the same ID, ensuring that the 30-minute Reality
    Collector does not create duplicate snapshots for the same collection
    window.

    Parameters
    ----------
    observed_at_utc:
        The UTC datetime at which sensor values were read.
        Must be timezone-aware.  Seconds and microseconds are discarded
        before formatting.

    Raises
    ------
    ValueError
        If observed_at_utc is a naive datetime (tzinfo is None).
    """
    if observed_at_utc.tzinfo is None:
        raise ValueError(
            "observed_at_utc must be a UTC-aware datetime; "
            f"received naive datetime: {observed_at_utc!r}"
        )
    normalised = observed_at_utc.replace(second=0, microsecond=0)
    ts = normalised.strftime("%Y%m%dT%H%M")
    return f"reality{_SEPARATOR}{ts}"
