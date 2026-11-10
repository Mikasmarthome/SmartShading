"""B3-010 correction — real restart/reload runtime proof (not source-string
inspection, not a manually-set previous_lifecycle_state on a bare WDI, not a
storage round-trip) and the real two-cycle dispatch proof through the actual
DispatchPlanExecutor path.

"Restart" and "reload" are exercised via TWO SEPARATE, REAL
SmartShadingCoordinator instances sharing the same lifecycle config: an "old"
instance whose own real `_lifecycle_state` field is put into a realistic
NIGHT state, then genuinely torn down via the real production
`async_shutdown()` (the exact call `async_unload_entry` makes in
custom_components/smartshading/__init__.py before a config-entry reload) --
followed by a brand-new instance (mirrors `_async_setup_zone_entry`
constructing a fresh SmartShadingCoordinator(...), the same call
`async_setup_entry` makes). The new instance's own real
`self.lifecycle_engine` / `self._lifecycle_config` are used for the first
real lifecycle evaluation, and the resulting decision is fed through the
REAL `_predispatch_sequential_plan()` DispatchPlanExecutor path (the same
production method test_t22_phase4b_sequential_dispatch_wiring.py /
test_t22_phase5a_live_validation.py / test_t22_phase5c_safety_preemption.py
already establish as this codebase's way of reaching an actual dispatch call
or a provable no-op without driving the full 600+-line `_async_update_data()`
orchestration method itself -- consistent with this suite's own established
precedent that the top-level update-cycle method is out of scope for direct
invocation (see test_active_control_switch_comfort_preemption.py's own
"full 600+-line _async_update_data() cycle" comment and every T22 phase file,
which all substitute a lighter callable rather than calling the real one).

"Restart" (no config-entry reload, whole HA process restarts) reaches the
IDENTICAL initial condition for a fresh coordinator, since `_lifecycle_state`
is a RAM-only field never loaded from storage in either case (see
coordinator.py `self._lifecycle_state: LifecycleState = LifecycleState.DAY`)
-- exercised here as a single fresh coordinator instance with no prior
in-process history at all, distinct from the two-instance reload proof above.
"""
from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import replace
from datetime import datetime, time, timezone
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
from custom_components.smartshading import coordinator as _coord_mod  # noqa: E402
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
from custom_components.smartshading.models.comfort import ComfortConfig  # noqa: E402
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults  # noqa: E402
from custom_components.smartshading.models.cover_group import CoverGroup  # noqa: E402
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    LifecycleState, MorningTrigger, NightDayLifecycleConfig, NightTrigger,
)
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
from custom_components.smartshading.models.window_decision_input import build_window_decision_input  # noqa: E402
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator  # noqa: E402

_apply_window_behavior_mode = _coord_mod._apply_window_behavior_mode
_ComfortMovementHold = _coord_mod._ComfortMovementHold


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


def _lifecycle_config(*, morning_position_ha: int = 70, active_months=None) -> NightDayLifecycleConfig:
    return NightDayLifecycleConfig(
        id="default",
        night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(22, 0), night_position=0,
        morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
        morning_position=morning_position_ha, active_months=active_months,
    )


def _make_coord(lifecycle_config: NightDayLifecycleConfig, **kwargs) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    coord = SmartShadingCoordinator(
        hass, entry, lifecycle_config=lifecycle_config, **kwargs,
    )
    coord.windows = {}
    coord.zones = {}
    coord.cover_groups = {}
    coord._startup_cycles_remaining = 0
    return coord


def _window(window_id: str = "w1", zone_id: str = "z1") -> WindowConfig:
    return WindowConfig(id=window_id, name=window_id, zone_id=zone_id, azimuth=180.0, floor_level=0, cover_group_id=f"cg_{window_id}")


def _zone(zone_id: str = "z1") -> ZoneConfig:
    return ZoneConfig(id=zone_id, name=zone_id)


