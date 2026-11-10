"""OptionsFlow coverage for the Manual Override policy pages.

Same real-selector-stub technique established in
tests/test_config_flow_presence_policy.py (T5) / test_config_flow_lifecycle_profile.py (T6).

History: T7 introduced the policy, T21 Phase B reduced the seven stored
release strategies to a 4-mode UI, T21 Phase C2 split the zone's policy into a
gate (System default or own settings + detection tolerance) and a custom form,
and Phase 5 replaced the single seven-field custom form by a short chain of
pages.  The persisted "release_strategy" key and its seven values never changed.

Pages (the System entry's default uses the very same ones):
  page 1  release choice (5 options) + allow comfort / protection + "also
          adjust the safety timeout"
  page 2  override_duration / override_fixed_time / override_decision - only
          the one the choice needs; lifecycle and manual have none
  page 3  override_timeout - only when requested, only for the choices the
          safety timeout affects

Coverage:
  CFMO-01..04  menu, gate defaults, gate save, gate -> page 1
  CFMO-05      page 1 shows only what every choice needs; stored / legacy /
               invalid strategies are pre-selected as what actually runs
  CFMO-06      routing: irrelevant pages are skipped, each detail page saves
               its values and the strategy
  CFMO-07      optional safety-timeout page
  CFMO-08      hidden settings are never reset (partial merge)
  CFMO-09      all seven strategies survive an unchanged save + reload
  CFMO-10      isolation / no mutation / unrelated keys
  CFMO-11      System default pages
  CFMO-12      translations
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import voluptuous as vol

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_config_flow_lifecycle_profile.py.
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
from custom_components.smartshading.config_entry_data import _override_policy_from_storage  # noqa: E402
from custom_components.smartshading.config_flow import SmartShadingOptionsFlow  # noqa: E402
from custom_components.smartshading.const import (  # noqa: E402
    CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS,
    CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS,
    CONF_OVERRIDE_DECISION_FILTER,
    CONF_OVERRIDE_DETECTION_TOLERANCE,
    CONF_OVERRIDE_DURATION_MIN,
    CONF_OVERRIDE_EDIT_TIMEOUTS,
    CONF_OVERRIDE_FIXED_UNTIL,
    CONF_OVERRIDE_NIGHT_DURATION_MIN,
    CONF_OVERRIDE_RELEASE_CHOICE,
    CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED,
    CONF_OVERRIDE_USE_SYSTEM_DEFAULT,
)
from custom_components.smartshading.models.manual_override import (  # noqa: E402
    RELEASE_CHOICES,
    OverrideReleaseStrategy,
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


def _field_names(schema: vol.Schema) -> set[str]:
    return {str(k) for k in schema.schema}


def _page1(choice, comfort=False, protection=False, edit_timeouts=False) -> dict:
    return {
        CONF_OVERRIDE_RELEASE_CHOICE: choice,
        CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: comfort,
        CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: protection,
        CONF_OVERRIDE_EDIT_TIMEOUTS: edit_timeouts,
    }


def _opt_out_of_system_default(flow: SmartShadingOptionsFlow, detection_tolerance: int = 15):
    """Drives the gate step with use_system_default=False, landing on page 1
    of the zone's own policy."""
    return asyncio.run(flow.async_step_manual_override(user_input={
        CONF_OVERRIDE_USE_SYSTEM_DEFAULT: False,
        CONF_OVERRIDE_DETECTION_TOLERANCE: detection_tolerance,
    }))


def _zone_page1(flow, **kw):
    return asyncio.run(flow.async_step_manual_override_custom(user_input=_page1(**kw)))


def _system_page1(flow, **kw):
    return asyncio.run(flow.async_step_system_manual_override(user_input=_page1(**kw)))


def _detail(flow, step: str, user_input):
    return asyncio.run(getattr(flow, f"async_step_{step}")(user_input=user_input))


def _saved(flow, key="override_policy") -> dict:
    _, kwargs = flow.hass.config_entries.async_update_entry.call_args
    return kwargs["data"][key]


def _not_saved(flow) -> bool:
    return not flow.hass.config_entries.async_update_entry.called


_STORED_FULL = {
    "release_strategy": "fixed_time", "fixed_until": "09:00:00", "allow_comfort_actions": True,
    "allow_protection_actions": False, "duration_min": 33, "night_duration_min": 444,
    "safety_timeout_enabled": False, "detection_tolerance": 25, "use_system_default": False,
}


