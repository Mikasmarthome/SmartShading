"""A4 -- the same cover in two SmartShading config entries, in real Home Assistant.

Real config entries, real coordinators, real service registry and the real
Repairs (issue) registry.  Zone cover services do NOT write the new position
back, so a second controller of the same cover cannot hide behind "already at
target": every command that is sent is visible.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import (
    CONF_ENTRY_TYPE,
    DATA_COVER_OWNERSHIP,
    DOMAIN,
    ENTRY_TYPE_ZONE,
)
from custom_components.smartshading.models.config import ShadePositionDefaults
from custom_components.smartshading.models.cover_group import CoverGroup, CoverHardwareType
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig

_NOON = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
_SHARED = "cover.a4_shared"
_RAIN = "binary_sensor.a4_rain"
_ISSUE = ("smartshading", "duplicate_cover_cover_a4_shared")


def _entry(entry_id, *, shade=100, hardware=CoverHardwareType.AWNING, cover=_SHARED, rain=_RAIN):
    zone = ZoneConfig(
        id=f"z_{entry_id}", name=f"Zone {entry_id}",
        execution=ZoneExecutionConfig(learning_enabled=False, active_control_enabled=True),
    )
    window = WindowConfig(id=f"w_{entry_id}", name=f"Window {entry_id}", zone_id=zone.id,
                          azimuth=180.0, floor_level=0, cover_group_id=f"cg_{entry_id}")
    group = CoverGroup(id=f"cg_{entry_id}", window_id=window.id, cover_ids=[cover], hardware_type=hardware)
    data = SmartShadingConfigEntryData(
        name=f"A4 {entry_id}", use_home_location=False, zones=[zone], windows=[window],
        cover_groups=[group], rain_sensor_id=rain,
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=shade, normal_shade_position=shade, strong_shade_position=shade),
    )
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
        entry_id=entry_id, title=f"Zone {entry_id}")


class _Recorder:
    def __init__(self, hass):
        self.hass = hass
        self.calls: list[tuple[str, int]] = []

    def install(self):
        async def _set_position(call):
            self.calls.append((call.data["entity_id"], call.data["position"]))   # no state write-back

        async def _noop(call):
            return None

        self.hass.services.async_register("cover", "set_cover_position", _set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", _noop)

    def for_cover(self, cover=_SHARED):
        return [p for e, p in self.calls if e == cover]


async def _boot(hass, entries, *, order=None):
    await hass.config.async_set_time_zone("UTC")
    hass.states.async_set("sun.sun", "above_horizon", {
        "azimuth": 0.0, "elevation": 30.0,
        "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00"})
    hass.states.async_set(_RAIN, "off")
    hass.states.async_set(_SHARED, "open", {"current_position": 0})
    rec = _Recorder(hass)
    rec.install()
    # Home Assistant sets up every entry of the domain together, in the order the
    # entries were added - so that order is what a test varies.
    for e in (order or entries):
        e.add_to_hass(hass)
    assert await hass.config_entries.async_setup((order or entries)[0].entry_id)
    await hass.async_block_till_done()
    return rec


async def _warm_up(hass, entries):
    for e in entries:
        await e.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def _sun_in_sector(hass, frozen):
    frozen.move_to(_NOON + timedelta(minutes=30))
    hass.states.async_set("sun.sun", "above_horizon", {
        "azimuth": 180.0, "elevation": 60.0,
        "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00"})
    hass.states.async_set(_SHARED, "open", {"current_position": 0})


def _issues(hass):
    return {k for k in ir.async_get(hass).issues if k[0] == "smartshading"}


class TestDuplicateCoverInRealHomeAssistant:
    async def test_identical_duplicate_is_commanded_once_not_twice(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            entries = [_entry("e1"), _entry("e2")]
            rec = await _boot(hass, entries)
            await _warm_up(hass, entries)
            rec.calls.clear()
            await _sun_in_sector(hass, frozen)
            for e in entries:
                await e.runtime_data.coordinator.async_refresh()
            await hass.async_block_till_done()
        assert rec.for_cover() == [100], "one owner sends; the identical duplicate must not"

    async def test_conflicting_targets_send_nothing(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            entries = [_entry("e1", shade=100), _entry("e2", shade=40)]
            rec = await _boot(hass, entries)
            await _warm_up(hass, entries)
            rec.calls.clear()
            await _sun_in_sector(hass, frozen)
            for e in entries:
                await e.runtime_data.coordinator.async_refresh()
            await hass.async_block_till_done()
            assert rec.for_cover() == [], "two controllers with different targets must not move the cover"
            assert _ISSUE in _issues(hass)

    async def test_rain_is_sent_exactly_once_by_one_sender(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            entries = [_entry("e1", shade=100), _entry("e2", shade=40)]
            rec = await _boot(hass, entries)
            hass.states.async_set(_SHARED, "open", {"current_position": 100})
            await _warm_up(hass, entries)
            rec.calls.clear()
            hass.states.async_set(_RAIN, "on")               # the rain listeners alone react
            await hass.async_block_till_done()
        assert rec.for_cover() == [0], "exactly one sender retracts; the other party stays silent"
        assert len(rec.calls) == 1                              # nothing else was sent

    async def test_ownership_is_independent_of_setup_order(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            entries = [_entry("e1", shade=100), _entry("e2", shade=40)]
            await _boot(hass, entries, order=list(reversed(entries)))
            reg = hass.data[DOMAIN][DATA_COVER_OWNERSHIP]
            assert reg.role("e1", "w_e1", _SHARED) == "quarantined_safety_only"
            assert reg.role("e2", "w_e2", _SHARED) == "quarantined"

    async def test_repair_issue_is_unique_stable_and_closed_when_the_user_fixes_it(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            e1, e2 = _entry("e1", shade=100), _entry("e2", shade=40)
            await _boot(hass, [e1, e2])
            assert _issues(hass) == {_ISSUE}
            issue = ir.async_get(hass).async_get_issue(*_ISSUE)
            assert issue.translation_key == "duplicate_cover_conflict" and not issue.is_fixable
            # repeated reloads: still exactly one issue, one listener per entry
            for _ in range(2):
                assert await hass.config_entries.async_reload(e1.entry_id)
                await hass.async_block_till_done()
            assert _issues(hass) == {_ISSUE}
            reg = hass.data[DOMAIN][DATA_COVER_OWNERSHIP]
            assert reg.listener_count() == 2
            # the user moves e2 to another cover and reloads it
            fixed = {**e2.data}
            fixed["cover_groups"] = [{**g, "cover_ids": ["cover.a4_other"]} for g in fixed["cover_groups"]]
            hass.states.async_set("cover.a4_other", "open", {"current_position": 0})
            hass.config_entries.async_update_entry(e2, data=fixed)
            assert await hass.config_entries.async_reload(e2.entry_id)
            await hass.async_block_till_done()
            assert _issues(hass) == set()
            assert reg.role("e1", "w_e1", _SHARED) == "active"

    async def test_removing_an_entry_closes_the_issue_and_releases_the_other(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            e1, e2 = _entry("e1", shade=100), _entry("e2", shade=40)
            await _boot(hass, [e1, e2])
            assert _issues(hass) == {_ISSUE}
            await hass.config_entries.async_remove(e2.entry_id)
            await hass.async_block_till_done()
            assert _issues(hass) == set()
            reg = hass.data[DOMAIN][DATA_COVER_OWNERSHIP]
            assert reg.role("e1", "w_e1", _SHARED) == "active" and reg.listener_count() == 1

    async def test_unload_removes_the_listener_without_touching_the_issue(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            e1, e2 = _entry("e1", shade=100), _entry("e2", shade=40)
            await _boot(hass, [e1, e2])
            reg = hass.data[DOMAIN][DATA_COVER_OWNERSHIP]
            assert await hass.config_entries.async_unload(e2.entry_id)
            await hass.async_block_till_done()
            assert reg.listener_count() == 1
            assert _issues(hass) == {_ISSUE}                   # the conflict is a property of the config

    async def test_a_waiting_plan_of_the_other_zone_is_dropped_when_a_second_entry_appears(self, hass) -> None:
        """e1 is alone and has a normal plan parked in a completion wait; e2
        (same cover, other settings) is added: e1's plan must end and not resume."""
        from custom_components.smartshading import coordinator as coordinator_module
        from custom_components.smartshading.cover_control.dispatch_completion import (
            wait_for_travel_completion as real_wait,
        )

        async def long_sleep(_s):
            for _ in range(20000):
                await asyncio.sleep(0)

        async def long_wait(*args, **kwargs):
            kwargs["sleep"] = long_sleep
            return await real_wait(*args, **kwargs)

        with freeze_time(_NOON, real_asyncio=True) as frozen:
            e1 = _entry("e1", shade=50)
            rec = await _boot(hass, [e1])
            await _warm_up(hass, [e1])
            rec.calls.clear()
            coordinator_module_wait = coordinator_module.wait_for_travel_completion
            coordinator_module.wait_for_travel_completion = long_wait
            try:
                await _sun_in_sector(hass, frozen)
                plan = asyncio.ensure_future(e1.runtime_data.coordinator.async_refresh())
                for _ in range(600):
                    if rec.for_cover() or plan.done():
                        break
                    await asyncio.sleep(0)
                assert rec.for_cover() == [50] and not plan.done()
                e2 = _entry("e2", shade=20)
                e2.add_to_hass(hass)
                assert await hass.config_entries.async_setup(e2.entry_id)
                for _ in range(3000):
                    if plan.done():
                        break
                    await asyncio.sleep(0)
                assert plan.done(), "the running plan of e1 must end when its cover becomes ambiguous"
                await asyncio.wait_for(plan, timeout=5)
                await hass.async_block_till_done()
            finally:
                coordinator_module.wait_for_travel_completion = coordinator_module_wait
        assert rec.for_cover() == [50]                         # nothing was added to the old plan


