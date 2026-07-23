"""OptionsFlow schema/save coverage for the new heat protection hysteresis
field on the "comfort" step — v1.2.0-beta.1, T9.

Same real-selector-stub technique established in
tests/test_config_flow_manual_override.py (T7) / test_config_flow_lifecycle_profile.py (T6).

Coverage:
  CFHH-01  Default (1.0 °C) pre-selected when nothing stored.
  CFHH-02  Stored value pre-selected on reopen.
  CFHH-03  Saving persists heat_protection_hysteresis_c into "comfort_config",
           alongside the pre-existing fields (which must stay untouched).
  CFHH-04  Out-of-range / malformed values are rejected with an error, no
           save happens.
  CFHH-05  A comfort_config dict missing the new key entirely (pre-T9 stored
           data) does not crash and shows the 1.0 default — backward compat.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
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
    CONF_GLARE_MIN_EXPOSURE_WM2,
    CONF_GLARE_PROTECTION_ENABLED,
    CONF_HEAT_PROTECTION_ENABLED,
    CONF_HEAT_PROTECTION_HYSTERESIS_C,
    CONF_INDOOR_TEMPERATURE_SENSOR_IDS,
    CONF_SOLAR_GAIN_ENABLED,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


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
    CONF_INDOOR_TEMPERATURE_SENSOR_IDS: [],
    CONF_HEAT_PROTECTION_ENABLED: True,
    CONF_GLARE_PROTECTION_ENABLED: True,
    CONF_SOLAR_GAIN_ENABLED: True,
    CONF_GLARE_MIN_EXPOSURE_WM2: 100.0,
    CONF_HEAT_PROTECTION_HYSTERESIS_C: 2.5,
}


class TestDefaultPreselected:
    def test_default_is_one_degree_when_nothing_stored(self) -> None:
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_HEAT_PROTECTION_HYSTERESIS_C)
        assert key is not None, "heat_protection_hysteresis_c field missing from comfort schema"
        assert key.default() == 1.0


class TestStoredValuePreselected:
    def test_stored_value_shown_on_reopen(self) -> None:
        flow = _make_options_flow(data={"comfort_config": {"heat_protection_hysteresis_c": 3.0}})
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_HEAT_PROTECTION_HYSTERESIS_C)
        assert key.default() == 3.0


class TestSavePersists:
    def test_value_saved_into_comfort_config(self) -> None:
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_comfort(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["comfort_config"]
        assert saved["heat_protection_hysteresis_c"] == 2.5
        # Pre-existing fields must remain correctly saved alongside it.
        assert saved["heat_protection_enabled"] is True
        assert saved["glare_min_exposure_wm2"] == 100.0

    def test_zero_is_a_valid_saved_value(self) -> None:
        flow = _make_options_flow(data={})
        legacy_input = dict(_FULL_INPUT)
        legacy_input[CONF_HEAT_PROTECTION_HYSTERESIS_C] = 0.0
        asyncio.run(flow.async_step_comfort(user_input=legacy_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["comfort_config"]["heat_protection_hysteresis_c"] == 0.0


class TestInvalidValueRejected:
    def test_negative_value_rejected(self) -> None:
        flow = _make_options_flow(data={})
        bad_input = dict(_FULL_INPUT)
        bad_input[CONF_HEAT_PROTECTION_HYSTERESIS_C] = -1.0
        result = asyncio.run(flow.async_step_comfort(user_input=bad_input))
        assert result["errors"]["base"] == "invalid_heat_hysteresis"
        flow.hass.config_entries.async_update_entry.assert_not_called()

    def test_above_max_rejected(self) -> None:
        flow = _make_options_flow(data={})
        bad_input = dict(_FULL_INPUT)
        bad_input[CONF_HEAT_PROTECTION_HYSTERESIS_C] = 5.1
        result = asyncio.run(flow.async_step_comfort(user_input=bad_input))
        assert result["errors"]["base"] == "invalid_heat_hysteresis"
        flow.hass.config_entries.async_update_entry.assert_not_called()

    def test_non_numeric_rejected(self) -> None:
        flow = _make_options_flow(data={})
        bad_input = dict(_FULL_INPUT)
        bad_input[CONF_HEAT_PROTECTION_HYSTERESIS_C] = "not-a-number"
        result = asyncio.run(flow.async_step_comfort(user_input=bad_input))
        assert result["errors"]["base"] == "invalid_heat_hysteresis"
        flow.hass.config_entries.async_update_entry.assert_not_called()


class TestBackwardCompatibilityMissingKey:
    def test_comfort_config_without_new_key_does_not_crash(self) -> None:
        """A pre-T9 stored comfort_config (no heat_protection_hysteresis_c
        key at all) must load without error and show the 1.0 default."""
        flow = _make_options_flow(data={
            "comfort_config": {
                "heat_protection_enabled": True,
                "glare_protection_enabled": True,
                "solar_gain_enabled": True,
                "glare_min_exposure_wm2": 100.0,
            }
        })
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_HEAT_PROTECTION_HYSTERESIS_C)
        assert key.default() == 1.0
