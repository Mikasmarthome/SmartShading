"""OptionsFlow schema/save coverage for the Dispatch strategy step —
v1.2.0-beta.1, T11.

Same real-selector-stub technique established in
tests/test_config_flow_manual_override.py (T7/T10).

Coverage:
  CFD-01  Menu reaches "dispatch" (reachability).
  CFD-02  Defaults pre-selected when nothing stored (backward compatibility:
          SPACED at 2.0s).
  CFD-03  Stored values pre-selected on reopen.
  CFD-04  Saving persists every field into "dispatch_config".
  CFD-05  Out-of-range numeric input is clamped server-side, never stored
          as-is or crashes the save.
  CFD-06  Invalid mode string falls back to SPACED on save.
  CFD-07  Form render (user_input=None) does not mutate ConfigEntry.data.
  CFD-08  Unrelated top-level keys untouched by a save.
  CFD-09  Decimal (float) start_interval_s values are accepted and saved.
  CFD-10  zone_batching boolean round-trips correctly.
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
    CONF_DISPATCH_MAX_TRAVEL_WAIT_S,
    CONF_DISPATCH_MODE,
    CONF_DISPATCH_POST_TRAVEL_PAUSE_S,
    CONF_DISPATCH_START_INTERVAL_S,
    CONF_DISPATCH_ZONE_BATCHING,
)
from custom_components.smartshading.models.dispatch_config import (
    DEFAULT_MAX_TRAVEL_WAIT_S,
    DEFAULT_POST_TRAVEL_PAUSE_S,
    DEFAULT_START_INTERVAL_S,
    DispatchMode,
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


_FULL_INPUT = {
    CONF_DISPATCH_MODE: DispatchMode.SEQUENTIAL.value,
    CONF_DISPATCH_START_INTERVAL_S: 1.5,
    CONF_DISPATCH_MAX_TRAVEL_WAIT_S: 45.0,
    CONF_DISPATCH_POST_TRAVEL_PAUSE_S: 0.8,
    CONF_DISPATCH_ZONE_BATCHING: True,
}


class TestMenuReachability:
    def test_dispatch_in_init_menu(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_init(user_input=None))
        assert result["type"] == "menu"
        assert "dispatch" in result["menu_options"]


class TestDefaultsPreselected:
    def test_defaults_when_nothing_stored(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_dispatch(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_DISPATCH_MODE).default() == DispatchMode.SPACED.value
        assert _schema_field_key(schema, CONF_DISPATCH_START_INTERVAL_S).default() == DEFAULT_START_INTERVAL_S
        assert _schema_field_key(schema, CONF_DISPATCH_MAX_TRAVEL_WAIT_S).default() == DEFAULT_MAX_TRAVEL_WAIT_S
        assert _schema_field_key(schema, CONF_DISPATCH_POST_TRAVEL_PAUSE_S).default() == DEFAULT_POST_TRAVEL_PAUSE_S
        assert _schema_field_key(schema, CONF_DISPATCH_ZONE_BATCHING).default() is False


class TestStoredValuesPreselected:
    def test_stored_values_shown_on_reopen(self):
        flow = _make_options_flow(data={
            "dispatch_config": {
                "mode": "sequential", "start_interval_s": 3.0,
                "max_travel_wait_s": 60.0, "post_travel_pause_s": 1.0,
                "zone_batching": True,
            }
        })
        result = asyncio.run(flow.async_step_dispatch(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_DISPATCH_MODE).default() == "sequential"
        assert _schema_field_key(schema, CONF_DISPATCH_START_INTERVAL_S).default() == 3.0
        assert _schema_field_key(schema, CONF_DISPATCH_ZONE_BATCHING).default() is True

    def test_form_render_does_not_mutate_entry(self):
        flow = _make_options_flow(data={"dispatch_config": {"mode": "parallel"}})
        before = dict(flow._config_entry.data)
        asyncio.run(flow.async_step_dispatch(user_input=None))
        assert flow._config_entry.data == before
        flow.hass.config_entries.async_update_entry.assert_not_called()


class TestSavePersistsEveryField:
    def test_full_input_saved(self):
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_dispatch(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["dispatch_config"]
        assert saved["mode"] == "sequential"
        assert saved["start_interval_s"] == 1.5
        assert saved["max_travel_wait_s"] == 45.0
        assert saved["post_travel_pause_s"] == 0.8
        assert saved["zone_batching"] is True

    def test_decimal_start_interval_saved(self):
        flow = _make_options_flow(data={})
        user_input = dict(_FULL_INPUT)
        user_input[CONF_DISPATCH_START_INTERVAL_S] = 0.5
        asyncio.run(flow.async_step_dispatch(user_input=user_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["dispatch_config"]["start_interval_s"] == 0.5

    def test_zero_start_interval_saved(self):
        flow = _make_options_flow(data={})
        user_input = dict(_FULL_INPUT)
        user_input[CONF_DISPATCH_START_INTERVAL_S] = 0.0
        asyncio.run(flow.async_step_dispatch(user_input=user_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["dispatch_config"]["start_interval_s"] == 0.0

    def test_unrelated_keys_untouched(self):
        flow = _make_options_flow(data={"weather_entity_id": "weather.home"})
        asyncio.run(flow.async_step_dispatch(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["weather_entity_id"] == "weather.home"


class TestServerSideValidation:
    def test_out_of_range_start_interval_clamped(self):
        # config_flow.py's _safe_float_in_range() clamps into [min, max]
        # (the user typed something real, just out of bounds — clamping to
        # the nearest valid value is more helpful than silently discarding
        # it back to the unrelated default). This differs deliberately from
        # config_entry_data.py's _dispatch_config_from_storage(), which
        # falls back to the default for a value it cannot trust came from
        # deliberate user input (e.g. a malformed/hand-edited storage file) —
        # see test_dispatch_config_storage.py's TestOutOfRangeClamping.
        from custom_components.smartshading.models.dispatch_config import START_INTERVAL_S_MAX
        flow = _make_options_flow(data={})
        user_input = dict(_FULL_INPUT)
        user_input[CONF_DISPATCH_START_INTERVAL_S] = 9999.0
        asyncio.run(flow.async_step_dispatch(user_input=user_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["dispatch_config"]["start_interval_s"] == START_INTERVAL_S_MAX

    def test_negative_max_travel_wait_clamped(self):
        from custom_components.smartshading.models.dispatch_config import MAX_TRAVEL_WAIT_S_MIN
        flow = _make_options_flow(data={})
        user_input = dict(_FULL_INPUT)
        user_input[CONF_DISPATCH_MAX_TRAVEL_WAIT_S] = -10.0
        asyncio.run(flow.async_step_dispatch(user_input=user_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["dispatch_config"]["max_travel_wait_s"] == MAX_TRAVEL_WAIT_S_MIN

    def test_invalid_mode_falls_back_to_spaced(self):
        flow = _make_options_flow(data={})
        user_input = dict(_FULL_INPUT)
        user_input[CONF_DISPATCH_MODE] = "warp_speed"
        asyncio.run(flow.async_step_dispatch(user_input=user_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["dispatch_config"]["mode"] == DispatchMode.SPACED.value

    def test_non_numeric_input_falls_back_to_default(self):
        flow = _make_options_flow(data={})
        user_input = dict(_FULL_INPUT)
        user_input[CONF_DISPATCH_START_INTERVAL_S] = "not_a_number"
        asyncio.run(flow.async_step_dispatch(user_input=user_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["dispatch_config"]["start_interval_s"] == DEFAULT_START_INTERVAL_S
