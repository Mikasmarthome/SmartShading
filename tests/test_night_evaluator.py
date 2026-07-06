"""Tests for NightEvaluator (Tier 3 Lifecycle Phase Gate).

NightEvaluator has a single, narrow contract:
  - NIGHT + night_position configured → WindowDecision(NIGHT_CLOSED, night_position)
  - Any other lifecycle state → None
  - night_position is None (disabled) → None

All other tiers (Manual Override, Absence, Heat, Glare) are outside its
scope and must not appear in any assertion here.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.evaluators.night_evaluator import NightEvaluator
from custom_components.smartshading.models.behavior_config import BehaviorConfig
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
def evaluator() -> NightEvaluator:
    return NightEvaluator()


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
    lifecycle_state: LifecycleState,
    *,
    night_position_ha: int = 0,       # HA convention → converted to internal in builder
    night_shading_enabled: bool = True,
):
    """Build a WindowDecisionInput with the given lifecycle state and night config."""
    lifecycle_config = NightDayLifecycleConfig(
        id="default",
        night_position=night_position_ha,
        night_enabled=True,
    )
    global_defaults = GlobalDefaults(night_shading_enabled=night_shading_enabled)
    return build_window_decision_input(
        window=window,
        zone=zone,
        global_defaults=global_defaults,
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=lifecycle_config,
        lifecycle_state=lifecycle_state,
        absence_active=False,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None,
        indoor_temp_c=None,
        exposure=None,
        is_in_solar_sector=False,
    )


# ---------------------------------------------------------------------------
# Core contract: NIGHT phase
# ---------------------------------------------------------------------------

class TestNightEvaluatorNightPhase:
    def test_night_phase_returns_decision(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.NIGHT)
        result = evaluator.evaluate(wdi)
        assert result is not None

    def test_night_phase_returns_night_closed_state(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.NIGHT)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.NIGHT_CLOSED

    def test_night_phase_returns_correct_window_id(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.NIGHT)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_night_phase_target_position_comes_from_effective_behavior(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # night_position=0 in HA convention → 100 internal (fully shaded)
        wdi = _wdi(window, zone, LifecycleState.NIGHT, night_position_ha=0)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == wdi.effective_behavior.night_position
        assert result.target_position == 100  # internal convention: 0=open, 100=shaded

    def test_night_phase_partial_position(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # night_position=20 in HA convention (20% open = nearly closed)
        # → 80 internal (80% shaded)
        wdi = _wdi(window, zone, LifecycleState.NIGHT, night_position_ha=20)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 80

    def test_decided_by_is_night_evaluator(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.NIGHT)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "NightEvaluator"

    def test_target_tilt_is_none(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.NIGHT)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None


# ---------------------------------------------------------------------------
# Core contract: non-NIGHT lifecycle states return None
# ---------------------------------------------------------------------------

class TestNightEvaluatorNonNightPhases:
    def test_day_phase_returns_none(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.DAY)
        assert evaluator.evaluate(wdi) is None

    def test_morning_phase_returns_none(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.MORNING)
        assert evaluator.evaluate(wdi) is None

    def test_evening_phase_returns_none(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # EVENING is a documented placeholder state; NightEvaluator must ignore it
        wdi = _wdi(window, zone, LifecycleState.EVENING)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: night shading disabled
# ---------------------------------------------------------------------------

class TestNightEvaluatorDisabled:
    def test_night_position_none_returns_none(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # night_shading_enabled=False → build_window_decision_input sets night_position=None
        wdi = _wdi(window, zone, LifecycleState.NIGHT, night_shading_enabled=False)
        assert wdi.effective_behavior.night_position is None
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Scope boundary: NightEvaluator has zero knowledge of other tiers
# ---------------------------------------------------------------------------

class TestNightEvaluatorScopeBoundary:
    def test_active_override_field_is_never_read(
        self, evaluator: NightEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """active_override is always None in MVP.  NightEvaluator must not read it.

        The orchestrator (not NightEvaluator) is responsible for checking
        Manual Override before calling Tier 3 evaluators.  This test ensures
        that NightEvaluator returns a decision regardless of the override field.
        """
        wdi = _wdi(window, zone, LifecycleState.NIGHT)
        assert wdi.active_override is None  # always None in MVP
        result = evaluator.evaluate(wdi)
        # NightEvaluator proceeds normally — it has no knowledge of overrides
        assert result is not None
        assert result.shading_state is ShadingState.NIGHT_CLOSED

    def test_absence_active_does_not_affect_result(
        self, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """AbsenceEvaluator's domain (absence_active) must not influence NightEvaluator."""
        evaluator = NightEvaluator()
        lifecycle_config = NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True)
        wdi_absent = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(night_shading_enabled=True),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=lifecycle_config,
            lifecycle_state=LifecycleState.NIGHT,
            absence_active=True,        # ← AbsenceEvaluator's domain
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
        )
        # NightEvaluator must return NIGHT_CLOSED regardless of absence_active
        result = evaluator.evaluate(wdi_absent)
        assert result is not None
        assert result.shading_state is ShadingState.NIGHT_CLOSED
