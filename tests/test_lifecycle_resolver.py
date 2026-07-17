"""Lifecycle profile resolution — v1.2.0-beta.1, Beta.1-T6.

Pure unit coverage of models/lifecycle_profile.py (LifecycleProfile) and
engines/lifecycle_resolver.py (resolve_lifecycle_config), plus config
storage round-trip. No Home Assistant dependency, no coordinator involved
(see tests/test_coordinator_lifecycle_profile.py for the wiring-level
tests: Coordinator diagnostics attributes, WindowBehaviorMode/Lifecycle/
Comfort/Protection/Presence/EMA/Manual-Override regression, restart/reload
behavior, and bug-injection).

Coverage:
  LP-01  No profiles key at all -> legacy config (byte-for-byte).
  LP-02  Empty profiles dict -> legacy config, never raises.
  LP-03  One valid profile, active -> that profile's config is used.
  LP-04  Multiple valid profiles -> the correct one (by id) is selected.
  LP-05  active_profile_id present and known -> stored source.
  LP-06  active_profile_id present but unknown -> fallback to legacy.
  LP-07  active_profile_id missing/None -> legacy (not merely "fallback").
  LP-08  Incomplete stored profile (missing individual fields) -> safe
         dataclass defaults for those fields, never raises.
  LP-09  Invalid field values in a stored profile -> safe per-field
         normalization (reuses _lifecycle_config_from_storage()).
  LP-10  Duplicate display_name across two profiles is technically allowed
         (identity is profile_id, never display_name).
  LP-11  profile_id is stable and independent of display_name (rename
         doesn't change identity; whitespace/case in the name never
         affects resolution).
  LP-12  Storage round-trip: profiles + active_profile_id survive
         serialize/deserialize exactly.
  LP-13  Unknown future keys inside a stored profile entry are safely
         ignored (forward compatibility), never raise.
  LP-14  Legacy flat lifecycle_config fields are completely unaffected by
         the presence of lifecycle_profiles (independent storage keys).
"""
from __future__ import annotations

from datetime import time

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    from_storage_dict,
    to_storage_dict,
)
from custom_components.smartshading.engines.lifecycle_resolver import (
    SOURCE_FALLBACK,
    SOURCE_LEGACY,
    SOURCE_STORED,
    resolve_lifecycle_config,
)
from custom_components.smartshading.models.lifecycle import (
    NightDayLifecycleConfig,
    NightTrigger,
    SunEvent,
)
from custom_components.smartshading.models.lifecycle_profile import LifecycleProfile


def _legacy() -> NightDayLifecycleConfig:
    return NightDayLifecycleConfig(id="default", night_fixed_time=time(22, 0), night_position=0)


# ---------------------------------------------------------------------------
# LP-01 / LP-02 — no profiles / empty profiles -> legacy.
# ---------------------------------------------------------------------------

class TestNoOrEmptyProfiles:
    def test_no_profiles_dict_uses_legacy(self):
        legacy = _legacy()
        result = resolve_lifecycle_config(legacy, {}, None)
        assert result.config is legacy
        assert result.source == SOURCE_LEGACY
        assert result.active_profile_id is None
        assert result.profile_count == 0

    def test_empty_profiles_with_active_id_set_still_uses_legacy(self):
        """Defensive: even a stray active_profile_id with no profiles at
        all must never raise or produce a fallback state — it's still
        legacy, since there's nothing to look up."""
        legacy = _legacy()
        result = resolve_lifecycle_config(legacy, {}, "some_id")
        assert result.config is legacy
        assert result.source == SOURCE_LEGACY


# ---------------------------------------------------------------------------
# LP-03 / LP-04 — valid profile(s), correct selection.
# ---------------------------------------------------------------------------

class TestValidProfileSelection:
    def test_one_profile_active(self):
        legacy = _legacy()
        weekend = LifecycleProfile(
            profile_id="p1", display_name="Weekend",
            config=NightDayLifecycleConfig(id="p1", night_position=50),
        )
        result = resolve_lifecycle_config(legacy, {"p1": weekend}, "p1")
        assert result.config is weekend.config
        assert result.config.night_position == 50
        assert result.source == SOURCE_STORED
        assert result.active_profile_id == "p1"
        assert result.profile_count == 1

    def test_multiple_profiles_correct_one_selected(self):
        legacy = _legacy()
        p1 = LifecycleProfile(profile_id="p1", display_name="A", config=NightDayLifecycleConfig(id="p1", night_position=10))
        p2 = LifecycleProfile(profile_id="p2", display_name="B", config=NightDayLifecycleConfig(id="p2", night_position=20))
        p3 = LifecycleProfile(profile_id="p3", display_name="C", config=NightDayLifecycleConfig(id="p3", night_position=30))
        profiles = {"p1": p1, "p2": p2, "p3": p3}
        result = resolve_lifecycle_config(legacy, profiles, "p2")
        assert result.config is p2.config
        assert result.config.night_position == 20
        assert result.profile_count == 3


