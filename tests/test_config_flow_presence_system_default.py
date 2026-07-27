"""OptionsFlow schema/save coverage for the Presence gate->custom->System
default split — T21 Phase C3.

Same real-selector-stub technique established in
tests/test_config_flow_manual_override.py.

Coverage:
  CFPS-01 Gate step shows only entity/policy/absence_position + the toggle;
          the delay field only appears on the custom step.
  CFPS-02 Gate default: True only when a zone has explicitly stored the
          flag; every zone (including one with just the legacy default 30)
          otherwise starts at False (own value) — no silent behavior change.
  CFPS-03 use_system_default=True saves immediately (gate does not advance).
  CFPS-04 use_system_default=False advances to the custom step.
  CFPS-05 Custom step save persists absence_delay_min + the fields collected
          on the gate step + use_system_default=False.
  CFPS-06 System entry's async_step_system_presence round-trips
          "system_absence_delay_min".
  CFPS-07 Form render (user_input=None) never mutates ConfigEntry.data.
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import voluptuous as vol

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_config_flow_manual_override.py.
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
from custom_components.smartshading.config_flow import SmartShadingOptionsFlow  # noqa: E402
from custom_components.smartshading.const import (  # noqa: E402
    CONF_ABSENCE_DELAY_MIN,
    CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT,
    CONF_PRESENCE_ENTITY_IDS,
    CONF_PRESENCE_POLICY,
    DEFAULT_ABSENCE_DELAY_MIN,
)


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


class TestGateSchemaShape:
    def test_gate_has_toggle_not_delay_field(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_presence(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT) is not None
        assert _schema_field_key(schema, CONF_ABSENCE_DELAY_MIN) is None


class TestGateDefaults:
    def test_unconfigured_zone_defaults_to_own_value(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_presence(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT).default() is False

    def test_zone_with_only_legacy_default_still_defaults_to_own_value(self):
        # No silent opt-in: a zone that merely has the untouched 30-minute
        # default stored must NOT be treated as "wants the system default".
        flow = _make_options_flow(data={"absence_delay_min": 30})
        result = asyncio.run(flow.async_step_presence(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT).default() is False

    def test_explicit_stored_flag_is_honored(self):
        flow = _make_options_flow(data={"absence_delay_use_system_default": True})
        result = asyncio.run(flow.async_step_presence(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT).default() is True


class TestGateSaveUsingSystemDefault:
    def test_saves_immediately_without_advancing(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_presence(user_input={
            CONF_PRESENCE_ENTITY_IDS: ["person.alice"],
            CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT: True,
            CONF_PRESENCE_POLICY: "any_home",
        }))
        assert result["type"] == "create_entry"
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["absence_delay_use_system_default"] is True
        assert kwargs["data"][CONF_PRESENCE_ENTITY_IDS] == ["person.alice"]


class TestGateAdvancesToCustomStep:
    def test_opting_out_advances_to_custom_form(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_presence(user_input={
            CONF_PRESENCE_ENTITY_IDS: [],
            CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT: False,
            CONF_PRESENCE_POLICY: "any_home",
        }))
        assert result["type"] == "form"
        assert result["step_id"] == "presence_custom"
        assert not flow.hass.config_entries.async_update_entry.called


class TestCustomStepSave:
    def test_saves_delay_plus_gate_fields(self):
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_presence(user_input={
            CONF_PRESENCE_ENTITY_IDS: ["person.bob"],
            CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT: False,
            CONF_PRESENCE_POLICY: "all_home",
        }))
        asyncio.run(flow.async_step_presence_custom(user_input={CONF_ABSENCE_DELAY_MIN: 77}))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]
        assert saved[CONF_ABSENCE_DELAY_MIN] == 77
        assert saved["absence_delay_use_system_default"] is False
        assert saved[CONF_PRESENCE_ENTITY_IDS] == ["person.bob"]
        assert saved[CONF_PRESENCE_POLICY] == "all_home"


class TestSystemPresenceStep:
    def test_round_trips_system_absence_delay_min(self):
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_system_presence(user_input={CONF_ABSENCE_DELAY_MIN: 90}))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["system_absence_delay_min"] == 90

    def test_defaults_when_nothing_stored(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_system_presence(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_ABSENCE_DELAY_MIN).default() == DEFAULT_ABSENCE_DELAY_MIN


class TestNoMutationOnRender:
    def test_gate_render_does_not_touch_entry(self):
        original = {"absence_delay_min": 45}
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_presence(user_input=None))
        assert flow._config_entry.data == original
        assert not flow.hass.config_entries.async_update_entry.called
