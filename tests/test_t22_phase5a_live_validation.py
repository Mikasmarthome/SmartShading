"""Behavioral tests for T22 Phase 5a — coordinator.py's real validate_item()
live revalidation, called by DispatchPlanExecutor immediately before each
comfort dispatch (via _predispatch_sequential_plan()).

Same self-contained real-SmartShadingCoordinator-with-HA-stubs technique as
test_t22_phase4b_sequential_dispatch_wiring.py (duplicated here rather than
imported — cross-test-file imports are not a pattern this codebase's test
suite uses, and the dependency-contract test flags them).

Coverage:
  LV-01  Same position (within tolerance) -> skip, already_at_target.
  LV-02  Just outside tolerance -> still executes.
  LV-03  Target 0 preserved (no truthiness loss) through the live check.
  LV-04  Unknown current position never auto-skips.
  LV-05  Cover unavailable -> skip, cover_unavailable, no dispatch.
  LV-06  Manual override active (live) -> skip, manual_override, no dispatch.
  LV-07  Comfort position hold active (live) -> skip, comfort_position_hold.
  LV-08  Night contact hold (Pass-1 mirror) -> skip, night_hold.
  LV-09  Already moving toward target (consistent direction) -> skip.
  LV-10  Moving in the OPPOSITE direction -> does NOT skip.
  LV-11  A blocker on one cover does not affect another cover's dispatch.
  LV-12  Blocker cleared before dispatch -> item still executes.
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


def _run_plan(coord, states, **setup_kwargs):
    zone_order, window_order_in_zone = _zone_maps(states)
    return coord._predispatch_sequential_plan(
        _ordered(states), _harm(states), _NOW, 0, zone_order, window_order_in_zone,
    )


class TestSamePosition:
    def test_within_tolerance_skips(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 41})
        # CommandFilter (both Pass-1 and this live re-check) compares
        # against AssumedStateManager's assumed_position_internal, not the
        # raw HA current_position attribute directly — seed it to match
        # the live HA position (41 -> internal 59) for a meaningful check.
        coord.assumed_state_manager.update("cover.w1", 59, _NOW, has_reliable_position_feedback=True)
        calls = []
        _patch_dispatch_cover_intent(
            monkeypatch, coord,
            lambda hass, intent, *, now_utc: calls.append(1) or _fake_sent_dispatch(hass, intent, now_utc=now_utc),
        )
        results = asyncio.run(_run_plan(coord, [s1]))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.NOT_ATTEMPTED
        assert results[("w1", "cover.w1")].reason == "already_at_target"
        assert calls == []

    def test_just_outside_tolerance_still_executes(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        # tolerance is 3 internal units; use a position clearly far enough
        # away that internal-unit conversion still exceeds it.
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 10})
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))
        results = asyncio.run(_run_plan(coord, [s1]))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT

    def test_target_zero_preserved(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        # current_position_ha=50 on the frozen Pass-1 snapshot so the item
        # is classified as movement-required (target 0 != 50) and enters
        # the plan at all; the live re-fetch below (80) is what
        # validate_item's own same-position re-check actually exercises.
        s1 = _state(
            "w1", "z1", entity_id="cover.w1",
            target_position_ha=0, current_position_ha=50,
        )
        _setup_coord(coord, [s1], current_position_by_entity={"cover.w1": 80})
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))
        results = asyncio.run(_run_plan(coord, [s1]))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT
        assert results[("w1", "cover.w1")].target_position_ha == 0

    def test_unknown_current_position_does_not_auto_skip(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        _setup_coord(coord, [s1])
        # ha_state "open" but no current_position attribute at all -> unknown.
        coord.hass.states.get = MagicMock(
            side_effect=lambda eid: _blank_state("open") if eid == "cover.w1" else None
        )
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))
        results = asyncio.run(_run_plan(coord, [s1]))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT


def _blank_state(state: str) -> MagicMock:
    st = MagicMock()
    st.state = state
    st.attributes = {}
    return st


class TestAvailability:
    def test_unavailable_skips_no_dispatch(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1], ha_state="unavailable")
        calls = []
        _patch_dispatch_cover_intent(
            monkeypatch, coord,
            lambda hass, intent, *, now_utc: (_ for _ in ()).throw(
                AssertionError("must not dispatch an unavailable cover")
            ),
        )
        results = asyncio.run(_run_plan(coord, [s1]))
        assert results[("w1", "cover.w1")].reason and "unavailable" in str(
            results[("w1", "cover.w1")].blocked_reason or results[("w1", "cover.w1")].reason
        )
        assert results[("w1", "cover.w1")].status is not ExecutionStatus.SENT


class TestManualOverride:
    def test_live_manual_override_skips(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)

        class _FakeOverride:
            pass

        monkeypatch.setattr(coord._override_detector, "get", lambda window_id, now: _FakeOverride())
        results = asyncio.run(_run_plan(coord, [s1]))
        assert calls == [], "must not dispatch while manual override is active"
        assert results[("w1", "cover.w1")].status is not ExecutionStatus.SENT
        assert results[("w1", "cover.w1")].reason == "manual_override"


class TestComfortHold:
    def test_live_comfort_hold_skips(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        _setup_coord(coord, [s1])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)

        hold = coord._comfort_movement_holds.setdefault(
            "w1", coord._predispatch_sequential_plan.__func__.__globals__["_ComfortMovementHold"]()
        )
        monkeypatch.setattr(hold, "should_hold", lambda **kwargs: True)
        results = asyncio.run(_run_plan(coord, [s1]))
        assert calls == [], "must not dispatch while comfort hold is active"
        assert results[("w1", "cover.w1")].status is not ExecutionStatus.SENT
        assert results[("w1", "cover.w1")].reason == "comfort_position_hold"


class TestNightHold:
    def test_night_contact_blocked_skips(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        s1.night_contact_blocked = True
        _setup_coord(coord, [s1])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)
        results = asyncio.run(_run_plan(coord, [s1]))
        assert calls == [], "must not dispatch under a night contact hold"
        assert results[("w1", "cover.w1")].status is not ExecutionStatus.SENT
        assert results[("w1", "cover.w1")].reason == "night_hold"


class TestAlreadyMoving:
    def _seed_assumed_position(self, coord, entity_id, position_internal):
        coord.assumed_state_manager.update(
            entity_id, position_internal, _NOW, has_reliable_position_feedback=True,
        )

    def test_moving_toward_target_skips(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        # target_position_ha=100 -> internal target 0 (open). Cover currently
        # "opening" (is_opening) with assumed_position_internal=50 -> moving
        # toward 0, i.e. toward the target. Must skip.
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=100)
        _setup_coord(coord, [s1], ha_state="opening", current_position_by_entity={"cover.w1": 50})
        self._seed_assumed_position(coord, "cover.w1", 50)
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)
        results = asyncio.run(_run_plan(coord, [s1]))
        assert calls == [], "must not double-dispatch an item already moving toward target"
        assert results[("w1", "cover.w1")].status is not ExecutionStatus.SENT
        assert results[("w1", "cover.w1")].reason == "already_moving_to_target"

    def test_moving_opposite_direction_does_not_skip(self, monkeypatch) -> None:
        # target_position_ha=100 -> internal target 0. Cover is "closing"
        # (moving toward higher internal values) -> opposite direction from
        # target -> must NOT skip as already_moving_to_target.
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=100)
        _setup_coord(coord, [s1], ha_state="closing", current_position_by_entity={"cover.w1": 50})
        self._seed_assumed_position(coord, "cover.w1", 50)
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))
        results = asyncio.run(_run_plan(coord, [s1]))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT


class TestBlockerIsolation:
    def test_blocker_on_one_cover_does_not_affect_another(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1", target_position_ha=40)
        s2 = _state("w2", "z1", entity_id="cover.w2", target_position_ha=100, current_position_ha=0)
        s1.night_contact_blocked = True
        _setup_coord(
            coord, [s1, s2],
            current_position_by_entity={"cover.w1": 40, "cover.w2": 0},
        )
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.cover_entity_id)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))
        results = asyncio.run(_run_plan(coord, [s1, s2]))
        assert calls == ["cover.w2"]
        assert results[("w1", "cover.w1")].status is not ExecutionStatus.SENT
        assert results[("w2", "cover.w2")].status is ExecutionStatus.SENT


class TestBlockerCleared:
    def test_blocker_cleared_before_dispatch_still_executes(self, monkeypatch) -> None:
        coord = _make_coord(dispatch_config=DispatchConfig(mode=DispatchMode.SEQUENTIAL))
        s1 = _state("w1", "z1", entity_id="cover.w1")
        # is_override_active reflects a Pass-1 snapshot only; live query
        # (via _override_detector.get) is what validate_item actually uses.
        _setup_coord(coord, [s1])
        monkeypatch.setattr(coord._override_detector, "get", lambda window_id, now: None)
        _patch_dispatch_cover_intent(monkeypatch, coord, _fake_sent_dispatch)
        _patch_wait_for_travel_completion(monkeypatch, coord, _fake_completion_factory())
        _patch_asyncio_sleep(monkeypatch, coord, lambda s: _real_sleep(0))
        results = asyncio.run(_run_plan(coord, [s1]))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT
