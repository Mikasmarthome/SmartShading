"""T21 Phase C3 — presence absence-delay System default resolver
(resolve_zone_absence_delay_min) and Lifecycle Profile removal migration
(_resolve_lifecycle_config_dict). Pure functions — no HA imports needed.
"""
from __future__ import annotations

from custom_components.smartshading.config_entry_data import (
    _resolve_lifecycle_config_dict,
    resolve_zone_absence_delay_min,
)


class TestLifecycleProfileCrudFullyRemoved:
    """T21 Phase C3: the named-profile CRUD/selection UI must not be
    reachable through any async_step_* method, hidden or not."""

    def test_no_lifecycle_profile_step_methods_remain(self) -> None:
        import sys
        import types
        from typing import Any as _Any

        def _stub(name: str, **attrs: _Any) -> types.ModuleType:
            m = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(m, k, v)
            return m

        class _SelectorConfigBase:
            def __init__(self, **kwargs: _Any) -> None:
                self.__dict__.update(kwargs)

        class _SelectorBase:
            def __init__(self, config: _Any = None) -> None:
                self.config = config

            def __call__(self, value: _Any) -> _Any:
                return value

        sys.modules["homeassistant.helpers.selector"] = _stub(
            "homeassistant.helpers.selector",
            EntitySelector=type("EntitySelector", (_SelectorBase,), {}),
            EntitySelectorConfig=type("EntitySelectorConfig", (_SelectorConfigBase,), {}),
            NumberSelector=type("NumberSelector", (_SelectorBase,), {}),
            NumberSelectorConfig=type("NumberSelectorConfig", (_SelectorConfigBase,), {}),
            NumberSelectorMode=types.SimpleNamespace(BOX="box", SLIDER="slider"),
            SelectSelector=type("SelectSelector", (_SelectorBase,), {}),
            SelectSelectorConfig=type("SelectSelectorConfig", (_SelectorConfigBase,), {}),
            SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown", LIST="list"),
            TimeSelector=type("TimeSelector", (_SelectorBase,), {}),
            BooleanSelector=type("BooleanSelector", (_SelectorBase,), {}),
        )
        sys.modules.pop("custom_components.smartshading.config_flow", None)
        from custom_components.smartshading.config_flow import SmartShadingOptionsFlow

        step_methods = [n for n in dir(SmartShadingOptionsFlow) if n.startswith("async_step_")]
        profile_related = [n for n in step_methods if "lifecycle_profile" in n or "_profile" in n]
        assert profile_related == [], f"unexpected surviving profile step(s): {profile_related}"


class TestResolveZoneAbsenceDelayMin:
    def test_zone_own_value_wins_by_default(self) -> None:
        zone_raw = {"absence_delay_min": 45}
        system_raw = {"system_absence_delay_min": 200}
        assert resolve_zone_absence_delay_min(zone_raw, system_raw) == 45

    def test_existing_install_with_only_the_legacy_default_still_keeps_its_own_value(self) -> None:
        # Every existing zone already has a concrete absence_delay_min
        # (a required field) — even the untouched default of 30 must NOT
        # silently start deferring to a differently-configured System value.
        zone_raw = {"absence_delay_min": 30}
        system_raw = {"system_absence_delay_min": 200}
        assert resolve_zone_absence_delay_min(zone_raw, system_raw) == 30

    def test_use_system_default_true_defers(self) -> None:
        zone_raw = {"absence_delay_min": 45, "absence_delay_use_system_default": True}
        system_raw = {"system_absence_delay_min": 200}
        assert resolve_zone_absence_delay_min(zone_raw, system_raw) == 200

    def test_falls_back_to_hardcoded_default_when_system_entry_unset(self) -> None:
        zone_raw = {"absence_delay_min": 45, "absence_delay_use_system_default": True}
        assert resolve_zone_absence_delay_min(zone_raw, None) == 30

    def test_removing_the_override_flag_reactivates_system_default(self) -> None:
        system_raw = {"system_absence_delay_min": 200}
        with_own = resolve_zone_absence_delay_min({"absence_delay_min": 45}, system_raw)
        with_system = resolve_zone_absence_delay_min(
            {"absence_delay_min": 45, "absence_delay_use_system_default": True}, system_raw
        )
        assert with_own == 45
        assert with_system == 200

    def test_two_zones_deferring_to_the_same_system_default_get_identical_values(self) -> None:
        system_raw = {"system_absence_delay_min": 77}
        zone_a = resolve_zone_absence_delay_min({"absence_delay_use_system_default": True}, system_raw)
        zone_b = resolve_zone_absence_delay_min({"absence_delay_use_system_default": True}, system_raw)
        assert zone_a == zone_b == 77

    def test_changing_the_system_default_only_affects_deferring_zones(self) -> None:
        own_zone_raw = {"absence_delay_min": 10}
        deferring_zone_raw = {"absence_delay_use_system_default": True}
        before = (
            resolve_zone_absence_delay_min(own_zone_raw, {"system_absence_delay_min": 100}),
            resolve_zone_absence_delay_min(deferring_zone_raw, {"system_absence_delay_min": 100}),
        )
        after = (
            resolve_zone_absence_delay_min(own_zone_raw, {"system_absence_delay_min": 999}),
            resolve_zone_absence_delay_min(deferring_zone_raw, {"system_absence_delay_min": 999}),
        )
        assert before[0] == after[0] == 10  # own-value zone unaffected
        assert before[1] == 100
        assert after[1] == 999  # deferring zone follows the new default


