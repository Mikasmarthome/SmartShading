"""ConfigFlow / OptionsFlow schema coverage for presence_policy — v1.2.0-beta.1,
Beta.1-T5 pre-push review follow-up.

Prior tickets (T1-T4) treated config_flow.py as untestable under this repo's
HA-stub harness (it imports the real homeassistant.helpers.selector, which
tests/conftest.py does not stub) and substituted const.py CONF_* constant
checks instead. That is a real coverage gap, not just an inconvenience: it
never actually exercises config_flow.py's schema-building or save logic.

This module closes the gap FOR PRESENCE_POLICY specifically by providing a
minimal, faithful `homeassistant.helpers.selector` stub (selectors that just
record their constructor kwargs, matching real HA's dataclass-like selector
config objects closely enough to inspect `options=`/`default=`/
`suggested_value=` without needing to render an actual HA form) so
config_flow.py can be imported and its real async_step_presence() methods
can be driven directly, using the REAL voluptuous package (already an
installed dependency, not stubbed) to build and inspect the actual
vol.Schema objects config_flow.py produces.

Coverage:
  CFP-01  CONF_PRESENCE_POLICY field exists in the initial ConfigFlow
          presence-step schema.
  CFP-02  CONF_PRESENCE_POLICY field exists in the OptionsFlow presence-step
          schema.
  CFP-03  Legacy default (ANY_HOME) is what the initial ConfigFlow schema
          proposes when nothing is prefilled.
  CFP-04  The SelectSelector offers exactly the 3 implemented enum values —
          no more, no fewer.
  CFP-05  A stored non-default value is pre-selected in the OptionsFlow form.
  CFP-06  Submitting a value in the initial ConfigFlow updates flow state
          (self._presence_policy), which is what final SmartShadingConfigEntryData
          construction reads (config_flow.py:~1013 `presence_policy=self._presence_policy`).
  CFP-07  Submitting a value in the OptionsFlow reaches the actual
          hass.config_entries.async_update_entry() call with the resolved
          .value string under CONF_PRESENCE_POLICY.
  CFP-08  An invalid/unrecognized submitted value falls back safely to
          ANY_HOME in both flows (never raises, never stores garbage).
  CFP-09  strings.json + all 24 translations each contain the presence_policy
          label, description, and all 3 (and only 3) selector options.
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
# HA stubs — must precede any config_flow import in this module. Builds on
# tests/conftest.py's baseline homeassistant.config_entries stub (already
# provides ConfigFlow/OptionsFlow base classes) and adds the ONE missing
# piece: homeassistant.helpers.selector.
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _SelectorConfigBase:
    """Records constructor kwargs as attributes — matches real HA's
    dataclass-like *SelectorConfig objects closely enough to inspect
    options=/mode=/translation_key=/domain=/etc. without needing a real
    HA selector schema validator."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class EntitySelectorConfig(_SelectorConfigBase):
    pass


class NumberSelectorConfig(_SelectorConfigBase):
    pass


class SelectSelectorConfig(_SelectorConfigBase):
    pass


class _SelectorBase:
    """Callable identity passthrough so voluptuous can use instances as
    schema values without a real HA selector validator implementation."""

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
    CONF_PRESENCE_POLICY,
    DEFAULT_PRESENCE_POLICY,
    PRESENCE_POLICY_OPTIONS,
)
from custom_components.smartshading.models.presence import PresencePolicy  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


def _schema_field_key(schema: vol.Schema, field_name: str):
    """Return the vol.Marker (Required/Optional) for `field_name`, or None."""
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


def _make_config_flow() -> SmartShadingConfigFlow:
    """tests/conftest.py's baseline ConfigFlow stub (_ConfigFlowStub) only
    implements __init_subclass__ (domain= support) — real HA's FlowHandler
    base class provides async_show_form(), which SmartShadingConfigFlow
    relies on for every step. Attach a minimal, faithful equivalent (same
    return shape as the OptionsFlow stub in conftest.py) so a full step
    call — including the async_step_window() step reached after
    async_step_presence() submits — can render its own form without
    raising AttributeError."""
    flow = SmartShadingConfigFlow()

    def _async_show_form(*, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {"type": "form", "step_id": step_id, "errors": errors or {}, "data_schema": data_schema}

    flow.async_show_form = _async_show_form  # type: ignore[method-assign]
    return flow


# ---------------------------------------------------------------------------
# CFP-01 / CFP-03 / CFP-04 — initial ConfigFlow presence-step schema.
# ---------------------------------------------------------------------------

class TestInitialConfigFlowSchema:
    def test_presence_policy_field_present_with_legacy_default(self):
        async def _run():
            flow = _make_config_flow()
            flow.hass = MagicMock()
            flow.hass.config_entries = MagicMock()
            flow.hass.config_entries.async_entries = MagicMock(return_value=[])
            return await flow.async_step_presence(user_input=None)

        result = asyncio.run(_run())
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_PRESENCE_POLICY)
        assert key is not None, "presence_policy field missing from initial ConfigFlow schema"
        assert key.default() == DEFAULT_PRESENCE_POLICY

    def test_selector_offers_exactly_three_values(self):
        async def _run():
            flow = _make_config_flow()
            flow.hass = MagicMock()
            flow.hass.config_entries = MagicMock()
            flow.hass.config_entries.async_entries = MagicMock(return_value=[])
            return await flow.async_step_presence(user_input=None)

        result = asyncio.run(_run())
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_PRESENCE_POLICY)
        selector_instance = schema.schema[key]
        assert set(selector_instance.config.options) == {"any_home", "all_home", "inverted_any_home"}
        assert set(selector_instance.config.options) == set(PRESENCE_POLICY_OPTIONS)


