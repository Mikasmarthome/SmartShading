"""Comfort step of the Setup and Options flows - no technical thresholds.

Originally (v1.2.0-beta.1, T9) this file covered the heat protection hysteresis
field on the Options "comfort" step.  The glare minimum exposure and the heat
release hysteresis are now fixed internal policy, so the comfort step offers
neither of them:

  CFHH-01  Options form: neither field is offered; the remaining controls are.
  CFHH-02  Setup form: neither field is offered; the remaining controls are.
  CFHH-03  Options save keeps toggles, sensors and every unrelated stored key.
  CFHH-04  Options save drops the two obsolete keys from an old stored dict
           (custom, zero and malformed values alike) without deleting anything
           else, and repeated saves are stable.
  CFHH-05  Opening the form on old or malformed stored values never fails.
  CFHH-06  Setup stores no obsolete keys.
  CFHH-07  No orphaned validation errors / constants remain.
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
    CONF_GLARE_PROTECTION_ENABLED,
    CONF_HEAT_PROTECTION_ENABLED,
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
    CONF_INDOOR_TEMPERATURE_SENSOR_IDS: ["sensor.indoor"],
    CONF_HEAT_PROTECTION_ENABLED: True,
    CONF_GLARE_PROTECTION_ENABLED: False,
    CONF_SOLAR_GAIN_ENABLED: True,
}

_REMOVED = ("glare_min_exposure_wm2", "heat_protection_hysteresis_c")
_KEPT_FIELDS = {
    CONF_INDOOR_TEMPERATURE_SENSOR_IDS,
    CONF_HEAT_PROTECTION_ENABLED,
    CONF_GLARE_PROTECTION_ENABLED,
    CONF_SOLAR_GAIN_ENABLED,
}


def _field_names(schema: vol.Schema) -> set[str]:
    return {str(k) for k in schema.schema}


def _saved(flow: SmartShadingOptionsFlow) -> dict:
    _, kwargs = flow.hass.config_entries.async_update_entry.call_args
    return kwargs["data"]


class TestOptionsFormOffersNoThresholds:
    def test_fields_absent_remaining_controls_present(self) -> None:
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        names = _field_names(result["data_schema"])
        assert names == _KEPT_FIELDS
        for removed in _REMOVED:
            assert removed not in names
        assert not result.get("errors")

    def test_stored_legacy_values_are_not_shown(self) -> None:
        flow = _make_options_flow(data={"comfort_config": {
            "glare_min_exposure_wm2": 300.0, "heat_protection_hysteresis_c": 3.0}})
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        assert _field_names(result["data_schema"]) == _KEPT_FIELDS

    def test_stored_toggles_are_prefilled(self) -> None:
        flow = _make_options_flow(data={"comfort_config": {
            "heat_protection_enabled": False, "glare_protection_enabled": False,
            "solar_gain_enabled": False}})
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        schema: vol.Schema = result["data_schema"]
        for name in (CONF_HEAT_PROTECTION_ENABLED, CONF_GLARE_PROTECTION_ENABLED, CONF_SOLAR_GAIN_ENABLED):
            assert _schema_field_key(schema, name).default() is False


class TestSetupFormOffersNoThresholds:
    def _flow(self, monkeypatch):
        import custom_components.smartshading.config_flow as cf

        monkeypatch.setattr(cf, "_first_zone_entry_data", lambda hass: None)
        flow = cf.SmartShadingConfigFlow()
        flow.hass = MagicMock()
        flow.async_show_form = lambda **kw: kw
        return flow

    def test_fields_absent_remaining_controls_present(self, monkeypatch) -> None:
        flow = self._flow(monkeypatch)
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        names = _field_names(result["data_schema"])
        assert names == _KEPT_FIELDS
        for removed in _REMOVED:
            assert removed not in names

    def test_prefill_from_an_existing_zone_does_not_reintroduce_them(self, monkeypatch) -> None:
        import custom_components.smartshading.config_flow as cf

        monkeypatch.setattr(cf, "_first_zone_entry_data", lambda hass: {
            "comfort_config": {"glare_min_exposure_wm2": 300.0,
                               "heat_protection_hysteresis_c": 3.0,
                               "glare_protection_enabled": False}})
        flow = cf.SmartShadingConfigFlow()
        flow.hass = MagicMock()
        flow.async_show_form = lambda **kw: kw
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _field_names(schema) == _KEPT_FIELDS
        assert _schema_field_key(schema, CONF_GLARE_PROTECTION_ENABLED).default() is False

    def test_submit_continues_without_the_fields(self, monkeypatch) -> None:
        flow = self._flow(monkeypatch)
        reached: list[str] = []

        async def _lifecycle(user_input=None):
            reached.append("lifecycle")
            return {"type": "form", "step_id": "lifecycle"}

        flow.async_step_lifecycle = _lifecycle
        asyncio.run(flow.async_step_comfort(user_input=dict(_FULL_INPUT)))
        assert reached == ["lifecycle"]
        assert flow._heat_protection_enabled is True
        assert flow._glare_protection_enabled is False
        assert flow._indoor_temperature_sensor_ids == ["sensor.indoor"]
        assert not hasattr(flow, "_glare_min_exposure_wm2")
        assert not hasattr(flow, "_heat_protection_hysteresis_c")


class TestOptionsSaveKeepsUnrelatedData:
    def test_toggles_sensors_and_thresholds_are_saved_and_kept(self) -> None:
        flow = _make_options_flow(data={
            "other_key": {"nested": [1, 2]},
            "comfort_config": {
                "heat_protection_indoor_temp_c": 22.0,
                "heat_protection_outdoor_temp_c": 28.5,
                "solar_gain_max_outdoor_temp_c": 10.0,
                "future_key": "kept",
            },
        })
        asyncio.run(flow.async_step_comfort(user_input=dict(_FULL_INPUT)))
        data = _saved(flow)
        comfort = data["comfort_config"]
        assert comfort["heat_protection_enabled"] is True
        assert comfort["glare_protection_enabled"] is False
        assert comfort["solar_gain_enabled"] is True
        assert comfort["heat_protection_indoor_temp_c"] == 22.0
        assert comfort["heat_protection_outdoor_temp_c"] == 28.5
        assert comfort["solar_gain_max_outdoor_temp_c"] == 10.0
        assert comfort["future_key"] == "kept"
        assert data[CONF_INDOOR_TEMPERATURE_SENSOR_IDS] == ["sensor.indoor"]
        assert data["other_key"] == {"nested": [1, 2]}

    def test_a_fresh_entry_saves_only_the_three_toggles(self) -> None:
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_comfort(user_input=dict(_FULL_INPUT)))
        assert set(_saved(flow)["comfort_config"]) == {
            "heat_protection_enabled", "glare_protection_enabled", "solar_gain_enabled"}


_LEGACY = [
    {"glare_min_exposure_wm2": 300.0, "heat_protection_hysteresis_c": 3.0},
    {"glare_min_exposure_wm2": 0, "heat_protection_hysteresis_c": 0},
    {"glare_min_exposure_wm2": -1, "heat_protection_hysteresis_c": -1},
    {"glare_min_exposure_wm2": None, "heat_protection_hysteresis_c": None},
    {"glare_min_exposure_wm2": "abc", "heat_protection_hysteresis_c": "x"},
    {"glare_min_exposure_wm2": [1, 2], "heat_protection_hysteresis_c": {"a": 1}},
    {"glare_min_exposure_wm2": 100.0},
    {"heat_protection_hysteresis_c": 1.0},
]


class TestObsoleteKeysAreDropped:
    def test_every_kind_of_legacy_value_is_removed_on_save(self) -> None:
        for legacy in _LEGACY:
            flow = _make_options_flow(data={"comfort_config": {
                "heat_protection_enabled": False, "heat_protection_indoor_temp_c": 21.5, **legacy}})
            asyncio.run(flow.async_step_comfort(user_input=dict(_FULL_INPUT)))
            comfort = _saved(flow)["comfort_config"]
            for removed in _REMOVED:
                assert removed not in comfort, (legacy, removed)
            assert comfort["heat_protection_indoor_temp_c"] == 21.5

    def test_stray_legacy_input_is_ignored(self) -> None:
        flow = _make_options_flow(data={})
        stray = dict(_FULL_INPUT)
        stray["glare_min_exposure_wm2"] = 999
        stray["heat_protection_hysteresis_c"] = "nope"
        result = asyncio.run(flow.async_step_comfort(user_input=stray))
        assert not (result or {}).get("errors")
        comfort = _saved(flow)["comfort_config"]
        for removed in _REMOVED:
            assert removed not in comfort

    def test_repeated_saves_are_stable(self) -> None:
        flow = _make_options_flow(data={"comfort_config": {
            "glare_min_exposure_wm2": 300.0, "heat_protection_hysteresis_c": 3.0}})
        asyncio.run(flow.async_step_comfort(user_input=dict(_FULL_INPUT)))
        first = _saved(flow)
        flow2 = _make_options_flow(data=first)
        asyncio.run(flow2.async_step_comfort(user_input=dict(_FULL_INPUT)))
        assert _saved(flow2) == first


class TestOpeningOnOldOrMalformedStorage:
    def test_form_opens_for_every_legacy_shape(self) -> None:
        for legacy in _LEGACY:
            flow = _make_options_flow(data={"comfort_config": dict(legacy)})
            result = asyncio.run(flow.async_step_comfort(user_input=None))
            assert _field_names(result["data_schema"]) == _KEPT_FIELDS

    def test_comfort_config_missing_entirely(self) -> None:
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_comfort(user_input=None))
        assert _field_names(result["data_schema"]) == _KEPT_FIELDS


class TestNoOrphans:
    def test_no_removed_error_keys_or_constants_remain_in_the_flow(self) -> None:
        source = (_REPO_ROOT / "custom_components" / "smartshading" / "config_flow.py").read_text(encoding="utf-8")
        for needle in (
            "invalid_glare_min_exposure", "invalid_heat_hysteresis",
            "CONF_GLARE_MIN_EXPOSURE_WM2", "CONF_HEAT_PROTECTION_HYSTERESIS_C",
            "GLARE_MIN_EXPOSURE_MAX_WM2", "HEAT_PROTECTION_HYSTERESIS_MAX_C",
        ):
            assert needle not in source, needle
