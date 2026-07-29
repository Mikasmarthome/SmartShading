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
