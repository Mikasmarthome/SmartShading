"""PA-5 (real Home Assistant): the end-to-end migration matrix of the zone-only architecture.

Real config entries, the real ConfigEntries API, real registries, the real options flow.  Two long
chains and a few focused cases:

* upgrade chain - the published beta.3.2 model (a System entry holding defaults, zones that defer
  to it, zones with their own values stored exactly as beta.3.2 stored them, a disabled zone), then
  the start, three restarts, enabling the disabled zone, removing zones, removing the final zone,
  adding and configuring a new zone.  At every stage: no System entry, every zone's effective
  settings equal what the PUBLISHED resolvers said before the upgrade, entry ids/titles, cover and
  window assignments unchanged, nothing written after the migration.
* fault chain - a damaged System entry, a zone that needs it, a zone with unreadable data, a failed
  removal, restarts during the transition, recovery through the zones' own options, retry.
* interrupted migration, restored backup, and the entity-before-device order of the registry clean-up.

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


_OWN_EFFECTIVE = {"strategy": "fixed_time", "duration": 77, "night": 400, "tolerance": 12,
                  "safety_timeout": False, "absence": 20}
_NOON_UNUSED = None


def _legacy(entry_id: str, **kw) -> MockConfigEntry:
    """A v1 zone that defers to the System entry for both inherited settings."""
    return _zone(entry_id, override_policy={"use_system_default": True}, absence_flag=True, **kw)


def _seed_legacy_rows(hass, system_entry) -> list[str]:
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


# ---------------------------------------------------------------------------
# Helpers of this module
# ---------------------------------------------------------------------------

PERSON_ENTITY = "person.pa5"
_MIGRATED_KEYS = ("override_policy", "absence_delay_min", "absence_delay_use_system_default")


def _core(data) -> dict:
    """Everything of a zone's stored data except the settings the migration materializes."""
    return {k: copy.deepcopy(v) for k, v in dict(data).items() if k not in _MIGRATED_KEYS}


def _before(entry, system_data) -> dict[str, Any]:
    """What the PUBLISHED resolvers (unchanged since beta.3.2) say the zone runs with, judged from
    the stored data BEFORE anything happened: the independent oracle for the effective settings."""
    from custom_components.smartshading.config_entry_data import (
        resolve_zone_absence_delay_min,
        resolve_zone_override_policy,
    )
    raw = copy.deepcopy(dict(entry.data))
    policy = resolve_zone_override_policy(raw, system_data)
    return {"strategy": policy.release_strategy.value, "duration": policy.duration_min,
            "night": policy.night_duration_min, "tolerance": policy.detection_tolerance,
            "safety_timeout": policy.safety_timeout_enabled,
            "absence": resolve_zone_absence_delay_min(raw, system_data)}


