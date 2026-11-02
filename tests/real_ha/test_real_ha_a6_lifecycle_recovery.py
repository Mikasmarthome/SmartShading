"""A6 -- lifecycle, recovery and diagnostics in real Home Assistant.

Real config entry, coordinator, state machine, storage and entity registry:

* a stored rain release hold survives an unload / setup and is not released early,
* an unload with a running travel wait ends the plan and leaves no task behind,
* a cover renamed in the entity registry is followed by the stored zone data,
* the official diagnostics route returns a bounded, JSON-safe payload with the
  lifecycle block, and leaves the learning store exactly as it was.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
from homeassistant.helpers import entity_registry as er
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
from custom_components.smartshading.diagnostics import async_get_config_entry_diagnostics
from custom_components.smartshading.engines.export_budget import EMBEDDED_EXPORT_BUDGET_BYTES
from custom_components.smartshading.models.config import ShadePositionDefaults
from custom_components.smartshading.models.cover_group import CoverGroup, CoverHardwareType
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig

_NOON = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
_RAIN = "binary_sensor.a6_rain"
_SUN = {"azimuth": 180.0, "elevation": 60.0,
        "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00"}


def _entry(covers, *, entry_id="a6") -> MockConfigEntry:
    zone = ZoneConfig(
        id="z_a6", name="Zone", execution=ZoneExecutionConfig(learning_enabled=False, active_control_enabled=True))
    window = WindowConfig(id="w_a6", name="Window", zone_id="z_a6", azimuth=180.0, floor_level=0,
                          cover_group_id="cg_a6")
    group = CoverGroup(id="cg_a6", window_id="w_a6", cover_ids=list(covers), hardware_type=CoverHardwareType.AWNING)
    data = SmartShadingConfigEntryData(
        name="A6", use_home_location=False, zones=[zone], windows=[window], cover_groups=[group],
        rain_sensor_id=_RAIN,
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=50, normal_shade_position=50, strong_shade_position=50))
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE}, entry_id=entry_id)


class Sim:
    """Fake clock and covers that publish their state to Home Assistant."""

    def __init__(self, hass, covers) -> None:
        self.hass, self.covers = hass, list(covers)
        self.t = 0.0
        self.calls: list[tuple[str, int]] = []
        self.pos = {c: 100 for c in self.covers}
        self.motion: dict[str, tuple[float, int, int]] = {}

    def publish(self, cover: str) -> None:
        pos, state = self.pos[cover], "open"
        motion = self.motion.get(cover)
        if motion:
            t0, start, target = motion
            frac = min(1.0, (self.t - t0) / 20.0)
            pos = round(start + (target - start) * frac)
            state = ("opening" if target > start else "closing") if frac < 1.0 else ("open" if pos else "closed")
            if frac >= 1.0:
                self.pos[cover] = target
        self.hass.states.async_set(cover, state, {"supported_features": 15, "current_position": pos})

    def install(self) -> None:
        async def set_position(call) -> None:
            cover, target = call.data["entity_id"], call.data["position"]
            self.calls.append((cover, target))
            self.motion[cover] = (self.t, self.pos[cover], target)
            self.publish(cover)

        async def noop(call) -> None:
            return None

        self.hass.services.async_register("cover", "set_cover_position", set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", noop)
        for cover in self.covers:
            self.publish(cover)

    def clock(self, monkeypatch) -> None:
        async def fake_sleep(seconds):
            self.t += max(0.0, seconds)
            for cover in self.covers:
                self.publish(cover)
            await asyncio.sleep(0)

        async def wrapped(*args, **kwargs):
            kwargs.update(mono_clock=lambda: self.t, sleep=fake_sleep)
            return await real_wait(*args, **kwargs)

        monkeypatch.setattr(coordinator_module, "wait_for_travel_completion", wrapped)
        monkeypatch.setattr(dispatch_plan_executor, "INTERMEDIATE_POST_COMPLETION_PAUSE_S", 0.0)
        monkeypatch.setattr(dispatch_plan_executor, "FULL_OPEN_START_INTERVAL_S", 0.0)

    def targets(self, cover: str) -> list[int]:
        return [target for c, target in self.calls if c == cover]


async def _boot(hass, monkeypatch, covers, *, rain="off", entry=None):
    await hass.config.async_set_time_zone("UTC")
    hass.states.async_set("sun.sun", "above_horizon", {**_SUN, "azimuth": 0.0, "elevation": 30.0})
    hass.states.async_set(_RAIN, rain)
    sim = Sim(hass, covers)
    sim.install()
    sim.clock(monkeypatch)
    entry = entry or _entry(covers)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    sim.calls.clear()
    return sim, entry


class TestRunningWaitAtUnload:
    async def test_unload_during_the_wait_ends_the_plan_and_leaves_no_task(self, hass, monkeypatch) -> None:
        covers = ["cover.a6_u0", "cover.a6_u1"]
        with freeze_time(_NOON, real_asyncio=True) as frozen:
            sim, entry = await _boot(hass, monkeypatch, covers)
            frozen.move_to(_NOON + timedelta(minutes=30))
            hass.states.async_set("sun.sun", "above_horizon", _SUN)
            plan = asyncio.ensure_future(entry.runtime_data.coordinator.async_refresh())
            for _ in range(400):
                if sim.calls or plan.done():
                    break
                await asyncio.sleep(0)
            assert sim.targets("cover.a6_u0") == [50]
            coordinator = entry.runtime_data.coordinator
            assert await hass.config_entries.async_unload(entry.entry_id)
            await asyncio.wait_for(plan, timeout=10)
            await hass.async_block_till_done()
            sent = list(sim.calls)
            await asyncio.sleep(0)
            await hass.async_block_till_done()
        assert sim.calls == sent and sim.targets("cover.a6_u1") == []
        diag = coordinator.lifecycle_diagnostics()
        assert diag["unloading"] is True and diag["running_cycles"] == 0
        assert diag["listeners_active"]["rain"] == 0 and diag["background_tasks"] == 0


class TestSafetyHoldRestart:
    async def test_a_latched_rain_hold_survives_unload_and_setup(self, hass, monkeypatch) -> None:
        covers = ["cover.a6_h0"]
        with freeze_time(_NOON, real_asyncio=True):
            sim, entry = await _boot(hass, monkeypatch, covers, rain="on")
            coordinator = entry.runtime_data.coordinator
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert coordinator.lifecycle_diagnostics()["safety_holds"]["latched"]["rain"] == 1
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            hass.states.async_set(_RAIN, "unavailable")            # sensor gone during the restart
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            fresh = entry.runtime_data.coordinator
            await fresh.async_refresh()
            await hass.async_block_till_done()
        diag = fresh.lifecycle_diagnostics()["safety_holds"]
        assert diag["restored"] == 1 and diag["latched"]["rain"] == 1
        assert all(target == 0 for target in sim.targets("cover.a6_h0"))   # still safe, never extended

    async def test_a_removed_entry_leaves_no_hold_storage_behind(self, hass, monkeypatch) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _sim, entry = await _boot(hass, monkeypatch, ["cover.a6_r0"], rain="on")
            await entry.runtime_data.coordinator.async_refresh()
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.config_entries.async_remove(entry.entry_id)
            await hass.async_block_till_done()
        assert not any(key.startswith("smartshading_safety_holds") for key in hass.data.get("storage", {}) if key)


class TestRenameFollowed:
    async def test_a_renamed_cover_is_rewritten_and_the_zone_is_reloaded(self, hass, monkeypatch) -> None:
        old, new = "cover.a6_old", "cover.a6_new"
        registry = er.async_get(hass)
        registry.async_get_or_create("cover", "test", "a6_unique", suggested_object_id="a6_old")
        assert registry.async_get(old) is not None
        sim, entry = await _boot(hass, monkeypatch, [old])
        registry.async_update_entity(old, new_entity_id=new)
        await hass.async_block_till_done()
        groups = entry.data["cover_groups"]
        assert [g["cover_ids"] for g in groups] == [[new]]
        assert entry.runtime_data.coordinator.cover_groups["cg_a6"].cover_ids == [new]


class TestDiagnosticsRoute:
    async def test_bounded_json_safe_payload_with_the_lifecycle_block_and_an_unchanged_learning_store(
        self, hass, monkeypatch
    ) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _sim, entry = await _boot(hass, monkeypatch, ["cover.a6_d0"])
            coordinator = entry.runtime_data.coordinator
            store = coordinator._learning_store
            before = repr(sorted(getattr(store, "__dict__", {}).keys())), len(repr(store.__dict__)) if store else 0
            payload = await async_get_config_entry_diagnostics(hass, entry)
            after = repr(sorted(getattr(store, "__dict__", {}).keys())), len(repr(store.__dict__)) if store else 0
        text = json.dumps(payload, default=str)
        assert len(text.encode()) <= EMBEDDED_EXPORT_BUDGET_BYTES * 1.25
        zones = payload["support_export"]["zones"]
        assert zones and "lifecycle" in zones[0]
        lifecycle = zones[0]["lifecycle"]
        assert {"listeners_active", "safety_holds", "completion_feedback", "refresh_paths",
            "last_plan_completion"} <= set(lifecycle)
        assert "cover." not in json.dumps(lifecycle) and "binary_sensor." not in json.dumps(lifecycle)
        assert before == after
