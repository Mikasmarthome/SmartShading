"""Tests for StormEvaluator (Tier 1 Safety Guard: Storm Protection).

StormEvaluator contract:
  - Fires (returns STORM_SAFE, position=0) when storm_protection_enabled
    AND (weather_condition in STORM_CONDITIONS OR effective_wind >= 20 m/s).
  - Returns None when disabled, or when all sensor data is unavailable.
  - Uses wind_gust_ms preferentially; falls back to wind_speed_ms.
  - Fail-safe: missing sensor data → no trigger (never raise).
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.engines.weather_engine import WeatherCondition
from custom_components.smartshading.evaluators.storm_evaluator import StormEvaluator
from custom_components.smartshading.models.behavior_config import BehaviorConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def evaluator() -> StormEvaluator:
    return StormEvaluator()


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
    weather_condition: WeatherCondition | None = None,
    storm_protection_enabled: bool = True,
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
        weather_condition=weather_condition,
        storm_protection_enabled=storm_protection_enabled,
    )


# ---------------------------------------------------------------------------
# Core contract: fire on STORM_CONDITIONS weather codes
# ---------------------------------------------------------------------------

class TestStormEvaluatorWeatherCondition:
    def test_storm_condition_fires(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.STORM)
        result = evaluator.evaluate(wdi)
        assert result is not None

    def test_thunderstorm_condition_fires(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.THUNDERSTORM)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STORM_SAFE

    def test_hail_condition_fires(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.HAIL)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STORM_SAFE

    def test_clear_condition_does_not_fire(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.CLEAR)
        assert evaluator.evaluate(wdi) is None

    def test_rain_condition_does_not_fire(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.RAIN)
        assert evaluator.evaluate(wdi) is None

    def test_windy_condition_does_not_fire(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # WINDY is not in STORM_CONDITIONS — only STORM, THUNDERSTORM, HAIL are
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.WINDY)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: fire on high wind speed/gust
# ---------------------------------------------------------------------------

class TestStormEvaluatorWindThreshold:
    def test_wind_speed_at_threshold_fires(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Default storm threshold is 20.0 m/s; >= is inclusive
        wdi = _wdi(window, zone, wind_speed_ms=20.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STORM_SAFE

    def test_wind_speed_above_threshold_fires(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=25.0)
        assert evaluator.evaluate(wdi) is not None

    def test_wind_speed_below_threshold_does_not_fire(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=19.9)
        assert evaluator.evaluate(wdi) is None

    def test_wind_gust_at_threshold_fires(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_gust_ms=20.0)
        assert evaluator.evaluate(wdi) is not None

    def test_gust_preferred_over_speed_high_gust(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Low speed, high gust → gust wins → fires
        wdi = _wdi(window, zone, wind_speed_ms=5.0, wind_gust_ms=22.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STORM_SAFE

    def test_gust_preferred_over_speed_low_gust(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # High speed, low gust → gust wins → does NOT fire
        wdi = _wdi(window, zone, wind_speed_ms=25.0, wind_gust_ms=10.0)
        assert evaluator.evaluate(wdi) is None

    def test_speed_used_as_fallback_when_gust_none(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # gust is None → falls back to speed
        wdi = _wdi(window, zone, wind_speed_ms=21.0, wind_gust_ms=None)
        assert evaluator.evaluate(wdi) is not None


# ---------------------------------------------------------------------------
# Core contract: fail-safe and disabled
# ---------------------------------------------------------------------------

class TestStormEvaluatorFailSafe:
    def test_both_none_returns_none(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone)
        assert evaluator.evaluate(wdi) is None

    def test_disabled_no_trigger_on_storm_condition(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone,
            weather_condition=WeatherCondition.STORM,
            storm_protection_enabled=False,
        )
        assert evaluator.evaluate(wdi) is None

    def test_disabled_no_trigger_on_high_wind(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, wind_speed_ms=30.0, storm_protection_enabled=False)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: output fields
# ---------------------------------------------------------------------------

class TestStormEvaluatorOutput:
    def test_returns_storm_safe_state(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.STORM)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STORM_SAFE

    def test_target_position_is_zero(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.STORM)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 0

    def test_decided_by_is_storm_evaluator(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.STORM)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "StormEvaluator"

    def test_category_is_safety(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """T7: Storm protection is tagged SAFETY — always allowed through an
        active Manual Override (matches pre-T7 behavior: Tier 1 runs before
        Tier 2 and is override-immune by evaluator order)."""
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.STORM)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.category is DecisionCategory.SAFETY

    def test_window_id_matches(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.STORM)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_target_tilt_is_none(
        self, evaluator: StormEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, weather_condition=WeatherCondition.STORM)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None
