"""Regression proof for the 120/240-minute BehaviorConfig.override_duration_min
question raised in the T7 pre-push review.

Full usage audit performed before this fix (see conversation record):
  1. BehaviorConfig.override_duration_min is constructed in exactly one
     production path: models/window_decision_input.py's
     build_window_decision_input(). It is NOT threaded through to the
     Coordinator via __init__.py — the Coordinator has always used its own
     hard-coded constructor default (120 min) instead.
  2. Grep across custom_components/ and tests/ found no BehaviorConfig(...)
     construction site (outside this file) that passes or asserts
     override_duration_min == 240.
  3. wdi.effective_behavior.override_duration_min is never read by
     diagnostics_builder.py, any sensor attribute, or any evaluator — grep
     confirms zero read sites besides the dataclass definition itself.
  4. No pre-existing test asserted the value 240.
  5. Therefore reverting BehaviorConfig's default to its original,
     historical 240 is safe: T7 does not change any observable behavior by
     doing so, and does not silently mutate a pre-existing public dataclass
     default that might be constructed by test harnesses, research/support
     tooling, or future callers outside the current setup path.

The REAL, actually-effective legacy duration (120 min daytime / 720 min
night) now lives exclusively on OverridePolicyConfig (models/
override_policy.py), which IS threaded through __init__.py ->
SmartShadingCoordinator's own constructor defaults — this file proves that
chain end-to-end.
"""
from __future__ import annotations

from pathlib import Path

from custom_components.smartshading.models.behavior_config import BehaviorConfig
from custom_components.smartshading.models.override_policy import OverridePolicyConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


class TestBehaviorConfigDefaultRestoredToHistoricalValue:
    def test_override_duration_min_is_240(self) -> None:
        """Historical default, unchanged since before T7 — this field is
        not on the production wiring path (see module docstring point 1)."""
        bc = BehaviorConfig()
        assert bc.override_duration_min == 240

    def test_behavior_config_has_no_night_duration_field(self) -> None:
        """T7 introduced and then removed this field again after the
        pre-push review: it was net-new (no historical precedent to
        preserve) and had zero consumers, identical to the override_
        duration_min dead-field problem this review flagged — so it is not
        reintroduced."""
        assert not hasattr(BehaviorConfig(), "override_night_duration_min")


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
        effective legacy values, unaffected by BehaviorConfig's reverted
        default."""
        source = (_INTEGRATION_ROOT / "coordinator.py").read_text(encoding="utf-8")
        assert "override_duration_min: int = 120," in source
        assert "override_night_duration_min: int = 720," in source
