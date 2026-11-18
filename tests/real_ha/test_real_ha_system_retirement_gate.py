"""PA-2c.1 (real Home Assistant): the System retirement safety gate against real ConfigEntries.

Covers the eligibility check over all zones (disabled included), the recovery of migrated
zones that depend on the System again, the closed re-inheritance paths of the Options Flow
(save, reload, restart) and the guarantee that nothing here retires or changes the System
entry.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
import logging
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.smartshading as ss
from custom_components.smartshading import system_retirement as sr
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    resolve_zone_absence_delay_min,
    resolve_zone_override_policy,
    to_storage_dict,
)
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
_OWN_POLICY = {"release_strategy": "fixed_time", "fixed_until": "06:30", "duration_min": 77,
               "night_duration_min": 400, "allow_comfort_actions": True,
               "allow_protection_actions": False, "detection_tolerance": 12,
               "safety_timeout_enabled": False, "use_system_default": False}


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


def _gate(hass) -> dict[str, Any]:
    return ss._assess_system_retirement(hass).summary()


def _independent_zone(entry_id: str, **kw) -> MockConfigEntry:
    return _zone(entry_id, override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20,
                 absence_flag=False, minor=2, **kw)


def _system_untouched(hass) -> None:
    system = hass.config_entries.async_get_entry("system")
    assert system is not None and system.state is ConfigEntryState.LOADED
    assert dict(system.data) == _SYSTEM_DATA and system.minor_version == 1


@pytest.fixture(autouse=True)
def _assessment_only(monkeypatch):
    """PA-2c.1 pins the ASSESSMENT (eligibility, recovery, closed flow paths).  The retirement
    that acts on it (PA-2c.2) is covered by test_real_ha_system_retirement.py and is switched
    off here, so these tests keep examining the System entry next to fully independent zones."""
    async def not_retired(_hass):
        return "blocked"

    monkeypatch.setattr(ss, "_async_retire_system_entry", not_retired)


@pytest.fixture
def writes(monkeypatch, hass):
    log: list[tuple[str, list[str]]] = []
    original = hass.config_entries.async_update_entry

    def spy(entry, **kwargs):
        log.append((entry.entry_id, sorted(kwargs)))
        return original(entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", spy)
    return log


async def _options(hass, entry_id: str, *path: str):
    result = await hass.config_entries.options.async_init(entry_id)
    for step in path:
        assert result["type"] is FlowResultType.MENU, result
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})
    return result


def _fields(result) -> set[str]:
    return {str(k) for k in result["data_schema"].schema}


async def _save_own_override(hass, entry_id: str, tolerance: int = 15):
    result = await _options(hass, entry_id, "advanced", "manual_override")
    assert result["step_id"] == "manual_override"
    user = {"override_detection_tolerance": tolerance}
    if "override_use_system_default" in _fields(result):
        user["override_use_system_default"] = False
    result = await hass.config_entries.options.async_configure(result["flow_id"], user)
    assert result["step_id"] == "manual_override_custom"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "override_release_choice": "lifecycle", "override_allow_comfort_actions": False,
        "override_allow_protection_actions": False, "override_edit_timeouts": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def _save_presence(hass, entry_id: str, delay: int = 40):
    result = await _options(hass, entry_id, "presence")
    user = {"presence_entity_ids": [], "presence_policy": "any_home"}
    if "absence_delay_use_system_default" in _fields(result):
        user["absence_delay_use_system_default"] = False
    result = await hass.config_entries.options.async_configure(result["flow_id"], user)
    assert result["step_id"] == "presence_custom"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"absence_delay_min": delay})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


# --- the gate over real entries -------------------------------------------------------------------

class TestRetirementEligibility:
    async def test_after_the_migration_every_zone_incl_disabled_ones_is_independent(self, hass) -> None:
        a = _zone("a", override_policy={"use_system_default": True, "release_strategy": "manual"})
        b = _zone("b", override_policy=None, absence_flag=True, disabled=True)
        c = _zone("c")
        _add(hass, a, b, c, _system())
        assert await _start(hass)
        summary = _gate(hass)
        assert summary == {"eligible": True, "zones_checked": 3, "system_entry_present": True, "blocked": {}}
        assert all(e.minor_version == 2 for e in (a, b, c))
        _system_untouched(hass)

    async def test_the_gate_stays_closed_while_one_zone_cannot_be_verified_and_the_others_run(self, hass) -> None:
        bad = _zone("bad", absence_flag="false")                     # string flag: unverifiable
        ok, other = _zone("ok", override_policy=None, absence_flag=True), _zone("other")
        _add(hass, bad, ok, other, _system())
        before = copy.deepcopy(dict(bad.data))
        assert await _start(hass)
        summary = _gate(hass)
        assert summary["eligible"] is False
        assert summary["blocked"] == {"bad": [sr.MINOR_VERSION_UNMIGRATED, sr.ABSENCE_FLAG_INVALID]}
        assert dict(bad.data) == before and bad.minor_version == 1
        assert ok.minor_version == 2 and other.minor_version == 2
        for e in (bad, ok, other):
            assert e.state is ConfigEntryState.LOADED               # normal setup for every zone
        _system_untouched(hass)

    async def test_a_disabled_blocked_zone_keeps_the_gate_closed(self, hass) -> None:
        off = _zone("off", absence_flag="false", disabled=True)
        _add(hass, off, _zone("a"), _system())
        assert await _start(hass)
        assert _gate(hass)["blocked"] == {"off": [sr.MINOR_VERSION_UNMIGRATED, sr.ABSENCE_FLAG_INVALID]}
        assert off.disabled_by is not None and off.state is ConfigEntryState.NOT_LOADED

    async def test_a_mixed_version_installation_is_closed_until_every_zone_is_migrated(self, hass) -> None:
        v2, v1 = _independent_zone("v2"), _zone("v1", absence_flag="false")
        _add(hass, v2, v1, _system())
        assert await _start(hass)
        summary = _gate(hass)
        assert summary["eligible"] is False and list(summary["blocked"]) == ["v1"]
        assert v2.minor_version == 2

    async def test_a_missing_system_entry_keeps_a_dependent_zone_closed(self, hass) -> None:
        dep = _zone("dep", override_policy=None, absence_flag=True)
        _add(hass, dep)
        assert await _start(hass)
        assert dep.minor_version == 1 and dep.state is ConfigEntryState.LOADED
        assert _gate(hass)["blocked"] == {"dep": [
            sr.MINOR_VERSION_UNMIGRATED, sr.OVERRIDE_POLICY_MISSING, sr.ABSENCE_INHERITANCE_ACTIVE]}

    async def test_a_check_is_repeatable_and_never_writes(self, hass, writes) -> None:
        _add(hass, _independent_zone("a"), _zone("b", absence_flag="false"), _system())
        assert await _start(hass)
        writes.clear()
        first = [_gate(hass) for _ in range(3)]
        assert first[0] == first[1] == first[2] and writes == []

    async def test_the_assessment_alone_never_removes_the_system_entry(self, hass, writes) -> None:
        _add(hass, _independent_zone("a"), _independent_zone("b"), _system())
        assert await _start(hass)
        assert _gate(hass)["eligible"] is True
        await hass.async_block_till_done()
        _system_untouched(hass)
        assert not [w for w in writes if w[0] == "system"]
        assert len(hass.config_entries.async_entries(DOMAIN)) == 3


# --- migrated zones that depend on the System again ---------------------------------------------

class TestRecoveryOfReInheritedZones:
    @pytest.mark.parametrize("reinherit", [
        {"override_policy": {**_OWN_POLICY, "use_system_default": True}},
        {"absence_flag": True},
        {"override_policy": {k: v for k, v in _OWN_POLICY.items() if k != "use_system_default"}},
        {"override_policy": {k: v for k, v in _OWN_POLICY.items() if k != "release_strategy"}},
    ])
    @pytest.mark.parametrize("disabled", [False, True])
    async def test_a_version_two_zone_with_returned_inheritance_is_recovered_at_startup(
            self, hass, writes, reinherit, disabled) -> None:
        kw = {"override_policy": copy.deepcopy(_OWN_POLICY), "absence_delay": 20, "absence_flag": False}
        kw.update(reinherit)
        zone = _zone("a", minor=2, disabled=disabled, **kw)
        _add(hass, zone, _system())
        want = (resolve_zone_override_policy(dict(zone.data), _SYSTEM_DATA),
                resolve_zone_absence_delay_min(dict(zone.data), _SYSTEM_DATA))
        assert await _start(hass)
        assert zone.minor_version == 2 and sr.zone_independence_reasons(zone.data, 2, _SYSTEM_DATA) == ()
        assert (resolve_zone_override_policy(dict(zone.data), None),
                resolve_zone_absence_delay_min(dict(zone.data), None)) == want
        assert _gate(hass)["eligible"] is True
        assert [w for w in writes if w[0] == "a"] == [("a", ["data", "minor_version"])]
        _system_untouched(hass)

    async def test_the_recovery_does_not_change_the_effective_settings_the_zone_runs_with(self, hass) -> None:
        zone = _zone("a", minor=2, override_policy={**_OWN_POLICY, "use_system_default": True},
                     absence_delay=20, absence_flag=True)
        _add(hass, zone, _system())
        assert await _start(hass)
        eff = _effective(zone)
        assert eff["strategy"] == "duration" and eff["duration"] == 45 and eff["absence"] == 90
        assert eff["tolerance"] == 12                              # the zone's own tolerance

    async def test_a_returned_dependency_without_a_system_entry_stays_closed_and_runs_as_before(self, hass) -> None:
        zone = _zone("a", minor=2, override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20,
                     absence_flag=True)
        _add(hass, zone)
        before = copy.deepcopy(dict(zone.data))
        assert await _start(hass)
        assert dict(zone.data) == before                           # nothing can be judged: untouched
        assert _gate(hass)["blocked"] == {"a": [sr.ABSENCE_INHERITANCE_ACTIVE]}
        assert zone.state is ConfigEntryState.LOADED

    async def test_an_unverifiable_version_two_zone_is_left_alone_and_blocks(self, hass, writes) -> None:
        zone = _zone("a", minor=2, override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20,
                     absence_flag="false")
        _add(hass, zone, _system())
        before = copy.deepcopy(dict(zone.data))
        assert await _start(hass)
        assert dict(zone.data) == before and [w for w in writes if w[0] == "a"] == []
        assert _gate(hass)["blocked"] == {"a": [sr.ABSENCE_FLAG_INVALID]}
        assert zone.state is ConfigEntryState.LOADED

    async def test_a_failing_recovery_leaves_the_zone_unchanged_and_a_retry_succeeds(
            self, hass, monkeypatch) -> None:
        zone = _zone("a", minor=2, override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20,
                     absence_flag=True)
        other = _independent_zone("other")
        _add(hass, zone, other, _system())
        before = copy.deepcopy(dict(zone.data))
        original = hass.config_entries.async_update_entry
        calls = {"n": 0}

        def flaky(entry, **kwargs):
            if entry.entry_id == "a":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("storage unavailable")
            return original(entry, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_update_entry", flaky)
        assert await _start(hass)
        assert dict(zone.data) == before                       # untouched ...
        assert _gate(hass)["blocked"] == {"a": [sr.ABSENCE_INHERITANCE_ACTIVE]}   # ... and still blocking
        assert zone.state is ConfigEntryState.LOADED and other.state is ConfigEntryState.LOADED
        ss._prepare_zone_entries(hass)                         # the next pass (restart) retries
        assert _gate(hass)["eligible"] is True and calls["n"] == 2

    async def test_a_failed_write_and_a_failed_rollback_keep_the_gate_closed_and_are_reported(
            self, hass, monkeypatch, caplog) -> None:
        zone = _zone("a", minor=2, override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20,
                     absence_flag=True)
        _add(hass, zone, _system())
        original = hass.config_entries.async_update_entry

        def broken(entry, **kwargs):
            if entry.entry_id == "a":
                original(entry, **{**kwargs, "data": {**kwargs["data"], "absence_delay_use_system_default": True}})
                raise RuntimeError("listener failed")
            return original(entry, **kwargs)

        monkeypatch.setattr(hass.config_entries, "async_update_entry", broken)
        with caplog.at_level(logging.WARNING):
            assert await _start(hass)
        assert _gate(hass)["eligible"] is False
        assert zone.state is ConfigEntryState.LOADED
        assert "was not prepared" in caplog.text

    async def test_a_second_pass_and_a_reload_after_the_recovery_write_nothing(self, hass, writes) -> None:
        zone = _zone("a", minor=2, override_policy=copy.deepcopy(_OWN_POLICY), absence_delay=20,
                     absence_flag=True)
        _add(hass, zone, _system())
        assert await _start(hass)
        stored = copy.deepcopy(dict(zone.data))
        writes.clear()
        ss._prepare_zone_entries(hass)
        assert await hass.config_entries.async_reload("a")
        await hass.async_block_till_done()
        assert writes == [] and dict(zone.data) == stored


# --- the Options Flow can no longer bring the dependency back ---------------------------------------

class TestOptionsFlowCannotReInherit:
    async def test_the_override_gate_of_a_migrated_zone_has_no_system_default_switch(self, hass) -> None:
        _add(hass, _independent_zone("a"), _system())
        assert await _start(hass)
        result = await _options(hass, "a", "advanced", "manual_override")
        assert _fields(result) == {"override_detection_tolerance"}

    async def test_the_presence_gate_of_a_migrated_zone_has_no_system_default_switch(self, hass) -> None:
        _add(hass, _independent_zone("a"), _system())
        assert await _start(hass)
        result = await _options(hass, "a", "presence")
        assert "absence_delay_use_system_default" not in _fields(result)
        assert {"presence_entity_ids", "presence_policy"} <= _fields(result)

    async def test_an_unmigrated_zone_keeps_the_legacy_switches(self, hass) -> None:
        zone = _zone("a", absence_flag="false")                  # stays unmigrated (unverifiable)
        _add(hass, zone, _system())
        assert await _start(hass) and zone.minor_version == 1
        assert "override_use_system_default" in _fields(await _options(hass, "a", "advanced", "manual_override"))
        assert "absence_delay_use_system_default" in _fields(await _options(hass, "a", "presence"))

    async def test_a_forged_system_default_answer_is_rejected_by_the_form(self, hass) -> None:
        zone = _independent_zone("a")
        _add(hass, zone, _system())
        assert await _start(hass)
        before = copy.deepcopy(dict(zone.data))
        result = await _options(hass, "a", "advanced", "manual_override")
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {"override_detection_tolerance": 5, "override_use_system_default": True})
        result = await _options(hass, "a", "presence")
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(result["flow_id"], {
                "presence_entity_ids": [], "presence_policy": "any_home",
                "absence_delay_use_system_default": True})
        assert dict(zone.data) == before

    @pytest.mark.parametrize("updates", [
        {"absence_delay_use_system_default": True},
        {"override_policy": {"use_system_default": True}},
    ])
    async def test_the_save_helper_refuses_to_write_inheritance_into_a_migrated_zone(self, hass, updates) -> None:
        zone = _independent_zone("a")
        _add(hass, zone, _system())
        assert await _start(hass)
        before = copy.deepcopy(dict(zone.data))
        await hass.config_entries.options.async_init("a")
        flow = next(iter(hass.config_entries.options._progress.values()))
        with pytest.raises(ValueError):
            flow._save_and_reload(updates)
        assert dict(zone.data) == before and zone.minor_version == 2

    async def test_the_save_helper_still_accepts_inheritance_for_a_zone_that_is_not_migrated(self, hass) -> None:
        zone = _zone("a", absence_flag="false")
        _add(hass, zone, _system())
        assert await _start(hass) and zone.minor_version == 1
        await hass.config_entries.options.async_init("a")
        flow = next(iter(hass.config_entries.options._progress.values()))
        flow._save_and_reload({"absence_delay_use_system_default": True})   # accepted, no refusal
        await hass.async_block_till_done()
        # the reload that follows migrates the now valid legacy zone: it keeps the System value
        # it just opted into, written explicitly and no longer inherited
        assert zone.minor_version == 2 and zone.data["absence_delay_use_system_default"] is False
        assert zone.data["absence_delay_min"] == 90 and _gate(hass)["eligible"] is True

    async def test_saving_the_override_options_keeps_the_zone_independent_across_reload_and_restart(
            self, hass, writes) -> None:
        zone = _independent_zone("a")
        _add(hass, zone, _system())
        assert await _start(hass)
        await _save_own_override(hass, "a", tolerance=15)
        assert zone.data["override_policy"]["use_system_default"] is False
        assert zone.data["override_policy"]["detection_tolerance"] == 15
        assert zone.data["absence_delay_min"] == 20 and zone.minor_version == 2
        assert _gate(hass)["eligible"] is True
        stored = copy.deepcopy(dict(zone.data))
        writes.clear()
        for _ in range(2):                                        # reload ...
            assert await hass.config_entries.async_reload("a")
            await hass.async_block_till_done()
        ss._prepare_zone_entries(hass)                            # ... and what a restart runs
        assert dict(zone.data) == stored and writes == [] and _gate(hass)["eligible"] is True
        assert _effective(zone)["tolerance"] == 15 and _effective(zone)["strategy"] == "lifecycle"

    async def test_saving_the_presence_options_keeps_the_zone_independent_and_its_other_settings(self, hass) -> None:
        zone = _independent_zone("a")
        _add(hass, zone, _system())
        assert await _start(hass)
        own_policy = copy.deepcopy(zone.data["override_policy"])
        await _save_presence(hass, "a", delay=40)
        assert zone.data["absence_delay_min"] == 40
        assert zone.data["absence_delay_use_system_default"] is False
        assert zone.data["override_policy"] == own_policy         # unrelated explicit settings
        assert _gate(hass)["eligible"] is True and _effective(zone)["absence"] == 40

    async def test_an_unrelated_options_save_leaves_the_explicit_settings_unchanged(self, hass) -> None:
        zone = _independent_zone("a")
        _add(hass, zone, _system())
        assert await _start(hass)
        before = {k: copy.deepcopy(zone.data[k]) for k in ("override_policy", "absence_delay_min",
                                                           "absence_delay_use_system_default")}
        result = await _options(hass, "a", "comfort")
        assert result["type"] is FlowResultType.FORM
        await hass.config_entries.options.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
        assert {k: zone.data[k] for k in before} == before and _gate(hass)["eligible"] is True

    @pytest.mark.parametrize("minor", [1, 2])
    async def test_adding_a_structure_keeps_the_stored_inheritance_state(self, hass, monkeypatch, minor) -> None:
        """The typed save used to turn a deferring zone into one with different settings."""
        legacy = minor == 1
        zone = _zone("a", minor=minor,
                     override_policy={"use_system_default": True, "release_strategy": "manual"} if legacy
                     else copy.deepcopy(_OWN_POLICY),
                     absence_flag=True if legacy else False, absence_delay=20)
        _add(hass, zone, _system())
        monkeypatch.setattr(ss, "_migrate_zone_entry", lambda hass_, entry: False)  # keep v1 as it is
        assert await _start(hass)
        before_policy = copy.deepcopy(zone.data["override_policy"])
        want = (resolve_zone_override_policy(dict(zone.data), _SYSTEM_DATA),
                resolve_zone_absence_delay_min(dict(zone.data), _SYSTEM_DATA))
        await hass.config_entries.options.async_init("a")
        flow = next(iter(hass.config_entries.options._progress.values()))
        flow._add_windows, flow._add_cover_groups = [], []
        flow._save_structure_and_reload()
        await hass.async_block_till_done()
        assert zone.data["override_policy"] == before_policy
        assert (resolve_zone_override_policy(dict(zone.data), _SYSTEM_DATA),
                resolve_zone_absence_delay_min(dict(zone.data), _SYSTEM_DATA)) == want

    async def test_adding_a_structure_to_a_zone_without_a_stored_policy_keeps_it_absent(
            self, hass, monkeypatch) -> None:
        zone = _zone("a", override_policy=None, absence_flag=True)
        _add(hass, zone, _system())
        monkeypatch.setattr(ss, "_migrate_zone_entry", lambda hass_, entry: False)
        assert await _start(hass)
        await hass.config_entries.options.async_init("a")
        flow = next(iter(hass.config_entries.options._progress.values()))
        flow._add_windows, flow._add_cover_groups = [], []
        flow._save_structure_and_reload()
        await hass.async_block_till_done()
        assert "override_policy" not in zone.data
        assert resolve_zone_override_policy(dict(zone.data), _SYSTEM_DATA).duration_min == 45


# --- Tier-1 and the shared runtime are untouched ------------------------------------------------------

class TestSharedRuntimeAndSystemEntry:
    async def test_dispatch_services_and_diagnostics_survive_with_a_blocked_zone(self, hass) -> None:
        a, bad = _independent_zone("a"), _zone("bad", absence_flag="false")
        _add(hass, a, bad, _system())
        assert await _start(hass)
        dispatch = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]
        assert a.runtime_data.coordinator._serial_dispatch is dispatch
        assert bad.runtime_data.coordinator._serial_dispatch is dispatch
        assert hass.services.has_service(DOMAIN, "clear_manual_override")
        from custom_components.smartshading.diagnostics import async_get_config_entry_diagnostics
        diag = await async_get_config_entry_diagnostics(hass, a)
        assert isinstance(diag, dict) and diag

    async def test_the_system_entry_is_never_changed_unloaded_or_removed_by_any_of_it(self, hass, writes) -> None:
        _add(hass, _zone("a", override_policy=None, absence_flag=True),
             _independent_zone("b"), _zone("c", absence_flag="false"), _system())
        assert await _start(hass)
        await _save_presence(hass, "b", delay=25)
        for _ in range(2):
            assert await hass.config_entries.async_reload("a")
            await hass.async_block_till_done()
        _system_untouched(hass)
        assert not [w for w in writes if w[0] == "system"]

    async def test_a_restart_with_a_partial_migration_finishes_it(self, hass) -> None:
        done, todo = _independent_zone("done"), _zone("todo", override_policy=None, absence_flag=True)
        _add(hass, done, todo, _system())
        assert await _start(hass)
        assert _gate(hass)["eligible"] is True
        assert _effective(todo) == _SYSTEM_EFFECTIVE
