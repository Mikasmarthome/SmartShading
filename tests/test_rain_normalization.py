"""Unit tests for rain sensor normalization (rain_engine.py).

Tests binary sensor and numeric mm/h sensor normalization, UNKNOWN
absent-evidence semantics, and staleness detection.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from custom_components.smartshading.engines.rain_engine import (
    RainStatus,
    RainSourceType,
    RainSensorReading,
    normalize_binary_rain_state,
    normalize_numeric_rain_rate,
    build_rain_sensor_reading,
)


# ---------------------------------------------------------------------------
# normalize_binary_rain_state
# ---------------------------------------------------------------------------

class TestNormalizeBinary:
    def test_on_returns_raining(self):
        assert normalize_binary_rain_state("on") is RainStatus.RAINING

    def test_off_returns_dry(self):
        assert normalize_binary_rain_state("off") is RainStatus.DRY

    def test_unavailable_returns_unknown(self):
        assert normalize_binary_rain_state("unavailable") is RainStatus.UNKNOWN

    def test_unknown_state_returns_unknown(self):
        assert normalize_binary_rain_state("unknown") is RainStatus.UNKNOWN

    def test_none_returns_unknown(self):
        assert normalize_binary_rain_state(None) is RainStatus.UNKNOWN

    def test_arbitrary_string_returns_unknown(self):
        assert normalize_binary_rain_state("whatever") is RainStatus.UNKNOWN

    def test_case_insensitive_on(self):
        assert normalize_binary_rain_state("ON") is RainStatus.RAINING

    def test_case_insensitive_off(self):
        assert normalize_binary_rain_state("OFF") is RainStatus.DRY


# ---------------------------------------------------------------------------
# normalize_numeric_rain_rate
# ---------------------------------------------------------------------------

class TestNormalizeNumeric:
    def test_zero_is_dry(self):
        assert normalize_numeric_rain_rate(0.0) is RainStatus.DRY

    def test_positive_is_raining(self):
        assert normalize_numeric_rain_rate(1.0) is RainStatus.RAINING

    def test_small_positive_is_raining(self):
        assert normalize_numeric_rain_rate(0.1) is RainStatus.RAINING

    def test_negative_is_dry(self):
        # Negative is not > 0.0 so normalizes to DRY (not UNKNOWN).
        assert normalize_numeric_rain_rate(-1.0) is RainStatus.DRY

    def test_none_is_unknown(self):
        assert normalize_numeric_rain_rate(None) is RainStatus.UNKNOWN


# ---------------------------------------------------------------------------
# build_rain_sensor_reading
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_FRESH = _NOW - timedelta(seconds=60)


class TestBuildRainSensorReading:
    def test_binary_on_returns_raining(self):
        r = build_rain_sensor_reading(
            entity_id="binary_sensor.rain",
            hass_state="on",
            source_type=RainSourceType.BINARY_SENSOR,
            read_at_utc=_FRESH,
            now_utc=_NOW,
        )
        assert r.status is RainStatus.RAINING
        assert r.source_type is RainSourceType.BINARY_SENSOR
        assert r.is_stale is False

    def test_binary_off_returns_dry(self):
        r = build_rain_sensor_reading(
            entity_id="binary_sensor.rain",
            hass_state="off",
            source_type=RainSourceType.BINARY_SENSOR,
            read_at_utc=_FRESH,
            now_utc=_NOW,
        )
        assert r.status is RainStatus.DRY

    def test_numeric_positive_returns_raining(self):
        r = build_rain_sensor_reading(
            entity_id="sensor.rain_rate",
            hass_state="3.5",
            source_type=RainSourceType.NUMERIC_RATE,
            read_at_utc=_FRESH,
            now_utc=_NOW,
        )
        assert r.status is RainStatus.RAINING

    def test_numeric_zero_returns_dry(self):
        r = build_rain_sensor_reading(
            entity_id="sensor.rain_rate",
            hass_state="0",
            source_type=RainSourceType.NUMERIC_RATE,
            read_at_utc=_FRESH,
            now_utc=_NOW,
        )
        assert r.status is RainStatus.DRY

    def test_stale_reading_is_flagged_but_keeps_status(self):
        # Stale readings keep their normalized status — is_stale is a diagnostic
        # flag for surface, NOT an override to UNKNOWN.
        stale_time = _NOW - timedelta(seconds=700)
        r = build_rain_sensor_reading(
            entity_id="binary_sensor.rain",
            hass_state="off",
            source_type=RainSourceType.BINARY_SENSOR,
            read_at_utc=stale_time,
            now_utc=_NOW,
            staleness_s=600.0,
        )
        assert r.status is RainStatus.DRY
        assert r.is_stale is True

    def test_none_entity_id_returns_unknown(self):
        r = build_rain_sensor_reading(
            entity_id=None,
            hass_state=None,
            source_type=RainSourceType.NONE,
            read_at_utc=None,
            now_utc=_NOW,
        )
        assert r.status is RainStatus.UNKNOWN
        assert r.source_type is RainSourceType.NONE

    def test_none_raw_state_returns_unknown(self):
        r = build_rain_sensor_reading(
            entity_id="binary_sensor.rain",
            hass_state=None,
            source_type=RainSourceType.BINARY_SENSOR,
            read_at_utc=_FRESH,
            now_utc=_NOW,
        )
        assert r.status is RainStatus.UNKNOWN

    def test_none_read_at_skips_staleness_check(self):
        # None read_at_utc means staleness cannot be evaluated — reading is not
        # flagged stale; status comes from the raw_state as usual.
        r = build_rain_sensor_reading(
            entity_id="binary_sensor.rain",
            hass_state="on",
            source_type=RainSourceType.BINARY_SENSOR,
            read_at_utc=None,
            now_utc=_NOW,
        )
        assert r.status is RainStatus.RAINING
        assert r.is_stale is False

    def test_non_numeric_state_for_numeric_sensor_returns_unknown(self):
        r = build_rain_sensor_reading(
            entity_id="sensor.rain_rate",
            hass_state="unavailable",
            source_type=RainSourceType.NUMERIC_RATE,
            read_at_utc=_FRESH,
            now_utc=_NOW,
        )
        assert r.status is RainStatus.UNKNOWN

    def test_raw_value_preserved_in_reading(self):
        r = build_rain_sensor_reading(
            entity_id="binary_sensor.rain",
            hass_state="on",
            source_type=RainSourceType.BINARY_SENSOR,
            read_at_utc=_FRESH,
            now_utc=_NOW,
        )
        assert r.raw_value == "on"
        assert r.sensor_entity_id == "binary_sensor.rain"

    def test_fresh_reading_not_stale(self):
        r = build_rain_sensor_reading(
            entity_id="binary_sensor.rain",
            hass_state="on",
            source_type=RainSourceType.BINARY_SENSOR,
            read_at_utc=_FRESH,
            now_utc=_NOW,
            staleness_s=600.0,
        )
        assert r.is_stale is False