class _Writes:
    """Counts the writes to config entries."""

    def __init__(self, hass, monkeypatch) -> None:
        self.calls: list[str] = []
        original = hass.config_entries.async_update_entry

        def spy(entry, **kwargs):
            self.calls.append(entry.entry_id)
            return original(entry, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_update_entry", spy)

    def clear(self) -> None:
        self.calls.clear()


async def _restart(hass) -> None:
    """A restart of the whole domain: every entry unloaded, the component setup run again (as
    Home Assistant does on start), every enabled entry set up."""
    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await ss.async_setup(hass, {})
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.disabled_by is None and entry.state is ConfigEntryState.NOT_LOADED:
            await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _beta32_own_zone(entry_id: str, **kw) -> MockConfigEntry:
    """A zone exactly as the published beta.3.2 stored it: its own policy WITHOUT the explicit
    use_system_default marker, its own absence delay WITHOUT the explicit flag."""
    policy = {k: v for k, v in _OWN_POLICY.items() if k != "use_system_default"}
    return _zone(entry_id, override_policy=policy, absence_delay=20, absence_flag=None, **kw)


def _never_configured_zone(entry_id: str, **kw) -> MockConfigEntry:
    """A zone that never configured its override step (defers by the 'never configured' signal),
    with its own absence delay."""
    return _zone(entry_id, override_policy=None, absence_delay=15, absence_flag=False, **kw)


# ---------------------------------------------------------------------------
# Chain A: the published beta.3.2 model, upgraded and then lived with
# ---------------------------------------------------------------------------

class TestUpgradeChain:
    async def test_beta32_upgrade_then_restarts_changes_removal_and_new_zones(self, hass, monkeypatch) -> None:
        system_entry = _system()
        inherit = _legacy("inh")                                   # defers for both settings
        own = _beta32_own_zone("own")                              # beta.3.2 explicit values, no markers
        mixed = _never_configured_zone("mix")                      # override defers, absence own
        off = _legacy("off", disabled=True)                        # disabled, defers
        zones = (inherit, own, mixed, off)
        _add(hass, system_entry, *zones)
        system_data = copy.deepcopy(_SYSTEM_DATA)
        expected = {e.entry_id: _before(e, system_data) for e in zones}
        assert expected["inh"]["absence"] == 90 and expected["inh"]["strategy"] == "duration"
        assert expected["own"]["strategy"] == "fixed_time" and expected["own"]["absence"] == 20
        assert expected["mix"]["strategy"] == "duration" and expected["mix"]["absence"] == 15
        cores = {e.entry_id: _core(e.data) for e in zones}
        titles = {e.entry_id: e.title for e in zones}
        writes = _Writes(hass, monkeypatch)

        # --- the start: migration and retirement happen during it -----------------------------
        assert await _start(hass)
        assert _systems(hass) == [] and _issue_ids(hass) == []
        for e in zones:
            assert e.minor_version == 2 and e.version == 1, e.entry_id            # also the disabled one
            assert _core(e.data) == cores[e.entry_id], e.entry_id                 # covers, windows, ids
            assert e.title == titles[e.entry_id]
            assert e.data["override_policy"]["use_system_default"] is False
            assert e.data["absence_delay_use_system_default"] is False
        assert off.disabled_by is ConfigEntryDisabler.USER and off.state is ConfigEntryState.NOT_LOADED
        for e in (inherit, own, mixed):
            assert e.state is ConfigEntryState.LOADED
            assert _effective(e) == {**expected[e.entry_id]}, e.entry_id
        # the System defaults were written into the inheriting zones as plain values
        assert inherit.data["absence_delay_min"] == 90 and mixed.data["absence_delay_min"] == 15
        migrated = {e.entry_id: copy.deepcopy(dict(e.data)) for e in zones}

        # --- repeated restarts: nothing is written, nothing comes back ---------------------------
        writes.clear()
        for _ in range(3):
            await _restart(hass)
            assert _systems(hass) == [] and _issue_ids(hass) == []
            for e in (inherit, own, mixed):
                assert e.state is ConfigEntryState.LOADED and _effective(e) == expected[e.entry_id]
            assert {e.entry_id: dict(e.data) for e in zones} == migrated
        assert writes.calls == []

        # --- the disabled zone is enabled later and runs with what it inherited -------------------
        await hass.config_entries.async_set_disabled_by("off", None)
        await hass.async_block_till_done()
        assert off.state is ConfigEntryState.LOADED and _effective(off) == expected["off"]
        assert _systems(hass) == []

        # --- a zone is removed: the others and the (absent) System entry are unaffected -----------
        await hass.config_entries.async_remove("mix")
        await hass.async_block_till_done()
        assert _systems(hass) == []
        for e in (inherit, own, off):
            assert e.state is ConfigEntryState.LOADED and _effective(e) == expected[e.entry_id]
            assert dict(e.data) == migrated[e.entry_id]

        # --- the final zones are removed: nothing of SmartShading is left, nothing is created -----
        for entry_id in ("inh", "own", "off"):
            await hass.config_entries.async_remove(entry_id)
            await hass.async_block_till_done()
        assert hass.config_entries.async_entries(DOMAIN) == []
        assert not [e for e in er.async_get(hass).entities.values() if e.platform == DOMAIN]
        assert not [d for d in dr.async_get(hass).devices.values()
                    if any(i[0] == DOMAIN for i in d.identifiers)]
        assert _issue_ids(hass) == []

        # --- a new zone afterwards: a plain zone-only entry, configurable, no System entry ---------
        fresh = _independent("fresh")
        _add(hass, fresh)
        await hass.config_entries.async_setup("fresh")
        await hass.async_block_till_done()
        assert fresh.state is ConfigEntryState.LOADED and _effective(fresh) == _OWN_EFFECTIVE
        assert _systems(hass) == []
        result = await _open(hass, "fresh", "presence")                     # a zone-local page: no System switch
        assert "absence_delay_use_system_default" not in _fields(result)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {
            "presence_entity_ids": [PERSON_ENTITY], "presence_policy": "any_home", "absence_position": 25})
        assert "absence_delay_min" in _fields(result)                       # the zone's own delay page
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"absence_delay_min": 7})
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        await hass.async_block_till_done()
        assert fresh.data["absence_delay_min"] == 7 and fresh.data["presence_entity_ids"] == [PERSON_ENTITY]
        assert fresh.data["zones"][0]["absence_position"] == 25
        await _restart(hass)
        assert fresh.state is ConfigEntryState.LOADED and _systems(hass) == []
        assert fresh.data["presence_entity_ids"] == [PERSON_ENTITY]
        assert _effective(fresh) == {**_OWN_EFFECTIVE, "absence": 7}


# ---------------------------------------------------------------------------
# Chain B: faults on the way (malformed data, failed removal, retry, restarts in between)
# ---------------------------------------------------------------------------

