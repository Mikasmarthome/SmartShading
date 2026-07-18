"""OptionsFlow schema/save coverage for the Manual Override policy step —
v1.2.0-beta.1, T7.

Same real-selector-stub technique established in
tests/test_config_flow_presence_policy.py (T5) / test_config_flow_lifecycle_profile.py (T6).

Coverage:
  CFMO-01  Menu reaches "manual_override" (reachability).
  CFMO-02  Legacy defaults pre-selected when nothing stored.
  CFMO-03  Stored values pre-selected on reopen.
  CFMO-04  Saving persists every field into "override_policy".
  CFMO-05  fixed_time mode without a fixed_until value is rejected with an
           error, no save happens (no crash, deterministic).
  CFMO-06  Form render (user_input=None) does not mutate ConfigEntry.data.
  CFMO-07  Unrelated top-level keys untouched by a save.
  CFMO-08  Translation/selector-key parity across all 25 files, no English
           leftovers, no unimplemented-feature strings (e.g. sun event
           expiry, select entity, presence-bound strategy, staged actions).
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
from custom_components.smartshading.config_flow import SmartShadingOptionsFlow  # noqa: E402
from custom_components.smartshading.const import (  # noqa: E402
    CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS,
    CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS,
    CONF_OVERRIDE_BREAK_ON_LIFECYCLE,
    CONF_OVERRIDE_DETECTION_TOLERANCE,
    CONF_OVERRIDE_DURATION_MIN,
    CONF_OVERRIDE_DURATION_MODE,
    CONF_OVERRIDE_FIXED_UNTIL,
    CONF_OVERRIDE_NIGHT_DURATION_MIN,
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


_FULL_INPUT = {
    CONF_OVERRIDE_DURATION_MODE: "fixed_time",
    CONF_OVERRIDE_FIXED_UNTIL: "08:00:00",
    CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS: True,
    CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS: True,
    CONF_OVERRIDE_BREAK_ON_LIFECYCLE: False,
    CONF_OVERRIDE_DURATION_MIN: 90,
    CONF_OVERRIDE_NIGHT_DURATION_MIN: 600,
    CONF_OVERRIDE_DETECTION_TOLERANCE: 15,
}


class TestMenuReachability:
    def test_manual_override_in_init_menu(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_init(user_input=None))
        assert result["type"] == "menu"
        assert "manual_override" in result["menu_options"]


class TestLegacyDefaultsPreselected:
    def test_defaults_when_nothing_stored(self):
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_DURATION_MODE).default() == "legacy"
        assert _schema_field_key(schema, CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS).default() is False
        assert _schema_field_key(schema, CONF_OVERRIDE_ALLOW_PROTECTION_ACTIONS).default() is False
        assert _schema_field_key(schema, CONF_OVERRIDE_BREAK_ON_LIFECYCLE).default() is True
        assert _schema_field_key(schema, CONF_OVERRIDE_DURATION_MIN).default() == 120
        assert _schema_field_key(schema, CONF_OVERRIDE_NIGHT_DURATION_MIN).default() == 720
        assert _schema_field_key(schema, CONF_OVERRIDE_DETECTION_TOLERANCE).default() == 10


class TestStoredValuesPreselected:
    def test_stored_values_shown_on_reopen(self):
        flow = _make_options_flow(data={
            "override_policy": {
                "duration_mode": "fixed_time", "fixed_until": "09:30:00",
                "allow_comfort_actions": True, "allow_protection_actions": False,
                "duration_min": 60, "night_duration_min": 500,
                "detection_tolerance": 20, "break_on_lifecycle": False,
            }
        })
        result = asyncio.run(flow.async_step_manual_override(user_input=None))
        schema: vol.Schema = result["data_schema"]
        assert _schema_field_key(schema, CONF_OVERRIDE_DURATION_MODE).default() == "fixed_time"
        assert _schema_field_key(schema, CONF_OVERRIDE_ALLOW_COMFORT_ACTIONS).default() is True
        assert _schema_field_key(schema, CONF_OVERRIDE_DURATION_MIN).default() == 60


class TestSavePersistsEveryField:
    def test_full_input_saved(self):
        flow = _make_options_flow(data={})
        asyncio.run(flow.async_step_manual_override(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["override_policy"]
        assert saved["duration_mode"] == "fixed_time"
        assert saved["fixed_until"] == "08:00:00"
        assert saved["allow_comfort_actions"] is True
        assert saved["allow_protection_actions"] is True
        assert saved["break_on_lifecycle"] is False
        assert saved["duration_min"] == 90
        assert saved["night_duration_min"] == 600
        assert saved["detection_tolerance"] == 15

    def test_legacy_mode_saved_without_fixed_until(self):
        flow = _make_options_flow(data={})
        legacy_input = dict(_FULL_INPUT)
        legacy_input[CONF_OVERRIDE_DURATION_MODE] = "legacy"
        legacy_input.pop(CONF_OVERRIDE_FIXED_UNTIL, None)
        asyncio.run(flow.async_step_manual_override(user_input=legacy_input))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        saved = kwargs["data"]["override_policy"]
        assert saved["duration_mode"] == "legacy"
        assert saved["fixed_until"] is None


class TestFixedTimeRequiresFixedUntil:
    def test_missing_fixed_until_rejected(self):
        flow = _make_options_flow(data={})
        bad_input = dict(_FULL_INPUT)
        bad_input.pop(CONF_OVERRIDE_FIXED_UNTIL, None)
        result = asyncio.run(flow.async_step_manual_override(user_input=bad_input))
        assert result["type"] == "form"
        assert result["errors"].get("base") == "override_fixed_until_required"
        assert not flow.hass.config_entries.async_update_entry.called


class TestNoMutationOnRender:
    def test_render_does_not_touch_entry(self):
        original = {"override_policy": {"duration_mode": "legacy"}}
        flow = _make_options_flow(data=dict(original))
        asyncio.run(flow.async_step_manual_override(user_input=None))
        assert flow._config_entry.data == original
        assert not flow.hass.config_entries.async_update_entry.called


class TestUnrelatedKeysUntouched:
    def test_unrelated_keys_preserved(self):
        flow = _make_options_flow(data={"name": "Zone A", "windows": ["do-not-touch"]})
        asyncio.run(flow.async_step_manual_override(user_input=dict(_FULL_INPUT)))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["name"] == "Zone A"
        assert kwargs["data"]["windows"] == ["do-not-touch"]


class TestTranslationCompleteness:
    def _all_i18n_files(self):
        yield _INTEGRATION_ROOT / "strings.json"
        yield from sorted((_INTEGRATION_ROOT / "translations").glob("*.json"))

    def test_every_file_has_manual_override_strings(self):
        files = list(self._all_i18n_files())
        assert len(files) == 25
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            opt = data["options"]
            assert "manual_override" in opt["step"]["init"]["menu_options"], path.name
            assert "manual_override" in opt["step"], path.name
            assert opt["step"]["manual_override"]["title"], path.name
            for key in (
                "override_duration_mode", "override_fixed_until",
                "override_allow_comfort_actions", "override_allow_protection_actions",
                "override_break_on_lifecycle", "override_duration_min",
                "override_night_duration_min", "override_detection_tolerance",
            ):
                assert key in opt["step"]["manual_override"]["data"], f"{path.name}: missing {key}"
            assert "override_fixed_until_required" in opt.get("error", {}), path.name
            assert "override_duration_mode" in data.get("selector", {}), path.name
            assert set(data["selector"]["override_duration_mode"]["options"].keys()) == {"legacy", "fixed_time"}, path.name

    def test_no_english_leftovers_in_translations(self):
        en = json.loads((_INTEGRATION_ROOT / "translations" / "en.json").read_text(encoding="utf-8"))
        en_strings = {
            en["options"]["step"]["init"]["menu_options"]["manual_override"],
            en["options"]["step"]["manual_override"]["title"],
            en["options"]["error"]["override_fixed_until_required"],
        }
        for path in sorted((_INTEGRATION_ROOT / "translations").glob("*.json")):
            if path.name == "en.json":
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            opt = data["options"]
            strings = {
                opt["step"]["init"]["menu_options"]["manual_override"],
                opt["step"]["manual_override"]["title"],
                opt["error"]["override_fixed_until_required"],
            }
            assert not (strings & en_strings), f"{path.name} has untranslated English text"

    def test_no_unimplemented_feature_strings_referenced(self):
        """T7 scope decision: no sun-event expiry, no select entity, no
        presence-bound strategy, no staged actions, no per-profile override
        config — none of these should be implied anywhere in the shipped
        UI strings for this feature."""
        en = json.loads((_INTEGRATION_ROOT / "strings.json").read_text(encoding="utf-8"))
        haystack = json.dumps(en["options"]["step"]["manual_override"]).lower()
        for forbidden in (
            "sunset", "sunrise", "select entity", "presence", "staged", "profile",
        ):
            assert forbidden not in haystack, forbidden