def _decide_via_real_coordinator_engine(
    coord: SmartShadingCoordinator, now: datetime, window: WindowConfig, zone: ZoneConfig,
    *, outdoor_temp_c: float | None = None, is_in_solar_sector: bool = False,
    comfort_config: ComfortConfig = ComfortConfig(
        heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False,
    ),
):
    """Uses the coordinator's OWN real self.lifecycle_engine and
    self._lifecycle_config (not a freestanding constructed engine/config) --
    the first real lifecycle evaluation this coordinator instance performs,
    exactly like its first real refresh after setup would."""
    state = coord.lifecycle_engine.get_lifecycle_state(
        now, sun_elevation_deg=None, config=coord._lifecycle_config,
        previous_lifecycle_state=coord._lifecycle_state,
    )
    profile = coord.lifecycle_engine.active_profile(now, coord._lifecycle_config)
    effective_config = replace(
        coord._lifecycle_config,
        night_position=profile.night_position, morning_position=profile.morning_position,
    )
    wdi = build_window_decision_input(
        window=window, zone=zone, global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(), lifecycle_config=effective_config,
        lifecycle_state=state, absence_active=False, current_shading_state=ShadingState.NIGHT_CLOSED,
        outdoor_temp_c=outdoor_temp_c, indoor_temp_c=(35.0 if outdoor_temp_c else None), exposure=None,
        is_in_solar_sector=is_in_solar_sector, comfort_config=comfort_config,
    )
    wdi = _apply_window_behavior_mode(wdi, window.behavior_mode)
    decision = TierOrchestrator().evaluate_window(wdi)
    return state, decision


def _state_from_decision(window: WindowConfig, zone: ZoneConfig, decision, *, entity_id: str = "cover.w1") -> _WindowComputeState:
    target_ha = 100 - decision.target_position if decision.target_position is not None else 100
    return _WindowComputeState(
        window=window, zone=zone, obs_enabled=False, active_control_enabled=True,
        new_state=decision.shading_state, exec_entity_id=entity_id,
        exec_cap=CoverCapability(entity_id=entity_id, supports_position=True, supports_tilt=False, supports_open_close_only=False),
        exec_snapshot=build_cover_entity_snapshot(entity_id=entity_id, state="open", attributes={"current_position": 0}),
        exec_mode=ExecutionMode.AUTOMATIC, is_safety=False,
        exec_target_internal=decision.target_position,
        exec_filter_result=CommandFilterResult(
            allowed=True, blocked_reason=None,
            target_position_internal=decision.target_position, target_position_ha=target_ha,
            execution_mode=ExecutionMode.AUTOMATIC.value, is_safety=False,
        ),
        tier_decided_by=decision.decided_by, is_override_active=False, cover_available=True,
    )


def _state_from_decision_real_filter(
    window: WindowConfig, zone: ZoneConfig, decision, hold, now: datetime,
    *, entity_id: str = "cover.w1", is_in_solar_sector: bool = False,
    current_position_internal: int | None = 100,
) -> _WindowComputeState:
    """Same as _state_from_decision(), but computes exec_filter_result via
    the REAL ComfortMovementHold.should_hold()/should_delay_fallback_open()
    and the REAL CommandFilter().evaluate() -- mirroring coordinator.py's
    actual per-cycle wiring (coordinator.py ~5245-5322) instead of
    fabricating CommandFilterResult(allowed=True) unconditionally. Required
    whenever a test needs to prove the ComfortMovementHold->CommandFilter
    gate itself, not just downstream DispatchPlanExecutor mechanics."""
    from custom_components.smartshading.cover_control.command_filter import (
        CommandFilter, ExecutionCapability,
    )

    target_ha = 100 - decision.target_position if decision.target_position is not None else 100
    is_fallback_confirmed_exit = (
        decision.decided_by == "TierOrchestrator:fallback" and not is_in_solar_sector
    )
    comfort_hold_held = hold.should_hold(
        proposed_decided_by=decision.decided_by,
        proposed_target_ha=target_ha,
        is_strong_escalation=False,
        is_confirmed_exit=is_fallback_confirmed_exit,
        now=now,
    )
    fallback_release_pending = hold.should_delay_fallback_open(
        proposed_decided_by=decision.decided_by,
        is_confirmed_exit=is_fallback_confirmed_exit,
    )
    filter_result = CommandFilter().evaluate(
        target_position_internal=decision.target_position,
        current_position_internal=current_position_internal,
        execution_mode=ExecutionMode.AUTOMATIC,
        is_safety=False,
        is_manual_override=False,
        is_cover_available=True,
        state_guard_allowed=True,
        execution_capability=ExecutionCapability(),
        comfort_hold_allowed=not comfort_hold_held,
        fallback_release_allowed=not fallback_release_pending,
    )
    return _WindowComputeState(
        window=window, zone=zone, obs_enabled=False, active_control_enabled=True,
        new_state=decision.shading_state, exec_entity_id=entity_id,
        exec_cap=CoverCapability(entity_id=entity_id, supports_position=True, supports_tilt=False, supports_open_close_only=False),
        exec_snapshot=build_cover_entity_snapshot(entity_id=entity_id, state="open", attributes={"current_position": (100 - current_position_internal) if current_position_internal is not None else 0}),
        exec_mode=ExecutionMode.AUTOMATIC, is_safety=False,
        exec_target_internal=decision.target_position,
        exec_filter_result=filter_result,
        tier_decided_by=decision.decided_by, is_override_active=False, cover_available=True,
    )


