"""Absence position 0 % must stay 0 % — prefill, storage, runtime.

The Presence options step used to prefill the absence position with a
truthiness fallback, so a stored 0 % was shown as the default (10 %) and an
unchanged re-save wrote the default back.  0 is a valid position; only a
missing / None value falls back to the default.

Same real-selector-stub technique as tests/test_config_flow_presence_system_default.py.

Coverage:
  APZ-01 Prefill shows every stored position unchanged, including 0.
  APZ-02 Missing / None / empty zone data falls back to the default.
  APZ-03 Opening the form and saving it unchanged keeps the stored value.
  APZ-04 0 survives the options save and the storage round trip.
  APZ-05 0 % resolves to a fully closed absence target at runtime
         (zone and window level); an unset zone still inherits the global value.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
import voluptuous as vol

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_config_flow_presence_system_default.py.
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
from custom_components.smartshading.config_entry_data import (  # noqa: E402
    from_storage_dict,
    to_storage_dict,
)
from custom_components.smartshading.config_flow import SmartShadingOptionsFlow  # noqa: E402
from custom_components.smartshading.const import (  # noqa: E402
    CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT,
    CONF_ABSENCE_POSITION,
    CONF_PRESENCE_ENTITY_IDS,
    CONF_PRESENCE_POLICY,
    DEFAULT_ABSENCE_POSITION,
)
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator  # noqa: E402
from custom_components.smartshading.models.comfort import ComfortConfig  # noqa: E402
from custom_components.smartshading.models.config import (  # noqa: E402
    GlobalDefaults,
    ShadePositionDefaults,
)
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    LifecycleState,
    NightDayLifecycleConfig,
)
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
from custom_components.smartshading.models.window_decision_input import (  # noqa: E402
    build_window_decision_input,
)
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402

_VALID_POSITIONS = [0, 1, 10, 30, 70, 99, 100]


def _field(schema: vol.Schema, name: str):
    for key in schema.schema:
        if str(key) == name:
            return key
    raise AssertionError(f"{name} not in schema")


def _make_flow(data: dict) -> SmartShadingOptionsFlow:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = data
    entry.options = {}
    flow = SmartShadingOptionsFlow(entry)
    flow.hass = MagicMock()
    return flow


def _zone_data(position: Any) -> dict:
    return {"name": "Zone", "zones": [{"id": "z1", "name": "Zone", "absence_position": position}]}


def _prefill(data: dict) -> Any:
    flow = _make_flow(data)
    schema = asyncio.run(flow.async_step_presence(user_input=None))["data_schema"]
    return _field(schema, CONF_ABSENCE_POSITION).description["suggested_value"]


def _gate_submit(flow: SmartShadingOptionsFlow, position: Any) -> dict:
    """Submit the gate the way the frontend does and return the saved entry data."""
    asyncio.run(flow.async_step_presence(user_input={
        CONF_PRESENCE_ENTITY_IDS: [],
        CONF_ABSENCE_DELAY_USE_SYSTEM_DEFAULT: True,
        CONF_ABSENCE_POSITION: position,
        CONF_PRESENCE_POLICY: "any_home",
    }))
    _, kwargs = flow.hass.config_entries.async_update_entry.call_args
    return kwargs["data"]


# ---------------------------------------------------------------------------
# APZ-01 / APZ-02: prefill
# ---------------------------------------------------------------------------


class TestPrefill:
    @pytest.mark.parametrize("position", _VALID_POSITIONS)
    def test_stored_position_is_shown_unchanged(self, position):
        assert _prefill(_zone_data(position)) == position

    def test_zero_is_not_replaced_by_the_default(self):
        assert DEFAULT_ABSENCE_POSITION != 0
        assert _prefill(_zone_data(0)) == 0

    @pytest.mark.parametrize("data", [
        {},
        {"zones": []},
        {"zones": [{}]},
        {"zones": [{"absence_position": None}]},
        {"zones": None},
    ])
    def test_missing_or_none_falls_back_to_the_default(self, data):
        assert _prefill(data) == DEFAULT_ABSENCE_POSITION


# ---------------------------------------------------------------------------
# APZ-03 / APZ-04: save, unchanged re-save, storage round trip
# ---------------------------------------------------------------------------


class TestSaveAndStorage:
    @pytest.mark.parametrize("submitted", [0, 0.0])
    def test_zero_is_saved_as_zero(self, submitted):
        saved = _gate_submit(_make_flow(_zone_data(55)), submitted)
        assert saved["zones"][0]["absence_position"] == 0
        assert isinstance(saved["zones"][0]["absence_position"], int)

    @pytest.mark.parametrize("position", _VALID_POSITIONS)
    def test_opening_and_saving_unchanged_keeps_the_stored_value(self, position):
        data = _zone_data(position)
        shown = _prefill(data)
        saved = _gate_submit(_make_flow(data), shown)
        assert saved["zones"][0]["absence_position"] == position

    @pytest.mark.parametrize("position", _VALID_POSITIONS)
    def test_storage_round_trip_preserves_the_position(self, position):
        saved = _gate_submit(_make_flow(_zone_data(55)), position)
        loaded = from_storage_dict(saved)
        assert loaded.zones[0].absence_position == position
        assert to_storage_dict(loaded)["zones"][0]["absence_position"] == position

    def test_unset_position_stays_unset_through_storage(self):
        loaded = from_storage_dict(_zone_data(None))
        assert loaded.zones[0].absence_position is None
        assert to_storage_dict(loaded)["zones"][0]["absence_position"] is None

    def test_render_does_not_mutate_the_entry(self):
        data = _zone_data(0)
        flow = _make_flow(data)
        asyncio.run(flow.async_step_presence(user_input=None))
        assert data["zones"][0]["absence_position"] == 0
        assert not flow.hass.config_entries.async_update_entry.called


# ---------------------------------------------------------------------------
# APZ-05: runtime target resolution
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)
_GLOBAL_ABSENCE_HA = 30


def _decision(*, zone_position: Any = None, window_position: Any = None):
    window = WindowConfig(
        id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0,
        cover_group_id="cg1", absence_position=window_position,
    )
    zone = ZoneConfig(id="z1", name="Zone", absence_position=zone_position)
    wdi = build_window_decision_input(
        window=window, zone=zone,
        global_defaults=GlobalDefaults(absence_shading_enabled=True, absence_position=_GLOBAL_ABSENCE_HA),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY, absence_active=True,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
        comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False,
                                     solar_gain_enabled=False),
    )
    return wdi, TierOrchestrator().evaluate_window(wdi)


class TestRuntimeTarget:
    def test_zone_zero_resolves_to_a_fully_closed_absence_target(self):
        wdi, result = _decision(zone_position=0)
        # internal convention: 100 = fully shaded / closed (HA 0)
        assert wdi.effective_behavior.absence_position == 100
        assert result.shading_state is ShadingState.ABSENCE_CLOSED
        assert result.target_position == 100

    def test_window_zero_overrides_a_zone_value(self):
        wdi, result = _decision(zone_position=40, window_position=0)
        assert wdi.effective_behavior.absence_position == 100
        assert result.target_position == 100

    def test_zone_zero_overrides_the_global_value(self):
        _, result = _decision(zone_position=0)
        assert result.target_position != 100 - _GLOBAL_ABSENCE_HA

    @pytest.mark.parametrize("position", [1, 10, 30, 70, 99, 100])
    def test_other_positions_resolve_unchanged(self, position):
        wdi, result = _decision(zone_position=position)
        assert wdi.effective_behavior.absence_position == 100 - position
        assert result.target_position == 100 - position

    def test_unset_zone_and_window_inherit_the_global_value(self):
        wdi, result = _decision(zone_position=None, window_position=None)
        assert wdi.effective_behavior.absence_position == 100 - _GLOBAL_ABSENCE_HA
        assert result.target_position == 100 - _GLOBAL_ABSENCE_HA
