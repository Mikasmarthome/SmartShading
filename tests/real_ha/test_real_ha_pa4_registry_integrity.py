"""PA-4 (real Home Assistant): registry integrity, retirement recovery and diagnostics.

Real config entries, the real ConfigEntries API, the real entity/device/issue registries and
the real diagnostics hook.  Covers: the rows an older version registered for the legacy System
entry are removed (also while the entry must stay, and also when an interrupted removal left
them without their config entry) while no zone row, zone setting or storage is touched; a
restored backup of an older version upgrades with its zones, disabled zones, cover and window
configuration and customized entities intact; Diagnostics stay available, serializable, private
and stable for active, disabled, unloaded and blocked zones and for the legacy entry itself.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
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


def _snapshot(hass, *, skip_entry: str | None = None) -> dict[str, Any]:
    """Every entity row and device row that does not belong to ``skip_entry``, as plain data."""
    reg = er.async_get(hass)
    rows = {
        e.entity_id: (e.unique_id, e.platform, e.config_entry_id, e.device_id, e.name,
                      e.area_id, e.disabled_by, e.original_name, e.translation_key)
        for e in reg.entities.values() if e.config_entry_id != skip_entry
    }
    devices = {d.id: (tuple(sorted(d.identifiers)), d.name, d.area_id, d.disabled_by)
               for d in dr.async_get(hass).devices.values()
               if tuple(sorted(d.identifiers)) != ((DOMAIN, SYSTEM_DEVICE_IDENTIFIER),)}
    return {"entities": rows, "devices": devices}


def _seed_legacy_rows(hass, system_entry) -> list[str]:
    """The registry rows an older version created for its System entry."""
    reg = er.async_get(hass)
    sid = system_entry.entry_id
    rows = [
        reg.async_get_or_create("button", DOMAIN, f"{sid}_system_export_support_data",
                                config_entry=system_entry).entity_id,
        reg.async_get_or_create("button", DOMAIN, f"{sid}_system_export_research_data",
                                config_entry=system_entry).entity_id,
        reg.async_get_or_create("switch", DOMAIN, f"{sid}_debug_logging",
                                config_entry=system_entry).entity_id,
    ]
    dr.async_get(hass).async_get_or_create(
        config_entry_id=sid, identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)}, name="SmartShading System")
    return rows


def _legacy_device(hass):
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)})


def _removed_events(hass) -> list[str]:
    events: list[str] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda e: events.append(e.data["entity_id"]) if e.data["action"] == "remove" else None)
    return events


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

class TestLegacySystemRowsOfAKeptEntry:
    async def test_rows_of_a_blocked_system_entry_are_removed_and_nothing_else(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)        # unverifiable
        good = _independent("good")
        system_entry = _system()
        _add(hass, system_entry, bad, good)
        legacy = _seed_legacy_rows(hass, system_entry)
        # something the user did to a zone entity must survive exactly as it is
        reg = er.async_get(hass)
        assert await _start(hass)
        zone_row = next(e for e in reg.entities.values() if e.config_entry_id == "good")
        reg.async_update_entity(zone_row.entity_id, name="My own name")
        await hass.async_block_till_done()
        before = _snapshot(hass, skip_entry="system")
        assert not [e for e in legacy if reg.async_get(e)], "legacy rows are gone"
        assert _legacy_device(hass) is None
        # a restart pass over the same registry removes nothing more and changes nothing
        removed = _removed_events(hass)
        await ss.async_setup(hass, {})
        await hass.async_block_till_done()
        assert removed == []
        assert _snapshot(hass, skip_entry="system") == before
        # the System entry itself is untouched and kept (a zone is blocked)
        assert [e.entry_id for e in _systems(hass)] == ["system"]
        assert hass.config_entries.async_get_entry("system").state is ConfigEntryState.LOADED
        assert dict(hass.config_entries.async_get_entry("system").data) == _SYSTEM_DATA
        assert _issue_ids(hass) == ["system_retirement_zone_bad"]

    async def test_only_exact_legacy_rows_are_matched(self, hass) -> None:
        """Rows that merely look similar - another platform, a zone entry's row, a row without
        config entry, a different unique id, a different entity domain - are never removed."""
        zone = _independent("zone")
        system_entry = _system()
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        _add(hass, system_entry, zone, bad)
        reg = er.async_get(hass)
        keep = [
            reg.async_get_or_create("switch", "other_integration", "system_debug_logging",
                                    config_entry=system_entry).entity_id,        # foreign platform
            reg.async_get_or_create("switch", DOMAIN, "zone_debug_logging",
                                    config_entry=zone).entity_id,                 # a zone entry's row
            reg.async_get_or_create("switch", DOMAIN, "system_debug_logging_other",
                                    config_entry=system_entry).entity_id,        # other unique id
            reg.async_get_or_create("sensor", DOMAIN, "system_debug_logging",
                                    config_entry=system_entry).entity_id,        # other entity domain
            reg.async_get_or_create("button", DOMAIN, "system_debug_logging",
                                    config_entry=system_entry).entity_id,        # domain/suffix mismatch
            reg.async_get_or_create("switch", DOMAIN, "unrelated_debug_logging").entity_id,   # no entry
            reg.async_get_or_create("switch", DOMAIN, "someone_debug_logging",
                                    config_entry=system_entry).entity_id,        # not this entry's id
        ]
        assert await _start(hass)
        assert all(reg.async_get(e) for e in keep), [e for e in keep if not reg.async_get(e)]

    @pytest.mark.parametrize("disabled_by", [None, er.RegistryEntryDisabler.USER])
    async def test_a_legacy_device_that_still_has_an_entity_is_kept(self, hass, disabled_by) -> None:
        """The device goes only when nothing references it any more (a disabled entity counts)."""
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        system_entry = _system()
        _add(hass, system_entry, bad)
        reg = er.async_get(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id="system", identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)})
        stranger = reg.async_get_or_create("sensor", "other_integration", "x",
                                           config_entry=system_entry, device_id=device.id,
                                           disabled_by=disabled_by)
        assert await _start(hass)
        assert reg.async_get(stranger.entity_id) and _legacy_device(hass) is not None

    async def test_a_disabled_legacy_entity_is_removed_too(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        system_entry = _system()
        _add(hass, system_entry, bad)
        reg = er.async_get(hass)
        row = reg.async_get_or_create("switch", DOMAIN, "system_debug_logging", config_entry=system_entry,
                                      disabled_by=er.RegistryEntryDisabler.USER)
        assert await _start(hass)
        assert reg.async_get(row.entity_id) is None


class TestTheCleanUpNeverBreaksTheSetup:
    async def test_a_failing_clean_up_is_logged_and_the_setup_and_the_retirement_go_on(
            self, hass, monkeypatch, caplog) -> None:
        a = _independent("a")
        system_entry = _system()
        _add(hass, system_entry, a)

        def broken(_hass):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(ss, "async_remove_legacy_system_registry_rows", broken)
        assert await _start(hass)
        assert a.state is ConfigEntryState.LOADED
        assert _systems(hass) == []                                    # the retirement still ran
        assert "legacy System registry clean-up failed" in caplog.text


class TestRowsLeftByAnInterruptedRemoval:
    async def test_rows_without_their_config_entry_are_removed_at_the_next_start(self, hass, monkeypatch) -> None:
        """The config entry removal was stored, the (separately, delayed saved) entity registry
        was not: the rows reference an entry that no longer exists."""
        good = _independent("good")
        system_entry = _system()
        _add(hass, system_entry, good)
        legacy = _seed_legacy_rows(hass, system_entry)
        reg = er.async_get(hass)
        # the interruption: the registries are not told about the removal
        monkeypatch.setattr(er.EntityRegistry, "async_clear_config_entry", lambda self, entry_id: None)
        monkeypatch.setattr(dr.DeviceRegistry, "async_clear_config_entry", lambda self, entry_id, *a: None)
        await hass.config_entries.async_remove("system")
        monkeypatch.undo()
        assert hass.config_entries.async_get_entry("system") is None
        assert all(reg.async_get(e) and reg.async_get(e).config_entry_id == "system" for e in legacy)
        zone_before = _snapshot(hass, skip_entry="system")
        assert await _start(hass)
        assert not [e for e in legacy if reg.async_get(e)]
        assert _legacy_device(hass) is None
        assert _systems(hass) == []
        assert good.state is ConfigEntryState.LOADED
        zone_rows_after = {k: v for k, v in _snapshot(hass, skip_entry="system")["entities"].items()
                           if k in zone_before["entities"]}
        assert zone_rows_after == zone_before["entities"]

    async def test_an_interrupted_retirement_with_rows_still_present_completes_at_the_next_start(self, hass) -> None:
        """The previous run unloaded the entry but stopped before it was removed."""
        a, b = _independent("a"), _independent("b")
        system_entry = _system()
        _add(hass, system_entry, a, b)
        legacy = _seed_legacy_rows(hass, system_entry)
        assert _systems(hass)[0].state is ConfigEntryState.NOT_LOADED     # stored, not set up yet
        assert await _start(hass)
        assert _systems(hass) == []
        reg = er.async_get(hass)
        assert not [e for e in legacy if reg.async_get(e)] and _legacy_device(hass) is None
        assert a.state is ConfigEntryState.LOADED and b.state is ConfigEntryState.LOADED
        assert _issue_ids(hass) == []


class TestFreshInstallAndRetirementTouchNoRegistryRow:
    async def test_a_fresh_zone_only_install_removes_no_registry_row(self, hass) -> None:
        zone = _independent("a")
        _add(hass, zone)
        removed = _removed_events(hass)
        assert await _start(hass)
        assert removed == []
        assert er.async_get(hass).entities and _legacy_device(hass) is None

    async def test_a_removal_failure_at_start_neither_hangs_the_setup_nor_touches_a_zone_and_a_retry_succeeds(
            self, hass, monkeypatch) -> None:
        """The start-up retirement runs inside the component setup.  If the removal fails there,
        restoring the kept System entry must not wait for that very setup (it used to, until Home
        Assistant's 300 s setup timeout failed the whole integration)."""
        import asyncio
        a = _independent("a")
        system_entry = _system()
        _add(hass, system_entry, a)
        legacy = _seed_legacy_rows(hass, system_entry)
        original = hass.config_entries.async_remove

        async def failing(entry_id):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(hass.config_entries, "async_remove", failing)
        assert await asyncio.wait_for(_start(hass), timeout=60)
        reg = er.async_get(hass)
        # the clean-up does not depend on the removal: the dead rows are gone, the entry is kept
        assert not [e for e in legacy if reg.async_get(e)]
        assert [e.entry_id for e in _systems(hass)] == ["system"]
        assert system_entry.state is ConfigEntryState.LOADED        # set up by Home Assistant itself
        assert a.state is ConfigEntryState.LOADED
        assert _issue_ids(hass) == [life.ISSUE_REMOVAL_FAILED]
        zone_rows = _snapshot(hass, skip_entry="system")
        monkeypatch.setattr(hass.config_entries, "async_remove", original)
        await ss.async_setup(hass, {})                         # the next start retries
        await hass.async_block_till_done()
        assert _systems(hass) == [] and _issue_ids(hass) == []
        assert _snapshot(hass, skip_entry="system") == zone_rows
        assert a.state is ConfigEntryState.LOADED


class TestRestoredBackupWithLegacyRecords:
    async def test_legacy_zones_rows_and_state_survive_the_upgrade_of_a_restored_backup(
            self, hass, hass_storage) -> None:
        """A backup of an older version: a System entry with its registry rows, zones that still
        defer to it (one of them disabled), a customized zone entity.  After the start the zones
        are migrated and keep everything, the System entry and its rows are gone."""
        legacy_a = _zone("a", override_policy={"use_system_default": True}, absence_flag=True)
        legacy_off = _zone("off", override_policy={"use_system_default": True}, absence_flag=True, disabled=True)
        system_entry = _system()
        _add(hass, system_entry, legacy_a, legacy_off)
        legacy_rows = _seed_legacy_rows(hass, system_entry)
        reg = er.async_get(hass)
        # the disabled zone's entities exist in the backup's registry (disabled by their config entry)
        off_row = reg.async_get_or_create("sensor", DOMAIN, "off_zone_off_zone_summary",
                                          config_entry=legacy_off,
                                          disabled_by=er.RegistryEntryDisabler.CONFIG_ENTRY)
        reg.async_update_entity(off_row.entity_id, name="Kept name")
        cover_ids_before = {e.entry_id: [g["cover_ids"] for g in e.data["cover_groups"]]
                            for e in (legacy_a, legacy_off)}
        windows_before = {e.entry_id: copy.deepcopy(e.data["windows"]) for e in (legacy_a, legacy_off)}
        # persistent learning / forecast / safety-hold state of the disabled zone (never set up here)
        from custom_components.smartshading.engines.forecast_persistence import STORAGE_KEY as FORECAST_KEY
        from custom_components.smartshading.engines.learning_persistence import LEARNING_STORAGE_KEY
        from custom_components.smartshading.engines.safety_hold_persistence import STORAGE_KEY as HOLD_KEY
        marker = {"version": 1, "minor_version": 1, "data": {"marker": "kept"}}
        keys = [f"{LEARNING_STORAGE_KEY}_off", f"{FORECAST_KEY}_off", f"{HOLD_KEY}_off"]
        for key in keys:
            hass_storage[key] = {**marker, "key": key}
        stored_before = copy.deepcopy({k: hass_storage[k] for k in keys})

        assert await _start(hass)

        assert _systems(hass) == [] and _issue_ids(hass) == []
        assert not [e for e in legacy_rows if reg.async_get(e)] and _legacy_device(hass) is None
        assert legacy_a.minor_version == 2 and legacy_off.minor_version == 2
        assert legacy_off.disabled_by is ConfigEntryDisabler.USER
        assert legacy_a.state is ConfigEntryState.LOADED and legacy_off.state is ConfigEntryState.NOT_LOADED
        kept = reg.async_get(off_row.entity_id)
        assert kept and kept.name == "Kept name" and kept.disabled_by is er.RegistryEntryDisabler.CONFIG_ENTRY
        assert {e.entry_id: [g["cover_ids"] for g in e.data["cover_groups"]]
                for e in (legacy_a, legacy_off)} == cover_ids_before
        assert {e.entry_id: e.data["windows"] for e in (legacy_a, legacy_off)} == windows_before
        # effective control settings are the System defaults the zones used to inherit
        assert _effective(legacy_a) == {"strategy": "duration", "duration": 45, "night": 300,
                                        "tolerance": 10, "safety_timeout": False, "absence": 90}
        # nothing removed any persistent learning/forecast/safety-hold storage
        assert {k: hass_storage.get(k) for k in keys} == stored_before
        # the disabled zone can be enabled afterwards and runs with the migrated values
        await hass.config_entries.async_set_disabled_by("off", None)
        await hass.async_block_till_done()
        assert legacy_off.state is ConfigEntryState.LOADED and _systems(hass) == []
        assert reg.async_get(off_row.entity_id).name == "Kept name"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def _json(self, payload) -> str:
        import json
        return json.dumps(payload, default=str)

    async def _setup_mixed(self, hass):
        active_a, active_b = _independent("a"), _independent("b")
        disabled = _independent("off", disabled=True)
        unloaded = _independent("idle")
        _add(hass, active_a, active_b, disabled, unloaded)
        assert await _start(hass)
        await hass.config_entries.async_unload("idle")
        await hass.async_block_till_done()
        return active_a, active_b, disabled, unloaded

    async def test_every_zone_state_returns_a_serializable_payload_with_the_intended_sections(self, hass) -> None:
        a, b, disabled, unloaded = await self._setup_mixed(hass)
        assert a.state is ConfigEntryState.LOADED and disabled.state is ConfigEntryState.NOT_LOADED
        assert unloaded.state is ConfigEntryState.NOT_LOADED
        for entry in (a, b, disabled, unloaded):
            diag = await async_get_config_entry_diagnostics(hass, entry)
            self._json(diag)                                       # serializable for the HA download
            assert diag["support_export"] and diag["research_export"] and "forecast_learning" in diag, entry.entry_id
            assert not diag["support_export"].get("section_errors")
            assert not diag["research_export"].get("section_errors")
        loaded = await async_get_config_entry_diagnostics(hass, a)
        idle = await async_get_config_entry_diagnostics(hass, disabled)
        assert loaded.get("section_errors", {}).get("no_coordinator") is None
        # a zone without a live coordinator reports it instead of failing
        assert idle["section_errors"] == {"no_coordinator": 1}
        assert idle["forecast_learning"]["store_available"] is False

    async def test_the_aggregate_covers_the_active_zones_only(self, hass) -> None:
        a, b, disabled, unloaded = await self._setup_mixed(hass)
        from custom_components.smartshading import diagnostics as diag_module
        coords = diag_module._active_zone_coordinators(hass)
        assert sorted(c.config_entry.entry_id for c in coords) == ["a", "b"]

    async def test_the_payload_after_the_retirement_matches_the_payload_with_the_system_entry(self, hass) -> None:
        a, b = _independent("a"), _independent("b")
        blocker = _zone("bad", override_policy="not a mapping", absence_flag=False)
        system_entry = _system()
        _add(hass, system_entry, a, b, blocker)
        assert await _start(hass)
        before = await async_get_config_entry_diagnostics(hass, a)
        assert [e.entry_id for e in _systems(hass)] == ["system"]
        # the blocker is fixed -> the System entry is retired on the next zone setup
        hass.config_entries.async_update_entry(
            blocker, data={**blocker.data, "override_policy": copy.deepcopy(_OWN_POLICY)}, minor_version=2)
        await hass.config_entries.async_reload("bad")
        await hass.async_block_till_done()
        assert _systems(hass) == []
        after = await async_get_config_entry_diagnostics(hass, a)
        assert set(before) == set(after)
        assert set(before["support_export"]) == set(after["support_export"])
        assert set(before["research_export"]) == set(after["research_export"])

    async def test_the_legacy_system_entry_still_answers_and_a_blocked_zone_is_reported_not_hidden(self, hass) -> None:
        a = _independent("a")
        blocker = _zone("bad", override_policy="not a mapping", absence_flag=False)
        system_entry = _system()
        _add(hass, system_entry, a, blocker)
        assert await _start(hass)
        diag = await async_get_config_entry_diagnostics(hass, system_entry)
        self._json(diag)
        assert diag["support_export"] and diag["research_export"]
        assert diag["section_errors"] == {"no_coordinator": 1}
        for entry in (a, blocker):
            self._json(await async_get_config_entry_diagnostics(hass, entry))

    async def test_the_payload_contains_no_entity_ids_names_or_configuration_values(self, hass) -> None:
        a = _independent("a", title="Private living room")
        _add(hass, a)
        assert await _start(hass)
        text = self._json(await async_get_config_entry_diagnostics(hass, a))
        for private in ("cover.test_a", "binary_sensor.rain", "Private living room", "Zone a", "South",
                        "window_a", "zone_a", "cg_a"):
            assert private not in text, private

    async def test_a_failing_section_degrades_to_a_section_error_and_the_rest_survives(
            self, hass, monkeypatch) -> None:
        a = _independent("a")
        _add(hass, a)
        assert await _start(hass)
        import custom_components.smartshading.engines.diagnostics_builder as builder
        import custom_components.smartshading.engines.support_export as support

        def boom(*args, **kwargs):
            raise RuntimeError("broken section")

        monkeypatch.setattr(builder, "build_consolidated_diagnostics", boom)
        diag = await async_get_config_entry_diagnostics(hass, a)
        assert diag["section_errors"] == {"builder": 1}
        assert diag["support_export"] and diag["research_export"] and "forecast_learning" in diag
        monkeypatch.undo()
        monkeypatch.setattr(support, "iter_support_export_all_zones", boom)
        diag = await async_get_config_entry_diagnostics(hass, a)
        assert diag["section_errors"] == {"support_research_export": 1}
        assert "forecast_learning" in diag and diag["system"] is not None

    async def test_two_requests_give_the_same_structure(self, hass) -> None:
        a, b = _independent("a"), _independent("b")
        _add(hass, a, b)
        assert await _start(hass)

        def shape(value):
            if isinstance(value, dict):
                return {k: shape(v) for k, v in value.items()}
            if isinstance(value, list):
                return [shape(v) for v in value][:1]
            return type(value).__name__

        first = await async_get_config_entry_diagnostics(hass, a)
        second = await async_get_config_entry_diagnostics(hass, a)
        assert set(first) == set(second)
        assert shape(first["system"]) == shape(second["system"])
        assert shape(first["forecast_learning"]) == shape(second["forecast_learning"])
