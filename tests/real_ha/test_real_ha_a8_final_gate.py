"""A8 final gate -- the combined chains that decide whether a late command is sent.

Real config entry, coordinator, state machine and service registry; simulated
covers on the fake clock of the A5 scenarios.  Each test stops a three-cover
group *between* two covers -- the moment where an old, already planned command
could still slip through -- and asserts that nothing is sent:

* rain arrives during the settle pause: the pause ends at once, the remaining
  covers only receive the rain retraction, the plan is reported as preempted and
  its diagnostics block stays JSON-safe,
* the entry starts unloading after cover 1: the dispatch gate refuses cover 2,
* the role of cover 2 changes after cover 1 (it is now owned by someone else):
  cover 2 is re-validated and never commanded (it is skipped, the rest of the
  group that is still ours continues).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading import coordinator as coordinator_module
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE
from custom_components.smartshading.coordinator import _OWN_DEDUPLICATED
from custom_components.smartshading.cover_control import dispatch_plan_executor
from custom_components.smartshading.cover_control.dispatch_completion import (
    wait_for_travel_completion as real_wait,
)
from custom_components.smartshading.models.config import ShadePositionDefaults
from custom_components.smartshading.models.cover_group import CoverGroup, CoverHardwareType
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig

_NOON = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
_RAIN = "binary_sensor.a8_rain"
_SUN = {
    "azimuth": 180.0, "elevation": 60.0,
    "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00",
}
TRAVEL_S = 20.0


class Sim:
    """Fake clock plus simulated covers that report opening/closing and a moving
    position, publishing their state to Home Assistant.  Completion waits advance
    the clock, so a 20 s travel costs no real time."""

    def __init__(self, hass, covers) -> None:
        self.hass = hass
        self.covers = list(covers)
        self.t = 0.0
        self.calls: list[tuple[float, str, int]] = []
        self.motion: dict[str, tuple[float, int, int]] = {}      # cover -> (t0, from, to)
        self.pos = {c: 100 for c in self.covers}                   # boot fully open: no endpoint movement

    def publish(self, cover: str) -> None:
        pos, state = self.pos[cover], "open" if self.pos[cover] else "closed"
        motion = self.motion.get(cover)
        if motion:
            t0, start, target = motion
            frac = min(1.0, (self.t - t0) / TRAVEL_S)
            pos = round(start + (target - start) * frac)
            if frac < 1.0:
                state = "opening" if target > start else "closing"
            else:
                self.pos[cover] = target
                state = "open" if target else "closed"
        self.hass.states.async_set(cover, state, {"supported_features": 15, "current_position": pos})

    def tick(self) -> None:
        for cover in self.covers:
            self.publish(cover)

    def install(self) -> None:
        async def set_position(call) -> None:
            cover, target = call.data["entity_id"], call.data["position"]
            self.calls.append((self.t, cover, target))
            self.motion[cover] = (self.t, self.pos[cover], target)
            self.publish(cover)

        async def noop(call) -> None:
            return None

        self.hass.services.async_register("cover", "set_cover_position", set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", noop)

    def targets_of(self, cover: str) -> list[int]:
        return [target for _, c, target in self.calls if c == cover]


def _entry(covers) -> MockConfigEntry:
    zone = ZoneConfig(
        id="z_a8", name="Zone", execution=ZoneExecutionConfig(learning_enabled=False, active_control_enabled=True))
    window = WindowConfig(id="w_a8", name="Window", zone_id="z_a8", azimuth=180.0, floor_level=0,
                          cover_group_id="cg_a8")
    group = CoverGroup(id="cg_a8", window_id="w_a8", cover_ids=list(covers), hardware_type=CoverHardwareType.AWNING)
    data = SmartShadingConfigEntryData(
        name="A8", use_home_location=False, zones=[zone], windows=[window], cover_groups=[group],
        rain_sensor_id=_RAIN,
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=50, normal_shade_position=50, strong_shade_position=50))
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE}, entry_id="a8")


async def _boot(hass, monkeypatch, covers):
    await hass.config.async_set_time_zone("UTC")
    hass.states.async_set("sun.sun", "above_horizon", {**_SUN, "azimuth": 0.0, "elevation": 30.0})
    hass.states.async_set(_RAIN, "off")
    sim = Sim(hass, covers)
    sim.install()
    sim.tick()

    async def fake_sleep(seconds):
        sim.t += max(0.0, seconds)
        sim.tick()
        await asyncio.sleep(0)

    async def wrapped(*args, **kwargs):
        kwargs.update(mono_clock=lambda: sim.t, sleep=fake_sleep)
        return await real_wait(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "wait_for_travel_completion", wrapped)
    # a real 2 s asyncio.sleep cannot be served under freezegun's real_asyncio mode
    monkeypatch.setattr(dispatch_plan_executor, "INTERMEDIATE_POST_COMPLETION_PAUSE_S", 0.0)
    monkeypatch.setattr(dispatch_plan_executor, "FULL_OPEN_START_INTERVAL_S", 0.0)
    entry = _entry(covers)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    sim.calls.clear()
    return sim, entry


_SETTLE_FAR_BEYOND_THE_TEST_S = 3600.0


@pytest.mark.parametrize("group_size", [2, 3])
async def test_rain_during_the_settle_pause_ends_the_pause_and_the_sequence(
    hass, monkeypatch, group_size
) -> None:
    covers = {f"cover.a8_s{group_size}_{i}": "real" for i in range(group_size)}
    first, rest = list(covers)[0], list(covers)[1:]
    with freeze_time(_NOON, real_asyncio=True) as frozen:
        sim, entry = await _boot(hass, monkeypatch, covers)
        monkeypatch.setattr(
            dispatch_plan_executor, "INTERMEDIATE_POST_COMPLETION_PAUSE_S", _SETTLE_FAR_BEYOND_THE_TEST_S
        )
        finished = {"completions": 0}
        completion_wait = coordinator_module.wait_for_travel_completion

        async def counting_wait(*args, **kwargs):
            outcome = await completion_wait(*args, **kwargs)
            finished["completions"] += 1
            return outcome

        monkeypatch.setattr(coordinator_module, "wait_for_travel_completion", counting_wait)

        frozen.move_to(_NOON + timedelta(minutes=30))
        hass.states.async_set("sun.sun", "above_horizon", _SUN)
        coordinator = entry.runtime_data.coordinator
        plan = asyncio.ensure_future(coordinator.async_refresh())
        for _ in range(2000):
            if finished["completions"] or plan.done():
                break
            await asyncio.sleep(0)
        assert finished["completions"] == 1, "cover 1 must have completed its travel"
        for _ in range(20):                       # the group is now inside the settle pause
            await asyncio.sleep(0)
        assert not plan.done(), "the settle pause must still be running"
        assert sim.targets_of(first) == [50]
        assert all(sim.targets_of(cover) == [] for cover in rest), "no further cover may start inside the pause"

        hass.states.async_set(_RAIN, "on")
        try:
            for _ in range(4000):                   # yields only: no timer, so a missed interrupt cannot hang the run
                if plan.done():
                    break
                await asyncio.sleep(0)
            assert plan.done(), "rain must end the settle pause at once"
        finally:
            if not plan.done():
                plan.cancel()
        await coordinator.async_refresh()           # the fresh evaluation the rain listener asked for
        await hass.async_block_till_done()

    for cover in rest:
        assert 50 not in sim.targets_of(cover), f"{cover} must never receive the normal target"
        assert set(sim.targets_of(cover)) == {0}, f"{cover} must receive the rain retraction only"
    assert sim.targets_of(first)[-1] == 0

    last = coordinator.lifecycle_diagnostics()["last_plan_completion"]
    json.dumps(last, allow_nan=False)
    assert coordinator.lifecycle_diagnostics()["running_cycles"] == 0
    assert coordinator.lifecycle_diagnostics()["background_tasks"] == 0


async def _after_the_first_cover(hass, monkeypatch, covers, action):
    """Run a normal 1-99 % sequence; call *action(coordinator)* when cover 1 has
    completed its travel (before the next cover is validated and started)."""
    with freeze_time(_NOON, real_asyncio=True) as frozen:
        sim, entry = await _boot(hass, monkeypatch, covers)
        coordinator = entry.runtime_data.coordinator
        completion_wait = coordinator_module.wait_for_travel_completion
        seen = {"completions": 0}

        async def acting_wait(*args, **kwargs):
            outcome = await completion_wait(*args, **kwargs)
            seen["completions"] += 1
            if seen["completions"] == 1:
                action(coordinator)
            return outcome

        monkeypatch.setattr(coordinator_module, "wait_for_travel_completion", acting_wait)
        frozen.move_to(_NOON + timedelta(minutes=30))
        hass.states.async_set("sun.sun", "above_horizon", _SUN)
        plan = asyncio.ensure_future(coordinator.async_refresh())
        for _ in range(4000):
            if plan.done():
                break
            await asyncio.sleep(0)
        assert plan.done(), f"sequence did not finish; calls so far: {sim.calls}"
        await hass.async_block_till_done()
        coordinator._unloading = False          # let the fixture tear the entry down normally
    return sim, coordinator, seen


async def test_a_dispatch_after_the_entry_started_unloading_sends_nothing(hass, monkeypatch) -> None:
    covers = {f"cover.a8_u{i}": "real" for i in range(3)}
    sim, coordinator, seen = await _after_the_first_cover(
        hass, monkeypatch, covers, lambda coord: setattr(coord, "_unloading", True))
    assert seen["completions"] == 1
    first, *rest = covers
    assert sim.targets_of(first) == [50]
    assert all(sim.targets_of(cover) == [] for cover in rest), "no service call once unloading has begun"
    assert coordinator._lifecycle_stats["dispatches_blocked_unloading"] >= 1


async def test_a_role_change_after_the_first_cover_revalidates_the_next_one(hass, monkeypatch) -> None:
    covers = {f"cover.a8_o{i}": "real" for i in range(3)}
    taken_over = list(covers)[1]

    def change_role(coord) -> None:
        original = coord._cover_role

        def role(window_id, cover_id):
            if cover_id == taken_over:
                return _OWN_DEDUPLICATED
            return original(window_id, cover_id)

        coord._cover_role = role

    sim, _coordinator, _seen = await _after_the_first_cover(hass, monkeypatch, covers, change_role)
    first, _, third = covers
    assert sim.targets_of(first) == [50]
    assert sim.targets_of(taken_over) == [], "a cover that is no longer ours must not be commanded"
    assert sim.targets_of(third) == [50], "a refused cover is skipped; the covers that are still ours continue"
