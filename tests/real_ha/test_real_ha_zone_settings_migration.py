"""PA-2b (real Home Assistant): the zone settings migration against real ConfigEntries.

Covers persistence (data + minor_version written together), the component-level pass for
every zone incl. disabled ones, the per-entry safety net, failure containment, retry,
idempotence and the unchanged System entry / shared runtime.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
import logging
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.smartshading as ss
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    resolve_zone_absence_delay_min,
    resolve_zone_override_policy,
    to_storage_dict,
)
from custom_components.smartshading.config_flow import SmartShadingConfigFlow
from custom_components.smartshading.const import (
    CONF_ENTRY_TYPE,
    DATA_GLOBAL_DISPATCH,
    DOMAIN,
    ENTRY_TYPE_SYSTEM,
    ENTRY_TYPE_ZONE,
)
from custom_components.smartshading.models.cover_group import CoverGroup
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig

_SYSTEM_POLICY = {
    "release_strategy": "duration", "duration_min": 45, "night_duration_min": 300,
    "detection_tolerance": 99, "allow_comfort_actions": True,
    "allow_protection_actions": True, "safety_timeout_enabled": False,
}
_SYSTEM_DATA = {CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM, "system_override_policy": _SYSTEM_POLICY,
                "system_absence_delay_min": 90}
_SYSTEM_EFFECTIVE = {"strategy": "duration", "duration": 45, "night": 300, "tolerance": 10,
                     "safety_timeout": False, "absence": 90}
_DEFAULTS = {"strategy": "lifecycle", "duration": 120, "night": 720, "tolerance": 10,
             "safety_timeout": True, "absence": 30}
_DEFER = {"use_system_default": True, "release_strategy": "manual"}


def _zone(entry_id: str, *, override_policy: Any = "keep", absence_delay: Any = "keep",
          absence_flag: Any = "keep", disabled: bool = False, minor: int = 1) -> MockConfigEntry:
    zone = ZoneConfig(id=f"zone_{entry_id}", name=f"Zone {entry_id}")
    window = WindowConfig(id=f"window_{entry_id}", name="South", zone_id=zone.id, azimuth=180.0,
                          floor_level=0, cover_group_id=f"cg_{entry_id}")
    group = CoverGroup(id=f"cg_{entry_id}", window_id=window.id, cover_ids=[f"cover.test_{entry_id}"])
    data = {**to_storage_dict(SmartShadingConfigEntryData(
        name=f"Zone {entry_id}", use_home_location=False, zones=[zone], windows=[window],
        cover_groups=[group])), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE}
    for key, value in (("override_policy", override_policy), ("absence_delay_min", absence_delay),
                       ("absence_delay_use_system_default", absence_flag)):
        if value == "keep":
            continue
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return MockConfigEntry(domain=DOMAIN, data=data, entry_id=entry_id, minor_version=minor,
                           disabled_by=ConfigEntryDisabler.USER if disabled else None)


def _system(data: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data=copy.deepcopy(_SYSTEM_DATA if data is None else data),
                           source="system", entry_id="system", title="SmartShading System")


def _add(hass, *entries: MockConfigEntry) -> None:
    for e in entries:
        if e.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SYSTEM:
            hass.states.async_set(f"cover.test_{e.entry_id}", "closed", {"current_position": 0})
        e.add_to_hass(hass)


async def _start(hass) -> bool:
    ok = await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return ok


def _effective(entry) -> dict[str, Any]:
    c = entry.runtime_data.coordinator
    return {"strategy": c._override_release_strategy.value, "duration": c._override_duration_min,
            "night": c._override_night_duration_min, "tolerance": c._override_detection_tolerance,
            "safety_timeout": c._override_safety_timeout_enabled, "absence": c._absence_delay_min}


def _stored_effective(entry, system_data=None) -> tuple:
    """What the stored data resolve to, whatever the System entry holds (None: no System data)."""
    return (resolve_zone_override_policy(dict(entry.data), system_data),
            resolve_zone_absence_delay_min(dict(entry.data), system_data))


def _migrated(entry) -> bool:
    return (entry.minor_version == 2
            and entry.data["override_policy"]["use_system_default"] is False
            and entry.data["absence_delay_use_system_default"] is False)


@pytest.fixture
def writes(monkeypatch, hass):
    """Every async_update_entry call, as (entry_id, sorted kwargs)."""
    log: list[tuple[str, list[str]]] = []
    original = hass.config_entries.async_update_entry

    def spy(entry, **kwargs):
        log.append((entry.entry_id, sorted(kwargs)))
        return original(entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", spy)
    return log


class TestMaterialization:
    async def test_a_deferring_v11_zone_gets_the_system_values_written_explicitly(self, hass, writes) -> None:
        system = _system()
        zone = _zone("a", override_policy=None, absence_delay=20, absence_flag=True)
        _add(hass, zone, system)
        assert await _start(hass)
        assert zone.state is ConfigEntryState.LOADED and _migrated(zone)
        assert _effective(zone) == _SYSTEM_EFFECTIVE
        assert zone.data["override_policy"]["duration_min"] == 45
        assert zone.data["override_policy"]["detection_tolerance"] == 10    # zone-local, never the System's 99
        assert zone.data["absence_delay_min"] == 90
        assert _stored_effective(zone, None) == _stored_effective(zone, _SYSTEM_DATA)  # independent of System
        assert writes == [("a", ["data", "minor_version"])]                  # ONE call, both fields

    async def test_a_beta32_zone_with_explicit_values_keeps_them(self, hass, writes) -> None:
        zone = _zone("a", absence_delay=20)       # as the config flow stores it
        before = copy.deepcopy(dict(zone.data))
        _add(hass, zone, _system())
        assert await _start(hass)
        assert _migrated(zone) and zone.data["absence_delay_min"] == 20
        assert _effective(zone) == {**_DEFAULTS, "absence": 20}
        for key, value in before.items():
            if key not in ("override_policy", "absence_delay_use_system_default"):
                assert zone.data[key] == value
        assert zone.data["override_policy"] == {**before["override_policy"], "use_system_default": False}

    async def test_a_system_entry_without_defaults_materializes_the_code_defaults(self, hass) -> None:
        zone = _zone("a", override_policy=None, absence_delay=20, absence_flag=True)
        _add(hass, zone, _system({CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM}))
        assert await _start(hass)
        assert _migrated(zone) and _effective(zone) == _DEFAULTS

    async def test_every_zone_is_migrated_including_disabled_ones(self, hass, writes) -> None:
        on = _zone("on", override_policy=None, absence_flag=True)
        off = _zone("off", override_policy=None, absence_flag=True, disabled=True)
        _add(hass, on, off, _system())
        assert await _start(hass)
        assert sorted(w[0] for w in writes) == ["off", "on"]
        assert _migrated(on) and _migrated(off)
        assert off.state is ConfigEntryState.NOT_LOADED and off.disabled_by is ConfigEntryDisabler.USER
        assert off.data["override_policy"]["duration_min"] == 45
        # enabling it later runs with exactly the values it had when it was disabled
        await hass.config_entries.async_set_disabled_by(off.entry_id, None)
        await hass.async_block_till_done()
        assert off.state is ConfigEntryState.LOADED and _effective(off) == _SYSTEM_EFFECTIVE

    @pytest.mark.parametrize("shape", [
        dict(override_policy=None, absence_flag=True),
        dict(override_policy=_DEFER, absence_delay=33, absence_flag=True),
        dict(override_policy={"release_strategy": "fixed_time", "fixed_until": "07:30:00"}),
        dict(override_policy={"use_system_default": False, "release_strategy": "duration", "duration_min": 5}),
        dict(override_policy={"break_on_lifecycle": False, "duration_mode": "legacy"}),
        dict(absence_delay=0),
    ])
    async def test_the_effective_values_do_not_change_across_the_migration(self, hass, monkeypatch, shape) -> None:
        zone = _zone("a", **shape)
        _add(hass, zone, _system())
        with monkeypatch.context() as m:                       # the unmigrated run
            m.setattr(ss, "_migrate_zone_entry", lambda hass, entry: False)
            assert await _start(hass)
            before = _effective(zone)
        assert zone.minor_version == 1 and not _migrated(zone)
        assert await hass.config_entries.async_reload("a")      # layer 2 migrates it on this setup
        await hass.async_block_till_done()
        assert _migrated(zone)
        assert _effective(zone) == before


class TestSystemEntryStaysUntouched:
    async def test_the_system_entry_is_neither_changed_nor_retired(self, hass, writes) -> None:
        system = _system()
        _add(hass, _zone("a", override_policy=None, absence_flag=True), system)
        assert await _start(hass)
        assert hass.config_entries.async_get_entry("system") is system
        assert system.state is ConfigEntryState.LOADED
        assert dict(system.data) == _SYSTEM_DATA and (system.version, system.minor_version) == (1, 1)
        assert all(w[0] != "system" for w in writes)

    async def test_a_changed_system_default_no_longer_reaches_a_migrated_zone(self, hass) -> None:
        """Documented consequence: once materialized the zone owns its values."""
        system = _system()
        zone = _zone("a", override_policy=None, absence_flag=True)
        _add(hass, zone, system)
        assert await _start(hass)
        hass.config_entries.async_update_entry(system, data={**system.data, "system_absence_delay_min": 5})
        await hass.config_entries.async_reload("a")
        await hass.async_block_till_done()
        assert _effective(zone)["absence"] == 90

    async def test_a_mix_of_migrated_and_unmigrated_zones_is_safe(self, hass, writes) -> None:
        done = _zone("done", override_policy=None, absence_flag=True, minor=2)   # already at 2, still deferring
        todo = _zone("todo", override_policy=None, absence_flag=True)
        _add(hass, done, todo, _system())
        before = copy.deepcopy(dict(done.data))
        assert await _start(hass)
        assert dict(done.data) == before and done.minor_version == 2           # never rewritten
        assert [w[0] for w in writes] == ["todo"]
        assert done.state is ConfigEntryState.LOADED and todo.state is ConfigEntryState.LOADED
        assert _effective(done) == _effective(todo) == _SYSTEM_EFFECTIVE


class TestFailureContainment:
    async def test_a_missing_system_entry_leaves_a_dependent_zone_unmigrated_until_one_exists(self, hass) -> None:
        zone = _zone("a", override_policy=None, absence_flag=True)
        explicit = _zone("b")
        _add(hass, zone, explicit)
        assert await _start(hass)
        assert zone.minor_version == 1 and not _migrated(zone)           # cannot be judged yet
        assert _migrated(explicit)                                       # independent zone unaffected
        assert zone.state is ConfigEntryState.LOADED and _effective(zone) == _DEFAULTS
        # zone setup created the (empty) System entry: now the next setup can judge it
        assert len([e for e in hass.config_entries.async_entries(DOMAIN)
                    if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM]) == 1
        assert await hass.config_entries.async_reload("a")
        await hass.async_block_till_done()
        assert _migrated(zone) and _effective(zone) == _DEFAULTS

    @pytest.mark.parametrize("shape,running", [
        (dict(override_policy="manual"), None),                          # cannot even be resolved
        (dict(override_policy={"use_system_default": "false", "release_strategy": "manual"}), 30),
        (dict(absence_flag="false", absence_delay=20), 90),
        (dict(absence_delay=-5), -5),
    ], ids=["non-dict-policy", "string-flag-policy", "string-flag-absence", "negative-delay"])
    async def test_unverifiable_zones_stay_exactly_as_they_were_while_others_migrate(
        self, hass, writes, caplog, shape, running  # running: None = cannot load, else its absence delay
    ) -> None:
        bad, good = _zone("bad", **shape), _zone("good", override_policy=None, absence_flag=True)
        _add(hass, bad, good, _system())
        before = copy.deepcopy(dict(bad.data))
        with caplog.at_level(logging.WARNING):
            assert await _start(hass)
        assert dict(bad.data) == before and bad.minor_version == 1       # nothing substituted
        assert "bad" in caplog.text and "unmigrated" in caplog.text
        assert _migrated(good) and good.state is ConfigEntryState.LOADED
        assert _effective(good) == _SYSTEM_EFFECTIVE
        assert all(w[0] != "bad" for w in writes)
        if running is None:
            assert bad.state is not ConfigEntryState.LOADED              # as before: it cannot be resolved
        else:                                                            # Tier-1 handling keeps running
            assert bad.state is ConfigEntryState.LOADED
            assert _effective(bad)["absence"] == running   # the legacy semantics, unchanged

    async def test_a_failing_write_leaves_that_zone_unmigrated_and_a_retry_succeeds(
        self, hass, monkeypatch, caplog
    ) -> None:
        bad, good = _zone("bad", override_policy=None, absence_flag=True), _zone("good", override_policy=None, absence_flag=True)
        _add(hass, bad, good, _system())
        before = copy.deepcopy(dict(bad.data))
        original = hass.config_entries.async_update_entry

        def flaky(entry, **kwargs):
            if entry.entry_id == "bad":
                raise OSError("disk full")
            return original(entry, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(hass.config_entries, "async_update_entry", flaky)
            with caplog.at_level(logging.WARNING):
                assert await _start(hass)
        assert dict(bad.data) == before and bad.minor_version == 1
        assert _migrated(good) and good.state is ConfigEntryState.LOADED
        assert bad.state is ConfigEntryState.LOADED                       # Tier-1 handling unaffected
        # cause fixed: the next setup of that zone finishes the job
        assert await hass.config_entries.async_reload("bad")
        await hass.async_block_till_done()
        assert _migrated(bad) and _effective(bad) == _SYSTEM_EFFECTIVE

    async def test_a_write_that_applied_but_does_not_verify_is_rolled_back(self, hass, monkeypatch) -> None:
        zone = _zone("a", override_policy=None, absence_flag=True)
        _add(hass, zone, _system())
        before = copy.deepcopy(dict(zone.data))
        original = hass.config_entries.async_update_entry

        def torn(entry, **kwargs):
            if kwargs.get("minor_version") == 2:                  # applies a wrong value, then reports failure
                original(entry, data={**kwargs["data"], "absence_delay_min": 1}, minor_version=2)
                raise RuntimeError("listener failed")
            return original(entry, **kwargs)                      # the rollback goes through

        monkeypatch.setattr(hass.config_entries, "async_update_entry", torn)
        assert await _start(hass)
        assert dict(zone.data) == before and zone.minor_version == 1
        assert zone.state is ConfigEntryState.LOADED

    async def test_a_persisted_state_that_does_not_verify_is_rolled_back(self, hass, monkeypatch) -> None:
        zone = _zone("a", override_policy=None, absence_flag=True)
        _add(hass, zone, _system())
        before = copy.deepcopy(dict(zone.data))
        original = hass.config_entries.async_update_entry

        def tampering(entry, **kwargs):
            if kwargs.get("minor_version") == 2:
                kwargs = {**kwargs, "data": {**kwargs["data"], "absence_delay_min": 1}}
            return original(entry, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_update_entry", tampering)
        assert await _start(hass)
        assert dict(zone.data) == before and zone.minor_version == 1

    async def test_an_unrelated_error_in_the_migration_is_not_swallowed(self, hass, monkeypatch, caplog) -> None:
        def boom(*a, **k):
            raise RuntimeError("unrelated defect")

        monkeypatch.setattr(ss, "migrate_zone_entry", boom)
        zone = _zone("a")
        _add(hass, zone, _system())
        with caplog.at_level(logging.ERROR):
            assert await _start(hass) is False
        assert "unrelated defect" in caplog.text and zone.minor_version == 1


class TestIdempotenceRestartAndReload:
    async def test_a_second_pass_and_reloads_write_nothing(self, hass, writes) -> None:
        zone = _zone("a", override_policy=None, absence_flag=True)
        _add(hass, zone, _system())
        assert await _start(hass)
        stored = copy.deepcopy(dict(zone.data))
        writes.clear()
        ss._prepare_zone_entries(hass)                       # what a restart would run again
        for _ in range(2):
            assert await hass.config_entries.async_reload("a")
            await hass.async_block_till_done()
        assert writes == [] and dict(zone.data) == stored and zone.minor_version == 2

    async def test_a_restart_with_the_persisted_state_runs_with_the_same_values(self, hass, writes) -> None:
        """The entry as persisted after the migration (version 2, explicit values) no longer
        depends on what the System entry holds."""
        from custom_components.smartshading.zone_settings_migration import materialize_zone_settings
        legacy = _zone("a", override_policy=None, absence_flag=True)
        persisted = materialize_zone_settings(dict(legacy.data), _SYSTEM_DATA)
        zone = MockConfigEntry(domain=DOMAIN, data=persisted, entry_id="a", minor_version=2)
        _add(hass, zone, _system({CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM, "system_absence_delay_min": 5}))
        assert await _start(hass)
        assert dict(zone.data) == persisted and writes == []
        assert _effective(zone) == _SYSTEM_EFFECTIVE

    async def test_the_flow_declares_the_new_minor_version_and_keeps_the_major(self) -> None:
        assert (SmartShadingConfigFlow.VERSION, SmartShadingConfigFlow.MINOR_VERSION) == (1, 2)

    async def test_a_fresh_zone_stored_at_version_2_is_left_alone(self, hass, writes) -> None:
        zone = _zone("a", absence_delay=20, minor=2)
        _add(hass, zone)
        before = copy.deepcopy(dict(zone.data))
        assert await _start(hass)
        assert dict(zone.data) == before and writes == []
        assert _effective(zone) == {**_DEFAULTS, "absence": 20}


class TestSharedRuntimeAndDiagnostics:
    async def test_dispatch_services_and_diagnostics_are_preserved(self, hass) -> None:
        a, b = _zone("a", override_policy=None, absence_flag=True), _zone("b")
        _add(hass, a, b, _system())
        assert await _start(hass)
        dispatch = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]
        assert a.runtime_data.coordinator._serial_dispatch is dispatch
        assert b.runtime_data.coordinator._serial_dispatch is dispatch
        assert hass.services.has_service(DOMAIN, "clear_manual_override")
        from custom_components.smartshading.diagnostics import async_get_config_entry_diagnostics
        diag = await async_get_config_entry_diagnostics(hass, a)
        assert isinstance(diag, dict) and diag
