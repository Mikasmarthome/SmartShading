"""PA-5b (real Home Assistant): non-destructive recovery from a malformed override policy.

A stored ``override_policy`` that is not a mapping (hand edited, restored from a damaged backup)
used to make the zone's Manual Override options crash, so the damage could not be repaired from
the UI.  Contract verified here with the real options flow and real config entries:

* a damaged zone is NOT run with an invented policy (it stays in SETUP_ERROR, fail closed);
* the Manual Override page opens, writes nothing by being opened or abandoned, never pre-selects
  an inheritance for data it cannot read, and an explicit save of own settings repairs the zone;
* unrelated data, entry ids and the other zones stay untouched; a damaged System policy is never
  rewritten, and the zones that defer to it can opt out one by one;
* once every dependent zone is independent the System entry is retired and the Repairs issues go.

Not covered: physical covers (none involved).  Run with the real-HA suite::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import copy
import importlib

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

# The helpers of the PA-5 migration matrix (same directory, same fixtures) are reused, not copied.
_matrix = importlib.import_module("test_real_ha_pa5_migration_matrix")
_OWN_EFFECTIVE = _matrix._OWN_EFFECTIVE
_SYSTEM_DATA = _matrix._SYSTEM_DATA
_Writes = _matrix._Writes
_add = _matrix._add
_core = _matrix._core
_effective = _matrix._effective
_independent = _matrix._independent
_issue_ids = _matrix._issue_ids
_legacy = _matrix._legacy
_open = _matrix._open
_own_override = _matrix._own_override
_restart = _matrix._restart
_start = _matrix._start
_system = _matrix._system
_systems = _matrix._systems
_zone = _matrix._zone

UNREADABLE = ["not a mapping", ["a", "list"], 7, True]
EMPTY = ["", [], 0, False]


def _default_of(result, field: str):
    for key in result["data_schema"].schema:
        if str(key) == field:
            return key.default() if callable(key.default) else key.default
    raise AssertionError(f"{field} not offered")


def _snapshot(entries) -> dict:
    return {e.entry_id: (copy.deepcopy(dict(e.data)), e.minor_version, e.title) for e in entries}


async def _abandon(hass, result) -> None:
    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Zone data corruption
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", UNREADABLE)
class TestUnreadableZonePolicy:
    async def test_the_page_opens_writes_nothing_and_preselects_no_inheritance(self, hass, monkeypatch, shape) -> None:
        broken = _zone("broken", override_policy=shape, absence_flag=False)
        healthy = _independent("healthy")
        system_entry = _system()
        _add(hass, system_entry, broken, healthy)
        assert await _start(hass)
        assert broken.state is ConfigEntryState.SETUP_ERROR          # fail closed: no invented policy runs
        assert healthy.state is ConfigEntryState.LOADED and _effective(healthy) == _OWN_EFFECTIVE
        before = _snapshot([broken, healthy, system_entry])
        writes = _Writes(hass, monkeypatch)

        result = await _open(hass, "broken", "advanced", "manual_override")
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual_override"
        assert _default_of(result, "override_use_system_default") is False
        await _abandon(hass, result)

        assert writes.calls == []
        assert _snapshot([broken, healthy, system_entry]) == before
        assert broken.state is ConfigEntryState.SETUP_ERROR

    async def test_an_explicit_save_repairs_the_zone_and_nothing_else_changes(self, hass, shape) -> None:
        broken = _zone("broken", override_policy=shape, absence_flag=False)
        healthy = _independent("healthy")
        system_entry = _system()
        _add(hass, system_entry, broken, healthy)
        assert await _start(hass)
        core_before = _core(broken.data)
        others_before = _snapshot([healthy, system_entry])

        await _own_override(hass, "broken")

        assert broken.state is ConfigEntryState.LOADED
        assert broken.data["override_policy"]["use_system_default"] is False
        assert broken.data["override_policy"]["detection_tolerance"] == 15
        assert broken.data["override_policy"]["release_strategy"] == "lifecycle"
        assert _core(broken.data) == core_before and broken.entry_id == "broken"
        assert _effective(broken)["tolerance"] == 15
        assert healthy.state is ConfigEntryState.LOADED and _effective(healthy) == _OWN_EFFECTIVE
        assert _snapshot([healthy]) == {k: v for k, v in others_before.items() if k == "healthy"}
        assert _systems(hass) == []                                      # the only dependent zone is repaired

    async def test_the_repair_lets_the_system_entry_retire_and_clears_the_issue(self, hass, shape) -> None:
        broken = _zone("broken", override_policy=shape, absence_flag=False)
        deferring = _legacy("deferring")
        off = _legacy("off", disabled=True)
        system_entry = _system()
        _add(hass, system_entry, broken, deferring, off)
        assert await _start(hass)
        assert [e.entry_id for e in _systems(hass)] == ["system"]    # kept: the broken zone is not verified
        assert "system_retirement_zone_broken" in _issue_ids(hass)
        assert deferring.minor_version == 2 and off.minor_version == 2

        await _own_override(hass, "broken")
        await hass.async_block_till_done()

        assert _systems(hass) == [] and _issue_ids(hass) == []
        assert broken.minor_version == 2 and broken.state is ConfigEntryState.LOADED
        assert {e.entry_id for e in hass.config_entries.async_entries("smartshading")} == {
            "broken", "deferring", "off"}
        await _restart(hass)
        assert _systems(hass) == [] and broken.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("shape", UNREADABLE)
async def test_a_window_can_still_be_added_to_a_zone_with_an_unreadable_policy_without_touching_it(
        hass, shape) -> None:
    broken = _zone("broken", override_policy=shape, absence_flag=False)
    _add(hass, _system(), broken)
    assert await _start(hass)
    windows_before = len(broken.data["windows"])
    # The structure save reads the whole entry through the typed model: the damaged value must not stop
    # it and must come out exactly as it went in.
    from custom_components.smartshading.config_flow import SmartShadingOptionsFlow
    handler = SmartShadingOptionsFlow(broken)
    handler.hass = hass
    handler._add_windows, handler._add_cover_groups = [], []  # noqa: SLF001
    handler._save_structure_and_reload()                       # noqa: SLF001
    await hass.async_block_till_done()
    assert broken.data["override_policy"] == shape
    assert len(broken.data["windows"]) == windows_before


# ---------------------------------------------------------------------------
# Shapes that read as "not configured" (they never crashed): still repairable, still not rewritten
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", EMPTY)
async def test_an_empty_policy_shape_keeps_its_behavior_and_the_page_still_opens(hass, monkeypatch, shape) -> None:
    zone = _zone("zone", override_policy=shape, absence_flag=False)
    system_entry = _system()
    _add(hass, system_entry, zone)
    assert await _start(hass)
    assert zone.state is ConfigEntryState.LOADED                      # reads as absent: defers, as before
    writes = _Writes(hass, monkeypatch)
    result = await _open(hass, "zone", "advanced", "manual_override")
    assert result["type"] is FlowResultType.FORM
    await _abandon(hass, result)
    assert writes.calls == []
    await _own_override(hass, "zone")
    assert zone.data["override_policy"]["use_system_default"] is False
    assert zone.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("shape", [None, {}])
async def test_an_absent_policy_is_migrated_as_before_and_the_page_opens(hass, monkeypatch, shape) -> None:
    zone = _zone("zone", override_policy=shape, absence_flag=False)
    _add(hass, _system(), zone)
    assert await _start(hass)
    assert zone.minor_version == 2 and zone.state is ConfigEntryState.LOADED
    writes = _Writes(hass, monkeypatch)
    result = await _open(hass, "zone", "advanced", "manual_override")
    assert result["type"] is FlowResultType.FORM
    await _abandon(hass, result)
    assert writes.calls == []


# ---------------------------------------------------------------------------
# No regression for valid data
# ---------------------------------------------------------------------------

async def test_valid_policies_behave_exactly_as_before(hass) -> None:
    own = _zone("own", override_policy={"release_strategy": "duration", "duration_min": 30,
                                        "use_system_default": False, "detection_tolerance": 12},
                absence_flag=False)
    defers = _legacy("defers")
    _add(hass, _system(), own, defers)
    assert await _start(hass)
    # (both zones were migrated at the start: they own their settings and are not offered inheritance)
    for entry_id in ("own", "defers"):
        result = await _open(hass, entry_id, "advanced", "manual_override")
        assert "override_use_system_default" not in {str(k) for k in result["data_schema"].schema}
        await _abandon(hass, result)
    assert [e.state for e in (own, defers)] == [ConfigEntryState.LOADED] * 2


async def test_an_unmigrated_zone_with_a_valid_deferring_policy_is_still_preselected_to_defer(hass) -> None:
    # A zone the start could not migrate (damaged System policy) but whose own policy is readable.
    defers = _legacy("defers")
    _add(hass, _system({**_SYSTEM_DATA, "system_override_policy": "garbage"}), defers)
    assert await _start(hass)
    assert defers.minor_version == 1
    result = await _open(hass, "defers", "advanced", "manual_override")
    assert _default_of(result, "override_use_system_default") is True
    await _abandon(hass, result)


# ---------------------------------------------------------------------------
# System data corruption: the zones recover one by one, the System data is never rewritten
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["garbage", ["x"], 3])
async def test_a_damaged_system_policy_is_recoverable_zone_by_zone_and_never_rewritten(hass, monkeypatch, shape) -> None:
    system_entry = _system({**copy.deepcopy(_SYSTEM_DATA), "system_override_policy": shape})
    a, b = _legacy("a"), _legacy("b")
    off = _legacy("off", disabled=True)
    healthy = _independent("healthy")
    _add(hass, system_entry, a, b, off, healthy)
    system_before = copy.deepcopy(dict(system_entry.data))
    assert await _start(hass)
    assert a.state is b.state is ConfigEntryState.SETUP_ERROR        # fail closed, nothing invented
    assert healthy.state is ConfigEntryState.LOADED and _effective(healthy) == _OWN_EFFECTIVE
    assert off.state is ConfigEntryState.NOT_LOADED

    writes = _Writes(hass, monkeypatch)
    result = await _open(hass, "a", "advanced", "manual_override")   # the page does not touch System data
    assert result["type"] is FlowResultType.FORM
    assert _default_of(result, "override_use_system_default") is True
    await _abandon(hass, result)
    assert writes.calls == []

    await _own_override(hass, "a")
    assert a.state is ConfigEntryState.LOADED                        # opted out: no System policy needed
    assert b.state is ConfigEntryState.SETUP_ERROR
    assert [e.entry_id for e in _systems(hass)] == ["system"]        # not retired: b and off still depend on it
    assert dict(system_entry.data) == system_before
    assert {"system_retirement_zone_b", "system_retirement_zone_off"} <= set(_issue_ids(hass))

    await _own_override(hass, "b")
    assert b.state is ConfigEntryState.LOADED
    assert [e.entry_id for e in _systems(hass)] == ["system"]        # the disabled zone still depends on it

    await _own_override(hass, "off")                                 # a disabled zone can be repaired too
    await _restart(hass)                                             # and is migrated by the next start
    assert off.minor_version == 2
    assert _systems(hass) == [] and _issue_ids(hass) == []
    assert all(e.state is ConfigEntryState.LOADED for e in (a, b, healthy))
    assert off.state is ConfigEntryState.NOT_LOADED and off.disabled_by is not None
