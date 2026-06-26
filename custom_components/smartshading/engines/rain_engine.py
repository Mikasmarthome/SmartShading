"""Rain sensor normalization — absent-evidence semantics for missing rain data.

Supported sensor types
----------------------
Binary sensor  — on / off (or device-class-specific states like wet/dry,
                 raining/not_raining).  Any truthy HA state maps to RAINING;
                 the canonical off / dry / not_raining maps to DRY.

Numeric sensor — rain rate in mm/h.  > 0.0 → RAINING; == 0.0 → DRY.
                 Cumulative daily or total rain values are NOT accepted as a
                 direct safety source: a non-zero cumulative value just means
                 it has rained at some point today, not that it is raining now.

Unavailable / unknown / None → RainStatus.UNKNOWN.

Design rules
------------
- No 0.0 substitution for absent data (cf. lifecycle_engine None semantics).
- Source, raw value, normalized status, and source quality are all included
  in RainSensorReading so the coordinator can surface them to diagnostics.
- No Home Assistant imports — normalization is pure Python so it stays
  unit-testable without a real HA instance (INV-18).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class RainStatus(Enum):
    """Normalized rain status — the only three states the rest of the system sees."""

    RAINING  = "raining"
    DRY      = "dry"
    UNKNOWN  = "unknown"


class RainSourceType(Enum):
    """Sensor type that produced this reading."""

    BINARY_SENSOR = "binary_sensor"
    NUMERIC_RATE  = "numeric_rate"   # mm/h — current rain rate
    NONE          = "none"           # no sensor configured


@dataclass(frozen=True)
class RainSensorReading:
    """Fully diagnosticable rain reading for one coordinator cycle.

    Fields intentionally mirror the WeatherSnapshot pattern so that the
    coordinator's diagnostic path can handle both uniformly.
    """

    status: RainStatus
    source_type: RainSourceType
    raw_value: Any                    # raw HA state string or float; None if absent
    sensor_entity_id: str | None      # entity_id of the sensor read; None if unconfigured
    read_at_utc: datetime | None      # UTC timestamp when value was read; None if absent
    is_stale: bool = False            # True when the reading is older than staleness_s threshold

    @property
    def is_available(self) -> bool:
        return self.status is not RainStatus.UNKNOWN

    @property
    def source_quality(self) -> str:
        """Human-readable quality indicator for diagnostics."""
        if self.source_type is RainSourceType.NONE:
            return "no_sensor"
        if self.status is RainStatus.UNKNOWN:
            return "unavailable"
        if self.is_stale:
            return "stale"
        return "ok"


# ---------------------------------------------------------------------------
# Binary-sensor state normalization
# ---------------------------------------------------------------------------

# States that explicitly mean "it is currently raining".
_BINARY_RAINING_STATES: frozenset[str] = frozenset({
    "on",
    "wet",
    "raining",
    "rain",
    "true",
    "1",
})

# States that explicitly mean "it is currently dry".
_BINARY_DRY_STATES: frozenset[str] = frozenset({
    "off",
    "dry",
    "not_raining",
    "no_rain",
    "false",
    "0",
})

# HA "sensor not ready" pseudo-states — map to UNKNOWN regardless of source type.
_HA_UNAVAILABLE_STATES: frozenset[str] = frozenset({
    "unavailable",
    "unknown",
    "none",
    "",
})


def normalize_binary_rain_state(hass_state: str | None) -> RainStatus:
    """Normalize a HA binary sensor state string to RainStatus.

    Matching is case-insensitive.  Any unrecognized state is UNKNOWN
    (fail-safe: absent evidence does not imply dry or raining).
    """
    if hass_state is None:
        return RainStatus.UNKNOWN
    normalized = hass_state.strip().lower()
    if normalized in _HA_UNAVAILABLE_STATES:
        return RainStatus.UNKNOWN
    if normalized in _BINARY_RAINING_STATES:
        return RainStatus.RAINING
    if normalized in _BINARY_DRY_STATES:
        return RainStatus.DRY
    return RainStatus.UNKNOWN


def normalize_numeric_rain_rate(rate_mmh: float | None) -> RainStatus:
    """Normalize a numeric rain rate (mm/h) to RainStatus.

    > 0.0 → RAINING (any positive rain rate means rain is currently falling).
    = 0.0 → DRY.
    None  → UNKNOWN.

    Cumulative rain values (e.g. daily total) must NOT be passed here — a
    non-zero cumulative value means it rained today, not that it is raining now.
    """
    if rate_mmh is None:
        return RainStatus.UNKNOWN
    return RainStatus.RAINING if rate_mmh > 0.0 else RainStatus.DRY


# ---------------------------------------------------------------------------
# Public factory — builds a fully diagnosticable RainSensorReading
# ---------------------------------------------------------------------------

def build_rain_sensor_reading(
    *,
    entity_id: str | None,
    hass_state: Any,
    source_type: RainSourceType,
    read_at_utc: datetime | None,
    staleness_s: float = 600.0,
    now_utc: datetime | None = None,
) -> RainSensorReading:
    """Build a RainSensorReading from raw HA sensor data.

    Parameters
    ----------
    entity_id:
        HA entity_id of the sensor; None when no rain sensor is configured.
    hass_state:
        Raw HA state value (str for binary sensors, float/str for numeric).
    source_type:
        Whether this is a binary sensor, a numeric rate sensor, or no sensor.
    read_at_utc:
        UTC timestamp when the raw value was last updated by HA.
    staleness_s:
        Maximum age (seconds) before a reading is considered stale.  A stale
        reading keeps its status but is flagged for diagnostics.
    now_utc:
        Current UTC time used for staleness calculation.  If None, staleness
        is not evaluated.
    """
    if source_type is RainSourceType.NONE or entity_id is None:
        return RainSensorReading(
            status=RainStatus.UNKNOWN,
            source_type=RainSourceType.NONE,
            raw_value=None,
            sensor_entity_id=None,
            read_at_utc=None,
        )

    # Normalize based on source type
    if source_type is RainSourceType.BINARY_SENSOR:
        status = normalize_binary_rain_state(
            str(hass_state) if hass_state is not None else None
        )
    elif source_type is RainSourceType.NUMERIC_RATE:
        try:
            rate = float(hass_state) if hass_state is not None else None
        except (TypeError, ValueError):
            rate = None
        status = normalize_numeric_rain_rate(rate)
    else:
        status = RainStatus.UNKNOWN

    # Staleness check
    is_stale = False
    if (
        status is not RainStatus.UNKNOWN
        and read_at_utc is not None
        and now_utc is not None
        and (now_utc - read_at_utc).total_seconds() > staleness_s
    ):
        is_stale = True

    return RainSensorReading(
        status=status,
        source_type=source_type,
        raw_value=hass_state,
        sensor_entity_id=entity_id,
        read_at_utc=read_at_utc,
        is_stale=is_stale,
    )
