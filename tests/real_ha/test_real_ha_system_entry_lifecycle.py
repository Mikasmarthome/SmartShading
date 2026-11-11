"""The SmartShading System entry follows its zones (GitHub issue 5).

The System entry has the source "system", for which Home Assistant offers no
Remove button.  It used to stay behind after the last zone was deleted, and the
integration could not be uninstalled cleanly.  Real config entries, the real
config-entries API, flow manager, entity/device registries and service registry.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs

Scheduling.  Home Assistant guarantees neither which task runs first nor whether an
awaited call suspends (that depends on caches the process has or has not warmed), and
``ConfigEntries.async_setup`` returns the entry's state at the moment it returns.  So
these tests never rely on the first task of a ``gather`` being slower than the others,
never race an explicit ``async_setup`` against the implicit setup of the same entry,
and never assert the return value of ``async_setup`` for an entry the integration may
legitimately remove while it is set up.  Concurrency is created with the domain already
loaded (every entry then has its own setup lock) and judged by the settled outcome.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading import services as svc
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_SYSTEM,
    ENTRY_TYPE_ZONE,
)
from custom_components.smartshading.models.cover_group import CoverGroup
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig


def _zone(entry_id: str) -> MockConfigEntry:
    zone = ZoneConfig(id=f"zone_{entry_id}", name=f"Zone {entry_id}")
    window = WindowConfig(
        id=f"window_{entry_id}", name="South", zone_id=zone.id, azimuth=180.0, floor_level=0,
        cover_group_id=f"cg_{entry_id}")
    group = CoverGroup(id=f"cg_{entry_id}", window_id=window.id, cover_ids=[f"cover.test_{entry_id}"])
    data = SmartShadingConfigEntryData(
        name=f"Zone {entry_id}", use_home_location=False,
        zones=[zone], windows=[window], cover_groups=[group])
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
        entry_id=entry_id)


async def _add_zone(hass, entry_id: str) -> MockConfigEntry:
    hass.states.async_set(f"cover.test_{entry_id}", "closed", {"current_position": 0})
    entry = _zone(entry_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _systems(hass):
    return [e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM]


def _zones(hass):
    return [e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_ZONE]


async def _remove(hass, entry_id: str):
    result = await hass.config_entries.async_remove(entry_id)
    await hass.async_block_till_done()
    return result


def _system_entry(entry_id: str = "system") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM}, source="system",
        entry_id=entry_id, title="SmartShading System")


def _cover(hass, *names: str) -> None:
    for name in names:
        hass.states.async_set(f"cover.test_{name}", "closed", {"current_position": 0})


async def _load_domain(hass) -> None:
    """Set the domain up with no entry, so that every entry set up afterwards takes
    Home Assistant's per-entry path (its own setup lock) instead of being set up as a
    side effect of the first call's component setup."""
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert await async_setup_component(hass, DOMAIN, {})
    assert DOMAIN in hass.config.components


def _assert_released(coordinator) -> None:
    """A removed or unloaded coordinator holds no listener, task, cycle or timer."""
    assert coordinator._unsub_presence_listeners == []
    assert coordinator._unsub_contact_listeners == []
    diag = coordinator.lifecycle_diagnostics()
    assert diag["unloading"] is True
    assert all(count == 0 for count in diag["listeners_active"].values())
    assert diag["background_tasks"] == 0 and diag["running_cycles"] == 0


def _add_unrelated(hass):
    """An entry of another domain with a device, an entity and a state."""
    other = MockConfigEntry(domain="other_domain", data={"k": "v"}, entry_id="other")
    other.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other_domain", "dev1")})
    entity = ent_reg.async_get_or_create(
        "sensor", "other_domain", "uid1", config_entry=other, device_id=device.id)
    hass.states.async_set("sensor.unrelated", "7")
    return other, device, entity


def _assert_unrelated_untouched(hass, unrelated) -> None:
    other, device, entity = unrelated
    assert hass.config_entries.async_get_entry("other") is other
    assert other.data == {"k": "v"}
    assert dr.async_get(hass).async_get(device.id) is not None
    assert er.async_get(hass).async_get(entity.entity_id) is not None
    assert hass.states.get("sensor.unrelated").state == "7"


