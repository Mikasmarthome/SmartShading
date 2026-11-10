"""conftest.py for tests/real_ha/ -- tests that run against a real Home Assistant.

The stub-based suite in tests/ registers placeholder modules for
`custom_components.smartshading` so that relative imports resolve without
executing the Home-Assistant-dependent package.  This tree needs the REAL package,
so it is run on its own with pytest's --confcutdir, which keeps tests/conftest.py
from being collected:

    pytest tests/real_ha --confcutdir=tests/real_ha -p no:cacheprovider

Requirements: requirements-real-ha.txt (Linux, macOS or WSL -- Home Assistant's
runner imports the POSIX-only `fcntl`).
"""
import sys
from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

# custom_components/ lives at the repo root, two levels up from this file
# (tests/real_ha/conftest.py -> tests/ -> repo root). tests/real_ha/ has no
# __init__.py, so pytest's default "prepend" import mode only puts
# tests/real_ha/ itself on sys.path; the repo root (parent of
# custom_components/) needs to be added explicitly, exactly like running a
# script from the repo root would.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def _b3013_real_wait_for_completion_safe_sleep(monkeypatch):
    """Every INTERMEDIATE dispatch item runs the real wait_for_travel_completion().
    Under freezegun's real_asyncio mode a real, nonzero asyncio.sleep() never
    returns (freezegun 1.5.x with pytest-homeassistant-custom-component and
    Python 3.14); asyncio.sleep(0) is unaffected.  The wait is therefore wrapped
    with a sleep of zero duration: the production completion logic (position and
    movement judgement, elapsed and timeout arithmetic) still runs, only the one
    primitive that cannot be served in this combination is replaced.  Tests that
    need a controlled clock install their own wrapper on top of this one.
    """
    import asyncio as _asyncio

    from custom_components.smartshading import coordinator as _coordinator_module
    from custom_components.smartshading.cover_control.dispatch_completion import (
        wait_for_travel_completion as _real_wait_for_travel_completion,
    )

    async def _safe_wait_for_travel_completion(*args, **kwargs):
        kwargs.setdefault("sleep", lambda _seconds: _asyncio.sleep(0))
        return await _real_wait_for_travel_completion(*args, **kwargs)

    monkeypatch.setattr(
        _coordinator_module, "wait_for_travel_completion",
        _safe_wait_for_travel_completion,
    )
    yield


@pytest.fixture
def virtual_completion_clock(monkeypatch):
    """Run the real completion logic against a virtual monotonic clock.

    A cover that jumps to its target without reporting movement is judged by an
    estimated travel time.  Against the wall clock that estimate would be waited
    out for real (tens of seconds per cover); here every ``sleep`` simply
    advances a virtual clock, so the production arithmetic (observed movement,
    estimate, timeout) runs unchanged and the test stays deterministic.
    Returns the clock state; ``state["t"]`` is the virtual elapsed seconds.
    """
    import asyncio as _asyncio

    from custom_components.smartshading import coordinator as _coordinator_module
    from custom_components.smartshading.cover_control.dispatch_completion import (
        wait_for_travel_completion as _real_wait_for_travel_completion,
    )

    state = {"t": 0.0}

    async def _advance(seconds):
        state["t"] += max(0.0, seconds)
        await _asyncio.sleep(0)

    async def _virtual_wait_for_travel_completion(*args, **kwargs):
        kwargs.update(mono_clock=lambda: state["t"], sleep=_advance)
        return await _real_wait_for_travel_completion(*args, **kwargs)

    monkeypatch.setattr(
        _coordinator_module, "wait_for_travel_completion",
        _virtual_wait_for_travel_completion,
    )
    yield state