# ---------------------------------------------------------------------------
# CFP-02 / CFP-05 — OptionsFlow presence-step schema.
# ---------------------------------------------------------------------------

class TestOptionsFlowSchema:
    def test_presence_policy_field_present(self):
        entry = _make_entry(data={})
        flow = SmartShadingOptionsFlow(entry)
        result = asyncio.run(flow.async_step_presence(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_PRESENCE_POLICY)
        assert key is not None, "presence_policy field missing from OptionsFlow schema"
        assert key.default() == DEFAULT_PRESENCE_POLICY

    def test_stored_non_default_value_is_preselected(self):
        entry = _make_entry(data={CONF_PRESENCE_POLICY: "all_home"})
        flow = SmartShadingOptionsFlow(entry)
        result = asyncio.run(flow.async_step_presence(user_input=None))
        schema: vol.Schema = result["data_schema"]
        key = _schema_field_key(schema, CONF_PRESENCE_POLICY)
        assert key.default() == "all_home"


# ---------------------------------------------------------------------------
# CFP-06 — submitted value updates initial ConfigFlow state.
# ---------------------------------------------------------------------------

class TestInitialConfigFlowSubmission:
    def test_submitted_value_updates_flow_state(self):
        flow = _make_config_flow()
        flow.hass = MagicMock()
        asyncio.run(flow.async_step_presence(user_input={
            "presence_entity_ids": [],
            "absence_delay_min": 30,
            CONF_PRESENCE_POLICY: "all_home",
        }))
        assert flow._presence_policy is PresencePolicy.ALL_HOME

    def test_invalid_submitted_value_falls_back_to_any_home(self):
        flow = _make_config_flow()
        flow.hass = MagicMock()
        asyncio.run(flow.async_step_presence(user_input={
            "presence_entity_ids": [],
            "absence_delay_min": 30,
            CONF_PRESENCE_POLICY: "vacation_mode_bogus",
        }))
        assert flow._presence_policy is PresencePolicy.ANY_HOME


# ---------------------------------------------------------------------------
# CFP-07 / CFP-08 — OptionsFlow submission reaches real storage call.
# ---------------------------------------------------------------------------

class TestOptionsFlowSubmission:
    def test_submitted_value_reaches_async_update_entry(self):
        entry = _make_entry(data={})
        flow = SmartShadingOptionsFlow(entry)
        flow.hass = MagicMock()
        asyncio.run(flow.async_step_presence(user_input={
            "presence_entity_ids": [],
            "absence_delay_min": 30,
            CONF_PRESENCE_POLICY: "inverted_any_home",
        }))
        assert flow.hass.config_entries.async_update_entry.called
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_PRESENCE_POLICY] == "inverted_any_home"

    def test_invalid_submitted_value_falls_back_and_stores_any_home(self):
        entry = _make_entry(data={})
        flow = SmartShadingOptionsFlow(entry)
        flow.hass = MagicMock()
        asyncio.run(flow.async_step_presence(user_input={
            "presence_entity_ids": [],
            "absence_delay_min": 30,
            CONF_PRESENCE_POLICY: "vacation_mode_bogus",
        }))
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][CONF_PRESENCE_POLICY] == "any_home"


# ---------------------------------------------------------------------------
# CFP-09 — i18n completeness across strings.json + all 24 translations.
# ---------------------------------------------------------------------------

class TestTranslationCompleteness:
    def _all_i18n_files(self):
        yield _INTEGRATION_ROOT / "strings.json"
        yield from sorted((_INTEGRATION_ROOT / "translations").glob("*.json"))

    def test_every_file_has_label_description_and_exactly_three_options(self):
        files = list(self._all_i18n_files())
        assert len(files) == 25
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            for section in ("config", "options"):
                presence = data[section]["step"]["presence"]
                assert "presence_policy" in presence["data"], f"{path.name}: missing label ({section})"
                assert presence["data"]["presence_policy"], f"{path.name}: empty label ({section})"
                assert "presence_policy" in presence.get("data_description", {}), f"{path.name}: missing description ({section})"
                assert presence["data_description"]["presence_policy"], f"{path.name}: empty description ({section})"
            options = set(data["selector"]["presence_policy"]["options"].keys())
            assert options == {"any_home", "all_home", "inverted_any_home"}, (
                f"{path.name}: unexpected/missing selector options: {options}"
            )