class TestCreation:
    async def test_the_first_zone_creates_exactly_one_system_entry(self, hass) -> None:
        await _add_zone(hass, "za")
        systems = _systems(hass)
        assert len(systems) == 1
        assert systems[0].source == "system"
        assert systems[0].state is ConfigEntryState.LOADED

    async def test_two_zones_still_have_exactly_one_system_entry(self, hass) -> None:
        await _add_zone(hass, "za")
        await _add_zone(hass, "zb")
        assert len(_systems(hass)) == 1 and len(_zones(hass)) == 2

    async def test_zones_started_with_home_assistant_create_one_system_entry(self, hass) -> None:
        """The complete-domain start-up: Home Assistant sets every stored entry up."""
        _cover(hass, "za", "zb", "zc")
        entries = [_zone(name) for name in ("za", "zb", "zc")]
        for entry in entries:
            entry.add_to_hass(hass)

        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

        assert len(_systems(hass)) == 1 and _systems(hass)[0].state is ConfigEntryState.LOADED
        assert sorted(e.entry_id for e in _zones(hass)) == ["za", "zb", "zc"]
        assert all(e.state is ConfigEntryState.LOADED for e in entries)

    async def test_zones_set_up_concurrently_create_one_system_entry(self, hass) -> None:
        """Three first zones race to create the System entry.  The domain is loaded
        first, so each explicit ``async_setup`` really runs on its own (own setup lock)
        and none of them is set up implicitly by another."""
        _cover(hass, "za", "zb", "zc")
        await _load_domain(hass)
        entries = [_zone(name) for name in ("za", "zb", "zc")]
        for entry in entries:
            entry.add_to_hass(hass)

        results = await asyncio.gather(*(hass.config_entries.async_setup(e.entry_id) for e in entries))
        await hass.async_block_till_done()

        assert results == [True, True, True]
        assert len(_systems(hass)) == 1, "the racing zones created exactly one System entry"
        assert _systems(hass)[0].state is ConfigEntryState.LOADED
        assert _systems(hass)[0].source == "system"
        assert sorted(e.entry_id for e in _zones(hass)) == ["za", "zb", "zc"]
        assert all(e.state is ConfigEntryState.LOADED for e in entries)

        # and a clean end: nothing of the three zones outlives their removal
        coordinators = [e.runtime_data.coordinator for e in entries]
        await asyncio.gather(*(hass.config_entries.async_remove(e.entry_id) for e in entries))
        await hass.async_block_till_done()
        assert hass.config_entries.async_entries(DOMAIN) == []
        for coordinator in coordinators:
            _assert_released(coordinator)


    async def test_system_flows_started_together_create_exactly_one_entry(self, hass) -> None:
        """The flow's own guard.  Zones normally find the System entry through
        ``_ensure_system_entry``; should several of them ask for it in the same moment,
        the flow itself lets exactly one of the requests create it."""
        await _load_domain(hass)
        _zone("za").add_to_hass(hass)           # a zone exists (not set up): no orphan removal

        results = await asyncio.gather(*(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": "system"})
            for _ in range(3)))
        await hass.async_block_till_done()

        types = [r["type"] for r in results]
        assert types.count(FlowResultType.CREATE_ENTRY) == 1, results
        assert types.count(FlowResultType.ABORT) == 2, results
        assert {r["reason"] for r in results if r["type"] is FlowResultType.ABORT} == {"system_already_exists"}
        assert len(_systems(hass)) == 1 and _systems(hass)[0].state is ConfigEntryState.LOADED


