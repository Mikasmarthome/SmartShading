"""Tests for AbsenceEvaluator (Tier 4 Protection Floor).

AbsenceEvaluator has a narrow contract:
  - absence_active=True + absence_position configured → WindowDecision(ABSENCE_CLOSED, floor)
  - absence_active=False → None
  - absence_position is None (disabled) → None

The returned target_position is a FLOOR, not an absolute target.
PositionResolver (Step 3) will take max() of all Tier 4 floors.
AbsenceEvaluator has no knowledge of other tiers.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.evaluators.absence_evaluator import AbsenceEvaluator
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
def evaluator() -> AbsenceEvaluator:
    return AbsenceEvaluator()


@pytest.fixture()
def window() -> WindowConfig:
    return WindowConfig(
        id="window-west", name="West", zone_id="zone-1",
        azimuth=270.0, floor_level=0, cover_group_id="cg-west",
    )


@pytest.fixture()
def zone() -> ZoneConfig:
    return ZoneConfig(id="zone-1", name="Living Room")


def _wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    absence_active: bool,
    absence_position_ha: int = 30,         # HA convention
    absence_shading_enabled: bool = True,
    lifecycle_state: LifecycleState = LifecycleState.DAY,
):
    global_defaults = GlobalDefaults(
        absence_shading_enabled=absence_shading_enabled,
        absence_position=absence_position_ha,
    )
    return build_window_decision_input(
        window=window, zone=zone,
        global_defaults=global_defaults,
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=lifecycle_state,
        absence_active=absence_active,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
        is_in_solar_sector=False,
    )


# ---------------------------------------------------------------------------
# Core contract: absence active
# ---------------------------------------------------------------------------

class TestAbsenceEvaluatorActive:
    def test_absence_active_returns_decision(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=True)
        assert evaluator.evaluate(wdi) is not None

    def test_shading_state_is_absence_closed(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.ABSENCE_CLOSED

    def test_correct_window_id(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_target_position_comes_from_effective_behavior(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # absence_position=30 in HA convention → 70 internal (70% shaded)
        wdi = _wdi(window, zone, absence_active=True, absence_position_ha=30)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == wdi.effective_behavior.absence_position
        assert result.target_position == 70  # internal convention

    def test_decided_by_is_absence_evaluator(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "AbsenceEvaluator"

    def test_target_tilt_is_none(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=True)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None


# ---------------------------------------------------------------------------
# Core contract: absence not active
# ---------------------------------------------------------------------------

class TestAbsenceEvaluatorNotActive:
    def test_absence_not_active_returns_none(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=False)
        assert evaluator.evaluate(wdi) is None

    def test_not_active_regardless_of_configured_position(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, absence_active=False, absence_position_ha=10)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: absence shading disabled
# ---------------------------------------------------------------------------

class TestAbsenceEvaluatorDisabled:
    def test_absence_position_none_returns_none(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # absence_shading_enabled=False → build_wdi sets absence_position=None
        wdi = _wdi(window, zone, absence_active=True, absence_shading_enabled=False)
        assert wdi.effective_behavior.absence_position is None
        assert evaluator.evaluate(wdi) is None

    def test_window_level_disable_respected(self, evaluator: AbsenceEvaluator) -> None:
        window_no_absence = WindowConfig(
            id="w-no-absence", name="No Absence", zone_id="z1",
            azimuth=90.0, floor_level=0, cover_group_id="cg-1",
            absence_shading_enabled=False,   # window-level override
        )
        zone = ZoneConfig(id="z1", name="Zone")
        wdi = _wdi(window_no_absence, zone, absence_active=True, absence_shading_enabled=True)
        # Window override (False) wins over global default (True)
        assert wdi.effective_behavior.absence_position is None
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Floor semantics: target_position is a floor, not an absolute target
# ---------------------------------------------------------------------------

class TestAbsenceEvaluatorFloorSemantics:
    def test_different_configured_floors(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The floor value comes from config — no hardcoded assumptions."""
        for ha_pos, expected_internal in [(30, 70), (50, 50), (10, 90), (0, 100)]:
            wdi = _wdi(window, zone, absence_active=True, absence_position_ha=ha_pos)
            result = evaluator.evaluate(wdi)
            assert result is not None, f"Expected decision for ha_pos={ha_pos}"
            assert result.target_position == expected_internal, (
                f"ha_pos={ha_pos} → expected internal={expected_internal}, "
                f"got {result.target_position}"
            )

    def test_floor_not_absolute_target_documented(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """AbsenceEvaluator returns a floor.

        The PositionResolver (Step 3) takes max() of all active Tier 4 floors.
        A heat evaluator could independently return a higher floor (e.g. 85),
        and PositionResolver would use 85 — the absence floor (70) only matters
        if nothing else demands more.  This test verifies that AbsenceEvaluator
        returns its floor value without knowledge of other evaluators.
        """
        wdi = _wdi(window, zone, absence_active=True, absence_position_ha=30)
        result = evaluator.evaluate(wdi)
        assert result is not None
        # 70 is the absence floor; PositionResolver decides the final position
        assert result.target_position == 70


# ---------------------------------------------------------------------------
# Scope boundary: AbsenceEvaluator has no knowledge of other tiers
# ---------------------------------------------------------------------------

class TestAbsenceEvaluatorScopeBoundary:
    def test_night_lifecycle_state_does_not_affect_result(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The orchestrator checks Night (Tier 3) before Absence (Tier 4).

        AbsenceEvaluator itself must not check lifecycle_state.  If called
        during NIGHT, it still returns a decision — the orchestrator decides
        whether to use it.
        """
        wdi = _wdi(window, zone, absence_active=True, lifecycle_state=LifecycleState.NIGHT)
        result = evaluator.evaluate(wdi)
        # AbsenceEvaluator returns its floor regardless of lifecycle phase
        assert result is not None
        assert result.shading_state is ShadingState.ABSENCE_CLOSED

    def test_active_override_field_is_never_read(
        self, evaluator: AbsenceEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """active_override is always None in MVP; AbsenceEvaluator must not read it."""
        wdi = _wdi(window, zone, absence_active=True)
        assert wdi.active_override is None
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.ABSENCE_CLOSED
