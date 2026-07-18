"""Legacy-oracle regression test — T7.

Proves that with every new T7 policy option at its default (allow_comfort=
False, allow_protection=False, duration_mode=legacy), TierOrchestrator's
Manual-Override behavior is byte-identical to the pre-T7 architecture:
whenever an override is active, the result is ALWAYS MANUAL_OVERRIDE at the
override's own position, decided_by="ManualOverrideEvaluator" — regardless
of what Tier 3/4/5 would otherwise have decided.

Before T7, this was structural (ManualOverrideEvaluator ran as an
unconditional Tier-2 early exit, before Tier 3-5 were even invoked). After
T7's restructure (Tier 3-5 candidates are always computed, then filtered by
ManualOverridePolicy), the SAME outcome must hold under default flags — this
file is the direct proof, covering one representative scenario per
DecisionCategory that can reach the policy gate (LIFECYCLE, PROTECTION x3,
COMFORT x2, HOLD).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.smartshading.engines.exposure_engine import WindowExposure
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

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


def _override(position: int = 42) -> ManualOverride:
    return ManualOverride(
        window_id="w-south",
        override_position=position,
        started_at=_NOW,
        expires_at=_NOW.replace(hour=16),
        source="position_delta",
        overridden_state=ShadingState.OPEN,
        overridden_position=0,
    )


def _exposure(wm2: float) -> WindowExposure:
    return WindowExposure(
        window_id="w-south", timestamp=_NOW,
        sun_azimuth=180.0, sun_elevation=45.0,
        is_above_horizon=True, is_in_tolerance_window=True,
        azimuth_delta_deg=0.0, direct_radiation_factor=1.0,
        elevation_clipped=False,
        theoretical_exposure=wm2, learned_solar_impact_factor=1.0,
        seasonal_factor=1.0, effective_exposure=wm2,
    )


def _assert_legacy_hold(orchestrator: TierOrchestrator, wdi, override: ManualOverride) -> None:
    result = orchestrator.evaluate_window(wdi)
    assert result.shading_state is ShadingState.MANUAL_OVERRIDE
    assert result.target_position == override.override_position
    assert result.decided_by == "ManualOverrideEvaluator"


class TestLegacyOracleWithDefaultPolicyFlags:
    """Every scenario here: active override + all-default flags (allow_
    comfort=False, allow_protection=False) → MANUAL_OVERRIDE always wins,
    exactly as before T7, no matter what Tier 3-5 would have decided."""

    def test_lifecycle_night_candidate_still_blocked(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = _override(42)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(night_shading_enabled=True),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True),
            lifecycle_state=LifecycleState.NIGHT,
            absence_active=False, current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False, active_override=override,
        )
        _assert_legacy_hold(orchestrator, wdi, override)

    def test_protection_absence_candidate_still_blocked(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = _override(42)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(absence_shading_enabled=True, absence_position=30),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=True, current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False, active_override=override,
        )
        _assert_legacy_hold(orchestrator, wdi, override)

    def test_protection_heat_candidate_still_blocked(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = _override(42)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False, current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=30.0, indoor_temp_c=None,
            exposure=_exposure(300.0), is_in_solar_sector=True,
            comfort_config=ComfortConfig(heat_protection_enabled=True, glare_protection_enabled=False, solar_gain_enabled=False),
            active_override=override,
        )
        _assert_legacy_hold(orchestrator, wdi, override)

    def test_protection_glare_candidate_still_blocked(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = _override(42)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False, current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None,
            exposure=_exposure(120.0), is_in_solar_sector=True,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=True, solar_gain_enabled=False),
            active_override=override,
        )
        _assert_legacy_hold(orchestrator, wdi, override)

    def test_comfort_solar_candidate_still_blocked(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = _override(42)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False, current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None,
            exposure=_exposure(300.0), is_in_solar_sector=True,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
            active_override=override,
        )
        _assert_legacy_hold(orchestrator, wdi, override)

    def test_comfort_fallback_open_candidate_still_blocked(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Nothing would fire at all (no night/absence/heat/glare/solar) —
        the plain daytime fallback OPEN is COMFORT-tagged and must still be
        blocked, matching pre-T7 (override always won, full stop)."""
        override = _override(42)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(night_shading_enabled=False, absence_shading_enabled=False),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False, current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
            active_override=override,
        )
        _assert_legacy_hold(orchestrator, wdi, override)

    def test_hold_presence_uncertain_candidate_still_blocked(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The most subtle case (see engines/manual_override_policy.py
        docstring): a genuine no-op HOLD candidate must ALSO be converted to
        MANUAL_OVERRIDE, not passed through — otherwise this edge case would
        leak a non-override decision through an active override."""
        override = _override(42)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(night_shading_enabled=False, absence_shading_enabled=False),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False, current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
            active_override=override,
            presence_uncertain=True,
        )
        _assert_legacy_hold(orchestrator, wdi, override)

    def test_no_override_every_scenario_unaffected(
        self, orchestrator: TierOrchestrator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Sanity counterpart: without an active override, the Comfort
        fallback fires normally (proves the policy gate does not
        accidentally suppress anything when there is nothing to gate)."""
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(night_shading_enabled=False, absence_shading_enabled=False),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False, current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
            active_override=None,
        )
        result = orchestrator.evaluate_window(wdi)
        assert result.shading_state is ShadingState.OPEN
        assert result.decided_by == "TierOrchestrator:fallback"
