"""PA-3 (real Home Assistant): the legacy System UI and entities are gone, nothing else is.

Real config entries, the real options flow, real registries, the real ConfigEntries API.

Covers: fresh installs show no System UI/entities, a kept (blocked) legacy System entry creates
no entity or device and has no options menu, the documented recovery of a blocked legacy zone
through ITS OWN options (with the System entry's menu gone) ends in the automatic retirement,
nothing is recreated by a restart/reload, standard HA Diagnostics keep the Support/Research
content, and the two retirement guards that the PA-2c.2 audit found undetected by assertion
(wait for a setup in flight, verification that the entry is really gone).

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

import custom_components.smartshading as ss
from custom_components.smartshading import system_retirement_lifecycle as life
from custom_components.smartshading.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_SYSTEM,
    SYSTEM_DEVICE_IDENTIFIER,
)
from custom_components.smartshading.diagnostics import async_get_config_entry_diagnostics

from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.setup import async_setup_component

from custom_components.smartshading.config_entry_data import SmartShadingConfigEntryData, to_storage_dict
from custom_components.smartshading.const import ENTRY_TYPE_ZONE
from custom_components.smartshading.models.cover_group import CoverGroup, CoverHardwareType
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig

_SYSTEM_POLICY = {
    "release_strategy": "duration", "duration_min": 45, "night_duration_min": 300,
    "detection_tolerance": 99, "allow_comfort_actions": True,
    "allow_protection_actions": True, "safety_timeout_enabled": False,
}

_SYSTEM_DATA = {CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM, "system_override_policy": _SYSTEM_POLICY,
                "system_absence_delay_min": 90}

_OWN_POLICY = {"release_strategy": "fixed_time", "fixed_until": "06:30", "duration_min": 77,
               "night_duration_min": 400, "allow_comfort_actions": True,
               "allow_protection_actions": False, "detection_tolerance": 12,
               "safety_timeout_enabled": False, "use_system_default": False}

def _zone(entry_id: str, *, override_policy: Any = "keep", absence_delay: Any = "keep",
          absence_flag: Any = "keep", disabled: bool = False, minor: int = 1, version: int = 1,
          hardware: CoverHardwareType = CoverHardwareType.GENERIC, extra: dict | None = None,
          title: str | None = None) -> MockConfigEntry:
    zone = ZoneConfig(id=f"zone_{entry_id}", name=f"Zone {entry_id}",
                      execution=ZoneExecutionConfig(learning_enabled=True, active_control_enabled=True))
    window = WindowConfig(id=f"window_{entry_id}", name="South", zone_id=zone.id, azimuth=180.0,
                          floor_level=0, cover_group_id=f"cg_{entry_id}")
    group = CoverGroup(id=f"cg_{entry_id}", window_id=window.id, cover_ids=[f"cover.test_{entry_id}"],
                       hardware_type=hardware)
    data = {**to_storage_dict(SmartShadingConfigEntryData(
        name=f"Zone {entry_id}", use_home_location=False, zones=[zone], windows=[window],
        cover_groups=[group], rain_sensor_id="binary_sensor.rain")), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE}
    for key, value in (("override_policy", override_policy), ("absence_delay_min", absence_delay),
                       ("absence_delay_use_system_default", absence_flag)):
        if value == "keep":
            continue
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    data.update(extra or {})
    return MockConfigEntry(domain=DOMAIN, data=data, entry_id=entry_id, minor_version=minor,
                           version=version, title=title or f"Zone {entry_id}",
                           disabled_by=ConfigEntryDisabler.USER if disabled else None)

def _independent(entry_id: str, **kw) -> MockConfigEntry:
    return _zone(entry_id, override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20,
                 absence_flag=False, minor=2, **kw)

def _system(data: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data=copy.deepcopy(_SYSTEM_DATA if data is None else data),
                           source="system", entry_id="system", title="SmartShading System")

def _add(hass, *entries: MockConfigEntry) -> None:
    hass.states.async_set("binary_sensor.rain", "off")
    for e in entries:
        if e.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SYSTEM:
            hass.states.async_set(f"cover.test_{e.entry_id}", "closed", {"current_position": 0})
        e.add_to_hass(hass)

async def _start(hass) -> bool:
    ok = await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return ok

def _systems(hass):
    return [e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM]

def _effective(entry) -> dict[str, Any]:
    c = entry.runtime_data.coordinator
    return {"strategy": c._override_release_strategy.value, "duration": c._override_duration_min,
            "night": c._override_night_duration_min, "tolerance": c._override_detection_tolerance,
            "safety_timeout": c._override_safety_timeout_enabled, "absence": c._absence_delay_min}

def _issue_ids(hass) -> list[str]:
    return sorted(i for (d, i) in ir.async_get(hass).issues
                  if d == DOMAIN and i.startswith(life.ISSUE_ZONE_PREFIX) or i == life.ISSUE_REMOVAL_FAILED)


def _registry_rows(hass, entry_id: str) -> list[str]:
    reg = er.async_get(hass)
    return sorted(e.entity_id for e in reg.entities.values() if e.config_entry_id == entry_id)


async def _open(hass, entry_id: str, *path: str):
    result = await hass.config_entries.options.async_init(entry_id)
    for step in path:
        assert result["type"] is FlowResultType.MENU, result
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})
    return result


def _fields(result) -> set[str]:
    return {str(k) for k in result["data_schema"].schema}


async def _own_presence(hass, entry_id: str, delay: int):
    """The zone's Presence options: stop following the System default, set an own delay."""
    result = await _open(hass, entry_id, "presence")
    assert "absence_delay_use_system_default" in _fields(result), "the zone still offers its own settings"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "presence_entity_ids": [], "presence_policy": "any_home",
        "absence_delay_use_system_default": False})
    assert result["step_id"] == "presence_custom"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"absence_delay_min": delay})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def _own_override(hass, entry_id: str):
    """The zone's Manual Override options: stop following the System default, choose own settings."""
    result = await _open(hass, entry_id, "advanced", "manual_override")
    assert "override_use_system_default" in _fields(result), "the zone still offers its own settings"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "override_use_system_default": False, "override_detection_tolerance": 15})
    assert result["step_id"] == "manual_override_custom"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "override_release_choice": "lifecycle", "override_allow_comfort_actions": False,
        "override_allow_protection_actions": False, "override_edit_timeouts": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Fresh installs: no System UI, no System entities
