"""Behavioral tests for T22 Phase 5c — cross-cycle safety preemption of an
active comfort DispatchPlanExecutor run.

Covers:
  - coordinator._cycle_has_executable_safety_intent() (module-level pure
    function): precise "does this cycle carry an executable safety intent"
    check, reused (not recomputed) from Pass-1 data.
  - coordinator.SmartShadingCoordinator._preempt_active_comfort_plan():
    the extracted invalidate-and-await-previous-only half of Phase 5b's
    _run_comfort_dispatch_for_cycle(), used when a cycle carries safety.

Same self-contained real-SmartShadingCoordinator-with-HA-stubs technique as
test_t22_phase5b_concurrent_cycle_guard.py.

Coverage:
  SP-01  A safety window with allowed=True CommandFilterResult -> True.
  SP-02  A safety window with allowed=False -> False (not executable).
  SP-03  A non-safety window, even allowed=True -> False.
  SP-04  No windows at all -> False.
  SP-05  Mixed: one non-executable safety + one executable safety -> True.
  SP-06  _preempt_active_comfort_plan() bumps generation even with no
         active previous task (no exception, no artificial delay).
  SP-07  _preempt_active_comfort_plan() signals and awaits an active
         previous comfort task, causing it to preempt promptly (via the
         SAME cancellation-race mechanism Phase 5b/3 already established)
         — proof this reuses, not reinvents, the preemption machinery.
  SP-08  _preempt_active_comfort_plan() does NOT create a new comfort task
         or cancellation Event itself — self._active_comfort_dispatch_task
         and self._active_dispatch_cancellation stay None/cleared after,
         so a later normal comfort cycle starts fresh (no stale reuse).
  SP-09  Two safety preemptions in quick succession are idempotent — no
         exception, generation keeps advancing, no double cleanup.
  SP-10  _preempt_active_comfort_plan() never touches asyncio.Task.cancel
         (structural — mirrors Phase 5b's own cancel()-freedom guarantee).
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
    _cycle_has_executable_safety_intent,
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


def _window(window_id: str, zone_id: str) -> WindowConfig:
    return WindowConfig(
        id=window_id, name=window_id, zone_id=zone_id,
        azimuth=180, floor_level=0, cover_group_id=f"cg_{window_id}",
    )


def _state(
    window_id: str, zone_id: str, *, entity_id: str, is_safety: bool = False,
    filter_allowed: bool | None = True, target_position_ha: int = 100,
) -> _WindowComputeState:
    filter_result = None
    if filter_allowed is not None:
        filter_result = CommandFilterResult(
            allowed=filter_allowed,
            blocked_reason=None if filter_allowed else "recommendation_only",
            target_position_internal=100 - target_position_ha,
            target_position_ha=target_position_ha,
            execution_mode=ExecutionMode.AUTOMATIC.value, is_safety=is_safety,
        )
    return _WindowComputeState(
        window=_window(window_id, zone_id),
        zone=ZoneConfig(id=zone_id, name=zone_id),
        obs_enabled=False,
        active_control_enabled=True,
        new_state=ShadingState.NORMAL_SHADE,
        exec_entity_id=entity_id,
        exec_cap=CoverCapability(entity_id=entity_id, supports_position=True, supports_tilt=False, supports_open_close_only=False),
        exec_snapshot=build_cover_entity_snapshot(
            entity_id=entity_id, state="open", attributes={"current_position": 0},
        ),
        exec_mode=ExecutionMode.AUTOMATIC,
        is_safety=is_safety,
        exec_target_internal=100 - target_position_ha,
        exec_filter_result=filter_result,
        tier_decided_by="TestEvaluator",
        is_override_active=False,
        cover_available=True,
    )


def _ordered(states):
    return [(s.window.id, s) for s in states]


_real_sleep = asyncio.sleep


class TestCycleHasExecutableSafetyIntent:
    def test_executable_safety_true(self) -> None:
        s1 = _state("w1", "z1", entity_id="cover.w1", is_safety=True, filter_allowed=True)
        assert _cycle_has_executable_safety_intent(_ordered([s1])) is True

    def test_blocked_safety_false(self) -> None:
        s1 = _state("w1", "z1", entity_id="cover.w1", is_safety=True, filter_allowed=False)
        assert _cycle_has_executable_safety_intent(_ordered([s1])) is False

    def test_non_safety_allowed_false(self) -> None:
        s1 = _state("w1", "z1", entity_id="cover.w1", is_safety=False, filter_allowed=True)
        assert _cycle_has_executable_safety_intent(_ordered([s1])) is False

    def test_no_windows_false(self) -> None:
        assert _cycle_has_executable_safety_intent([]) is False

    def test_mixed_one_executable_true(self) -> None:
        s1 = _state("w1", "z1", entity_id="cover.w1", is_safety=True, filter_allowed=False)
        s2 = _state("w2", "z1", entity_id="cover.w2", is_safety=True, filter_allowed=True)
        assert _cycle_has_executable_safety_intent(_ordered([s1, s2])) is True

    def test_no_filter_result_yet_false(self) -> None:
        s1 = _state("w1", "z1", entity_id="cover.w1", is_safety=True, filter_allowed=None)
        assert _cycle_has_executable_safety_intent(_ordered([s1])) is False


class TestPreemptWithoutActivePlan:
    def test_bumps_generation_no_active_task(self) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        gen_before = coord._dispatch_generation

        async def _run():
            return await coord._preempt_active_comfort_plan()

        result_gen = asyncio.run(_run())
        assert result_gen == gen_before + 1
        assert coord._dispatch_generation == gen_before + 1
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None


class TestPreemptWithActivePlan:
    def test_signals_and_awaits_active_comfort_task_promptly(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation

        async def _run():
            task = asyncio.ensure_future(_run_forever_placeholder(cancellation))
            coord._active_comfort_dispatch_task = task
            await asyncio.sleep(0)
            assert not task.done()
            result_gen = await asyncio.wait_for(
                coord._preempt_active_comfort_plan(), timeout=2.0
            )
            return result_gen, task

        result_gen, task = asyncio.run(_run())
        assert task.done()
        assert task.result() == "wound_down"
        assert result_gen == 1

    def test_leaves_no_new_task_or_cancellation_registered(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation

        # Mirrors _run_comfort_dispatch_for_cycle()'s own identity-guarded
        # self-cleanup in its finally block — in real production, THIS
        # (the previous task's own wrapper) is what clears the
        # registration once it winds down, not _preempt_active_comfort_
        # plan() itself. A bare placeholder task without this wrapper
        # would never clear anything, which would test an unrealistic
        # scenario.
        async def _wrapped_previous_plan():
            try:
                return await _run_forever_placeholder(cancellation)
            finally:
                if coord._active_comfort_dispatch_task is task:
                    coord._active_comfort_dispatch_task = None
                if coord._active_dispatch_cancellation is cancellation:
                    coord._active_dispatch_cancellation = None

        async def _run():
            nonlocal task
            task = asyncio.ensure_future(_wrapped_previous_plan())
            coord._active_comfort_dispatch_task = task
            await asyncio.wait_for(coord._preempt_active_comfort_plan(), timeout=2.0)

        task = None
        asyncio.run(_run())
        # _preempt_active_comfort_plan() must not create its own new
        # registration — a later normal comfort cycle needs to see a
        # clean slate, not a leftover Event/Task from the preemption.
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None


async def _run_forever_placeholder(cancellation: asyncio.Event) -> str:
    await cancellation.wait()
    return "wound_down"


class TestIdempotentDoublePreemption:
    def test_two_preemptions_in_succession_are_safe(self) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))

        async def _run():
            gen1 = await coord._preempt_active_comfort_plan()
            gen2 = await coord._preempt_active_comfort_plan()
            return gen1, gen2

        gen1, gen2 = asyncio.run(_run())
        assert gen2 == gen1 + 1
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None


class TestNoHardCancel:
    def test_preempt_never_calls_task_cancel(self) -> None:
        import re
        from pathlib import Path
        source = (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        # Extract just the _preempt_active_comfort_plan method body.
        match = re.search(
            r"async def _preempt_active_comfort_plan\(self\).*?\n"
            r"(?:.*\n)*?"
            r"        return this_dispatch_gen\n",
            source,
        )
        assert match, "could not locate _preempt_active_comfort_plan() in coordinator.py"
        body = match.group(0)
        assert ".cancel()" not in body, (
            "_preempt_active_comfort_plan() must never call Task.cancel() — "
            "preemption is cooperative via generation + cancellation Event."
        )


class TestSequentialResultRoutingReason:
    def test_safety_preempted_reason_present_in_source(self) -> None:
        from pathlib import Path
        source = (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        assert 'reason="safety_preempted"' in source, (
            "the per-window loop must record a comfort item deferred by "
            "T22 Phase 5c safety preemption with the stable, diagnostically "
            "distinct reason 'safety_preempted' — not fall through to the "
            "legacy lock block, and not reuse an unrelated reason string."
        )

    def test_safety_preempted_branch_condition_is_real_not_neutralized(self) -> None:
        # The bare string check above cannot detect an `elif False:` (or
        # similarly neutralized) condition guarding an otherwise-untouched
        # reason string — this test specifically requires the REAL
        # condition (referencing _cycle_has_executable_safety) to
        # immediately precede the reason string, not just co-exist
        # somewhere in the file.
        import re
        from pathlib import Path
        source = (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        pattern = re.compile(
            r"elif \(\s*\n\s*_cycle_has_executable_safety\s*\n"
            r"(?:.*\n){0,25}?.*reason=\"safety_preempted\"",
        )
        assert pattern.search(source), (
            "the per-window loop's safety_preempted branch must be gated "
            "on the real _cycle_has_executable_safety condition — an "
            "`elif False:` (or other neutralized condition) with the "
            "reason string merely left behind as dead code would defeat "
            "this entirely without the bare string check catching it."
        )


def _fake_completion_hangs_forever_factory(counter: dict):
    async def fake_completion(*a, **k):
        counter["completion_waits_entered"] = counter.get("completion_waits_entered", 0) + 1
        await asyncio.Event().wait()
    return fake_completion


class TestSafetyPreemptsActiveComfortPlanMidCompletionWait:
    # End-to-end: cycle A starts a real _run_comfort_dispatch_for_cycle()
    # run (a genuine comfort plan, not merely _preempt_active_comfort_plan()
    # in isolation) and hangs inside its completion-wait for an
    # INTERMEDIATE target. Cycle B then discovers an executable safety
    # intent and — mirroring the real call-site branch exactly — calls
    # ONLY _preempt_active_comfort_plan(), never _run_comfort_dispatch_for_
    # cycle() again. This proves safety preemption reaches into an
    # ACTIVELY-RUNNING comfort plan's completion-wait phase, winds it down
    # promptly without a second dispatch call, and never itself starts a
    # new comfort plan.
    def test_safety_preemption_during_active_completion_wait(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        coord.windows = {"w1": s1.window}
        coord.zones = {"z1": s1.zone}
        from custom_components.smartshading.models.cover_group import CoverGroup
        coord.cover_groups = {"cg_w1": CoverGroup(id="cg_w1", window_id="w1", cover_ids=["cover.w1"])}
        coord._cover_capabilities["cover.w1"] = s1.exec_cap
        st = MagicMock()
        st.state = "open"
        st.attributes = {"current_position": 0}
        coord.hass.states.get = MagicMock(side_effect=lambda eid: {"cover.w1": st}.get(eid))

        dispatch_calls: list[str] = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "dispatch_cover_intent", fake_dispatch,
        )
        counter: dict = {}
        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "wait_for_travel_completion", _fake_completion_hangs_forever_factory(counter),
        )
        real_asyncio = coord._predispatch_sequential_plan.__func__.__globals__["asyncio"]
        monkeypatch.setattr(real_asyncio, "sleep", lambda s: _real_sleep(0))

        async def _run():
            zone_order = {"z1": 0}
            window_order_in_zone = {"w1": 0}
            from custom_components.smartshading.cover_control.shading_group_harmonizer import (
                HarmonizationResult,
            )
            harm = {
                "w1": HarmonizationResult(
                    harmonized=False, final_target_position_ha=None,
                    pre_harmonization_target_position_ha=None,
                ),
            }
            task_a = asyncio.ensure_future(
                coord._run_comfort_dispatch_for_cycle(
                    _ordered([s1]), harm, datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
                    zone_order, window_order_in_zone,
                )
            )
            # Let A dispatch and enter the (hanging) completion wait.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert dispatch_calls == ["cover.w1"], (
                "cycle A must have actually sent its dispatch and be "
                "genuinely stuck in completion-wait before B preempts, "
                "otherwise this isn't testing mid-completion-wait at all"
            )
            assert not task_a.done()
            assert counter.get("completion_waits_entered", 0) == 1

            # Cycle B mirrors the real call-site: it found executable
            # safety, so it calls ONLY _preempt_active_comfort_plan() —
            # never _run_comfort_dispatch_for_cycle() again.
            gen_b = await asyncio.wait_for(coord._preempt_active_comfort_plan(), timeout=2.0)
            await asyncio.wait_for(task_a, timeout=2.0)
            return gen_b

        gen_b = asyncio.run(_run())
        assert gen_b == 2  # A's own run bumped to 1, B's preemption bumps to 2
        assert dispatch_calls == ["cover.w1"], (
            "no second dispatch/service call may occur for the same window "
            "after safety preemption — the previous plan must not resume "
            "or re-dispatch after being preempted"
        )
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None


def _setup_two_window_coord(coord, s1, s2, *, positions=None):
    from custom_components.smartshading.models.cover_group import CoverGroup
    coord.windows = {s1.window.id: s1.window, s2.window.id: s2.window}
    coord.zones = {s1.zone.id: s1.zone, s2.zone.id: s2.zone}
    coord.cover_groups = {
        s1.window.cover_group_id: CoverGroup(
            id=s1.window.cover_group_id, window_id=s1.window.id, cover_ids=[s1.exec_entity_id]),
        s2.window.cover_group_id: CoverGroup(
            id=s2.window.cover_group_id, window_id=s2.window.id, cover_ids=[s2.exec_entity_id]),
    }
    coord._cover_capabilities[s1.exec_entity_id] = s1.exec_cap
    coord._cover_capabilities[s2.exec_entity_id] = s2.exec_cap
    positions = positions or {}
    states = {}
    for s in (s1, s2):
        st = MagicMock()
        st.state = "open"
        st.attributes = {"current_position": positions.get(s.exec_entity_id, 0)}
        states[s.exec_entity_id] = st
    coord.hass.states.get = MagicMock(side_effect=lambda eid: states.get(eid))


def _harm_for(*states):
    from custom_components.smartshading.cover_control.shading_group_harmonizer import (
        HarmonizationResult,
    )
    return {
        s.window.id: HarmonizationResult(
            harmonized=False, final_target_position_ha=None,
            pre_harmonization_target_position_ha=None,
        )
        for s in states
    }


class TestSafetyPreemptsDuringFullOpenPacing:
    # Ticket §3.1: a comfort plan with two FULL_OPEN items — after the
    # first item's real dispatch call, the executor is genuinely inside
    # its FULL_OPEN-to-FULL_OPEN pacing sleep (the real
    # `_race_against_cancellation(sleep(remaining), cancellation)` call in
    # dispatch_plan_executor.py, not a source-level stand-in). Safety
    # preemption must not wait for that pacing interval to elapse and must
    # prevent the second FULL_OPEN item from ever dispatching.
    def test_safety_preemption_during_full_open_pacing_interval(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=100)
        s2 = _state("w2", "z1", entity_id="cover.w2", target_position_ha=100)
        _setup_two_window_coord(coord, s1, s2)

        dispatch_calls: list[str] = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "dispatch_cover_intent", fake_dispatch,
        )
        # completion wait is irrelevant for FULL_OPEN items (no completion
        # wait is performed for them at all) — still patched defensively so
        # a classification regression cannot silently hang the test.
        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "wait_for_travel_completion", _fake_completion_hangs_forever_factory({}),
        )
        # The pacing sleep itself never resolves on its own — only the
        # cancellation Event (set by real _preempt_active_comfort_plan())
        # may unblock it. This is the real production sleep() port, not a
        # recursive self-calling stand-in.
        real_asyncio = coord._predispatch_sequential_plan.__func__.__globals__["asyncio"]
        monkeypatch.setattr(real_asyncio, "sleep", lambda s: asyncio.Event().wait())

        async def _run():
            zone_order = {"z1": 0}
            window_order_in_zone = {"w1": 0, "w2": 1}
            task_a = asyncio.ensure_future(
                coord._run_comfort_dispatch_for_cycle(
                    _ordered([s1, s2]), _harm_for(s1, s2),
                    datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
                    zone_order, window_order_in_zone,
                )
            )
            # Real asyncio.sleep is patched to hang for the rest of this
            # test — use the captured original for our own control-flow
            # yields so this test doesn't hang itself.
            await _real_sleep(0)
            await _real_sleep(0)
            await _real_sleep(0)
            assert dispatch_calls == ["cover.w1"], (
                "item 1 (FULL_OPEN) must have actually dispatched and the "
                "executor must now be genuinely stuck in the real pacing "
                "sleep before item 2 — otherwise this isn't testing "
                "mid-pacing preemption at all"
            )
            assert not task_a.done()

            gen_b = await asyncio.wait_for(coord._preempt_active_comfort_plan(), timeout=2.0)
            await asyncio.wait_for(task_a, timeout=2.0)
            return gen_b

        asyncio.run(_run())
        assert dispatch_calls == ["cover.w1"], (
            "the second FULL_OPEN item must never dispatch once safety "
            "preemption interrupts the pacing wait between items"
        )
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None


class TestSafetyPreemptsDuringIntermediatePostCompletionPause:
    # Ticket §3.2: an INTERMEDIATE item completes, the executor enters its
    # real fixed post-completion pause (`_race_against_cancellation(sleep(
    # INTERMEDIATE_POST_COMPLETION_PAUSE_S), cancellation)`), and safety
    # preemption must interrupt that pause immediately rather than waiting
    # for it to elapse naturally, and prevent the next item from dispatching.
    def test_safety_preemption_during_post_completion_pause(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        s2 = _state("w2", "z1", entity_id="cover.w2", target_position_ha=40)
        _setup_two_window_coord(coord, s1, s2)

        dispatch_calls: list[str] = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "dispatch_cover_intent", fake_dispatch,
        )

        async def completion_resolves_once(*a, **k):
            from custom_components.smartshading.cover_control.dispatch_completion import (
                CompletionMethod, CompletionResult,
            )
            return CompletionResult(method=CompletionMethod.TIMEOUT, elapsed_s=0.01, timed_out=False)

        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "wait_for_travel_completion", completion_resolves_once,
        )
        # The post-completion pause sleep never resolves on its own —
        # only preemption's cancellation Event may unblock it.
        real_asyncio = coord._predispatch_sequential_plan.__func__.__globals__["asyncio"]
        monkeypatch.setattr(real_asyncio, "sleep", lambda s: asyncio.Event().wait())

        async def _run():
            zone_order = {"z1": 0}
            window_order_in_zone = {"w1": 0, "w2": 1}
            task_a = asyncio.ensure_future(
                coord._run_comfort_dispatch_for_cycle(
                    _ordered([s1, s2]), _harm_for(s1, s2),
                    datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
                    zone_order, window_order_in_zone,
                )
            )
            await _real_sleep(0)
            await _real_sleep(0)
            await _real_sleep(0)
            await _real_sleep(0)
            assert dispatch_calls == ["cover.w1"], (
                "item 1 (INTERMEDIATE) must have dispatched and completed, "
                "and the executor must now be genuinely stuck in the real "
                "post-completion pause before item 2 dispatches"
            )
            assert not task_a.done()

            gen_b = await asyncio.wait_for(coord._preempt_active_comfort_plan(), timeout=2.0)
            await asyncio.wait_for(task_a, timeout=2.0)
            return gen_b

        asyncio.run(_run())
        assert dispatch_calls == ["cover.w1"], (
            "the second INTERMEDIATE item must never dispatch once safety "
            "preemption interrupts the post-completion pause"
        )
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None


class TestSafetyPreemptsRightAfterValidationBeforeServiceCall:
    # Ticket §3.4: the executor's own second cancellation.is_set() check
    # (dispatch_plan_executor.py, immediately after validate_item returns
    # EXECUTE and immediately before dispatch_item is awaited) must prevent
    # the comfort service call once safety has set the cancellation Event
    # in that exact window — proven at the real DispatchPlanExecutor level
    # with a validate_item port that itself sets the cancellation the
    # instant it is called, mirroring safety becoming active in the gap
    # between live-validation returning EXECUTE and the service call.
    def test_cancellation_set_between_validation_and_dispatch_blocks_service_call(self) -> None:
        from custom_components.smartshading.cover_control.dispatch_plan import (
            DispatchPlan, DispatchPlanItem,
        )
        from custom_components.smartshading.cover_control.dispatch_plan_executor import (
            DispatchPlanExecutor, DispatchOutcome, ExecutionValidation, ValidationAction,
        )
        from custom_components.smartshading.engines.dispatch_classification import (
            DispatchTargetClass,
        )

        cancellation = asyncio.Event()
        dispatch_calls: list[str] = []

        item = DispatchPlanItem(
            zone_id="z1", zone_index=0, cover_entity_id="cover.w1", cover_index=0,
            target_ha=40, target_class=DispatchTargetClass.INTERMEDIATE,
            decision_ref="d1", zone_generation=1,
        )
        plan = DispatchPlan(plan_id="p1", trigger="test", created_at=None, items=(item,))

        async def dispatch_item(plan_item):
            dispatch_calls.append(plan_item.cover_entity_id)
            return DispatchOutcome(success=True)

        async def wait_for_completion(plan_item, cancel_sig):
            from custom_components.smartshading.cover_control.dispatch_completion import (
                CompletionMethod, CompletionResult,
            )
            return CompletionResult(method=CompletionMethod.TIMEOUT, elapsed_s=0.01, timed_out=False)

        def validate_item(plan_item):
            # Mirrors safety becoming active in the exact gap between
            # live-validation returning EXECUTE and the service call.
            cancellation.set()
            return ExecutionValidation(ValidationAction.EXECUTE)

        result = asyncio.run(DispatchPlanExecutor().execute_plan(
            plan=plan,
            dispatch_item=dispatch_item,
            wait_for_completion=wait_for_completion,
            validate_item=validate_item,
            is_generation_current=lambda i: True,
            cancellation=cancellation,
            sleep=lambda s: _real_sleep(0),
            clock=lambda: 0.0,
        ))
        assert dispatch_calls == [], (
            "the comfort service call must never fire once cancellation "
            "was set before the executor's pre-dispatch cancellation check "
            "— this proves the real second-check race window is closed, "
            "not merely asserted from source"
        )
        assert result.preempted is True


class TestMixedSafetyAndComfortCycle:
    # Ticket §4.1: within one cycle carrying an executable safety intent,
    # the real call-site behavior — reused, not reimplemented here — must
    # be: an active PREVIOUS comfort plan is preempted, and _run_comfort_
    # dispatch_for_cycle() (which would start a brand-new comfort plan) is
    # never invoked for THIS cycle. Proven by monkeypatching _run_comfort_
    # dispatch_for_cycle() to raise if called at all, then exercising
    # exactly the call-site branch shape via _cycle_has_executable_safety_
    # intent() + _preempt_active_comfort_plan(), matching coordinator.py's
    # own `if _cycle_has_executable_safety: await self._preempt_active_
    # comfort_plan() else: ... _run_comfort_dispatch_for_cycle(...)`.
    def test_executable_safety_preempts_and_never_starts_new_comfort_plan(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        safety_state = _state("w_safety", "z1", entity_id="cover.safety", is_safety=True, filter_allowed=True)
        comfort_state = _state("w_comfort", "z1", entity_id="cover.comfort", is_safety=False, filter_allowed=True)

        async def _must_not_be_called(*a, **k):
            raise AssertionError(
                "_run_comfort_dispatch_for_cycle() must never be invoked "
                "for a cycle carrying an executable safety intent"
            )

        monkeypatch.setattr(coord, "_run_comfort_dispatch_for_cycle", _must_not_be_called)

        async def _run():
            cycle_has_safety = _cycle_has_executable_safety_intent(
                _ordered([safety_state, comfort_state])
            )
            assert cycle_has_safety is True
            if cycle_has_safety:
                await coord._preempt_active_comfort_plan()
            else:
                await coord._run_comfort_dispatch_for_cycle(
                    _ordered([comfort_state]), _harm_for(comfort_state),
                    datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), {"z1": 0}, {"w_comfort": 0},
                )

        asyncio.run(_run())  # raises via monkeypatch if the forbidden path is taken
        assert coord._dispatch_generation == 1


class TestBlockedSafetyIntentDoesNotPreempt:
    # Ticket §4.2: a theoretical safety state whose own Pass-1
    # exec_filter_result.allowed is False must NOT be treated as
    # executable — _cycle_has_executable_safety_intent() already proves
    # this at the pure-function level (SP-02); this test additionally
    # proves the actual call-site shape (reused verbatim) correctly skips
    # preemption and takes the normal comfort path instead.
    def test_blocked_safety_intent_leaves_comfort_path_untouched(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        blocked_safety_state = _state(
            "w_safety", "z1", entity_id="cover.safety", is_safety=True, filter_allowed=False,
        )
        comfort_state = _state("w_comfort", "z1", entity_id="cover.comfort", is_safety=False, filter_allowed=True)

        preempt_calls: list[str] = []
        comfort_calls: list[str] = []

        async def _fake_preempt():
            preempt_calls.append("preempt")
            return 1

        async def _fake_comfort(*a, **k):
            comfort_calls.append("comfort")
            return {}

        monkeypatch.setattr(coord, "_preempt_active_comfort_plan", _fake_preempt)
        monkeypatch.setattr(coord, "_run_comfort_dispatch_for_cycle", _fake_comfort)

        async def _run():
            cycle_has_safety = _cycle_has_executable_safety_intent(
                _ordered([blocked_safety_state, comfort_state])
            )
            assert cycle_has_safety is False
            if cycle_has_safety:
                await coord._preempt_active_comfort_plan()
            else:
                await coord._run_comfort_dispatch_for_cycle(
                    _ordered([comfort_state]), _harm_for(comfort_state),
                    datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), {"z1": 0}, {"w_comfort": 0},
                )

        asyncio.run(_run())
        assert preempt_calls == []
        assert comfort_calls == ["comfort"]


class TestExceptionInWindingDownPreviousPlanDoesNotBlockSafety:
    # Ticket §5.6: if the previous (winding-down) comfort plan's own task
    # raises an exception while cooperatively unwinding, that must not
    # propagate out of _preempt_active_comfort_plan() and must not prevent
    # safety from proceeding this cycle.
    def test_exception_in_previous_task_is_isolated(self) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation

        async def _raises_after_cancellation():
            await cancellation.wait()
            raise RuntimeError("simulated failure while winding down")

        async def _run():
            task = asyncio.ensure_future(_raises_after_cancellation())
            coord._active_comfort_dispatch_task = task
            return await asyncio.wait_for(coord._preempt_active_comfort_plan(), timeout=2.0)

        result_gen = asyncio.run(_run())
        assert result_gen == 1  # no exception propagated out


class TestCancellationEventNeverReusedAcrossCycles:
    # Audit item #10 ("dauerhaft gesetztes Event wird wiederverwendet"): a
    # cancellation Event that was permanently .set() for one comfort cycle
    # must never be handed to a LATER cycle's plan (a set Event would make
    # the later plan preempt itself instantly on its very first check).
    # Proven by running two real, independent comfort cycles back-to-back
    # and asserting the Event object identity differs, and that the
    # second cycle's Event is not already set when its own plan begins.
    def test_each_cycle_gets_its_own_fresh_unset_event(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=100)
        coord.windows = {"w1": s1.window}
        coord.zones = {"z1": s1.zone}
        from custom_components.smartshading.models.cover_group import CoverGroup
        coord.cover_groups = {"cg_w1": CoverGroup(id="cg_w1", window_id="w1", cover_ids=["cover.w1"])}
        coord._cover_capabilities["cover.w1"] = s1.exec_cap
        st = MagicMock()
        st.state = "open"
        st.attributes = {"current_position": 0}
        coord.hass.states.get = MagicMock(side_effect=lambda eid: {"cover.w1": st}.get(eid))

        seen_events: list = []

        async def fake_dispatch(hass, intent, *, now_utc):
            seen_events.append(coord._active_dispatch_cancellation)
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "dispatch_cover_intent", fake_dispatch,
        )
        real_asyncio = coord._predispatch_sequential_plan.__func__.__globals__["asyncio"]
        monkeypatch.setattr(real_asyncio, "sleep", lambda s: _real_sleep(0))

        async def _run():
            await coord._run_comfort_dispatch_for_cycle(
                _ordered([s1]), _harm_for(s1),
                datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), {"z1": 0}, {"w1": 0},
            )
            await coord._run_comfort_dispatch_for_cycle(
                _ordered([s1]), _harm_for(s1),
                datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), {"z1": 0}, {"w1": 0},
            )

        asyncio.run(_run())
        assert len(seen_events) == 2
        assert seen_events[0] is not seen_events[1], (
            "each comfort cycle must build its own fresh asyncio.Event() — "
            "reusing a previous (possibly already-.set()) Event would make "
            "a later cycle preempt itself instantly"
        )
        assert seen_events[0].is_set() is False
        assert seen_events[1].is_set() is False


class TestPresenceLogicNotSneakedIntoPhase5cCode:
    # Audit item #25 ("Presence-Logik wird eingeschlichen"): the two T22
    # Phase 5c-owned functions must stay fully independent of any
    # presence/occupancy concept per the ticket's explicit scope-out.
    def test_no_presence_reference_in_safety_preemption_functions(self) -> None:
        import re
        from pathlib import Path
        source = (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        for fn_name, end_marker in (
            ("_cycle_has_executable_safety_intent", "\ndef "),
            ("_preempt_active_comfort_plan", "\n    async def _run_comfort_dispatch_for_cycle"),
        ):
            start = source.index(f"def {fn_name}(")
            end = source.index(end_marker, start + 10)
            body = source[start:end]
            assert "presence" not in body.lower(), (
                f"{fn_name}() must stay fully independent of presence/"
                f"occupancy logic per the ticket's explicit scope-out — "
                f"found a 'presence' reference in its body"
            )


class TestComfortRestartsAfterSafetyPreemption:
    # Ticket §17: the NEXT cycle without an executable safety intent must
    # be able to start a brand-new, genuine comfort plan after a previous
    # preemption — no leftover registration blocks it.
    def test_comfort_plan_starts_fresh_after_a_prior_preemption(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        # Simulate a prior preemption with no active plan (SP-06 covers the
        # await/signal path; here we only need the generation bump + clean
        # registration state it leaves behind).
        asyncio.run(coord._preempt_active_comfort_plan())
        assert coord._active_comfort_dispatch_task is None
        assert coord._active_dispatch_cancellation is None
        gen_after_preemption = coord._dispatch_generation

        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=100)
        coord.windows = {"w1": s1.window}
        coord.zones = {"z1": s1.zone}
        from custom_components.smartshading.models.cover_group import CoverGroup
        coord.cover_groups = {"cg_w1": CoverGroup(id="cg_w1", window_id="w1", cover_ids=["cover.w1"])}
        coord._cover_capabilities["cover.w1"] = s1.exec_cap
        st = MagicMock()
        st.state = "open"
        st.attributes = {"current_position": 0}
        coord.hass.states.get = MagicMock(side_effect=lambda eid: {"cover.w1": st}.get(eid))

        dispatch_calls: list[str] = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "dispatch_cover_intent", fake_dispatch,
        )
        real_asyncio = coord._predispatch_sequential_plan.__func__.__globals__["asyncio"]
        monkeypatch.setattr(real_asyncio, "sleep", lambda s: _real_sleep(0))

        async def _run():
            return await coord._run_comfort_dispatch_for_cycle(
                _ordered([s1]), _harm_for(s1),
                datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), {"z1": 0}, {"w1": 0},
            )

        results = asyncio.run(_run())
        assert dispatch_calls == ["cover.w1"], (
            "a fresh comfort plan must be able to dispatch normally in the "
            "next cycle after a previous safety preemption left no stale "
            "registration behind"
        )
        assert coord._dispatch_generation == gen_after_preemption + 1
        assert coord._active_comfort_dispatch_task is None


class TestFinallyBlockNeverClobbersANewerRegistration:
    # Ticket §5.2 / audit item #9 ("aktive Task-Referenz wird vorzeitig
    # gelöscht"): _run_comfort_dispatch_for_cycle()'s own finally block
    # must clear self._active_comfort_dispatch_task / self._active_
    # dispatch_cancellation ONLY when they still identify THIS cycle's own
    # task/Event — never unconditionally. Proven by swapping in sentinel
    # "newer cycle" objects from inside the real dispatch_item port (i.e.
    # mid-flight, before this cycle's own finally runs) and asserting they
    # survive untouched afterward — a plain identity check on source code
    # cannot prove this, only exercising the real finally block can.
    def test_a_stale_finally_does_not_clear_a_newer_registration(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=100)
        coord.windows = {"w1": s1.window}
        coord.zones = {"z1": s1.zone}
        from custom_components.smartshading.models.cover_group import CoverGroup
        coord.cover_groups = {"cg_w1": CoverGroup(id="cg_w1", window_id="w1", cover_ids=["cover.w1"])}
        coord._cover_capabilities["cover.w1"] = s1.exec_cap
        st = MagicMock()
        st.state = "open"
        st.attributes = {"current_position": 0}
        coord.hass.states.get = MagicMock(side_effect=lambda eid: {"cover.w1": st}.get(eid))

        sentinel_task = object()
        sentinel_cancellation = object()

        async def fake_dispatch(hass, intent, *, now_utc):
            # Simulate a newer cycle having already raced ahead and
            # registered its own active task/cancellation by the time
            # THIS (older) cycle's own dispatch call is in flight.
            coord._active_comfort_dispatch_task = sentinel_task
            coord._active_dispatch_cancellation = sentinel_cancellation
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        monkeypatch.setitem(
            coord._predispatch_sequential_plan.__func__.__globals__,
            "dispatch_cover_intent", fake_dispatch,
        )
        real_asyncio = coord._predispatch_sequential_plan.__func__.__globals__["asyncio"]
        monkeypatch.setattr(real_asyncio, "sleep", lambda s: _real_sleep(0))

        asyncio.run(coord._run_comfort_dispatch_for_cycle(
            _ordered([s1]), _harm_for(s1),
            datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc), {"z1": 0}, {"w1": 0},
        ))
        assert coord._active_comfort_dispatch_task is sentinel_task, (
            "an older cycle's own finally block must never clear a newer "
            "cycle's active-task registration — it must only clear its own"
        )
        assert coord._active_dispatch_cancellation is sentinel_cancellation


class TestShutdownDuringSafetyPreemptionRace:
    # Ticket §5.5: coordinator shutdown happening concurrently with an
    # active safety preemption must not raise or leave the previous
    # comfort task orphaned — same cooperative-wind-down contract as
    # Phase 5b's TestShutdown, now driven via the safety-preemption entry
    # point instead of a second overlapping comfort cycle.
    def test_shutdown_concurrent_with_preemption_no_exception_no_orphan(self) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        cancellation = asyncio.Event()
        coord._active_dispatch_cancellation = cancellation

        async def _run_forever():
            await cancellation.wait()
            return "wound_down"

        async def _run():
            task = asyncio.ensure_future(_run_forever())
            coord._active_comfort_dispatch_task = task
            await asyncio.sleep(0)
            # Safety preemption and shutdown race concurrently.
            preempt_task = asyncio.ensure_future(coord._preempt_active_comfort_plan())
            shutdown_task = asyncio.ensure_future(coord.async_shutdown())
            await asyncio.wait_for(asyncio.gather(preempt_task, shutdown_task), timeout=2.0)
            assert task.done()
            assert task.cancelled() is False

        asyncio.run(_run())  # must not raise


class TestSafetyPreemptedSurvivesIntoDiagnostics:
    # Ticket §6: ExecutionResult.reason == "safety_preempted" must survive
    # into _record_decision_trace()'s no_dispatch.primary_reason (feeding
    # Support Export's recent_no_dispatches) and into _record_support_
    # event()'s own primary reason — not collapse into the generic
    # "dispatch_not_required" fallback, which would make a real safety
    # preemption diagnostically indistinguishable from an ordinary no-op
    # cycle.
    def test_decision_trace_primary_reason_is_safety_preempted(self) -> None:
        from custom_components.smartshading.cover_control.execution_result import (
            build_not_attempted_result,
        )
        from custom_components.smartshading.models.decision_provenance import DispatchProvenance
        from custom_components.smartshading.cover_control.execution_plan import (
            CoverIntent, CoverCommandType,
        )
        from custom_components.smartshading.cover_control.command_filter import ExecutionMode

        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s = _state("w1", "z1", entity_id="cover.w1")
        # decision_id is required for _record_decision_trace()'s own
        # materiality gate (only material decisions get traced).
        s.decision_id = "d1"

        intent = CoverIntent(
            cover_entity_id="cover.w1", command_type=CoverCommandType.MOVE_TO_POSITION,
            target_position_internal=0, target_position_ha=100, target_tilt=None,
            is_safety=False, execution_mode=ExecutionMode.AUTOMATIC.value,
            allowed=True, blocked_reason=None, decided_by="TestEvaluator",
            computed_at=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
        )
        exec_result = build_not_attempted_result(intent, reason="safety_preempted")
        dispatch_prov = DispatchProvenance(
            dispatch_allowed=True, dispatch_filter_reason=None,
            dispatch_attempted=False, dispatch_succeeded=None,
        )
        harm = _harm_for(s)["w1"]
        coord._record_decision_trace("w1", s, harm, dispatch_prov, exec_result, _NOW := datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))
        snap = coord.decision_trace_snapshot()
        rec = snap["z1"]["records"][-1]
        assert rec["no_dispatch"]["primary_reason"] == "safety_preempted", (
            "a safety-preempted comfort item must surface 'safety_preempted' "
            "in Decision Trace no_dispatch.primary_reason, not the generic "
            "'dispatch_not_required' fallback"
        )
        assert rec["no_dispatch"]["command_sent"] is False

    def test_support_event_primary_reason_is_safety_preempted(self) -> None:
        from custom_components.smartshading.cover_control.execution_result import (
            build_not_attempted_result,
        )
        from custom_components.smartshading.models.decision_provenance import DispatchProvenance
        from custom_components.smartshading.cover_control.execution_plan import (
            CoverIntent, CoverCommandType,
        )
        from custom_components.smartshading.cover_control.command_filter import ExecutionMode

        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s = _state("w1", "z1", entity_id="cover.w1")

        intent = CoverIntent(
            cover_entity_id="cover.w1", command_type=CoverCommandType.MOVE_TO_POSITION,
            target_position_internal=0, target_position_ha=100, target_tilt=None,
            is_safety=False, execution_mode=ExecutionMode.AUTOMATIC.value,
            allowed=True, blocked_reason=None, decided_by="TestEvaluator",
            computed_at=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
        )
        exec_result = build_not_attempted_result(intent, reason="safety_preempted")
        dispatch_prov = DispatchProvenance(
            dispatch_allowed=True, dispatch_filter_reason=None,
            dispatch_attempted=False, dispatch_succeeded=None,
        )
        before = len(coord._support_critical_events)
        coord._record_support_event(
            "w1", s, dispatch_prov, datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
            last_exec_result=exec_result, disp_ctx={},
        )
        assert len(coord._support_critical_events) == before + 1
        evt = coord._support_critical_events[-1]
        assert evt.get("reason") == "safety_preempted", (
            f"support event must carry the safety_preempted reason, got: {evt}"
        )


class TestCallSiteActuallyChecksSafetyCondition:
    def test_safety_branch_condition_is_the_real_check(self) -> None:
        # Structural: the call site's branch must be gated on the ACTUAL
        # _cycle_has_executable_safety value, not merely have the right
        # if/else shape with a neutralized condition (e.g. `if False:`) —
        # a defect the general if/else-shape check alone cannot catch.
        from pathlib import Path
        source = (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        assert "if _cycle_has_executable_safety:" in source, (
            "the comfort-dispatch call site must branch on the real "
            "_cycle_has_executable_safety value — found no such condition, "
            "meaning the safety-preemption branch may have been "
            "neutralized (e.g. replaced with `if False:`)."
        )
