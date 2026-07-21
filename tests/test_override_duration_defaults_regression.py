"""Regression proof for the 120/240-minute BehaviorConfig.override_duration_min
question raised in the T7 pre-push review, and its subsequent cleanup.

Full usage audit performed before the T7 fix (see conversation record):
  1. BehaviorConfig.override_duration_min was constructed in exactly one
     production path: models/window_decision_input.py's
     build_window_decision_input(). It was NOT threaded through to the
     Coordinator via __init__.py — the Coordinator has always used its own
     hard-coded constructor default (120 min) instead.
  2. Grep across custom_components/ and tests/ found no BehaviorConfig(...)
     construction site (outside this file) that passed or asserted
     override_duration_min == 240.
  3. wdi.effective_behavior.override_duration_min was never read by
     diagnostics_builder.py, any sensor attribute, or any evaluator — grep
     confirmed zero read sites besides the dataclass definition itself.
  4. No pre-existing test asserted the value 240.

The REAL, actually-effective legacy duration (120 min daytime / 720 min
night) lives exclusively on OverridePolicyConfig (models/
override_policy.py), which IS threaded through __init__.py ->
SmartShadingCoordinator's own constructor defaults — this file proves that
chain end-to-end.

Follow-up cleanup (this file's current state): since the field was proven
dead (never read anywhere), it — and the matching build_window_decision_
input() parameter that only ever fed it — were removed entirely rather than
left in place. This file now asserts their absence instead of pinning a
default that no longer exists.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from custom_components.smartshading.models.behavior_config import BehaviorConfig
from custom_components.smartshading.models.override_policy import OverridePolicyConfig
from custom_components.smartshading.models.window_decision_input import (
    build_window_decision_input,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


class TestDeadOverrideDurationMinFieldWasRemoved:
    def test_behavior_config_has_no_override_duration_min_field(self) -> None:
        """The dead field (see module docstring) has been removed entirely
        — not merely deprecated or left with a stale default."""
        assert "override_duration_min" not in BehaviorConfig.__dataclass_fields__

    def test_behavior_config_has_no_night_duration_field(self) -> None:
        """T7 introduced and then removed this field again after the
        pre-push review: it was net-new (no historical precedent to
        preserve) and had zero consumers, identical to the override_
        duration_min dead-field problem — so it was never reintroduced."""
        assert "override_night_duration_min" not in BehaviorConfig.__dataclass_fields__

    def test_build_window_decision_input_has_no_override_duration_min_parameter(self) -> None:
        """The builder parameter that only ever fed the dead BehaviorConfig
        field was removed alongside it."""
        params = inspect.signature(build_window_decision_input).parameters
        assert "override_duration_min" not in params


class TestOverridePolicyConfigCarriesTheRealEffectiveLegacyDefault:
    def test_duration_min_is_120(self) -> None:
        policy = OverridePolicyConfig()
        assert policy.duration_min == 120

    def test_night_duration_min_is_720(self) -> None:
        policy = OverridePolicyConfig()
        assert policy.night_duration_min == 720


class TestEffectiveRuntimeChainUsesOverridePolicyConfigNotBehaviorConfigDefault:
    def test_init_wires_coordinator_from_override_policy_not_behavior_config(self) -> None:
        """__init__.py passes override_duration_min=entry_data.override_policy.duration_min
        (120) into SmartShadingCoordinator — never from a BehaviorConfig
        instance. Verified by source text inspection (no HA-dependent
        import needed, unlike importing coordinator.py directly)."""
        source = (_INTEGRATION_ROOT / "__init__.py").read_text(encoding="utf-8")
        assert "override_duration_min=entry_data.override_policy.duration_min" in source
        assert "BehaviorConfig(" not in source

    def test_coordinator_constructor_default_is_still_120_and_720(self) -> None:
        """SmartShadingCoordinator's own constructor defaults (used whenever
        __init__.py's override_policy-derived kwargs are not supplied, e.g.
        directly-constructed test coordinators) remain the actually-
        effective legacy values, unaffected by the removal of
        BehaviorConfig.override_duration_min."""
        source = (_INTEGRATION_ROOT / "coordinator.py").read_text(encoding="utf-8")
        assert "override_duration_min: int = 120," in source
        assert "override_night_duration_min: int = 720," in source


class TestBuilderStillFunctionsAfterFieldRemoval:
    def _make_wdi(self):
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
        )

    def test_builder_runs_with_its_production_call_parameters(self) -> None:
        """Smoke test: removing the dead parameter must not break the
        builder for its remaining, still-productive parameters."""
        wdi = self._make_wdi()
        assert wdi.effective_behavior.override_detection_tolerance == 10

    def test_resulting_behavior_config_has_no_override_duration_min_attribute(self) -> None:
        wdi = self._make_wdi()
        assert not hasattr(wdi.effective_behavior, "override_duration_min")
