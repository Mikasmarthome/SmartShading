"""Tests for GlareEvaluator (Tier 4 Protection Floor).

Contract:
  - glare_protection_enabled=False → always returns None.
  - is_in_solar_sector=False → always returns None.
  - effective window exposure < glare_min_exposure_wm2 → None (geometry alone is
    not enough; the window must be meaningfully lit).
  - glare_protection_enabled=True AND is_in_solar_sector=True AND effective
    exposure >= glare_min_exposure_wm2 → LIGHT_SHADE floor.
  - Result: WindowDecision(LIGHT_SHADE, light_shade_position, "GlareEvaluator").

Key semantic difference from SolarEvaluator:
  SolarEvaluator:  is_in_solar_sector=True AND exposure >= light threshold (150).
  GlareEvaluator:  is_in_solar_sector=True AND exposure >= glare_min (default 100)
                   — fires on diffuse glare below the solar threshold but never on
                   geometry alone.  Uses the authoritative effective exposure
                   (measured source when valid), never raw weather brightness.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.evaluators.glare_evaluator import GlareEvaluator
from custom_components.smartshading.models.comfort import ComfortConfig
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
def evaluator() -> GlareEvaluator:
    return GlareEvaluator()


@pytest.fixture()
def window() -> WindowConfig:
    return WindowConfig(
        id="w-west", name="West", zone_id="z1",
        azimuth=270.0, floor_level=0, cover_group_id="cg-west",
    )


@pytest.fixture()
def zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Living")


def _wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    is_in_solar_sector: bool,
    glare_protection_enabled: bool = True,
    light_shade_ha: int = 40,    # HA 40 → internal 60
    # Default: window is meaningfully lit (above the glare_min_exposure default of
    # 100 W/m²).  Tests that exercise low/absent exposure override this explicitly.
    exposure_wm2: float | None = 200.0,
):
    from custom_components.smartshading.engines.exposure_engine import WindowExposure
    from datetime import datetime, timezone

    comfort = ComfortConfig(
        heat_protection_enabled=False,
        glare_protection_enabled=glare_protection_enabled,
    )
    exposure = None
    if exposure_wm2 is not None:
        now = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)
        exposure = WindowExposure(
            window_id=window.id, timestamp=now,
            sun_azimuth=270.0, sun_elevation=30.0,
            is_above_horizon=True, is_in_tolerance_window=True,
            azimuth_delta_deg=0.0, direct_radiation_factor=1.0,
            elevation_clipped=False,
            theoretical_exposure=exposure_wm2, learned_solar_impact_factor=1.0,
            seasonal_factor=1.0, effective_exposure=exposure_wm2,
        )
    return build_window_decision_input(
        window=window, zone=zone,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(light_shade_position=light_shade_ha),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY,
        absence_active=False,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None, indoor_temp_c=None,
        exposure=exposure,
        is_in_solar_sector=is_in_solar_sector,
        comfort_config=comfort,
    )


def _lit(window: WindowConfig, wm2: float = 200.0):
    """A WindowExposure that is meaningfully lit (above the glare_min default)
    so glare-firing tests that focus on other dimensions stay valid."""
    from custom_components.smartshading.engines.exposure_engine import WindowExposure
    from datetime import datetime, timezone
    return WindowExposure(
        window_id=window.id, timestamp=datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc),
        sun_azimuth=270.0, sun_elevation=30.0, is_above_horizon=True,
        is_in_tolerance_window=True, azimuth_delta_deg=0.0, direct_radiation_factor=1.0,
        elevation_clipped=False, theoretical_exposure=wm2, learned_solar_impact_factor=1.0,
        seasonal_factor=1.0, effective_exposure=wm2,
    )


# ---------------------------------------------------------------------------
# Glare disabled
# ---------------------------------------------------------------------------

class TestGlareEvaluatorDisabled:
    def test_disabled_returns_none_even_in_sector(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, glare_protection_enabled=False)
        assert evaluator.evaluate(wdi) is None

    def test_disabled_returns_none_with_high_exposure(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, glare_protection_enabled=False,
                   exposure_wm2=800.0)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Not in solar sector
# ---------------------------------------------------------------------------

class TestGlareEvaluatorNotInSector:
    def test_not_in_sector_returns_none(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=False)
        assert evaluator.evaluate(wdi) is None

    def test_not_in_sector_regardless_of_exposure(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=False, exposure_wm2=1000.0)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: in sector + enabled → LIGHT_SHADE
# ---------------------------------------------------------------------------

class TestGlareEvaluatorActive:
    def test_in_sector_enabled_returns_decision(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True)
        assert evaluator.evaluate(wdi) is not None

    def test_shading_state_is_light_shade(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_decided_by_is_glare_evaluator(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "GlareEvaluator"

    def test_window_id_is_correct(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_target_position_is_light_shade_position(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # light_shade_ha=40 → internal 60
        wdi = _wdi(window, zone, is_in_solar_sector=True, light_shade_ha=40)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == wdi.effective_behavior.light_shade_position
        assert result.target_position == 60  # 100 - 40

    def test_target_tilt_is_none(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None

    def test_custom_light_shade_position_respected(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Glare uses light_shade_position from effective_behavior — no hardcoded value."""
        # light_shade_ha=20 → internal 80
        wdi = _wdi(window, zone, is_in_solar_sector=True, light_shade_ha=20)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 80  # 100 - 20


