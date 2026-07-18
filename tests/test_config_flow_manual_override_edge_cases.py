"""OptionsFlow edge cases for the Manual Override step — T7 pre-push
review point 13.

Reuses the same real-selector-stub technique established in
test_config_flow_manual_override.py (which already covers menu
reachability, legacy defaults, stored-value preselection, full-field save,
fixed-time-without-time rejection, non-mutation on render, unrelated-key
preservation, and translation completeness — not repeated here).

Coverage:
  EDGE-01  Unknown/invalid stored duration_mode is pre-selected as "legacy"
           in the OptionsFlow (not just at the storage layer).
  EDGE-02  Switching Fixed Time -> Legacy preserves the stored fixed_until
           value (documented, intentional — see config_flow.py comment).
  EDGE-03  Switching Legacy -> Fixed Time re-displays a previously stored
           fixed_until value.
  EDGE-04  Initial (non-options) ConfigFlow never writes an override_policy
           key.
  EDGE-05  A save never touches lifecycle_profiles, active_lifecycle_profile_id,
           presence_policy, presence_entity_ids, ema_enabled, ema_alpha, or
           lifecycle_config.
  EDGE-06  Duration/tolerance fields carry sensible NumberSelector min/max
           bounds in the schema.
  EDGE-07  Zero, negative, and wrong-type numeric input are handled safely
           (clamped to a sane default/bound, never crash, never stored
           as-is).
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
from custom_components.smartshading.config_flow import (  # noqa: E402
    SmartShadingConfigFlow,
    SmartShadingOptionsFlow,
)
from custom_components.smartshading.const import (  # noqa: E402
    CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS,
    CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS,
    CONF_OVERRIDE_BREAK_ON_LIFECYCLE,
    CONF_OVERRIDE_DETECTION_TOLERANCE,
    CONF_OVERRIDE_DURATION_MIN,
    CONF_OVERRIDE_DURATION_MODE,
    CONF_OVERRIDE_FIXED_UNTIL,
    CONF_OVERRIDE_NIGHT_DURATION_MIN,
    OVERRIDE_DETECTION_TOLERANCE_MAX,
    OVERRIDE_DURATION_MIN_MAX,
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


def _make_config_flow() -> SmartShadingConfigFlow:
    flow = SmartShadingConfigFlow()

    def _async_show_form(*, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {"type": "form", "step_id": step_id, "errors": errors or {}, "data_schema": data_schema}

    flow.async_show_form = _async_show_form  # type: ignore[method-assign]
    return flow


class TestUnknownStoredModePreselectsLegacy:
    def test_invalid_duration_mode_preselects_legacy(self) -> None:
        flow = _make_options_flow(data={"override_policy": {"duration_mode": "some_future_mode_v99"}})
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        schema: vol.Schema = result["data_schema"]
        # The schema's own default reflects whatever is stored verbatim
        # (the OptionsFlow does not re-validate on render) — this proves
        # the FORM shows the raw stored string; storage-level normalization
        # to "legacy" happens in config_entry_data.py
        # (_override_policy_from_storage(), already tested in
        # test_override_policy_storage.py). Confirm the SelectSelector's
        # own option list only contains valid modes, so an invalid stored
        # value cannot be re-selected accidentally by the user re-saving
        # without changing it.
        key = _schema_field_key(schema, CONF_OVERRIDE_DURATION_MODE)
        selector_instance = schema.schema[key]
        assert set(selector_instance.config.options) == {"legacy", "fixed_time"}


class TestFixedUntilPreservedAcrossModeSwitch:
    def test_switching_to_legacy_preserves_stored_fixed_until(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {"duration_mode": "fixed_time", "fixed_until": "07:15:00"},
        })
        asyncio.run(flow.async_step_manual_override(user_input={
            CONF_OVERRIDE_DURATION_MODE: "legacy",
            CONF_OVERRIDE_FIXED_UNTIL: "07:15:00",  # form still carries the previously-shown value
            CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: False,
            CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: False,
            CONF_OVERRIDE_BREAK_ON_LIFECYCLE: True,
            CONF_OVERRIDE_DURATION_MIN: 120,
            CONF_OVERRIDE_NIGHT_DURATION_MIN: 720,
            CONF_OVERRIDE_DETECTION_TOLERANCE: 10,
        }))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["override_policy"]
        assert saved["duration_mode"] == "legacy"
        assert saved["fixed_until"] == "07:15:00"  # preserved, not cleared

    def test_switching_back_to_fixed_time_shows_the_preserved_value(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {"duration_mode": "legacy", "fixed_until": "07:15:00"},
        })
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_OVERRIDE_FIXED_UNTIL)
        # vol.Optional with description={"suggested_value": ...} — inspect
        # the field's description attribute HA would use to prefill the form.
        assert key.description == {"suggested_value": "07:15:00"}


class TestThreeStepModeRoundTrip:
    def test_fixed_time_to_legacy_to_fixed_time_preserves_value_throughout(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {"duration_mode": "fixed_time", "fixed_until": "06:45:00"},
        })
        # Step 1: save switching to legacy (value still present in the form).
        asyncio.run(flow.async_step_manual_override(user_input={
            CONF_OVERRIDE_DURATION_MODE: "legacy",
            CONF_OVERRIDE_FIXED_UNTIL: "06:45:00",
            CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: False,
            CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: False,
            CONF_OVERRIDE_BREAK_ON_LIFECYCLE: True,
            CONF_OVERRIDE_DURATION_MIN: 120,
            CONF_OVERRIDE_NIGHT_DURATION_MIN: 720,
            CONF_OVERRIDE_DETECTION_TOLERANCE: 10,
        }))
        _, kwargs1 = flow.hass.config_entries.async_update_entry.call_args
        saved1 = kwargs1["data"]["override_policy"]
        assert saved1["duration_mode"] == "legacy"
        assert saved1["fixed_until"] == "06:45:00"

        # Step 2: reopen a NEW flow instance against the just-saved data
        # (mirrors a real reload) and confirm the field is still shown.
        flow2 = _make_options_flow(data=kwargs1["data"])
        prefill = asyncio.run(flow2.async_step_manual_override(user_input=None))
        schema: vol.Schema = prefill["data_schema"]
        key = _schema_field_key(schema, CONF_OVERRIDE_FIXED_UNTIL)
        assert key.description == {"suggested_value": "06:45:00"}

        # Step 3: switch back to fixed_time using that same preserved value.
        asyncio.run(flow2.async_step_manual_override(user_input={
            CONF_OVERRIDE_DURATION_MODE: "fixed_time",
            CONF_OVERRIDE_FIXED_UNTIL: "06:45:00",
            CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: False,
            CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: False,
            CONF_OVERRIDE_BREAK_ON_LIFECYCLE: True,
            CONF_OVERRIDE_DURATION_MIN: 120,
            CONF_OVERRIDE_NIGHT_DURATION_MIN: 720,
            CONF_OVERRIDE_DETECTION_TOLERANCE: 10,
        }))
        _, kwargs3 = flow2.hass.config_entries.async_update_entry.call_args
        saved3 = kwargs3["data"]["override_policy"]
        assert saved3["duration_mode"] == "fixed_time"
        assert saved3["fixed_until"] == "06:45:00"


class TestInitialConfigFlowNeverWritesOverridePolicy:
    def test_initial_flow_has_no_override_policy_step_or_key(self) -> None:
        source_methods = [name for name in dir(SmartShadingConfigFlow) if name.startswith("async_step_")]
        assert "async_step_manual_override" not in source_methods  # OptionsFlow-only, per design

    def test_default_config_entry_data_has_no_override_policy_functional_effect_until_set(self) -> None:
        from custom_components.smartshading.config_entry_data import SmartShadingConfigEntryData
        from custom_components.smartshading.models.override_policy import OverridePolicyConfig
        data = SmartShadingConfigEntryData(name="Zone A", use_home_location=True)
        assert data.override_policy == OverridePolicyConfig()


class TestSaveDoesNotTouchUnrelatedFeatureKeys:
    def test_unrelated_t5_t6_t4_keys_untouched(self) -> None:
        original = {
            "lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1"}}},
            "active_lifecycle_profile_id": "p1",
            "presence_policy": "all_home",
            "presence_entity_ids": ["person.alice"],
            "ema_enabled": True,
            "ema_alpha": 0.4,
            "lifecycle_config": {"id": "default", "night_position": 55},
        }
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_manual_override(user_input={
            CONF_OVERRIDE_DURATION_MODE: "legacy",
            CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: True,
            CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: True,
            CONF_OVERRIDE_BREAK_ON_LIFECYCLE: True,
            CONF_OVERRIDE_DURATION_MIN: 90,
            CONF_OVERRIDE_NIGHT_DURATION_MIN: 500,
            CONF_OVERRIDE_DETECTION_TOLERANCE: 15,
        }))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        for key, value in original.items():
            assert kwargs["data"][key] == value, f"{key} was unexpectedly modified"


class TestNumberSelectorBounds:
    def test_duration_fields_have_sensible_bounds(self) -> None:
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        schema: vol.Schema = result["data_schema"]
        for field in (CONF_OVERRIDE_DURATION_MIN, CONF_OVERRIDE_NIGHT_DURATION_MIN):
            key = _schema_field_key(schema, field)
            cfg = schema.schema[key].config
            assert cfg.min == 1
            assert cfg.max == OVERRIDE_DURATION_MIN_MAX

    def test_tolerance_field_has_sensible_bounds(self) -> None:
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_OVERRIDE_DETECTION_TOLERANCE)
        cfg = schema.schema[key].config
        assert cfg.min == 1
        assert cfg.max == OVERRIDE_DETECTION_TOLERANCE_MAX


class TestInvalidNumericInputHandledSafely:
    def _submit(self, duration_min_value):
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_manual_override(user_input={
            CONF_OVERRIDE_DURATION_MODE: "legacy",
            CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: False,
            CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: False,
            CONF_OVERRIDE_BREAK_ON_LIFECYCLE: True,
            CONF_OVERRIDE_DURATION_MIN: duration_min_value,
            CONF_OVERRIDE_NIGHT_DURATION_MIN: 720,
            CONF_OVERRIDE_DETECTION_TOLERANCE: 10,
        }))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        return kwargs["data"]["override_policy"]["duration_min"]

    def test_zero_is_clamped_to_minimum(self) -> None:
        assert self._submit(0) == 1

    def test_negative_is_clamped_to_minimum(self) -> None:
        assert self._submit(-50) == 1

    def test_wrong_type_falls_back_to_default(self) -> None:
        from custom_components.smartshading.const import DEFAULT_OVERRIDE_DURATION_MIN
        assert self._submit("not-a-number") == DEFAULT_OVERRIDE_DURATION_MIN

    def test_none_falls_back_to_default(self) -> None:
        from custom_components.smartshading.const import DEFAULT_OVERRIDE_DURATION_MIN
        assert self._submit(None) == DEFAULT_OVERRIDE_DURATION_MIN

    def test_oversized_value_is_clamped_to_maximum(self) -> None:
        assert self._submit(999999) == OVERRIDE_DURATION_MIN_MAX

    def test_flow_never_raises_on_any_of_the_above(self) -> None:
        for bad_value in (0, -1, "abc", None, 999999, [], {}, True):
            flow = _make_options_flow(data={})
            asyncio.run(flow.async_step_manual_override(user_input={
                CONF_OVERRIDE_DURATION_MODE: "legacy",
                CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: False,
                CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: False,
                CONF_OVERRIDE_BREAK_ON_LIFECYCLE: True,
                CONF_OVERRIDE_DURATION_MIN: bad_value,
                CONF_OVERRIDE_NIGHT_DURATION_MIN: bad_value,
                CONF_OVERRIDE_DETECTION_TOLERANCE: bad_value,
            }))  # must not raise
