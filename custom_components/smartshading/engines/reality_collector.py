"""Reality Collector Foundation — Phase 9F12k-2.

Provides build_reality_snapshot(), a pure-Python factory function that
creates a RealitySnapshot from current sensor readings.

This module is the pure-Python foundation of the Reality Collector.
The HA-layer (reading hass.states, scheduling the 30-minute interval)
is out of scope for this step and will be added later.

build_reality_snapshot()
------------------------
Accepts the three weather variables as plain Python floats (or None when
a sensor is unavailable), validates the timestamp, and returns a
RealitySnapshot — or None when all three values are None.

All-None strategy
-----------------
A RealitySnapshot in which every variable is None can never produce a
ForecastRecord: the Forecast Matcher returns None for every variable
when get_variable_value() returns None.  Storing such a snapshot would
consume space in ForecastLearningStore without ever contributing to
trust computation.  Therefore build_reality_snapshot() returns None
when cloud_coverage, temperature, and solar_irradiance are all None.

Callers should log a warning and skip persistence in this case.

Physical validation
-------------------
No physical range checks are performed here.  Out-of-range values
(e.g. cloud_coverage=150.0) are passed through unchanged.  Data-error
detection is the Forecast Matcher's responsibility (is_data_error flag
on ForecastRecord).  The Reality Collector's job is to faithfully
capture what the sensors reported.

UTC enforcement
---------------
observed_at_utc must be a UTC-aware datetime.  A naive datetime (tzinfo
is None) is rejected with ValueError because naive datetimes cannot be
reliably compared to other UTC timestamps stored in the system.

ID normalisation
----------------
The snapshot_id is produced by make_reality_snapshot_id(), which
truncates seconds and microseconds to whole-minute precision.  The
observed_at_utc field itself is stored as received (no truncation) so
that the exact measurement time is preserved.

Tier safety
-----------
No threshold is read or written.  No runtime state is modified.
Pure data construction — no I/O, no scheduling, no HA dependencies.
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..models.forecast_snapshots import RealitySnapshot
from ..models.snapshot_ids import make_reality_snapshot_id

_log = logging.getLogger(__name__)


def build_reality_snapshot(
    *,
    observed_at_utc: datetime,
    cloud_coverage: float | None,
    temperature: float | None,
    solar_irradiance: float | None,
) -> RealitySnapshot | None:
    """Construct a RealitySnapshot from current sensor readings.

    Returns None when all three variables are None — such a snapshot
    cannot contribute to any ForecastRecord and should not be stored.

    Parameters
    ----------
    observed_at_utc:
        UTC datetime at which the sensor values were read.
        Must be timezone-aware.  Seconds and microseconds are preserved
        in this field; only the snapshot ID is normalised to minutes.
    cloud_coverage:
        Cloud coverage percentage, or None if sensor unavailable.
    temperature:
        Outdoor temperature in °C, or None if sensor unavailable.
    solar_irradiance:
        Solar irradiance in W/m², or None if sensor unavailable.

    Returns
    -------
    RealitySnapshot
        When at least one variable is not None.
    None
        When all three variables are None (no usable data).

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

    if cloud_coverage is None and temperature is None and solar_irradiance is None:
        return None

    return RealitySnapshot(
        reality_snapshot_id=make_reality_snapshot_id(observed_at_utc),
        observed_at_utc=observed_at_utc,
        cloud_coverage=cloud_coverage,
        temperature=temperature,
        solar_irradiance=solar_irradiance,
    )