class TestRemoval:
    async def test_removing_one_of_two_zones_keeps_the_system_entry(self, hass) -> None:
        a = await _add_zone(hass, "za")
        await _add_zone(hass, "zb")
        system_id = _systems(hass)[0].entry_id

        await _remove(hass, a.entry_id)

        assert [e.entry_id for e in _systems(hass)] == [system_id]
        assert _systems(hass)[0].state is ConfigEntryState.LOADED
        assert [e.entry_id for e in _zones(hass)] == ["zb"]
        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)

    async def test_removing_the_final_zone_removes_the_system_entry(self, hass) -> None:
        a = await _add_zone(hass, "za")
        await _remove(hass, a.entry_id)
        assert hass.config_entries.async_entries(DOMAIN) == []
        assert not hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)

    async def test_removing_two_zones_in_turn_ends_with_nothing_left(self, hass) -> None:
        a = await _add_zone(hass, "za")
        b = await _add_zone(hass, "zb")
        await _remove(hass, a.entry_id)
        assert len(_systems(hass)) == 1
        await _remove(hass, b.entry_id)
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_removing_two_zones_at_the_same_time_ends_with_nothing_left(self, hass) -> None:
        a = await _add_zone(hass, "za")
        b = await _add_zone(hass, "zb")
        await asyncio.gather(
            hass.config_entries.async_remove(a.entry_id),
            hass.config_entries.async_remove(b.entry_id))
        await hass.async_block_till_done()
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_the_removed_entry_is_already_gone_when_the_hook_runs(self, hass) -> None:
        """Home Assistant deletes the entry from its list before async_remove_entry."""
        a = await _add_zone(hass, "za")
        seen = {}
        real = hass.config_entries.async_entries

        from custom_components.smartshading import _async_remove_orphaned_system_entry as hook

        async def spy(hass_, removed_entry_id=None):
            seen["listed"] = [e.entry_id for e in real(DOMAIN)]
            await hook(hass_, removed_entry_id)

        with patch("custom_components.smartshading._async_remove_orphaned_system_entry", spy):
            await _remove(hass, a.entry_id)
        assert "za" not in seen["listed"]


class TestRaceAndReinstall:
    async def test_adding_a_zone_while_the_last_one_is_removed_keeps_a_system_entry(self, hass) -> None:
        a = await _add_zone(hass, "za")
        hass.states.async_set("cover.test_zb", "closed", {"current_position": 0})
        b = _zone("zb")
        b.add_to_hass(hass)
        await asyncio.gather(
            hass.config_entries.async_remove(a.entry_id),
            hass.config_entries.async_setup(b.entry_id))
        await hass.async_block_till_done()
        assert [e.entry_id for e in _zones(hass)] == ["zb"]
        assert len(_systems(hass)) == 1
        assert _systems(hass)[0].state is ConfigEntryState.LOADED

    async def test_a_zone_added_while_the_system_entry_is_being_removed_gets_one_again(self, hass) -> None:
        """The zone found the System entry still present and did not recreate it."""
        a = await _add_zone(hass, "za")
        hass.states.async_set("cover.test_zb", "closed", {"current_position": 0})
        b = _zone("zb")
        real_remove = hass.config_entries.async_remove

        async def add_then_remove(entry_id):
            if entry_id != "za":                 # the System entry is still listed: a zone arrives now
                b.add_to_hass(hass)
                await hass.config_entries.async_setup(b.entry_id)
                assert len(_systems(hass)) == 1, "the new zone saw it and did not create another"
            return await real_remove(entry_id)

        with patch.object(hass.config_entries, "async_remove", add_then_remove):
            await real_remove(a.entry_id)
            await hass.async_block_till_done()
        await hass.async_block_till_done()
        assert [e.entry_id for e in _zones(hass)] == ["zb"]
        assert len(_systems(hass)) == 1

    async def test_reinstalling_after_complete_removal_works(self, hass) -> None:
        a = await _add_zone(hass, "za")
        await _remove(hass, a.entry_id)
        assert hass.config_entries.async_entries(DOMAIN) == []

        await _add_zone(hass, "zb")

        assert len(_systems(hass)) == 1 and _systems(hass)[0].state is ConfigEntryState.LOADED
        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)