class TestFaultChain:
    async def test_malformed_data_failed_removal_retry_and_restarts_in_between(self, hass, monkeypatch) -> None:
        # a damaged System entry, a zone that needs it, a zone with unreadable own data, a healthy one
        system_entry = _system({CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM,
                                "system_override_policy": {"release_strategy": "zzz", "duration_min": "x"},
                                "system_absence_delay_min": "oops"})
        needs_system = _legacy("needs")
        broken = _zone("broken", override_policy=copy.deepcopy(_OWN_POLICY), absence_flag="false")
        healthy = _independent("healthy")
        zones = (needs_system, broken, healthy)
        _add(hass, system_entry, *zones)
        originals = {e.entry_id: copy.deepcopy(dict(e.data)) for e in (needs_system, broken)}
        system_before = copy.deepcopy(dict(system_entry.data))
        expected_needs = _before(needs_system, dict(system_entry.data))
        writes = _Writes(hass, monkeypatch)

        # --- start: the gate stays closed, the unverifiable zones are left exactly as they are -----
        assert await _start(hass)
        assert [e.entry_id for e in _systems(hass)] == ["system"]
        assert dict(system_entry.data) == system_before
        assert _issue_ids(hass) == ["system_retirement_zone_broken", "system_retirement_zone_needs"]
        for e in (needs_system, broken):
            assert dict(e.data) == originals[e.entry_id] and e.minor_version == 1, e.entry_id
        assert healthy.state is ConfigEntryState.LOADED and _effective(healthy) == _OWN_EFFECTIVE
        assert _effective(needs_system) == expected_needs         # runs as before: nothing guessed
        assert writes.calls == [] or set(writes.calls) <= {"healthy"}

        # --- restarts in this state: same picture, no duplicate issue, no write --------------------
        writes.clear()
        for _ in range(2):
            await _restart(hass)
            assert [e.entry_id for e in _systems(hass)] == ["system"]
            assert _issue_ids(hass) == ["system_retirement_zone_broken", "system_retirement_zone_needs"]
            for e in (needs_system, broken):
                assert dict(e.data) == originals[e.entry_id]
        assert not [w for w in writes.calls if w in ("needs", "broken")]

        # --- the owner recovers the zone that needs the System entry through ITS OWN options -------
        await _own_presence(hass, "needs", 12)
        # its absence delay is its own now; the override settings, which it only inherited, are
        # materialized with exactly the values it has been running with
        assert needs_system.minor_version == 2
        assert _effective(needs_system) == {**expected_needs, "absence": 12}
        assert needs_system.data["override_policy"]["use_system_default"] is False
        assert [e.entry_id for e in _systems(hass)] == ["system"], "'broken' is still unverified"
        assert _issue_ids(hass) == ["system_retirement_zone_broken"]

        # --- the last blocker is repaired; the removal then FAILS once -------------------------------
        hass.config_entries.async_update_entry(
            broken, data={**broken.data, "absence_delay_use_system_default": False}, minor_version=2)
        expected_broken = _before(broken, dict(system_entry.data))
        original_remove = hass.config_entries.async_remove

        async def failing(entry_id):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(hass.config_entries, "async_remove", failing)
        await hass.config_entries.async_reload("broken")
        await hass.async_block_till_done()
        assert [e.entry_id for e in _systems(hass)] == ["system"]
        assert system_entry.state is ConfigEntryState.LOADED           # kept and working
        assert _issue_ids(hass) == [life.ISSUE_REMOVAL_FAILED]
        for e in zones:
            assert e.state is ConfigEntryState.LOADED, e.entry_id      # no zone was disturbed

        # --- restart during the failed transition, then a restart with a working removal ------------
        await _restart(hass)
        assert [e.entry_id for e in _systems(hass)] == ["system"]
        assert _issue_ids(hass) == [life.ISSUE_REMOVAL_FAILED]
        monkeypatch.setattr(hass.config_entries, "async_remove", original_remove)
        await _restart(hass)
        assert _systems(hass) == [] and _issue_ids(hass) == []
        for e in zones:
            assert e.state is ConfigEntryState.LOADED, e.entry_id
        # the recovered zone runs with the values the owner chose, the repaired one with its own
        assert _effective(needs_system) == {**expected_needs, "absence": 12}
        assert _effective(broken) == expected_broken and expected_broken["strategy"] == "fixed_time"
        assert _effective(healthy) == _OWN_EFFECTIVE
        for e in (needs_system, broken):
            assert _core(e.data) == _core(originals[e.entry_id]), e.entry_id


# ---------------------------------------------------------------------------
# KNOWN, PINNED weakness (since beta.3.2, pinned by PA-1): a stored policy that is not a mapping
# ---------------------------------------------------------------------------

