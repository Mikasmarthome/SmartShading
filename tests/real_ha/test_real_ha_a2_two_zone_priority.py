"""A2 -- global Safety priority across two zones, in real Home Assistant.

Zone A runs a normal (comfort) plan whose cover never reports completion, so
its plan sits in a completion wait.  Zone B then gets rain.  Safety of zone B
must be dispatched without waiting for zone A's wait, and zone A's wait must be
ended by the integration-wide Safety signal (not replayed).  Both entries share
the one GlobalSerialDispatch that lives in hass.data[DOMAIN].
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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
_A_COVER = "cover.a2_zone_a"
_B_COVER = "cover.a2_zone_b"
_RAIN_B = "binary_sensor.a2_rain_b"


def _entry(entry_id, zone_id, cover, hardware, *, rain=None) -> MockConfigEntry:
    zone = ZoneConfig(
        id=zone_id, name=zone_id,
        execution=ZoneExecutionConfig(learning_enabled=False, active_control_enabled=True),
    )
    window = WindowConfig(id=f"w_{zone_id}", name=f"W {zone_id}", zone_id=zone_id,
                          azimuth=180.0, floor_level=0, cover_group_id=f"cg_{zone_id}")
    group = CoverGroup(id=f"cg_{zone_id}", window_id=window.id, cover_ids=[cover],
                       hardware_type=hardware)
    data = SmartShadingConfigEntryData(
        name=f"A2 {zone_id}", use_home_location=False,
        zones=[zone], windows=[window], cover_groups=[group],
        rain_sensor_id=rain,
    )
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
        entry_id=entry_id,
    )


class _Recorder:
    """Real cover services.  Zone A's cover never reports its new position (so
    its completion wait cannot finish); zone B's cover does."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, int]] = []

    def install(self) -> None:
        async def _set_position(call) -> None:
            ent, pos = call.data["entity_id"], call.data["position"]
            self.calls.append((ent, pos))
            if ent == _B_COVER:
                self.hass.states.async_set(
                    ent, "open" if pos > 0 else "closed", {"current_position": pos})

        async def _noop(call) -> None:
            return None

        self.hass.services.async_register("cover", "set_cover_position", _set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", _noop)

    def for_cover(self, cover: str) -> list[int]:
        return [p for e, p in self.calls if e == cover]


async def test_zone_b_safety_is_not_blocked_by_zone_a_completion_wait(hass, monkeypatch) -> None:
    from custom_components.smartshading import coordinator as coordinator_module
    from custom_components.smartshading.cover_control.dispatch_completion import (
        wait_for_travel_completion as real_wait,
    )

    async def long_sleep(_seconds):
        # A long wait that is still a plain await point: the (frozen-clock safe)
        # stand-in for "the cover is travelling for many seconds".
        for _ in range(20000):
            await asyncio.sleep(0)

    async def long_wait(*args, **kwargs):
        kwargs["sleep"] = long_sleep
        return await real_wait(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "wait_for_travel_completion", long_wait)
    with freeze_time(_NOON, real_asyncio=True) as frozen:
        await hass.config.async_set_time_zone("UTC")
        # Sun outside zone A's sector while both zones warm up (nothing to dispatch).
        hass.states.async_set("sun.sun", "above_horizon", {
            "azimuth": 0.0, "elevation": 30.0,
            "next_rising": "2026-08-12T05:00:00+00:00",
            "next_setting": "2026-08-11T19:00:00+00:00",
        })
        hass.states.async_set(_RAIN_B, "off")
        hass.states.async_set(_A_COVER, "closed", {"current_position": 0})
        hass.states.async_set(_B_COVER, "open", {"current_position": 100})
        rec = _Recorder(hass)
        rec.install()

        entry_a = _entry("a2_a", "za", _A_COVER, CoverHardwareType.AWNING)
        entry_b = _entry("a2_b", "zb", _B_COVER, CoverHardwareType.AWNING, rain=_RAIN_B)
        for e in (entry_a, entry_b):
            e.add_to_hass(hass)
            assert await hass.config_entries.async_setup(e.entry_id)
            await hass.async_block_till_done()
        coord_a = entry_a.runtime_data.coordinator
        coord_b = entry_b.runtime_data.coordinator
        admission = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH].admission
        await coord_a.async_refresh()
        await coord_b.async_refresh()
        await hass.async_block_till_done()
        rec.calls.clear()
        # An INTERMEDIATE comfort target (HA 50) so the plan needs a real completion wait.
        coord_a.shade_position_defaults = ShadePositionDefaults(
            light_shade_position=50, normal_shade_position=50, strong_shade_position=50)
        hass.states.async_set(_A_COVER, "closed", {"current_position": 0})
        frozen.move_to(_NOON + timedelta(minutes=30))   # past the StateGuard action interval
        # The sun now stands in zone A's sector: its first real comfort dispatch.
        hass.states.async_set("sun.sun", "above_horizon", {
            "azimuth": 180.0, "elevation": 60.0,
            "next_rising": "2026-08-12T05:00:00+00:00",
            "next_setting": "2026-08-11T19:00:00+00:00",
        })

        # Zone A: a normal plan; its cover never completes -> completion wait.
        plan_a = asyncio.ensure_future(coord_a.async_refresh())
        for _ in range(400):
            if rec.for_cover(_A_COVER) or plan_a.done():
                break
            await asyncio.sleep(0)
        a_dispatched = bool(rec.for_cover(_A_COVER))

        hass.states.async_set(_RAIN_B, "on")
        await coord_b.async_refresh()
        for _ in range(400):
            if plan_a.done():
                break
            await asyncio.sleep(0)
        a_finished = plan_a.done()
        await asyncio.wait_for(plan_a, timeout=5)
        await hass.async_block_till_done()

    assert a_dispatched, "precondition: zone A's normal plan dispatched and is waiting"
    b_calls = rec.for_cover(_B_COVER)
    assert b_calls and set(b_calls) == {0}, "zone B Safety must retract the awning"
    assert a_finished, "zone A's completion wait must end when zone B's Safety enters"
    assert rec.for_cover(_A_COVER).count(rec.for_cover(_A_COVER)[0]) == len(rec.for_cover(_A_COVER))
    assert not admission.locked() and admission.queued_count == 0
    stats = admission.stats()
    assert stats["admitted_safety"] >= 1 and stats["queued"] == 0
