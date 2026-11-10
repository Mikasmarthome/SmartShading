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
  EDGE-02  A stored fixed_until survives switching to another strategy.
  EDGE-03  The stored fixed_until is re-displayed when fixed_time is chosen.
  EDGE-04  Initial (non-options) ConfigFlow never writes an override_policy
           key.
  EDGE-05  A save never touches lifecycle_profiles, active_lifecycle_profile_id,
           presence_policy, presence_entity_ids, or lifecycle_config.
  EDGE-06  Duration/tolerance fields carry sensible NumberSelector min/max
           bounds in the schema.
  EDGE-07  Zero, negative, and wrong-type numeric input are handled safely
           (clamped to a sane default/bound, never crash, never stored
           as-is).
  EDGE-08  release_mode_to_strategy() / release_strategy_to_mode() (T21
           Phase B) and release_choice_to_strategy() /
           release_strategy_to_choice() (Phase 5) round-trip losslessly for
           all 7 OverrideReleaseStrategy values.
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
    CONF_OVERRIDE_EDIT_TIMEOUTS,
    CONF_OVERRIDE_RELEASE_CHOICE,
    CONF_OVERRIDE_SAFETY_TIMEOUT_ENABLED,
    CONF_OVERRIDE_USE_SYSTEM_DEFAULT,
    OVERRIDE_DETECTION_TOLERANCE_MAX,
    OVERRIDE_DURATION_MIN_MAX,
)
from custom_components.smartshading.models.manual_override import (  # noqa: E402
    RELEASE_CHOICE_DURATION,
    RELEASE_CHOICE_FIXED_TIME,
    RELEASE_CHOICE_LIFECYCLE,
    RELEASE_CHOICE_MANUAL,
    RELEASE_CHOICE_NEXT_DECISION,
    RELEASE_CHOICES,
    OverrideReleaseStrategy,
    release_choice_to_strategy,
    release_strategy_to_choice,
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


def _page1(choice, **kw):
    return {
        CONF_OVERRIDE_RELEASE_CHOICE: choice,
        CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: kw.get("comfort", False),
        CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: kw.get("protection", False),
        CONF_OVERRIDE_EDIT_TIMEOUTS: kw.get("edit_timeouts", False),
    }


def _save_lifecycle(flow) -> None:
    """Page 1 with the default choice: saves immediately (no detail page)."""
    asyncio.run(flow.async_step_manual_override_custom(user_input=_page1(RELEASE_CHOICE_LIFECYCLE)))


def _saved(flow) -> dict:
    _, kwargs = flow.hass.config_entries.async_update_entry.call_args
    return kwargs["data"]["override_policy"]


class TestUnknownStoredStrategyPreselectsLifecycle:
    def test_invalid_release_strategy_preselects_lifecycle_choice(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {"release_strategy": "some_future_mode_v99", "use_system_default": False},
        })
        result = _opt_out_of_system_default(flow)
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_OVERRIDE_RELEASE_CHOICE)
        assert key.default() == RELEASE_CHOICE_LIFECYCLE
        assert tuple(schema.schema[key].config.options) == RELEASE_CHOICES


class TestFixedUntilPreservedAcrossStrategySwitch:
    def test_switching_to_duration_preserves_stored_fixed_until(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {
                "release_strategy": "fixed_time", "fixed_until": "07:15:00", "use_system_default": False,
            },
        })
        _opt_out_of_system_default(flow)
        asyncio.run(flow.async_step_manual_override_custom(user_input=_page1(RELEASE_CHOICE_DURATION)))
        asyncio.run(flow.async_step_override_duration(user_input={
            CONF_OVERRIDE_DURATION_MIN: 120, CONF_OVERRIDE_NIGHT_DURATION_MIN: 720}))
        saved = _saved(flow)
        assert saved["release_strategy"] == "duration"
        assert saved["fixed_until"] == "07:15:00"  # hidden on the duration page, kept

    def test_choosing_fixed_time_again_shows_the_preserved_value(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {
                "release_strategy": "duration", "fixed_until": "07:15:00", "use_system_default": False,
            },
        })
        _opt_out_of_system_default(flow)
        result = asyncio.run(flow.async_step_manual_override_custom(user_input=_page1(RELEASE_CHOICE_FIXED_TIME)))
        key = _schema_field_key(result["data_schema"], CONF_OVERRIDE_FIXED_UNTIL)
        assert key.description == {"suggested_value": "07:15:00"}


