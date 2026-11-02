"""T21 Phase C2 — System entry global defaults: the resolver functions in
config_entry_data.py (resolve_zone_override_policy)
that decide, for each zone, whether to use its own explicit config or defer
to the System entry's default. (The Cover Dispatch resolver was removed in
1.2.0 together with the user-selectable dispatch modes.)

Precedence (documented in each resolver's own docstring):
  Manual Override — zone's stored override_policy.use_system_default flag
    (migration default: True only when the zone never configured this step
    at all, i.e. no "release_strategy" key stored) decides; detection_tolerance
    is always the zone's own value regardless.

These are pure functions — no HA imports needed, no selector stub required.
"""
from __future__ import annotations

from custom_components.smartshading.config_entry_data import (
    resolve_zone_override_policy,
)
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