class TestMenuReachability:
    def test_manual_override_in_init_menu(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_init(user_input=None))
        assert result["type"] == "menu"
        assert "advanced" in result["menu_options"]
        advanced = asyncio.run(flow.async_step_advanced(user_input=None))
        assert advanced["type"] == "menu"
        assert "manual_override" in advanced["menu_options"]


class TestGateDefaults:
    def test_unconfigured_zone_defaults_to_use_system_default(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        assert result["type"] == "form"
        assert result["step_id"] == "manual_override"
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_USE_SYSTEM_DEFAULT).default() is True
        assert _schema_field_key(schema, CONF_OVERRIDE_DETECTION_TOLERANCE).default() == 10

    def test_zone_with_explicit_pre_c2_strategy_defaults_to_its_own_settings(self):
        flow = _make_options_flow(data={
            "override_policy": {"release_strategy": "fixed_time", "detection_tolerance": 25},
        })
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_USE_SYSTEM_DEFAULT).default() is False
        assert _schema_field_key(schema, CONF_OVERRIDE_DETECTION_TOLERANCE).default() == 25

    def test_explicit_stored_use_system_default_flag_wins(self):
        flow = _make_options_flow(data={
            "override_policy": {"release_strategy": "manual", "use_system_default": True},
        })
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_USE_SYSTEM_DEFAULT).default() is True


class TestGateSaveUsingSystemDefault:
    def test_saves_flag_and_tolerance_without_advancing(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_manual_override(user_input={
            CONF_OVERRIDE_USE_SYSTEM_DEFAULT: True,
            CONF_OVERRIDE_DETECTION_TOLERANCE: 30,
        }))
        assert result["type"] == "create_entry"
        saved = _saved(flow)
        assert saved["use_system_default"] is True
        assert saved["detection_tolerance"] == 30

    def test_preserves_previously_stored_custom_fields_as_inert_prefill(self):
        flow = _make_options_flow(data={
            "override_policy": {
                "release_strategy": "fixed_time", "fixed_until": "09:00:00",
                "duration_min": 77, "use_system_default": False,
            },
        })
        asyncio.run(flow.async_step_manual_override(user_input={
            CONF_OVERRIDE_USE_SYSTEM_DEFAULT: True,
            CONF_OVERRIDE_DETECTION_TOLERANCE: 10,
        }))
        saved = _saved(flow)
        assert saved["use_system_default"] is True
        assert saved["release_strategy"] == "fixed_time"
        assert saved["duration_min"] == 77


class TestGateAdvancesToCustomStep:
    def test_opting_out_advances_to_page_one(self):
        flow = _make_options_flow(data={})
        result = _opt_out_of_system_default(flow)
        assert result["type"] == "form"
        assert result["step_id"] == "manual_override_custom"
        assert _not_saved(flow)


