"""Real Home Assistant: the zone-local Manual Override and Presence option routes.

A freshly created zone and a zone converted from an older version are offered their own
settings only, reach all five release choices behind the first Manual Override page, and
run what was chosen.  The form texts are covered by tests/test_zone_option_texts.py.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

from typing import Any

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE
from custom_components.smartshading.models.cover_group import CoverGroup
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig

# release choice -> (detail page behind the choice page, its input, stored and running strategy)
_CHOICES: dict[str, tuple[str | None, dict[str, Any], str]] = {
    "duration": ("override_duration",
                 {"override_duration_min": 33, "override_night_duration_min": 44}, "duration"),
    "fixed_time": ("override_fixed_time", {"override_fixed_until": "06:30"}, "fixed_time"),
    "lifecycle": (None, {}, "lifecycle"),
    "next_decision": ("override_decision",
                      {"override_decision_filter": "comfort"}, "first_comfort"),
    "manual": (None, {}, "manual"),
}


async def _start_zone(hass, minor: int) -> MockConfigEntry:
    """minor=2 is what the config flow creates today; minor=1 is converted at start."""
    zone = ZoneConfig(id="zone_a", name="Zone A")
    window = WindowConfig(id="window_a", name="South", zone_id=zone.id, azimuth=180.0,
                          floor_level=0, cover_group_id="cg_a")
    group = CoverGroup(id="cg_a", window_id=window.id, cover_ids=["cover.test_a"])
    data = {**to_storage_dict(SmartShadingConfigEntryData(
        name="Zone A", use_home_location=False, zones=[zone], windows=[window],
        cover_groups=[group])), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE}
    entry = MockConfigEntry(domain=DOMAIN, data=data, entry_id="a", minor_version=minor)
    hass.states.async_set("cover.test_a", "closed", {"current_position": 0})
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    return entry


async def _open(hass, *path: str) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init("a")
    for step in path:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step})
    return result


def _fields(result) -> set[str]:
    return {str(key) for key in result["data_schema"].schema}


async def _send(hass, result, user_input: dict[str, Any]) -> dict[str, Any]:
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


@pytest.mark.parametrize("minor", [2, 1], ids=["fresh", "converted"])
async def test_a_zone_is_offered_its_own_settings_only(hass, minor) -> None:
    await _start_zone(hass, minor)
    override = await _open(hass, "advanced", "manual_override")
    assert override["step_id"] == "manual_override"
    assert _fields(override) == {"override_detection_tolerance"}
    presence = await _open(hass, "presence")
    assert "absence_delay_use_system_default" not in _fields(presence)
    presence = await _send(hass, presence, {"presence_entity_ids": [], "presence_policy": "any_home"})
    assert presence["step_id"] == "presence_custom" and _fields(presence) == {"absence_delay_min"}


@pytest.mark.parametrize("minor", [2, 1], ids=["fresh", "converted"])
@pytest.mark.parametrize("choice", list(_CHOICES))
async def test_every_release_choice_is_reachable_persisted_and_run(hass, minor, choice) -> None:
    entry = await _start_zone(hass, minor)
    detail_step, detail_input, strategy = _CHOICES[choice]

    result = await _open(hass, "advanced", "manual_override")
    result = await _send(hass, result, {"override_detection_tolerance": 7})
    assert result["step_id"] == "manual_override_custom"
    result = await _send(hass, result, {
        "override_release_choice": choice, "override_allow_comfort_actions": False,
        "override_allow_protection_actions": False, "override_edit_timeouts": False})
    if detail_step is not None:
        assert result["step_id"] == detail_step
        result = await _send(hass, result, detail_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    policy = entry.data["override_policy"]
    assert policy["release_strategy"] == strategy
    assert policy["use_system_default"] is False and policy["detection_tolerance"] == 7
    coordinator = entry.runtime_data.coordinator
    assert coordinator._override_release_strategy.value == strategy
    assert coordinator._override_detection_tolerance == 7