class TestOrphans:
    # An orphan is removed by a task its own setup starts, so ``async_setup`` may return
    # True (the entry was loaded when it returned) or False (the removal had already
    # unloaded it): both are legitimate and the tests below do not look at the result.
    # What is guaranteed - and asserted - is the outcome once Home Assistant has settled.

    async def test_an_orphaned_system_entry_is_cleaned_up_when_it_is_set_up(self, hass) -> None:
        unrelated = _add_unrelated(hass)
        orphan = _system_entry("orphan")
        orphan.add_to_hass(hass)

        await hass.config_entries.async_setup(orphan.entry_id)
        await hass.async_block_till_done()

        assert hass.config_entries.async_entries(DOMAIN) == []
        assert orphan.state is ConfigEntryState.NOT_LOADED
        assert not hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)
        assert [e for e in er.async_get(hass).entities.values() if e.platform == DOMAIN] == []
        _assert_unrelated_untouched(hass, unrelated)

    async def test_an_orphan_from_an_older_version_is_cleaned_up_at_home_assistant_start(self, hass) -> None:
        """The stored System entry of a removed last zone, set up by the complete-domain start-up."""
        unrelated = _add_unrelated(hass)
        _system_entry("orphan").add_to_hass(hass)

        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

        assert hass.config_entries.async_entries(DOMAIN) == []
        _assert_unrelated_untouched(hass, unrelated)

    async def test_a_system_entry_with_a_zone_is_not_an_orphan(self, hass) -> None:
        system = _system_entry()
        system.add_to_hass(hass)
        _cover(hass, "za")
        zone = _zone("za")
        zone.add_to_hass(hass)
        assert await hass.config_entries.async_setup(system.entry_id)
        await hass.async_block_till_done()
        assert system.state is ConfigEntryState.LOADED
        assert len(_zones(hass)) == 1

    async def test_a_disabled_zone_still_counts_as_a_zone(self, hass) -> None:
        system = _system_entry()
        system.add_to_hass(hass)
        zone = _zone("za")
        zone.disabled_by = "user"
        zone.add_to_hass(hass)
        assert await hass.config_entries.async_setup(system.entry_id)
        await hass.async_block_till_done()
        assert system.state is ConfigEntryState.LOADED
        assert zone.state is ConfigEntryState.NOT_LOADED and zone.disabled_by is not None

    async def test_an_ignored_entry_is_not_a_zone(self, hass) -> None:
        unrelated = _add_unrelated(hass)
        orphan = _system_entry("orphan")
        orphan.add_to_hass(hass)
        ignored = MockConfigEntry(domain=DOMAIN, data={}, source="ignore", entry_id="ign")
        ignored.add_to_hass(hass)

        await hass.config_entries.async_setup(orphan.entry_id)
        await hass.async_block_till_done()

        # the System entry is gone although an entry of the domain remains: the ignored
        # one is no zone - and it is itself left exactly as it was
        assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == ["ign"]
        assert ignored.source == "ignore" and ignored.data == {}
        assert ignored.state is ConfigEntryState.NOT_LOADED
        _assert_unrelated_untouched(hass, unrelated)

    async def test_a_real_zone_protects_the_system_entry_next_to_an_ignored_entry(self, hass) -> None:
        unrelated = _add_unrelated(hass)
        system = _system_entry()
        system.add_to_hass(hass)
        ignored = MockConfigEntry(domain=DOMAIN, data={}, source="ignore", entry_id="ign")
        ignored.add_to_hass(hass)
        _cover(hass, "za")
        zone = _zone("za")
        zone.add_to_hass(hass)

        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

        assert sorted(e.entry_id for e in hass.config_entries.async_entries(DOMAIN)) == ["ign", "system", "za"]
        assert system.state is ConfigEntryState.LOADED and zone.state is ConfigEntryState.LOADED
        assert ignored.state is ConfigEntryState.NOT_LOADED
        _assert_unrelated_untouched(hass, unrelated)

    async def test_a_zone_set_up_together_with_its_system_entry_keeps_it(self, hass) -> None:
        """System entry and zone set up at the same time, each on its own (the domain is
        loaded first): there is a zone, so nothing is an orphan and nothing is removed."""
        _cover(hass, "za")
        await _load_domain(hass)
        system = _system_entry()
        system.add_to_hass(hass)
        zone = _zone("za")
        zone.add_to_hass(hass)

        results = await asyncio.gather(
            hass.config_entries.async_setup(system.entry_id),
            hass.config_entries.async_setup(zone.entry_id))
        await hass.async_block_till_done()

        assert results == [True, True]
        assert [e.entry_id for e in _systems(hass)] == ["system"], "the very same entry survives"
        assert system.state is ConfigEntryState.LOADED and zone.state is ConfigEntryState.LOADED

    async def test_a_zone_added_after_the_orphan_decision_still_ends_with_one_system_entry(self, hass) -> None:
        """The real race: the System entry is set up with no zone, so its removal task
        decides "orphan"; a zone is then added and set up while that removal still
        waits for the System entry's setup to finish.  The zone is injected inside the
        System entry's own setup (after the removal task exists, before its setup lock
        is released), which is deterministic and does not depend on task order.

        Whether the removal re-checks or removes and the integration heals does not
        matter to the user: what must hold once everything settled is one loaded
        System entry next to the loaded zone."""
        _cover(hass, "za")
        await _load_domain(hass)
        system = _system_entry()
        system.add_to_hass(hass)
        zone = _zone("za")
        seen = {}
        real_forward = hass.config_entries.async_forward_entry_setups

        async def forward_and_add_the_zone(entry, platforms):
            if "zones_when_decided" not in seen:             # only the first System entry's setup
                seen["zones_when_decided"] = len(
                    [e for e in hass.config_entries.async_entries(DOMAIN) if e is not entry])
                zone.add_to_hass(hass)
                seen["zone_set_up"] = await hass.config_entries.async_setup(zone.entry_id)
            return await real_forward(entry, platforms)

        with patch.object(hass.config_entries, "async_forward_entry_setups", forward_and_add_the_zone):
            await hass.config_entries.async_setup(system.entry_id)
            await hass.async_block_till_done()

        assert seen == {"zones_when_decided": 0, "zone_set_up": True}, "the race was really set up"
        assert [e.entry_id for e in _zones(hass)] == ["za"] and zone.state is ConfigEntryState.LOADED
        assert len(_systems(hass)) == 1, "exactly one System entry"
        assert _systems(hass)[0].state is ConfigEntryState.LOADED
        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)


