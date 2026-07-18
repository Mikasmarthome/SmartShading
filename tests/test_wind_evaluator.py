"""Tests for WindEvaluator (Tier 1 Safety Guard: Wind Protection).

WindEvaluator contract:
  - Fires (returns WIND_SAFE, position=0) ONLY when wind_protection_enabled
    is True AND effective_wind >= wind_threshold_ms.
  - Returns None when disabled (the default) or when wind data is unavailable.
  - Uses wind_gust_ms preferentially; falls back to wind_speed_ms.
  - Fail-safe: missing wind data → no trigger.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.evaluators.wind_evaluator import WindEvaluator
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState

_DEFAULT_THRESHOLD = 14.0


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def evaluator() -> WindEvaluator:
    return WindEvaluator()


@pytest.fixture()
def window() -> WindowConfig:
    return WindowConfig(
        id="window-south", name="South", zone_id="zone-1",
        azimuth=180.0, floor_level=0, cover_group_id="cg-south",
    )


@pytest.fixture()
def zone() -> ZoneConfig:
    return ZoneConfig(id="zone-1", name="Living Room")


def _wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    wind_speed_ms: float | None = None,
    wind_gust_ms: float | None = None,
    wind_protection_enabled: bool = True,
    wind_threshold_ms: float = _DEFAULT_THRESHOLD,
):
    return build_window_decision_input(
        window=window,
        zone=zone,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY,
        absence_active=False,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None,
        indoor_temp_c=None,
        exposure=None,
        is_in_solar_sector=False,
        wind_speed_ms=wind_speed_ms,
        wind_gust_ms=wind_gust_ms,
        wind_protection_enabled=wind_protection_enabled,
        wind_threshold_ms=wind_threshold_ms,
    )


# ---------------------------------------------------------------------------
# Core contract: opt-in gate
# ---------------------------------------------------------------------------

class TestWindEvaluatorOptIn:
    def test_disabled_by_default_no_trigger(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Default: wind_protection_enabled=False → never triggers
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
            wind_speed_ms=30.0,
        )
        assert evaluator.evaluate(wdi) is None

    def test_disabled_explicitly_no_trigger(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=25.0, wind_protection_enabled=False)
        assert evaluator.evaluate(wdi) is None

    def test_enabled_above_threshold_fires(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=15.0, wind_protection_enabled=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.WIND_SAFE


# ---------------------------------------------------------------------------
# Core contract: threshold logic
# ---------------------------------------------------------------------------

class TestWindEvaluatorThreshold:
    def test_at_threshold_fires(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=_DEFAULT_THRESHOLD)
        assert evaluator.evaluate(wdi) is not None

    def test_below_threshold_no_trigger(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=_DEFAULT_THRESHOLD - 0.1)
        assert evaluator.evaluate(wdi) is None

    def test_above_threshold_fires(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=_DEFAULT_THRESHOLD + 1.0)
        assert evaluator.evaluate(wdi) is not None

    def test_custom_threshold_respected(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Custom threshold 10 m/s; wind at 11 → fires
        wdi = _wdi(window, zone, wind_speed_ms=11.0, wind_threshold_ms=10.0)
        assert evaluator.evaluate(wdi) is not None

    def test_custom_threshold_not_met(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=9.9, wind_threshold_ms=10.0)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: gust vs. speed priority
# ---------------------------------------------------------------------------

class TestWindEvaluatorGustPriority:
    def test_gust_used_when_available(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Low speed (below threshold), high gust (above threshold) → gust wins → fires
        wdi = _wdi(window, zone, wind_speed_ms=5.0, wind_gust_ms=16.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.WIND_SAFE

    def test_gust_preferred_low_gust_overrides_high_speed(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # High speed (above threshold), low gust (below threshold) → gust wins → no trigger
        wdi = _wdi(window, zone, wind_speed_ms=20.0, wind_gust_ms=8.0)
        assert evaluator.evaluate(wdi) is None

    def test_speed_used_when_gust_none(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=15.0, wind_gust_ms=None)
        assert evaluator.evaluate(wdi) is not None


# ---------------------------------------------------------------------------
# Core contract: fail-safe
# ---------------------------------------------------------------------------

class TestWindEvaluatorFailSafe:
    def test_no_wind_data_returns_none(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_protection_enabled=True)
        assert evaluator.evaluate(wdi) is None

    def test_both_speed_and_gust_none_returns_none(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=None, wind_gust_ms=None, wind_protection_enabled=True)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: output fields
# ---------------------------------------------------------------------------

class TestWindEvaluatorOutput:
    def test_returns_wind_safe_state(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=15.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.WIND_SAFE

    def test_target_position_is_zero(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=15.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 0

    def test_decided_by_is_wind_evaluator(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=15.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "WindEvaluator"

    def test_category_is_safety(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """T7: Wind protection is tagged SAFETY — always allowed through an
        active Manual Override."""
        wdi = _wdi(window, zone, wind_speed_ms=15.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.category is DecisionCategory.SAFETY

    def test_window_id_matches(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=15.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_target_tilt_is_none(
        self, evaluator: WindEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=15.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None