# ----------------------------------------------------------------------------
# Multi-zone harness for the zone-package tests
# ----------------------------------------------------------------------------
# Multi-zone harness for the real-Home-Assistant tests of the global zone packages.
#
# Real config entries, coordinators, state machine, service registry and the real
# shared ``GlobalSerialDispatch``; simulated covers.  Time is virtual and under the
# test's control:
#
# * every ``asyncio.sleep`` of the coordinator module and every sleep of the real
#   completion wait goes to ONE heap (:class:`VirtualClock`); the driver advances
#   the clock only when every task is blocked, so a 20 s travel costs no real time
#   and nothing depends on short real sleeps,
# * Home Assistant's own timers (the refresh debouncer) are driven with the
#   standard ``async_fire_time_changed`` on top of freezegun, never by patching
#   the loop clock.
#
# Every event is logged with its virtual time: service calls, completion start and
# end, rain switches.  Package grants/releases are read from the gate's statistics.


import asyncio
import heapq
import itertools
from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.smartshading import coordinator as cm
from custom_components.smartshading.config_entry_data import SmartShadingConfigEntryData, to_storage_dict
from custom_components.smartshading.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE
from custom_components.smartshading.cover_control.dispatch_completion import (
    wait_for_travel_completion as real_wait,
)
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import ShadePositionDefaults
from custom_components.smartshading.models.cover_group import CoverGroup, CoverHardwareType
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig

NOON = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
SUN = {
    "azimuth": 180.0, "elevation": 60.0,
    "next_rising": "2026-08-12T05:00:00+00:00", "next_setting": "2026-08-11T19:00:00+00:00",
}
TRAVEL_S = 20.0
_YIELD = asyncio.sleep          # the real one: a single event-loop turn for sleep(0)


class VirtualClock:
    def __init__(self) -> None:
        self.t = 0.0
        self._heap: list = []
        self._seq = itertools.count()
        self.log: list[tuple[float, str, str]] = []

    def event(self, kind: str, what: str = "") -> None:
        self.log.append((round(self.t, 2), kind, what))

    async def sleep(self, seconds: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self.t + max(0.0, seconds), next(self._seq), fut))
        await fut

    def advance(self) -> bool:
        while self._heap and self._heap[0][2].done():
            heapq.heappop(self._heap)
        if not self._heap:
            return False
        self.t = max(self.t, self._heap[0][0])
        while self._heap and self._heap[0][0] <= self.t:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)
        return True

    def release_all(self) -> None:
        """Teardown: let every pending virtual sleep return (time no longer matters)."""
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)

    def pending_sleeps(self) -> int:
        return sum(1 for _, _, f in self._heap if not f.done())

    def calls(self) -> list[tuple[float, str, int]]:
        out = []
        for t, kind, what in self.log:
            if kind == "CALL":
                cover, target = what.split("->")
                out.append((t, cover, int(target)))
        return out

    def calls_of(self, prefix: str) -> list[tuple[float, str, int]]:
        return [c for c in self.calls() if c[1].startswith(prefix)]

    def times_of(self, kind: str, contains: str = "") -> list[float]:
        return [t for t, k, w in self.log if k == kind and contains in w]


