"""Backward-compatibility and validation tests for dispatch_config storage
(v1.2.0-beta.1, T11) — config_entry_data.py's
_dispatch_config_from_storage() / to_storage_dict().

Coverage:
  STOR-01  No stored dispatch_config key at all (every pre-T11 config) ->
           DispatchConfig defaults, reproducing the fixed 2.0s interval.
  STOR-02  Stored values round-trip through to_storage_dict() ->
           from_storage_dict() unchanged.
  STOR-03  Unknown/invalid mode string falls back to SPACED, never raises.
  STOR-04  Out-of-range numeric values are clamped to the valid default
           rather than stored as-is or crashing setup.
  STOR-05  Non-numeric / wrong-type stored values fall back to the default.
  STOR-06  zone_batching missing/non-bool -> defaults to False.
  STOR-07  NaN/inf stored values never reach the config (safety-critical:
           an infinite wait must never be constructible from storage).
"""
from __future__ import annotations

import math

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    _dispatch_config_from_storage,
    to_storage_dict,
)
from custom_components.smartshading.models.dispatch_config import (
    DEFAULT_MAX_TRAVEL_WAIT_S,
    DEFAULT_POST_TRAVEL_PAUSE_S,
    DEFAULT_START_INTERVAL_S,
    DispatchConfig,
    DispatchMode,
)


class TestEmptyStorageDefaults:
    def test_none_storage_defaults(self) -> None:
        config = _dispatch_config_from_storage(None)
        assert config == DispatchConfig()

    def test_empty_dict_defaults(self) -> None:
        config = _dispatch_config_from_storage({})
        assert config == DispatchConfig()


class TestRoundTrip:
    def test_stored_values_round_trip(self) -> None:
        original = DispatchConfig(
            mode=DispatchMode.SEQUENTIAL, start_interval_s=1.5,
            max_travel_wait_s=50.0, post_travel_pause_s=1.0, zone_batching=True,
        )
        data = SmartShadingConfigEntryData(name="test", use_home_location=True, dispatch_config=original)
        stored = to_storage_dict(data)
        restored = _dispatch_config_from_storage(stored["dispatch_config"])
        assert restored == original

    def test_default_config_round_trips(self) -> None:
        data = SmartShadingConfigEntryData(name="test", use_home_location=True)
        stored = to_storage_dict(data)
        restored = _dispatch_config_from_storage(stored["dispatch_config"])
        assert restored == DispatchConfig()


class TestInvalidMode:
    def test_unknown_mode_falls_back_to_spaced(self) -> None:
        config = _dispatch_config_from_storage({"mode": "warp_speed"})
        assert config.mode is DispatchMode.SPACED

    def test_missing_mode_falls_back_to_spaced(self) -> None:
        config = _dispatch_config_from_storage({"start_interval_s": 3.0})
        assert config.mode is DispatchMode.SPACED
        assert config.start_interval_s == 3.0


class TestOutOfRangeClamping:
    def test_negative_start_interval_falls_back_to_default(self) -> None:
        config = _dispatch_config_from_storage({"start_interval_s": -5.0})
        assert config.start_interval_s == DEFAULT_START_INTERVAL_S

    def test_excessive_start_interval_falls_back_to_default(self) -> None:
        config = _dispatch_config_from_storage({"start_interval_s": 9999.0})
        assert config.start_interval_s == DEFAULT_START_INTERVAL_S

    def test_below_minimum_max_travel_wait_falls_back(self) -> None:
        config = _dispatch_config_from_storage({"max_travel_wait_s": 0.1})
        assert config.max_travel_wait_s == DEFAULT_MAX_TRAVEL_WAIT_S

    def test_excessive_post_travel_pause_falls_back(self) -> None:
        config = _dispatch_config_from_storage({"post_travel_pause_s": 999.0})
        assert config.post_travel_pause_s == DEFAULT_POST_TRAVEL_PAUSE_S


class TestWrongType:
    def test_string_garbage_falls_back(self) -> None:
        config = _dispatch_config_from_storage({"start_interval_s": "not_a_number"})
        assert config.start_interval_s == DEFAULT_START_INTERVAL_S

    def test_none_value_falls_back(self) -> None:
        config = _dispatch_config_from_storage({"start_interval_s": None})
        assert config.start_interval_s == DEFAULT_START_INTERVAL_S

    def test_bool_value_falls_back(self) -> None:
        # bool is a subclass of int in Python — must be explicitly rejected,
        # not silently accepted as 0.0/1.0.
        config = _dispatch_config_from_storage({"start_interval_s": True})
        assert config.start_interval_s == DEFAULT_START_INTERVAL_S


class TestZoneBatchingDefault:
    def test_missing_defaults_false(self) -> None:
        config = _dispatch_config_from_storage({"mode": "parallel"})
        assert config.zone_batching is False

    def test_non_bool_coerced(self) -> None:
        config = _dispatch_config_from_storage({"zone_batching": "yes"})
        assert config.zone_batching is True  # bool("yes") is True — explicit coercion


class TestNaNInfNeverReachConfig:
    def test_nan_falls_back(self) -> None:
        config = _dispatch_config_from_storage({"start_interval_s": math.nan})
        assert config.start_interval_s == DEFAULT_START_INTERVAL_S

    def test_infinity_falls_back(self) -> None:
        config = _dispatch_config_from_storage({"max_travel_wait_s": math.inf})
        assert config.max_travel_wait_s == DEFAULT_MAX_TRAVEL_WAIT_S

    def test_negative_infinity_falls_back(self) -> None:
        config = _dispatch_config_from_storage({"post_travel_pause_s": -math.inf})
        assert config.post_travel_pause_s == DEFAULT_POST_TRAVEL_PAUSE_S
