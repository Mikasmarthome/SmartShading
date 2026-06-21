"""Forecast Matcher Engine — Phase 9F12g.

Answers: Does this ForecastSnapshot pair with a RealitySnapshot, and if so
what is the resulting ForecastRecord?

Input:
  ForecastSnapshot  — one predicted value for one variable at one target time
  RealitySnapshot   — observed sensor values at a nearby point in time

Output:
  ForecastRecord    — matched pair ready for the Forecast Trust Engine
  None              — no qualifying RealitySnapshot available

---

Matching algorithm
------------------

A ForecastSnapshot matches a RealitySnapshot when:

  1. Temporal proximity:
       |reality.observed_at_utc − forecast.forecast_target_utc|
         ≤ DEFAULT_TOLERANCE_MINUTES (default 15 minutes, inclusive)

  2. Variable available:
       get_variable_value(reality, forecast.variable) is not None

  3. Both conditions must hold simultaneously.

When multiple RealitySnapshots are candidates (temporal proximity ≤
tolerance), find_best_match() selects the one with the minimum absolute
time offset from forecast_target_utc.  Ties are broken by position in
the input list (first candidate wins).

---

Data-error detection
---------------------

Physically impossible values in either forecast_value or actual_value
result in a ForecastRecord with is_data_error=True.  The record is still
created so that data_error_count is tracked by the Trust Engine.

Valid ranges (bounds inclusive):
  cloud_coverage    [0.0, 100.0]   percentage points
  temperature       [−80.0, 60.0]  °C
  solar_irradiance  [0.0, 1500.0]  W/m²

A value that falls exactly on a bound is valid.

---

Error computation
-----------------

  absolute_error = |forecast_value − actual_value|   always ≥ 0
  bias_error     = forecast_value − actual_value

  bias_error > 0  → forecast was too high (over-prediction)
  bias_error < 0  → forecast was too low  (under-prediction)
  bias_error = 0  → perfect

  These are pre-computed here so the Trust Engine treats them as
  ground truth and never re-derives them.

---

Horizon computation
--------------------

  forecast_horizon_minutes =
      max(0, int((forecast_target_utc − forecast_created_utc) / 60 s))

  Clamped at 0 to guard against inconsistent snapshot timestamps.
  The HorizonBucket classifier in forecast_trust_engine.py uses this
  value directly.

---

Duplicate handling
-------------------

NOT the engine's responsibility.  match_forecast() is a pure function:
identical inputs always produce identical outputs.  Preventing duplicate
ForecastRecords is the persistence layer's job — it checks whether a
record with the same forecast_snapshot_id already exists before writing.

---

Out of scope
------------
  - HA sensor reading
  - Scheduling (hourly / 30-min collection)
  - Persistence (to_dict / from_dict / HA storage)
  - Coordinator integration
  - Outlier detection (is_outlier is always False here; the Trust Engine
    sets it during IQR analysis across the full record pool)

---

Tier safety
-----------
Purely diagnostic pipeline step.  No threshold is modified.  No runtime
state is read or written.  No Tier 1–5 logic is affected.
"""
from __future__ import annotations

from datetime import timedelta

from ..models.forecast_learning import ForecastRecord, ForecastVariable
from ..models.forecast_snapshots import (
    ForecastSnapshot,
    RealitySnapshot,
    get_variable_value,
)


# ---------------------------------------------------------------------------
# Architecture constants
# ---------------------------------------------------------------------------

# Default temporal tolerance for matching a RealitySnapshot to a target time.
# Inclusive: a reality snapshot with an offset of exactly this many minutes
# is accepted.  Compatible with 30-minute reality collection intervals —
# the maximum possible offset is 15 minutes when both sides are clock-aligned.
DEFAULT_TOLERANCE_MINUTES: int = 15

