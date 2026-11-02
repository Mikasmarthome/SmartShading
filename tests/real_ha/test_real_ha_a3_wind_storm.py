"""A3 -- Wind / Storm safety in real Home Assistant.

Real config entry (no wind flag anywhere: the default installation), real
coordinator, real state machine, real service registry.  The wind sensor is
changed through the state machine ONLY -- no manual refresh -- so the event
listener is what has to react.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import (
    CONF_ENTRY_TYPE,
    DATA_GLOBAL_DISPATCH,
    DOMAIN,
    ENTRY_TYPE_ZONE,
)
from custom_components.smartshading.models.config import ShadePositionDefaults
from custom_components.smartshading.models.cover_group import CoverGroup, CoverHardwareType
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig

_NOON = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def _entry(entry_id, zone_id, cover, wind, hardware=CoverHardwareType.AWNING) -> MockConfigEntry:
    zone = ZoneConfig(
        id=zone_id, name=zone_id,
        execution=ZoneExecutionConfig(learning_enabled=False, active_control_enabled=True),
    )
    window = WindowConfig(id=f"w_{zone_id}", name=f"W {zone_id}", zone_id=zone_id,
                          azimuth=180.0, floor_level=0, cover_group_id=f"cg_{zone_id}")
    group = CoverGroup(id=f"cg_{zone_id}", window_id=window.id, cover_ids=[cover],
                       hardware_type=hardware)
    data = SmartShadingConfigEntryData(
        name=f"A3 {zone_id}", use_home_location=False,
        zones=[zone], windows=[window], cover_groups=[group],
        wind_speed_sensor_id=wind,
    )
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
        entry_id=entry_id,
    )


class _Recorder:
    def __init__(self, hass: HomeAssistant, *, feedback: set[str]) -> None:
        self.hass = hass
        self.calls: list[tuple[str, int]] = []
        self.feedback = feedback

    def install(self) -> None:
        async def _set_position(call) -> None:
            ent, pos = call.data["entity_id"], call.data["position"]
            self.calls.append((ent, pos))
            if ent in self.feedback:
                self.hass.states.async_set(
                    ent, "open" if pos > 0 else "closed", {"current_position": pos})

        async def _noop(call) -> None:
            return None

        self.hass.services.async_register("cover", "set_cover_position", _set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", _noop)

    def for_cover(self, cover: str) -> list[int]:
        return [p for e, p in self.calls if e == cover]


def _sun(hass, azimuth=0.0, elevation=30.0) -> None:
    hass.states.async_set("sun.sun", "above_horizon", {
        "azimuth": azimuth, "elevation": elevation,
        "next_rising": "2026-08-12T05:00:00+00:00",
        "next_setting": "2026-08-11T19:00:00+00:00",
    })


def _wind(hass, value) -> None:
    hass.states.async_set("sensor.a3_wind", str(value), {"unit_of_measurement": "m/s"})


async def _setup(hass: HomeAssistant, entry_id="a3", cover="cover.a3_awning"):
    await hass.config.async_set_time_zone("UTC")
    _sun(hass)
    _wind(hass, 3)
    hass.states.async_set(cover, "open", {"current_position": 100})
    rec = _Recorder(hass, feedback={cover})
    rec.install()
    entry = _entry(entry_id, "z_a3", cover, "sensor.a3_wind")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    rec.calls.clear()
    return entry, coordinator, rec


class TestWindStormEventDriven:
    async def test_wind_acts_on_the_state_change_alone_in_a_default_installation(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass)
            _wind(hass, 16)                               # no manual refresh
            await hass.async_block_till_done()
        assert rec.for_cover("cover.a3_awning") == [0]
        assert coordinator._wind_listener_stats["band_changes"] == 1

    async def test_storm_acts_on_the_state_change_alone(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _, _, rec = await _setup(hass)
            _wind(hass, 25)
            await hass.async_block_till_done()
        assert rec.for_cover("cover.a3_awning") == [0]

    async def test_routine_changes_inside_a_band_cause_no_cycle(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass)
            for v in (4, 5, 7.5, 9, 13.9):
                _wind(hass, v)
                await hass.async_block_till_done()
        stats = coordinator._wind_listener_stats
        assert stats["events"] == 5 and stats["refreshes_requested"] == 0
        assert rec.calls == []

    async def test_sensor_loss_keeps_the_protection_and_never_extends(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            _, coordinator, rec = await _setup(hass)
            _wind(hass, 16)
            await hass.async_block_till_done()
            hass.states.async_set("sensor.a3_wind", "unavailable")
            await hass.async_block_till_done()
            for minutes in (15, 45):
                frozen.move_to(_NOON + timedelta(minutes=minutes))
                await coordinator.async_refresh()
                await hass.async_block_till_done()
            data = coordinator.data.execution_diagnostics["w_z_a3"]
            assert data.wind_hold_data_missing is True
        assert rec.for_cover("cover.a3_awning") and set(rec.for_cover("cover.a3_awning")) == {0}

    async def test_reload_leaves_exactly_one_listener_and_unload_none(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            entry, _, rec = await _setup(hass)
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data.coordinator
            assert coordinator._unsub_wind_listener is not None
            rec.calls.clear()
            _wind(hass, 16)
            await hass.async_block_till_done()
            assert coordinator._wind_listener_stats["events"] == 1      # not doubled
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            assert coordinator._unsub_wind_listener is None
            rec.calls.clear()
            _wind(hass, 25)
            await hass.async_block_till_done()
        assert rec.calls == []

    async def test_zone_b_wind_safety_ends_zone_a_completion_wait(self, hass, monkeypatch) -> None:
        from custom_components.smartshading import coordinator as coordinator_module
        from custom_components.smartshading.cover_control.dispatch_completion import (
            wait_for_travel_completion as real_wait,
        )

        async def long_sleep(_seconds):
            for _ in range(20000):
                await asyncio.sleep(0)

        async def long_wait(*args, **kwargs):
            kwargs["sleep"] = long_sleep
            return await real_wait(*args, **kwargs)

        monkeypatch.setattr(coordinator_module, "wait_for_travel_completion", long_wait)
        a_cover, b_cover = "cover.a3_zone_a", "cover.a3_zone_b"
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            await hass.config.async_set_time_zone("UTC")
            _sun(hass)                                     # outside zone A's sector while warming up
            hass.states.async_set("sensor.a3_wind_a", "3", {"unit_of_measurement": "m/s"})
            hass.states.async_set("sensor.a3_wind_b", "3", {"unit_of_measurement": "m/s"})
            hass.states.async_set(a_cover, "closed", {"current_position": 0})
            hass.states.async_set(b_cover, "open", {"current_position": 100})
            rec = _Recorder(hass, feedback={b_cover})      # zone A's cover never reports back
            rec.install()
            entry_a = _entry("a3_a", "za", a_cover, "sensor.a3_wind_a")
            entry_b = _entry("a3_b", "zb", b_cover, "sensor.a3_wind_b")
            for e in (entry_a, entry_b):
                e.add_to_hass(hass)
                assert await hass.config_entries.async_setup(e.entry_id)
                await hass.async_block_till_done()
            coord_a = entry_a.runtime_data.coordinator
            admission = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH].admission
            await coord_a.async_refresh()
            await entry_b.runtime_data.coordinator.async_refresh()
            await hass.async_block_till_done()
            rec.calls.clear()
            coord_a.shade_position_defaults = ShadePositionDefaults(
                light_shade_position=50, normal_shade_position=50, strong_shade_position=50)
            frozen.move_to(_NOON + timedelta(minutes=30))
            _sun(hass, azimuth=180.0, elevation=60.0)
            hass.states.async_set(a_cover, "closed", {"current_position": 0})

            plan_a = asyncio.ensure_future(coord_a.async_refresh())
            for _ in range(400):
                if rec.for_cover(a_cover) or plan_a.done():
                    break
                await asyncio.sleep(0)
            a_dispatched = bool(rec.for_cover(a_cover))

            hass.states.async_set("sensor.a3_wind_b", "16", {"unit_of_measurement": "m/s"})
            for _ in range(8000):                          # the listener alone drives zone B
                if plan_a.done():
                    break
                await asyncio.sleep(0)
            a_finished = plan_a.done()
            await asyncio.wait_for(plan_a, timeout=5)
            await hass.async_block_till_done()
        assert a_dispatched, "precondition: zone A's normal plan is in its completion wait"
        assert a_finished, "zone B's wind safety must end zone A's completion wait"
        assert rec.for_cover(b_cover) and set(rec.for_cover(b_cover)) == {0}
        assert not admission.locked() and admission.queued_count == 0