class Sim:
    """Simulated covers: they report ``opening``/``closing`` and a moving position
    (a *silent* cover accepts commands but never reports anything new)."""

    def __init__(self, hass, clock, covers, initial=None, silent=()) -> None:
        self.hass, self.clock = hass, clock
        self.covers = list(covers)
        self.pos = {c: (initial or {}).get(c, 100) for c in self.covers}
        self.motion: dict = {}
        self.silent = set(silent)

    def publish(self, cover: str) -> None:
        if cover in self.silent and cover in self.motion:
            return
        pos, state = self.pos[cover], "open" if self.pos[cover] else "closed"
        motion = self.motion.get(cover)
        if motion:
            t0, start, target = motion
            frac = min(1.0, (self.clock.t - t0) / TRAVEL_S)
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
            self.clock.event("CALL", f"{cover}->{target}")
            self.motion[cover] = (self.clock.t, self.pos[cover], target)
            self.publish(cover)

        async def noop(call) -> None:
            return None

        self.hass.services.async_register("cover", "set_cover_position", set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", noop)


def zone_entry(entry_id: str, windows: dict, shade: int, rain=None, learning: bool = False,
               presence=None, absence_position=None, comfort_off: bool = False,
               solar_sensor=None, absence_delay_min: int = 0) -> MockConfigEntry:
    """One zone = one config entry.  ``windows`` maps window id -> cover ids (one
    cover group per window)."""
    zone_id = f"z_{entry_id}"
    zone = ZoneConfig(
        id=zone_id, name=zone_id, absence_position=absence_position,
        execution=ZoneExecutionConfig(learning_enabled=learning, active_control_enabled=True))
    wins, groups = [], []
    for window_id, covers in windows.items():
        wins.append(WindowConfig(id=window_id, name=window_id, zone_id=zone_id, azimuth=180.0,
                                 floor_level=0, cover_group_id=f"cg_{window_id}"))
        groups.append(CoverGroup(id=f"cg_{window_id}", window_id=window_id, cover_ids=list(covers),
                                 hardware_type=CoverHardwareType.AWNING))
    data = SmartShadingConfigEntryData(
        name=entry_id, use_home_location=False, zones=[zone], windows=wins, cover_groups=groups,
        rain_sensor_id=rain, solar_radiation_sensor_id=solar_sensor,
        presence_entity_ids=list(presence or []), absence_delay_min=absence_delay_min,
        **({"comfort_config": ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False,
                                           solar_gain_enabled=False)} if comfort_off else {}),
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=shade, normal_shade_position=shade, strong_shade_position=shade))
    return MockConfigEntry(
        domain=DOMAIN, data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
        entry_id=entry_id)


class _AsyncioProxy:
    def __init__(self, clock: VirtualClock) -> None:
        self._clock = clock

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @property
    def sleep(self):
        return self._clock.sleep