def _setup_coord_for_dispatch(coord, states, *, current_position_by_entity=None) -> None:
    coord.windows = {s.window.id: s.window for s in states}
    coord.zones = {s.zone.id: s.zone for s in states}
    coord.cover_groups = {
        s.window.cover_group_id: CoverGroup(id=s.window.cover_group_id, window_id=s.window.id, cover_ids=[s.exec_entity_id])
        for s in states
    }
    current_position_by_entity = current_position_by_entity or {}
    ha_states: dict[str, MagicMock] = {}
    for s in states:
        pos = current_position_by_entity.get(s.exec_entity_id, 0)
        st = MagicMock()
        st.state = "open"
        st.attributes = {"current_position": pos}
        ha_states[s.exec_entity_id] = st
        coord._cover_capabilities[s.exec_entity_id] = s.exec_cap
    coord.hass.states.get = MagicMock(side_effect=lambda eid: ha_states.get(eid))


def _harm(states) -> dict:
    return {
        s.window.id: HarmonizationResult(harmonized=False, final_target_position_ha=None, pre_harmonization_target_position_ha=None)
        for s in states
    }


def _zone_maps(states):
    zone_order, window_order_in_zone, counters = {}, {}, {}
    for s in states:
        zone_order.setdefault(s.zone.id, len(zone_order))
        idx = counters.get(s.zone.id, 0)
        window_order_in_zone[s.window.id] = idx
        counters[s.zone.id] = idx + 1
    return zone_order, window_order_in_zone


def _ordered(states):
    return [(s.window.id, s) for s in states]


async def _fake_sent_dispatch(hass, intent, *, now_utc):
    return build_sent_result(intent, sent_at_utc=now_utc, reason="test")


def _run_plan(coord, states, now, gen: int = 0):
    zone_order, window_order_in_zone = _zone_maps(states)
    return coord._predispatch_sequential_plan(
        _ordered(states), _harm(states), now, gen, zone_order, window_order_in_zone, asyncio.Event(),
    )


def _patch_dispatch(monkeypatch, coord, fake=_fake_sent_dispatch):
    monkeypatch.setitem(coord._predispatch_sequential_plan.__func__.__globals__, "dispatch_cover_intent", fake)
    _patch_completion_and_sleep(monkeypatch, coord)


def _patch_completion_and_sleep(monkeypatch, coord) -> None:
    """A partial (INTERMEDIATE) target waits for travel completion under
    SEQUENTIAL dispatch (T22 Phase 3 design) -- patch both to real-time-free
    fakes, same as test_t22_phase4b/5a, so tests run instantly instead of
    hitting the real ~40s completion timeout."""
    async def _fake_completion(*a, **k):
        from custom_components.smartshading.cover_control.dispatch_completion import (
            CompletionMethod, CompletionResult,
        )
        return CompletionResult(method=CompletionMethod.TIMEOUT, elapsed_s=0.01, timed_out=False)

    monkeypatch.setitem(
        coord._predispatch_sequential_plan.__func__.__globals__,
        "wait_for_travel_completion", _fake_completion,
    )
    real_asyncio = coord._predispatch_sequential_plan.__func__.__globals__["asyncio"]
    monkeypatch.setattr(real_asyncio, "sleep", lambda s: _real_sleep(0))