class TestKnownWeaknessNonMappingPolicy:
    async def test_end_state_of_a_zone_that_cannot_be_set_up_because_of_non_mapping_policy_data(
            self, hass, monkeypatch) -> None:
        """PINNED, NOT an endorsement: a zone whose stored override policy is not a mapping, or that
        defers to a System entry whose policy is not a mapping, is not set up (the resolver raises,
        the zone ends in SETUP_ERROR and runs nothing).  Changing that needs an owner decision
        (see the roadmap, PA-5 findings).  What PA-5 pins is that everything around it stays safe:
        nothing is rewritten, the System entry is kept, each affected zone gets its Repairs issue,
        the other zones run, restarts change nothing and no System entry is created."""
        system_entry = _system({CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM, "system_override_policy": "garbage"})
        defers = _legacy("defers")
        own_broken = _zone("ownbroken", override_policy="not a mapping", absence_flag=False)
        healthy = _independent("healthy")
        _add(hass, system_entry, defers, own_broken, healthy)
        originals = {e.entry_id: copy.deepcopy(dict(e.data)) for e in (defers, own_broken)}
        system_before = copy.deepcopy(dict(system_entry.data))
        writes = _Writes(hass, monkeypatch)
        assert await _start(hass)
        for _ in range(2):
            assert defers.state is ConfigEntryState.SETUP_ERROR
            assert own_broken.state is ConfigEntryState.SETUP_ERROR
            assert healthy.state is ConfigEntryState.LOADED and _effective(healthy) == _OWN_EFFECTIVE
            assert [e.entry_id for e in _systems(hass)] == ["system"]
            assert dict(system_entry.data) == system_before
            assert {e.entry_id: dict(e.data) for e in (defers, own_broken)} == originals
            assert _issue_ids(hass) == ["system_retirement_zone_defers", "system_retirement_zone_ownbroken"]
            await _restart(hass)
        assert not [w for w in writes.calls if w != "healthy"]


# ---------------------------------------------------------------------------
# Interrupted migration and backup restoration around the matrix
# ---------------------------------------------------------------------------

class TestInterruptedAndRestored:
    async def test_an_interrupted_migration_is_finished_by_the_next_start_without_loss(self, hass) -> None:
        """Some zones were already converted (minor 2) when the previous run stopped, the others not;
        the System entry is still stored."""
        done = _independent("done")
        half = _legacy("half")
        off = _legacy("off", disabled=True)
        system_entry = _system()
        _add(hass, system_entry, done, half, off)
        expected = {e.entry_id: _before(e, copy.deepcopy(_SYSTEM_DATA)) for e in (half, off)}
        cores = {e.entry_id: _core(e.data) for e in (done, half, off)}
        assert await _start(hass)
        assert _systems(hass) == [] and _issue_ids(hass) == []
        assert {e.entry_id: e.minor_version for e in (done, half, off)} == {"done": 2, "half": 2, "off": 2}
        assert {e.entry_id: _core(e.data) for e in (done, half, off)} == cores
        assert _effective(half) == expected["half"] and _effective(done) == _OWN_EFFECTIVE
        await hass.config_entries.async_set_disabled_by("off", None)
        await hass.async_block_till_done()
        assert _effective(off) == expected["off"]

    async def test_a_restored_backup_of_a_finished_migration_with_leftover_legacy_rows_converges(self, hass) -> None:
        """The backup holds the already migrated zones and a System entry that was NOT yet removed when
        the backup was taken (plus its registry rows): the next start retires it and removes the rows."""
        a, b = _independent("a"), _independent("b", disabled=True)
        system_entry = _system()
        _add(hass, system_entry, a, b)
        legacy_rows = [er.async_get(hass).async_get_or_create(
            "switch", DOMAIN, "system_debug_logging", config_entry=system_entry).entity_id]
        assert await _start(hass)
        assert _systems(hass) == [] and not er.async_get(hass).async_get(legacy_rows[0])
        assert a.state is ConfigEntryState.LOADED and b.disabled_by is ConfigEntryDisabler.USER
        await _restart(hass)
        assert _systems(hass) == [] and a.state is ConfigEntryState.LOADED


# ---------------------------------------------------------------------------
# Registry: entity rows go before the device row (no dangling reference at any moment)
# ---------------------------------------------------------------------------

class TestEntityBeforeDevice:
    async def test_legacy_entities_are_removed_before_their_device(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        system_entry = _system()
        _add(hass, system_entry, bad)
        _seed_legacy_rows(hass, system_entry)
        order: list[str] = []
        hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            lambda e: order.append("entity") if e.data["action"] == "remove" else None)
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            lambda e: order.append("device") if e.data["action"] == "remove" else None)
        assert await _start(hass)
        await hass.async_block_till_done()
        assert order.count("entity") == 3 and order.count("device") == 1
        assert order.index("device") > max(i for i, k in enumerate(order) if k == "entity")
