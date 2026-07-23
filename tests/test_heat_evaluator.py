"""Tests for HeatEvaluator (Tier 4 Protection Floor).

Contract:
  - heat_protection_enabled=False → both thresholds become None
    → HeatEvaluator always returns None.
  - outdoor_temp_c >= heat_outdoor_threshold_c → heat needed.
  - indoor_temp_c  >= heat_indoor_threshold_c  → heat needed (OR logic).
  - Both temperatures below thresholds → None.
  - Sensor unavailable (None) → no false-positive trigger (fail-safe).
  - Result: WindowDecision(NORMAL_SHADE, normal_shade_position, "HeatEvaluator").

1:1 migration of ComfortAwareStateEvaluator Rule 1 into Tier 4 floor pattern.
"""
from __future__ import annotations

import types as _types

import pytest

from custom_components.smartshading.evaluators.heat_evaluator import HeatEvaluator
from custom_components.smartshading.models.behavior_config import BehaviorConfig
from custom_components.smartshading.models.comfort import ComfortConfig
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
def evaluator() -> HeatEvaluator:
    return HeatEvaluator()


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
    outdoor_temp_c: float | None,
    indoor_temp_c: float | None,
    heat_protection_enabled: bool = True,
    heat_outdoor_threshold_c: float = 26.0,
    heat_indoor_threshold_c: float = 24.0,
    heat_hysteresis_c: float = 1.0,
    heat_previously_active: bool = False,
    normal_shade_ha: int = 25,   # HA 25 → internal 75
    is_in_solar_sector: bool = True,
    exposure=None,
):
    comfort_config = ComfortConfig(
        heat_protection_enabled=heat_protection_enabled,
        heat_protection_outdoor_temp_c=heat_outdoor_threshold_c,
        heat_protection_indoor_temp_c=heat_indoor_threshold_c,
        heat_protection_hysteresis_c=heat_hysteresis_c,
        glare_protection_enabled=False,
    )
    return build_window_decision_input(
        window=window, zone=zone,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(normal_shade_position=normal_shade_ha),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY,
        absence_active=False,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=outdoor_temp_c,
        indoor_temp_c=indoor_temp_c,
        exposure=exposure,
        is_in_solar_sector=is_in_solar_sector,
        comfort_config=comfort_config,
        heat_previously_active=heat_previously_active,
    )


# ---------------------------------------------------------------------------
# Heat disabled
# ---------------------------------------------------------------------------