class TestReloadAndCleanup:
    async def test_reloading_a_zone_keeps_the_system_entry_and_the_zone(self, hass) -> None:
        a = await _add_zone(hass, "za")
        system_id = _systems(hass)[0].entry_id
        old = a.runtime_data.coordinator

        assert await hass.config_entries.async_reload(a.entry_id)
        await hass.async_block_till_done()

        assert [e.entry_id for e in _systems(hass)] == [system_id]
        assert a.state is ConfigEntryState.LOADED
        assert a.runtime_data.coordinator is not old

    async def test_reloading_the_system_entry_with_a_zone_keeps_both(self, hass) -> None:
        a = await _add_zone(hass, "za")
        system = _systems(hass)[0]
        assert await hass.config_entries.async_reload(system.entry_id)
        await hass.async_block_till_done()
        assert system.state is ConfigEntryState.LOADED and a.state is ConfigEntryState.LOADED
        assert len(_systems(hass)) == 1

    async def test_unloading_the_last_zone_is_not_removing_it(self, hass) -> None:
        """Unload (disable, reload, shutdown) never counts as the zone being gone."""
        a = await _add_zone(hass, "za")
        coordinator = a.runtime_data.coordinator
        system_id = _systems(hass)[0].entry_id

        assert await hass.config_entries.async_unload(a.entry_id)
        await hass.async_block_till_done()

        assert a.state is ConfigEntryState.NOT_LOADED
        assert [e.entry_id for e in _zones(hass)] == ["za"]
        assert [e.entry_id for e in _systems(hass)] == [system_id]
        assert _systems(hass)[0].state is ConfigEntryState.LOADED
        _assert_released(coordinator)

    async def test_removal_leaves_no_listeners_tasks_or_services(self, hass) -> None:
        a = await _add_zone(hass, "za")
        coordinator = a.runtime_data.coordinator
        await _remove(hass, a.entry_id)
        _assert_released(coordinator)
        assert not hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)
        ent_reg = er.async_get(hass)
        assert [e for e in ent_reg.entities.values() if e.platform == DOMAIN] == []


