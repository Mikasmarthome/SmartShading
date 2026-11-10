"""The SmartShading System entry follows its zones (GitHub issue 5).

The System entry has the source "system", for which Home Assistant offers no
Remove button.  It used to stay behind after the last zone was deleted, and the
integration could not be uninstalled cleanly.  Real config entries, the real
config-entries API, flow manager, entity/device registries and service registry.

Run with the rest of the real-HA suite (Linux, macOS or WSL)::

    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
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

    async def test_zones_set_up_concurrently_create_one_system_entry(self, hass) -> None:
        for name in ("za", "zb", "zc"):
            hass.states.async_set(f"cover.test_{name}", "closed", {"current_position": 0})
        entries = [_zone(name) for name in ("za", "zb", "zc")]
        for entry in entries:
            entry.add_to_hass(hass)
        await asyncio.gather(*(hass.config_entries.async_setup(e.entry_id) for e in entries))
        await hass.async_block_till_done()
        assert len(_systems(hass)) == 1
        assert all(e.state is ConfigEntryState.LOADED for e in entries)


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
    async def test_an_orphaned_system_entry_is_cleaned_up_when_it_is_set_up(self, hass) -> None:
        orphan = MockConfigEntry(
            domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM}, source="system",
            entry_id="orphan", title="SmartShading System")
        orphan.add_to_hass(hass)
        assert await hass.config_entries.async_setup(orphan.entry_id)
        await hass.async_block_till_done()
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_a_system_entry_with_a_zone_is_not_an_orphan(self, hass) -> None:
        system = MockConfigEntry(
            domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM}, source="system",
            entry_id="system", title="SmartShading System")
        system.add_to_hass(hass)
        hass.states.async_set("cover.test_za", "closed", {"current_position": 0})
        zone = _zone("za")
        zone.add_to_hass(hass)
        assert await hass.config_entries.async_setup(system.entry_id)
        await hass.async_block_till_done()
        assert system.state is ConfigEntryState.LOADED
        assert len(_zones(hass)) == 1

    async def test_a_disabled_zone_still_counts_as_a_zone(self, hass) -> None:
        system = MockConfigEntry(
            domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM}, source="system",
            entry_id="system", title="SmartShading System")
        system.add_to_hass(hass)
        zone = _zone("za")
        zone.disabled_by = "user"
        zone.add_to_hass(hass)
        assert await hass.config_entries.async_setup(system.entry_id)
        await hass.async_block_till_done()
        assert system.state is ConfigEntryState.LOADED

    async def test_an_ignored_entry_is_not_a_zone(self, hass) -> None:
        orphan = MockConfigEntry(
            domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM}, source="system",
            entry_id="orphan", title="SmartShading System")
        orphan.add_to_hass(hass)
        ignored = MockConfigEntry(domain=DOMAIN, data={}, source="ignore", entry_id="ign")
        ignored.add_to_hass(hass)
        assert await hass.config_entries.async_setup(orphan.entry_id)
        await hass.async_block_till_done()
        assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == ["ign"]

    async def test_a_zone_added_before_the_orphan_removal_runs_keeps_the_system_entry(self, hass) -> None:
        """Temporarily empty during an in-progress setup is not an orphan: the removal
        re-checks when it finally runs."""
        system = MockConfigEntry(
            domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM}, source="system",
            entry_id="system", title="SmartShading System")
        system.add_to_hass(hass)
        hass.states.async_set("cover.test_za", "closed", {"current_position": 0})
        zone = _zone("za")
        # the zone is added (and set up) in the same loop turn the System entry finishes
        zone.add_to_hass(hass)
        await asyncio.gather(
            hass.config_entries.async_setup(system.entry_id),
            hass.config_entries.async_setup(zone.entry_id))
        await hass.async_block_till_done()
        assert [e.entry_id for e in _systems(hass)] == ["system"]
        assert system.state is ConfigEntryState.LOADED


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

    async def test_removal_leaves_no_listeners_tasks_or_services(self, hass) -> None:
        a = await _add_zone(hass, "za")
        coordinator = a.runtime_data.coordinator
        await _remove(hass, a.entry_id)
        assert coordinator._unsub_presence_listeners == []
        assert coordinator._unsub_contact_listeners == []
        diag = coordinator.lifecycle_diagnostics()
        assert diag["unloading"] is True
        assert all(count == 0 for count in diag["listeners_active"].values())
        assert diag["background_tasks"] == 0 and diag["running_cycles"] == 0
        assert not hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)
        ent_reg = er.async_get(hass)
        assert [e for e in ent_reg.entities.values() if e.platform == DOMAIN] == []


class TestOthersAreUntouched:
    async def test_unrelated_entries_and_registries_survive_the_complete_removal(self, hass) -> None:
        other = MockConfigEntry(domain="other_domain", data={"k": "v"}, entry_id="other")
        other.add_to_hass(hass)
        dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
        device = dev_reg.async_get_or_create(
            config_entry_id=other.entry_id, identifiers={("other_domain", "dev1")})
        entity = ent_reg.async_get_or_create(
            "sensor", "other_domain", "uid1", config_entry=other, device_id=device.id)
        hass.states.async_set("sensor.unrelated", "7")

        a = await _add_zone(hass, "za")
        await _remove(hass, a.entry_id)

        assert hass.config_entries.async_entries(DOMAIN) == []
        assert hass.config_entries.async_get_entry("other") is other
        assert other.data == {"k": "v"}
        assert dev_reg.async_get(device.id) is not None
        assert ent_reg.async_get(entity.entity_id) is not None
        assert hass.states.get("sensor.unrelated").state == "7"


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