# ---------------------------------------------------------------------------
# LP-05 / LP-06 / LP-07 — active_profile_id known/unknown/missing.
# ---------------------------------------------------------------------------

class TestActiveProfileIdHandling:
    def test_known_id_is_stored_source(self):
        legacy = _legacy()
        p1 = LifecycleProfile(profile_id="p1", display_name="A", config=NightDayLifecycleConfig(id="p1"))
        result = resolve_lifecycle_config(legacy, {"p1": p1}, "p1")
        assert result.source == SOURCE_STORED

    def test_unknown_id_falls_back_to_legacy(self):
        legacy = _legacy()
        p1 = LifecycleProfile(profile_id="p1", display_name="A", config=NightDayLifecycleConfig(id="p1"))
        result = resolve_lifecycle_config(legacy, {"p1": p1}, "does_not_exist")
        assert result.config is legacy
        assert result.source == SOURCE_FALLBACK
        assert result.active_profile_id == "does_not_exist"  # echoed for diagnostics
        assert result.profile_count == 1

    def test_missing_active_id_is_legacy_not_fallback(self):
        """None is a deliberate, valid 'use legacy' state — distinct from
        an unknown ID, which is corrupted/stale data. Both END with the
        legacy config, but the source differs for diagnostics honesty."""
        legacy = _legacy()
        p1 = LifecycleProfile(profile_id="p1", display_name="A", config=NightDayLifecycleConfig(id="p1"))
        result = resolve_lifecycle_config(legacy, {"p1": p1}, None)
        assert result.config is legacy
        assert result.source == SOURCE_LEGACY


# ---------------------------------------------------------------------------
# LP-08 / LP-09 — incomplete / invalid stored profile fields.
# ---------------------------------------------------------------------------

class TestIncompleteOrInvalidStoredProfile:
    def test_incomplete_profile_gets_dataclass_defaults(self):
        from custom_components.smartshading.config_entry_data import _lifecycle_config_from_storage
        raw = {"id": "p1", "night_position": 77}  # everything else missing
        config = _lifecycle_config_from_storage(raw)
        defaults = NightDayLifecycleConfig(id="p1")
        assert config.night_position == 77
        assert config.night_trigger == defaults.night_trigger  # dataclass default, not legacy's value
        assert config.morning_position == defaults.morning_position

    def test_invalid_field_values_normalize_safely(self):
        from custom_components.smartshading.config_entry_data import _lifecycle_config_from_storage
        raw = {"id": "p1", "night_trigger": "garbage", "night_sun_event": "not_a_real_event"}
        config = _lifecycle_config_from_storage(raw)  # must not raise
        assert config.night_trigger is NightTrigger.BOTH  # safe fallback
        assert config.night_sun_event is None


# ---------------------------------------------------------------------------
# LP-10 / LP-11 — duplicate names, stable id independent of name.
# ---------------------------------------------------------------------------

class TestIdentityIsProfileIdNotDisplayName:
    def test_duplicate_display_names_are_technically_allowed(self):
        legacy = _legacy()
        p1 = LifecycleProfile(profile_id="p1", display_name="Weekend", config=NightDayLifecycleConfig(id="p1", night_position=1))
        p2 = LifecycleProfile(profile_id="p2", display_name="Weekend", config=NightDayLifecycleConfig(id="p2", night_position=2))
        profiles = {"p1": p1, "p2": p2}
        result = resolve_lifecycle_config(legacy, profiles, "p2")
        assert result.config.night_position == 2  # resolves by id, unambiguous despite same name

    def test_rename_does_not_affect_resolution(self):
        legacy = _legacy()
        p1 = LifecycleProfile(profile_id="p1", display_name="Old Name", config=NightDayLifecycleConfig(id="p1", night_position=5))
        result_before = resolve_lifecycle_config(legacy, {"p1": p1}, "p1")
        p1_renamed = LifecycleProfile(profile_id="p1", display_name="New Name — with spaces & CAPS", config=p1.config)
        result_after = resolve_lifecycle_config(legacy, {"p1": p1_renamed}, "p1")
        assert result_before.config is result_after.config


# ---------------------------------------------------------------------------
# LP-12 — storage round-trip.
# ---------------------------------------------------------------------------

