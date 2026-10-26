"""A1 -- TierOrchestrator.evaluate_tier1() / evaluate_without_sun().

Pure unit tests (no Home Assistant, no coordinator): Tier-1 Safety does not
read any sun value, so it can be evaluated on its own; everything else must
hold when sun data is unusable.  The coordinator-level behavior is covered in
test_rain_safe_position_semantics.py ("A1: Tier-1 Safety without usable
sun.sun") and, against real Home Assistant, in
tests/real_ha/test_real_ha_a1_safety_without_sun.py.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.engines.rain_engine import RainStatus
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState




def _wdi(*, rain: RainStatus = RainStatus.DRY, wind: float | None = None, **kw):
    return build_window_decision_input(
        window=WindowConfig(
            id="w1", name="w1", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1",
        ),
        zone=ZoneConfig(id="z1", name="z1"),
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
        wind_speed_ms=wind,
        wind_protection_enabled=True,
        wind_threshold_ms=14.0,
        rain_status=rain,
        rain_protection_enabled=True,
        rain_safe_position=100,
        rain_release_delay_min=30,
        **kw,
    )


class TestOrchestratorWithoutSun:
    def test_nothing_active_holds_with_no_target(self):
        d = TierOrchestrator().evaluate_without_sun(_wdi())
        assert d.decided_by == "SunUnavailable:hold"
        assert d.target_position is None
        assert d.category is DecisionCategory.HOLD

    def test_without_the_sun_variant_the_daytime_fallback_would_open(self):
        """Why the hold exists: the regular pipeline would fall through to the
        daytime fallback OPEN when nothing else fires."""
        d = TierOrchestrator().evaluate_window(_wdi())
        assert d.decided_by == "TierOrchestrator:fallback"
        assert d.target_position == 0

    @pytest.mark.parametrize("kw,state", [
        (dict(rain=RainStatus.RAINING), ShadingState.RAIN_SAFE),
        (dict(wind=16.0), ShadingState.WIND_SAFE),
        (dict(wind=25.0), ShadingState.STORM_SAFE),
    ])
    def test_tier1_wins_without_the_sun(self, kw, state):
        d = TierOrchestrator().evaluate_without_sun(_wdi(**kw))
        assert d.shading_state is state
        assert d.category is DecisionCategory.SAFETY

    def test_priority_storm_over_wind_over_rain(self):
        orch = TierOrchestrator()
        assert orch.evaluate_without_sun(
            _wdi(rain=RainStatus.RAINING, wind=25.0)).shading_state is ShadingState.STORM_SAFE
        assert orch.evaluate_without_sun(
            _wdi(rain=RainStatus.RAINING, wind=16.0)).shading_state is ShadingState.WIND_SAFE

    @pytest.mark.parametrize("kw", [dict(rain=RainStatus.RAINING), dict(wind=16.0), dict(wind=25.0), dict()])
    def test_tier1_helper_is_what_the_full_pipeline_uses(self, kw):
        orch = TierOrchestrator()
        wdi = _wdi(**kw)
        t1 = orch.evaluate_tier1(wdi)
        full = orch.evaluate_window(wdi)
        if t1 is not None:
            assert full == t1
        else:
            assert full.category is not DecisionCategory.SAFETY
