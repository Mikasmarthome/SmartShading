"""Entry lifecycle in real Home Assistant: setup, unload, reload, services.

Real config entries, coordinator and service registry (no stubs).  Covers what
the scenario files do not: the entry reaches ``loaded`` and unloads cleanly,
the domain service exists exactly while a zone entry is loaded, a reload builds
a fresh coordinator and leaves the old one without any runtime authority, and
repeated setup / reload / unload cycles never accumulate listeners or tasks.

Run it with the rest of the real-HA suite (Linux, macOS or WSL -- Home
Assistant's runner imports the POSIX-only ``fcntl``)::

    python -m pip install -r tests/real_ha/requirements-real-ha.txt
    python -m pytest tests/real_ha --confcutdir=tests/real_ha -q -rs

``--confcutdir`` keeps the stub-based ``tests/conftest.py`` out of this tree.
"""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading import services as svc
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE
from custom_components.smartshading.models.cover_group import CoverGroup
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig


def _zone_entry_data(suffix: str = "") -> dict:
    # `suffix` keeps zone/window/cover_group/cover ids unique across
    # multiple entries created in the same test -- reusing identical ids
    # across two real ConfigEntries causes real entity-registry unique_id
    # collisions (HA auto-suffixes conflicting entities with "_2"), which
    # is a real-world misconfiguration, not what these multi-entry tests
    # are meant to exercise.
    zone = ZoneConfig(id=f"zone1{suffix}", name="Living Room")
    window = WindowConfig(
        id=f"window1{suffix}", name="South Window", zone_id=f"zone1{suffix}",
        azimuth=180.0, floor_level=0, cover_group_id=f"cg1{suffix}",
    )
    cover_group = CoverGroup(
        id=f"cg1{suffix}", window_id=f"window1{suffix}",
        cover_ids=[f"cover.test_cover{suffix}"],
    )
    data = SmartShadingConfigEntryData(
        name="Test Zone", use_home_location=False,
        zones=[zone], windows=[window], cover_groups=[cover_group],
    )
    return to_storage_dict(data)


def _make_entry(entry_id: str, suffix: str = "") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={**_zone_entry_data(suffix), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
        entry_id=entry_id,
    )