class ZoneHarness:
    def __init__(self, hass, monkeypatch, frozen) -> None:
        self.hass, self.monkeypatch, self.frozen = hass, monkeypatch, frozen
        self.clock = VirtualClock()
        self.sim: Sim | None = None
        self.entries: dict[str, MockConfigEntry] = {}
        self.rains: list[str] = []
        #: per entry id: extra ``zone_entry`` keywords (``presence``, ``absence_position``,
        #: ``comfort_off``, ``solar_sensor``, ``absence_delay_min``)
        self.zone_extra: dict[str, dict] = {}

    # -- setup -----------------------------------------------------------------
    async def boot(self, specs, *, initial=None, silent=(), learning=()):
        """``specs``: list of ``(entry_id, windows, shade, rain_entity_or_None)``;
        ``learning``: entry ids whose zone has Learning Mode on."""
        hass, clock = self.hass, self.clock
        await hass.config.async_set_time_zone("UTC")
        hass.states.async_set("sun.sun", "above_horizon", {**SUN, "azimuth": 0.0, "elevation": 30.0})
        self.rains = [r for _, _, _, r in specs if r]
        for rain in self.rains:
            hass.states.async_set(rain, "off")
        covers = [c for _, w, _, _ in specs for cs in w.values() for c in cs]
        self.sim = Sim(hass, clock, covers, initial=initial, silent=silent)
        self.sim.install()
        self.sim.tick()
        self.monkeypatch.setattr(cm, "asyncio", _AsyncioProxy(clock))
        self.monkeypatch.setattr(cm, "_monotonic", lambda: clock.t)
        sim = self.sim

        async def wrapped(*args, **kwargs):
            cover = kwargs.get("entity_id")
            clock.event("WAIT_START", cover)

            async def vsleep(seconds):
                await clock.sleep(seconds)
                sim.tick()

            kwargs.update(mono_clock=lambda: clock.t, sleep=vsleep)
            outcome = await real_wait(*args, **kwargs)
            clock.event("WAIT_END", f"{cover} {outcome.status.value}/{outcome.method.value}")
            return outcome

        self.monkeypatch.setattr(cm, "wait_for_travel_completion", wrapped)
        for entry_id, windows, shade, rain in specs:
            await self.add_zone(entry_id, windows, shade, rain, entry_id in learning)
        for entry in self.entries.values():
            await entry.runtime_data.coordinator.async_refresh()
            await hass.async_block_till_done()
        clock.log.clear()
        clock.t = 0.0
        return self

    async def add_zone(self, entry_id, windows, shade, rain=None, learning=False):
        entry = zone_entry(entry_id, windows, shade, rain, learning,
                           **self.zone_extra.get(entry_id, {}))
        entry.add_to_hass(self.hass)
        assert await self.hass.config_entries.async_setup(entry.entry_id)
        await self.hass.async_block_till_done()
        self.entries[entry_id] = entry
        return entry

    # -- access ----------------------------------------------------------------
    def coord(self, entry_id):
        return self.entries[entry_id].runtime_data.coordinator

    @property
    def serial(self):
        from custom_components.smartshading.const import DATA_GLOBAL_DISPATCH
        return self.hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]

    # -- triggers ---------------------------------------------------------------
    def sun_up(self) -> None:
        self.frozen.move_to(NOON + timedelta(minutes=30))
        self.hass.states.async_set("sun.sun", "above_horizon", SUN)

    def refresh(self, *entry_ids):
        ids = entry_ids or tuple(self.entries)
        return [asyncio.ensure_future(self.coord(i).async_refresh()) for i in ids]

    def rain(self, rain_entity: str, on: bool = True) -> None:
        self.clock.event("RAIN_ON" if on else "RAIN_OFF", rain_entity)
        self.hass.states.async_set(rain_entity, "on" if on else "off")

    # -- driver -----------------------------------------------------------------
    async def drive(self, tasks=(), *, until=None, cap=4000, horizon=None):
        """Run until ``until()`` is true, every task is done, or the clock passed
        ``horizon``; the virtual clock moves only when everything is blocked."""
        for _ in range(cap):
            for _ in range(60):
                await _YIELD(0)
            if until is not None and until():
                return
            if horizon is not None and self.clock.t >= horizon:
                return
            if tasks and all(t.done() for t in tasks) and until is None and horizon is None:
                return
            if self.clock.advance():
                self.sim.tick()
            elif until is None and not tasks:
                return   # nothing left that could move time
        raise AssertionError(f"did not settle (t={self.clock.t}, log tail={self.clock.log[-6:]})")

    async def settle(self, turns: int = 400) -> None:
        for _ in range(turns):
            await _YIELD(0)

    async def fire_ha_timers(self, seconds: float) -> None:
        """Advance Home Assistant's own timers (debouncer cooldown) by ``seconds``
        of controlled time, then let everything they start run."""
        self.frozen.move_to(datetime.now(timezone.utc) + timedelta(seconds=seconds))
        async_fire_time_changed(self.hass, datetime.now(timezone.utc))
        await self.settle()

    async def pass_time(self, seconds: float) -> None:
        """Let ``seconds`` pass for Home Assistant's clock AND for the integration's
        monotonic clock (``fire_ha_timers`` alone moves only the first, so the bounded
        own-motion window would never run out), then fire what is due -- the
        coordinator's own periodic refresh included."""
        self.clock.t += seconds
        await self.fire_ha_timers(seconds)
        await self.drive()

    async def cleanup(self, tasks=()) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for entry_id in list(self.entries):
            self.clock.release_all()
            await self.settle(50)
            try:
                await self.hass.config_entries.async_unload(entry_id)
            except Exception:           # noqa: BLE001 - already unloaded by the test
                pass
        self.clock.release_all()
        await self.settle(50)


@pytest.fixture
async def zones(hass, monkeypatch):
    """Multi-zone harness (see zone_harness.py): real entries/coordinators and the
    real shared dispatch, simulated covers, virtual time under freezegun."""
    from freezegun import freeze_time

    with freeze_time(NOON, real_asyncio=True) as frozen:
        harness = ZoneHarness(hass, monkeypatch, frozen)
        try:
            yield harness
        finally:
            await harness.cleanup()