class TestPageOneShowsOnlyWhatEveryChoiceNeeds:
    def test_fields_and_defaults_when_nothing_is_stored(self):
        flow = _make_options_flow(data={})
        result = _opt_out_of_system_default(flow)
        schema: vol.Schema = result["data_schema"]
        assert _field_names(schema) == {
            CONF_OVERRIDE_RELEASE_CHOICE, CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS,
            CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS, CONF_OVERRIDE_EDIT_TIMEOUTS,
        }
        assert _schema_field_key(schema, CONF_OVERRIDE_RELEASE_CHOICE).default() == "lifecycle"
        assert _schema_field_key(schema, CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS).default() is False
        assert _schema_field_key(schema, CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS).default() is False
        assert _schema_field_key(schema, CONF_OVERRIDE_EDIT_TIMEOUTS).default() is False

    def test_the_selector_offers_exactly_the_five_choices(self):
        flow = _make_options_flow(data={})
        schema: vol.Schema = _opt_out_of_system_default(flow)["data_schema"]
        key = _schema_field_key(schema, CONF_OVERRIDE_RELEASE_CHOICE)
        cfg = schema.schema[key].config
        assert list(cfg.options) == ["duration", "fixed_time", "lifecycle", "next_decision", "manual"]
        assert cfg.translation_key == "override_release_choice"

    def test_no_irrelevant_field_is_on_the_first_page(self):
        flow = _make_options_flow(data={})
        names = _field_names(_opt_out_of_system_default(flow)["data_schema"])
        for gone in (CONF_OVERRIDE_DURATION_MIN, CONF_OVERRIDE_NIGHT_DURATION_MIN, CONF_OVERRIDE_FIXED_UNTIL,
                     CONF_OVERRIDE_DECISION_FILTER, CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED,
                     "override_release_mode", "override_time_based_kind"):
            assert gone not in names

    @pytest.mark.parametrize("stored,expected", [
        ("duration", "duration"), ("fixed_time", "fixed_time"), ("lifecycle", "lifecycle"),
        ("first_comfort", "next_decision"), ("first_protection", "next_decision"),
        ("first_any_decision", "next_decision"), ("manual", "manual"),
    ])
    def test_every_stored_strategy_is_preselected(self, stored, expected):
        flow = _make_options_flow(data={"override_policy": {
            "release_strategy": stored, "fixed_until": "08:00:00", "use_system_default": False}})
        schema = _opt_out_of_system_default(flow)["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_RELEASE_CHOICE).default() == expected

    @pytest.mark.parametrize("policy,expected", [
        ({"release_strategy": "bogus"}, "lifecycle"),
        ({"release_strategy": "fixed_time"}, "duration"),            # no time -> runtime falls back to duration
        ({"break_on_lifecycle": False}, "duration"),                  # pre-T10 format
        ({"break_on_lifecycle": False, "duration_mode": "fixed_time", "fixed_until": "06:00:00"}, "fixed_time"),
        ({"break_on_lifecycle": True}, "lifecycle"),
        ({}, "lifecycle"),
    ])
    def test_legacy_and_invalid_storage_preselect_what_runs(self, policy, expected):
        flow = _make_options_flow(data={"override_policy": {**policy, "use_system_default": False}})
        schema = _opt_out_of_system_default(flow)["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_RELEASE_CHOICE).default() == expected

    def test_stored_allow_flags_are_prefilled(self):
        flow = _make_options_flow(data={"override_policy": dict(_STORED_FULL)})
        schema = _opt_out_of_system_default(flow)["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS).default() is True
        assert _schema_field_key(schema, CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS).default() is False


class TestRoutingSkipsIrrelevantPages:
    def test_lifecycle_saves_without_a_detail_page(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow, 15)
        result = _zone_page1(flow, choice="lifecycle")
        assert result["type"] == "create_entry"
        saved = _saved(flow)
        assert saved["release_strategy"] == "lifecycle"
        assert saved["use_system_default"] is False
        assert saved["detection_tolerance"] == 15

    def test_manual_saves_without_a_detail_page(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        assert _zone_page1(flow, choice="manual")["type"] == "create_entry"
        assert _saved(flow)["release_strategy"] == "manual"

    def test_duration_shows_both_durations_only(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        result = _zone_page1(flow, choice="duration")
        assert result["type"] == "form" and result["step_id"] == "override_duration"
        schema = result["data_schema"]
        assert _field_names(schema) == {CONF_OVERRIDE_DURATION_MIN, CONF_OVERRIDE_NIGHT_DURATION_MIN}
        assert _schema_field_key(schema, CONF_OVERRIDE_DURATION_MIN).default() == 120
        assert _schema_field_key(schema, CONF_OVERRIDE_NIGHT_DURATION_MIN).default() == 720
        assert _not_saved(flow)
        done = _detail(flow, "override_duration", {CONF_OVERRIDE_DURATION_MIN: 45, CONF_OVERRIDE_NIGHT_DURATION_MIN: 600})
        assert done["type"] == "create_entry"
        saved = _saved(flow)
        assert (saved["release_strategy"], saved["duration_min"], saved["night_duration_min"]) == ("duration", 45, 600)

    def test_fixed_time_shows_the_clock_time_only(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        result = _zone_page1(flow, choice="fixed_time")
        assert result["type"] == "form" and result["step_id"] == "override_fixed_time"
        assert _field_names(result["data_schema"]) == {CONF_OVERRIDE_FIXED_UNTIL}
        done = _detail(flow, "override_fixed_time", {CONF_OVERRIDE_FIXED_UNTIL: "07:30:00"})
        assert done["type"] == "create_entry"
        saved = _saved(flow)
        assert (saved["release_strategy"], saved["fixed_until"]) == ("fixed_time", "07:30:00")

    def test_fixed_time_without_a_time_is_rejected_and_nothing_is_saved(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="fixed_time")
        result = _detail(flow, "override_fixed_time", {})
        assert result["type"] == "form" and result["step_id"] == "override_fixed_time"
        assert result["errors"] == {"base": "override_fixed_until_required"}
        assert _not_saved(flow)
        for bad in (None, "", "25:99", 7):
            res = _detail(flow, "override_fixed_time", {CONF_OVERRIDE_FIXED_UNTIL: bad})
            assert res["errors"] == {"base": "override_fixed_until_required"}
        assert _not_saved(flow)
        done = _detail(flow, "override_fixed_time", {CONF_OVERRIDE_FIXED_UNTIL: "06:15:00"})
        assert done["type"] == "create_entry" and _saved(flow)["fixed_until"] == "06:15:00"

    def test_fixed_time_page_shows_the_stored_time(self):
        flow = _make_options_flow(data={"override_policy": {
            "release_strategy": "duration", "fixed_until": "07:15:00", "use_system_default": False}})
        _opt_out_of_system_default(flow)
        result = _zone_page1(flow, choice="fixed_time")
        key = _schema_field_key(result["data_schema"], CONF_OVERRIDE_FIXED_UNTIL)
        assert key.description == {"suggested_value": "07:15:00"}

    @pytest.mark.parametrize("flt,strategy", [("any", "first_any_decision"), ("comfort", "first_comfort"),
                                              ("protection", "first_protection")])
    def test_next_decision_asks_for_the_filter(self, flt, strategy):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        result = _zone_page1(flow, choice="next_decision")
        assert result["type"] == "form" and result["step_id"] == "override_decision"
        assert _field_names(result["data_schema"]) == {CONF_OVERRIDE_DECISION_FILTER}
        assert _schema_field_key(result["data_schema"], CONF_OVERRIDE_DECISION_FILTER).default() == "any"
        done = _detail(flow, "override_decision", {CONF_OVERRIDE_DECISION_FILTER: flt})
        assert done["type"] == "create_entry"
        assert _saved(flow)["release_strategy"] == strategy

    @pytest.mark.parametrize("stored,expected", [("first_comfort", "comfort"), ("first_protection", "protection"),
                                                 ("first_any_decision", "any"), ("manual", "any")])
    def test_decision_filter_default_is_the_stored_one(self, stored, expected):
        flow = _make_options_flow(data={"override_policy": {"release_strategy": stored, "use_system_default": False}})
        _opt_out_of_system_default(flow)
        result = _zone_page1(flow, choice="next_decision")
        assert _schema_field_key(result["data_schema"], CONF_OVERRIDE_DECISION_FILTER).default() == expected

    def test_an_unknown_filter_or_choice_degrades_to_the_defaults(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="next_decision")
        _detail(flow, "override_decision", {CONF_OVERRIDE_DECISION_FILTER: "bogus"})
        assert _saved(flow)["release_strategy"] == "first_any_decision"
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        assert _zone_page1(flow, choice="bogus")["type"] == "create_entry"
        assert _saved(flow)["release_strategy"] == "lifecycle"


class TestOptionalSafetyTimeoutPage:
    @pytest.mark.parametrize("choice", ["lifecycle", "manual"])
    def test_requested_page_follows_for_open_ended_choices(self, choice):
        flow = _make_options_flow(data={"override_policy": {**_STORED_FULL, "release_strategy": "lifecycle"}})
        _opt_out_of_system_default(flow)
        result = _zone_page1(flow, choice=choice, edit_timeouts=True)
        assert result["type"] == "form" and result["step_id"] == "override_timeout"
        schema = result["data_schema"]
        assert _field_names(schema) == {
            CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED, CONF_OVERRIDE_DURATION_MIN, CONF_OVERRIDE_NIGHT_DURATION_MIN}
        # prefilled with the stored values - a stored "off" is NOT shown as on
        assert _schema_field_key(schema, CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED).default() is False
        assert _schema_field_key(schema, CONF_OVERRIDE_DURATION_MIN).default() == 33
        assert _schema_field_key(schema, CONF_OVERRIDE_NIGHT_DURATION_MIN).default() == 444
        assert _not_saved(flow)

    def test_the_timeout_can_be_switched_on_and_the_durations_changed(self):
        flow = _make_options_flow(data={"override_policy": dict(_STORED_FULL)})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="manual", edit_timeouts=True)
        done = _detail(flow, "override_timeout", {
            CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED: True,
            CONF_OVERRIDE_DURATION_MIN: 90, CONF_OVERRIDE_NIGHT_DURATION_MIN: 500})
        assert done["type"] == "create_entry"
        saved = _saved(flow)
        assert (saved["release_strategy"], saved["safety_timeout_enabled"],
                saved["duration_min"], saved["night_duration_min"]) == ("manual", True, 90, 500)

    def test_next_decision_shows_the_filter_page_then_the_timeout_page(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        assert _zone_page1(flow, choice="next_decision", edit_timeouts=True)["step_id"] == "override_decision"
        nxt = _detail(flow, "override_decision", {CONF_OVERRIDE_DECISION_FILTER: "protection"})
        assert nxt["type"] == "form" and nxt["step_id"] == "override_timeout"
        assert _not_saved(flow)
        _detail(flow, "override_timeout", {
            CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED: False,
            CONF_OVERRIDE_DURATION_MIN: 120, CONF_OVERRIDE_NIGHT_DURATION_MIN: 720})
        saved = _saved(flow)
        assert (saved["release_strategy"], saved["safety_timeout_enabled"]) == ("first_protection", False)

    @pytest.mark.parametrize("choice", ["duration", "fixed_time"])
    def test_no_timeout_page_for_choices_that_have_their_own_deadline(self, choice):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        result = _zone_page1(flow, choice=choice, edit_timeouts=True)
        assert result["step_id"] in ("override_duration", "override_fixed_time")
        done = _detail(
            flow, result["step_id"],
            {CONF_OVERRIDE_DURATION_MIN: 50, CONF_OVERRIDE_NIGHT_DURATION_MIN: 60}
            if choice == "duration" else {CONF_OVERRIDE_FIXED_UNTIL: "05:00:00"})
        assert done["type"] == "create_entry"

    def test_the_default_case_does_not_ask(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        assert _zone_page1(flow, choice="lifecycle")["type"] == "create_entry"


class TestHiddenSettingsAreNeverReset:
    @pytest.mark.parametrize("choice,detail", [
        ("lifecycle", None), ("manual", None), ("duration", None),
    ])
    def test_switching_strategy_keeps_every_other_stored_value(self, choice, detail):
        flow = _make_options_flow(data={"override_policy": dict(_STORED_FULL)})
        _opt_out_of_system_default(flow, 25)
        result = _zone_page1(flow, choice=choice, comfort=True, protection=False)
        if choice == "duration":
            result = _detail(flow, "override_duration", {
                CONF_OVERRIDE_DURATION_MIN: 33, CONF_OVERRIDE_NIGHT_DURATION_MIN: 444})
        assert result["type"] == "create_entry"
        saved = _saved(flow)
        assert saved["release_strategy"] == choice
        assert saved["safety_timeout_enabled"] is False          # not forced back to True
        assert saved["fixed_until"] == "09:00:00"                # hidden, kept
        assert (saved["duration_min"], saved["night_duration_min"]) == (33, 444)
        assert saved["allow_comfort_actions"] is True and saved["allow_protection_actions"] is False

    def test_leaving_fixed_time_and_coming_back_keeps_the_time(self):
        flow = _make_options_flow(data={"override_policy": dict(_STORED_FULL)})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="manual")
        stored = _saved(flow)
        flow2 = _make_options_flow(data={"override_policy": stored})
        _opt_out_of_system_default(flow2)
        result = _zone_page1(flow2, choice="fixed_time")
        key = _schema_field_key(result["data_schema"], CONF_OVERRIDE_FIXED_UNTIL)
        assert key.description == {"suggested_value": "09:00:00"}

    def test_the_allow_flags_are_saved_from_page_one_and_can_be_switched_off(self):
        flow = _make_options_flow(data={"override_policy": dict(_STORED_FULL)})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="lifecycle", comfort=False, protection=True)
        saved = _saved(flow)
        assert saved["allow_comfort_actions"] is False and saved["allow_protection_actions"] is True

    def test_stop_hold_durations_never_change_with_a_strategy_switch(self):
        policy = {**_STORED_FULL, "duration_min": 61, "night_duration_min": 307}
        for choice in ("lifecycle", "manual", "next_decision", "fixed_time", "duration"):
            flow = _make_options_flow(data={"override_policy": dict(policy)})
            _opt_out_of_system_default(flow)
            result = _zone_page1(flow, choice=choice)
            while result["type"] == "form":
                step = result["step_id"]
                user_input = {
                    "override_duration": {CONF_OVERRIDE_DURATION_MIN: 61, CONF_OVERRIDE_NIGHT_DURATION_MIN: 307},
                    "override_fixed_time": {CONF_OVERRIDE_FIXED_UNTIL: "09:00:00"},
                    "override_decision": {CONF_OVERRIDE_DECISION_FILTER: "comfort"},
                }[step]
                result = _detail(flow, step, user_input)
            saved = _saved(flow)
            assert (saved["duration_min"], saved["night_duration_min"]) == (61, 307), choice

    def test_malformed_hidden_values_are_not_rewritten_and_still_load_safely(self):
        stored = {**_STORED_FULL, "duration_min": "abc", "night_duration_min": None, "fixed_until": "oops",
                  "safety_timeout_enabled": 0}
        flow = _make_options_flow(data={"override_policy": stored})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="lifecycle")
        saved = _saved(flow)
        assert (saved["duration_min"], saved["night_duration_min"], saved["fixed_until"]) == ("abc", None, "oops")
        cfg = _override_policy_from_storage(saved)
        assert (cfg.duration_min, cfg.night_duration_min, cfg.fixed_until, cfg.release_strategy) == (
            120, 720, None, OverrideReleaseStrategy.LIFECYCLE)

    def test_malformed_values_are_shown_as_safe_defaults_on_the_pages(self):
        stored = {**_STORED_FULL, "release_strategy": "lifecycle", "duration_min": "abc", "night_duration_min": -5}
        flow = _make_options_flow(data={"override_policy": stored})
        _opt_out_of_system_default(flow)
        result = _zone_page1(flow, choice="lifecycle", edit_timeouts=True)
        schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_DURATION_MIN).default() == 120
        assert _schema_field_key(schema, CONF_OVERRIDE_NIGHT_DURATION_MIN).default() == 1

    def test_a_fresh_zone_gets_the_complete_default_shape(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="lifecycle")
        assert _saved(flow) == {
            "release_strategy": "lifecycle", "fixed_until": None, "allow_comfort_actions": False,
            "allow_protection_actions": False, "duration_min": 120, "night_duration_min": 720,
            "safety_timeout_enabled": True, "use_system_default": False, "detection_tolerance": 15,
        }

    def test_superseded_pre_t10_keys_are_not_written_back(self):
        flow = _make_options_flow(data={"override_policy": {
            "duration_mode": "fixed_time", "break_on_lifecycle": False, "use_system_default": False}})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="manual")
        saved = _saved(flow)
        assert "duration_mode" not in saved and "break_on_lifecycle" not in saved
        assert saved["release_strategy"] == "manual"


class TestRoundTripOfAllSevenStrategies:
    @pytest.mark.parametrize("strategy", [s.value for s in OverrideReleaseStrategy])
    def test_each_stored_strategy_survives_an_unchanged_save_and_reload(self, strategy):
        stored = {"release_strategy": strategy, "fixed_until": "08:00:00", "duration_min": 50,
                  "night_duration_min": 500, "use_system_default": False}
        flow = _make_options_flow(data={"override_policy": dict(stored)})
        schema = _opt_out_of_system_default(flow)["data_schema"]
        choice = _schema_field_key(schema, CONF_OVERRIDE_RELEASE_CHOICE).default()
        result = _zone_page1(flow, choice=choice)
        while result["type"] == "form":
            step = result["step_id"]
            default_filter = _schema_field_key(result["data_schema"], CONF_OVERRIDE_DECISION_FILTER).default() \
                if step == "override_decision" else None
            result = _detail(flow, step, {
                "override_duration": {CONF_OVERRIDE_DURATION_MIN: 50, CONF_OVERRIDE_NIGHT_DURATION_MIN: 500},
                "override_fixed_time": {CONF_OVERRIDE_FIXED_UNTIL: "08:00:00"},
                "override_decision": {CONF_OVERRIDE_DECISION_FILTER: default_filter},
            }[step])
        saved = _saved(flow)
        assert saved["release_strategy"] == strategy
        assert _override_policy_from_storage(saved).release_strategy is OverrideReleaseStrategy(strategy)


class TestPolicyIsolationAndNoMutation:
    def test_page_renders_do_not_touch_the_entry(self):
        flow = _make_options_flow(data={"override_policy": dict(_STORED_FULL)})
        asyncio.run(flow.async_step_manual_override(user_input=None))
        _opt_out_of_system_default(flow)
        asyncio.run(flow.async_step_manual_override_custom(user_input=None))
        assert _not_saved(flow)
        assert flow._config_entry.data["override_policy"] == _STORED_FULL

    def test_a_zone_save_does_not_touch_the_system_policy_and_vice_versa(self):
        original_system = {"release_strategy": "manual", "duration_min": 99}
        flow = _make_options_flow(data={"system_override_policy": dict(original_system)})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="lifecycle")
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["system_override_policy"] == original_system
        flow = _make_options_flow(data={"override_policy": dict(_STORED_FULL)})
        _system_page1(flow, choice="lifecycle")
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["override_policy"] == _STORED_FULL

    def test_unrelated_keys_preserved(self):
        flow = _make_options_flow(data={"presence_policy": "all_home", "other": {"x": 1}})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="manual")
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["presence_policy"] == "all_home" and kwargs["data"]["other"] == {"x": 1}

    def test_a_second_flow_run_does_not_inherit_a_stale_draft(self):
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="duration")                    # abandoned on the detail page
        flow.hass.config_entries.async_update_entry.reset_mock()
        _opt_out_of_system_default(flow)
        _zone_page1(flow, choice="lifecycle")
        assert _saved(flow)["release_strategy"] == "lifecycle"

    def test_detail_steps_without_a_started_flow_fall_back_to_the_menu(self):
        flow = _make_options_flow(data={})
        for step in ("override_duration", "override_fixed_time", "override_decision", "override_timeout"):
            result = _detail(flow, step, None)
            assert result["type"] == "menu"
        assert _not_saved(flow)