class TestRealEntrySetupAndTeardown:
    async def test_real_config_entry_setup_succeeds(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_setup_1")
        entry.add_to_hass(hass)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state.value == "loaded"

    async def test_real_coordinator_and_first_refresh(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_setup_2")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data.coordinator
        assert coordinator is not None
        assert "window1" in coordinator.windows
        # The real DataUpdateCoordinator first refresh already ran inside
        # async_config_entry_first_refresh() during setup.
        assert coordinator.data is not None

    async def test_listeners_are_registered_after_setup(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_setup_3")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data.coordinator
        # No presence/contact entities configured in this minimal fixture,
        # so the lists exist (real attributes, not AttributeError) but are
        # legitimately empty -- proves the real setup path ran far enough
        # to reach listener registration, not that specific listeners fired.
        assert isinstance(coordinator._unsub_presence_listeners, list)
        assert isinstance(coordinator._unsub_contact_listeners, list)

    async def test_service_is_registered_after_setup(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_setup_4")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)

    async def test_real_unload_succeeds_and_removes_service_when_last_entry(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_unload_1")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state.value == "not_loaded"
        # Real listener cleanup: the lists the coordinator itself clears in
        # async_teardown_presence_listeners()/async_teardown_contact_listeners()
        # (called from async_unload_entry) are empty after unload.
        assert coordinator._unsub_presence_listeners == []
        assert coordinator._unsub_contact_listeners == []
        # Real service removal: no other zone entry remains loaded.
        assert not hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)

    async def test_service_survives_unload_while_another_zone_entry_still_loaded(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        hass.states.async_set("cover.test_coverB", "closed", {"current_position": 0})
        entry_a = _make_entry("entry_multi_a")
        entry_b = _make_entry("entry_multi_b", suffix="B")
        entry_a.add_to_hass(hass)
        entry_b.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()
        # Real, observed HA behavior (not a smartshading-specific quirk):
        # once the "smartshading" base component itself is set up (as a
        # side effect of entry_a's setup), HA's own bootstrap picks up and
        # auto-loads any other already-added, not-yet-loaded entry of the
        # same domain -- entry_b is frequently already ConfigEntryState.
        # LOADED by this point. Only call async_setup() explicitly if it
        # is not.
        if entry_b.state.value == "not_loaded":
            assert await hass.config_entries.async_setup(entry_b.entry_id)
            await hass.async_block_till_done()
        assert entry_b.state.value == "loaded"

        assert await hass.config_entries.async_unload(entry_a.entry_id)
        await hass.async_block_till_done()

        assert entry_a.state.value == "not_loaded"
        assert entry_b.state.value == "loaded"
        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)

    async def test_no_service_calls_succeed_after_full_unload(self, hass) -> None:
        from homeassistant.exceptions import ServiceNotFound

        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_unload_2")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        with pytest.raises(ServiceNotFound):
            await hass.services.async_call(
                DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE, {}, blocking=True,
            )


class TestRealReload:
    async def test_reload_creates_exactly_one_fresh_coordinator_instance(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_reload_1")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        first_coordinator = entry.runtime_data.coordinator

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state.value == "loaded"
        second_coordinator = entry.runtime_data.coordinator
        assert second_coordinator is not None
        assert second_coordinator is not first_coordinator
        assert "window1" in second_coordinator.windows

    async def test_old_coordinator_instance_has_no_active_runtime_authority_after_reload(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_reload_2")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        first_coordinator = entry.runtime_data.coordinator

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        # async_shutdown() was called on the OLD coordinator during unload
        # (see __init__.py's async_unload_entry) -- its listeners are torn
        # down and it is no longer the one entry.runtime_data references.
        assert entry.runtime_data.coordinator is not first_coordinator
        assert first_coordinator._unsub_presence_listeners == []
        assert first_coordinator._unsub_contact_listeners == []

    async def test_repeated_reload_does_not_duplicate_service_registration(self, hass) -> None:
        hass.states.async_set("cover.test_cover", "closed", {"current_position": 0})
        entry = _make_entry("entry_reload_3")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        for _ in range(3):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()

        assert entry.state.value == "loaded"
        assert hass.services.has_service(DOMAIN, svc.SERVICE_CLEAR_MANUAL_OVERRIDE)
        assert DOMAIN in hass.data


def _entry_with_all_listeners(entry_id: str) -> MockConfigEntry:
    zone = ZoneConfig(id="zoneL", name="Living Room")
    window = WindowConfig(
        id="windowL", name="South Window", zone_id="zoneL", azimuth=180.0, floor_level=0,
        cover_group_id="cgL", contact_sensor_entity_id="binary_sensor.contact_l",
    )
    cover_group = CoverGroup(id="cgL", window_id="windowL", cover_ids=["cover.test_coverL"])
    data = SmartShadingConfigEntryData(
        name="Listener Zone", use_home_location=False,
        zones=[zone], windows=[window], cover_groups=[cover_group],
        presence_entity_ids=["person.listener_l"],
        rain_sensor_id="binary_sensor.rain_l",
    )
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
        entry_id=entry_id,
    )


class TestSetupReloadUnloadSetupCycle:
    async def test_repeated_cycles_never_accumulate_listeners_or_tasks(self, hass) -> None:
        hass.states.async_set("cover.test_coverL", "closed", {"current_position": 0})
        hass.states.async_set("binary_sensor.contact_l", "off")
        hass.states.async_set("binary_sensor.rain_l", "off")
        hass.states.async_set("person.listener_l", "home")
        entry = _entry_with_all_listeners("entry_cycle")
        entry.add_to_hass(hass)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        baseline = entry.runtime_data.coordinator.lifecycle_diagnostics()["listeners_active"]
        assert baseline["presence"] == 1 and baseline["contact"] == 1 and baseline["rain"] == 1

        for _ in range(3):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            diag = entry.runtime_data.coordinator.lifecycle_diagnostics()
            assert diag["listeners_active"] == baseline
            assert diag["background_tasks"] == 0 and diag["running_cycles"] == 0

        old = entry.runtime_data.coordinator
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        gone = old.lifecycle_diagnostics()
        assert gone["unloading"] is True
        assert all(count == 0 for count in gone["listeners_active"].values())
        assert gone["background_tasks"] == 0 and gone["running_cycles"] == 0

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        again = entry.runtime_data.coordinator
        assert again is not old
        assert again.lifecycle_diagnostics()["listeners_active"] == baseline
