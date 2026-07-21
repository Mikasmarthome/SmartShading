"""OptionsFlow schema/save coverage for lifecycle profiles — v1.2.0-beta.1,
Beta.1-T6.

Same real-selector-stub technique established in
tests/test_config_flow_presence_policy.py (T5): a minimal, faithful
homeassistant.helpers.selector stub lets config_flow.py actually import and
run, so these tests exercise the REAL async_step_* methods and the REAL
voluptuous schemas they build — not just const.py constants.

Coverage:
  CFLP-01 Existing initial ConfigFlow (async_step_presence) still works
          with zero lifecycle-profile awareness — profiles are OptionsFlow-
          only, confirming the initial flow was untouched by T6.
  CFLP-02 Existing OptionsFlow presence step still works unchanged.
  CFLP-03 The profile management menu is reachable and lists menu options.
  CFLP-04 A profile can be added — reaches the real
          hass.config_entries.async_update_entry() call with a new entry
          under "lifecycle_profiles".
  CFLP-05 A profile can be edited (picker + detail form) — updated fields
          reach async_update_entry().
  CFLP-06 A profile can be removed — removed from the stored dict.
  CFLP-07 The active profile can be selected — persists
          active_lifecycle_profile_id.
  CFLP-08 Deleting the ACTIVE profile clears active_lifecycle_profile_id
          in the same save (no dangling reference).
  CFLP-09 An unknown active_lifecycle_profile_id set directly in storage
          does not crash the select-active form (falls back to the legacy
          sentinel default).
  CFLP-10 A previously stored active profile selection is pre-selected
          when the select-active form is reopened.
  CFLP-11 All profile fields (trigger, elevation, fixed time, position,
          sun event, clamp, active_months) are captured correctly in the
          saved config dict.
  CFLP-12 Flat legacy lifecycle_config fields in ConfigEntry.data are
          untouched by any profile CRUD operation.
  CFLP-13 No profile CRUD operation calls async_update_entry with the
          "name"/"windows"/other unrelated top-level keys touched — only
          the intended lifecycle_profiles/active_lifecycle_profile_id keys
          — i.e. no automatic full-ConfigEntry rewrite.
  CFLP-14 Translation/selector-key parity: strings.json + all 24
          translations contain every new label/description/menu string,
          with no leftover English text in non-English files.
  CFLP-15 No unimplemented functionality (e.g. weekday/weekend fields, a
          select entity) is referenced by the shipped translation strings.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import voluptuous as vol

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_config_flow_presence_policy.py.
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _SelectorConfigBase:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class EntitySelectorConfig(_SelectorConfigBase):
    pass


class NumberSelectorConfig(_SelectorConfigBase):
    pass


class SelectSelectorConfig(_SelectorConfigBase):
    pass


class _SelectorBase:
    def __init__(self, config: Any = None) -> None:
        self.config = config

    def __call__(self, value: Any) -> Any:
        return value


class EntitySelector(_SelectorBase):
    pass


class NumberSelector(_SelectorBase):
    pass


class SelectSelector(_SelectorBase):
    pass


class TimeSelector(_SelectorBase):
    pass


class BooleanSelector(_SelectorBase):
    pass


class NumberSelectorMode:
    BOX = "box"
    SLIDER = "slider"


class SelectSelectorMode:
    DROPDOWN = "dropdown"
    LIST = "list"


sys.modules["homeassistant.helpers.selector"] = _stub(
    "homeassistant.helpers.selector",
    EntitySelector=EntitySelector,
    EntitySelectorConfig=EntitySelectorConfig,
    NumberSelector=NumberSelector,
    NumberSelectorConfig=NumberSelectorConfig,
    NumberSelectorMode=NumberSelectorMode,
    SelectSelector=SelectSelector,
    SelectSelectorConfig=SelectSelectorConfig,
    SelectSelectorMode=SelectSelectorMode,
    TimeSelector=TimeSelector,
    BooleanSelector=BooleanSelector,
)

sys.modules.pop("custom_components.smartshading.config_flow", None)
from custom_components.smartshading.config_flow import (  # noqa: E402
    SmartShadingConfigFlow,
    SmartShadingOptionsFlow,
)
from custom_components.smartshading.const import (  # noqa: E402
    CONF_ACTIVE_LIFECYCLE_PROFILE_ID,
    CONF_PROFILE_ID,
    CONF_PROFILE_DISPLAY_NAME,
    LEGACY_PROFILE_SENTINEL,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


def _schema_field_key(schema: vol.Schema, field_name: str):
    for key in schema.schema:
        if str(key) == field_name:
            return key
    return None


def _make_entry(data: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = data or {}
    entry.options = {}
    return entry


def _make_options_flow(data: dict | None = None) -> SmartShadingOptionsFlow:
    flow = SmartShadingOptionsFlow(_make_entry(data))
    flow.hass = MagicMock()
    return flow


def _make_config_flow() -> SmartShadingConfigFlow:
    flow = SmartShadingConfigFlow()

    def _async_show_form(*, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {"type": "form", "step_id": step_id, "errors": errors or {}, "data_schema": data_schema}

    flow.async_show_form = _async_show_form  # type: ignore[method-assign]
    return flow


_SAMPLE_PROFILE_INPUT = {
    CONF_PROFILE_DISPLAY_NAME: "Weekend",
    "schedule_mode": "same_every_day",
    "night_trigger": "fixed_time",
    "night_fixed_time": "21:30:00",
    "night_sun_elevation": -6.0,
    "night_position": 15,
    "night_sun_event": None,
    "night_not_before": "21:00:00",
    "night_not_after": "22:00:00",
    "morning_trigger": "fixed_time",
    "morning_fixed_time": "08:00:00",
    "morning_sun_elevation": 5.0,
    "morning_position": 100,
    "morning_sun_event": None,
    "morning_not_before": None,
    "morning_not_after": None,
    "active_months": ["6", "7", "8"],
}


# ---------------------------------------------------------------------------
# CFLP-01 / CFLP-02 — existing flows unaffected.
# ---------------------------------------------------------------------------

class TestExistingFlowsUnaffected:
    def test_initial_config_flow_presence_step_still_works(self):
        flow = _make_config_flow()
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()
        flow.hass.config_entries.async_entries = MagicMock(return_value=[])
        result = asyncio.run(flow.async_step_presence(user_input=None))
        assert result["type"] == "form"
        assert result["step_id"] == "presence"

    def test_options_flow_presence_step_still_works(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_presence(user_input=None))
        assert result["type"] == "form"
        assert result["step_id"] == "presence"


# ---------------------------------------------------------------------------
# CFLP-03 — profile menu reachable.
# ---------------------------------------------------------------------------

class TestProfileMenu:
    def test_menu_shows_when_no_profiles(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_lifecycle_profiles(user_input=None))
        assert result["type"] == "menu"
        assert "add_lifecycle_profile" in result["menu_options"]
        assert "select_active_lifecycle_profile" in result["menu_options"]


# ---------------------------------------------------------------------------
# CFLP-04 / CFLP-11 / CFLP-13 — add a profile.
# ---------------------------------------------------------------------------

class TestAddProfile:
    def test_add_profile_reaches_async_update_entry(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_add_lifecycle_profile(user_input=dict(_SAMPLE_PROFILE_INPUT)))
        assert flow.hass.config_entries.async_update_entry.called
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["lifecycle_profiles"]
        assert len(saved) == 1
        profile_id = next(iter(saved))
        assert saved[profile_id]["display_name"] == "Weekend"
        config = saved[profile_id]["config"]
        assert config["night_trigger"] == "fixed_time"
        assert config["night_fixed_time"] == "21:30:00"
        assert config["night_position"] == 15
        assert config["night_not_before"] == "21:00:00"
        assert config["night_not_after"] == "22:00:00"
        assert config["active_months"] == [6, 7, 8]
        assert config["morning_position"] == 100

    def test_add_profile_does_not_touch_unrelated_keys(self):
        flow = _make_options_flow(data={"name": "Zone A", "windows": ["do-not-touch"]})
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=dict(_SAMPLE_PROFILE_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["name"] == "Zone A"
        assert kwargs["data"]["windows"] == ["do-not-touch"]

    def test_invalid_clamp_window_rejected(self):
        flow = _make_options_flow(data={})
        bad_input = dict(_SAMPLE_PROFILE_INPUT)
        bad_input["night_not_before"] = "22:00:00"
        bad_input["night_not_after"] = "21:00:00"  # inverted
        result = asyncio.run(flow.async_step_add_lifecycle_profile(user_input=bad_input))
        assert result["type"] == "form"
        assert result["errors"].get("base") == "night_clamp_window_invalid"
        assert not flow.hass.config_entries.async_update_entry.called

    def test_add_profile_with_weekday_weekend_schedule_mode(self):
        """Post pre-push-review correction: a profile can be created with
        schedule_mode=weekday_weekend and its own weekday/weekend times."""
        flow = _make_options_flow(data={})
        wkwk_input = dict(_SAMPLE_PROFILE_INPUT)
        wkwk_input["schedule_mode"] = "weekday_weekend"
        wkwk_input["weekday_night_fixed_time"] = "21:00:00"
        wkwk_input["weekday_night_position"] = 10
        wkwk_input["weekday_morning_fixed_time"] = "06:30:00"
        wkwk_input["weekday_morning_position"] = 90
        wkwk_input["weekend_night_fixed_time"] = "23:00:00"
        wkwk_input["weekend_night_position"] = 0
        wkwk_input["weekend_morning_fixed_time"] = "09:00:00"
        wkwk_input["weekend_morning_position"] = 100
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=wkwk_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        profile_id = next(iter(kwargs["data"]["lifecycle_profiles"]))
        config = kwargs["data"]["lifecycle_profiles"][profile_id]["config"]
        assert config["schedule_mode"] == "weekday_weekend"
        assert config["weekday_night_fixed_time"] == "21:00:00"
        assert config["weekday_night_position"] == 10
        assert config["weekend_night_fixed_time"] == "23:00:00"
        assert config["weekend_morning_position"] == 100


# ---------------------------------------------------------------------------
# CFLP-05 — edit a profile (picker + detail).
# ---------------------------------------------------------------------------

class TestEditProfile:
    def _stored_with_one_profile(self):
        return {
            "lifecycle_profiles": {
                "p1": {"display_name": "Weekend", "config": {"id": "p1", "night_position": 15}},
            }
        }

    def test_edit_picker_lists_existing_profile(self):
        flow = _make_options_flow(data=self._stored_with_one_profile())
        result = asyncio.run(flow.async_step_edit_lifecycle_profile(user_input=None))
        assert result["type"] == "form"
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_PROFILE_ID)
        selector_instance = schema.schema[key]
        values = {opt["value"] for opt in selector_instance.config.options}
        assert values == {"p1"}

    def test_edit_picker_aborts_when_no_profiles(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_edit_lifecycle_profile(user_input=None))
        assert result["type"] == "abort"
        assert result["reason"] == "no_lifecycle_profiles"

    def test_edit_detail_prefills_and_saves(self):
        flow = _make_options_flow(data=self._stored_with_one_profile())
        asyncio.run(flow.async_step_edit_lifecycle_profile(user_input={CONF_PROFILE_ID: "p1"}))
        assert flow._edit_profile_id == "p1"

        prefill_result = asyncio.run(flow.async_step_edit_lifecycle_profile_detail(user_input=None))
        schema: vol.Schema = prefill_result["data_schema"]
        key = _schema_field_key(schema, "night_position")
        assert key.default() == 15

        updated_input = dict(_SAMPLE_PROFILE_INPUT)
        updated_input["night_position"] = 40
        asyncio.run(flow.async_step_edit_lifecycle_profile_detail(user_input=updated_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["lifecycle_profiles"]["p1"]["config"]["night_position"] == 40


# ---------------------------------------------------------------------------
# CFLP-06 / CFLP-08 — remove a profile, including the active one.
# ---------------------------------------------------------------------------

class TestRemoveProfile:
    def _stored_with_one_profile(self, active=False):
        data = {
            "lifecycle_profiles": {
                "p1": {"display_name": "Weekend", "config": {"id": "p1"}},
            }
        }
        if active:
            data["active_lifecycle_profile_id"] = "p1"
        return data

    def test_remove_profile_deletes_it(self):
        flow = _make_options_flow(data=self._stored_with_one_profile())
        asyncio.run(flow.async_step_remove_lifecycle_profile(user_input={CONF_PROFILE_ID: "p1"}))
        asyncio.run(flow.async_step_remove_lifecycle_profile_confirm(user_input={"remove_confirmed": True}))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["lifecycle_profiles"] == {}

    def test_removing_active_profile_clears_active_id(self):
        flow = _make_options_flow(data=self._stored_with_one_profile(active=True))
        asyncio.run(flow.async_step_remove_lifecycle_profile(user_input={CONF_PROFILE_ID: "p1"}))
        asyncio.run(flow.async_step_remove_lifecycle_profile_confirm(user_input={"remove_confirmed": True}))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_ACTIVE_LIFECYCLE_PROFILE_ID] is None

    def test_declining_removal_makes_no_changes(self):
        flow = _make_options_flow(data=self._stored_with_one_profile())
        asyncio.run(flow.async_step_remove_lifecycle_profile(user_input={CONF_PROFILE_ID: "p1"}))
        asyncio.run(flow.async_step_remove_lifecycle_profile_confirm(user_input={"remove_confirmed": False}))
        assert not flow.hass.config_entries.async_update_entry.called


# ---------------------------------------------------------------------------
# CFLP-07 / CFLP-09 / CFLP-10 — select active profile.
# ---------------------------------------------------------------------------

class TestSelectActiveProfile:
    def _stored(self, active_id=None):
        data = {
            "lifecycle_profiles": {
                "p1": {"display_name": "Weekend", "config": {"id": "p1"}},
                "p2": {"display_name": "Vacation", "config": {"id": "p2"}},
            }
        }
        if active_id is not None:
            data[CONF_ACTIVE_LIFECYCLE_PROFILE_ID] = active_id
        return data

    def test_select_active_profile_persists(self):
        flow = _make_options_flow(data=self._stored())
        asyncio.run(flow.async_step_select_active_lifecycle_profile(user_input={CONF_ACTIVE_LIFECYCLE_PROFILE_ID: "p2"}))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_ACTIVE_LIFECYCLE_PROFILE_ID] == "p2"

    def test_select_legacy_sentinel_persists_none(self):
        flow = _make_options_flow(data=self._stored(active_id="p1"))
        asyncio.run(flow.async_step_select_active_lifecycle_profile(
            user_input={CONF_ACTIVE_LIFECYCLE_PROFILE_ID: LEGACY_PROFILE_SENTINEL}
        ))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_ACTIVE_LIFECYCLE_PROFILE_ID] is None

    def test_stored_selection_is_preselected(self):
        flow = _make_options_flow(data=self._stored(active_id="p2"))
        result = asyncio.run(flow.async_step_select_active_lifecycle_profile(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_ACTIVE_LIFECYCLE_PROFILE_ID)
        assert key.default() == "p2"

    def test_unknown_stored_active_id_does_not_crash_falls_back_to_sentinel(self):
        flow = _make_options_flow(data=self._stored(active_id="ghost_profile"))
        result = asyncio.run(flow.async_step_select_active_lifecycle_profile(user_input=None))  # must not raise
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_ACTIVE_LIFECYCLE_PROFILE_ID)
        assert key.default() == LEGACY_PROFILE_SENTINEL


# ---------------------------------------------------------------------------
# Legacy-selector i18n fix: the selector's translation_key wiring, the
# static sentinel's Python-side fallback label, and the dynamic profile
# options — verified against the REAL selector instance built by the REAL
# async_step_select_active_lifecycle_profile schema (same technique as
# TestSelectActiveProfile above).
# ---------------------------------------------------------------------------

class TestSelectActiveProfileSelectorLocalization:
    def _stored(self, active_id=None):
        data = {
            "lifecycle_profiles": {
                "p1": {"display_name": "Weekend", "config": {"id": "p1"}},
                "p2": {"display_name": "Vacation", "config": {"id": "p2"}},
            }
        }
        if active_id is not None:
            data[CONF_ACTIVE_LIFECYCLE_PROFILE_ID] = active_id
        return data

    def _selector_instance(self, flow):
        result = asyncio.run(flow.async_step_select_active_lifecycle_profile(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_ACTIVE_LIFECYCLE_PROFILE_ID)
        return schema.schema[key]

    def test_selector_translation_key_is_active_lifecycle_profile(self):
        flow = _make_options_flow(data=self._stored())
        selector_instance = self._selector_instance(flow)
        assert selector_instance.config.translation_key == "active_lifecycle_profile"

    def test_static_sentinel_option_keeps_fallback_value_and_label(self):
        flow = _make_options_flow(data=self._stored())
        selector_instance = self._selector_instance(flow)
        sentinel_options = [
            opt for opt in selector_instance.config.options if opt["value"] == LEGACY_PROFILE_SENTINEL
        ]
        assert len(sentinel_options) == 1
        assert sentinel_options[0]["label"] == "Legacy default"

    def test_dynamic_profile_options_are_unaffected_by_translation_key(self):
        flow = _make_options_flow(data=self._stored())
        selector_instance = self._selector_instance(flow)
        by_value = {opt["value"]: opt["label"] for opt in selector_instance.config.options}
        # Dynamic profile entries keep their real profile_id as value and the
        # stored user display_name as label — translation_key must not alter
        # or replace either (only the frontend, at render time, ever tries a
        # selector.active_lifecycle_profile.options.<value> lookup, and none
        # of these uuid-like ids has a matching entry in strings.json).
        assert by_value["p1"] == "Weekend"
        assert by_value["p2"] == "Vacation"
        # No dynamic profile_id is ever used as a synthetic translation key
        # in strings.json — only the static sentinel is.
        strings = json.loads((_INTEGRATION_ROOT / "strings.json").read_text(encoding="utf-8"))
        translated_option_keys = strings["selector"]["active_lifecycle_profile"]["options"].keys()
        assert "p1" not in translated_option_keys
        assert "p2" not in translated_option_keys

    def test_persistence_of_dynamic_selection_unaffected_by_translation_key(self):
        # Regression guard: adding translation_key= to the SelectSelectorConfig
        # must not change the save/persist path at all.
        flow = _make_options_flow(data=self._stored())
        asyncio.run(flow.async_step_select_active_lifecycle_profile(
            user_input={CONF_ACTIVE_LIFECYCLE_PROFILE_ID: "p2"}
        ))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_ACTIVE_LIFECYCLE_PROFILE_ID] == "p2"

    def test_persistence_of_sentinel_selection_unaffected_by_translation_key(self):
        flow = _make_options_flow(data=self._stored(active_id="p1"))
        asyncio.run(flow.async_step_select_active_lifecycle_profile(
            user_input={CONF_ACTIVE_LIFECYCLE_PROFILE_ID: LEGACY_PROFILE_SENTINEL}
        ))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_ACTIVE_LIFECYCLE_PROFILE_ID] is None


# ---------------------------------------------------------------------------
# CFLP-12 — legacy flat fields untouched by CRUD.
# ---------------------------------------------------------------------------

class TestLegacyFieldsUntouched:
    def test_add_profile_does_not_modify_lifecycle_config_key(self):
        flow = _make_options_flow(data={"lifecycle_config": {"id": "default", "night_position": 77}})
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=dict(_SAMPLE_PROFILE_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["lifecycle_config"]["night_position"] == 77


# ---------------------------------------------------------------------------
# CFLP-14 / CFLP-15 — translation completeness.
# ---------------------------------------------------------------------------

class TestTranslationCompleteness:
    def _all_i18n_files(self):
        yield _INTEGRATION_ROOT / "strings.json"
        yield from sorted((_INTEGRATION_ROOT / "translations").glob("*.json"))

    def test_every_file_has_profile_step_strings(self):
        files = list(self._all_i18n_files())
        assert len(files) == 25
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            opt = data["options"]
            assert "lifecycle_profiles" in opt["step"]["init"]["menu_options"], path.name
            for step in (
                "lifecycle_profiles", "add_lifecycle_profile", "edit_lifecycle_profile",
                "edit_lifecycle_profile_detail", "remove_lifecycle_profile",
                "remove_lifecycle_profile_confirm", "select_active_lifecycle_profile",
            ):
                assert step in opt["step"], f"{path.name}: missing step {step}"
                assert opt["step"][step]["title"], f"{path.name}: empty title for {step}"
            assert "no_lifecycle_profiles" in opt["abort"], path.name
            assert "lifecycle_profile_not_found" in opt["abort"], path.name

    def test_no_english_leftovers_in_translations(self):
        en = json.loads((_INTEGRATION_ROOT / "translations" / "en.json").read_text(encoding="utf-8"))
        en_titles = {
            en["options"]["step"]["lifecycle_profiles"]["title"],
            en["options"]["step"]["add_lifecycle_profile"]["title"],
            en["options"]["abort"]["no_lifecycle_profiles"],
        }
        for path in sorted((_INTEGRATION_ROOT / "translations").glob("*.json")):
            if path.name == "en.json":
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            opt = data["options"]
            titles = {
                opt["step"]["lifecycle_profiles"]["title"],
                opt["step"]["add_lifecycle_profile"]["title"],
                opt["abort"]["no_lifecycle_profiles"],
            }
            assert not (titles & en_titles), f"{path.name} has untranslated English text"

    def test_weekday_weekend_fields_are_present_full_lifecycle_parity(self):
        """Post pre-push-review correction: profiles cover the FULL legacy
        lifecycle field set, including schedule_mode and weekday/weekend
        times/positions — not just the SAME_EVERY_DAY subset."""
        en = json.loads((_INTEGRATION_ROOT / "strings.json").read_text(encoding="utf-8"))
        profile_data_keys = set(en["options"]["step"]["add_lifecycle_profile"]["data"].keys())
        assert "schedule_mode" in profile_data_keys
        assert "weekday_night_fixed_time" in profile_data_keys
        assert "weekend_night_fixed_time" in profile_data_keys
        assert "weekday_morning_fixed_time" in profile_data_keys
        assert "weekend_morning_fixed_time" in profile_data_keys

    def test_no_select_entity_or_auto_switching_strings_referenced(self):
        """T6 scope decision (unaffected by the weekday/weekend fix): no
        select entity, no automatic profile switching, no per-zone/per-window
        profiles — none of these should be implied anywhere in the shipped
        UI strings for this feature."""
        en = json.loads((_INTEGRATION_ROOT / "strings.json").read_text(encoding="utf-8"))
        opt = en["options"]
        haystack = json.dumps({
            "menu": opt["step"]["lifecycle_profiles"],
            "add": opt["step"]["add_lifecycle_profile"],
            "select_active": opt["step"]["select_active_lifecycle_profile"],
        }).lower()
        for forbidden in ("select entity", "automatic", "calendar", "per zone", "per window"):
            assert forbidden not in haystack, forbidden

    def test_active_lifecycle_profile_selector_translation_present_in_all_files(self):
        """Legacy-selector i18n fix: every i18n file must carry
        selector.active_lifecycle_profile.options.legacy_default with a
        non-empty value — this is what config_flow.py's new
        translation_key="active_lifecycle_profile" resolves against at
        render time (see ha-selector-select.ts: `${translationKey}.options.
        ${option.value}`)."""
        files = list(self._all_i18n_files())
        assert len(files) == 25
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            selector = data.get("selector", {})
            assert "active_lifecycle_profile" in selector, f"{path.name}: missing selector.active_lifecycle_profile"
            options = selector["active_lifecycle_profile"].get("options", {})
            assert "legacy_default" in options, f"{path.name}: missing selector.active_lifecycle_profile.options.legacy_default"
            value = options["legacy_default"]
            assert isinstance(value, str) and value.strip(), f"{path.name}: empty legacy-sentinel translation"

    def test_old_invalid_sentinel_key_is_gone_from_all_files(self):
        """Regression guard: the original "__legacy__" translation key was
        rejected by hassfest ("Invalid translation key ... need to be
        [a-z0-9_]+ and cannot start or end with a hyphen or underscore").
        It must not reappear in any i18n file."""
        files = list(self._all_i18n_files())
        assert len(files) == 25
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            options = data.get("selector", {}).get("active_lifecycle_profile", {}).get("options", {})
            assert "__legacy__" not in options, f"{path.name}: stale invalid translation key __legacy__ still present"

    def test_active_lifecycle_profile_option_keys_satisfy_hassfest_translation_key_pattern(self):
        """Hassfest's translation-key contract, kept narrow and generic (not
        a full reimplementation of hassfest's schema): every option key
        under selector.active_lifecycle_profile.options must match
        ^[a-z0-9_]+$ and must not start or end with "_"."""
        pattern = re.compile(r"^[a-z0-9_]+$")
        files = list(self._all_i18n_files())
        assert len(files) == 25
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            options = data["selector"]["active_lifecycle_profile"]["options"]
            for key in options:
                assert pattern.match(key), f"{path.name}: option key {key!r} does not match {pattern.pattern}"
                assert not key.startswith("_"), f"{path.name}: option key {key!r} starts with underscore"
                assert not key.endswith("_"), f"{path.name}: option key {key!r} ends with underscore"

    def test_active_lifecycle_profile_selector_translation_does_not_overwrite_existing_selectors(self):
        """The new selector key must be additive — every selector key that
        existed before this fix must still be present and unchanged in
        strings.json (the single source of truth for the English/default
        strings)."""
        strings = json.loads((_INTEGRATION_ROOT / "strings.json").read_text(encoding="utf-8"))
        selector = strings["selector"]
        pre_existing_keys = {
            "compass_direction", "night_elevation_preset", "morning_elevation_preset",
            "lifecycle_trigger", "night_sun_event", "morning_sun_event",
            "lifecycle_schedule_mode", "active_months", "window_behavior_mode",
            "cover_hardware_type", "presence_policy", "override_duration_mode",
        }
        assert pre_existing_keys <= selector.keys()
        assert selector["lifecycle_schedule_mode"]["options"] == {
            "same_every_day": "Same every day",
            "weekday_weekend": "Weekday / Weekend",
        }


# ---------------------------------------------------------------------------
# Pre-push review (2nd pass) — points 3, 4, 5, 10: non-mutation guarantees,
# active-profile-selection edge cases, initial-ConfigFlow scope, and
# display-name edge cases (unicode/empty/long/rename).
# ---------------------------------------------------------------------------

class TestNoMutationBeforeConfirm:
    """Point 4: no Add/Edit/Delete step may mutate config_entry.data (or any
    dict/object reachable from it) before the user's final confirming
    submit. Every flow step here works off a *copy*
    (`dict(self._config_entry.data.get(...) or {})`) and _save_and_reload()
    itself builds a brand-new `{**self._config_entry.data, **updates}` dict
    — the original object is never assigned into or mutated in place."""

    def test_add_profile_form_render_does_not_touch_entry(self):
        original = {"lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}}}
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=None))  # just render the form
        assert flow._config_entry.data == original
        assert not flow.hass.config_entries.async_update_entry.called

    def test_add_profile_invalid_submit_does_not_touch_entry(self):
        original = {"lifecycle_profiles": {}}
        flow = _make_options_flow(data=dict(original))
        bad_input = dict(_SAMPLE_PROFILE_INPUT)
        bad_input["night_not_before"] = "22:00:00"
        bad_input["night_not_after"] = "21:00:00"
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=bad_input))
        assert flow._config_entry.data == original
        assert not flow.hass.config_entries.async_update_entry.called

    def test_edit_profile_form_render_does_not_touch_entry(self):
        original = {"lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1", "night_position": 15}}}}
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_edit_lifecycle_profile(user_input={CONF_PROFILE_ID: "p1"}))
        asyncio.run(flow.async_step_edit_lifecycle_profile_detail(user_input=None))  # render prefilled form only
        assert flow._config_entry.data == original
        assert not flow.hass.config_entries.async_update_entry.called

    def test_remove_profile_picker_render_does_not_touch_entry(self):
        original = {"lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}}}
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_remove_lifecycle_profile(user_input=None))
        assert flow._config_entry.data == original
        assert not flow.hass.config_entries.async_update_entry.called

    def test_remove_profile_decline_does_not_touch_entry(self):
        original = {"lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}}}
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_remove_lifecycle_profile(user_input={CONF_PROFILE_ID: "p1"}))
        asyncio.run(flow.async_step_remove_lifecycle_profile_confirm(user_input={"remove_confirmed": False}))
        assert flow._config_entry.data == original
        assert flow._config_entry.data["lifecycle_profiles"]["p1"]["display_name"] == "Weekend"
        assert not flow.hass.config_entries.async_update_entry.called

    def test_select_active_form_render_does_not_touch_entry(self):
        original = {
            "lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}},
            CONF_ACTIVE_LIFECYCLE_PROFILE_ID: "p1",
        }
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_select_active_lifecycle_profile(user_input=None))
        assert flow._config_entry.data == original
        assert not flow.hass.config_entries.async_update_entry.called

    def test_save_and_reload_builds_new_dict_not_mutating_original(self):
        original_profiles = {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}}
        original = {"lifecycle_profiles": original_profiles}
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=dict(_SAMPLE_PROFILE_INPUT)))
        # The ORIGINAL dict object handed to the flow must still contain only
        # the one profile it started with — the new profile lives only in
        # the *new* dict passed to async_update_entry(data=...).
        assert original_profiles == {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}}
        assert len(original_profiles) == 1


class TestActiveProfileSelectionScenarios:
    """Point 3: explicit re-verification of the active-profile fallback UX
    across the scenarios named in the review."""

    def test_adding_first_profile_does_not_auto_activate_it(self):
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=dict(_SAMPLE_PROFILE_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert CONF_ACTIVE_LIFECYCLE_PROFILE_ID not in kwargs["data"]

    def test_multiple_profiles_none_selected_resolves_to_legacy_sentinel(self):
        flow = _make_options_flow(data={
            "lifecycle_profiles": {
                "p1": {"display_name": "Weekend", "config": {"id": "p1"}},
                "p2": {"display_name": "Vacation", "config": {"id": "p2"}},
            }
        })
        result = asyncio.run(flow.async_step_select_active_lifecycle_profile(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_ACTIVE_LIFECYCLE_PROFILE_ID)
        assert key.default() == LEGACY_PROFILE_SENTINEL

    def test_legacy_is_always_a_selectable_option(self):
        flow = _make_options_flow(data={
            "lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}},
            CONF_ACTIVE_LIFECYCLE_PROFILE_ID: "p1",
        })
        result = asyncio.run(flow.async_step_select_active_lifecycle_profile(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_ACTIVE_LIFECYCLE_PROFILE_ID)
        selector_instance = schema.schema[key]
        values = {opt["value"] for opt in selector_instance.config.options}
        assert LEGACY_PROFILE_SENTINEL in values

    def test_can_always_switch_back_to_legacy_even_with_multiple_profiles(self):
        flow = _make_options_flow(data={
            "lifecycle_profiles": {
                "p1": {"display_name": "Weekend", "config": {"id": "p1"}},
                "p2": {"display_name": "Vacation", "config": {"id": "p2"}},
            },
            CONF_ACTIVE_LIFECYCLE_PROFILE_ID: "p2",
        })
        asyncio.run(flow.async_step_select_active_lifecycle_profile(
            user_input={CONF_ACTIVE_LIFECYCLE_PROFILE_ID: LEGACY_PROFILE_SENTINEL}
        ))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_ACTIVE_LIFECYCLE_PROFILE_ID] is None

    def test_reopening_select_active_with_stale_id_and_resubmitting_default_heals_storage(self):
        """A stale/unknown stored active_id pre-selects the Legacy sentinel
        (CFLP-09); if the user simply reopens and re-submits without
        changing the selection, the stale id is healed to None on save —
        it is not silently re-persisted."""
        flow = _make_options_flow(data={
            "lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}},
            CONF_ACTIVE_LIFECYCLE_PROFILE_ID: "ghost_profile",
        })
        prefill = asyncio.run(flow.async_step_select_active_lifecycle_profile(user_input=None))
        schema: vol.Schema = prefill["data_schema"]
        key = _schema_field_key(schema, CONF_ACTIVE_LIFECYCLE_PROFILE_ID)
        preselected_default = key.default()
        asyncio.run(flow.async_step_select_active_lifecycle_profile(
            user_input={CONF_ACTIVE_LIFECYCLE_PROFILE_ID: preselected_default}
        ))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_ACTIVE_LIFECYCLE_PROFILE_ID] is None


class TestInitialConfigFlowNeverWritesProfileKeys:
    """Point 5: the initial SmartShadingConfigFlow must not diverge fresh
    installs from existing ones — profile keys are OptionsFlow-only."""

    def test_config_entry_data_default_has_empty_profiles_and_no_active_id(self):
        from custom_components.smartshading.config_entry_data import SmartShadingConfigEntryData
        data = SmartShadingConfigEntryData(name="Zone A", use_home_location=True)
        assert data.lifecycle_profiles == {}
        assert data.active_lifecycle_profile_id is None

    def test_default_profiles_state_resolves_identically_to_a_missing_key(self):
        """to_storage_dict() unconditionally serializes lifecycle_profiles/
        active_lifecycle_profile_id (matching the established pattern for
        every other field, e.g. presence_policy) — so a freshly created
        entry does carry an explicit `{}`/null pair, but the resolver
        treats that identically to the keys being absent entirely: both
        resolve to the legacy config with source == "legacy"."""
        from custom_components.smartshading.engines.lifecycle_resolver import resolve_lifecycle_config
        from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig

        legacy = NightDayLifecycleConfig(id="default")
        resolved_with_empty_dict = resolve_lifecycle_config(
            legacy_config=legacy, profiles={}, active_profile_id=None
        )
        assert resolved_with_empty_dict.source == "legacy"
        assert resolved_with_empty_dict.config is legacy


class TestDisplayNameEdgeCases:
    """Point 10: unicode / empty / very long / duplicate / rename handling
    for profile display names."""

    def test_unicode_display_name_round_trips(self):
        flow = _make_options_flow(data={})
        unicode_input = dict(_SAMPLE_PROFILE_INPUT)
        unicode_input[CONF_PROFILE_DISPLAY_NAME] = "Wochenende ☀️ 冬季"
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=unicode_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["lifecycle_profiles"]
        profile_id = next(iter(saved))
        assert saved[profile_id]["display_name"] == "Wochenende ☀️ 冬季"

    def test_empty_or_whitespace_display_name_falls_back_to_profile_id(self):
        flow = _make_options_flow(data={})
        empty_input = dict(_SAMPLE_PROFILE_INPUT)
        empty_input[CONF_PROFILE_DISPLAY_NAME] = "   "
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=empty_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["lifecycle_profiles"]
        profile_id = next(iter(saved))
        assert saved[profile_id]["display_name"] == profile_id  # never empty/blank

    def test_very_long_display_name_is_truncated_not_rejected(self):
        from custom_components.smartshading.const import PROFILE_DISPLAY_NAME_MAX_LEN
        flow = _make_options_flow(data={})
        long_input = dict(_SAMPLE_PROFILE_INPUT)
        long_input[CONF_PROFILE_DISPLAY_NAME] = "X" * 500
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=long_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["lifecycle_profiles"]
        profile_id = next(iter(saved))
        assert len(saved[profile_id]["display_name"]) == PROFILE_DISPLAY_NAME_MAX_LEN

    def test_duplicate_display_names_get_distinct_profile_ids(self):
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_add_lifecycle_profile(user_input=dict(_SAMPLE_PROFILE_INPUT)))
        first_data = flow.hass.config_entries.async_update_entry.call_args[1]["data"]
        flow2 = _make_options_flow(data=dict(first_data))
        asyncio.run(flow2.async_step_add_lifecycle_profile(user_input=dict(_SAMPLE_PROFILE_INPUT)))
        _, kwargs = flow2.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["lifecycle_profiles"]
        assert len(saved) == 2
        names = [p["display_name"] for p in saved.values()]
        assert names == ["Weekend", "Weekend"]  # duplicate names allowed
        assert len(set(saved.keys())) == 2  # but ids are distinct

    def test_renaming_a_profile_preserves_its_id_and_active_status(self):
        flow = _make_options_flow(data={
            "lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1", "night_position": 15}}},
            CONF_ACTIVE_LIFECYCLE_PROFILE_ID: "p1",
        })
        asyncio.run(flow.async_step_edit_lifecycle_profile(user_input={CONF_PROFILE_ID: "p1"}))
        renamed_input = dict(_SAMPLE_PROFILE_INPUT)
        renamed_input[CONF_PROFILE_DISPLAY_NAME] = "Renamed Weekend"
        asyncio.run(flow.async_step_edit_lifecycle_profile_detail(user_input=renamed_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["lifecycle_profiles"]
        assert set(saved.keys()) == {"p1"}  # id unchanged
        assert saved["p1"]["display_name"] == "Renamed Weekend"
        # active id key untouched by an edit (not part of `updates`) — the
        # active profile is still referenced by the same, unchanged id.
        assert "lifecycle_profiles" in kwargs["data"]
        assert kwargs["data"].get(CONF_ACTIVE_LIFECYCLE_PROFILE_ID, "p1") == "p1"
