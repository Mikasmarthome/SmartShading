"""OptionsFlow edge cases for the Manual Override step — T7 pre-push
review point 13; updated for the T21 Phase B simplified release-mode UI.

Reuses the same real-selector-stub technique established in
test_config_flow_manual_override.py (which already covers menu
reachability, legacy defaults, stored-value preselection/mapping,
full-field save, fixed-time-without-time rejection, non-mutation on
render, unrelated-key preservation, and translation completeness — not
repeated here).

Coverage:
  EDGE-01  Unknown/invalid stored release_strategy is pre-selected as
           LIFECYCLE in the OptionsFlow (not just at the storage layer).
  EDGE-02  Switching Fixed Time -> Duration preserves the stored
           fixed_until value (documented, intentional — see config_flow.py
           comment).
  EDGE-03  Switching Duration -> Fixed Time re-displays a previously stored
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
  EDGE-08  release_mode_to_strategy() / release_strategy_to_mode() round-trip
           losslessly for all 7 legacy OverrideReleaseStrategy values (the
           mapping table T21 Phase B introduced).
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
    CONF_OVERRIDE_DECISION_FILTER,
    CONF_OVERRIDE_DETECTION_TOLERANCE,
    CONF_OVERRIDE_DURATION_MIN,
    CONF_OVERRIDE_FIXED_UNTIL,
    CONF_OVERRIDE_NIGHT_DURATION_MIN,
    CONF_OVERRIDE_RELEASE_MODE,
    CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED,
    CONF_OVERRIDE_TIME_BASED_KIND,
    CONF_OVERRIDE_USE_SYSTEM_DEFAULT,
    OVERRIDE_DETECTION_TOLERANCE_MAX,
    OVERRIDE_DURATION_MIN_MAX,
)
from custom_components.smartshading.models.manual_override import (  # noqa: E402
    DECISION_FILTER_ANY,
    TIME_BASED_KIND_DURATION,
    TIME_BASED_KIND_FIXED_TIME,
    OverrideReleaseMode,
    OverrideReleaseStrategy,
    release_mode_to_strategy,
    release_strategy_to_mode,
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


def _opt_out_of_system_default(flow: SmartShadingOptionsFlow, detection_tolerance: int = 10):
    """T21 Phase C2: the gate step (async_step_manual_override) must be
    answered with use_system_default=False before the 8-field custom form
    (async_step_manual_override_custom) is reachable."""
    return asyncio.run(flow.async_step_manual_override(user_input={
        CONF_OVERRIDE_USE_SYSTEM_DEFAULT: False,
        CONF_OVERRIDE_DETECTION_TOLERANCE: detection_tolerance,
    }))


_BASE_TIME_BASED_INPUT = {
    CONF_OVERRIDE_RELEASE_MODE: OverrideReleaseMode.TIME_BASED.value,
    CONF_OVERRIDE_TIME_BASED_KIND: TIME_BASED_KIND_DURATION,
    CONF_OVERRIDE_DECISION_FILTER: DECISION_FILTER_ANY,
    CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: False,
    CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: False,
    CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED: True,
    CONF_OVERRIDE_DURATION_MIN: 120,
    CONF_OVERRIDE_NIGHT_DURATION_MIN: 720,
    CONF_OVERRIDE_DETECTION_TOLERANCE: 10,
}


class TestUnknownStoredStrategyPreselectsLifecycle:
    def test_invalid_release_strategy_preselects_lifecycle_mode(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {"release_strategy": "some_future_mode_v99", "use_system_default": False},
        })
        result = _opt_out_of_system_default(flow)
        schema: vol.Schema = result["data_schema"]
        # An unrecognized stored strategy string falls back to LIFECYCLE at
        # the config_flow layer (mirrors config_entry_data.py's storage-level
        # fallback, already tested in test_override_policy_storage.py) —
        # confirm the schema's own default reflects that fallback, not the
        # raw invalid string, and that the mode selector's own option list
        # only contains the 4 real OverrideReleaseMode values.
        key = _schema_field_key(schema, CONF_OVERRIDE_RELEASE_MODE)
        assert key.default() == OverrideReleaseMode.LIFECYCLE.value
        selector_instance = schema.schema[key]
        assert set(selector_instance.config.options) == {m.value for m in OverrideReleaseMode}


class TestFixedUntilPreservedAcrossModeSwitch:
    def test_switching_to_duration_preserves_stored_fixed_until(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {
                "release_strategy": "fixed_time", "fixed_until": "07:15:00", "use_system_default": False,
            },
        })
        _opt_out_of_system_default(flow)
        asyncio.run(flow.async_step_manual_override_custom(user_input={
            **_BASE_TIME_BASED_INPUT,
            CONF_OVERRIDE_TIME_BASED_KIND: TIME_BASED_KIND_DURATION,
            CONF_OVERRIDE_FIXED_UNTIL: "07:15:00",  # form still carries the previously-shown value
        }))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["override_policy"]
        assert saved["release_strategy"] == "duration"
        assert saved["fixed_until"] == "07:15:00"  # preserved, not cleared

    def test_switching_back_to_fixed_time_shows_the_preserved_value(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {
                "release_strategy": "duration", "fixed_until": "07:15:00", "use_system_default": False,
            },
        })
        result = _opt_out_of_system_default(flow)
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_OVERRIDE_FIXED_UNTIL)
        # vol.Optional with description={"suggested_value": ...} — inspect
        # the field's description attribute HA would use to prefill the form.
        assert key.description == {"suggested_value": "07:15:00"}


class TestThreeStepModeRoundTrip:
    def test_fixed_time_to_duration_to_fixed_time_preserves_value_throughout(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {
                "release_strategy": "fixed_time", "fixed_until": "06:45:00", "use_system_default": False,
            },
        })
        # Step 1: save switching to duration (value still present in the form).
        _opt_out_of_system_default(flow)
        asyncio.run(flow.async_step_manual_override_custom(user_input={
            **_BASE_TIME_BASED_INPUT,
            CONF_OVERRIDE_TIME_BASED_KIND: TIME_BASED_KIND_DURATION,
            CONF_OVERRIDE_FIXED_UNTIL: "06:45:00",
        }))
        _, kwargs1 = flow.hass.config_entries.async_update_entry.call_args
        saved1 = kwargs1["data"]["override_policy"]
        assert saved1["release_strategy"] == "duration"
        assert saved1["fixed_until"] == "06:45:00"

        # Step 2: reopen a NEW flow instance against the just-saved data
        # (mirrors a real reload) and confirm the field is still shown.
        flow2 = _make_options_flow(data=kwargs1["data"])
        prefill = _opt_out_of_system_default(flow2)
        schema: vol.Schema = prefill["data_schema"]
        key = _schema_field_key(schema, CONF_OVERRIDE_FIXED_UNTIL)
        assert key.description == {"suggested_value": "06:45:00"}

        # Step 3: switch back to fixed_time using that same preserved value.
        asyncio.run(flow2.async_step_manual_override_custom(user_input={
            **_BASE_TIME_BASED_INPUT,
            CONF_OVERRIDE_TIME_BASED_KIND: TIME_BASED_KIND_FIXED_TIME,
            CONF_OVERRIDE_FIXED_UNTIL: "06:45:00",
        }))
        _, kwargs3 = flow2.hass.config_entries.async_update_entry.call_args
        saved3 = kwargs3["data"]["override_policy"]
        assert saved3["release_strategy"] == "fixed_time"
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
    def test_unrelated_t5_t4_keys_untouched(self) -> None:
        original = {
            "presence_policy": "all_home",
            "presence_entity_ids": ["person.alice"],
            "ema_enabled": True,
            "ema_alpha": 0.4,
            "lifecycle_config": {"id": "default", "night_position": 55},
        }
        flow = _make_options_flow(data=dict(original))
        _opt_out_of_system_default(flow)
        asyncio.run(flow.async_step_manual_override_custom(user_input=dict(_BASE_TIME_BASED_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        for key, value in original.items():
            assert kwargs["data"][key] == value, f"{key} was unexpectedly modified"

    def test_t6_lifecycle_profile_keys_are_migrated_and_dropped_by_any_save(self) -> None:
        # T21 Phase C3: a pre-C3 install's active-profile keys are NOT just
        # "left untouched" — they are migrated into "lifecycle_config" (the
        # active profile wins over the stale flat field) and then dropped,
        # by ANY subsequent OptionsFlow save, not just a lifecycle-specific one.
        flow = _make_options_flow(data={
            "lifecycle_profiles": {"p1": {"display_name": "Weekend", "config": {"id": "p1", "night_position": 77}}},
            "active_lifecycle_profile_id": "p1",
            "lifecycle_config": {"id": "default", "night_position": 55},
        })
        _opt_out_of_system_default(flow)
        asyncio.run(flow.async_step_manual_override_custom(user_input=dict(_BASE_TIME_BASED_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert "lifecycle_profiles" not in kwargs["data"]
        assert "active_lifecycle_profile_id" not in kwargs["data"]
        assert kwargs["data"]["lifecycle_config"]["night_position"] == 77


class TestNumberSelectorBounds:
    def test_duration_fields_have_sensible_bounds(self) -> None:
        flow = _make_options_flow(data={})
        result = _opt_out_of_system_default(flow)
        schema: vol.Schema = result["data_schema"]
        for field in (CONF_OVERRIDE_DURATION_MIN, CONF_OVERRIDE_NIGHT_DURATION_MIN):
            key = _schema_field_key(schema, field)
            cfg = schema.schema[key].config
            assert cfg.min == 1
            assert cfg.max == OVERRIDE_DURATION_MIN_MAX

    def test_tolerance_field_has_sensible_bounds(self) -> None:
        # detection_tolerance stays on the gate step (T21 Phase C2 — zone-only).
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
        _opt_out_of_system_default(flow)
        asyncio.run(flow.async_step_manual_override_custom(user_input={
            **_BASE_TIME_BASED_INPUT,
            CONF_OVERRIDE_DURATION_MIN: duration_min_value,
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
            _opt_out_of_system_default(flow)
            asyncio.run(flow.async_step_manual_override_custom(user_input={
                **_BASE_TIME_BASED_INPUT,
                CONF_OVERRIDE_DURATION_MIN: bad_value,
                CONF_OVERRIDE_NIGHT_DURATION_MIN: bad_value,
            }))  # must not raise


class TestReleaseModeMappingRoundTrip:
    """T21 Phase B's mapping table: every one of the 7 legacy
    OverrideReleaseStrategy values must round-trip losslessly through
    release_strategy_to_mode() -> release_mode_to_strategy()."""

    def test_all_seven_legacy_strategies_round_trip_losslessly(self) -> None:
        for strategy in OverrideReleaseStrategy:
            mode, sub_choice = release_strategy_to_mode(strategy)
            back = release_mode_to_strategy(mode, sub_choice)
            assert back is strategy, f"{strategy} -> {mode}/{sub_choice} -> {back}"

    def test_mapping_table_exact_values(self) -> None:
        expected = {
            OverrideReleaseStrategy.DURATION: (OverrideReleaseMode.TIME_BASED, TIME_BASED_KIND_DURATION),
            OverrideReleaseStrategy.FIXED_TIME: (OverrideReleaseMode.TIME_BASED, TIME_BASED_KIND_FIXED_TIME),
            OverrideReleaseStrategy.LIFECYCLE: (OverrideReleaseMode.LIFECYCLE, None),
            OverrideReleaseStrategy.FIRST_COMFORT: (OverrideReleaseMode.NEXT_DECISION, "comfort"),
            OverrideReleaseStrategy.FIRST_PROTECTION: (OverrideReleaseMode.NEXT_DECISION, "protection"),
            OverrideReleaseStrategy.FIRST_ANY_DECISION: (OverrideReleaseMode.NEXT_DECISION, "any"),
            OverrideReleaseStrategy.MANUAL: (OverrideReleaseMode.MANUAL, None),
        }
        for strategy, (expected_mode, expected_sub) in expected.items():
            mode, sub = release_strategy_to_mode(strategy)
            assert mode is expected_mode
            assert sub == expected_sub
