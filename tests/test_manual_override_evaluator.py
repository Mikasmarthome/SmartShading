"""Tests for ManualOverrideEvaluator (Tier 2 gate).

ManualOverrideEvaluator contract:
  - Returns None when wdi.active_override is None (no active override).
  - Returns MANUAL_OVERRIDE decision when active_override is set.
  - Decision carries the override's position as target_position.
  - Evaluator is stateless: same input always produces same output.
  - decided_by is always "ManualOverrideEvaluator".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.evaluators.manual_override_evaluator import (
    ManualOverrideEvaluator,
)
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)
_OVERRIDE_POSITION = 40  # internal: 40 % shaded


def _make_override(
    window_id: str = "w-south",
    override_position: int = _OVERRIDE_POSITION,
    overridden_state: ShadingState = ShadingState.NORMAL_SHADE,
) -> ManualOverride:
    return ManualOverride(
        window_id=window_id,
        override_position=override_position,
        started_at=_NOW,
        expires_at=_NOW + timedelta(hours=4),
        source="position_delta",
        overridden_state=overridden_state,
        overridden_position=75,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def evaluator() -> ManualOverrideEvaluator:
    return ManualOverrideEvaluator()


@pytest.fixture()
def window() -> WindowConfig:
    return WindowConfig(
        id="w-south", name="South", zone_id="z1",
        azimuth=180.0, floor_level=0, cover_group_id="cg-south",
    )


@pytest.fixture()
def zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Living Room")


def _wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    *,
    active_override: ManualOverride | None = None,
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
        active_override=active_override,
    )


# ---------------------------------------------------------------------------
# Core contract: no override → None
# ---------------------------------------------------------------------------

class TestManualOverrideEvaluatorNoOverride:
    def test_returns_none_when_no_override(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, active_override=None)
        assert evaluator.evaluate(wdi) is None

    def test_returns_none_explicitly_no_override(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: active override → MANUAL_OVERRIDE decision
# ---------------------------------------------------------------------------

class TestManualOverrideEvaluatorActive:
    def test_returns_decision_when_override_active(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, active_override=_make_override(window.id))
        result = evaluator.evaluate(wdi)
        assert result is not None

    def test_returns_manual_override_state(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, active_override=_make_override(window.id))
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE

    def test_target_position_matches_override(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        override = _make_override(window.id, override_position=55)
        wdi = _wdi(window, zone, active_override=override)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 55

    def test_target_position_zero_override(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Override at 0 (fully open in internal convention) is a valid position."""
        override = _make_override(window.id, override_position=0)
        wdi = _wdi(window, zone, active_override=override)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 0

    def test_target_position_hundred_override(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Override at 100 (fully shaded in internal convention) is a valid position."""
        override = _make_override(window.id, override_position=100)
        wdi = _wdi(window, zone, active_override=override)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 100


# ---------------------------------------------------------------------------
# Core contract: output fields
# ---------------------------------------------------------------------------

class TestManualOverrideEvaluatorOutputFields:
    def test_decided_by_is_manual_override_evaluator(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, active_override=_make_override(window.id))
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "ManualOverrideEvaluator"

    def test_category_is_hold(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """T7: the MANUAL_OVERRIDE decision itself is tagged HOLD."""
        wdi = _wdi(window, zone, active_override=_make_override(window.id))
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.category is DecisionCategory.HOLD

    def test_window_id_matches(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, active_override=_make_override(window.id))
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_target_tilt_is_none(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """target_tilt is Phase 2 only — always None in MVP."""
        wdi = _wdi(window, zone, active_override=_make_override(window.id))
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None


# ---------------------------------------------------------------------------
# Core contract: statelessness
# ---------------------------------------------------------------------------

class TestManualOverrideEvaluatorStateless:
    def test_same_input_same_output(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Evaluator is stateless — repeated calls produce identical results."""
        override = _make_override(window.id, override_position=30)
        wdi = _wdi(window, zone, active_override=override)
        result_1 = evaluator.evaluate(wdi)
        result_2 = evaluator.evaluate(wdi)
        assert result_1 is not None
        assert result_2 is not None
        assert result_1.shading_state is result_2.shading_state
        assert result_1.target_position == result_2.target_position

    def test_override_from_different_previous_states(
        self, evaluator: ManualOverrideEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The evaluator only reads override_position — overridden_state has no effect."""
        for prev in (ShadingState.NIGHT_CLOSED, ShadingState.STRONG_SHADE, ShadingState.OPEN):
            override = _make_override(window.id, override_position=50, overridden_state=prev)
            wdi = _wdi(window, zone, active_override=override)
            result = evaluator.evaluate(wdi)
            assert result is not None
            assert result.shading_state is ShadingState.MANUAL_OVERRIDE
            assert result.target_position == 50