class TestSystemManualOverrideStep:
    def test_page_one_has_the_same_fields_and_no_gate(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_system_manual_override(user_input=None))
        assert result["step_id"] == "system_manual_override"
        assert _field_names(result["data_schema"]) == {
            CONF_OVERRIDE_RELEASE_CHOICE, CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS,
            CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS, CONF_OVERRIDE_EDIT_TIMEOUTS}

    def test_the_default_strategy_saves_in_one_page(self):
        flow = _make_options_flow(data={})
        assert _system_page1(flow, choice="lifecycle")["type"] == "create_entry"
        saved = _saved(flow, "system_override_policy")
        assert saved["release_strategy"] == "lifecycle"
        assert "use_system_default" not in saved and "detection_tolerance" not in saved

    def test_detail_pages_and_hidden_values_work_for_the_system_policy(self):
        stored = {"release_strategy": "manual", "safety_timeout_enabled": False, "duration_min": 40,
                  "night_duration_min": 410, "fixed_until": "10:00:00", "allow_protection_actions": True}
        flow = _make_options_flow(data={"system_override_policy": dict(stored)})
        result = _system_page1(flow, choice="duration", protection=True)
        assert result["step_id"] == "override_duration"
        _detail(flow, "override_duration", {CONF_OVERRIDE_DURATION_MIN: 25, CONF_OVERRIDE_NIGHT_DURATION_MIN: 410})
        saved = _saved(flow, "system_override_policy")
        assert saved["release_strategy"] == "duration" and saved["duration_min"] == 25
        assert saved["safety_timeout_enabled"] is False and saved["fixed_until"] == "10:00:00"
        assert saved["allow_protection_actions"] is True

    def test_fixed_time_is_required_for_the_system_policy_too(self):
        flow = _make_options_flow(data={})
        _system_page1(flow, choice="fixed_time")
        res = _detail(flow, "override_fixed_time", {})
        assert res["errors"] == {"base": "override_fixed_until_required"} and _not_saved(flow)
        _detail(flow, "override_fixed_time", {CONF_OVERRIDE_FIXED_UNTIL: "22:00:00"})
        assert _saved(flow, "system_override_policy")["fixed_until"] == "22:00:00"

    def test_a_system_save_reloads_the_zone_entries(self):
        flow = _make_options_flow(data={})
        zone = MagicMock()
        zone.entry_id = "zone1"
        zone.data = {}
        flow.hass.config_entries.async_entries.return_value = [zone]
        _system_page1(flow, choice="manual")
        reloaded = [c.args[0] for c in flow.hass.config_entries.async_reload.call_args_list]
        assert "zone1" in reloaded and "e1" in reloaded