class TestOthersAreUntouched:
    async def test_unrelated_entries_and_registries_survive_the_complete_removal(self, hass) -> None:
        unrelated = _add_unrelated(hass)

        a = await _add_zone(hass, "za")
        await _remove(hass, a.entry_id)

        assert hass.config_entries.async_entries(DOMAIN) == []
        _assert_unrelated_untouched(hass, unrelated)


class TestRecursionAndFailures:
    async def test_the_system_entry_hook_does_nothing_and_cannot_recreate_it(self, hass) -> None:
        a = await _add_zone(hass, "za")
        system = _systems(hass)[0]
        from custom_components.smartshading import async_remove_entry

        with patch("custom_components.smartshading._async_remove_orphaned_system_entry",
                   AsyncMock()) as hook, patch(
                "custom_components.smartshading._ensure_system_entry") as ensure:
            await async_remove_entry(hass, system)
        hook.assert_not_called()
        ensure.assert_not_called()
        assert a.state is ConfigEntryState.LOADED

    async def test_removing_the_system_entry_directly_leaves_the_zone_alone(self, hass) -> None:
        a = await _add_zone(hass, "za")
        await _remove(hass, _systems(hass)[0].entry_id)
        assert a.state is ConfigEntryState.LOADED
        assert [e.entry_id for e in _zones(hass)] == ["za"]
        assert _systems(hass) == []                  # nothing recreates it behind the caller's back

    async def test_removal_is_requested_exactly_once_for_the_last_zone(self, hass) -> None:
        a = await _add_zone(hass, "za")
        real = hass.config_entries.async_remove
        removed = []

        async def spy(entry_id):
            removed.append(entry_id)
            return await real(entry_id)

        with patch.object(hass.config_entries, "async_remove", spy):
            await real(a.entry_id)
            await hass.async_block_till_done()
        assert len(removed) == 1 and removed[0] != "za"

    async def test_a_failing_system_removal_does_not_break_the_zone_removal(self, hass, caplog) -> None:
        other = MockConfigEntry(domain="other_domain", data={"k": "v"}, entry_id="other")
        other.add_to_hass(hass)
        a = await _add_zone(hass, "za")
        real = hass.config_entries.async_remove

        async def failing(entry_id):
            if entry_id == "za":
                return await real(entry_id)
            raise RuntimeError("boom")

        with patch.object(hass.config_entries, "async_remove", failing):
            await real("za")
            await hass.async_block_till_done()

        assert _zones(hass) == []
        assert hass.config_entries.async_get_entry("other") is other
        assert "removing the orphaned System entry failed" in caplog.text
        # the stranded entry is picked up by the orphan cleanup the next time it is set up
        stranded = _systems(hass)[0]
        assert await hass.config_entries.async_reload(stranded.entry_id)
        await hass.async_block_till_done()
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_a_failing_storage_cleanup_still_removes_the_system_entry(self, hass) -> None:
        a = await _add_zone(hass, "za")
        with patch("custom_components.smartshading.engines.safety_hold_persistence."
                   "SafetyHoldStore.async_remove", AsyncMock(side_effect=OSError("disk"))):
            await _remove(hass, a.entry_id)
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_a_failing_removal_with_a_zone_left_never_touches_the_zone(self, hass) -> None:
        a = await _add_zone(hass, "za")
        b = await _add_zone(hass, "zb")
        with patch.object(hass.config_entries, "async_remove", AsyncMock(side_effect=RuntimeError)) as spy:
            # the hook must not even try while a zone remains
            from custom_components.smartshading import _async_remove_orphaned_system_entry
            await _async_remove_orphaned_system_entry(hass, removed_entry_id=a.entry_id)
        spy.assert_not_called()
        assert b.state is ConfigEntryState.LOADED and len(_systems(hass)) == 1