# ---------------------------------------------------------------------------

class TestFreshInstallShowsNoSystemUi:
    async def test_a_zone_only_install_has_no_system_entry_buttons_or_debug_switch(self, hass) -> None:
        zone = _independent("a")
        _add(hass, zone)
        assert await _start(hass)
        assert _systems(hass) == []
        reg = er.async_get(hass)
        rows = [e for e in reg.entities.values() if e.platform == DOMAIN]
        assert rows, "the zone's own entities exist"
        assert not [e for e in rows if e.domain == "button"]
        assert not [e for e in rows if "debug_logging" in (e.unique_id or "") or "export" in (e.unique_id or "")]
        assert sorted(e.translation_key for e in rows if e.domain == "switch") == ["active_control", "learning_mode"]
        assert not dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)})
        assert "debug_logging" not in hass.data[DOMAIN]

    async def test_diagnostics_still_carry_support_and_research_without_any_system_entry(self, hass) -> None:
        zone = _independent("a")
        _add(hass, zone)
        assert await _start(hass)
        assert _systems(hass) == []
        diag = await async_get_config_entry_diagnostics(hass, zone)
        assert diag["support_export"] and diag["research_export"] and "forecast_learning" in diag


class TestLegacyExportRetentionWithoutSystemEntry:
    async def test_expired_public_export_files_are_deleted_at_start_whether_or_not_a_system_entry_exists(
            self, hass, tmp_path) -> None:
        """The cleanup of old files an earlier version left in /config/www used to run from the
        System entry's setup; it now runs once per start from the component setup."""
        hass.config.config_dir = str(tmp_path)
        www = tmp_path / "www"
        www.mkdir()
        old = www / "smartshading_support_export_20200101T000000Z.json"
        recent = www / "smartshading_support_export_29990101T000000Z.json"
        unrelated = www / "my_own_file.json"
        for f in (old, recent, unrelated):
            f.write_text("{}", encoding="utf-8")
        _add(hass, _independent("a"))
        assert await _start(hass) and _systems(hass) == []
        assert not old.exists() and recent.exists() and unrelated.exists()


