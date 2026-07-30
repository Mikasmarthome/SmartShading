"""Behavioral tests for genuine concurrent (PARALLEL) dispatch —
v1.2.0-beta.1, T11.1 — coordinator.py's _predispatch_parallel_batches() /
_dispatch_one_parallel_item().

Same real-SmartShadingCoordinator-with-HA-stubs technique as
test_coordinator_dispatch_config_wiring.py. dispatch_cover_intent() is
patched at the coordinator module level so tests control exactly when each
"service call" resolves, without any real HA services.

Coverage:
  PDW-01  Two dispatches genuinely overlap: the second STARTS before an
          artificially-blocked first one completes — proof this is not the
          old sequential-await loop.
  PDW-02  Three+ covers dispatch concurrently as one batch.
  PDW-03  No artificial start interval between concurrently-dispatched
          items (elapsed time approx 0, not N * start_interval_s).
  PDW-04  Safety items form their own fast-lane batch, dispatched before
          non-safety items, never delayed behind them.
  PDW-05  zone_batching: a deterministic boundary gap is enforced between
          zone batches; no such gap without zone_batching.
  PDW-06  One item's FAILED result does not lose or corrupt the others'
          results (structured per-item outcome, not a swallowed exception).
  PDW-07  A generation change between batches cancels only NOT-YET-
          dispatched non-safety items in later batches; safety is exempt.
  PDW-08  Every dispatched item carries parallel_batch_id/parallel_batch_size.
  PDW-09  No dispatchable items -> empty result, no batches attempted.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_coordinator_dispatch_config_wiring.py.
# ---------------------------------------------------------------------------


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
from custom_components.smartshading.cover_control.execution_result import ExecutionStatus  # noqa: E402
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
    # These tests exercise steady-state dispatch, not the first-cycle
    # startup-grace suppression window (STARTUP_GRACE_CYCLES) — advance past
    # it so non-safety intents are not universally skipped.
    coord._startup_cycles_remaining = 0
    return coord


def _patch_dispatch_cover_intent(monkeypatch, coord, fake):
    """Patch dispatch_cover_intent in the EXACT module coord's own bound
    methods read from — resolved via the running method's own __globals__,
    not the name captured at this test file's import time. Multiple test
    files in this suite each pop-and-reimport coordinator.py with their own
    HA stub set; a module-level name captured at collection time can end up
    stale by the time another file's reimport runs (see
    test_coordinator_override_release_strategy_wiring.py for the earlier
    occurrence of this exact class of bug and its fix)."""
    monkeypatch.setitem(
        coord._predispatch_parallel_batches.__func__.__globals__,
        "dispatch_cover_intent", fake,
    )


def _patch_asyncio_sleep(monkeypatch, coord, fake):
    """Same rationale as _patch_dispatch_cover_intent — patch the asyncio
    module object the running method actually reads `asyncio.sleep` from."""
    real_asyncio = coord._predispatch_parallel_batches.__func__.__globals__["asyncio"]
    monkeypatch.setattr(real_asyncio, "sleep", fake)


def _window(window_id: str, zone_id: str) -> WindowConfig:
    return WindowConfig(
        id=window_id, name=window_id, zone_id=zone_id,
        azimuth=180, floor_level=0, cover_group_id=f"cg_{window_id}",
    )


def _state(window_id: str, zone_id: str, *, entity_id: str, is_safety: bool = False) -> _WindowComputeState:
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
        exec_target_internal=30,
        exec_filter_result=CommandFilterResult(
            allowed=True, blocked_reason=None,
            target_position_internal=30, target_position_ha=70,
            execution_mode=ExecutionMode.AUTOMATIC.value, is_safety=is_safety,
        ),
        tier_decided_by="TestEvaluator",
        is_override_active=False,
        cover_available=True,
    )


def _setup_coord(coord: SmartShadingCoordinator, states: list[_WindowComputeState]) -> None:
    coord.windows = {s.window.id: s.window for s in states}
    coord.zones = {s.zone.id: s.zone for s in states}
    coord.cover_groups = {
        s.window.cover_group_id: CoverGroup(
            id=s.window.cover_group_id, window_id=s.window.id, cover_ids=[s.exec_entity_id],
        )
        for s in states
    }


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


_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


class TestGenuineConcurrency:
    def test_second_dispatch_starts_before_first_blocked_call_completes(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        s2 = _state("w2", "z1", entity_id="cover.w2")
        _setup_coord(coord, [s1, s2])

        gate = asyncio.Event()
        started = []

        async def fake_dispatch(hass, intent, *, now_utc):
            started.append(intent.cover_entity_id)
            if intent.cover_entity_id == "cover.w1":
                await gate.wait()  # blocks until released
            from custom_components.smartshading.cover_control.execution_result import build_sent_result
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        async def run():
            task = asyncio.create_task(
                coord._predispatch_parallel_batches(_ordered([s1, s2]), _harm([s1, s2]), _NOW, 0)
            )
            # Give the event loop a chance to schedule both concurrent dispatches.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert set(started) == {"cover.w1", "cover.w2"}, (
                "Both dispatches must have STARTED before the blocked one "
                "resolves — proof of genuine concurrency, not sequential await."
            )
            gate.set()
            results = await task
            return results

        results = asyncio.run(run())
        assert len(results) == 2
        assert all(r.status is ExecutionStatus.SENT for r in results.values())


class TestThreeOrMoreConcurrent:
    def test_three_covers_dispatch_in_one_batch(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        states = [_state(f"w{i}", "z1", entity_id=f"cover.w{i}") for i in range(3)]
        _setup_coord(coord, states)

        from custom_components.smartshading.cover_control.execution_result import build_sent_result
        call_order = []

        async def fake_dispatch(hass, intent, *, now_utc):
            call_order.append(intent.cover_entity_id)
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        results = asyncio.run(
            coord._predispatch_parallel_batches(_ordered(states), _harm(states), _NOW, 0)
        )
        assert len(results) == 3
        assert len(call_order) == 3
        assert all(r.status is ExecutionStatus.SENT for r in results.values())


class TestNoArtificialStartInterval:
    def test_no_delay_between_starts(self, monkeypatch) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(
                mode=DispatchMode.PARALLEL, start_interval_s=5.0,  # would be huge if applied
            )
        )
        states = [_state(f"w{i}", "z1", entity_id=f"cover.w{i}") for i in range(3)]
        _setup_coord(coord, states)

        from custom_components.smartshading.cover_control.execution_result import build_sent_result

        async def fake_dispatch(hass, intent, *, now_utc):
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        real_sleep = asyncio.sleep
        sleep_calls = []

        async def spying_sleep(s):
            sleep_calls.append(s)
            await real_sleep(0)

        _patch_asyncio_sleep(monkeypatch, coord, spying_sleep)

        results = asyncio.run(
            coord._predispatch_parallel_batches(_ordered(states), _harm(states), _NOW, 0)
        )
        assert len(results) == 3, "sanity: items were actually dispatched, not silently skipped"
        # zone_batching is off (default) — no boundary sleeps at all for a
        # single non-safety batch.
        assert sleep_calls == []


class TestSafetyFastLane:
    def test_safety_dispatched_separately_from_non_safety(self, monkeypatch) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL, zone_batching=True)
        )
        safety = _state("w_safety", "z1", entity_id="cover.safety", is_safety=True)
        normal = _state("w_normal", "z2", entity_id="cover.normal")
        _setup_coord(coord, [safety, normal])

        from custom_components.smartshading.cover_control.execution_result import build_sent_result
        batch_ids_seen = {}

        async def fake_dispatch(hass, intent, *, now_utc):
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        results = asyncio.run(
            coord._predispatch_parallel_batches(
                _ordered([safety, normal]), _harm([safety, normal]), _NOW, 0,
            )
        )
        safety_result = results[("w_safety", "cover.safety")]
        normal_result = results[("w_normal", "cover.normal")]
        assert safety_result.parallel_batch_id == "safety"
        assert normal_result.parallel_batch_id != "safety"


class TestZoneBatchingBoundary:
    def test_boundary_gap_enforced_between_zones(self, monkeypatch) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(
                mode=DispatchMode.PARALLEL, zone_batching=True, start_interval_s=2.0,
            )
        )
        w1 = _state("w1", "z1", entity_id="cover.w1")
        w2 = _state("w2", "z2", entity_id="cover.w2")
        _setup_coord(coord, [w1, w2])

        from custom_components.smartshading.cover_control.execution_result import build_sent_result

        async def fake_dispatch(hass, intent, *, now_utc):
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        # Pre-arm the throttle as if a dispatch JUST happened, so the zone
        # boundary wait is guaranteed non-zero and observable.
        coord._serial_dispatch.record_dispatch(_NOW)

        real_sleep = asyncio.sleep
        sleeps = []

        async def spying_sleep(s):
            sleeps.append(s)
            await real_sleep(0)

        _patch_asyncio_sleep(monkeypatch, coord, spying_sleep)

        asyncio.run(
            coord._predispatch_parallel_batches(_ordered([w1, w2]), _harm([w1, w2]), _NOW, 0)
        )
        assert len(sleeps) == 1
        assert sleeps[0] > 0

    def test_no_boundary_gap_without_zone_batching(self, monkeypatch) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(
                mode=DispatchMode.PARALLEL, zone_batching=False, start_interval_s=2.0,
            )
        )
        w1 = _state("w1", "z1", entity_id="cover.w1")
        w2 = _state("w2", "z2", entity_id="cover.w2")
        _setup_coord(coord, [w1, w2])

        from custom_components.smartshading.cover_control.execution_result import build_sent_result

        async def fake_dispatch(hass, intent, *, now_utc):
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)
        coord._serial_dispatch.record_dispatch(_NOW)

        real_sleep = asyncio.sleep
        sleeps = []

        async def spying_sleep(s):
            sleeps.append(s)
            await real_sleep(0)

        _patch_asyncio_sleep(monkeypatch, coord, spying_sleep)

        results = asyncio.run(
            coord._predispatch_parallel_batches(_ordered([w1, w2]), _harm([w1, w2]), _NOW, 0)
        )
        assert len(results) == 2, "sanity: items were actually dispatched, not silently skipped"
        assert sleeps == []


class TestErrorIsolation:
    def test_one_failed_result_does_not_lose_others(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        states = [_state(f"w{i}", "z1", entity_id=f"cover.w{i}") for i in range(3)]
        _setup_coord(coord, states)

        from custom_components.smartshading.cover_control.execution_result import (
            build_failed_result, build_sent_result,
        )

        async def fake_dispatch(hass, intent, *, now_utc):
            if intent.cover_entity_id == "cover.w1":
                return build_failed_result(
                    intent, error="simulated failure", sent_at_utc=now_utc, reason="test failure",
                )
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        results = asyncio.run(
            coord._predispatch_parallel_batches(_ordered(states), _harm(states), _NOW, 0)
        )
        assert len(results) == 3
        assert results[("w1", "cover.w1")].status is ExecutionStatus.FAILED
        assert results[("w0", "cover.w0")].status is ExecutionStatus.SENT
        assert results[("w2", "cover.w2")].status is ExecutionStatus.SENT

    def test_unexpected_exception_in_wrapper_does_not_lose_others(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        states = [_state(f"w{i}", "z1", entity_id=f"cover.w{i}") for i in range(2)]
        _setup_coord(coord, states)

        from custom_components.smartshading.cover_control.execution_result import build_sent_result

        async def flaky_dispatch(hass, intent, *, now_utc):
            if intent.cover_entity_id == "cover.w0":
                raise RuntimeError("boom")
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, flaky_dispatch)

        results = asyncio.run(
            coord._predispatch_parallel_batches(_ordered(states), _harm(states), _NOW, 0)
        )
        assert len(results) == 2
        assert results[("w0", "cover.w0")].status is ExecutionStatus.NOT_ATTEMPTED
        assert "parallel_dispatch_error" in results[("w0", "cover.w0")].reason
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT


class TestGenerationPreemption:
    def test_generation_change_cancels_only_non_safety(self, monkeypatch) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL, zone_batching=True)
        )
        safety = _state("w_safety", "z1", entity_id="cover.safety", is_safety=True)
        normal = _state("w_normal", "z2", entity_id="cover.normal")
        _setup_coord(coord, [safety, normal])

        from custom_components.smartshading.cover_control.execution_result import build_sent_result

        async def fake_dispatch(hass, intent, *, now_utc):
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        # Simulate the generation having ALREADY changed before this pass runs
        # (as if a presence/safety event fired mid-cycle before we started).
        coord._dispatch_generation = 5
        this_dispatch_gen = 1  # stale relative to current generation

        results = asyncio.run(
            coord._predispatch_parallel_batches(
                _ordered([safety, normal]), _harm([safety, normal]), _NOW, this_dispatch_gen,
            )
        )
        assert results[("w_safety", "cover.safety")].status is ExecutionStatus.SENT
        assert results[("w_normal", "cover.normal")].status is ExecutionStatus.NOT_ATTEMPTED
        assert results[("w_normal", "cover.normal")].reason == "stale_presence_superseded"


class TestBatchDiagnosticsFields:
    def test_every_result_carries_batch_id_and_size(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        states = [_state(f"w{i}", "z1", entity_id=f"cover.w{i}") for i in range(2)]
        _setup_coord(coord, states)

        from custom_components.smartshading.cover_control.execution_result import build_sent_result

        async def fake_dispatch(hass, intent, *, now_utc):
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        results = asyncio.run(
            coord._predispatch_parallel_batches(_ordered(states), _harm(states), _NOW, 0)
        )
        assert len(results) == 2, "sanity: items were actually dispatched, not silently skipped"
        for result in results.values():
            assert result.parallel_batch_id is not None
            assert result.parallel_batch_size == 2


class TestNoDispatchableItems:
    def test_empty_result_when_nothing_eligible(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        # Make the intent BLOCKED (not allowed) — nothing eligible.
        s1 = _WindowComputeState(
            **{**s1.__dict__, "exec_filter_result": CommandFilterResult(
                allowed=False, blocked_reason="test_blocked",
                target_position_internal=30, target_position_ha=70,
                execution_mode=ExecutionMode.AUTOMATIC.value, is_safety=False,
            )}
        )
        _setup_coord(coord, [s1])

        called = False

        async def fake_dispatch(hass, intent, *, now_utc):
            nonlocal called
            called = True
            raise AssertionError("should never be called")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        results = asyncio.run(
            coord._predispatch_parallel_batches(_ordered([s1]), _harm([s1]), _NOW, 0)
        )
        assert results == {}
        assert called is False


class TestParallelSafetyPreemption:
    # T22 PARALLEL safety-preemption fix: an executable safety intent this
    # cycle must still dispatch via its own fast-lane batch, but no
    # non-safety batch may start at all — mirroring the SEQUENTIAL/SPACED
    # safety_preempted pattern (Phase 5c), reusing the SAME
    # cycle_has_executable_safety snapshot, no new mechanism.
    def test_non_safety_batch_skipped_safety_batch_unaffected(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        safety = _state("w_safety", "z1", entity_id="cover.safety", is_safety=True)
        comfort = _state("w_comfort", "z1", entity_id="cover.comfort")
        _setup_coord(coord, [safety, comfort])

        from custom_components.smartshading.cover_control.execution_result import build_sent_result
        dispatch_calls: list[str] = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        results = asyncio.run(
            coord._predispatch_parallel_batches(
                _ordered([safety, comfort]), _harm([safety, comfort]), _NOW, 0,
                cycle_has_executable_safety=True,
            )
        )
        assert dispatch_calls == ["cover.safety"], (
            "safety must dispatch normally via its own fast-lane batch "
            "even when cycle_has_executable_safety is True (that flag "
            "describes THIS safety intent's own presence, not a reason "
            "to block it)"
        )
        assert results[("w_safety", "cover.safety")].status is ExecutionStatus.SENT
        comfort_result = results[("w_comfort", "cover.comfort")]
        assert comfort_result.status is ExecutionStatus.NOT_ATTEMPTED
        assert comfort_result.reason == "safety_preempted"

    def test_multiple_non_safety_zone_batches_all_skipped(self, monkeypatch) -> None:
        coord = _make_coord(
            dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL, zone_batching=True)
        )
        w1 = _state("w1", "z1", entity_id="cover.w1")
        w2 = _state("w2", "z2", entity_id="cover.w2")
        _setup_coord(coord, [w1, w2])

        dispatch_calls: list[str] = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            raise AssertionError("must never be called once preempted")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)
        sleep_calls: list[float] = []

        async def spying_sleep(s):
            sleep_calls.append(s)

        _patch_asyncio_sleep(monkeypatch, coord, spying_sleep)

        results = asyncio.run(
            coord._predispatch_parallel_batches(
                _ordered([w1, w2]), _harm([w1, w2]), _NOW, 0,
                cycle_has_executable_safety=True,
            )
        )
        assert dispatch_calls == [], "no batch may start a service call once preempted"
        assert sleep_calls == [], "no zone-boundary sleep for a batch that never starts"
        assert results[("w1", "cover.w1")].reason == "safety_preempted"
        assert results[("w2", "cover.w2")].reason == "safety_preempted"

    def test_no_executable_safety_dispatches_normally(self, monkeypatch) -> None:
        # Regression guard: the default (cycle_has_executable_safety=False)
        # must reproduce the exact pre-fix behavior — nothing skipped.
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.PARALLEL))
        comfort = _state("w_comfort", "z1", entity_id="cover.comfort")
        _setup_coord(coord, [comfort])

        from custom_components.smartshading.cover_control.execution_result import build_sent_result
        dispatch_calls: list[str] = []

        async def fake_dispatch(hass, intent, *, now_utc):
            dispatch_calls.append(intent.cover_entity_id)
            return build_sent_result(intent, sent_at_utc=now_utc, reason="test")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake_dispatch)

        results = asyncio.run(
            coord._predispatch_parallel_batches(
                _ordered([comfort]), _harm([comfort]), _NOW, 0,
            )
        )
        assert dispatch_calls == ["cover.comfort"]
        assert results[("w_comfort", "cover.comfort")].status is ExecutionStatus.SENT
