"""T21 Phase C2 — System entry global defaults: the resolver functions in
config_entry_data.py (resolve_zone_override_policy / resolve_zone_dispatch_config)
that decide, for each zone, whether to use its own explicit config or defer
to the System entry's default.

Precedence (documented in each resolver's own docstring):
  Manual Override — zone's stored override_policy.use_system_default flag
    (migration default: True only when the zone never configured this step
    at all, i.e. no "release_strategy" key stored) decides; detection_tolerance
    is always the zone's own value regardless.
  Cover Dispatch — a zone with its own explicit "dispatch_config" key
    (any pre-C2 install) always keeps it; every other zone uses the System
    entry's default.

These are pure functions — no HA imports needed, no selector stub required.
"""
from __future__ import annotations

from custom_components.smartshading.config_entry_data import (
    resolve_zone_dispatch_config,
    resolve_zone_override_policy,
)
from custom_components.smartshading.models.dispatch_config import DispatchConfig, DispatchMode
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.models.override_policy import OverridePolicyConfig


class TestResolveZoneOverridePolicy:
    def test_zone_never_configured_defers_to_system_default(self) -> None:
        zone_raw = {"name": "Living Room"}
        system_raw = {"system_override_policy": {
            "release_strategy": "manual", "duration_min": 200,
        }}
        resolved = resolve_zone_override_policy(zone_raw, system_raw)
        assert resolved.release_strategy is OverrideReleaseStrategy.MANUAL
        assert resolved.duration_min == 200

    def test_zone_never_configured_falls_back_to_hardcoded_default_when_system_unset(self) -> None:
        resolved = resolve_zone_override_policy({"name": "Living Room"}, None)
        assert resolved == OverridePolicyConfig()

    def test_zone_with_explicit_pre_c2_strategy_ignores_system_default(self) -> None:
        # Migration guarantee: a zone that already had "release_strategy"
        # stored keeps its own behavior byte-for-byte, even if a System
        # default is later configured differently.
        zone_raw = {"override_policy": {
            "release_strategy": "fixed_time", "fixed_until": "07:00:00", "duration_min": 45,
        }}
        system_raw = {"system_override_policy": {"release_strategy": "manual", "duration_min": 999}}
        resolved = resolve_zone_override_policy(zone_raw, system_raw)
        assert resolved.release_strategy is OverrideReleaseStrategy.FIXED_TIME
        assert resolved.duration_min == 45

    def test_explicit_use_system_default_true_defers(self) -> None:
        zone_raw = {"override_policy": {
            "release_strategy": "fixed_time", "use_system_default": True, "detection_tolerance": 33,
        }}
        system_raw = {"system_override_policy": {"release_strategy": "manual", "duration_min": 300}}
        resolved = resolve_zone_override_policy(zone_raw, system_raw)
        assert resolved.release_strategy is OverrideReleaseStrategy.MANUAL
        assert resolved.duration_min == 300
        # detection_tolerance is zone-only — always the zone's own value.
        assert resolved.detection_tolerance == 33

    def test_explicit_use_system_default_false_keeps_own_settings(self) -> None:
        zone_raw = {"override_policy": {
            "release_strategy": "duration", "duration_min": 15, "use_system_default": False,
        }}
        system_raw = {"system_override_policy": {"release_strategy": "manual", "duration_min": 300}}
        resolved = resolve_zone_override_policy(zone_raw, system_raw)
        assert resolved.release_strategy is OverrideReleaseStrategy.DURATION
        assert resolved.duration_min == 15

    def test_two_zones_deferring_to_the_same_system_default_get_identical_values(self) -> None:
        system_raw = {"system_override_policy": {"release_strategy": "manual"}}
        zone_a = resolve_zone_override_policy({"name": "A"}, system_raw)
        zone_b = resolve_zone_override_policy({"name": "B"}, system_raw)
        assert zone_a == zone_b

    def test_changing_the_system_default_changes_every_deferring_zone(self) -> None:
        zone_raw = {"name": "A"}
        before = resolve_zone_override_policy(zone_raw, {"system_override_policy": {"duration_min": 100}})
        after = resolve_zone_override_policy(zone_raw, {"system_override_policy": {"duration_min": 200}})
        assert before.duration_min == 100
        assert after.duration_min == 200


class TestResolveZoneDispatchConfig:
    def test_zone_with_no_stored_dispatch_config_uses_system_default(self) -> None:
        system_raw = {"system_dispatch_config": {"mode": "sequential", "start_interval_s": 5.0}}
        resolved = resolve_zone_dispatch_config({"name": "A"}, system_raw)
        assert resolved.mode is DispatchMode.SEQUENTIAL
        assert resolved.start_interval_s == 5.0

    def test_zone_with_no_stored_dispatch_config_falls_back_to_hardcoded_default_when_system_unset(self) -> None:
        resolved = resolve_zone_dispatch_config({"name": "A"}, None)
        assert resolved == DispatchConfig()

    def test_zone_with_own_explicit_dispatch_config_ignores_system_default(self) -> None:
        # Migration guarantee: an install that already configured the (now
        # removed) zone-level Dispatch step keeps its own value forever,
        # regardless of what the System default later becomes.
        zone_raw = {"dispatch_config": {"mode": "parallel", "start_interval_s": 0.0}}
        system_raw = {"system_dispatch_config": {"mode": "sequential", "start_interval_s": 5.0}}
        resolved = resolve_zone_dispatch_config(zone_raw, system_raw)
        assert resolved.mode is DispatchMode.PARALLEL
        assert resolved.start_interval_s == 0.0

    def test_multiple_zones_with_different_pre_c2_values_stay_different_after_migration(self) -> None:
        zone_a = resolve_zone_dispatch_config(
            {"dispatch_config": {"mode": "parallel"}},
            {"system_dispatch_config": {"mode": "sequential"}},
        )
        zone_b = resolve_zone_dispatch_config(
            {"dispatch_config": {"mode": "sequential", "start_interval_s": 9.0}},
            {"system_dispatch_config": {"mode": "sequential"}},
        )
        assert zone_a.mode is DispatchMode.PARALLEL
        assert zone_b.start_interval_s == 9.0
        assert zone_a != zone_b

    def test_two_zones_deferring_to_the_same_system_default_get_identical_values(self) -> None:
        system_raw = {"system_dispatch_config": {"mode": "sequential"}}
        zone_a = resolve_zone_dispatch_config({"name": "A"}, system_raw)
        zone_b = resolve_zone_dispatch_config({"name": "B"}, system_raw)
        assert zone_a == zone_b

    def test_removing_a_zones_own_dispatch_config_activates_system_default(self) -> None:
        # Simulates a user editing storage to drop their zone-level override
        # (the only way to "reset to system default" since the zone-level
        # Dispatch step no longer exists) — the zone must pick up the System
        # default immediately, not silently keep stale behavior.
        system_raw = {"system_dispatch_config": {"mode": "sequential"}}
        with_override = resolve_zone_dispatch_config({"dispatch_config": {"mode": "parallel"}}, system_raw)
        without_override = resolve_zone_dispatch_config({}, system_raw)
        assert with_override.mode is DispatchMode.PARALLEL
        assert without_override.mode is DispatchMode.SEQUENTIAL