# ---------------------------------------------------------------------------
# A kept legacy System entry: loadable, empty, no menu, nothing recreated
# ---------------------------------------------------------------------------

def _blocked_install(hass, *, bad=None):
    """One zone that cannot be verified (keeps the System entry) next to an independent one."""
    bad = bad or _zone("bad", absence_flag="false")
    good = _independent("good")
    system = _system()
    _add(hass, system, bad, good)
    return system, bad, good


class TestKeptLegacySystemEntry:
    async def test_it_stays_loaded_but_owns_no_entity_device_or_platform(self, hass) -> None:
        system, bad, good = _blocked_install(hass)
        assert await _start(hass)
        assert system.state is ConfigEntryState.LOADED and _systems(hass) == [system]
        assert _registry_rows(hass, "system") == []
        assert not dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)})
        assert hass.states.async_entity_ids("button") == []
        assert dict(system.data) == _SYSTEM_DATA and dict(system.options) == {}

    async def test_restart_and_reload_recreate_nothing(self, hass) -> None:
        system, bad, good = _blocked_install(hass)
        assert await _start(hass)
        before = (_registry_rows(hass, "system"), _registry_rows(hass, "good"),
                  sorted(e.entity_id for e in er.async_get(hass).entities.values()))
        for _ in range(2):
            assert await hass.config_entries.async_reload("system")
            await hass.async_block_till_done()
        assert await hass.config_entries.async_reload("bad")
        await hass.async_block_till_done()
        assert system.state is ConfigEntryState.LOADED and _systems(hass) == [system]
        after = (_registry_rows(hass, "system"), _registry_rows(hass, "good"),
                 sorted(e.entity_id for e in er.async_get(hass).entities.values()))
        assert after == before

    async def test_its_options_flow_aborts_with_guidance_and_changes_nothing(self, hass) -> None:
        system, bad, good = _blocked_install(hass)
        assert await _start(hass)
        data_before, options_before = copy.deepcopy(dict(system.data)), copy.deepcopy(dict(system.options))
        result = await hass.config_entries.options.async_init("system")
        assert result["type"] is FlowResultType.ABORT and result["reason"] == "legacy_system_entry"
        assert dict(system.data) == data_before and dict(system.options) == options_before
        assert system.state is ConfigEntryState.LOADED

    async def test_the_zone_options_flow_is_unchanged_next_to_it(self, hass) -> None:
        system, bad, good = _blocked_install(hass)
        assert await _start(hass)
        result = await hass.config_entries.options.async_init("bad")
        assert result["type"] is FlowResultType.MENU
        assert set(result["menu_options"]) == {
            "weather", "lifecycle", "presence", "comfort", "behavior", "windows", "advanced"}


# ---------------------------------------------------------------------------
# Recovery of a blocked legacy installation without the System menu
# ---------------------------------------------------------------------------

class TestBlockedLegacyZoneRecoversThroughItsOwnOptions:
    async def test_an_unverifiable_presence_flag_is_cured_in_the_zone_presence_options(self, hass) -> None:
        system, bad, good = _blocked_install(hass)               # absence flag stored as the string "false"
        assert await _start(hass)
        assert _systems(hass) == [system] and "system_retirement_zone_bad" in _issue_ids(hass)
        await _own_presence(hass, "bad", delay=40)
        # the zone now carries its own value, was converted and the System entry retired itself
        assert bad.data["absence_delay_use_system_default"] is False and bad.data["absence_delay_min"] == 40
        assert bad.minor_version == 2 and _systems(hass) == []
        assert _issue_ids(hass) == []
        assert bad.state is ConfigEntryState.LOADED and _effective(bad)["absence"] == 40
        assert good.state is ConfigEntryState.LOADED

    async def test_a_zone_blocked_by_both_settings_is_cured_with_the_two_option_pages_and_a_restart(self, hass) -> None:
        bad = _zone("bad", override_policy={"use_system_default": True}, absence_flag="false")
        system, bad, good = _blocked_install(hass, bad=bad)
        assert await _start(hass)
        assert _systems(hass) == [system] and bad.minor_version == 1
        await _own_override(hass, "bad")
        assert _systems(hass) == [system], "one setting alone does not retire the entry"
        await _own_presence(hass, "bad", delay=25)
        assert _systems(hass) == [] and bad.minor_version == 2 and _issue_ids(hass) == []
        eff = _effective(bad)
        assert eff["strategy"] == "lifecycle" and eff["tolerance"] == 15 and eff["absence"] == 25
        # a restart (component setup over the stored data) leaves it as it is
        assert await hass.config_entries.async_reload("bad")
        await hass.async_block_till_done()
        assert _systems(hass) == [] and _effective(bad) == eff

    async def test_the_cure_never_deletes_zone_data_or_the_other_zones(self, hass) -> None:
        system, bad, good = _blocked_install(hass)
        assert await _start(hass)
        windows = copy.deepcopy(bad.data["windows"]), copy.deepcopy(bad.data["cover_groups"])
        good_before = copy.deepcopy(dict(good.data))
        await _own_presence(hass, "bad", delay=40)
        assert (bad.data["windows"], bad.data["cover_groups"]) == windows
        assert dict(good.data) == good_before
        assert bad.title == "Zone bad" and bad.entry_id == "bad"