_real_sleep = asyncio.sleep
_LATE_MORNING = datetime(2026, 8, 3, 10, 0, 0, tzinfo=timezone.utc)  # Monday, well past 06:30 trigger


class TestRealRestartAfterMissedTriggerReachesDispatch:
    """A single fresh coordinator (restart: no config-entry reload, but the
    RAM-only _lifecycle_state field starts at the same DAY default either
    way) whose first real cycle runs well after the morning trigger."""

    def test_partial_position_no_stronger_candidate_dispatches_the_real_day_decision(self, monkeypatch) -> None:
        coord = _make_coord(_lifecycle_config(morning_position_ha=70))
        window, zone = _window(), _zone()
        state, decision = _decide_via_real_coordinator_engine(coord, _LATE_MORNING, window, zone)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "TierOrchestrator:fallback", (
            "a missed Morning event reconstructs to the real, current DAY "
            "decision -- the pipeline takes over immediately, exactly as "
            "BehaviorConfig.morning_position documents; no catch-up move to "
            "the missed morning_position is ever synthesized"
        )

        s = _state_from_decision(window, zone, decision)
        _setup_coord_for_dispatch(coord, [s])
        _patch_dispatch(monkeypatch, coord)
        results = asyncio.run(_run_plan(coord, [s], _LATE_MORNING))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT, (
            "nothing was dispatched yet this run (fresh coordinator, empty "
            "ComfortMovementHold), so the real DAY decision reaches an "
            "actual dispatch call -- not blocked, not a no-op"
        )

    def test_active_heat_protection_dispatches_the_protective_target_directly(self, monkeypatch) -> None:
        coord = _make_coord(_lifecycle_config(morning_position_ha=70))
        window, zone = _window(), _zone()
        hot = ComfortConfig(
            heat_protection_enabled=True, heat_protection_outdoor_temp_c=25.0,
            heat_protection_indoor_temp_c=24.0,
        )
        state, decision = _decide_via_real_coordinator_engine(
            coord, _LATE_MORNING, window, zone,
            outdoor_temp_c=30.0, is_in_solar_sector=True, comfort_config=hot,
        )
        assert state is LifecycleState.DAY
        assert decision.decided_by == "HeatEvaluator"
        assert decision.shading_state is not ShadingState.OPEN

        s = _state_from_decision(window, zone, decision)
        _setup_coord_for_dispatch(coord, [s])
        _patch_dispatch(monkeypatch, coord)
        results = asyncio.run(_run_plan(coord, [s], _LATE_MORNING))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT

    def test_outside_active_months_no_morning_candidate_at_all(self) -> None:
        coord = _make_coord(_lifecycle_config(morning_position_ha=70, active_months=(12, 1, 2)))
        window, zone = _window(), _zone()
        state, decision = _decide_via_real_coordinator_engine(coord, _LATE_MORNING, window, zone)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "TierOrchestrator:fallback"


