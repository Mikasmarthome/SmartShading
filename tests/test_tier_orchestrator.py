"""Tests for TierOrchestrator.

Contract:
  - Tier 3 (NightEvaluator) fires first and causes an early exit.
    No Tier 4 / Tier 5 result overrides a Night decision.
  - Tier 4 Protection Floors (Absence, Heat, Glare) all run; PositionResolver
    picks the most-shaded winner.
  - Tier 5 (Solar) also contributes; PositionResolver arbitrates across all tiers.
  - Absence alone works.
  - Heat alone works (outdoor OR indoor threshold exceeded).
  - Glare alone works (sun in sector, enabled).
  - Solar alone works (when heat/glare disabled to isolate Tier 5).
  - Combined scenarios: higher position always wins.
  - All evaluators return None → fallback OPEN is returned.
  - Fallback OPEN has target_position=0 and decided_by="TierOrchestrator:fallback".
  - evaluate_window() always returns a WindowDecision, never None.
  - INV-18: no config resolution or HA access inside the orchestrator.

Helper note on isolation tests:
  When testing SolarEvaluator in isolation (TestTierOrchestratorSolarOnly),
  pass ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False)
  to prevent HeatEvaluator / GlareEvaluator from co-firing.  This matches
  the real production pattern: comfort config is always pre-resolved into
  BehaviorConfig before the evaluators run (INV-18).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.smartshading.engines.exposure_engine import WindowExposure
from custom_components.smartshading.engines.weather_engine import WeatherCondition
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState

# Sentinel: all comfort goals disabled — used to isolate individual evaluators.
_NO_COMFORT = ComfortConfig(
    heat_protection_enabled=False,
    glare_protection_enabled=False,
    solar_gain_enabled=False,
)
# Sentinel: only heat active (no glare) — for heat isolation tests.
_HEAT_ONLY = ComfortConfig(
    heat_protection_enabled=True,
    glare_protection_enabled=False,
    solar_gain_enabled=False,
)
# Sentinel: only glare active (no heat) — for glare isolation tests.
_GLARE_ONLY = ComfortConfig(
    heat_protection_enabled=False,
    glare_protection_enabled=True,
    solar_gain_enabled=False,
)


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)


@pytest.fixture()
def orchestrator() -> TierOrchestrator:
    return TierOrchestrator()


@pytest.fixture()
def window() -> WindowConfig:
    return WindowConfig(
        id="w-south", name="South", zone_id="z1",
        azimuth=180.0, floor_level=0, cover_group_id="cg-south",
    )


@pytest.fixture()
def zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Living")


def _exposure(wm2: float) -> WindowExposure:
    return WindowExposure(
        window_id="w-south",
        timestamp=_NOW,
        sun_azimuth=180.0, sun_elevation=45.0,
        is_above_horizon=True, is_in_tolerance_window=True,
        azimuth_delta_deg=0.0, direct_radiation_factor=1.0,
        elevation_clipped=False,
        theoretical_exposure=wm2, learned_solar_impact_factor=1.0,
        seasonal_factor=1.0, effective_exposure=wm2,
    )


def _wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    lifecycle_state: LifecycleState = LifecycleState.DAY,
    absence_active: bool = False,
    is_in_solar_sector: bool = False,
    exposure_wm2: float | None = None,
    outdoor_temp_c: float | None = None,
    indoor_temp_c: float | None = None,
    # HA-convention positions (converted to internal by builder)
    night_position_ha: int = 0,          # HA 0 → internal 100 (fully shaded)
    night_shading_enabled: bool = True,
    morning_position_ha: int = 100,      # HA 100 → internal 0 (fully open)
    morning_shading_enabled: bool = True,
    absence_position_ha: int = 30,        # HA 30 → internal 70
    absence_shading_enabled: bool = True,
    light_shade_ha: int = 40,             # HA 40 → internal 60
    normal_shade_ha: int = 25,            # HA 25 → internal 75
    strong_shade_ha: int = 10,            # HA 10 → internal 90
    comfort_config: ComfortConfig | None = None,
    presence_uncertain: bool = False,
    current_shading_state: ShadingState = ShadingState.OPEN,
    current_position_internal: int | None = None,
):
    return build_window_decision_input(
        window=window,
        zone=zone,
        global_defaults=GlobalDefaults(
            night_shading_enabled=night_shading_enabled,
            absence_shading_enabled=absence_shading_enabled,
            absence_position=absence_position_ha,
        ),
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=light_shade_ha,
            normal_shade_position=normal_shade_ha,
            strong_shade_position=strong_shade_ha,
        ),
        lifecycle_config=NightDayLifecycleConfig(
            id="default",
            night_position=night_position_ha,
            night_enabled=True,
            morning_position=morning_position_ha,
            morning_enabled=morning_shading_enabled,
        ),
        lifecycle_state=lifecycle_state,
        absence_active=absence_active,
        current_shading_state=current_shading_state,
        outdoor_temp_c=outdoor_temp_c,
        indoor_temp_c=indoor_temp_c,
        exposure=_exposure(exposure_wm2) if exposure_wm2 is not None else None,
        is_in_solar_sector=is_in_solar_sector,
        comfort_config=comfort_config,
        presence_uncertain=presence_uncertain,
        current_position_internal=current_position_internal,
    )


# ---------------------------------------------------------------------------
# Tier 3 (Night) — early exit
# ---------------------------------------------------------------------------

class TestTierOrchestratorNightEarlyExit:
    def test_night_returns_night_closed(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, lifecycle_state=LifecycleState.NIGHT)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NIGHT_CLOSED

    def test_night_decided_by_night_evaluator(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, lifecycle_state=LifecycleState.NIGHT)
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "NightEvaluator"

    def test_night_early_exit_ignores_absence(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Night fires first; AbsenceEvaluator must not override it."""
        wdi = _wdi(
            window, zone,
            lifecycle_state=LifecycleState.NIGHT,
            absence_active=True,       # Tier 4 active — must be ignored
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NIGHT_CLOSED
        assert result.decided_by == "NightEvaluator"

    def test_night_early_exit_ignores_solar(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Night fires first; SolarEvaluator must not override it."""
        wdi = _wdi(
            window, zone,
            lifecycle_state=LifecycleState.NIGHT,
            is_in_solar_sector=True,
            exposure_wm2=800.0,        # Tier 5 active — must be ignored
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NIGHT_CLOSED

    def test_night_early_exit_ignores_both_lower_tiers(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """All three tiers active — Night wins."""
        wdi = _wdi(
            window, zone,
            lifecycle_state=LifecycleState.NIGHT,
            absence_active=True,
            is_in_solar_sector=True,
            exposure_wm2=800.0,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NIGHT_CLOSED

    def test_night_uses_night_position_from_behavior(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # HA night_position=0 → internal 100 (fully shaded)
        wdi = _wdi(window, zone, lifecycle_state=LifecycleState.NIGHT, night_position_ha=0)
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 100

    def test_night_disabled_does_not_early_exit(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """If night shading is disabled, Night does not fire → fall through."""
        wdi = _wdi(
            window, zone,
            lifecycle_state=LifecycleState.NIGHT,
            night_shading_enabled=False,
        )
        result = orchestrator.evaluate_window(wdi)
        # No tier fires → fallback OPEN
        assert result.shading_state is ShadingState.OPEN
        assert result.decided_by == "TierOrchestrator:fallback"


# ---------------------------------------------------------------------------
# Tier 4 (Absence) only
# ---------------------------------------------------------------------------

class TestTierOrchestratorAbsenceOnly:
    def test_absence_active_returns_absence_closed(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=True)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.ABSENCE_CLOSED

    def test_absence_decided_by_absence_evaluator(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=True)
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "AbsenceEvaluator"

    def test_absence_target_position_is_correct(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # absence_position_ha=30 → internal 70
        wdi = _wdi(window, zone, absence_active=True, absence_position_ha=30)
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 70

    def test_absence_not_active_falls_through(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=False)
        result = orchestrator.evaluate_window(wdi)
        # No Tier 5 either → fallback
        assert result.shading_state is ShadingState.OPEN


# ---------------------------------------------------------------------------
# Tier 5 (Solar) only
# ---------------------------------------------------------------------------

class TestTierOrchestratorSolarOnly:
    """Test SolarEvaluator in isolation.

    Heat and Glare are explicitly disabled via _NO_COMFORT so that only the
    Tier 5 SolarEvaluator can fire.  This is a valid production scenario
    (user has comfort goals off) and also a clean isolation test.
    """

    def test_light_shade_from_solar(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=200.0,
                   comfort_config=_NO_COMFORT)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.LIGHT_SHADE
        assert result.decided_by == "SolarEvaluator"

    def test_normal_shade_from_solar(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=350.0,
                   comfort_config=_NO_COMFORT)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NORMAL_SHADE
        assert result.decided_by == "SolarEvaluator"

    def test_strong_shade_from_solar(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=600.0,
                   comfort_config=_NO_COMFORT)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.STRONG_SHADE
        assert result.decided_by == "SolarEvaluator"

    def test_solar_not_in_sector_falls_through(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=False, exposure_wm2=800.0,
                   comfort_config=_NO_COMFORT)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN


# ---------------------------------------------------------------------------
# Tier 4 + Tier 5 arbitration
# ---------------------------------------------------------------------------

class TestTierOrchestratorArbitration:
    def test_solar_higher_than_absence_solar_wins(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Absence floor=70 (HA 30), Solar normal=75 (HA 25) → Solar wins."""
        wdi = _wdi(
            window, zone,
            absence_active=True, absence_position_ha=30,   # internal 70
            is_in_solar_sector=True, exposure_wm2=350.0,   # NORMAL_SHADE → internal 75
            normal_shade_ha=25,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 75
        assert result.decided_by == "SolarEvaluator"

    def test_absence_higher_than_solar_absence_wins(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Absence floor=80 (HA 20), Solar light=60 (HA 40) → Absence wins."""
        wdi = _wdi(
            window, zone,
            absence_active=True, absence_position_ha=20,   # internal 80
            is_in_solar_sector=True, exposure_wm2=200.0,   # LIGHT_SHADE → internal 60
            light_shade_ha=40,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 80
        assert result.decided_by == "AbsenceEvaluator"

    def test_absence_equals_solar_absence_is_first(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Tie: Absence listed before Solar in Tier 4 results → Absence wins (first-wins)."""
        # Both resolve to internal 75
        wdi = _wdi(
            window, zone,
            absence_active=True, absence_position_ha=25,   # internal 75
            is_in_solar_sector=True, exposure_wm2=350.0,   # NORMAL_SHADE → internal 75
            normal_shade_ha=25,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 75
        # Absence is first in tier4_results → wins on tie
        assert result.decided_by == "AbsenceEvaluator"

    def test_strong_solar_beats_high_absence_floor(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Strong shade (internal 90) beats absence floor (internal 70)."""
        wdi = _wdi(
            window, zone,
            absence_active=True, absence_position_ha=30,   # internal 70
            is_in_solar_sector=True, exposure_wm2=600.0,   # STRONG_SHADE → internal 90
            strong_shade_ha=10,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 90
        assert result.decided_by == "SolarEvaluator"


# ---------------------------------------------------------------------------
# Fallback OPEN
# ---------------------------------------------------------------------------

class TestTierOrchestratorFallback:
    def test_no_evaluator_active_returns_open(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone)  # DAY, no absence, no solar sector
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN

    def test_fallback_decided_by(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone)
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "TierOrchestrator:fallback"

    def test_fallback_category_is_comfort(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """T7: the daytime fallback OPEN (nothing needs protection/comfort
        shading) is tagged COMFORT — gated by allow_comfort when an
        override is active, same as SolarEvaluator's regular comfort
        decisions."""
        wdi = _wdi(window, zone)
        result = orchestrator.evaluate_window(wdi)
        assert result.category is DecisionCategory.COMFORT

    def test_fallback_target_position_is_zero(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Fallback OPEN uses internal position 0 (fully open)."""
        wdi = _wdi(window, zone)
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 0

    def test_fallback_window_id_is_correct(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone)
        result = orchestrator.evaluate_window(wdi)
        assert result.window_id == window.id

    def test_evaluate_window_never_returns_none(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """evaluate_window() must always return a concrete WindowDecision."""
        for lifecycle_state in (LifecycleState.DAY, LifecycleState.MORNING, LifecycleState.EVENING):
            wdi = _wdi(window, zone, lifecycle_state=lifecycle_state)
            result = orchestrator.evaluate_window(wdi)
            assert result is not None, f"Got None for lifecycle_state={lifecycle_state}"


# ---------------------------------------------------------------------------
# INV-18 and scope boundary
# ---------------------------------------------------------------------------

class TestTierOrchestratorInvariants:
    def test_window_id_always_matches_input(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """window_id in the result must always match the WDI's window_config.id."""
        for state in (LifecycleState.NIGHT, LifecycleState.DAY):
            wdi = _wdi(window, zone, lifecycle_state=state)
            result = orchestrator.evaluate_window(wdi)
            assert result.window_id == window.id

    def test_target_tilt_always_none_in_mvp(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """target_tilt is Phase 2 only — always None in MVP."""
        scenarios = [
            _wdi(window, zone, lifecycle_state=LifecycleState.NIGHT),
            _wdi(window, zone, absence_active=True),
            _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=300.0,
                 comfort_config=_NO_COMFORT),
            _wdi(window, zone),
        ]
        for wdi in scenarios:
            result = orchestrator.evaluate_window(wdi)
            assert result.target_tilt is None, (
                f"target_tilt should be None, got {result.target_tilt} "
                f"for decided_by={result.decided_by}"
            )


# ---------------------------------------------------------------------------
# Tier 4: Heat Protection
# ---------------------------------------------------------------------------

class TestTierOrchestratorHeatOnly:
    """HeatEvaluator in isolation (no glare, no solar, no absence)."""

    def test_outdoor_threshold_exceeded_returns_normal_shade(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=27.0, comfort_config=_HEAT_ONLY,
                   is_in_solar_sector=True)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NORMAL_SHADE
        assert result.decided_by == "HeatEvaluator"

    def test_indoor_threshold_exceeded_returns_normal_shade(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, indoor_temp_c=25.0, comfort_config=_HEAT_ONLY,
                   is_in_solar_sector=True)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NORMAL_SHADE
        assert result.decided_by == "HeatEvaluator"

    def test_both_thresholds_exceeded_returns_normal_shade(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=30.0, indoor_temp_c=26.0,
                   comfort_config=_HEAT_ONLY, is_in_solar_sector=True)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NORMAL_SHADE

    def test_below_threshold_falls_through(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=20.0, indoor_temp_c=22.0,
                   comfort_config=_HEAT_ONLY)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN

    def test_none_temp_does_not_trigger_heat(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Missing sensor → no false-positive heat trigger (fail-safe)."""
        wdi = _wdi(window, zone, outdoor_temp_c=None, indoor_temp_c=None,
                   comfort_config=_HEAT_ONLY)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN

    def test_heat_disabled_does_not_fire(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, outdoor_temp_c=35.0,
                   comfort_config=ComfortConfig(heat_protection_enabled=False))
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN

    def test_heat_uses_normal_shade_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # normal_shade_ha=25 → internal 75
        wdi = _wdi(window, zone, outdoor_temp_c=27.0, normal_shade_ha=25,
                   comfort_config=_HEAT_ONLY, is_in_solar_sector=True)
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 75  # 100 - 25


# ---------------------------------------------------------------------------
# Tier 4: Glare Protection
# ---------------------------------------------------------------------------

class TestTierOrchestratorGlareOnly:
    """GlareEvaluator in isolation (no heat, no solar threshold, no absence)."""

    def test_in_sector_with_exposure_returns_light_shade(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # In sector AND meaningfully lit (120 >= glare_min 100, < solar light 150).
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=120.0,
                   comfort_config=_GLARE_ONLY)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.LIGHT_SHADE
        assert result.decided_by == "GlareEvaluator"

    def test_not_in_sector_falls_through(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=False, comfort_config=_GLARE_ONLY)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN

    def test_glare_does_not_fire_without_exposure(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Geometry alone is not enough: no exposure → glare suppressed → OPEN."""
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=None,
                   comfort_config=_GLARE_ONLY)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN

    def test_glare_suppressed_below_min_exposure(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Real case: in sector but only ~66.7 W/m² (< glare_min 100) → OPEN."""
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=66.7,
                   comfort_config=_GLARE_ONLY)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN

    def test_glare_fires_below_solar_entry_threshold(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Sun in sector at 120 W/m² (>= glare_min 100, < 150 solar): Glare fires, Solar doesn't."""
        wdi = _wdi(window, zone, is_in_solar_sector=True, exposure_wm2=120.0,
                   comfort_config=_GLARE_ONLY)
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.LIGHT_SHADE
        assert result.decided_by == "GlareEvaluator"

    def test_glare_disabled_does_not_fire(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, is_in_solar_sector=True,
                   comfort_config=ComfortConfig(glare_protection_enabled=False))
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN

    def test_glare_uses_light_shade_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # light_shade_ha=40 → internal 60
        wdi = _wdi(window, zone, is_in_solar_sector=True, light_shade_ha=40,
                   exposure_wm2=120.0, comfort_config=_GLARE_ONLY)
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 60  # 100 - 40


# ---------------------------------------------------------------------------
# Heat + Glare + Solar arbitration
# ---------------------------------------------------------------------------

class TestTierOrchestratorHeatGlareArbitration:
    """Combined scenarios: Heat (NORMAL_SHADE=75), Glare (LIGHT_SHADE=60),
    Solar (various), Absence (various) interacting via PositionResolver."""

    def test_heat_beats_glare(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Heat floor (75) > Glare floor (60) → Heat wins."""
        wdi = _wdi(
            window, zone,
            is_in_solar_sector=True, outdoor_temp_c=27.0,
            normal_shade_ha=25,   # internal 75
            light_shade_ha=40,    # internal 60
            comfort_config=ComfortConfig(
                heat_protection_enabled=True,
                glare_protection_enabled=True,
            ),
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 75
        assert result.decided_by == "HeatEvaluator"

    def test_solar_strong_beats_heat_and_glare(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Solar STRONG (90) > Heat (75) > Glare (60) → Solar wins."""
        wdi = _wdi(
            window, zone,
            is_in_solar_sector=True, exposure_wm2=600.0,
            outdoor_temp_c=27.0,
            strong_shade_ha=10,   # internal 90
            normal_shade_ha=25,   # internal 75
            light_shade_ha=40,    # internal 60
            comfort_config=ComfortConfig(
                heat_protection_enabled=True,
                glare_protection_enabled=True,
            ),
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 90
        assert result.decided_by == "SolarEvaluator"

    def test_absence_plus_heat_absence_higher(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Absence floor (80) > Heat floor (75) → Absence wins."""
        wdi = _wdi(
            window, zone,
            absence_active=True, absence_position_ha=20,  # internal 80
            outdoor_temp_c=27.0,
            normal_shade_ha=25,   # internal 75
            comfort_config=_HEAT_ONLY,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.target_position == 80
        assert result.decided_by == "AbsenceEvaluator"

    def test_night_beats_heat_and_glare(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Night early-exit: even with heat+glare active, Night wins."""
        wdi = _wdi(
            window, zone,
            lifecycle_state=LifecycleState.NIGHT,
            is_in_solar_sector=True, outdoor_temp_c=30.0,
            comfort_config=ComfortConfig(
                heat_protection_enabled=True,
                glare_protection_enabled=True,
            ),
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.NIGHT_CLOSED
        assert result.decided_by == "NightEvaluator"

    def test_all_comfort_disabled_returns_open(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """All comfort goals off, no solar sector → fallback OPEN."""
        wdi = _wdi(
            window, zone,
            outdoor_temp_c=35.0, indoor_temp_c=28.0,
            is_in_solar_sector=True,
            comfort_config=_NO_COMFORT,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN


# ---------------------------------------------------------------------------
# Tier 1 (Safety) vs. Tier 4/5 (Comfort) — F21 audit follow-up.
#
# F21's Safety/Gating audit found the priority ordering (Tier 1 evaluated,
# and returned on, before any Tier 4/5 comfort/learning-adapted target is
# ever read — coordinator.py injects experiment/adoption/strategy deltas
# only into wdi.effective_behavior's comfort-tier fields, never into the
# Tier 1 inputs) architecturally sound from static reading, but flagged a
# genuine test gap: no test proves this live, in combination, at the real
# orchestrator level — every existing safety test exercises Tier 1 in
# isolation, and every comfort/learning test exercises Tier 4/5 without an
# active Tier 1 condition. This class closes exactly that gap: it builds a
# WDI where BOTH a live wind-safety trigger AND an aggressively-adapted
# comfort-tier target (as a learned/adopted position swing would produce)
# are present in the SAME evaluation, and proves Tier 1 wins regardless of
# how the comfort-tier thresholds/positions were adapted.
# ---------------------------------------------------------------------------

class TestTierOrchestratorSafetyOverridesAdaptedComfortTarget:
    def test_wind_safe_wins_over_aggressively_adapted_solar_target(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Wind above the default 14.0 m/s threshold — Tier 1 must fire.
        # Comfort-tier (Tier 5 Solar) inputs are set exactly as an aggressive
        # learned/adopted adaptation would leave them: full sun exposure,
        # in-sector, and shade positions pushed to their most-shaded extreme
        # (light/normal/strong all deep-closed) — the kind of target a
        # position adoption's -10 delta could produce. If Tier 1 priority
        # were ever bypassed by an adapted comfort target, this scenario
        # would surface it as a non-WIND_SAFE result.
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(
                light_shade_position=5, normal_shade_position=2, strong_shade_position=0,
            ),
            lifecycle_config=NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=32.0, indoor_temp_c=29.0,
            exposure=_exposure(900.0),
            is_in_solar_sector=True,
            # 16.0 m/s: above the 14.0 wind-protection threshold but below
            # the 20.0 m/s storm threshold (DEFAULT_STORM_WIND_THRESHOLD_MS
            # in engines/weather_engine.py) — isolates WindEvaluator from
            # StormEvaluator, which would otherwise also fire and mask
            # whether WindEvaluator itself actually won on priority.
            wind_speed_ms=16.0, wind_protection_enabled=True, wind_threshold_ms=14.0,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.WIND_SAFE
        assert result.decided_by == "WindEvaluator"

    def test_wind_below_threshold_lets_adapted_solar_target_through(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # Control case: identical adapted comfort-tier scenario, but wind
        # below the threshold — Tier 1 must NOT fire, and the comfort tier's
        # (adapted) result is what actually decides. Confirms the safety
        # check above isn't trivially true because comfort never fires at
        # all in this WDI shape.
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(
                light_shade_position=5, normal_shade_position=2, strong_shade_position=0,
            ),
            lifecycle_config=NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=32.0, indoor_temp_c=29.0,
            exposure=_exposure(900.0),
            is_in_solar_sector=True,
            wind_speed_ms=2.0, wind_protection_enabled=True, wind_threshold_ms=14.0,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is not ShadingState.WIND_SAFE
        assert result.shading_state is not ShadingState.STORM_SAFE
        # The comfort tier (here: SolarEvaluator, given full sun exposure and
        # in-sector) is what actually decided — proving the prior test's
        # WIND_SAFE result came from real Tier 1 priority, not from comfort
        # never firing at all in this WDI shape.
        assert result.decided_by not in ("TierOrchestrator:fallback", "WindEvaluator", "StormEvaluator")


class TestTierOrchestratorAdoptionCannotOverrideSafetyOrManualOverride:
    """F8: position adoption (coordinator._adoption_apply) only ever rewrites
    wdi.effective_behavior.{light,normal,strong}_shade_position — the same
    fields exercised above as an "aggressively adopted" comfort target. This
    class extends that proof to STORM_SAFE and MANUAL_OVERRIDE (the WIND_SAFE
    case is already covered above), closing the F8 audit gap: no existing
    test previously proved adoption cannot override Safety or Manual
    Override specifically.
    """

    def test_storm_safe_wins_over_adopted_comfort_target(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            # Deep-closed shade positions — the shape _adoption_apply would
            # leave after a maximal -10pp adoption delta.
            shade_position_defaults=ShadePositionDefaults(
                light_shade_position=5, normal_shade_position=2, strong_shade_position=0,
            ),
            lifecycle_config=NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=32.0, indoor_temp_c=29.0,
            exposure=_exposure(900.0),
            is_in_solar_sector=True,
            weather_condition=WeatherCondition.STORM,
            storm_protection_enabled=True,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.STORM_SAFE
        assert result.decided_by == "StormEvaluator"

    def test_manual_override_wins_over_adopted_comfort_target(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = ManualOverride(
            window_id=window.id, override_position=60,
            started_at=_NOW, expires_at=_NOW.replace(hour=18),
            source="position_delta", overridden_state=ShadingState.NORMAL_SHADE,
            overridden_position=75,
        )
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(
                light_shade_position=5, normal_shade_position=2, strong_shade_position=0,
            ),
            lifecycle_config=NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=32.0, indoor_temp_c=29.0,
            exposure=_exposure(900.0),
            is_in_solar_sector=True,
            active_override=override,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.target_position == override.override_position
        assert result.decided_by == "ManualOverrideEvaluator"


class TestManualOverrideVsAbsencePriorityIsIntentionalDesign:
    """Real-world bug report (ABSENCE_AND_SCHEDULE terrace-door window):
    "should Absence-Close rank above Manual Override, similar to Safety?"
    Audit finding: Manual Override (Tier 2) intentionally outranks
    Absence-close (Tier 4) — a genuine user override (e.g. the user just
    opened the cover by hand) must not be immediately undone by an automatic
    absence-close. This is deliberate UX design, not a bug, and is
    independent from the separate, real false-positive DETECTION issue
    fixed alongside this audit (OverrideDetector settle-window guard,
    tests/test_v10_override_fix.py). This test documents the priority
    itself as intentional so a future change here is a conscious decision,
    not an accidental regression.
    """

    def test_manual_override_wins_over_absence_close(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = ManualOverride(
            window_id=window.id, override_position=20,
            started_at=_NOW, expires_at=_NOW.replace(hour=18),
            source="position_delta", overridden_state=ShadingState.OPEN,
            overridden_position=80,
        )
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True),
            lifecycle_state=LifecycleState.DAY,
            absence_active=True,  # zone is in absence — would normally close
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=22.0, indoor_temp_c=21.0,
            exposure=_exposure(0.0),
            is_in_solar_sector=False,
            active_override=override,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.decided_by == "ManualOverrideEvaluator"

    def test_absence_close_applies_when_no_override_is_active(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Control: with no active override, absence-close fires normally —
        confirms the previous test's result is due to the override, not an
        unrelated config difference."""
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True),
            lifecycle_state=LifecycleState.DAY,
            absence_active=True,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=22.0, indoor_temp_c=21.0,
            exposure=_exposure(0.0),
            is_in_solar_sector=False,
            active_override=None,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.ABSENCE_CLOSED
        assert result.decided_by == "AbsenceEvaluator"


# ---------------------------------------------------------------------------
# Tier 3 (cont.): Morning — v1.2.0-beta.3, B3-010
#
# Unlike Night, Morning is NOT an early exit: it must be a candidate inside
# the SAME PositionResolver.resolve() call as Tier 4/5, so a real Heat/Glare/
# Absence floor or an already-required Solar shade wins over morning_position
# automatically via the existing max(target_position) rule — no separate
# arbitration path, no early return, no intermediate full-open dispatch.
# ---------------------------------------------------------------------------

class TestTierOrchestratorMorningNoProtectionNeeded:
    def test_morning_alone_returns_open_at_morning_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            morning_position_ha=100, comfort_config=_NO_COMFORT,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN
        assert result.decided_by == "MorningEvaluator"
        assert result.target_position == 0  # HA 100 -> internal 0 (fully open)

    def test_morning_partial_position_wins_over_plain_fallback(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """A non-fully-open configured morning_position must be used exactly
        as configured, not silently replaced by the generic OPEN=0 fallback."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            morning_position_ha=70, comfort_config=_NO_COMFORT,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "MorningEvaluator"
        assert result.target_position == 30  # HA 70 -> internal 30

    def test_morning_disabled_falls_through_to_plain_fallback(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Regression guard for the pre-B3-010 behavior: with morning
        explicitly disabled, MORNING state still resolves to the generic
        fallback OPEN, exactly as before this change."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            morning_shading_enabled=False, comfort_config=_NO_COMFORT,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN
        assert result.decided_by == "TierOrchestrator:fallback"
        assert result.target_position == 0

    def test_morning_category_is_lifecycle_end_to_end(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            comfort_config=_NO_COMFORT,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.category is DecisionCategory.LIFECYCLE


class TestTierOrchestratorMorningVsProtectionArbitration:
    """B3-010 requirement 5: if protection is already required at the exact
    morning transition, its final protective position is reached directly —
    never an intermediate full-open-then-reshade sequence."""

    def test_heat_already_required_wins_over_morning_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            morning_position_ha=100,  # would otherwise fully open
            outdoor_temp_c=30.0, comfort_config=_HEAT_ONLY,
            is_in_solar_sector=True,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "HeatEvaluator"
        assert result.shading_state is not ShadingState.OPEN
        # Exactly one decision reached this cycle -- no intermediate OPEN.
        assert result.target_position > 0

    def test_glare_already_required_wins_over_morning_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            morning_position_ha=100,
            is_in_solar_sector=True, exposure_wm2=120.0, comfort_config=_GLARE_ONLY,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "GlareEvaluator"
        assert result.target_position > 0

    def test_absence_already_active_wins_over_morning_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            morning_position_ha=100,
            absence_active=True, comfort_config=_NO_COMFORT,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "AbsenceEvaluator"
        assert result.shading_state is ShadingState.ABSENCE_CLOSED

    def test_strong_solar_shade_already_required_wins_over_morning_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Solar's regular Tier 5 comfort shading (not just Tier 4 Glare
        protection) also correctly wins when it wants more shade than
        morning_position -- prevents the exact double-movement bug B3-010
        forbids (open fully, then reshade moments later for direct sun)."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            morning_position_ha=100,  # internal 0
            is_in_solar_sector=True, exposure_wm2=800.0,
            comfort_config=_NO_COMFORT,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "SolarEvaluator"
        assert result.target_position == 90  # strong_shade_ha=10 -> internal 90

    def test_morning_position_wins_when_more_shaded_than_mild_solar(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The reverse: a mild Solar candidate that wants LESS shade than
        morning_position must not override the user's configured morning
        position -- max(target_position) picks morning_position here."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            morning_position_ha=30,  # internal 70 -- fairly shaded morning position
            is_in_solar_sector=True, exposure_wm2=50.0,  # below any shade threshold
            comfort_config=_NO_COMFORT,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "MorningEvaluator"
        assert result.target_position == 70

    def test_night_hard_hold_style_no_double_dispatch_in_single_evaluation(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """A single evaluate_window() call for a MORNING cycle with active
        heat protection returns exactly ONE decision -- proving there is no
        separate "open first" step anywhere in this call."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            outdoor_temp_c=30.0, comfort_config=_HEAT_ONLY,
            is_in_solar_sector=True,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result is not None
        assert result.shading_state is not ShadingState.OPEN


class TestTierOrchestratorMorningPresenceUncertain:
    """B3-010: presence-uncertain must take priority over Morning, exactly
    like it already takes priority over the plain daytime fallback OPEN --
    morning_position typically opens the cover, and an unconfirmed presence
    reading is precisely when guessing "not absent" is riskiest."""

    def test_presence_uncertain_holds_instead_of_morning_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            comfort_config=_NO_COMFORT, presence_uncertain=True,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "PresenceUncertain:hold"
        assert result.target_position is None

    def test_presence_uncertain_does_not_block_a_real_protection_winner(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """presence_uncertain only removes Morning from the candidate pool --
        a genuine Heat/Glare/Absence floor still applies normally."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            outdoor_temp_c=30.0, comfort_config=_HEAT_ONLY, presence_uncertain=True,
            is_in_solar_sector=True,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "HeatEvaluator"

    def test_presence_known_uses_morning_position_normally(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Control: presence_uncertain=False (the default) -> Morning applies
        normally, confirming the prior test's hold was due to the uncertain
        flag specifically."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            comfort_config=_NO_COMFORT, presence_uncertain=False,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "MorningEvaluator"

    def test_numeric_ha_50_to_70_is_opening_and_stays_held(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The exact counter-example from the correction ticket: real
        position HA 50 (internal 50), Morning target HA 70 (internal 30).
        internal 30 < internal 50 -- the cover would numerically OPEN
        further (30 is LESS shaded than 50), even though both HA 50 and
        HA 70 share the coarse ShadingState.OPEN label. Must be held --
        the old current_shading_state-is-OPEN heuristic got this case
        wrong (it only checked target_position > 0, which is true here,
        and would have incorrectly treated this as closing)."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            comfort_config=_NO_COMFORT, presence_uncertain=True,
            morning_position_ha=70, current_position_internal=50,  # HA 50 -> internal 50
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "PresenceUncertain:hold", (
            "HA 50 -> HA 70 is numerically OPENING (internal 50 -> 30), "
            "must be held even though the coarse state label is the same "
            "OPEN on both sides"
        )
        assert result.target_position is None

    def test_numeric_ha_70_to_70_is_identical_and_held(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Identical position (HA 70 -> HA 70, internal 30 -> 30): not a
        closing move (target > current is False when equal) -- held, no
        spurious dispatch either way."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            comfort_config=_NO_COMFORT, presence_uncertain=True,
            morning_position_ha=70, current_position_internal=30,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "PresenceUncertain:hold"

    def test_closing_direction_is_not_suppressed_real_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """B3-010 correction requirement 4 (numeric direction-aware fix): a
        Morning target that shades the window MORE than its real current
        position (internal target > internal current) is a genuinely
        CLOSING, more protective move and must NOT be suppressed while
        presence is uncertain -- suppressing it would deny a legitimate
        protective improvement. Uses wdi.current_position_internal, the
        Coordinator-resolved numeric position, not the coarse
        ShadingState.OPEN label."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            comfort_config=_NO_COMFORT, presence_uncertain=True,
            morning_position_ha=70, current_position_internal=0,  # HA 100 -> internal 0
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "MorningEvaluator", (
            "a closing move away from the current position must apply "
            "even while presence is uncertain"
        )
        assert result.target_position == 30

    def test_unknown_position_is_the_conservative_unknown_path(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """current_position_internal=None (genuinely unknown/untrustworthy,
        e.g. AssumedStateManager.is_position_trustworthy() was False) is the
        explicit conservative Unknown-path -- never a numeric guess, always
        held, exactly like the opening case."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            comfort_config=_NO_COMFORT, presence_uncertain=True,
            morning_position_ha=70, current_position_internal=None,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "PresenceUncertain:hold"
        assert result.target_position is None

    def test_concurrent_heat_protection_still_wins_regardless_of_direction(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The direction-aware Morning gate must not interfere with a
        genuinely active Heat/Solar/Absence protective tier, which is
        entirely unaffected by presence_uncertain either way."""
        wdi = _wdi(
            window, zone, lifecycle_state=LifecycleState.MORNING,
            outdoor_temp_c=30.0, comfort_config=_HEAT_ONLY, presence_uncertain=True,
            is_in_solar_sector=True, morning_position_ha=70,
            current_position_internal=0,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.decided_by == "HeatEvaluator"


class TestTierOrchestratorMorningVsSafetyAndOverride:
    """B3-010 requirement 4: Morning must not bypass Safety or an active
    Manual Override -- Tier 1 still early-exits before Morning is ever
    reached, and the central Tier 2 gate still applies to a Morning
    candidate exactly like it already does for Night."""

    def test_storm_safety_wins_over_morning_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(
                id="default", morning_position=100, morning_enabled=True,
            ),
            lifecycle_state=LifecycleState.MORNING,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            weather_condition=WeatherCondition.STORM,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.STORM_SAFE

    def test_active_manual_override_blocks_morning_position(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = ManualOverride(
            window_id=window.id, override_position=20,
            started_at=_NOW, expires_at=_NOW.replace(hour=18),
            source="position_delta", overridden_state=ShadingState.OPEN,
            overridden_position=80,
        )
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(
                id="default", morning_position=100, morning_enabled=True,
            ),
            lifecycle_state=LifecycleState.MORNING,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=22.0, indoor_temp_c=21.0,
            exposure=_exposure(0.0),
            is_in_solar_sector=False,
            comfort_config=_NO_COMFORT,
            active_override=override,
        )
        result = orchestrator.evaluate_window(wdi)
        # LIFECYCLE candidates are always blocked while an override is
        # active (unchanged manual_override_policy.py semantics, same as
        # NightEvaluator) -- the override's own hold position applies.
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.decided_by == "ManualOverrideEvaluator"