class TestTranslationCompleteness:
    _STEPS = ("manual_override", "manual_override_custom", "system_manual_override", "override_duration",
              "override_fixed_time", "override_decision", "override_timeout")

    def _all_i18n_files(self):
        yield _INTEGRATION_ROOT / "strings.json"
        yield from sorted((_INTEGRATION_ROOT / "translations").glob("*.json"))

    def _load(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_file_has_the_manual_override_strings(self):
        files = list(self._all_i18n_files())
        assert len(files) == 25
        for path in files:
            data = self._load(path)
            opt = data["options"]
            assert "manual_override" in opt["step"]["advanced"]["menu_options"], path.name
            for step_id in self._STEPS:
                assert opt["step"][step_id]["title"].strip(), f"{path.name}: {step_id}"
                assert opt["step"][step_id].get("description", "").strip(), f"{path.name}: {step_id} description"
            expected = {
                "manual_override_custom": {"override_release_choice", "override_allow_comfort_actions",
                                           "override_allow_protection_actions", "override_edit_timeouts"},
                "system_manual_override": {"override_release_choice", "override_allow_comfort_actions",
                                           "override_allow_protection_actions", "override_edit_timeouts"},
                "override_duration": {"override_duration_min", "override_night_duration_min"},
                "override_fixed_time": {"override_fixed_until"},
                "override_decision": {"override_decision_filter"},
                "override_timeout": {"override_safety_timeout_enabled", "override_duration_min",
                                     "override_night_duration_min"},
                "manual_override": {"override_use_system_default", "override_detection_tolerance"},
            }
            for step_id, fields in expected.items():
                step = opt["step"][step_id]
                assert set(step["data"]) == fields, f"{path.name}: {step_id} data"
                assert set(step["data_description"]) == fields, f"{path.name}: {step_id} data_description"
                for f in fields:
                    assert step["data"][f].strip() and step["data_description"][f].strip(), (path.name, step_id, f)
            assert opt["error"]["override_fixed_until_required"].strip(), path.name

    def test_selectors(self):
        for path in self._all_i18n_files():
            data = self._load(path)
            sel = data.get("selector", {})
            for old in ("override_release_strategy", "override_release_mode", "override_time_based_kind"):
                assert old not in sel, f"{path.name}: obsolete selector {old}"
            assert set(sel["override_release_choice"]["options"]) == set(RELEASE_CHOICES), path.name
            assert all(v.strip() for v in sel["override_release_choice"]["options"].values()), path.name
            assert set(sel["override_decision_filter"]["options"]) == {"any", "comfort", "protection"}, path.name

    def test_no_obsolete_wording_in_any_file(self):
        for path in self._all_i18n_files():
            text = path.read_text(encoding="utf-8")
            for old in ("override_release_mode", "override_time_based_kind"):
                assert old not in text, f"{path.name}: {old}"
        en = self._load(_INTEGRATION_ROOT / "strings.json")
        steps = json.dumps({k: en["options"]["step"][k] for k in self._STEPS}) + json.dumps(
            en["selector"]["override_release_choice"])
        assert "legacy" not in steps.lower()
        assert 'set to "Fixed time"' not in en["options"]["error"]["override_fixed_until_required"]

    def test_day_and_night_values_are_explained_on_the_duration_page(self):
        en = self._load(_INTEGRATION_ROOT / "strings.json")["options"]["step"]["override_duration"]
        assert "daytime" in en["data"]["override_duration_min"].lower()
        assert "night" in en["data"]["override_night_duration_min"].lower()
        assert "night" in en["description"].lower() and "day" in en["description"].lower()

    def test_the_translations_are_translated(self):
        en = self._load(_INTEGRATION_ROOT / "translations" / "en.json")["options"]
        keys = [("step", s, "title") for s in self._STEPS] + [("error", "override_fixed_until_required")]
        for path in sorted((_INTEGRATION_ROOT / "translations").glob("*.json")):
            if path.name == "en.json":
                continue
            opt = self._load(path)["options"]
            for k in keys:
                a, b = en, opt
                for part in k:
                    a, b = a[part], b[part]
                assert a != b, f"{path.name} has untranslated English text at {'/'.join(k)}"
            for step in ("override_duration", "override_timeout", "override_fixed_time", "override_decision"):
                assert en["step"][step]["description"] != opt["step"][step]["description"], (path.name, step)

    def test_no_unimplemented_feature_strings_referenced(self):
        en = self._load(_INTEGRATION_ROOT / "strings.json")
        haystack = json.dumps({k: en["options"]["step"][k] for k in self._STEPS}).lower()
        for forbidden in ("sunset", "sunrise", "select entity", "presence", "staged", "profile"):
            assert forbidden not in haystack, forbidden
