"""Authoritative solar-radiation source selection — LE 2.0 (pure, HA-free).

A configured, valid, fresh and plausible measured solar-radiation sensor (W/m²)
is THE authoritative solar source.  A weather/cloud-derived estimate is used ONLY
as a clearly diagnosed fallback when no usable measured value exists (sensor not
configured, unavailable/non-numeric, implausible, or stale).

Authority rules enforced here:
  - A valid measured value is never replaced or overridden by a weather/forecast
    estimate.
  - Cloud cover is folded into the estimate exactly ONCE (fallback path) and is
    NEVER applied on top of an authoritative measured value — the measurement
    already reflects cloud physically (no double damping).
  - The fallback is reported with a stable reason code and a lower source quality
    than a measured value, so downstream learning/diagnostics can treat it as
    less reliable.

This module classifies only.  It never reads Home Assistant state, never mutates
runtime, and never raises.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Selected source.
SOURCE_MEASURED = "measured_sensor"
SOURCE_ESTIMATE = "weather_estimate"
SOURCE_NONE = "unavailable"

# Source quality (measured > estimated > none).
QUALITY_HIGH = "measured_high"
QUALITY_LOW = "estimated_low"
QUALITY_NONE = "none"

# Fallback reason codes (stable, privacy-safe).
FB_NOT_CONFIGURED = "sensor_not_configured"
FB_UNAVAILABLE = "sensor_unavailable"     # None / unknown / unavailable / non-numeric
FB_IMPLAUSIBLE = "sensor_implausible"     # outside [0, MAX_PLAUSIBLE_SOLAR_WM2]
FB_STALE = "sensor_stale"                 # last update older than SOLAR_SENSOR_MAX_AGE_S
FB_UNIT_MISMATCH = "sensor_unit_mismatch"  # F20: unit_of_measurement is not W/m² (e.g. lux)

# A global-horizontal solar-radiation reading above this is physically
# implausible (clear-sky peak is ~1000 W/m²; allow head-room for edge devices).
MAX_PLAUSIBLE_SOLAR_WM2 = 1500.0
# A measured solar reading older than this is treated as stale → fallback.
SOLAR_SENSOR_MAX_AGE_S = 1800  # 30 minutes

# F20: illuminance units (lux family) a user could plausibly miswire into the
# solar-radiation sensor slot — visually a "brightness" sensor like W/m², but
# a different physical quantity entirely.  No safe automatic conversion
# exists, so a reading in one of these units is rejected (FB_UNIT_MISMATCH)
# rather than trusted as W/m².  A missing/unrecognized unit is NOT rejected
# here — it is trusted exactly like before this check existed.
_NON_SOLAR_IRRADIANCE_UNITS = frozenset({"lx", "lux", "klx", "klux"})


@dataclass(frozen=True)
class SolarSourceResult:
    """Result of authoritative solar-source selection (all JSON-safe scalars).

    effective_radiation_wm2 is the value to hand to the ExposureEngine.
    measured_wm2/estimated_wm2 are echoed for diagnostics so a user/support can
    see the measured value AND the estimate that was (or was not) used.
    """

    source: str
    quality: str
    effective_radiation_wm2: float
    sensor_configured: bool
    measured_wm2: float | None
    measured_valid: bool
    estimated_wm2: float | None
    cloud_cover_pct: float | None
    cloud_applied: bool
    cloud_not_applied_reason: str | None
    fallback_reason: str | None


def _finite(value: object) -> bool:
    """True only for a real finite int/float (rejects bool, NaN, ±Infinity)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _num(value: object) -> float | None:
    return float(value) if _finite(value) else None


def classify_solar_source(
    *,
    sensor_configured: bool,
    measured_wm2: object,
    measured_age_s: float | None,
    estimated_wm2: object,
    cloud_cover_pct: object,
    measured_unit: object = None,
    max_plausible_wm2: float = MAX_PLAUSIBLE_SOLAR_WM2,
    max_age_s: float = SOLAR_SENSOR_MAX_AGE_S,
) -> SolarSourceResult:
    """Select the authoritative solar source from a measured reading and an
    estimate.

    measured_wm2 is the raw parsed sensor value (or None when unavailable).
    measured_age_s is the seconds since the sensor last updated (None = unknown,
    treated as not-stale; the caller supplies it from HA state.last_updated).
    estimated_wm2 is the weather/cloud-derived fallback estimate (already
    cloud-folded by the WeatherEngine).
    measured_unit (F20) is the sensor's raw unit_of_measurement string, or
    None when unavailable/not supplied — a value reported in an illuminance
    unit (lux family) is rejected as FB_UNIT_MISMATCH rather than trusted as
    W/m²; a missing/unrecognized unit is trusted exactly as before this
    parameter existed.
    """
    # Determine measured validity and, if invalid, the precise reason.
    reject: str | None = None
    if not sensor_configured:
        reject = FB_NOT_CONFIGURED
    elif not _finite(measured_wm2):
        reject = FB_UNAVAILABLE
    elif isinstance(measured_unit, str) and measured_unit.strip().lower() in _NON_SOLAR_IRRADIANCE_UNITS:
        reject = FB_UNIT_MISMATCH
    elif measured_wm2 < 0.0 or measured_wm2 > max_plausible_wm2:  # type: ignore[operator]
        reject = FB_IMPLAUSIBLE
    elif measured_age_s is not None and _finite(measured_age_s) and measured_age_s > max_age_s:
        reject = FB_STALE

    measured_valid = reject is None
    est = _num(estimated_wm2)
    cloud = _num(cloud_cover_pct)
    raw_measured = _num(measured_wm2)

    if measured_valid:
        # Authoritative measured value — cloud is NOT applied again.
        return SolarSourceResult(
            source=SOURCE_MEASURED,
            quality=QUALITY_HIGH,
            effective_radiation_wm2=float(measured_wm2),  # type: ignore[arg-type]
            sensor_configured=True,
            measured_wm2=raw_measured,
            measured_valid=True,
            estimated_wm2=est,                 # echoed for comparison (the ignored value)
            cloud_cover_pct=cloud,
            cloud_applied=False,
            cloud_not_applied_reason="measured_authoritative",
            fallback_reason=None,
        )

    if est is not None:
        # Diagnosed fallback — estimate carries cloud exactly once.
        return SolarSourceResult(
            source=SOURCE_ESTIMATE,
            quality=QUALITY_LOW,
            effective_radiation_wm2=est,
            sensor_configured=sensor_configured,
            measured_wm2=raw_measured,         # echoed even when rejected (diagnostics)
            measured_valid=False,
            estimated_wm2=est,
            cloud_cover_pct=cloud,
            cloud_applied=cloud is not None,
            cloud_not_applied_reason=None,
            fallback_reason=reject,
        )

    # No usable solar value at all.
    return SolarSourceResult(
        source=SOURCE_NONE,
        quality=QUALITY_NONE,
        effective_radiation_wm2=0.0,
        sensor_configured=sensor_configured,
        measured_wm2=raw_measured,
        measured_valid=False,
        estimated_wm2=None,
        cloud_cover_pct=cloud,
        cloud_applied=False,
        cloud_not_applied_reason=None,
        fallback_reason=reject,
    )