# ---------------------------------------------------------------------------
# Key distinction from SolarEvaluator: exposure-independent
# ---------------------------------------------------------------------------

class TestGlareVsSolarSemantics:
    def test_does_not_fire_without_exposure(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Geometry alone is not enough: exposure None → glare suppressed."""
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=None)
        assert evaluator.evaluate(wdi) is None

    def test_fires_below_solar_threshold_but_above_glare_min(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Glare fires at 120 W/m² (>= glare_min 100, < solar light threshold 150)
        where SolarEvaluator would still return None — diffuse glare band."""
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=120.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_fires_at_exactly_solar_threshold(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """At 150 W/m² both Glare and Solar would fire; Glare is a Tier 4 floor."""
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=150.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE


# ---------------------------------------------------------------------------
# Scope boundary
# ---------------------------------------------------------------------------

class TestGlareEvaluatorScopeBoundary:
    def test_temperature_does_not_affect_result_when_solar_gain_disabled(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """GlareEvaluator ignores temperature when solar gain is disabled."""
        comfort = ComfortConfig(
            heat_protection_enabled=False,
            glare_protection_enabled=True,
            solar_gain_enabled=False,
        )
        for outdoor_c in (None, 5.0, 35.0):
            wdi = build_window_decision_input(
                window=window, zone=zone,
                global_defaults=GlobalDefaults(),
                shade_position_defaults=ShadePositionDefaults(),
                lifecycle_config=NightDayLifecycleConfig(id="default"),
                lifecycle_state=LifecycleState.DAY,
                absence_active=False,
                current_shading_state=ShadingState.OPEN,
                outdoor_temp_c=outdoor_c, indoor_temp_c=None,
                exposure=_lit(window), is_in_solar_sector=True,
                comfort_config=comfort,
            )
            result = evaluator.evaluate(wdi)
            assert result is not None, (
                f"Expected glare decision for outdoor_temp_c={outdoor_c} "
                f"when solar_gain_enabled=False"
            )
            assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_absence_does_not_affect_result(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """absence_active is AbsenceEvaluator's domain; GlareEvaluator ignores it."""
        comfort = ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=True)
        for absence in (True, False):
            wdi = build_window_decision_input(
                window=window, zone=zone,
                global_defaults=GlobalDefaults(),
                shade_position_defaults=ShadePositionDefaults(),
                lifecycle_config=NightDayLifecycleConfig(id="default"),
                lifecycle_state=LifecycleState.DAY,
                absence_active=absence,
                current_shading_state=ShadingState.OPEN,
                outdoor_temp_c=None, indoor_temp_c=None,
                exposure=_lit(window), is_in_solar_sector=True,
                comfort_config=comfort,
            )
            result = evaluator.evaluate(wdi)
            assert result is not None
            assert result.shading_state is ShadingState.LIGHT_SHADE


# ---------------------------------------------------------------------------
# Solar gain suppression (winter sun mode)
# ---------------------------------------------------------------------------

def _solar_gain_wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    outdoor_temp_c: float | None,
    solar_gain_enabled: bool = True,
    solar_gain_max_outdoor_temp_c: float = 12.0,
    heat_protection_enabled: bool = False,
    is_in_solar_sector: bool = True,
):
    """Build a WindowDecisionInput for solar gain suppression tests."""
    comfort = ComfortConfig(
        heat_protection_enabled=heat_protection_enabled,
        glare_protection_enabled=True,
        solar_gain_enabled=solar_gain_enabled,
        solar_gain_max_outdoor_temp_c=solar_gain_max_outdoor_temp_c,
    )
    return build_window_decision_input(
        window=window, zone=zone,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY,
        absence_active=False,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=outdoor_temp_c, indoor_temp_c=None,
        exposure=_lit(window), is_in_solar_sector=is_in_solar_sector,
        comfort_config=comfort,
    )


class TestGlareEvaluatorSolarGainSuppression:
    """Solar gain mode suppresses GlareEvaluator to allow winter heat gain."""

    def test_cold_outdoor_suppresses_glare(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Below solar_gain_max_outdoor_temp_c, GlareEvaluator returns None."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=5.0)
        assert evaluator.evaluate(wdi) is None

    def test_outdoor_at_boundary_suppresses_glare(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """At exactly solar_gain_max_outdoor_temp_c - 0.1, still suppressed."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=11.9)
        assert evaluator.evaluate(wdi) is None

    def test_outdoor_at_max_does_not_suppress_glare(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """At solar_gain_max_outdoor_temp_c, solar gain inactive — glare fires."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=12.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_warm_outdoor_does_not_suppress_glare(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Above solar_gain_max_outdoor_temp_c, solar gain inactive — glare fires."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=20.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_solar_gain_disabled_does_not_suppress_glare_at_cold_temp(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """solar_gain_enabled=False: GlareEvaluator fires regardless of temperature."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=5.0, solar_gain_enabled=False)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_outdoor_temp_none_does_not_suppress_glare(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """When outdoor_temp is unavailable, solar gain suppression is inactive."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=None)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_heat_protection_overrides_solar_gain_suppression(
        self, evaluator: GlareEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Heat protection is active + outdoor temp cold: suppression is cancelled."""
        comfort = ComfortConfig(
            heat_protection_enabled=True,
            heat_protection_outdoor_temp_c=5.0,
            glare_protection_enabled=True,
            solar_gain_enabled=True,
            solar_gain_max_outdoor_temp_c=12.0,
        )
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=6.0, indoor_temp_c=None,
            exposure=_lit(window), is_in_solar_sector=True,
            comfort_config=comfort,
        )
        result = evaluator.evaluate(wdi)
        assert result is not None, "Heat protection threshold exceeded: glare must fire"
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_solar_gain_suppression_flag_set_in_wdi(
        self, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """build_window_decision_input sets solar_gain_suppresses_shading correctly."""
        cold_wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=5.0)
        warm_wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=20.0)
        none_wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=None)

        assert cold_wdi.effective_behavior.solar_gain_suppresses_shading is True
        assert warm_wdi.effective_behavior.solar_gain_suppresses_shading is False
        assert none_wdi.effective_behavior.solar_gain_suppresses_shading is False


# ---------------------------------------------------------------------------
# Exposure gating: glare must not fire on geometry alone (real-case fix)
# ---------------------------------------------------------------------------

class TestGlareEvaluatorExposureGate:
    def test_in_sector_low_exposure_returns_none(self, evaluator, window, zone):
        # Real case: in sector but only ~66.7 W/m² effective → no glare.
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=66.7)
        assert evaluator.evaluate(wdi) is None

    def test_in_sector_no_exposure_returns_none(self, evaluator, window, zone):
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=None)
        assert evaluator.evaluate(wdi) is None

    def test_in_sector_sufficient_exposure_returns_light_shade(self, evaluator, window, zone):
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=120.0)
        result = evaluator.evaluate(wdi)
        assert result is not None and result.shading_state is ShadingState.LIGHT_SHADE

    def test_exposure_exactly_at_threshold_fires(self, evaluator, window, zone):
        # default glare_min_exposure_wm2 == 100.0; boundary is inclusive (>=).
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=100.0)
        assert evaluator.evaluate(wdi) is not None

    def test_exposure_just_below_threshold_suppressed(self, evaluator, window, zone):
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=99.9)
        assert evaluator.evaluate(wdi) is None