class TestStorageRoundTrip:
    def test_profiles_and_active_id_survive_round_trip(self):
        legacy = _legacy()
        p1 = LifecycleProfile(
            profile_id="p1", display_name="Weekend",
            config=NightDayLifecycleConfig(
                id="p1", night_fixed_time=time(21, 30), night_position=25,
                active_months=[6, 7, 8], night_sun_event=SunEvent.SUNSET,
                night_not_before=time(21, 0), night_not_after=time(22, 0),
            ),
        )
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True, lifecycle_config=legacy,
            lifecycle_profiles={"p1": p1}, active_lifecycle_profile_id="p1",
        )
        stored = to_storage_dict(data)
        assert stored["lifecycle_profiles"]["p1"]["display_name"] == "Weekend"
        assert stored["active_lifecycle_profile_id"] == "p1"
        restored = from_storage_dict(stored)
        assert "p1" in restored.lifecycle_profiles
        restored_profile = restored.lifecycle_profiles["p1"]
        assert restored_profile.display_name == "Weekend"
        assert restored_profile.config.night_fixed_time == time(21, 30)
        assert restored_profile.config.night_position == 25
        assert restored_profile.config.active_months == [6, 7, 8]
        assert restored_profile.config.night_sun_event is SunEvent.SUNSET
        assert restored_profile.config.night_not_before == time(21, 0)
        assert restored.active_lifecycle_profile_id == "p1"

    def test_missing_lifecycle_profiles_key_defaults_to_empty(self):
        raw = {"name": "Test", "use_home_location": True}
        restored = from_storage_dict(raw)
        assert restored.lifecycle_profiles == {}
        assert restored.active_lifecycle_profile_id is None

    def test_malformed_lifecycle_profiles_value_defaults_to_empty(self):
        raw = {"name": "Test", "use_home_location": True, "lifecycle_profiles": "not-a-dict"}
        restored = from_storage_dict(raw)
        assert restored.lifecycle_profiles == {}


# ---------------------------------------------------------------------------
# LP-13 — unknown future keys are safely ignored.
# ---------------------------------------------------------------------------

class TestForwardCompatibility:
    def test_unknown_keys_in_profile_entry_are_ignored(self):
        from custom_components.smartshading.config_entry_data import _lifecycle_profiles_from_storage
        raw = {
            "p1": {
                "display_name": "A",
                "config": {"id": "p1", "night_position": 40},
                "future_field_v2": {"some": "data"},  # unknown, must not raise
            }
        }
        profiles = _lifecycle_profiles_from_storage(raw)
        assert profiles["p1"].config.night_position == 40

    def test_malformed_individual_profile_entry_is_skipped_not_fatal(self):
        from custom_components.smartshading.config_entry_data import _lifecycle_profiles_from_storage
        raw = {
            "p1": {"display_name": "A", "config": {"id": "p1", "night_position": 1}},
            "p2": "not-a-dict-at-all",
            "p3": {"display_name": "C"},  # missing "config" entirely
        }
        profiles = _lifecycle_profiles_from_storage(raw)
        assert set(profiles.keys()) == {"p1"}

    def test_a_profile_is_always_complete_after_load_by_dataclass_construction(self):
        """Pre-push review point 2: is a profile guaranteed complete after a
        storage round-trip? Yes — _lifecycle_profiles_from_storage() always
        constructs a full NightDayLifecycleConfig dataclass instance via
        .get(key, default), so partial/incomplete raw data can never
        produce a partially-initialized profile in memory; every field has
        a concrete value (either the stored one or the dataclass default)."""
        from custom_components.smartshading.config_entry_data import _lifecycle_profiles_from_storage
        raw = {"p1": {"display_name": "A", "config": {"id": "p1"}}}  # only "id" present
        profiles = _lifecycle_profiles_from_storage(raw)
        cfg = profiles["p1"].config
        # every dataclass field resolved to *some* concrete value, none missing
        import dataclasses
        for f in dataclasses.fields(cfg):
            getattr(cfg, f.name)  # must not raise AttributeError

    def test_unknown_field_does_not_survive_an_edit_and_resave_cycle(self):
        """Documented, accepted limitation (pre-push review point 2): T6
        profiles do not implement field-level schema migration. An unknown
        future field present in raw storage is safely ignored on load
        (proven above), but if that profile is edited and its config is
        rebuilt+resaved via to_storage_dict(), the unknown field is not
        carried forward — identical to how the pre-existing legacy
        lifecycle_config already behaves for any field the UI does not
        manage. A dedicated field-schema migration is out of scope for T6
        and would be a separate, explicit follow-up ticket if ever needed."""
        from custom_components.smartshading.config_entry_data import (
            _lifecycle_profiles_from_storage,
            _lifecycle_config_to_storage_dict,
        )
        raw = {
            "p1": {
                "display_name": "A",
                "config": {"id": "p1", "night_position": 40, "future_field_v2": "x"},
            }
        }
        profiles = _lifecycle_profiles_from_storage(raw)
        resaved = _lifecycle_config_to_storage_dict(profiles["p1"].config)
        assert "future_field_v2" not in resaved


# ---------------------------------------------------------------------------
# LP-14 — legacy flat fields unaffected by profiles.
# ---------------------------------------------------------------------------

class TestLegacyFieldsUnaffected:
    def test_legacy_lifecycle_config_untouched_regardless_of_profiles(self):
        legacy = _legacy()
        p1 = LifecycleProfile(profile_id="p1", display_name="A", config=NightDayLifecycleConfig(id="p1", night_position=99))
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True, lifecycle_config=legacy,
            lifecycle_profiles={"p1": p1}, active_lifecycle_profile_id="p1",
        )
        stored = to_storage_dict(data)
        assert stored["lifecycle_config"]["night_position"] == legacy.night_position
        assert stored["lifecycle_config"]["night_fixed_time"] == "22:00:00"
        restored = from_storage_dict(stored)
        assert restored.lifecycle_config.night_position == legacy.night_position
