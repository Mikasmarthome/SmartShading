"""Tests for Step 8c: lifecycle-triggered override break.

Contracts tested:
  1. lifecycle_should_break_override() pure function — unit tests.
  2. OVERRIDE_EVENT_TYPES includes "cleared_by_lifecycle".
  3. break_enabled=False: lifecycle transition does NOT clear override.
  4. break_enabled=True: any lifecycle transition clears override.
  5. No lifecycle transition: override stays regardless of break_enabled.
  6. All transition pairs that SmartShading can produce are covered:
       DAY→NIGHT, NIGHT→MORNING, MORNING→DAY, DAY→DAY (no-op).
  7. BehaviorConfig.override_break_on_lifecycle defaults to True and is
     threaded through build_window_decision_input() unchanged.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.engines.lifecycle_guard import (
    lifecycle_should_break_override,
)
from custom_components.smartshading.models.behavior_config import BehaviorConfig
from custom_components.smartshading.models.learning import OVERRIDE_EVENT_TYPES
from custom_components.smartshading.models.lifecycle import LifecycleState
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy


# ===========================================================================
# Pure function: lifecycle_should_break_override()
# ===========================================================================

class TestLifecycleShouldBreakOverride:
    """Unit tests for the pure helper in engines/lifecycle_guard.py."""

    # --- break_enabled=False: never breaks ------------------------------------

    def test_disabled_no_break_on_day_to_night(self) -> None:
        assert not lifecycle_should_break_override(
            prev=LifecycleState.DAY,
            new=LifecycleState.NIGHT,
            break_enabled=False,
        )

    def test_disabled_no_break_on_night_to_morning(self) -> None:
        assert not lifecycle_should_break_override(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.MORNING,
            break_enabled=False,
        )

    def test_disabled_no_break_on_morning_to_day(self) -> None:
        assert not lifecycle_should_break_override(
            prev=LifecycleState.MORNING,
            new=LifecycleState.DAY,
            break_enabled=False,
        )

    def test_disabled_no_break_on_identical_states(self) -> None:
        assert not lifecycle_should_break_override(
            prev=LifecycleState.DAY,
            new=LifecycleState.DAY,
            break_enabled=False,
        )

    # --- break_enabled=True: breaks on any transition -------------------------

    def test_enabled_breaks_on_day_to_night(self) -> None:
        assert lifecycle_should_break_override(
            prev=LifecycleState.DAY,
            new=LifecycleState.NIGHT,
            break_enabled=True,
        )

    def test_enabled_breaks_on_night_to_morning(self) -> None:
        assert lifecycle_should_break_override(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.MORNING,
            break_enabled=True,
        )

    def test_enabled_breaks_on_morning_to_day(self) -> None:
        assert lifecycle_should_break_override(
            prev=LifecycleState.MORNING,
            new=LifecycleState.DAY,
            break_enabled=True,
        )

    # --- break_enabled=True but no transition: no break -----------------------

    @pytest.mark.parametrize("state", list(LifecycleState))
    def test_enabled_no_break_on_identical_state(self, state: LifecycleState) -> None:
        assert not lifecycle_should_break_override(
            prev=state,
            new=state,
            break_enabled=True,
        )

    # --- Return type is always bool -------------------------------------------

    def test_returns_bool_when_breaking(self) -> None:
        result = lifecycle_should_break_override(
            prev=LifecycleState.DAY,
            new=LifecycleState.NIGHT,
            break_enabled=True,
        )
        assert result is True

    def test_returns_bool_when_not_breaking(self) -> None:
        result = lifecycle_should_break_override(
            prev=LifecycleState.DAY,
            new=LifecycleState.DAY,
            break_enabled=True,
        )
        assert result is False


# ===========================================================================
# Learning model: OVERRIDE_EVENT_TYPES contains "cleared_by_lifecycle"
# ===========================================================================

class TestOverrideEventTypesIncludesLifecycle:
    def test_cleared_by_lifecycle_in_event_types(self) -> None:
        assert "cleared_by_lifecycle" in OVERRIDE_EVENT_TYPES

    def test_event_types_has_eight_members(self) -> None:
        # T10: three release-strategy-triggered clear reasons were added
        # (cleared_by_comfort, cleared_by_protection, cleared_by_manual)
        # alongside the original five.
        assert len(OVERRIDE_EVENT_TYPES) == 8


# ===========================================================================
# BehaviorConfig: field presence, default, and configurability
# ===========================================================================

class TestBehaviorConfigOverrideBreakOnLifecycle:
    """T10: the old bool override_break_on_lifecycle field became the
    OverrideReleaseStrategy-valued override_release_strategy field —
    LIFECYCLE reproduces the old True (break on any lifecycle transition),
    any other strategy reproduces the old False (see lifecycle_guard.py
    lifecycle_should_break_override(), still a plain bool at that layer;
    the coordinator now derives that bool from
    ``override_release_strategy is OverrideReleaseStrategy.LIFECYCLE``)."""

    def test_default_is_lifecycle(self) -> None:
        bc = BehaviorConfig()
        assert bc.override_release_strategy is OverrideReleaseStrategy.LIFECYCLE

    def test_opt_out_to_duration(self) -> None:
        bc = BehaviorConfig(override_release_strategy=OverrideReleaseStrategy.DURATION)
        assert bc.override_release_strategy is OverrideReleaseStrategy.DURATION

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError
        bc = BehaviorConfig()
        with pytest.raises(FrozenInstanceError):
            bc.override_release_strategy = OverrideReleaseStrategy.DURATION  # type: ignore[misc]


# ===========================================================================
# build_window_decision_input(): override_break_on_lifecycle is threaded
# ===========================================================================

class TestWindowDecisionInputThreading:
    """Verify the new param reaches BehaviorConfig through the builder."""

    def _make_wdi(self, release_strategy: OverrideReleaseStrategy):
        from custom_components.smartshading.models.window_decision_input import (
            build_window_decision_input,
        )
        from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
        from custom_components.smartshading.models.lifecycle import (
            LifecycleState,
            NightDayLifecycleConfig,
        )
        from custom_components.smartshading.models.window import WindowConfig
        from custom_components.smartshading.models.zone import ZoneConfig
        from custom_components.smartshading.state_machine.states import ShadingState

        window = WindowConfig(
            id="w1", name="South", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1"
        )
        zone = ZoneConfig(id="z1", name="Living Room")
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
            override_release_strategy=release_strategy,
        )

    def test_lifecycle_reaches_behavior(self) -> None:
        wdi = self._make_wdi(OverrideReleaseStrategy.LIFECYCLE)
        assert wdi.effective_behavior.override_release_strategy is OverrideReleaseStrategy.LIFECYCLE

    def test_duration_reaches_behavior(self) -> None:
        wdi = self._make_wdi(OverrideReleaseStrategy.DURATION)
        assert wdi.effective_behavior.override_release_strategy is OverrideReleaseStrategy.DURATION

    def test_default_is_lifecycle(self) -> None:
        """Calling the builder without the param must default to LIFECYCLE."""
        from custom_components.smartshading.models.window_decision_input import (
            build_window_decision_input,
        )
        from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
        from custom_components.smartshading.models.lifecycle import (
            LifecycleState,
            NightDayLifecycleConfig,
        )
        from custom_components.smartshading.models.window import WindowConfig
        from custom_components.smartshading.models.zone import ZoneConfig
        from custom_components.smartshading.state_machine.states import ShadingState

        window = WindowConfig(
            id="w1", name="South", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1"
        )
        zone = ZoneConfig(id="z1", name="Living Room")
        wdi = build_window_decision_input(
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
            # override_release_strategy NOT passed — must default to LIFECYCLE
        )
        assert wdi.effective_behavior.override_release_strategy is OverrideReleaseStrategy.LIFECYCLE


# ===========================================================================
# Transition coverage matrix
# ===========================================================================

class TestAllTransitionPairs:
    """Every LifecycleState transition pair must be handled correctly."""

    @pytest.mark.parametrize("prev,new,expected", [
        # Transitions — should break when enabled
        (LifecycleState.DAY,     LifecycleState.NIGHT,   True),
        (LifecycleState.NIGHT,   LifecycleState.MORNING, True),
        (LifecycleState.MORNING, LifecycleState.DAY,     True),
        (LifecycleState.DAY,     LifecycleState.MORNING, True),  # unusual but valid
        (LifecycleState.NIGHT,   LifecycleState.DAY,     True),  # unusual but valid
        # No-op — same state, must never break even when enabled
        (LifecycleState.DAY,     LifecycleState.DAY,     False),
        (LifecycleState.NIGHT,   LifecycleState.NIGHT,   False),
        (LifecycleState.MORNING, LifecycleState.MORNING, False),
    ])
    def test_break_enabled_matrix(
        self,
        prev: LifecycleState,
        new: LifecycleState,
        expected: bool,
    ) -> None:
        result = lifecycle_should_break_override(prev=prev, new=new, break_enabled=True)
        assert result is expected

    @pytest.mark.parametrize("prev,new", [
        (LifecycleState.DAY,     LifecycleState.NIGHT),
        (LifecycleState.NIGHT,   LifecycleState.MORNING),
        (LifecycleState.MORNING, LifecycleState.DAY),
    ])
    def test_break_disabled_never_breaks(
        self,
        prev: LifecycleState,
        new: LifecycleState,
    ) -> None:
        result = lifecycle_should_break_override(prev=prev, new=new, break_enabled=False)
        assert result is False