# Valid physical ranges per variable (inclusive bounds).
# Values outside these ranges are physically impossible and are flagged as
# data errors on the resulting ForecastRecord.
_VALID_RANGES: dict[str, tuple[float, float]] = {
    ForecastVariable.CLOUD_COVERAGE.value:   (0.0,   100.0),
    ForecastVariable.TEMPERATURE.value:      (-80.0,  60.0),
    ForecastVariable.SOLAR_IRRADIANCE.value: (0.0,  1500.0),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _value_in_range(variable: ForecastVariable, value: float) -> bool:
    """Return True when *value* falls within the valid physical range for *variable*."""
    lo, hi = _VALID_RANGES[variable.value]
    return lo <= value <= hi


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_best_match(
    forecast: ForecastSnapshot,
    realities: list[RealitySnapshot],
    *,
    tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES,
) -> RealitySnapshot | None:
    """Return the RealitySnapshot closest to forecast.forecast_target_utc.

    Searches *realities* for candidates whose observed_at_utc is within
    *tolerance_minutes* of the forecast's target time (inclusive).  Among
    qualifying candidates, the one with the smallest absolute time offset
    is returned.  Ties (equal offset) are broken by position — the first
    candidate in *realities* wins.

    Returns None when no qualifying candidate exists.

    Parameters
    ----------
    forecast:
        The ForecastSnapshot whose target time is used as the reference.
    realities:
        Pool of RealitySnapshots to search.  Order matters for tie-breaking.
    tolerance_minutes:
        Maximum allowed offset in minutes (inclusive).
    """
    tolerance_seconds = tolerance_minutes * 60
    best: RealitySnapshot | None = None
    best_offset: float = float("inf")

    for reality in realities:
        offset = abs(
            (reality.observed_at_utc - forecast.forecast_target_utc).total_seconds()
        )
        if offset <= tolerance_seconds and offset < best_offset:
            best = reality
            best_offset = offset

    return best


def match_forecast(
    forecast: ForecastSnapshot,
    reality: RealitySnapshot,
    *,
    tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES,
) -> ForecastRecord | None:
    """Match a ForecastSnapshot to a single RealitySnapshot.

    Returns a ForecastRecord when:
      - reality.observed_at_utc is within tolerance_minutes of
        forecast.forecast_target_utc (inclusive), AND
      - the reality snapshot contains a non-None value for forecast.variable.

    Returns None otherwise.  None means "no match possible" — the caller
    should try again when more RealitySnapshots become available.

    The returned ForecastRecord has:
      is_outlier   = False  (outlier detection is the Trust Engine's job)
      is_data_error = True  when either forecast_value or actual_value falls
                            outside the physically valid range for the variable

    Parameters
    ----------
    forecast:
        ForecastSnapshot to match.
    reality:
        Candidate RealitySnapshot.
    tolerance_minutes:
        Maximum allowed temporal offset in minutes (inclusive).
        Defaults to DEFAULT_TOLERANCE_MINUTES (15).
    """
    # --- Temporal tolerance check ---
    tolerance_seconds = tolerance_minutes * 60
    offset_seconds = abs(
        (reality.observed_at_utc - forecast.forecast_target_utc).total_seconds()
    )
    if offset_seconds > tolerance_seconds:
        return None

    # --- Variable availability check ---
    actual_value = get_variable_value(reality, forecast.variable)
    if actual_value is None:
        return None

    # --- Derived fields ---
    horizon_minutes = max(
        0,
        int(
            (forecast.forecast_target_utc - forecast.forecast_created_utc)
            .total_seconds()
            / 60
        ),
    )
    absolute_error = abs(forecast.forecast_value - actual_value)
    bias_error = forecast.forecast_value - actual_value

    # --- Data-error detection ---
    is_data_error = not _value_in_range(
        forecast.variable, forecast.forecast_value
    ) or not _value_in_range(forecast.variable, actual_value)

    return ForecastRecord(
        forecast_snapshot_id=forecast.forecast_snapshot_id,
        variable=forecast.variable,
        forecast_created_utc=forecast.forecast_created_utc,
        forecast_target_utc=forecast.forecast_target_utc,
        forecast_horizon_minutes=horizon_minutes,
        forecast_value=forecast.forecast_value,
        actual_value=actual_value,
        absolute_error=absolute_error,
        bias_error=bias_error,
        is_outlier=False,
        is_data_error=is_data_error,
    )


def match_all(
    forecasts: list[ForecastSnapshot],
    realities: list[RealitySnapshot],
    *,
    tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES,
) -> list[ForecastRecord]:
    """Match every ForecastSnapshot to its best available RealitySnapshot.

    For each ForecastSnapshot in *forecasts*, find_best_match() selects the
    closest qualifying RealitySnapshot, then match_forecast() produces the
    ForecastRecord.  ForecastSnapshots with no qualifying reality snapshot are
    silently skipped — they remain unmatched and should be retried when new
    RealitySnapshots become available.

    The order of results mirrors the order of *forecasts* (excluding unmatched
    entries).  Deduplication is not performed here — that is the persistence
    layer's responsibility.

    Parameters
    ----------
    forecasts:
        ForecastSnapshots to match.  May include multiple variables and
        multiple horizon distances.
    realities:
        Pool of RealitySnapshots to search for each forecast.  A single
        reality snapshot may match multiple forecasts (different variables
        or different horizons targeting the same time).
    tolerance_minutes:
        Passed through to find_best_match() and match_forecast().
    """
    results: list[ForecastRecord] = []

    for forecast in forecasts:
        best_reality = find_best_match(
            forecast, realities, tolerance_minutes=tolerance_minutes
        )
        if best_reality is None:
            continue
        record = match_forecast(
            forecast, best_reality, tolerance_minutes=tolerance_minutes
        )
        if record is not None:
            results.append(record)

    return results