class TestRecoveryAndRestart:
    async def test_concurrent_reload_of_both_entries_converges_without_duplicates(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            e1, e2 = _entry("e1", shade=100), _entry("e2", shade=40)
            await _boot(hass, [e1, e2])
            results = await asyncio.gather(
                hass.config_entries.async_reload(e1.entry_id),
                hass.config_entries.async_reload(e2.entry_id),
            )
            await hass.async_block_till_done()
            assert all(results)
            reg = hass.data[DOMAIN][DATA_COVER_OWNERSHIP]
            assert _issues(hass) == {_ISSUE}
            assert reg.listener_count() == 2 and reg.summary()["active_conflicts"] == 1
            assert reg.role("e1", "w_e1", _SHARED) == "quarantined_safety_only"

    async def test_restart_with_a_corrected_configuration_has_no_conflict_and_clears_a_stale_issue(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            # a repair left over from before the user corrected the configuration
            ir.async_create_issue(
                hass, "smartshading", _ISSUE[1], is_fixable=False, is_persistent=True,
                severity=ir.IssueSeverity.ERROR, translation_key="duplicate_cover_conflict",
                translation_placeholders={"cover": _SHARED, "locations": "x", "count": "2"})
            e1, e2 = _entry("e1"), _entry("e2", cover="cover.a4_other")
            hass.states.async_set("cover.a4_other", "open", {"current_position": 0})
            await _boot(hass, [e1, e2])
            assert _issues(hass) == set()
            assert hass.data[DOMAIN][DATA_COVER_OWNERSHIP].summary()["active_conflicts"] == 0

    async def test_three_parties_then_back_to_one(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            e1, e2, e3 = _entry("e1", shade=100), _entry("e2", shade=40), _entry("e3", shade=70)
            await _boot(hass, [e1, e2])
            reg = hass.data[DOMAIN][DATA_COVER_OWNERSHIP]
            e3.add_to_hass(hass)
            assert await hass.config_entries.async_setup(e3.entry_id)
            await hass.async_block_till_done()
            assert len(reg.result.covers[_SHARED].parties) == 3 and len(_issues(hass)) == 1
            await hass.config_entries.async_remove(e3.entry_id)
            await hass.config_entries.async_remove(e2.entry_id)
            await hass.async_block_till_done()
            assert _issues(hass) == set() and reg.role("e1", "w_e1", _SHARED) == "active"
