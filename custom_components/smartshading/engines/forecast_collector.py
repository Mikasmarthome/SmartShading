"""Forecast Collector Foundation — Phase 9F12k-3.

Provides ForecastEntry (normalised input) and build_forecast_snapshots(),
a pure-Python factory function that converts a list of ForecastEntry objects
into a flat list of ForecastSnapshot objects — one per variable per entry.

ForecastEntry
-------------
Represents one forecast horizon point as received from a weather provider.
All three weather variables are optional; None means the provider did not
supply that variable for this horizon.

build_forecast_snapshots()
--------------------------
Iterates over every entry × every variable, skips None values, and creates
one ForecastSnapshot per (entry, variable) pair that has a real value.

Caller responsibilities
-----------------------
  horizon filtering   The HA-layer that calls this function is responsible
                      for discarding entries whose target_utc is more than
                      24 hours ahead.  This function accepts any horizon
                      distance without filtering so it stays testable in
                      isolation.

  physical validation ForecastRecord.is_data_error is set by the Forecast
                      Matcher, not here.  Out-of-range values are passed
                      through unchanged.

UTC enforcement
---------------
Both forecast_created_utc and each entry's target_utc must be UTC-aware.
A naive datetime at either position is rejected with ValueError.  Validation
fires on forecast_created_utc before the entry loop, and on each entry's
target_utc as it is processed, so the first offending argument is flagged.

Variable mapping
----------------
  ForecastEntry.cloud_coverage    → ForecastVariable.CLOUD_COVERAGE
  ForecastEntry.temperature       → ForecastVariable.TEMPERATURE
  ForecastEntry.solar_irradiance  → ForecastVariable.SOLAR_IRRADIANCE

The mapping is defined once as a module-level tuple and consumed by the
inner loop — no duplicated field-to-enum logic anywhere.

None handling
-------------
  Entry field None   → that variable's snapshot is silently skipped
  All-None entry     → no snapshot for that entry (0 outputs)
  All-None entries   → empty list returned

Tier safety
-----------
No threshold is read or written.  No runtime state is modified.
Pure data construction — no I/O, no scheduling, no HA dependencies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..models.forecast_learning import ForecastVariable
from ..models.forecast_snapshots import ForecastSnapshot
from ..models.snapshot_ids import make_forecast_snapshot_id

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Variable mapping — single source of truth for field → enum
# ---------------------------------------------------------------------------

# Each tuple: (ForecastVariable member, attribute name on ForecastEntry)
_VARIABLE_ACCESSORS: tuple[tuple[ForecastVariable, str], ...] = (
    (ForecastVariable.CLOUD_COVERAGE,   "cloud_coverage"),
    (ForecastVariable.TEMPERATURE,      "temperature"),
    (ForecastVariable.SOLAR_IRRADIANCE, "solar_irradiance"),
)


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastEntry:
    """One forecast point from a weather provider.

    Represents a single target time with up to three optional weather
    variables.  Fields set to None indicate that the provider did not
    supply that variable for this horizon — they do not imply the value
    is zero.

    target_utc must be UTC-aware.  This is not validated at construction
    time; build_forecast_snapshots() validates it during processing.
    """

    target_utc:        datetime
    cloud_coverage:    float | None = None
    temperature:       float | None = None
    solar_irradiance:  float | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_forecast_snapshots(
    *,
    source_id: str,
    forecast_created_utc: datetime,
    entries: list[ForecastEntry],
) -> list[ForecastSnapshot]:
    """Convert a list of ForecastEntry objects into ForecastSnapshot objects.

    One ForecastSnapshot is produced per (entry, variable) pair where the
    entry's value for that variable is not None.  Entries with all-None
    values contribute zero snapshots.  An empty *entries* list or a list
    of all-None entries returns an empty list without raising.

    Parameters
    ----------
    source_id:
        entity_id of the weather entity used as forecast source
        (e.g. "weather.met_no_hourly").  Passed through unchanged to
        each ForecastSnapshot.
    forecast_created_utc:
        UTC datetime at which this forecast was retrieved from the provider.
        Must be timezone-aware.
    entries:
        Normalised forecast data for one or more horizon points.
        Each entry's target_utc must be timezone-aware.

    Returns
    -------
    list[ForecastSnapshot]
        Flat list ordered by (entry order, variable order).

    Raises
    ------
    ValueError
        If forecast_created_utc is naive, or if any entry's target_utc
        is naive.
    """
    if forecast_created_utc.tzinfo is None:
        raise ValueError(
            "forecast_created_utc must be a UTC-aware datetime; "
            f"received naive datetime: {forecast_created_utc!r}"
        )

    snapshots: list[ForecastSnapshot] = []

    for entry in entries:
        if entry.target_utc.tzinfo is None:
            raise ValueError(
                "ForecastEntry.target_utc must be a UTC-aware datetime; "
                f"received naive datetime: {entry.target_utc!r}"
            )

        for variable, attr in _VARIABLE_ACCESSORS:
            value: float | None = getattr(entry, attr)
            if value is None:
                continue

            snapshot_id = make_forecast_snapshot_id(source_id, variable, entry.target_utc)
            snapshots.append(
                ForecastSnapshot(
                    forecast_snapshot_id=snapshot_id,
                    source_id=source_id,
                    variable=variable,
                    forecast_created_utc=forecast_created_utc,
                    forecast_target_utc=entry.target_utc,
                    forecast_value=value,
                )
            )

    return snapshots
