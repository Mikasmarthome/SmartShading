"""PA-2c.2 (real Home Assistant): safe retirement of the legacy System ConfigEntry.

Real config entries, the real ConfigEntries API (unload, removal and its return value, setup
locks), the real entity/device/issue registries, real coordinators and the real service
registry.  Covers: removal only after a fresh eligibility assessment, every blocker keeps the
System entry (with a Repairs issue), failure and retry, the time-of-check/time-of-use window,
no resurrection of the System entry, and continuity of everything the zones share.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.smartshading as ss
from custom_components.smartshading import system_retirement_lifecycle as life
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import (
    CONF_ENTRY_TYPE,
    DATA_GLOBAL_DISPATCH,
    DOMAIN,
    ENTRY_TYPE_SYSTEM,
    ENTRY_TYPE_ZONE,
    SYSTEM_DEVICE_IDENTIFIER,
)
from custom_components.smartshading.diagnostics import async_get_config_entry_diagnostics
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
_SYSTEM_EFFECTIVE = {"strategy": "duration", "duration": 45, "night": 300, "tolerance": 10,
                     "safety_timeout": False, "absence": 90}
_OWN_POLICY = {"release_strategy": "fixed_time", "fixed_until": "06:30", "duration_min": 77,
               "night_duration_min": 400, "allow_comfort_actions": True,
               "allow_protection_actions": False, "detection_tolerance": 12,
               "safety_timeout_enabled": False, "use_system_default": False}
_OWN_EFFECTIVE = {"strategy": "fixed_time", "duration": 77, "night": 400, "tolerance": 12,
                  "safety_timeout": False, "absence": 20}
_NOON = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


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


def _legacy(entry_id: str, **kw) -> MockConfigEntry:
    """A v1 zone that defers to the System entry for both inherited settings."""
    return _zone(entry_id, override_policy={"use_system_default": True}, absence_flag=True, **kw)


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


def _issue(hass, issue_id: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


@pytest.fixture
def removals(monkeypatch, hass):
    log: list[str] = []
    original = hass.config_entries.async_remove

    async def spy(entry_id):
        log.append(entry_id)
        return await original(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_remove", spy)
    return log


# ---------------------------------------------------------------------------
# Retirement at start and its conditions
# ---------------------------------------------------------------------------

class TestRetirementAtStart:
    async def test_an_upgrade_with_independent_zones_retires_the_system_entry(self, hass, removals) -> None:
        a, b = _independent("a"), _independent("b", disabled=True)
        _add(hass, _system(), a, b)
        before = {z.entry_id: (copy.deepcopy(dict(z.data)), z.minor_version) for z in (a, b)}
        assert await _start(hass)
        assert _systems(hass) == [] and removals == ["system"]
        assert {z.entry_id: (dict(z.data), z.minor_version) for z in (a, b)} == before
        assert a.state is ConfigEntryState.LOADED and b.state is ConfigEntryState.NOT_LOADED
        assert b.disabled_by is ConfigEntryDisabler.USER
        assert _effective(a) == _OWN_EFFECTIVE
        assert _issue_ids(hass) == []

    async def test_legacy_zones_are_migrated_and_then_the_system_entry_is_retired(self, hass, removals) -> None:
        a, b = _legacy("a"), _legacy("b", disabled=True)
        _add(hass, _system(), a, b)
        assert await _start(hass)
        assert _systems(hass) == []
        assert a.minor_version == b.minor_version == 2
        # the effective values did not change: they were materialized from the System entry
        assert _effective(a) == _SYSTEM_EFFECTIVE
        assert b.data["absence_delay_min"] == 90 and b.data["override_policy"]["duration_min"] == 45
        assert b.data["absence_delay_use_system_default"] is False

    async def test_blocked_system_entry_registers_nothing_and_old_rows_are_cleaned_at_start(self, hass) -> None:
        """PA-3/PA-4: the System entry forwards no platform, so while it is kept it creates no
        entity and no device, and the rows an older version left in the registries are removed
        at the start (they can never come alive again) without touching the entry or any zone."""
        blocked = _zone("bad", override_policy="not a mapping", absence_flag=False)   # unverifiable
        good = _independent("good")
        system_entry = _system()
        _add(hass, system_entry, blocked, good)
        reg = er.async_get(hass)
        legacy_rows = [
            reg.async_get_or_create("button", DOMAIN, "system_system_export_support_data",
                                    config_entry=system_entry).entity_id,
            reg.async_get_or_create("switch", DOMAIN, "system_debug_logging",
                                    config_entry=system_entry).entity_id,
        ]
        dr.async_get(hass).async_get_or_create(
            config_entry_id="system", identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)})
        assert await _start(hass)
        system = hass.config_entries.async_get_entry("system")
        assert system.state is ConfigEntryState.LOADED                      # kept: a zone is blocked
        assert not [e for e in reg.entities.values() if e.config_entry_id == "system"]
        assert not dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)})
        assert all(hass.states.get(entity_id) is None for entity_id in legacy_rows)
        assert hass.states.async_entity_ids("button") == []
        zone_rows = [e.entity_id for e in reg.entities.values() if e.config_entry_id == "good"]
        assert zone_rows
        # fix the blocker: the next zone setup retires the System entry live
        hass.config_entries.async_update_entry(
            blocked, data={**blocked.data, "override_policy": copy.deepcopy(_OWN_POLICY)}, minor_version=2)
        await hass.config_entries.async_reload(blocked.entry_id)
        await hass.async_block_till_done()
        assert _systems(hass) == []
        assert not [e for e in reg.entities.values() if e.config_entry_id == "system"]
        assert not dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)})
        assert [e.entity_id for e in reg.entities.values() if e.config_entry_id == "good"] == zone_rows
        assert good.state is ConfigEntryState.LOADED
        assert _issue_ids(hass) == []

    async def test_an_unverifiable_zone_keeps_the_system_entry_and_raises_an_issue(self, hass, removals) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        good = _independent("good")
        _add(hass, _system(), bad, good)
        before = copy.deepcopy(dict(bad.data))
        assert await _start(hass)
        assert removals == [] and len(_systems(hass)) == 1
        assert dict(bad.data) == before and bad.minor_version == 1       # never silently changed
        assert good.state is ConfigEntryState.LOADED and _effective(good) == _OWN_EFFECTIVE
        assert _issue_ids(hass) == [life.ISSUE_ZONE_PREFIX + "bad"]
        issue = _issue(hass, life.ISSUE_ZONE_PREFIX + "bad")
        assert issue.translation_key == life.KEY_ZONE_BLOCKED and issue.is_fixable is False
        assert issue.translation_placeholders == {"zone": "Zone bad"}
        assert "not a mapping" not in repr(issue)

    async def test_a_disabled_blocked_zone_keeps_the_system_entry(self, hass, removals) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False, disabled=True)
        _add(hass, _system(), bad, _independent("good"))
        assert await _start(hass)
        assert removals == [] and len(_systems(hass)) == 1
        assert _issue_ids(hass) == [life.ISSUE_ZONE_PREFIX + "bad"]
        assert bad.state is ConfigEntryState.NOT_LOADED and bad.disabled_by is ConfigEntryDisabler.USER

    async def test_a_zone_with_an_unsupported_major_version_blocks_with_its_own_issue(self, hass, removals) -> None:
        future = _independent("future", version=2)
        _add(hass, _system(), future, _independent("good"))
        assert await _start(hass)
        assert removals == [] and len(_systems(hass)) == 1
        assert _issue(hass, life.ISSUE_ZONE_PREFIX + "future").translation_key == life.KEY_ZONE_UNSUPPORTED
        assert future.state is ConfigEntryState.MIGRATION_ERROR
        assert hass.config_entries.async_get_entry("good").state is ConfigEntryState.LOADED

    async def test_a_zone_saved_by_a_newer_minor_version_blocks_and_is_not_rewritten(self, hass, removals) -> None:
        newer = _zone("newer", override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20,
                      absence_flag=False, minor=3)
        _add(hass, _system(), newer)
        before = copy.deepcopy(dict(newer.data))
        assert await _start(hass)
        assert removals == [] and dict(newer.data) == before and newer.minor_version == 3
        assert _issue(hass, life.ISSUE_ZONE_PREFIX + "newer").translation_key == life.KEY_ZONE_UNSUPPORTED

    async def test_minor_version_two_alone_never_retires(self, hass, removals) -> None:
        fake = _zone("fake", minor=2, absence_flag=True)    # claims v2 but still defers
        _add(hass, _system(), fake)
        assert await _start(hass)
        # the recovery path re-materializes it (still verified), then it IS independent
        assert fake.data["absence_delay_use_system_default"] is False
        assert _effective(fake)["absence"] == 90

    async def test_a_failed_persistence_keeps_the_system_entry_with_a_matching_issue(self, hass, monkeypatch, removals) -> None:
        a = _legacy("a")
        _add(hass, _system(), a)
        real = hass.config_entries.async_update_entry

        def failing(entry, **kwargs):
            if entry.entry_id == "a":
                raise OSError("disk full")
            return real(entry, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_update_entry", failing)
        assert await _start(hass)
        assert removals == [] and len(_systems(hass)) == 1
        assert a.minor_version == 1 and a.state is ConfigEntryState.LOADED
        assert _issue(hass, life.ISSUE_ZONE_PREFIX + "a").translation_key == life.KEY_ZONE_NOT_SAVED
        assert _effective(a) == _SYSTEM_EFFECTIVE            # keeps running with its legacy settings

    async def test_the_failure_record_is_cleared_when_a_later_pass_succeeds(self, hass, monkeypatch) -> None:
        a = _legacy("a")
        _add(hass, _system(), a)
        real = hass.config_entries.async_update_entry
        state = {"fail": True}

        def flaky(entry, **kwargs):
            if entry.entry_id == "a" and state["fail"]:
                raise OSError("disk full")
            return real(entry, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_update_entry", flaky)
        assert await _start(hass)
        assert life.migration_failures(hass) == {"a": life.FAILURE_PERSISTENCE}
        state["fail"] = False
        await ss.async_setup(hass, {})                      # the next pass (e.g. the next start)
        await hass.async_block_till_done()
        assert life.migration_failures(hass) == {}
        assert a.minor_version == 2 and _systems(hass) == [] and _issue_ids(hass) == []

    async def test_a_failed_persistence_with_a_failed_rollback_keeps_the_gate_closed(self, hass, monkeypatch, removals) -> None:
        a = _legacy("a")
        _add(hass, _system(), a)
        real = hass.config_entries.async_update_entry

        def failing(entry, **kwargs):
            if entry.entry_id == "a":
                if "data" in kwargs and kwargs.get("minor_version") == 2:
                    real(entry, data=kwargs["data"])        # only the data lands, not the version...
                raise OSError("the write fails, and so does the rollback")
            return real(entry, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_update_entry", failing)
        assert await _start(hass)
        assert len(_systems(hass)) == 1 and removals == []

    async def test_missing_system_data_for_a_deferring_zone_never_recreates_it(self, hass, removals) -> None:
        legacy = _legacy("a")
        _add(hass, legacy)                                   # there is no System entry at all
        assert await _start(hass)
        assert _systems(hass) == [] and removals == []
        assert legacy.minor_version == 1 and legacy.state is ConfigEntryState.LOADED
        assert _issue_ids(hass) == [life.ISSUE_ZONE_PREFIX + "a"]
        await hass.config_entries.async_reload("a")
        await hass.async_block_till_done()
        assert _systems(hass) == []

    async def test_a_malformed_system_entry_keeps_the_gate_closed_for_zones_that_need_it(self, hass, removals) -> None:
        legacy = _legacy("a")
        bad_system = _system({CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM, "system_override_policy": "garbage"})
        _add(hass, bad_system, legacy)
        before = copy.deepcopy(dict(legacy.data))
        assert await _start(hass)
        assert removals == [] and len(_systems(hass)) == 1
        assert dict(legacy.data) == before and legacy.minor_version == 1
        assert _issue_ids(hass) == [life.ISSUE_ZONE_PREFIX + "a"]

    async def test_a_system_entry_without_any_zone_is_retired(self, hass, removals) -> None:
        _add(hass, _system())
        assert await _start(hass)
        assert _systems(hass) == []


# ---------------------------------------------------------------------------
# Repairs: deduplication and clearance
# ---------------------------------------------------------------------------

class TestRepairs:
    async def test_issues_are_not_duplicated_by_repeated_setup_and_reload(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        _add(hass, _system(), bad)
        assert await _start(hass)
        for _ in range(3):
            await ss.async_setup(hass, {})
            await hass.config_entries.async_reload("bad")
            await hass.async_block_till_done()
        assert _issue_ids(hass) == [life.ISSUE_ZONE_PREFIX + "bad"]
        assert len(ir.async_get(hass).issues) == 1

    async def test_issues_are_cleared_when_the_blocker_is_resolved(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        _add(hass, _system(), bad)
        assert await _start(hass)
        assert _issue_ids(hass)
        hass.config_entries.async_update_entry(
            bad, data={**bad.data, "override_policy": copy.deepcopy(_OWN_POLICY), "absence_delay_min": 20},
            minor_version=2)
        await hass.config_entries.async_reload("bad")
        await hass.async_block_till_done()
        assert _issue_ids(hass) == [] and _systems(hass) == []

    async def test_removing_the_blocked_zone_clears_its_issue_and_retires_the_system_entry(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        good = _independent("good")
        _add(hass, _system(), bad, good)
        assert await _start(hass)
        assert _issue_ids(hass) == [life.ISSUE_ZONE_PREFIX + "bad"]
        await hass.config_entries.async_remove("bad")
        await hass.async_block_till_done()
        assert _issue_ids(hass) == [] and _systems(hass) == []
        assert good.state is ConfigEntryState.LOADED

    async def test_other_issues_survive_the_clearance(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        _add(hass, _system(), bad)
        assert await _start(hass)
        ir.async_create_issue(hass, "other", "system_retirement_zone_lookalike", is_fixable=False,
                              severity=ir.IssueSeverity.WARNING, translation_key="x")
        ir.async_create_issue(hass, DOMAIN, "duplicate_cover_foo", is_fixable=False,
                              severity=ir.IssueSeverity.WARNING, translation_key="duplicate_cover_identical",
                              translation_placeholders={"cover": "c", "locations": "l", "count": "2"})
        await ss._async_retire_system_entry(hass)
        reg = ir.async_get(hass)
        assert reg.async_get_issue("other", "system_retirement_zone_lookalike") is not None
        assert reg.async_get_issue(DOMAIN, "duplicate_cover_foo") is not None

    async def test_issues_are_not_persistent(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        _add(hass, _system(), bad)
        assert await _start(hass)
        assert _issue(hass, life.ISSUE_ZONE_PREFIX + "bad").is_persistent is False


# ---------------------------------------------------------------------------
# Failure, retry, time of check / time of use
# ---------------------------------------------------------------------------

class TestFailureRetryAndRaces:
    async def _blocked_then_ready(self, hass):
        """A System entry kept alive by one blocker; returns (system, zone) once it is fixed."""
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        _add(hass, _system(), bad)
        assert await _start(hass)
        system = hass.config_entries.async_get_entry("system")
        assert system.state is ConfigEntryState.LOADED
        hass.config_entries.async_update_entry(
            bad, data={**bad.data, "override_policy": copy.deepcopy(_OWN_POLICY), "absence_delay_min": 20},
            minor_version=2)
        return system, bad

    async def test_a_removal_api_failure_keeps_the_entry_loaded_and_raises_an_issue(self, hass, monkeypatch, caplog) -> None:
        system, _bad = await self._blocked_then_ready(hass)

        async def failing(entry_id):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(hass.config_entries, "async_remove", failing)
        assert await ss._async_retire_system_entry(hass) == life.FAILED
        assert hass.config_entries.async_get_entry("system") is system
        assert system.state is ConfigEntryState.LOADED          # unloaded for the attempt, then restored
        assert life.ISSUE_REMOVAL_FAILED in _issue_ids(hass)
        issue = _issue(hass, life.ISSUE_REMOVAL_FAILED)
        assert issue.translation_key == life.KEY_REMOVAL_FAILED and issue.is_fixable is False
        assert "registry unavailable" in caplog.text            # the cause is logged, not hidden

    async def test_the_retry_after_a_failure_succeeds_and_clears_the_issue(self, hass, monkeypatch) -> None:
        await self._blocked_then_ready(hass)
        original = hass.config_entries.async_remove

        async def failing(entry_id):
            raise RuntimeError("temporary")

        monkeypatch.setattr(hass.config_entries, "async_remove", failing)
        assert await ss._async_retire_system_entry(hass) == life.FAILED
        monkeypatch.setattr(hass.config_entries, "async_remove", original)
        assert await ss._async_retire_system_entry(hass) == life.RETIRED
        assert _systems(hass) == [] and _issue_ids(hass) == []

    async def test_a_zone_that_turns_dependent_while_the_system_entry_unloads_prevents_the_removal(self, hass, monkeypatch, removals) -> None:
        system, bad = await self._blocked_then_ready(hass)
        real_unload = hass.config_entries.async_unload

        async def unload_then_regress(entry_id, *args, **kwargs):
            result = await real_unload(entry_id, *args, **kwargs)
            if entry_id == "system":      # another task flips the zone back to inheriting
                hass.config_entries.async_update_entry(
                    bad, data={**bad.data, "absence_delay_use_system_default": True})
            return result

        monkeypatch.setattr(hass.config_entries, "async_unload", unload_then_regress)
        assert await ss._async_retire_system_entry(hass) == life.BLOCKED
        assert hass.config_entries.async_get_entry("system") is system
        assert system.state is ConfigEntryState.LOADED and removals == []

    async def test_concurrent_attempts_remove_the_entry_exactly_once(self, hass, removals) -> None:
        await self._blocked_then_ready(hass)
        results = await asyncio.gather(*(ss._async_retire_system_entry(hass) for _ in range(4)))
        assert sorted(results) == [life.ABSENT] * 3 + [life.RETIRED]
        assert removals == ["system"]

    async def test_concurrent_zone_reloads_and_retirement_leave_everything_consistent(self, hass, removals) -> None:
        system, bad = await self._blocked_then_ready(hass)
        good = _independent("good")
        _add(hass, good)
        await hass.config_entries.async_setup("good")
        await asyncio.gather(
            hass.config_entries.async_reload("good"), hass.config_entries.async_reload("bad"),
            ss._async_retire_system_entry(hass), ss._async_retire_system_entry(hass))
        await hass.async_block_till_done()
        assert _systems(hass) == [] and removals == ["system"]
        assert good.state is ConfigEntryState.LOADED and bad.state is ConfigEntryState.LOADED

    async def test_a_zone_removed_concurrently_does_not_break_the_retirement(self, hass, removals) -> None:
        system, bad = await self._blocked_then_ready(hass)
        await asyncio.gather(hass.config_entries.async_remove("bad"), ss._async_retire_system_entry(hass))
        await hass.async_block_till_done()
        assert _systems(hass) == [] and removals.count("system") == 1

    async def test_interrupted_removal_is_repeated_on_the_next_start(self, hass) -> None:
        system, bad = await self._blocked_then_ready(hass)
        # the previous run stopped before the removal: the entry is still stored, unloaded
        await hass.config_entries.async_unload("system")
        assert system.state is ConfigEntryState.NOT_LOADED
        await ss.async_setup(hass, {})                      # the next start's component setup
        assert _systems(hass) == []

    async def test_retirement_is_idempotent_across_repeated_setup(self, hass, removals) -> None:
        _add(hass, _system(), _independent("a"))
        assert await _start(hass)
        for _ in range(3):
            await ss.async_setup(hass, {})
        assert removals == ["system"] and _systems(hass) == []

    async def test_a_removal_that_needs_a_restart_is_reported_but_the_entry_is_gone(self, hass, monkeypatch, caplog) -> None:
        await self._blocked_then_ready(hass)
        original = hass.config_entries.async_remove

        async def needs_restart(entry_id):
            await original(entry_id)
            return {"require_restart": True}

        monkeypatch.setattr(hass.config_entries, "async_remove", needs_restart)
        assert await ss._async_retire_system_entry(hass) == life.RETIRED
        assert "restart" in caplog.text

    async def test_retirement_leaves_no_custom_debug_flag_behind(self, hass) -> None:
        system, bad = await self._blocked_then_ready(hass)
        assert await ss._async_retire_system_entry(hass) == life.RETIRED
        assert "debug_logging" not in hass.data[DOMAIN]

    async def test_a_blocked_assessment_never_touches_the_system_entry(self, hass, removals) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        _add(hass, _system(), bad)
        assert await _start(hass)
        system = hass.config_entries.async_get_entry("system")
        data = copy.deepcopy(dict(system.data))
        assert await ss._async_retire_system_entry(hass) == life.BLOCKED
        assert dict(system.data) == data and system.state is ConfigEntryState.LOADED and removals == []


# ---------------------------------------------------------------------------
# No resurrection, fresh installs and upgrades
# ---------------------------------------------------------------------------

class TestNoResurrection:
    async def test_a_fresh_install_never_has_a_system_entry(self, hass) -> None:
        a = _independent("a")
        _add(hass, a)
        assert await _start(hass)
        assert _systems(hass) == [] and a.state is ConfigEntryState.LOADED
        await hass.config_entries.async_reload("a")
        await hass.async_block_till_done()
        assert _systems(hass) == []

    async def test_a_system_flow_can_no_longer_be_started(self, hass) -> None:
        _add(hass, _independent("a"))
        assert await _start(hass)
        with pytest.raises(Exception):
            await hass.config_entries.flow.async_init(DOMAIN, context={"source": "system"})
        await hass.async_block_till_done()
        assert _systems(hass) == []

    async def test_reload_unload_and_restart_do_not_recreate_the_system_entry(self, hass) -> None:
        a, b = _independent("a"), _independent("b")
        _add(hass, _system(), a, b)
        assert await _start(hass)
        assert _systems(hass) == []
        await hass.config_entries.async_reload("a")
        await hass.config_entries.async_unload("b")
        await hass.config_entries.async_setup("b")
        await ss.async_setup(hass, {})                      # a restart runs the component setup again
        await hass.async_block_till_done()
        assert _systems(hass) == []

    async def test_removing_every_zone_after_the_retirement_leaves_nothing_and_creates_nothing(self, hass) -> None:
        a = _independent("a")
        _add(hass, _system(), a)
        assert await _start(hass)
        await hass.config_entries.async_remove("a")
        await hass.async_block_till_done()
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_a_new_zone_can_be_onboarded_after_the_retirement(self, hass) -> None:
        _add(hass, _system(), _independent("a"))
        assert await _start(hass)
        fresh = _independent("fresh")
        _add(hass, fresh)
        assert await hass.config_entries.async_setup("fresh")
        await hass.async_block_till_done()
        assert fresh.state is ConfigEntryState.LOADED and _systems(hass) == []
        assert _effective(fresh) == _OWN_EFFECTIVE

    async def test_the_zone_options_flow_still_works_without_a_system_entry(self, hass) -> None:
        a = _independent("a")
        _add(hass, _system(), a)
        assert await _start(hass)
        result = await hass.config_entries.options.async_init("a")
        assert result["type"].value == "menu" and "presence" in result["menu_options"]
        assert _systems(hass) == []

    async def test_an_upgrade_from_an_old_install_ends_without_a_system_entry_and_with_the_same_behavior(self, hass) -> None:
        zones = [_legacy("a"), _legacy("b"), _zone("c", override_policy=copy.deepcopy({**_OWN_POLICY, "use_system_default": False}),
                                                  absence_delay=15, absence_flag=False)]
        _add(hass, _system(), *zones)
        assert await _start(hass)
        assert _systems(hass) == []
        assert _effective(zones[0]) == _effective(zones[1]) == _SYSTEM_EFFECTIVE
        assert _effective(zones[2]) == {**_OWN_EFFECTIVE, "absence": 15}
        assert {z.minor_version for z in zones} == {2}


# ---------------------------------------------------------------------------
# Continuity of everything the zones share
# ---------------------------------------------------------------------------

class TestRuntimeContinuity:
    async def test_the_shared_dispatcher_and_services_survive_a_live_retirement(self, hass) -> None:
        bad = _zone("bad", override_policy="not a mapping", absence_flag=False)
        a = _independent("a")
        _add(hass, _system(), bad, a)
        assert await _start(hass)
        dispatch = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]
        assert a.runtime_data.coordinator._serial_dispatch is dispatch
        assert hass.services.has_service(DOMAIN, "clear_manual_override")
        hass.config_entries.async_update_entry(
            bad, data={**bad.data, "override_policy": copy.deepcopy(_OWN_POLICY), "absence_delay_min": 20},
            minor_version=2)
        await hass.config_entries.async_reload("bad")
        await hass.async_block_till_done()
        assert _systems(hass) == []
        assert hass.data[DOMAIN][DATA_GLOBAL_DISPATCH] is dispatch
        assert a.runtime_data.coordinator._serial_dispatch is dispatch
        assert bad.runtime_data.coordinator._serial_dispatch is dispatch
        assert hass.services.has_service(DOMAIN, "clear_manual_override")

    async def test_cover_ownership_survives_the_retirement(self, hass) -> None:
        a = _independent("a", extra={})
        b = _independent("b")
        _add(hass, _system(), a, b)
        b_data = copy.deepcopy(dict(b.data))
        shared = "cover.test_a"
        for group in b_data["cover_groups"]:
            group["cover_ids"] = [shared]
        hass.config_entries.async_update_entry(b, data=b_data)
        assert await _start(hass)
        assert _systems(hass) == []
        owned = [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN and i.startswith("duplicate_cover")]
        assert owned, "the duplicate-cover repair of the ownership layer is untouched"

    async def test_diagnostics_stay_available_for_every_zone(self, hass) -> None:
        a, b = _independent("a"), _independent("b", disabled=True)
        _add(hass, _system(), a, b)
        assert await _start(hass)
        assert _systems(hass) == []
        diag = await async_get_config_entry_diagnostics(hass, a)
        assert "support_export" in diag and "research_export" in diag and "forecast_learning" in diag

    async def test_disabled_zone_persistence_and_reenabling_after_the_retirement(self, hass) -> None:
        a, b = _independent("a"), _independent("b", disabled=True)
        _add(hass, _system(), a, b)
        assert await _start(hass)
        before = copy.deepcopy(dict(b.data))
        assert dict(b.data) == before and b.disabled_by is ConfigEntryDisabler.USER
        await hass.config_entries.async_set_disabled_by("b", None)
        await hass.async_block_till_done()
        assert b.state is ConfigEntryState.LOADED and _systems(hass) == []
        assert _effective(b) == _OWN_EFFECTIVE

    async def test_a_reenabled_legacy_zone_after_the_start_is_migrated_and_never_needs_a_new_system_entry(self, hass) -> None:
        legacy = _zone("old", override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20, absence_flag=False,
                       disabled=True)
        a = _independent("a")
        _add(hass, _system(), legacy, a)
        assert await _start(hass)
        assert _systems(hass) == [] and legacy.minor_version == 2      # migrated while disabled
        await hass.config_entries.async_set_disabled_by("old", None)
        await hass.async_block_till_done()
        assert legacy.state is ConfigEntryState.LOADED and _systems(hass) == []

    async def test_tier_one_rain_protection_works_after_the_retirement(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            await hass.config.async_set_time_zone("UTC")
            hass.states.async_set("sun.sun", "above_horizon", {
                "azimuth": 0.0, "elevation": 30.0,
                "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00"})
            calls: list[dict] = []

            async def set_position(call) -> None:
                calls.append(dict(call.data))
                pos = call.data["position"]
                hass.states.async_set(call.data["entity_id"], "open" if pos > 0 else "closed",
                                      {"current_position": pos})

            async def set_tilt(call) -> None:
                return None

            hass.services.async_register("cover", "set_cover_position", set_position)
            hass.services.async_register("cover", "set_cover_tilt_position", set_tilt)
            awning = _legacy("awn", hardware=CoverHardwareType.AWNING)      # defers to the System entry
            _add(hass, _system(), awning)
            hass.states.async_set("cover.test_awn", "open", {"current_position": 100})
            assert await _start(hass)
            assert _systems(hass) == [] and awning.minor_version == 2
            coordinator = awning.runtime_data.coordinator
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            calls.clear()
            hass.states.async_set("binary_sensor.rain", "on")
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert calls and all(c["entity_id"] == "cover.test_awn" and c["position"] == 0 for c in calls)

    async def test_two_zones_keep_sharing_one_dispatcher_through_a_restart_cycle(self, hass) -> None:
        a, b = _independent("a"), _independent("b")
        _add(hass, _system(), a, b)
        assert await _start(hass)
        first = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]
        await hass.config_entries.async_reload("a")
        await hass.config_entries.async_reload("b")
        await hass.async_block_till_done()
        assert hass.data[DOMAIN][DATA_GLOBAL_DISPATCH] is first
        assert a.runtime_data.coordinator._serial_dispatch is b.runtime_data.coordinator._serial_dispatch
        assert _systems(hass) == []