class TestRealReloadAfterMissedTriggerReachesDispatch:
    """TWO real SmartShadingCoordinator instances: the "old" one genuinely
    torn down via async_shutdown() (the exact call async_unload_entry makes)
    after reaching a realistic NIGHT state; a brand-new instance constructed
    afterward (mirrors _async_setup_zone_entry), whose own first real cycle
    -- using ITS OWN lifecycle_engine/_lifecycle_config -- runs well after
    the morning trigger."""

    def test_partial_position_no_stronger_candidate_dispatches_the_real_day_decision(self, monkeypatch) -> None:
        cfg = _lifecycle_config(morning_position_ha=70)
        old_coord = _make_coord(cfg)
        old_coord._lifecycle_state = LifecycleState.NIGHT  # realistic pre-unload state
        asyncio.run(old_coord.async_shutdown())  # the real teardown call async_unload_entry makes

        new_coord = _make_coord(cfg)  # mirrors _async_setup_zone_entry's fresh construction
        assert new_coord._lifecycle_state is LifecycleState.DAY, (
            "a fresh coordinator instance never inherits the old instance's "
            "RAM-only lifecycle state -- confirmed on the actual new object, "
            "not merely asserted about the source code"
        )
        assert new_coord is not old_coord

        window, zone = _window(), _zone()
        state, decision = _decide_via_real_coordinator_engine(new_coord, _LATE_MORNING, window, zone)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "TierOrchestrator:fallback"

        s = _state_from_decision(window, zone, decision)
        _setup_coord_for_dispatch(new_coord, [s])
        _patch_dispatch(monkeypatch, new_coord)
        results = asyncio.run(_run_plan(new_coord, [s], _LATE_MORNING))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT

    def test_active_sun_protection_dispatches_the_protective_target_directly(self, monkeypatch) -> None:
        from custom_components.smartshading.engines.exposure_engine import WindowExposure

        cfg = _lifecycle_config(morning_position_ha=70)
        old_coord = _make_coord(cfg)
        old_coord._lifecycle_state = LifecycleState.NIGHT
        asyncio.run(old_coord.async_shutdown())

        new_coord = _make_coord(cfg)
        window, zone = _window(), _zone()
        # SolarEvaluator needs a real WindowExposure, unlike Heat -- built
        # directly rather than through the shared helper's simpler signature.
        state = new_coord.lifecycle_engine.get_lifecycle_state(
            _LATE_MORNING, sun_elevation_deg=None, config=new_coord._lifecycle_config,
            previous_lifecycle_state=new_coord._lifecycle_state,
        )
        profile = new_coord.lifecycle_engine.active_profile(_LATE_MORNING, new_coord._lifecycle_config)
        effective_config = replace(
            new_coord._lifecycle_config,
            night_position=profile.night_position, morning_position=profile.morning_position,
        )
        wdi = build_window_decision_input(
            window=window, zone=zone, global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(), lifecycle_config=effective_config,
            lifecycle_state=state, absence_active=False, current_shading_state=ShadingState.NIGHT_CLOSED,
            outdoor_temp_c=None, indoor_temp_c=None,
            exposure=WindowExposure(
                window_id="w1", timestamp=_LATE_MORNING,
                sun_azimuth=180.0, sun_elevation=45.0,
                is_above_horizon=True, is_in_tolerance_window=True,
                azimuth_delta_deg=0.0, direct_radiation_factor=1.0, elevation_clipped=False,
                theoretical_exposure=500.0, learned_solar_impact_factor=1.0,
                seasonal_factor=1.0, effective_exposure=500.0,
            ),
            is_in_solar_sector=True,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=True),
        )
        wdi = _apply_window_behavior_mode(wdi, window.behavior_mode)
        decision = TierOrchestrator().evaluate_window(wdi)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "SolarEvaluator"
        assert decision.shading_state is not ShadingState.OPEN

        s = _state_from_decision(window, zone, decision)
        _setup_coord_for_dispatch(new_coord, [s])
        _patch_dispatch(monkeypatch, new_coord)
        results = asyncio.run(_run_plan(new_coord, [s], _LATE_MORNING))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT

    def test_outside_active_months_no_morning_candidate(self) -> None:
        cfg = _lifecycle_config(morning_position_ha=70, active_months=(12, 1, 2))
        old_coord = _make_coord(cfg)
        old_coord._lifecycle_state = LifecycleState.NIGHT
        asyncio.run(old_coord.async_shutdown())

        new_coord = _make_coord(cfg)
        window, zone = _window(), _zone()
        state, decision = _decide_via_real_coordinator_engine(new_coord, _LATE_MORNING, window, zone)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "TierOrchestrator:fallback"


