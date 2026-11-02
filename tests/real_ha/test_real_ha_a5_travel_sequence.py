"""A5 -- one cover at a time for 1-99 %, in real Home Assistant.

Real config entry, coordinator, state machine and service registry.  The covers
are simulated on a fake clock that the completion waits advance (so a 20 s
travel costs no real time): each simulated cover reports opening/closing and a
moving position ("real"), jumps its position attribute to the target at once
("jump"), reports no feedback at all ("assumed"), or never reacts ("silent").
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading import coordinator as coordinator_module
from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE
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
_RAIN = "binary_sensor.a5_rain"
TRAVEL_S = 20.0


class Sim:
    """Fake clock + simulated covers that publish their state to Home Assistant."""

    def __init__(self, hass, behaviours: dict[str, str]) -> None:
        self.hass = hass
        self.behaviours = behaviours
        self.t = 0.0
        self.calls: list[tuple[float, str, int]] = []
        self.motion: dict[str, tuple[float, int, int]] = {}      # cover -> (t0, from, to)
        # covers start fully open: no endpoint movement (and no 2 s start pacing,
        # a real asyncio.sleep that freezegun's real_asyncio mode cannot serve) at boot
        self.pos = {c: 100 for c in behaviours}

    def publish(self, cover: str) -> None:
        behaviour = self.behaviours[cover]
        attrs: dict = {"supported_features": 15}
        if behaviour == "assumed":
            attrs["assumed_state"] = True
            self.hass.states.async_set(cover, "open", attrs)
            return
        state, pos = "open" if self.pos[cover] else "closed", self.pos[cover]
        motion = self.motion.get(cover)
        if motion and behaviour in ("real", "jump"):
            t0, start, target = motion
            frac = min(1.0, (self.t - t0) / TRAVEL_S)
            moving = frac < 1.0
            if behaviour == "real":
                pos = round(start + (target - start) * frac)
            else:
                pos = target
            state = ("opening" if target > start else "closing") if (moving and behaviour == "real") else (
                "open" if pos else "closed")
            if frac >= 1.0:
                self.pos[cover] = target
        attrs["current_position"] = pos
        self.hass.states.async_set(cover, state, attrs)

    def tick(self) -> None:
        for cover in self.behaviours:
            self.publish(cover)

    def install(self) -> None:
        async def set_position(call) -> None:
            cover, target = call.data["entity_id"], call.data["position"]
            self.calls.append((self.t, cover, target))
            if self.behaviours[cover] in ("real", "jump"):
                self.motion[cover] = (self.t, self.pos[cover], target)
            self.publish(cover)

        async def noop(call) -> None:
            return None

        self.hass.services.async_register("cover", "set_cover_position", set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", noop)

    def starts(self) -> list[float]:
        return [t for t, _, target in self.calls if target == 50]

    def targets_of(self, cover: str) -> list[int]:
        return [target for _, c, target in self.calls if c == cover]


def _entry(covers, *, entry_id="a5") -> MockConfigEntry:
    zone = ZoneConfig(
        id="z_a5", name="Zone", execution=ZoneExecutionConfig(learning_enabled=False, active_control_enabled=True))
    window = WindowConfig(id="w_a5", name="Window", zone_id="z_a5", azimuth=180.0, floor_level=0,
                          cover_group_id="cg_a5")
    group = CoverGroup(id="cg_a5", window_id="w_a5", cover_ids=list(covers), hardware_type=CoverHardwareType.AWNING)
    data = SmartShadingConfigEntryData(
        name="A5", use_home_location=False, zones=[zone], windows=[window], cover_groups=[group],
        rain_sensor_id=_RAIN,
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=50, normal_shade_position=50, strong_shade_position=50))
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE}, entry_id=entry_id)


def _install_clock(monkeypatch, sim: Sim) -> None:
    async def fake_sleep(seconds):
        sim.t += max(0.0, seconds)
        sim.tick()
        await asyncio.sleep(0)

    async def wrapped(*args, **kwargs):
        kwargs.update(mono_clock=lambda: sim.t, sleep=fake_sleep)
        return await real_wait(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "wait_for_travel_completion", wrapped)
    monkeypatch.setattr(dispatch_plan_executor, "INTERMEDIATE_POST_COMPLETION_PAUSE_S", 0.0)
    # a real 2 s asyncio.sleep cannot be served under freezegun's real_asyncio mode
    monkeypatch.setattr(dispatch_plan_executor, "FULL_OPEN_START_INTERVAL_S", 0.0)


async def _boot(hass, monkeypatch, behaviours: dict[str, str], *, sun_in_sector=False):
    await hass.config.async_set_time_zone("UTC")
    hass.states.async_set("sun.sun", "above_horizon", {
        "azimuth": 180.0 if sun_in_sector else 0.0, "elevation": 60.0 if sun_in_sector else 30.0,
        "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00"})
    hass.states.async_set(_RAIN, "off")
    sim = Sim(hass, behaviours)
    sim.install()
    sim.tick()
    _install_clock(monkeypatch, sim)
    entry = _entry(list(behaviours))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    sim.calls.clear()
    return sim, entry


async def _run_sequence(hass, sim, entry, frozen, *, minutes=30):
    frozen.move_to(_NOON + timedelta(minutes=minutes))
    hass.states.async_set("sun.sun", "above_horizon", {
        "azimuth": 180.0, "elevation": 60.0,
        "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00"})
    try:
        await asyncio.wait_for(entry.runtime_data.coordinator.async_refresh(), timeout=20)
    except asyncio.TimeoutError:
        raise AssertionError(f"sequence hung; calls so far: {sim.calls}") from None
    await hass.async_block_till_done()


class TestSequenceInRealHomeAssistant:
    async def test_two_three_and_four_real_feedback_covers_move_strictly_one_after_the_other(
        self, hass, monkeypatch
    ) -> None:
        for count in (2, 3, 4):
            hass.states.async_remove("sun.sun")
            covers = {f"cover.a5_{count}_{i}": "real" for i in range(count)}
            with freeze_time(_NOON, real_asyncio=True) as frozen:
                sim, entry = await _boot(hass, monkeypatch, covers)
                await _run_sequence(hass, sim, entry, frozen)
                starts = sim.starts()
                assert len(starts) == count, f"{count} covers: {sim.calls}"
                for earlier, later in zip(starts, starts[1:]):
                    assert later - earlier >= TRAVEL_S - 1.5, f"{count} covers: started too early {starts}"
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.config_entries.async_remove(entry.entry_id)
            await hass.async_block_till_done()

    async def test_an_instantly_jumping_position_does_not_start_the_next_cover_early(self, hass, monkeypatch) -> None:
        covers = {f"cover.a5_j{i}": "jump" for i in range(3)}
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            sim, entry = await _boot(hass, monkeypatch, covers)
            await _run_sequence(hass, sim, entry, frozen)
            starts = sim.starts()
        assert len(starts) == 3
        for earlier, later in zip(starts, starts[1:]):
            assert later - earlier >= 14.0   # estimated 50 % of the 30 s default travel time

    async def test_assumed_state_covers_wait_the_estimated_travel_time(self, hass, monkeypatch) -> None:
        covers = {"cover.a5_s0": "assumed", "cover.a5_s1": "assumed"}
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            sim, entry = await _boot(hass, monkeypatch, covers)
            await _run_sequence(hass, sim, entry, frozen)
            starts = sim.starts()
        assert len(starts) == 2 and starts[1] - starts[0] >= 14.0     # 50 % of the 30 s default

    async def test_a_silent_cover_stops_the_rest_of_the_group_and_nothing_resumes_later(self, hass, monkeypatch) -> None:
        covers = {"cover.a5_x0": "silent", "cover.a5_x1": "real"}
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            sim, entry = await _boot(hass, monkeypatch, covers)
            await _run_sequence(hass, sim, entry, frozen)
            await asyncio.sleep(0)
            await hass.async_block_till_done()
        assert sim.targets_of("cover.a5_x0") == [50] and sim.targets_of("cover.a5_x1") == []

    async def test_rain_during_the_wait_overtakes_the_rest_of_the_sequence(self, hass, monkeypatch) -> None:
        covers = {"cover.a5_r0": "real", "cover.a5_r1": "real"}
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            sim, entry = await _boot(hass, monkeypatch, covers)
            frozen.move_to(_NOON + timedelta(minutes=30))
            hass.states.async_set("sun.sun", "above_horizon", {
                "azimuth": 180.0, "elevation": 60.0,
                "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00"})
            plan = asyncio.ensure_future(entry.runtime_data.coordinator.async_refresh())
            for _ in range(400):
                if sim.calls or plan.done():
                    break
                await asyncio.sleep(0)
            assert sim.targets_of("cover.a5_r0") == [50]
            hass.states.async_set(_RAIN, "on")
            await asyncio.wait_for(plan, timeout=10)
            await entry.runtime_data.coordinator.async_refresh()   # the fresh evaluation the rain listener asked for
            await hass.async_block_till_done()
        assert 50 not in sim.targets_of("cover.a5_r1")               # the normal sequence never reached it
        assert sim.targets_of("cover.a5_r1") and set(sim.targets_of("cover.a5_r1")) == {0}
        assert sim.targets_of("cover.a5_r0")[-1] == 0

    async def test_reload_during_the_wait_leaves_nothing_behind(self, hass, monkeypatch) -> None:
        covers = {"cover.a5_u0": "real", "cover.a5_u1": "real"}
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            sim, entry = await _boot(hass, monkeypatch, covers)
            frozen.move_to(_NOON + timedelta(minutes=30))
            hass.states.async_set("sun.sun", "above_horizon", {
                "azimuth": 180.0, "elevation": 60.0,
                "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00"})
            plan = asyncio.ensure_future(entry.runtime_data.coordinator.async_refresh())
            for _ in range(400):
                if sim.calls or plan.done():
                    break
                await asyncio.sleep(0)
            assert sim.targets_of("cover.a5_u0") == [50]
            assert await hass.config_entries.async_unload(entry.entry_id)
            await asyncio.wait_for(plan, timeout=10)
            await hass.async_block_till_done()
            before = list(sim.calls)
            await asyncio.sleep(0)
            await hass.async_block_till_done()
        assert sim.calls == before and sim.targets_of("cover.a5_u1") == []     # no service call after unload