# ---------------------------------------------------------------------------
# The two PA-2c.2 guards that the audit found undetected by assertion (follow-up D)
# ---------------------------------------------------------------------------

class TestRetirementGuards:
    async def _ready(self, hass):
        bad = _zone("bad", absence_flag="false")
        system = _system()
        _add(hass, system, bad, _independent("good"))
        assert await _start(hass)
        assert system.state is ConfigEntryState.LOADED
        hass.config_entries.async_update_entry(
            bad, data={**bad.data, "absence_delay_use_system_default": False, "absence_delay_min": 20,
                       "override_policy": copy.deepcopy(_OWN_POLICY)}, minor_version=2)
        return system

    async def test_the_removal_waits_for_a_setup_in_flight_before_it_unloads(self, hass, monkeypatch) -> None:
        """async_unload rejects a SETUP_IN_PROGRESS entry before it takes the lock; the retirement
        must therefore let the setup finish first and unload an entry that is really loaded."""
        import asyncio

        system = await self._ready(hass)
        assert await hass.config_entries.async_unload("system")
        gate = asyncio.Event()
        real_setup = ss._async_setup_system_entry

        async def slow_setup(h, entry):
            await gate.wait()
            return await real_setup(h, entry)

        monkeypatch.setattr(ss, "_async_setup_system_entry", slow_setup)
        states_at_unload: list[ConfigEntryState] = []
        real_unload = hass.config_entries.async_unload

        async def spy(entry_id, *args, **kwargs):
            if entry_id == "system":
                states_at_unload.append(system.state)
            return await real_unload(entry_id, *args, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_unload", spy)
        setup_task = hass.async_create_task(hass.config_entries.async_setup("system"))
        await asyncio.sleep(0)
        while system.state is not ConfigEntryState.SETUP_IN_PROGRESS:
            await asyncio.sleep(0)
        retire = hass.async_create_task(ss._async_retire_system_entry(hass))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not retire.done() and states_at_unload == [], "retirement waits while the setup is in flight"
        gate.set()
        assert await retire == life.RETIRED
        await setup_task
        # the retirement's own unload saw a loaded entry (HA's removal unloads once more: no-op)
        assert states_at_unload[0] is ConfigEntryState.LOADED
        assert ConfigEntryState.SETUP_IN_PROGRESS not in states_at_unload
        assert _systems(hass) == []

    async def test_a_removal_that_leaves_the_entry_registered_is_a_failure_with_a_repairs_issue(
            self, hass, monkeypatch, caplog) -> None:
        system = await self._ready(hass)

        async def removes_nothing(entry_id):
            return {"require_restart": False}

        monkeypatch.setattr(hass.config_entries, "async_remove", removes_nothing)
        assert await ss._async_retire_system_entry(hass) == life.FAILED
        assert hass.config_entries.async_get_entry("system") is system
        assert system.state is ConfigEntryState.LOADED, "the kept entry is set up again"
        assert life.ISSUE_REMOVAL_FAILED in _issue_ids(hass)
        assert ir.async_get(hass).async_get_issue(DOMAIN, life.ISSUE_REMOVAL_FAILED).translation_key \
            == life.KEY_REMOVAL_FAILED
        assert "still registered" in caplog.text