class TestLifecycleProfileMigration:
    """T21 Phase C3: Lifecycle Profiles (T6) were removed. A pre-C3 install's
    active profile (if any) must be resolved into the plain lifecycle_config
    dict, in memory, on every load — never rewriting storage, never raising."""

    def test_active_profile_config_is_used_verbatim(self) -> None:
        raw = {
            "lifecycle_config": {"id": "default", "night_position": 10},
            "lifecycle_profiles": {
                "p1": {"display_name": "Weekend", "config": {"id": "p1", "night_position": 99}},
            },
            "active_lifecycle_profile_id": "p1",
        }
        assert _resolve_lifecycle_config_dict(raw) == {"id": "p1", "night_position": 99}

    def test_multiple_profiles_only_the_active_one_is_used(self) -> None:
        raw = {
            "lifecycle_config": {"id": "default"},
            "lifecycle_profiles": {
                "p1": {"display_name": "A", "config": {"id": "p1", "night_position": 11}},
                "p2": {"display_name": "B", "config": {"id": "p2", "night_position": 22}},
                "p3": {"display_name": "C", "config": {"id": "p3", "night_position": 33}},
            },
            "active_lifecycle_profile_id": "p2",
        }
        assert _resolve_lifecycle_config_dict(raw)["night_position"] == 22

    def test_no_active_profile_id_falls_back_to_legacy_flat_config(self) -> None:
        raw = {
            "lifecycle_config": {"id": "default", "night_position": 55},
            "lifecycle_profiles": {"p1": {"display_name": "X", "config": {"id": "p1", "night_position": 1}}},
            "active_lifecycle_profile_id": None,
        }
        assert _resolve_lifecycle_config_dict(raw) == {"id": "default", "night_position": 55}

    def test_pre_t6_payload_with_no_profile_keys_at_all(self) -> None:
        raw = {"lifecycle_config": {"id": "default", "night_position": 55}}
        assert _resolve_lifecycle_config_dict(raw) == {"id": "default", "night_position": 55}

    def test_active_profile_id_pointing_at_unknown_profile_falls_back_safely(self) -> None:
        raw = {
            "lifecycle_config": {"id": "default", "night_position": 55},
            "lifecycle_profiles": {"p1": {"display_name": "X", "config": {"id": "p1", "night_position": 1}}},
            "active_lifecycle_profile_id": "does-not-exist",
        }
        assert _resolve_lifecycle_config_dict(raw) == {"id": "default", "night_position": 55}

    def test_malformed_lifecycle_profiles_value_never_raises(self) -> None:
        for bad in (None, "not-a-dict", [], 42, {"p1": "not-a-dict-entry"}, {"p1": {}}):
            raw = {
                "lifecycle_config": {"id": "default", "night_position": 55},
                "lifecycle_profiles": bad,
                "active_lifecycle_profile_id": "p1",
            }
            assert _resolve_lifecycle_config_dict(raw) == {"id": "default", "night_position": 55}

    def test_resolution_is_idempotent(self) -> None:
        raw = {
            "lifecycle_config": {"id": "default", "night_position": 55},
            "lifecycle_profiles": {"p1": {"display_name": "X", "config": {"id": "p1", "night_position": 99}}},
            "active_lifecycle_profile_id": "p1",
        }
        first = _resolve_lifecycle_config_dict(raw)
        # simulate "restart after migration": profile keys gone, resolved
        # config now lives at the flat key — running the same resolver
        # again on that already-migrated shape must return byte-identical
        # output (no further change on repeated reload/restart).
        migrated_raw = {"lifecycle_config": first}
        second = _resolve_lifecycle_config_dict(migrated_raw)
        assert first == second == {"id": "p1", "night_position": 99}

    def test_weekday_weekend_and_sun_event_fields_survive_migration_untouched(self) -> None:
        profile_config = {
            "id": "p1",
            "weekday_night_fixed_time": "22:00:00",
            "weekend_night_fixed_time": "23:30:00",
            "night_sun_event": "sunset",
            "morning_sun_event": "sunrise",
        }
        raw = {
            "lifecycle_config": {"id": "default"},
            "lifecycle_profiles": {"p1": {"display_name": "X", "config": profile_config}},
            "active_lifecycle_profile_id": "p1",
        }
        assert _resolve_lifecycle_config_dict(raw) == profile_config
