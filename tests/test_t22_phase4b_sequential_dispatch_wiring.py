"""Behavioral tests for T22 Phase 4b — coordinator.py's
_predispatch_sequential_plan(), the DispatchPlanExecutor-routed SEQUENTIAL/
SPACED comfort dispatch pre-pass.

Same real-SmartShadingCoordinator-with-HA-stubs technique as
test_coordinator_parallel_dispatch.py (T11.1) — dispatch_cover_intent() and
wait_for_travel_completion() are patched at the coordinator module level so
tests control exactly when each "service call"/"completion" resolves,
without any real HA services or real sleeps.

Coverage:
  SDW-01  A single comfort FULL_OPEN item dispatches via the new path.
  SDW-02  Safety items are excluded from the plan entirely (empty result —
          they fall through to the legacy lock block instead).
  SDW-03  An item already at its target (NO_MOVEMENT-classified) is excluded
          from the plan — falls through to the legacy path rather than
          being silently skipped (T22 Phase 4b's own bug-fix regression).
  SDW-04  The global lock is NOT held during the completion wait (the
          actual point of Phase 4b — fixes the legacy lock-span bug).
  SDW-05  The global lock IS held during the dispatch call itself.
  SDW-06  PARALLEL mode: pre-pass is a no-op (empty dict), does not touch
          dispatch_cover_intent at all.
  SDW-07  No dispatchable items -> empty result, executor never runs.
  SDW-08  A stale generation (bumped before this item's dispatch turn)
          produces a stale_presence_superseded NOT_ATTEMPTED result.
  SDW-09  Structural: the per-window loop's SEQUENTIAL/SPACED comfort
          routing branch is gated on plan MEMBERSHIP (`in _sequential_
          results`), never merely `not is_safety` — a regression here
          would KeyError on any comfort item the pre-pass legitimately
          excluded (e.g. a NO_MOVEMENT-classified item), a defect no
          behavioral test here can reach without driving the full,
          600+-line _async_update_data() cycle end-to-end (out of scope
          for this file — see SDW-03's isolated pre-pass-level coverage
          of the exclusion itself). Same established pattern as
          test_dispatch_safety_generation_exemption_structural.py.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
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
            # target_position_internal must stay consistent with
            # target_position_ha (100 - ha, no invert) — T22 Phase 5a live
            # validation compares live current_position_internal against
            # this same value, so a decoupled hardcoded internal target
            # would silently defeat same-position detection in tests.
            target_position_internal=100 - target_position_ha,
            target_position_ha=target_position_ha,
            execution_mode=ExecutionMode.AUTOMATIC.value, is_safety=is_safety,
        ),
        tier_decided_by="TestEvaluator",
        is_override_active=False,
        cover_available=True,
    )


def _setup_coord(
    coord: SmartShadingCoordinator, states: list[_WindowComputeState],
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
    # T22 Phase 5a: validate_item() re-fetches a FRESH snapshot via
    # self._build_cover_entity_snapshot_for_window(), which reads
    # self.hass.states.get() and self._get_or_detect_capability() — both
    # must return something usable, or every item is skipped as
    # cover_unavailable before dispatch is ever reached.
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


def _ordered(states: list[_WindowComputeState]):
    return [(s.window.id, s) for s in states]


def _harm(states: list[_WindowComputeState]) -> dict:
    return {
        s.window.id: HarmonizationResult(
            harmonized=False, final_target_position_ha=None,
            pre_harmonization_target_position_ha=None,
        )
        for s in states
    }


def _zone_maps(states: list[_WindowComputeState]):
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


class TestBasicDispatch:
    def test_single_comfort_item_dispatches(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: asyncio.sleep(0))

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([s1])
            return await coord._predispatch_sequential_plan(
                _ordered([s1]), _harm([s1]), _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        results = asyncio.run(_run())
        assert calls == ["cover.w1"]
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT
        # B3-012: the real coordinator wiring records the classification
        # this item was actually dispatched under (target_position_ha=100
        # -> FULL_OPEN, see _state()'s own default).
        assert results[("w1", "cover.w1")].dispatch_target_class == "full_open"

    def test_full_close_target_is_classified_and_dispatched_like_full_open(
        self, monkeypatch,
    ) -> None:
        # B3-012: a 0% target must reach the same fast dispatch path as a
        # 100% target -- proven here via the real coordinator wiring
        # (target_position_ha=0 -> FULL_CLOSE, not INTERMEDIATE).
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=0, current_position_ha=100)
        _setup_coord(coord, [s1])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: asyncio.sleep(0))

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([s1])
            return await coord._predispatch_sequential_plan(
                _ordered([s1]), _harm([s1]), _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        results = asyncio.run(_run())
        assert calls == ["cover.w1"]
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT
        assert results[("w1", "cover.w1")].dispatch_target_class == "full_close"


class TestSafetyExcluded:
    def test_safety_item_never_in_plan(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", is_safety=True)
        _setup_coord(coord, [s1])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([s1])
            return await coord._predispatch_sequential_plan(
                _ordered([s1]), _harm([s1]), _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        results = asyncio.run(_run())
        assert results == {}
        assert calls == [], "safety must never be dispatched by this pre-pass"


class TestNoMovementExcluded:
    def test_already_at_target_item_not_in_plan(self, monkeypatch) -> None:
        # target_position_ha == current_position_ha -> classify_cover_intent
        # returns NO_MOVEMENT -> must NOT enter the plan (falls through to
        # the legacy path instead of being silently skipped here).
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state(
            "w1", "z1", entity_id="cover.w1",
            target_position_ha=40, current_position_ha=40,
        )
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 40})
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([s1])
            return await coord._predispatch_sequential_plan(
                _ordered([s1]), _harm([s1]), _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        results = asyncio.run(_run())
        assert results == {}
        assert calls == []


class TestLockOwnership:
    def test_lock_not_held_during_completion_wait(self, monkeypatch) -> None:
        # INTERMEDIATE target (not full-open) — only INTERMEDIATE items wait
        # for completion under the T22 target-aware policy; FULL_OPEN never
        # does (Phase 3 design), so a FULL_OPEN target wouldn't exercise the
        # completion-wait code path this test targets at all.
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        _setup_coord(coord, [s1])

        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: asyncio.sleep(0))

        lock_held_during_completion = {"value": None}

        def _record():
            lock_held_during_completion["value"] = coord._serial_dispatch.lock.locked()

        _patch_wait_for_travel_completion(
            monkeypatch, coord, _fake_completion_factory(on_call=_record)
        )

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([s1])
            await coord._predispatch_sequential_plan(
                _ordered([s1]), _harm([s1]), _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        asyncio.run(_run())
        assert lock_held_during_completion["value"] is False, (
            "T22 Phase 4b's whole point: the lock must be released before "
            "the completion wait begins, unlike the legacy block it replaces."
        )

    def test_lock_is_held_during_dispatch_call(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])

        lock_held_during_dispatch = {"value": None}

        async def fake(hass, intent, *, now_utc):
            lock_held_during_dispatch["value"] = coord._serial_dispatch.lock.locked()
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: asyncio.sleep(0))

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([s1])
            await coord._predispatch_sequential_plan(
                _ordered([s1]), _harm([s1]), _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        asyncio.run(_run())
        assert lock_held_during_dispatch["value"] is True


class TestParallelModeNoOp:
    def test_parallel_mode_returns_empty_and_never_dispatches(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([s1])
            return await coord._predispatch_sequential_plan(
                _ordered([s1]), _harm([s1]), _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        results = asyncio.run(_run())
        assert results == {}
        assert calls == []


class TestEmptyPlan:
    def test_no_states_returns_empty(self) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        _setup_coord(coord, [])

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([])
            return await coord._predispatch_sequential_plan(
                [], {}, _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        results = asyncio.run(_run())
        assert results == {}


class TestStaleGeneration:
    def test_stale_generation_before_dispatch_yields_not_attempted(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])

        async def fake(hass, intent, *, now_utc):
            raise AssertionError("must not dispatch a stale-generation item")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)

        # this_dispatch_gen (arg) = 0, but coordinator's live generation is 1
        # -> is_generation_current returns False for every item immediately.
        coord._dispatch_generation = 1

        async def _run():
            zone_order, window_order_in_zone = _zone_maps([s1])
            return await coord._predispatch_sequential_plan(
                _ordered([s1]), _harm([s1]), _NOW, 0, zone_order, window_order_in_zone,
                asyncio.Event(),
            )

        results = asyncio.run(_run())
        assert results[("w1", "cover.w1")].status is ExecutionStatus.NOT_ATTEMPTED
        # T22 Phase 5a fix: the coordinator now surfaces the EXECUTOR's own
        # per-item reason ("stale_generation", DispatchPlanExecutor's own
        # is_generation_current-skip label) instead of a blanket
        # "stale_presence_superseded" fallback that used to mask every
        # never-dispatched-item cause (including later, real validate_item
        # skips like already_at_target/manual_override) under one string.
        assert results[("w1", "cover.w1")].reason == "stale_generation"


class TestOuterLoopRoutingIsMembershipGated:
    def test_routing_condition_checks_plan_membership(self) -> None:
        import re
        from pathlib import Path
        source = (
            Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        pattern = re.compile(
            r"elif \(\s*\n\s*window_id, _intent\.cover_entity_id\s*\n\s*\) in _sequential_results:",
        )
        assert pattern.search(source), (
            "The per-window loop's SEQUENTIAL/SPACED comfort routing branch "
            "must remain gated on membership in _sequential_results (i.e. "
            "the pre-pass actually included this item), never merely "
            "`not is_safety` — items the pre-pass legitimately excludes "
            "(e.g. NO_MOVEMENT-classified comfort items) would otherwise "
            "KeyError or be mishandled. If this branch's shape genuinely "
            "changed, update this test's pattern to match the new "
            "(still-membership-gated) structure rather than removing the "
            "check."
        )