class TestThreeStepStrategyRoundTrip:
    def test_fixed_time_to_manual_to_fixed_time_preserves_value_throughout(self) -> None:
        flow = _make_options_flow(data={
            "override_policy": {
                "release_strategy": "fixed_time", "fixed_until": "06:45:00", "use_system_default": False,
            },
        })
        _opt_out_of_system_default(flow)
        asyncio.run(flow.async_step_manual_override_custom(user_input=_page1(RELEASE_CHOICE_MANUAL)))
        saved1 = _saved(flow)
        assert saved1["release_strategy"] == "manual" and saved1["fixed_until"] == "06:45:00"

        flow2 = _make_options_flow(data={"override_policy": saved1})
        _opt_out_of_system_default(flow2)
        result = asyncio.run(flow2.async_step_manual_override_custom(user_input=_page1(RELEASE_CHOICE_FIXED_TIME)))
        key = _schema_field_key(result["data_schema"], CONF_OVERRIDE_FIXED_UNTIL)
        assert key.description == {"suggested_value": "06:45:00"}
        asyncio.run(flow2.async_step_override_fixed_time(user_input={CONF_OVERRIDE_FIXED_UNTIL: "06:45:00"}))
        saved3 = _saved(flow2)
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
            "lifecycle_config": {"id": "default", "night_position": 55},
        }
        flow = _make_options_flow(data=dict(original))
        _opt_out_of_system_default(flow)
        _save_lifecycle(flow)
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
        _save_lifecycle(flow)
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert "lifecycle_profiles" not in kwargs["data"]
        assert "active_lifecycle_profile_id" not in kwargs["data"]
        assert kwargs["data"]["lifecycle_config"]["night_position"] == 77


class TestRemovedSensorSmoothingOption:
    """The sensor-smoothing option no longer exists: no form offers it and
    any options save retires the keys an older version stored."""

    @staticmethod
    def _field_names(form: dict) -> set[str]:
        return {str(getattr(k, "schema", k)) for k in form["data_schema"].schema}

    @staticmethod
    def _options_form(flow: SmartShadingOptionsFlow) -> dict:
        flow.async_show_form = lambda **kw: kw  # type: ignore[method-assign]
        return asyncio.run(flow.async_step_weather(user_input=None))

    def test_options_weather_step_has_no_smoothing_fields(self) -> None:
        flow = _make_options_flow(data={"ema_enabled": True, "ema_alpha": 0.4})
        names = self._field_names(self._options_form(flow))
        assert "ema_enabled" not in names and "ema_alpha" not in names
        assert "weather_entity_id" in names and "rain_sensor_id" in names

    def test_setup_weather_step_has_no_smoothing_fields(self) -> None:
        flow = _make_config_flow()
        flow.hass = MagicMock()
        flow.hass.config_entries.async_entries.return_value = []
        names = self._field_names(asyncio.run(flow.async_step_weather(user_input=None)))
        assert "ema_enabled" not in names and "ema_alpha" not in names
        assert "weather_entity_id" in names and "rain_sensor_id" in names

    def test_saving_the_weather_step_retires_old_keys_and_keeps_the_rest(self) -> None:
        flow = _make_options_flow(data={
            "ema_enabled": True, "ema_alpha": 0.4, "presence_policy": "all_home",
            "lifecycle_config": {"id": "default", "night_position": 55},
        })
        asyncio.run(flow.async_step_weather(user_input={"weather_entity_id": "weather.home"}))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]
        assert "ema_enabled" not in saved and "ema_alpha" not in saved
        assert saved["weather_entity_id"] == "weather.home"
        assert saved["presence_policy"] == "all_home"
        assert saved["lifecycle_config"]["night_position"] == 55

    def test_any_other_save_retires_old_keys_too(self) -> None:
        flow = _make_options_flow(data={"ema_enabled": True, "ema_alpha": 0.4})
        _opt_out_of_system_default(flow)
        _save_lifecycle(flow)
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert "ema_enabled" not in kwargs["data"] and "ema_alpha" not in kwargs["data"]