class TestHeatEvaluatorDisabled:
    def test_disabled_never_fires(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """heat_protection_enabled=False → both thresholds are None → None always."""
        wdi = _wdi(window, zone, outdoor_temp_c=40.0, indoor_temp_c=35.0,
                   heat_protection_enabled=False)
        assert evaluator.evaluate(wdi) is None

    def test_both_thresholds_none_returns_none(self, evaluator: HeatEvaluator) -> None:
        """Direct BehaviorConfig with both thresholds None."""
        from custom_components.smartshading.models.window_decision_input import WindowDecisionInput
        from custom_components.smartshading.models.window import WindowConfig as WC
        from custom_components.smartshading.models.zone import ZoneConfig as ZC

        w = WC(id="w", name="W", zone_id="z", azimuth=0.0, floor_level=0, cover_group_id="c")
        z = ZC(id="z", name="Z")
        b = BehaviorConfig(heat_outdoor_threshold_c=None, heat_indoor_threshold_c=None)
        wdi = WindowDecisionInput(
            window_config=w, zone_config=z, effective_behavior=b,
            lifecycle_state=LifecycleState.DAY, absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=50.0, indoor_temp_c=50.0,
            exposure=None, is_in_solar_sector=False,
        )
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Outdoor threshold
# ---------------------------------------------------------------------------

class TestHeatEvaluatorOutdoorThreshold:
    def test_outdoor_exactly_at_threshold_fires(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=26.0, indoor_temp_c=None,
                   heat_outdoor_threshold_c=26.0)
        assert evaluator.evaluate(wdi) is not None

    def test_outdoor_above_threshold_fires(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None)
        assert evaluator.evaluate(wdi) is not None

    def test_outdoor_below_threshold_no_fire(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=25.9, indoor_temp_c=None)
        assert evaluator.evaluate(wdi) is None

    def test_outdoor_none_sensor_no_fire(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Missing outdoor sensor → no false-positive (fail-safe)."""
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=None)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Indoor threshold
# ---------------------------------------------------------------------------

class TestHeatEvaluatorIndoorThreshold:
    def test_indoor_exactly_at_threshold_fires(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=24.0,
                   heat_indoor_threshold_c=24.0)
        assert evaluator.evaluate(wdi) is not None

    def test_indoor_above_threshold_fires(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=25.0)
        assert evaluator.evaluate(wdi) is not None

    def test_indoor_below_threshold_no_fire(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=23.9)
        assert evaluator.evaluate(wdi) is None

    def test_indoor_none_sensor_no_fire(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Missing indoor sensor → no false-positive."""
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=None)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# OR logic: either outdoor OR indoor is sufficient
# ---------------------------------------------------------------------------

class TestHeatEvaluatorOrLogic:
    def test_outdoor_triggers_even_when_indoor_below(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=20.0)
        assert evaluator.evaluate(wdi) is not None

    def test_indoor_triggers_even_when_outdoor_below(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=20.0, indoor_temp_c=25.0)
        assert evaluator.evaluate(wdi) is not None

    def test_both_below_threshold_no_fire(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=20.0, indoor_temp_c=22.0)
        assert evaluator.evaluate(wdi) is None

    def test_outdoor_none_indoor_triggers(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Outdoor sensor unavailable, indoor above threshold → still fires."""
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=25.0)
        assert evaluator.evaluate(wdi) is not None

    def test_indoor_none_outdoor_triggers(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Indoor sensor unavailable, outdoor above threshold → still fires."""
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None)
        assert evaluator.evaluate(wdi) is not None


# ---------------------------------------------------------------------------
# Output fields
# ---------------------------------------------------------------------------

class TestHeatEvaluatorOutput:
    def test_shading_state_is_normal_shade(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.NORMAL_SHADE

    def test_decided_by_is_heat_evaluator(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "HeatEvaluator"

    def test_category_is_protection(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """T7: Heat protection is tagged PROTECTION — this is the category
        the Manual Override policy checks for allow_protection gating."""
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.category is DecisionCategory.PROTECTION

    def test_target_position_is_normal_shade_position(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # normal_shade_ha=25 → internal 75
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None, normal_shade_ha=25)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == wdi.effective_behavior.normal_shade_position
        assert result.target_position == 75  # 100 - 25

    def test_window_id_is_correct(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_target_tilt_is_none(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None


# ---------------------------------------------------------------------------
# Scope boundary
# ---------------------------------------------------------------------------

class TestHeatEvaluatorScopeBoundary:
    def test_lifecycle_state_does_not_affect_result(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """HeatEvaluator is a Tier 4 floor; it doesn't check lifecycle state."""
        for lifecycle_state in (LifecycleState.NIGHT, LifecycleState.MORNING, LifecycleState.DAY):
            comfort = ComfortConfig(heat_protection_enabled=True, glare_protection_enabled=False)
            wdi = build_window_decision_input(
                window=window, zone=zone,
                global_defaults=GlobalDefaults(),
                shade_position_defaults=ShadePositionDefaults(),
                lifecycle_config=NightDayLifecycleConfig(id="default"),
                lifecycle_state=lifecycle_state,
                absence_active=False,
                current_shading_state=ShadingState.OPEN,
                outdoor_temp_c=30.0, indoor_temp_c=None,
                exposure=None, is_in_solar_sector=True,
                comfort_config=comfort,
            )
            result = evaluator.evaluate(wdi)
            assert result is not None, f"Expected decision for lifecycle_state={lifecycle_state}"
            assert result.shading_state is ShadingState.NORMAL_SHADE

    def test_in_solar_sector_permits_heat_shading_regardless_of_exposure(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """is_in_solar_sector=True allows heat shading even when exposure=None."""
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None, is_in_solar_sector=True, exposure=None)
        assert evaluator.evaluate(wdi) is not None


# ---------------------------------------------------------------------------
# Sector gate
# ---------------------------------------------------------------------------

class TestHeatEvaluatorSectorGate:
    """HeatEvaluator must not shade when is_in_solar_sector is False.

    is_in_solar_sector already incorporates the manual sector override,
    obstruction zones, and the automatic tolerance sector.  Using
    exposure.effective_exposure as an alternative trigger path would
    incorrectly fire when a manual sector blocks the sun but the automatic
    tolerance sector still matches — causing false heat-shade decisions.

    Gate: return None whenever is_in_solar_sector is False (hard gate,
    identical to SolarEvaluator and GlareEvaluator).
    """

    def test_no_shade_outside_sector_with_no_exposure_outdoor(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Outside solar sector, no exposure → no heat shading."""
        wdi = _wdi(window, zone, outdoor_temp_c=35.0, indoor_temp_c=None,
                   is_in_solar_sector=False, exposure=None)
        assert evaluator.evaluate(wdi) is None

    def test_no_shade_outside_sector_with_no_exposure_indoor(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Indoor trigger fires temperatures above threshold; outside sector
        → hard gate blocks regardless of exposure."""
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=28.0,
                   is_in_solar_sector=False, exposure=None)
        assert evaluator.evaluate(wdi) is None

    def test_shades_when_in_solar_sector_exposure_none(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """is_in_solar_sector=True is sufficient to allow heat shading even
        when the exposure object is missing (sun.sun entity unavailable)."""
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None,
                   is_in_solar_sector=True, exposure=None)
        assert evaluator.evaluate(wdi) is not None

    def test_no_shade_outside_sector_with_positive_exposure(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Hard sector gate: is_in_solar_sector=False → None, even when
        effective_exposure is positive (e.g. from the automatic tolerance sector
        while the manual sector blocks).  This is the v1.0.4 bug fix."""
        exp = _types.SimpleNamespace(effective_exposure=560.0)
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None,
                   is_in_solar_sector=False, exposure=exp)
        assert evaluator.evaluate(wdi) is None

    def test_no_shade_outside_sector_zero_effective_exposure(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Zero effective_exposure with sector=False → None (gate blocks)."""
        exp = _types.SimpleNamespace(effective_exposure=0.0)
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None,
                   is_in_solar_sector=False, exposure=exp)
        assert evaluator.evaluate(wdi) is None


class TestHeatEvaluatorEffectiveExposureGate:
    """beta.10 field regression: heat protection must not fire on a geometry-only
    sun sector when almost no solar energy reaches the window (heavy cloud)."""

    def test_field_case_cloudy_low_exposure_no_shade(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Real export: outdoor 17.1 C, indoor missing, sector geometrically true,
        # effective exposure ~5 W/m² (heavy cloud) → must NOT shade.
        exp = _types.SimpleNamespace(effective_exposure=5.13)
        wdi = _wdi(window, zone, outdoor_temp_c=17.1, indoor_temp_c=None,
                   heat_outdoor_threshold_c=17.0, is_in_solar_sector=True, exposure=exp)
        assert evaluator.evaluate(wdi) is None

    def test_geometry_only_low_exposure_no_shade(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        exp = _types.SimpleNamespace(effective_exposure=5.0)
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None,
                   is_in_solar_sector=True, exposure=exp)
        assert evaluator.evaluate(wdi) is None

    def test_indoor_missing_low_exposure_stays_conservative(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Missing indoor value must make heat MORE conservative, not fire anyway.
        exp = _types.SimpleNamespace(effective_exposure=20.0)
        wdi = _wdi(window, zone, outdoor_temp_c=40.0, indoor_temp_c=None,
                   is_in_solar_sector=True, exposure=exp)
        assert evaluator.evaluate(wdi) is None

    def test_none_exposure_preserves_prior_behavior(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # No sun data at all → prior temperature+sector behaviour (backward compat).
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None,
                   is_in_solar_sector=True, exposure=None)
        assert evaluator.evaluate(wdi) is not None

    def test_sufficient_exposure_still_shades(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Real heat context: hot + genuine solar load → still shades.
        exp = _types.SimpleNamespace(effective_exposure=400.0)
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=None,
                   is_in_solar_sector=True, exposure=exp)
        d = evaluator.evaluate(wdi)
        assert d is not None and d.decided_by == "HeatEvaluator"

    def test_indoor_hot_with_sufficient_exposure_shades(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        exp = _types.SimpleNamespace(effective_exposure=180.0)
        wdi = _wdi(window, zone, outdoor_temp_c=20.0, indoor_temp_c=28.0,
                   heat_indoor_threshold_c=26.0, is_in_solar_sector=True, exposure=exp)
        assert evaluator.evaluate(wdi) is not None

    def test_floor_never_exceeds_configured_light_threshold(
        self, evaluator: HeatEvaluator
    ) -> None:
        # If a window configures a light-shade threshold below the 100 W/m² floor,
        # heat protection must use the lower configured threshold, so it never
        # demands more solar than the lightest comfort shade.
        from custom_components.smartshading.models.window_decision_input import WindowDecisionInput
        from custom_components.smartshading.models.window import WindowConfig as WC
        from custom_components.smartshading.models.zone import ZoneConfig as ZC
        w = WC(id="w", name="W", zone_id="z", azimuth=0.0, floor_level=0, cover_group_id="c")
        z = ZC(id="z", name="Z")
        b = BehaviorConfig(heat_outdoor_threshold_c=26.0, heat_indoor_threshold_c=None,
                           light_shade_threshold_wm2=80.0, normal_shade_position=75)

        def _wdi_exp(effective):
            return WindowDecisionInput(
                window_config=w, zone_config=z, effective_behavior=b,
                lifecycle_state=LifecycleState.DAY, absence_active=False,
                current_shading_state=ShadingState.OPEN,
                outdoor_temp_c=30.0, indoor_temp_c=None,
                exposure=_types.SimpleNamespace(effective_exposure=effective),
                is_in_solar_sector=True)

        # floor = min(glare_min 100, light 80) = 80 → 90 above → shades; 70 below → no.
        assert evaluator.evaluate(_wdi_exp(90.0)) is not None
        assert evaluator.evaluate(_wdi_exp(70.0)) is None

    def test_floor_follows_configured_glare_min_exposure(self, evaluator: HeatEvaluator):
        # The heat floor reuses the user-configurable glare minimum exposure
        # ("Minimum exposure for glare protection"), not a hidden constant.
        from custom_components.smartshading.models.window_decision_input import WindowDecisionInput
        from custom_components.smartshading.models.window import WindowConfig as WC
        from custom_components.smartshading.models.zone import ZoneConfig as ZC
        w = WC(id="w", name="W", zone_id="z", azimuth=0.0, floor_level=0, cover_group_id="c")
        z = ZC(id="z", name="Z")

        def _eval(glare_min, effective):
            b = BehaviorConfig(heat_outdoor_threshold_c=26.0, heat_indoor_threshold_c=None,
                               glare_min_exposure_wm2=glare_min,
                               light_shade_threshold_wm2=400.0, normal_shade_position=75)
            wdi = WindowDecisionInput(
                window_config=w, zone_config=z, effective_behavior=b,
                lifecycle_state=LifecycleState.DAY, absence_active=False,
                current_shading_state=ShadingState.OPEN, outdoor_temp_c=30.0,
                indoor_temp_c=None,
                exposure=_types.SimpleNamespace(effective_exposure=effective),
                is_in_solar_sector=True)
            return evaluator.evaluate(wdi)

        # Raise the configured glare minimum to 200 → heat now needs >= 200.
        assert _eval(200.0, 150.0) is None       # 150 < 200 → no heat shade
        assert _eval(200.0, 220.0) is not None    # 220 >= 200 → shades
        # Lower it to 50 → heat fires from just 70 W/m².
        assert _eval(50.0, 70.0) is not None

    def test_east_window_afternoon_west_sun_no_shade(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Real-world case: east-facing window, sun in western sector (afternoon).
        is_in_solar_sector=False → gate returns None."""
        east_window = WindowConfig(
            id="w-east", name="East", zone_id="z1",
            azimuth=90.0, floor_level=0, cover_group_id="cg-east",
        )
        wdi = _wdi(east_window, zone, outdoor_temp_c=33.0, indoor_temp_c=None,
                   is_in_solar_sector=False, exposure=None)
        assert evaluator.evaluate(wdi) is None

    def test_gate_does_not_block_when_heat_thresholds_already_disabled(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """When heat protection is disabled (thresholds=None), the early-exit
        already returns None before the sector gate is reached."""
        wdi = _wdi(window, zone, outdoor_temp_c=40.0, indoor_temp_c=35.0,
                   heat_protection_enabled=False,
                   is_in_solar_sector=False, exposure=None)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Entry/exit hysteresis (v1.2.0-beta.1, T9)
# ---------------------------------------------------------------------------

class TestHeatEvaluatorHysteresis:
    """HeatEvaluator delegates to engines.heat_hysteresis.resolve_heat_needed()
    — these tests prove the delegation is wired correctly end-to-end through
    the real evaluator + WindowDecisionInput, not just the pure function in
    isolation (see tests/test_heat_hysteresis.py for the exhaustive pure-
    function matrix)."""

    def test_default_wdi_reproduces_legacy_exact_threshold(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """heat_previously_active defaults to False — a WDI built exactly
        like every pre-T9 call site fires only at/above the entry threshold,
        unchanged."""
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=23.9)
        assert evaluator.evaluate(wdi) is None

    def test_value_between_entry_and_exit_stays_shaded_when_previously_active(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Ticket example: entry 24.0, exit 23.0 (hysteresis 1.0), 23.6 held.
        wdi = _wdi(
            window, zone, outdoor_temp_c=None, indoor_temp_c=23.6,
            heat_previously_active=True,
        )
        decision = evaluator.evaluate(wdi)
        assert decision is not None
        assert decision.shading_state is ShadingState.NORMAL_SHADE

    def test_value_between_entry_and_exit_does_not_fire_when_not_previously_active(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, outdoor_temp_c=None, indoor_temp_c=23.6,
            heat_previously_active=False,
        )
        assert evaluator.evaluate(wdi) is None

    def test_value_below_exit_releases_even_if_previously_active(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, outdoor_temp_c=None, indoor_temp_c=22.9,
            heat_outdoor_threshold_c=None, heat_previously_active=True,
        )
        assert evaluator.evaluate(wdi) is None

    def test_zero_hysteresis_disables_the_band(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, outdoor_temp_c=None, indoor_temp_c=23.9,
            heat_outdoor_threshold_c=None,
            heat_hysteresis_c=0.0, heat_previously_active=True,
        )
        assert evaluator.evaluate(wdi) is None

    def test_sector_gate_still_applies_while_hysteresis_active(
        self, evaluator: HeatEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The sector/exposure gates are NOT hysteretic — they remain
        unconditional immediate suppressors even while the thermal
        hysteresis state is active."""
        wdi = _wdi(
            window, zone, outdoor_temp_c=None, indoor_temp_c=23.6,
            heat_previously_active=True, is_in_solar_sector=False,
        )
        assert evaluator.evaluate(wdi) is None
