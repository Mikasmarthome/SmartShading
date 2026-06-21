"""Forecast Snapshot data models — Phase 9F12f.

Foundation dataclasses for the Forecast Collection & Matching pipeline.
No computation logic, no persistence, no HA dependencies.

Data flow:
  ForecastSnapshot   (what was predicted)
  + RealitySnapshot  (what actually happened)
       ↓  (forecast_matcher — 9F12g)
  ForecastRecord     (matched pair, ready for Trust Engine)

Architecture invariants:
  - Both models are purely descriptive.  No threshold is touched here.
  - Missing sensor data (None fields) must never prevent normal shading.
  - All datetime fields are UTC and timezone-aware.  Naive datetimes are
    forbidden — callers must always pass tz-aware objects.
  - forecast_snapshot_id and reality_snapshot_id are deterministic hashes
    so that duplicate collection runs produce identical IDs and can be
    detected by the persistence layer without storing duplicates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .forecast_learning import ForecastVariable


# ---------------------------------------------------------------------------
# ForecastSnapshot — one predicted value for one variable at one target time
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastSnapshot:
    """A single forecast value for one variable at a specific target time.

    Created by the HA Forecast Collector (future step) once per hour per
    variable.  Each snapshot is keyed by a deterministic ID so that the
    persistence layer can detect and reject duplicates without querying
    the full record.

    Field semantics
    ---------------
    forecast_snapshot_id   deterministic identifier; hash of
                           (source_id, variable, forecast_created_utc,
                            forecast_target_utc).  Used as primary key
                           and for deduplication.
    source_id              weather-provider entity ID as seen in HA,
                           e.g. "weather.openweathermap_home".  Preserved
                           so that future per-source trust computation
                           remains possible without schema migration.
    variable               which weather variable this snapshot predicts
    forecast_created_utc   when the forecast was fetched; UTC, tz-aware
    forecast_target_utc    the point in time the forecast predicts;
                           UTC, tz-aware; always ≥ forecast_created_utc
    forecast_value         predicted value in the variable's natural unit:
                             cloud_coverage    percentage points [0, 100]
                             temperature       °C
                             solar_irradiance  W/m²
                           No unit field — the unit is implied by variable.
    """

    forecast_snapshot_id:  str
    source_id:             str
    variable:              ForecastVariable
    forecast_created_utc:  datetime
    forecast_target_utc:   datetime
    forecast_value:        float


# ---------------------------------------------------------------------------
# RealitySnapshot — observed sensor values at a single point in time
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RealitySnapshot:
    """Observed sensor values for all tracked weather variables at one moment.

    Created by the HA Reality Collector (future step) every 30 minutes on
    clock-aligned boundaries (:00 and :30).  A single RealitySnapshot covers
    all available variables so that the Forecast Matcher can extract the
    relevant value per ForecastSnapshot without a separate query.

    Missing sensor data is represented as None — not an error.  A snapshot
    where all three variable fields are None is valid: it records that all
    sensors were unavailable at that point in time.  The Forecast Matcher
    returns None for any ForecastSnapshot whose corresponding variable field
    is None.

    Field semantics
    ---------------
    reality_snapshot_id   deterministic identifier; hash of observed_at_utc.
                          One RealitySnapshot per 30-minute collection slot.
    observed_at_utc       when the sensor readings were recorded;
                          UTC, tz-aware; clock-aligned to :00 or :30
    cloud_coverage        measured cloud coverage in percentage [0, 100];
                          None when the sensor was unavailable
    temperature           measured air temperature in °C;
                          None when the sensor was unavailable
    solar_irradiance      measured solar irradiance in W/m²;
                          None when the sensor was unavailable
    """

    reality_snapshot_id:  str
    observed_at_utc:      datetime
    cloud_coverage:       float | None
    temperature:          float | None
    solar_irradiance:     float | None


# ---------------------------------------------------------------------------
# Utility accessor
# ---------------------------------------------------------------------------

def get_variable_value(
    snapshot: RealitySnapshot,
    variable: ForecastVariable,
) -> float | None:
    """Return the sensor value for *variable* from a RealitySnapshot.

    Returns None when the sensor was unavailable at collection time (the
    corresponding field is None) or when *variable* has no mapping (which
    cannot happen with the current ForecastVariable enum but is handled
    defensively).

    Used by the Forecast Matcher to extract the actual observed value for
    a given ForecastSnapshot without duplicating the variable→field mapping.
    """
    if variable is ForecastVariable.CLOUD_COVERAGE:
        return snapshot.cloud_coverage
    if variable is ForecastVariable.TEMPERATURE:
        return snapshot.temperature
    if variable is ForecastVariable.SOLAR_IRRADIANCE:
        return snapshot.solar_irradiance
    return None  # unreachable with the current enum; guards future extensions