class TestRealTwoCycleDispatchNoSecondServiceCall:
    """The genuine two-cycle proof, extended from
    test_b3010_real_runtime_path.py's structural bypasses_guard() check all
    the way to real dispatch calls: cycle 1 (MORNING) dispatches the
    configured partial target via the real DispatchPlanExecutor path; cycle
    2 (the immediately following DAY, plain fallback) must NOT issue a
    second dispatch_cover_intent() call, via the ComfortMovementHold
    mechanism now recording MorningEvaluator's cycle-1 dispatch."""

    def _cycle1_dispatch(self, coord, monkeypatch, window, zone, now, hold, *, is_in_solar_sector=False):
        coord._lifecycle_state = LifecycleState.NIGHT  # realistic: night just ended
        state, decision = _decide_via_real_coordinator_engine(coord, now, window, zone)
        assert state is LifecycleState.MORNING
        assert decision.decided_by == "MorningEvaluator"
        # Mirrors coordinator.py's own self._lifecycle_state = ... assignment
        # at the end of a real cycle, so the SAME coordinator's next cycle
        # correctly sees previous=MORNING (not the stale NIGHT set above).
        coord._lifecycle_state = state
        s = _state_from_decision_real_filter(
            window, zone, decision, hold, now, is_in_solar_sector=is_in_solar_sector,
            current_position_internal=100,  # was fully open (HA 0) before Morning
        )
        _setup_coord_for_dispatch(coord, [s])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.target_position_ha)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch(monkeypatch, coord, fake)
        results = asyncio.run(_run_plan(coord, [s], now))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT
        # Mirrors what coordinator.py's real per-window loop does after a
        # confirmed sent dispatch -- feeds the SAME hold instance the next
        # cycle's should_hold() call reads.
        hold.record_dispatch(decided_by=decision.decided_by, target_ha=calls[0], now=now)
        return decision, calls

    def test_reliable_feedback_no_second_dispatch_next_cycle(self, monkeypatch) -> None:
        cfg = _lifecycle_config(morning_position_ha=70)
        coord = _make_coord(cfg)
        window, zone = _window(), _zone()
        hold = coord._comfort_movement_holds.setdefault(window.id, _ComfortMovementHold())
        now1 = datetime(2026, 8, 3, 6, 30, 0, tzinfo=timezone.utc)
        decision1, calls1 = self._cycle1_dispatch(coord, monkeypatch, window, zone, now1, hold)
        assert calls1 == [70]

        now2 = now1.replace(minute=31)
        state2, decision2 = _decide_via_real_coordinator_engine(coord, now2, window, zone)
        assert state2 is LifecycleState.DAY
        assert decision2.decided_by == "TierOrchestrator:fallback"

        # Cover is physically at HA 70 (internal 30) after cycle 1's real
        # dispatch -- the SAME hold instance cycle 1 recorded into.
        s2 = _state_from_decision_real_filter(
            window, zone, decision2, hold, now2, current_position_internal=30,
        )
        assert s2.exec_filter_result.allowed is False, (
            "the REAL CommandFilter().evaluate() call, fed by the REAL "
            "ComfortMovementHold recorded in cycle 1, must itself block "
            "this proposal -- not merely a should_hold() value asserted in "
            "isolation"
        )
        _setup_coord_for_dispatch(coord, [s2], current_position_by_entity={"cover.w1": 70})

        calls2 = []

        async def fake2(hass, intent, *, now_utc):
            calls2.append(intent.target_position_ha)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch(monkeypatch, coord, fake2)
        # T22 Phase 4b: an item whose exec_filter_result.allowed is False is
        # excluded from the SEQUENTIAL pre-pass plan entirely (SDW-02/03
        # pattern) -- confirmed empty, and the fake dispatch is never
        # invoked either way.
        results = asyncio.run(_run_plan(coord, [s2], now2))
        assert results == {}, "a blocked item must not appear in the dispatch plan at all"
        assert calls2 == [], "no service call was issued for the still-held cycle 2 proposal"

    def test_out_of_solar_sector_confirmed_exit_still_no_second_dispatch(self, monkeypatch) -> None:
        """The exact regression scenario found in review: a window OUT of
        its solar sector (is_confirmed_exit=True every cycle) must still
        have its Morning-dispatched target held against the immediately
        following Fallback/Open proposal."""
        cfg = _lifecycle_config(morning_position_ha=70)
        coord = _make_coord(cfg)
        window, zone = _window(), _zone()
        hold = coord._comfort_movement_holds.setdefault(window.id, _ComfortMovementHold())
        now1 = datetime(2026, 8, 3, 6, 30, 0, tzinfo=timezone.utc)
        decision1, calls1 = self._cycle1_dispatch(
            coord, monkeypatch, window, zone, now1, hold, is_in_solar_sector=False,
        )
        assert calls1 == [70]

        now2 = now1.replace(minute=31)
        state2, decision2 = _decide_via_real_coordinator_engine(coord, now2, window, zone)
        assert decision2.decided_by == "TierOrchestrator:fallback"

        s2 = _state_from_decision_real_filter(
            window, zone, decision2, hold, now2,
            is_in_solar_sector=False,  # is_confirmed_exit=True this cycle too
            current_position_internal=30,
        )
        assert s2.exec_filter_result.allowed is False, (
            "is_confirmed_exit=True must NOT bypass a Morning-recorded hold "
            "-- that geometric fact is unrelated to a non-solar-driven "
            "Morning position (see engines/comfort_movement_hold.py fix)"
        )
        _setup_coord_for_dispatch(coord, [s2], current_position_by_entity={"cover.w1": 70})
        calls2 = []

        async def fake2(hass, intent, *, now_utc):
            calls2.append(intent.target_position_ha)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch(monkeypatch, coord, fake2)
        results = asyncio.run(_run_plan(coord, [s2], now2))
        assert results == {}
        assert calls2 == []

    def test_multiple_windows_same_zone_only_final_targets(self, monkeypatch) -> None:
        cfg = _lifecycle_config(morning_position_ha=70)
        coord = _make_coord(cfg)
        zone = _zone("z1")
        w1 = _window("w1", "z1")
        w2 = _window("w2", "z1")
        now = datetime(2026, 8, 3, 6, 30, 0, tzinfo=timezone.utc)
        coord._lifecycle_state = LifecycleState.NIGHT  # realistic: night just ended

        _, d1 = _decide_via_real_coordinator_engine(coord, now, w1, zone)
        _, d2 = _decide_via_real_coordinator_engine(coord, now, w2, zone)
        assert d1.decided_by == d2.decided_by == "MorningEvaluator"
        assert d1.target_position == d2.target_position == 30

        s1 = _state_from_decision(w1, zone, d1, entity_id="cover.w1")
        s2 = _state_from_decision(w2, zone, d2, entity_id="cover.w2")
        _setup_coord_for_dispatch(coord, [s1, s2])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append((intent.cover_entity_id, intent.target_position_ha))
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch(monkeypatch, coord, fake)
        results = asyncio.run(_run_plan(coord, [s1, s2], now))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT
        assert results[("w2", "cover.w2")].status is ExecutionStatus.SENT
        assert sorted(calls) == [("cover.w1", 70), ("cover.w2", 70)], (
            "each window reaches exactly one, final dispatch call with the "
            "same resolved target -- no intermediate/partial calls"
        )

    def test_stale_generation_discards_the_morning_dispatch(self, monkeypatch) -> None:
        cfg = _lifecycle_config(morning_position_ha=70)
        coord = _make_coord(cfg)
        window, zone = _window(), _zone()
        now = datetime(2026, 8, 3, 6, 30, 0, tzinfo=timezone.utc)
        coord._lifecycle_state = LifecycleState.NIGHT
        state, decision = _decide_via_real_coordinator_engine(coord, now, window, zone)
        assert decision.decided_by == "MorningEvaluator"
        s = _state_from_decision(window, zone, decision)
        _setup_coord_for_dispatch(coord, [s])

        async def fake(hass, intent, *, now_utc):
            raise AssertionError("must not dispatch a stale-generation item")

        _patch_dispatch(monkeypatch, coord, fake)
        coord._dispatch_generation = 1  # newer cycle already superseded this plan
        results = asyncio.run(_run_plan(coord, [s], now, gen=0))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.NOT_ATTEMPTED
        assert results[("w1", "cover.w1")].reason == "stale_generation"

    def test_full_open_morning_target_also_dispatches_cleanly(self, monkeypatch) -> None:
        cfg = _lifecycle_config(morning_position_ha=100)  # fully open
        coord = _make_coord(cfg)
        window, zone = _window(), _zone()
        now = datetime(2026, 8, 3, 6, 30, 0, tzinfo=timezone.utc)
        coord._lifecycle_state = LifecycleState.NIGHT
        state, decision = _decide_via_real_coordinator_engine(coord, now, window, zone)
        assert decision.decided_by == "MorningEvaluator"
        assert decision.target_position == 0
        s = _state_from_decision(window, zone, decision)
        _setup_coord_for_dispatch(coord, [s])
        calls = []

        async def fake(hass, intent, *, now_utc):
            calls.append(intent.target_position_ha)
            return await _fake_sent_dispatch(hass, intent, now_utc=now_utc)

        _patch_dispatch(monkeypatch, coord, fake)
        results = asyncio.run(_run_plan(coord, [s], now))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.SENT
        assert calls == [100]
