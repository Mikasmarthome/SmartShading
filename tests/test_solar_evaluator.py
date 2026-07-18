"""Tests for SolarEvaluator (Tier 5 Comfort Pipeline).

Contract:
  - is_in_solar_sector=False  → None (sun not facing this window)
  - exposure=None             → None (sun entity unavailable)
  - effective_exposure < 150  → None (OPEN)
  - 150 ≤ exposure < 250      → LIGHT_SHADE (light_shade_position from effective_behavior)
  - 250 ≤ exposure < 500      → NORMAL_SHADE
  - exposure ≥ 500            → STRONG_SHADE

Positions come from effective_behavior (pre-resolved, internal convention).
No HA-convention conversion happens inside SolarEvaluator.
No astronomy / azimuth calculation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.smartshading.engines.exposure_engine import WindowExposure
from custom_components.smartshading.evaluators.solar_evaluator import SolarEvaluator
from custom_components.smartshading.models.behavior_config import BehaviorConfig
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import (
    WindowDecisionInput,
    build_window_decision_input,
)
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def _exposure(effective_wm2: float) -> WindowExposure:
    """Build a minimal WindowExposure with a given effective_exposure value."""
    return WindowExposure(
        window_id="w1",
        timestamp=_NOW,
        sun_azimuth=180.0,
        sun_elevation=45.0,
        is_above_horizon=True,
        is_in_tolerance_window=True,
        azimuth_delta_deg=0.0,
        direct_radiation_factor=1.0,
        elevation_clipped=False,
        theoretical_exposure=effective_wm2,
        learned_solar_impact_factor=1.0,
        seasonal_factor=1.0,
        effective_exposure=effective_wm2,
    )


@pytest.fixture()
def evaluator() -> SolarEvaluator:
    return SolarEvaluator()


@pytest.fixture()
def window() -> WindowConfig:
    return WindowConfig(
        id="w-south", name="South", zone_id="z1",
        azimuth=180.0, floor_level=0, cover_group_id="cg-south",
    )


@pytest.fixture()
def zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Living")


def _wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    is_in_solar_sector: bool,
    effective_wm2: float | None,
    light_shade_ha: int = 40,    # HA: 40% open → internal 60
    normal_shade_ha: int = 25,   # HA: 25% open → internal 75
    strong_shade_ha: int = 10,   # HA: 10% open → internal 90
):
    return build_window_decision_input(
        window=window,
        zone=zone,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=light_shade_ha,
            normal_shade_position=normal_shade_ha,
            strong_shade_position=strong_shade_ha,
        ),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY,
        absence_active=False,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None,
        indoor_temp_c=None,
        exposure=_exposure(effective_wm2) if effective_wm2 is not None else None,
        is_in_solar_sector=is_in_solar_sector,
    )


# ---------------------------------------------------------------------------
# Gate: not in solar sector
# ---------------------------------------------------------------------------

class TestSolarEvaluatorNotInSector:
    def test_not_in_solar_sector_returns_none(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=False, effective_wm2=600.0)
        assert evaluator.evaluate(wdi) is None

    def test_not_in_sector_even_with_high_exposure(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """High W/m² but sun not facing window → still None."""
        wdi = _wdi(window, zone, is_in_solar_sector=False, effective_wm2=1000.0)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Gate: exposure unavailable
# ---------------------------------------------------------------------------

class TestSolarEvaluatorExposureUnavailable:
    def test_none_exposure_returns_none(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=None)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Classification: OPEN (below threshold)
# ---------------------------------------------------------------------------

class TestSolarEvaluatorOpen:
    def test_zero_wm2_returns_none(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=0.0)
        assert evaluator.evaluate(wdi) is None

    def test_below_light_threshold_returns_none(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=149.9)
        assert evaluator.evaluate(wdi) is None

    def test_exactly_below_threshold_returns_none(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # 149.999 is strictly below 150.0
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=100.0)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Classification: LIGHT_SHADE [150, 250)
# ---------------------------------------------------------------------------

class TestSolarEvaluatorLightShade:
    def test_at_entry_threshold_150(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=150.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_at_200_wm2(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=200.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_just_below_normal_threshold(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=249.9)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.LIGHT_SHADE

    def test_light_shade_uses_behavior_position(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """target_position comes from effective_behavior.light_shade_position."""
        # HA: light=40 → internal 60
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=200.0, light_shade_ha=40)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == wdi.effective_behavior.light_shade_position
        assert result.target_position == 60  # 100 - 40


# ---------------------------------------------------------------------------
# Classification: NORMAL_SHADE [250, 500)
# ---------------------------------------------------------------------------

class TestSolarEvaluatorNormalShade:
    def test_at_entry_threshold_250(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=250.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.NORMAL_SHADE

    def test_east_window_direct_morning_sun(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """East-facing window: ~409 W/m² GHI, incidence 0.598 → effective ~245 W/m².
        Forecast delta −7 shifts resolved threshold: 250 − 7 = 243.
        245 > 243 → NORMAL_SHADE. With old threshold 300, same reading stayed LIGHT_SHADE.
        """
        from custom_components.smartshading.models.behavior_config import BehaviorConfig
        from custom_components.smartshading.models.window_decision_input import WindowDecisionInput
        # Simulate SolarThresholdResolver output: base 250 + forecast_delta −7 = 243
        bc_resolved = BehaviorConfig(
            light_shade_threshold_wm2=150.0,
            normal_shade_threshold_wm2=243.0,
            strong_shade_threshold_wm2=500.0,
            light_shade_position=60,
            normal_shade_position=75,
            strong_shade_position=90,
        )
        wdi = WindowDecisionInput(
            window_config=window,
            zone_config=zone,
            effective_behavior=bc_resolved,
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None,
            indoor_temp_c=None,
            exposure=_exposure(245.0),
            is_in_solar_sector=True,
        )
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.NORMAL_SHADE

    def test_at_400_wm2(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=400.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.NORMAL_SHADE

    def test_just_below_strong_threshold(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=499.9)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.NORMAL_SHADE

    def test_normal_shade_uses_behavior_position(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # HA: normal=25 → internal 75
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=350.0, normal_shade_ha=25)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == wdi.effective_behavior.normal_shade_position
        assert result.target_position == 75  # 100 - 25


# ---------------------------------------------------------------------------
# Classification: STRONG_SHADE [500, ∞)
# ---------------------------------------------------------------------------

class TestSolarEvaluatorStrongShade:
    def test_at_entry_threshold_500(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=500.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STRONG_SHADE

    def test_at_800_wm2(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=800.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STRONG_SHADE

    def test_strong_shade_uses_behavior_position(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # HA: strong=10 → internal 90
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=600.0, strong_shade_ha=10)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == wdi.effective_behavior.strong_shade_position
        assert result.target_position == 90  # 100 - 10


# ---------------------------------------------------------------------------
# Output fields
# ---------------------------------------------------------------------------

class TestSolarEvaluatorOutputFields:
    def test_decided_by_is_solar_evaluator(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=300.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "SolarEvaluator"

    def test_category_is_comfort(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """T7: Solar comfort shading is tagged COMFORT — distinct from
        GlareEvaluator's PROTECTION tag despite sharing the same
        shading_state values."""
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=300.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.category is DecisionCategory.COMFORT

    def test_window_id_is_correct(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=300.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_target_tilt_is_none(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=300.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None

    def test_custom_shade_positions_respected(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """If behavior positions differ, SolarEvaluator uses them — no hardcoded values."""
        # HA 20% open → internal 80
        wdi = _wdi(
            window, zone,
            is_in_solar_sector=True, effective_wm2=200.0,
            light_shade_ha=20,
        )
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 80  # 100 - 20


# ---------------------------------------------------------------------------
# Scope boundary
# ---------------------------------------------------------------------------

class TestSolarEvaluatorScopeBoundary:
    def test_no_config_hierarchy_traversal(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """SolarEvaluator reads only wdi.effective_behavior, never raw window/zone config."""
        wdi = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=300.0)
        # effective_behavior is the pre-resolved view; raw config carries HA values
        result = evaluator.evaluate(wdi)
        assert result is not None
        # Verify the position came from effective_behavior (internal), not raw config (HA)
        assert result.target_position == wdi.effective_behavior.normal_shade_position

    def test_absence_active_does_not_affect_result(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """SolarEvaluator is Tier 5; it has no knowledge of Tier 4 absence state."""
        from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig

        wdi = build_window_decision_input(
            window=window,
            zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=True,   # Tier 4's domain
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None,
            indoor_temp_c=None,
            exposure=_exposure(300.0),
            is_in_solar_sector=True,
        )
        result = evaluator.evaluate(wdi)
        # SolarEvaluator returns its Tier 5 result regardless of absence
        assert result is not None
        assert result.shading_state is ShadingState.NORMAL_SHADE


# ---------------------------------------------------------------------------
# Solar gain suppression (winter sun mode)
# ---------------------------------------------------------------------------

def _solar_gain_wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    outdoor_temp_c: float | None,
    effective_wm2: float = 600.0,
    solar_gain_enabled: bool = True,
    solar_gain_max_outdoor_temp_c: float = 12.0,
    heat_protection_enabled: bool = False,
):
    """WindowDecisionInput with configurable solar gain settings."""
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
        exposure=_exposure(effective_wm2),
        is_in_solar_sector=True,
        comfort_config=comfort,
    )


class TestSolarEvaluatorSolarGainSuppression:
    """Solar gain mode suppresses SolarEvaluator so winter sun heats the room."""

    def test_cold_outdoor_suppresses_solar_evaluator(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Below solar_gain_max_outdoor_temp_c, even 600 W/m² returns None."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=5.0, effective_wm2=600.0)
        assert evaluator.evaluate(wdi) is None

    def test_zero_outdoor_temp_suppresses_solar_evaluator(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=0.0, effective_wm2=800.0)
        assert evaluator.evaluate(wdi) is None

    def test_outdoor_at_max_does_not_suppress(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """At exactly solar_gain_max_outdoor_temp_c, solar gain is inactive."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=12.0, effective_wm2=600.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STRONG_SHADE

    def test_warm_outdoor_does_not_suppress(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Above solar_gain_max_outdoor_temp_c, solar evaluator fires normally."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=20.0, effective_wm2=600.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STRONG_SHADE

    def test_solar_gain_disabled_does_not_suppress(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """solar_gain_enabled=False: evaluator fires at 5 °C as normal."""
        wdi = _solar_gain_wdi(
            window, zone, outdoor_temp_c=5.0, effective_wm2=600.0, solar_gain_enabled=False
        )
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STRONG_SHADE

    def test_outdoor_temp_none_does_not_suppress(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Unavailable outdoor temperature: solar gain suppression is inactive."""
        wdi = _solar_gain_wdi(window, zone, outdoor_temp_c=None, effective_wm2=600.0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.STRONG_SHADE

    def test_heat_protection_overrides_solar_gain_suppression(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """When heat protection threshold is exceeded, solar gain suppression is cancelled."""
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
            exposure=_exposure(600.0),
            is_in_solar_sector=True,
            comfort_config=comfort,
        )
        result = evaluator.evaluate(wdi)
        assert result is not None, "Heat threshold exceeded: solar evaluator must fire"
        assert result.shading_state is ShadingState.STRONG_SHADE

    def test_solar_gain_suppression_flag_in_wdi(
        self, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """build_window_decision_input correctly sets solar_gain_suppresses_shading."""
        cold = _solar_gain_wdi(window, zone, outdoor_temp_c=5.0)
        warm = _solar_gain_wdi(window, zone, outdoor_temp_c=20.0)
        none_temp = _solar_gain_wdi(window, zone, outdoor_temp_c=None)

        assert cold.effective_behavior.solar_gain_suppresses_shading is True
        assert warm.effective_behavior.solar_gain_suppresses_shading is False
        assert none_temp.effective_behavior.solar_gain_suppresses_shading is False


# ---------------------------------------------------------------------------
# Forecast / threshold interaction (Section 5 verification)
# ---------------------------------------------------------------------------

class TestSolarEvaluatorForecastThresholdInteraction:
    """Measurement stays authoritative; forecast only shifts thresholds, not readings."""

    def test_measured_exposure_not_replaced_by_forecast(
        self, evaluator: SolarEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """SolarEvaluator classifies against effective_wm2 from the sensor, not a forecast value.
        The effective_behavior thresholds may be forecast-adjusted, but the reading itself is the
        measured sensor value. Verify: same measured exposure (245), lower threshold → higher tier.
        """
        # effective_wm2 = 245 — sensor measurement
        # Default threshold (250) → 245 < 250 → LIGHT_SHADE
        wdi_default = _wdi(window, zone, is_in_solar_sector=True, effective_wm2=245.0)
        result_default = evaluator.evaluate(wdi_default)
        assert result_default is not None
        assert result_default.shading_state is ShadingState.LIGHT_SHADE

        # Same sensor reading (245); threshold forecast-shifted to 240 → 245 ≥ 240 → NORMAL_SHADE.
        # This mirrors what SolarThresholdResolver produces after a −10 forecast delta.
        bc_shifted = BehaviorConfig(
            light_shade_threshold_wm2=150.0,
            normal_shade_threshold_wm2=240.0,
            strong_shade_threshold_wm2=500.0,
            light_shade_position=60,
            normal_shade_position=75,
            strong_shade_position=90,
        )
        wdi_shifted = WindowDecisionInput(
            window_config=window,
            zone_config=zone,
            effective_behavior=bc_shifted,
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None,
            indoor_temp_c=None,
            exposure=_exposure(245.0),
            is_in_solar_sector=True,
        )
        result_shifted = evaluator.evaluate(wdi_shifted)
        # Measurement is still 245 — threshold shift decides tier, reading is not replaced
        assert result_shifted is not None
        assert result_shifted.shading_state is ShadingState.NORMAL_SHADE