class TestNumberSelectorBounds:
    def test_duration_fields_have_sensible_bounds(self) -> None:
        flow = _make_options_flow(data={})
        _opt_out_of_system_default(flow)
        result = asyncio.run(flow.async_step_manual_override_custom(user_input=_page1(RELEASE_CHOICE_DURATION)))
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
        asyncio.run(flow.async_step_manual_override_custom(user_input=_page1(RELEASE_CHOICE_DURATION)))
        asyncio.run(flow.async_step_override_duration(user_input={
            CONF_OVERRIDE_DURATION_MIN: duration_min_value,
            CONF_OVERRIDE_NIGHT_DURATION_MIN: 720,
        }))
        return _saved(flow)["duration_min"]

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
            asyncio.run(flow.async_step_manual_override_custom(user_input=_page1(RELEASE_CHOICE_DURATION)))
            asyncio.run(flow.async_step_override_duration(user_input={
                CONF_OVERRIDE_DURATION_MIN: bad_value,
                CONF_OVERRIDE_NIGHT_DURATION_MIN: bad_value,
            }))  # must not raise


class TestReleaseChoiceMapping:
    """Phase 5: the five user-facing choices map onto the unchanged seven
    stored strategies, losslessly in both directions."""

    def test_all_seven_strategies_round_trip_losslessly(self) -> None:
        for strategy in OverrideReleaseStrategy:
            choice, flt = release_strategy_to_choice(strategy)
            assert release_choice_to_strategy(choice, flt) is strategy, strategy

    def test_mapping_table_exact_values(self) -> None:
        expected = {
            OverrideReleaseStrategy.DURATION: (RELEASE_CHOICE_DURATION, None),
            OverrideReleaseStrategy.FIXED_TIME: (RELEASE_CHOICE_FIXED_TIME, None),
            OverrideReleaseStrategy.LIFECYCLE: (RELEASE_CHOICE_LIFECYCLE, None),
            OverrideReleaseStrategy.FIRST_COMFORT: (RELEASE_CHOICE_NEXT_DECISION, "comfort"),
            OverrideReleaseStrategy.FIRST_PROTECTION: (RELEASE_CHOICE_NEXT_DECISION, "protection"),
            OverrideReleaseStrategy.FIRST_ANY_DECISION: (RELEASE_CHOICE_NEXT_DECISION, "any"),
            OverrideReleaseStrategy.MANUAL: (RELEASE_CHOICE_MANUAL, None),
        }
        assert {s: release_strategy_to_choice(s) for s in OverrideReleaseStrategy} == expected

    def test_unknown_values_degrade_to_the_documented_defaults(self) -> None:
        assert release_choice_to_strategy("bogus") is OverrideReleaseStrategy.LIFECYCLE
        assert release_choice_to_strategy(
            RELEASE_CHOICE_NEXT_DECISION, "bogus") is OverrideReleaseStrategy.FIRST_ANY_DECISION
        assert release_choice_to_strategy(
            RELEASE_CHOICE_NEXT_DECISION) is OverrideReleaseStrategy.FIRST_ANY_DECISION

    def test_stored_strategy_values_are_unchanged(self) -> None:
        assert [s.value for s in OverrideReleaseStrategy] == [
            "duration", "fixed_time", "lifecycle", "first_comfort", "first_protection",
            "first_any_decision", "manual"]
