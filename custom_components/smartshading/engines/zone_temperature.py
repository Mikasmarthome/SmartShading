"""Zone temperature aggregation — LE 2.0 / Phase P4.

Robust aggregation of a zone's configured indoor temperature sensors
(``indoor_temperature_sensor_ids`` — already zone-specific because one config
entry == one zone).  Used ONLY by the thermal-response learning path: it never
replaces the existing Heat-Evaluator temperature source.

Pure: takes already-parsed sensor values (float | None) and returns a typed
reading with a slim source classification.  No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.thermal_response import (
    AGG_MEDIAN,
    AGG_NONE,
    AGG_SINGLE,
    SOURCE_CONFIGURED,
    SOURCE_CONFIGURED_PARTIAL,
    SOURCE_NONE,
)

# Plausibility window for an indoor temperature reading (°C).  Values outside
# are sensor garbage and are rejected from aggregation.  Chosen wide enough to
# never reject a real indoor reading.
PLAUSIBLE_MIN_C: float = -30.0
PLAUSIBLE_MAX_C: float = 60.0

# A change larger than this between two consecutive zone reads (~5 min) is
# physically implausible for a room → flagged as a jump confounder (the value
# is still used, but the observation reliability is reduced upstream).
MAX_JUMP_C_PER_READ: float = 10.0


@dataclass(frozen=True)
class ZoneTemperatureReading:
    """Result of aggregating a zone's configured temperature sensors."""

    value: float | None              # robust aggregate (median) of valid in-range values
    configured_count: int
    valid_count: int
    source_kind: str
    aggregation_method: str
    outliers_rejected: int = 0
    jump_detected: bool = False

    @property
    def available(self) -> bool:
        return self.value is not None


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def aggregate_zone_temperature(
    values: list[float | None],
    *,
    previous_value: float | None = None,
) -> ZoneTemperatureReading:
    """Aggregate parsed sensor values into a robust zone temperature.

    Parameters
    ----------
    values:
        One entry per configured sensor; None for unavailable/unknown/non-numeric.
    previous_value:
        The previous accepted aggregate (for jump detection); None to skip.
    """
    configured = len(values)
    in_range: list[float] = []
    outliers = 0
    for v in values:
        if v is None:
            continue
        if PLAUSIBLE_MIN_C <= v <= PLAUSIBLE_MAX_C:
            in_range.append(v)
        else:
            outliers += 1

    valid = len(in_range)
    if valid == 0:
        return ZoneTemperatureReading(
            value=None, configured_count=configured, valid_count=0,
            source_kind=SOURCE_NONE, aggregation_method=AGG_NONE,
            outliers_rejected=outliers, jump_detected=False,
        )

    agg = _median(in_range)
    method = AGG_SINGLE if valid == 1 else AGG_MEDIAN

    # Source classification (slim): all configured valid → configured;
    # some missing → partial.
    if configured > 0 and valid == configured:
        source = SOURCE_CONFIGURED
    else:
        source = SOURCE_CONFIGURED_PARTIAL

    jump = (
        previous_value is not None
        and abs(agg - previous_value) > MAX_JUMP_C_PER_READ
    )

    return ZoneTemperatureReading(
        value=agg, configured_count=configured, valid_count=valid,
        source_kind=source, aggregation_method=method,
        outliers_rejected=outliers, jump_detected=jump,
    )


def source_reliability_factor(reading: ZoneTemperatureReading) -> float:
    """Reliability multiplier [0,1] derived from the source classification.

    configured (all valid) → 1.0; partial → valid/configured (reduced);
    none → 0.0.  A detected jump halves the factor (suspect reading).
    """
    if reading.source_kind == SOURCE_NONE or reading.value is None:
        return 0.0
    if reading.source_kind == SOURCE_CONFIGURED:
        base = 1.0
    else:  # partial
        base = (reading.valid_count / reading.configured_count) if reading.configured_count else 0.0
    if reading.jump_detected:
        base *= 0.5
    return max(0.0, min(1.0, base))
