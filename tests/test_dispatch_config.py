"""Tests for models/dispatch_config.py — DispatchMode/DispatchConfig
(v1.2.0-beta.1, T11).

Coverage:
  DCFG-01  Default DispatchConfig reproduces the pre-T11 fixed 2.0s SPACED
           interval exactly (backward compatibility contract).
  DCFG-02  All three DispatchMode values exist with the expected string
           values (used directly in storage/config_flow).
  DCFG-03  zone_batching defaults to False (no behavior change for existing
           installations).
  DCFG-04  DispatchConfig is frozen (immutable) — same discipline as
           OverridePolicyConfig.
"""
from __future__ import annotations

import dataclasses

import pytest

from custom_components.smartshading.models.dispatch_config import (
    DEFAULT_MAX_TRAVEL_WAIT_S,
    DEFAULT_POST_TRAVEL_PAUSE_S,
    DEFAULT_START_INTERVAL_S,
    DispatchConfig,
    DispatchMode,
)


class TestDefaultsReproducePreT11Behavior:
    def test_default_mode_is_spaced(self) -> None:
        assert DispatchConfig().mode is DispatchMode.SPACED

    def test_default_interval_matches_legacy_constant(self) -> None:
        assert DispatchConfig().start_interval_s == 2.0
        assert DEFAULT_START_INTERVAL_S == 2.0

    def test_default_zone_batching_off(self) -> None:
        assert DispatchConfig().zone_batching is False


class TestDispatchModeValues:
    def test_expected_string_values(self) -> None:
        assert DispatchMode.PARALLEL.value == "parallel"
        assert DispatchMode.SPACED.value == "spaced"
        assert DispatchMode.SEQUENTIAL.value == "sequential"

    def test_exactly_three_modes(self) -> None:
        assert len(list(DispatchMode)) == 3


class TestDefaultsForSequentialFields:
    def test_max_travel_wait_and_post_pause_have_sane_defaults(self) -> None:
        config = DispatchConfig()
        assert config.max_travel_wait_s == DEFAULT_MAX_TRAVEL_WAIT_S
        assert config.post_travel_pause_s == DEFAULT_POST_TRAVEL_PAUSE_S
        assert config.max_travel_wait_s > 30.0  # comfortably above default travel time


class TestImmutability:
    def test_frozen_dataclass(self) -> None:
        config = DispatchConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.mode = DispatchMode.PARALLEL  # type: ignore[misc]
