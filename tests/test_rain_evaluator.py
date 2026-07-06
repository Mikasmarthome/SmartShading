"""Unit tests for RainEvaluator (Tier 1 Safety Guard: Rain Protection).

Contract:
  - Returns RAIN_SAFE only when rain_protection_enabled=True AND rain_status=RAINING.
  - Returns None when protection is disabled (opt-out covers).
  - Returns None when rain_status is None or UNKNOWN (fail-safe).
  - Uses effective_behavior.rain_safe_position as target_position.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.engines.rain_engine import RainStatus
from custom_components.smartshading.evaluators.rain_evaluator import RainEvaluator
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def evaluator() -> RainEvaluator:
    return RainEvaluator()


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
    rain_status: RainStatus | None = RainStatus.RAINING,
    rain_protection_enabled: bool = True,
    rain_safe_position: int = 100,   # internal convention: 100 = deployed/awning retracted
    rain_release_delay_min: int = 30,
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
        rain_status=rain_status,
        rain_protection_enabled=rain_protection_enabled,
        rain_safe_position=rain_safe_position,
        rain_release_delay_min=rain_release_delay_min,
    )


# ---------------------------------------------------------------------------
# Core contract
# ---------------------------------------------------------------------------

class TestRainEvaluatorOptIn:
    def test_disabled_with_rain_returns_none(self, evaluator, window, zone):
        wdi = _wdi(window, zone, rain_protection_enabled=False)
        assert evaluator.evaluate(wdi) is None

    def test_enabled_with_raining_returns_rain_safe(self, evaluator, window, zone):
        wdi = _wdi(window, zone, rain_protection_enabled=True, rain_status=RainStatus.RAINING)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.RAIN_SAFE

    def test_decided_by_is_rain_evaluator(self, evaluator, window, zone):
        wdi = _wdi(window, zone)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "RainEvaluator"

    def test_window_id_matches(self, evaluator, window, zone):
        wdi = _wdi(window, zone)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id


# ---------------------------------------------------------------------------
# Fail-safe: absent data never triggers
# ---------------------------------------------------------------------------

class TestRainEvaluatorFailSafe:
    def test_unknown_status_returns_none(self, evaluator, window, zone):
        wdi = _wdi(window, zone, rain_status=RainStatus.UNKNOWN)
        assert evaluator.evaluate(wdi) is None

    def test_none_rain_status_returns_none(self, evaluator, window, zone):
        wdi = _wdi(window, zone, rain_status=None)
        assert evaluator.evaluate(wdi) is None

    def test_dry_status_returns_none(self, evaluator, window, zone):
        wdi = _wdi(window, zone, rain_status=RainStatus.DRY)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Position semantics
# ---------------------------------------------------------------------------

class TestRainEvaluatorPosition:
    def test_safe_position_passed_to_decision(self, evaluator, window, zone):
        wdi = _wdi(window, zone, rain_safe_position=75)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 75

    def test_zero_safe_position(self, evaluator, window, zone):
        wdi = _wdi(window, zone, rain_safe_position=0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 0
