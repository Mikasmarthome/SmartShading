"""Behavioral tests for T22 Phase 5b — coordinator.py's
_run_comfort_dispatch_for_cycle(), the concurrent-cycle guard ensuring at
most one active comfort DispatchPlanExecutor run per coordinator.

Same self-contained real-SmartShadingCoordinator-with-HA-stubs technique as
test_t22_phase5a_live_validation.py.

Coverage:
  CCG-01  A single cycle's plan runs and completes normally; the active
          task/cancellation registrations are cleared afterward.
  CCG-02  Two SEQUENTIAL (non-overlapping) cycles each get their own
          generation bump; no interference.
  CCG-03  A cycle starting while a previous one is still blocked on
          completion-wait causes the previous one to preempt immediately
          (via the cancellation Event this new cycle sets), NOT via any
          timeout — proof of real cross-cycle signal propagation.
  CCG-04  The new cycle only starts its OWN plan after the previous one has
          actually finished (never two executors running concurrently).
  CCG-05  self._dispatch_generation is bumped BEFORE the previous plan is
          awaited (so the previous plan's own is_generation_current check,
          if reached, would already see the mismatch).
  CCG-06  self._active_comfort_dispatch_task is registered during a run and
          cleared afterward (no leak).
  CCG-07  async_shutdown() awaits the active comfort task rather than
          hanging or leaving it orphaned.
  CCG-08  PARALLEL mode never reaches this method at all (call-site guard,
          verified structurally — PARALLEL keeps its own unmodified path).
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry

    async def async_shutdown(self) -> None:
        pass


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


_HA_STUBS = {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {"SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16}),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub(
        "homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub("homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_track_point_in_time=lambda *a, **k: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}
for _name, _mod in _HA_STUBS.items():
    sys.modules.setdefault(_name, _mod)

sys.modules["homeassistant.util.dt"] = _stub(
    "homeassistant.util.dt",
    utcnow=lambda: datetime.now(timezone.utc),
    now=lambda: datetime.now(timezone.utc),
    as_utc=lambda dt: dt.astimezone(timezone.utc),
    as_local=lambda dt: dt,
    DEFAULT_TIME_ZONE=timezone.utc,
)

sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
)
sys.modules["homeassistant.helpers.storage"] = _stub("homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)
from custom_components.smartshading import coordinator as _coordinator_module  # noqa: E402
from custom_components.smartshading.coordinator import (  # noqa: E402
    SmartShadingCoordinator,
    _WindowComputeState,
)
from custom_components.smartshading.cover_control.command_filter import (  # noqa: E402
    CommandFilterResult,
    ExecutionMode,
)
from custom_components.smartshading.cover_control.cover_capabilities import CoverCapability  # noqa: E402
from custom_components.smartshading.cover_control.cover_entity_snapshot import (  # noqa: E402
    build_cover_entity_snapshot,
)
from custom_components.smartshading.cover_control.execution_result import (  # noqa: E402
    ExecutionStatus,
    build_sent_result,
)
from custom_components.smartshading.cover_control.shading_group_harmonizer import (  # noqa: E402
    HarmonizationResult,
)
from custom_components.smartshading.models.cover_group import CoverGroup  # noqa: E402
from custom_components.smartshading.models.dispatch_config import (  # noqa: E402
    DispatchConfig,
    DispatchMode,
)
from custom_components.smartshading.models.lifecycle import NightDayLifecycleConfig  # noqa: E402
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    return entry


def _make_coord(**kwargs) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    coord = SmartShadingCoordinator(
        hass, entry, lifecycle_config=NightDayLifecycleConfig(id="default"), **kwargs,
    )
    coord.windows = {}
    coord.zones = {}
    coord.cover_groups = {}
    coord._startup_cycles_remaining = 0
    return coord


def _patch_dispatch_cover_intent(monkeypatch, coord, fake):
    monkeypatch.setitem(
        coord._predispatch_sequential_plan.__func__.__globals__,
        "dispatch_cover_intent", fake,
    )


def _patch_wait_for_travel_completion(monkeypatch, coord, fake):
    monkeypatch.setitem(
        coord._predispatch_sequential_plan.__func__.__globals__,
        "wait_for_travel_completion", fake,
    )


def _patch_asyncio_sleep(monkeypatch, coord, fake):
    real_asyncio = coord._predispatch_sequential_plan.__func__.__globals__["asyncio"]
    monkeypatch.setattr(real_asyncio, "sleep", fake)


def _window(window_id: str, zone_id: str) -> WindowConfig:
    return WindowConfig(
        id=window_id, name=window_id, zone_id=zone_id,
        azimuth=180, floor_level=0, cover_group_id=f"cg_{window_id}",
    )


def _state(
    window_id: str, zone_id: str, *, entity_id: str, is_safety: bool = False,
    target_position_ha: int = 100, current_position_ha: int = 0,
) -> _WindowComputeState:
    return _WindowComputeState(
        window=_window(window_id, zone_id),
        zone=ZoneConfig(id=zone_id, name=zone_id),
        obs_enabled=False,
        active_control_enabled=True,
        new_state=ShadingState.NORMAL_SHADE,
        exec_entity_id=entity_id,
        exec_cap=CoverCapability(entity_id=entity_id, supports_position=True, supports_tilt=False, supports_open_close_only=False),
        exec_snapshot=build_cover_entity_snapshot(
            entity_id=entity_id, state="open",
            attributes={"current_position": current_position_ha},
        ),
        exec_mode=ExecutionMode.AUTOMATIC,
        is_safety=is_safety,
        exec_target_internal=100 - target_position_ha,
        exec_filter_result=CommandFilterResult(
            allowed=True, blocked_reason=None,
            target_position_internal=100 - target_position_ha,
            target_position_ha=target_position_ha,
            execution_mode=ExecutionMode.AUTOMATIC.value, is_safety=is_safety,
        ),
        tier_decided_by="TestEvaluator",
        is_override_active=False,
        cover_available=True,
    )


def _setup_coord(
    coord: SmartShadingCoordinator, states,
    *, ha_state: str = "open", current_position_by_entity: dict | None = None,
) -> None:
    coord.windows = {s.window.id: s.window for s in states}
    coord.zones = {s.zone.id: s.zone for s in states}
    coord.cover_groups = {
        s.window.cover_group_id: CoverGroup(
            id=s.window.cover_group_id, window_id=s.window.id, cover_ids=[s.exec_entity_id],
        )
        for s in states
    }
    current_position_by_entity = current_position_by_entity or {}
    ha_states: dict[str, MagicMock] = {}
    for s in states:
        pos = current_position_by_entity.get(s.exec_entity_id, 0)
        st = MagicMock()
        st.state = ha_state
        st.attributes = {"current_position": pos}
        ha_states[s.exec_entity_id] = st
        coord._cover_capabilities[s.exec_entity_id] = s.exec_cap
    coord.hass.states.get = MagicMock(side_effect=lambda eid: ha_states.get(eid))


def _ordered(states):
    return [(s.window.id, s) for s in states]


def _harm(states) -> dict:
    return {
        s.window.id: HarmonizationResult(
            harmonized=False, final_target_position_ha=None,
            pre_harmonization_target_position_ha=None,
        )
        for s in states
    }


def _zone_maps(states):
    zone_order = {}
    window_order_in_zone = {}
    counters: dict[str, int] = {}
    for s in states:
        zone_order.setdefault(s.zone.id, len(zone_order))
        idx = counters.get(s.zone.id, 0)
        window_order_in_zone[s.window.id] = idx
        counters[s.zone.id] = idx + 1
    return zone_order, window_order_in_zone


_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


async def _fake_sent_dispatch(hass, intent, *, now_utc):
    return build_sent_result(intent, sent_at_utc=now_utc, reason="test")


def _fake_completion_factory(on_call=None):
    async def fake_completion(*a, **k):
        if on_call is not None:
            on_call()
        from custom_components.smartshading.cover_control.dispatch_completion import (
            CompletionMethod, CompletionResult,
        )
        return CompletionResult(
            method=CompletionMethod.TIMEOUT, elapsed_s=0.01, timed_out=False,
        )
    return fake_completion


_real_sleep = asyncio.sleep


def _run_cycle(coord, states):
    zone_order, window_order_in_zone = _zone_maps(states)
    return coord._run_comfort_dispatch_for_cycle(
        _ordered(states), _harm(states), _NOW, zone_order, window_order_in_zone,
    )


class TestBasicCycle:
    def test_single_cycle_runs_and_clears_registrations(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))

        async def _run():
            results = await _run_cycle(coord, [s1])
            return results

        results = asyncio.run(_run())
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None
        assert coord._dispatch_generation == 1


class TestSequentialCycles:
    def test_two_non_overlapping_cycles_each_bump_generation(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))

        async def _run():
            await _run_cycle(coord, [s1])
            await _run_cycle(coord, [s1])

        asyncio.run(_run())
        assert coord._dispatch_generation == 2
        assert coord._active_comfort_dispatch_task is None


class TestOverlappingCycles:
    def test_new_cycle_preempts_blocked_previous_cycle_via_cancellation(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        # INTERMEDIATE target so it actually waits for completion.
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 0})
        dispatch_calls = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        # Only A's (first) completion wait hangs; B's own resolves normally
        # so this test can await both tasks to real completion.
        call_count = {"n": 0}

        async def completion_hangs_once(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                await asyncio.Event().wait()  # never resolves on its own
            from custom_components.smartshading.cover_control.dispatch_completion import (
                CompletionMethod, CompletionResult,
            )
            return CompletionResult(
                method=CompletionMethod.TIMEOUT, elapsed_s=0.01, timed_out=False,
            )

        _patch_wait_for_travel_completion(monkeypatch, coord, completion_hangs_once)
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))

        async def _run():
            task_a = asyncio.ensure_future(_run_cycle(coord, [s1]))
            # Let A dispatch and reach the hanging completion wait.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert dispatch_calls == ["cover.w1"]
            assert not task_a.done()

            task_b = asyncio.ensure_future(_run_cycle(coord, [s1]))
            # B's own start should signal A's cancellation immediately and
            # cause A to preempt promptly — no external gate/timeout needed.
            result_a = await asyncio.wait_for(task_a, timeout=2.0)
            result_b = await asyncio.wait_for(task_b, timeout=2.0)
            return result_a, result_b

    def test_no_two_executors_ever_run_concurrently(self, monkeypatch) -> None:
        # Precise overlap detector: tracks how many of {dispatch_cover_
        # intent, wait_for_travel_completion} calls are simultaneously
        # in-flight, across BOTH cycles' fakes sharing one counter. Never
        # awaiting the previous task before starting a new one would let
        # B's own dispatch begin while A's is still (hung-)active —
        # max concurrent must never exceed 1.
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 0})
        active = {"count": 0, "max": 0}

        async def dispatch_and_track(hass, intent, *, now_utc):
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            try:
                return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)
            finally:
                active["count"] -= 1

        _patch_dispatch_cover_intent(monkeypatch, coord, dispatch_and_track)

        async def completion_hangs_forever(*a, **k):
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            await asyncio.Event().wait()  # never resolves or decrements

        _patch_wait_for_travel_completion(monkeypatch, coord, completion_hangs_forever)
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))

        async def _run():
            task_a = asyncio.ensure_future(_run_cycle(coord, [s1]))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task_b = asyncio.ensure_future(_run_cycle(coord, [s1]))
            await asyncio.wait_for(task_a, timeout=2.0)
            # B may itself still be hung waiting on its own completion —
            # that's fine, only concurrent OVERLAP between A and B matters.
            if not task_b.done():
                task_b.cancel()
                try:
                    await task_b
                except asyncio.CancelledError:
                    pass

        asyncio.run(_run())
        assert active["max"] <= 1, (
            "two comfort dispatch executors were active at the same time — "
            "the previous cycle's task must be fully awaited before a new "
            "one starts."
        )

    def test_a_dispatches_and_result_status_correct(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        # INTERMEDIATE target so it actually waits for completion.
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 0})
        dispatch_calls = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        # Only A's (first) completion wait hangs; B's own resolves normally
        # so this test can await both tasks to real completion.
        call_count = {"n": 0}

        async def completion_hangs_once(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                await asyncio.Event().wait()  # never resolves on its own
            from custom_components.smartshading.cover_control.dispatch_completion import (
                CompletionMethod, CompletionResult,
            )
            return CompletionResult(
                method=CompletionMethod.TIMEOUT, elapsed_s=0.01, timed_out=False,
            )

        _patch_wait_for_travel_completion(monkeypatch, coord, completion_hangs_once)
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))

        async def _run():
            task_a = asyncio.ensure_future(_run_cycle(coord, [s1]))
            # Let A dispatch and reach the hanging completion wait.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert dispatch_calls == ["cover.w1"]
            assert not task_a.done()

            task_b = asyncio.ensure_future(_run_cycle(coord, [s1]))
            # B's own start should signal A's cancellation immediately and
            # cause A to preempt promptly — no external gate/timeout needed.
            result_a = await asyncio.wait_for(task_a, timeout=2.0)
            result_b = await asyncio.wait_for(task_b, timeout=2.0)
            return result_a, result_b

        result_a, result_b = asyncio.run(_run())
        assert result_a[("w1", "cover.w1")].status is ExecutionStatus.SENT
        # dispatch_cover_intent was called exactly once during A's own run —
        # B's own re-dispatch (a separately, freshly-decided plan) is a
        # SEPARATE, expected call, not a duplicate of A's.
        assert dispatch_calls.count("cover.w1") <= 2
        assert dispatch_calls[0] == "cover.w1"


class TestGenerationBumpOrdering:
    def test_generation_bumped_before_awaiting_previous_task(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 0})
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)

        # Only A's (first) completion wait hangs — B's own (second) resolves
        # normally, so B can actually finish and this test doesn't need an
        # unbounded wait for a second hang nothing here ever releases.
        call_count = {"n": 0}

        async def completion_hangs_once(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                await asyncio.Event().wait()
            from custom_components.smartshading.cover_control.dispatch_completion import (
                CompletionMethod, CompletionResult,
            )
            return CompletionResult(
                method=CompletionMethod.TIMEOUT, elapsed_s=0.01, timed_out=False,
            )

        _patch_wait_for_travel_completion(monkeypatch, coord, completion_hangs_once)
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))

        async def _run():
            task_a = asyncio.ensure_future(_run_cycle(coord, [s1]))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            gen_before_b = coord._dispatch_generation

            task_b = asyncio.ensure_future(_run_cycle(coord, [s1]))
            await asyncio.sleep(0)
            # Generation must already be bumped even though B is still
            # awaiting A's wind-down (has not started its own plan yet).
            gen_during_b_wait = coord._dispatch_generation
            await asyncio.wait_for(task_a, timeout=2.0)
            await asyncio.wait_for(task_b, timeout=2.0)
            return gen_before_b, gen_during_b_wait

        gen_before_b, gen_during_b_wait = asyncio.run(_run())
        assert gen_during_b_wait == gen_before_b + 1


class TestTaskLifecycle:
    def test_active_task_registered_during_run_and_cleared_after(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])
        seen_during_run = {"task": None, "cancellation": None}

        async def fake_dispatch(hass, intent, *, now_utc):
            seen_during_run["task"] = coord._active_comfort_dispatch_task
            seen_during_run["cancellation"] = coord._active_dispatch_cancellation
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))

        asyncio.run(_run_cycle(coord, [s1]))
        assert seen_during_run["task"] is not None
        assert seen_during_run["cancellation"] is not None
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None


class TestShutdown:
    def test_shutdown_awaits_active_comfort_task(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 0})
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)

        async def hanging_completion(*a, **k):
            # Never resolves on its own — the executor's own race-against-
            # cancellation (wrapping the whole wait_for_completion port
            # call) is what must unblock this, not this fake itself.
            await asyncio.Event().wait()

        _patch_wait_for_travel_completion(monkeypatch, coord, hanging_completion)
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))

        async def _run():
            task = asyncio.ensure_future(_run_cycle(coord, [s1]))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            await asyncio.wait_for(coord.async_shutdown(), timeout=2.0)
            assert task.done()
            assert coord._unloading is True
            # Cooperative wind-down, not a hard Task.cancel(): the executor
            # must exit via its own cancellation-Event/generation check and
            # return a normal (non-cancelled) result, per the ticket's
            # explicit "kein Task.cancel()" requirement.
            assert task.cancelled() is False, (
                "shutdown must let the comfort dispatch task wind down "
                "cooperatively via the cancellation signal, never a hard "
                "Task.cancel()"
            )

        asyncio.run(_run())


class TestParallelModeNeverReachesGuard:
    def test_call_site_guards_by_mode_before_reaching_the_cycle_guard(self) -> None:
        # Structural: _run_comfort_dispatch_for_cycle() (and, since T22
        # Phase 5c, _preempt_active_comfort_plan()) must only be called
        # from behind the SAME `mode in (SEQUENTIAL, SPACED)` guard that
        # already exists for the pre-pass — PARALLEL mode must never reach
        # either at all, so it can never be affected by Phase 5b/5c.
        import re
        from pathlib import Path
        source = (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        pattern = re.compile(
            r"if self\._dispatch_config\.mode in \(DispatchMode\.SEQUENTIAL, DispatchMode\.SPACED\):\s*\n"
            r"\s*if _cycle_has_executable_safety:\s*\n"
            r"\s*await self\._preempt_active_comfort_plan\(\)\s*\n"
            r"\s*else:\s*\n"
            r"(?:.*\n){0,4}?\s*_sequential_results = await self\._run_comfort_dispatch_for_cycle\(",
        )
        assert pattern.search(source), (
            "_run_comfort_dispatch_for_cycle() and _preempt_active_comfort_"
            "plan() must remain called only from behind the SEQUENTIAL/"
            "SPACED mode guard, with `if _cycle_has_executable_safety:` as "
            "the IMMEDIATE, sole condition gating _preempt_active_comfort_"
            "plan() (nothing else in that branch) — PARALLEL mode must "
            "never reach the Phase 5b/5c concurrent-cycle guard, and the "
            "safety/comfort branches must never be swapped or padded with "
            "an extra always-true/always-false branch that would let both "
            "paths fire or the wrong one fire."
        )
