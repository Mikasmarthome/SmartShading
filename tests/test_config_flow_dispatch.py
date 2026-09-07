"""OptionsFlow schema/save coverage for the Cover Dispatch step —
v1.2.0-beta.1, T11; moved to the System entry in T21 Phase C2 (Cover
Dispatch is a hardware/RF-pacing property of the shared dispatch pipeline,
not a per-zone concern — see the Phase C2 ownership analysis).

Same real-selector-stub technique established in
tests/test_config_flow_manual_override.py (T7/T10).

Coverage:
  CFD-01  System entry menu reaches "system_dispatch" (reachability); the
          zone menu no longer offers a Dispatch step at all.
  CFD-02  Defaults pre-selected when nothing stored for the fields that are
          still user-facing (max_travel_wait_s/post_travel_pause_s/
          zone_batching); mode/start_interval_s are no longer schema fields
          at all (B3-013 — SmartShading has exactly one fixed dispatch rule,
          see _dispatch_schema()'s docstring in config_flow.py).
  CFD-03  Stored values for the still-user-facing fields are pre-selected
          on reopen; mode/start_interval_s stay absent from the schema
          regardless of what is stored.
  CFD-04  Saving persists every still-user-facing field into
          "system_dispatch_config" on the System entry; mode/
          start_interval_s are carried through UNCHANGED from whatever was
          previously stored (tolerant pass-through), never re-derived from
          the submission.
  CFD-05  Out-of-range numeric input (max_travel_wait_s/post_travel_pause_s)
          is clamped server-side, never stored as-is or crashes the save.
  CFD-06  Form render (user_input=None) does not mutate ConfigEntry.data.
  CFD-07  Unrelated top-level keys untouched by a save.
  CFD-08  zone_batching boolean round-trips correctly.
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
from custom_components.smartshading.const import CONF_ENTRY_TYPE, ENTRY_TYPE_SYSTEM
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


# B3-013: CONF_DISPATCH_MODE / CONF_DISPATCH_START_INTERVAL_S are deliberately
# absent — the form no longer submits them (see _dispatch_schema()).
_FULL_INPUT = {
    CONF_DISPATCH_MAX_TRAVEL_WAIT_S: 45.0,
    CONF_DISPATCH_POST_TRAVEL_PAUSE_S: 0.8,
    CONF_DISPATCH_ZONE_BATCHING: True,
}


class TestMenuReachability:
    def test_system_dispatch_in_system_init_menu(self):
        flow = _make_options_flow(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM})
        result = asyncio.run(flow.async_step_init(user_input=None))
        assert result["type"] == "menu"
        assert "system_dispatch" in result["menu_options"]

    def test_dispatch_no_longer_in_zone_advanced_menu(self):
        flow = _make_options_flow(data={})
        advanced = asyncio.run(flow.async_step_advanced(user_input=None))
        assert advanced["type"] == "menu"
        assert "dispatch" not in advanced["menu_options"]


class TestDefaultsPreselected:
    def test_defaults_when_nothing_stored(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_system_dispatch(user_input=None))
        schema: vol.Schema = result["data_schema"]
        # B3-013: mode/start_interval_s are no longer schema fields at all.
        assert _schema_field_key(schema, CONF_DISPATCH_MODE) is None
        assert _schema_field_key(schema, CONF_DISPATCH_START_INTERVAL_S) is None
        assert _schema_field_key(schema, CONF_DISPATCH_MAX_TRAVEL_WAIT_S).default() == DEFAULT_MAX_TRAVEL_WAIT_S
        assert _schema_field_key(schema, CONF_DISPATCH_POST_TRAVEL_PAUSE_S).default() == DEFAULT_POST_TRAVEL_PAUSE_S
        assert _schema_field_key(schema, CONF_DISPATCH_ZONE_BATCHING).default() is False


class TestStoredValuesPreselected:
    def test_stored_values_shown_on_reopen(self):
        flow = _make_options_flow(data={
            "system_dispatch_config": {
                "mode": "sequential", "start_interval_s": 3.0,
                "max_travel_wait_s": 60.0, "post_travel_pause_s": 1.0,
                "zone_batching": True,
            }
        })
        result = asyncio.run(flow.async_step_system_dispatch(user_input=None))
        schema: vol.Schema = result["data_schema"]
        # B3-013: still absent from the schema even though a value is
        # stored — the old mode/interval never resurface in the UI.
        assert _schema_field_key(schema, CONF_DISPATCH_MODE) is None
        assert _schema_field_key(schema, CONF_DISPATCH_START_INTERVAL_S) is None
        assert _schema_field_key(schema, CONF_DISPATCH_MAX_TRAVEL_WAIT_S).default() == 60.0
        assert _schema_field_key(schema, CONF_DISPATCH_ZONE_BATCHING).default() is True

    def test_form_render_does_not_mutate_entry(self):
        flow = _make_options_flow(data={"system_dispatch_config": {"mode": "parallel"}})
        before = dict(flow._config_entry.data)
        asyncio.run(flow.async_step_system_dispatch(user_input=None))
        assert flow._config_entry.data == before
        flow.hass.config_entries.async_update_entry.assert_not_called()


class TestSavePersistsEveryField:
    def test_full_input_saved(self):
        # Nothing was previously stored, so mode/start_interval_s fall back
        # to their tolerant defaults (SPACED / DEFAULT_START_INTERVAL_S) —
        # B3-013: they are carried through from `stored`, never from this
        # submission, since the field no longer exists in the form.
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_system_dispatch(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["system_dispatch_config"]
        assert saved["mode"] == DispatchMode.SPACED.value
        assert saved["start_interval_s"] == DEFAULT_START_INTERVAL_S
        assert saved["max_travel_wait_s"] == 45.0
        assert saved["post_travel_pause_s"] == 0.8
        assert saved["zone_batching"] is True

    def test_stored_mode_and_interval_pass_through_unchanged_on_save(self):
        # B3-013: a pre-existing stored mode/start_interval_s (from before
        # this package) must round-trip through a save UNCHANGED, since the
        # submission no longer carries a value for either field at all.
        flow = _make_options_flow(data={
            "system_dispatch_config": {
                "mode": "sequential", "start_interval_s": 3.0,
                "max_travel_wait_s": 45.0, "post_travel_pause_s": 0.8,
                "zone_batching": False,
            }
        })
        asyncio.run(flow.async_step_system_dispatch(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["system_dispatch_config"]
        assert saved["mode"] == "sequential"
        assert saved["start_interval_s"] == 3.0

    def test_unrelated_keys_untouched(self):
        flow = _make_options_flow(data={"weather_entity_id": "weather.home"})
        asyncio.run(flow.async_step_system_dispatch(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["weather_entity_id"] == "weather.home"


class TestServerSideValidation:
    def test_negative_max_travel_wait_clamped(self):
        from custom_components.smartshading.models.dispatch_config import MAX_TRAVEL_WAIT_S_MIN
        flow = _make_options_flow(data={})
        user_input = dict(_FULL_INPUT)
        user_input[CONF_DISPATCH_MAX_TRAVEL_WAIT_S] = -10.0
        asyncio.run(flow.async_step_system_dispatch(user_input=user_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["system_dispatch_config"]["max_travel_wait_s"] == MAX_TRAVEL_WAIT_S_MIN

    def test_malformed_stored_mode_falls_back_to_spaced(self):
        # B3-013: an old stored value that is no longer a valid DispatchMode
        # member falls back to SPACED — this can no longer happen via the
        # UI (the field is gone), only via a hand-edited/corrupted storage
        # payload, so it's exercised through `stored` now, not `user_input`.
        flow = _make_options_flow(data={
            "system_dispatch_config": {"mode": "warp_speed"},
        })
        asyncio.run(flow.async_step_system_dispatch(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["system_dispatch_config"]["mode"] == DispatchMode.SPACED.value
