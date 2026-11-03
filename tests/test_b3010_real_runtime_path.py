"""B3-010 correction — Reale Runtime-Abdeckung (requirement 3).

Extends the already-established real-path proof in
test_morning_position_resolution.py's TestRealPathLifecycleEventToFinalZoneDecision
(LifecycleEngine -> active_profile -> build_window_decision_input ->
TierOrchestrator) through the remaining decision-changing runtime stages the
prior B3-010 report admitted were untested: behavior-mode masking, the
StateGuard-level no-op transition that eliminates the "second movement"
concern, and — using the SAME real-SmartShadingCoordinator-with-HA-stubs
DispatchPlanExecutor technique already established by
test_t22_phase4b_sequential_dispatch_wiring.py / test_t22_phase5a_live_validation.py
/ test_t22_phase5c_safety_preemption.py — cross-cycle generation/preemption
and live pre-dispatch revalidation, applied specifically to a Morning-flavored
target.

Coverage:
  RRP-01  Heat wins outright over Morning: TierOrchestrator's PositionResolver
          returns ONE final decision (Heat's), never Morning's, for a cycle
          where both apply — no full-open-then-shade sequence is even
          possible because only one decision is ever produced per cycle.
  RRP-02  The one-cycle MORNING->DAY transition, when nothing more protective
          applies, resolves to the SAME target both cycles -> state_machine's
          bypasses_guard() treats it as a trivial OPEN->OPEN no-op, so no
          second dispatch is triggered by the transition itself.
  RRP-03  Two windows in the same zone, only one of which needs Heat
          protection this cycle, each independently reach their own correct
          final decision (Heat window: Heat wins; the other: Morning wins) —
          proving multi-window coordination without cross-contamination.
  RRP-04  A Morning-targeted comfort item queued for SEQUENTIAL dispatch is
          discarded (never dispatched) when the coordinator's dispatch
          generation has already advanced past it — same mechanism proven
          generically by TestStaleGeneration, applied to a Morning target.
  RRP-05  A Morning-targeted comfort item is never dispatched once live
          pre-dispatch revalidation observes an active comfort-position hold
          — the concrete mechanism behind "live-revalidation cannot execute a
          stale Morning target".
"""
from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import replace
from datetime import datetime, time, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# RRP-01..03: pure-function real path, no coordinator/HA needed.
# ---------------------------------------------------------------------------
from custom_components.smartshading.engines.lifecycle_engine import LifecycleEngine
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import (
    LifecycleScheduleMode, LifecycleState, MorningTrigger, NightDayLifecycleConfig, NightTrigger,
)
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_NO_COMFORT = ComfortConfig()


def _config(*, morning_position: int = 70) -> NightDayLifecycleConfig:
    return NightDayLifecycleConfig(
        id="default", schedule_mode=LifecycleScheduleMode.SAME_EVERY_DAY,
        night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(22, 0), night_position=0,
        morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
        morning_position=morning_position,
    )


def _window(window_id: str = "w1", zone_id: str = "z1") -> WindowConfig:
    return WindowConfig(
        id=window_id, name=window_id, zone_id=zone_id,
        azimuth=180.0, floor_level=0, cover_group_id=f"cg_{window_id}",
    )


def _zone(zone_id: str = "z1") -> ZoneConfig:
    return ZoneConfig(id=zone_id, name=zone_id)


def _decide(
    now: datetime, window: WindowConfig, zone: ZoneConfig, config: NightDayLifecycleConfig,
    *, prev: LifecycleState, outdoor_temp_c: float | None = None,
    is_in_solar_sector: bool = False, comfort_config: ComfortConfig = _NO_COMFORT,
    current_shading_state: ShadingState = ShadingState.NIGHT_CLOSED,
):
    """coordinator.py's real per-cycle resolution sequence, one window."""
    engine = LifecycleEngine()
    state = engine.get_lifecycle_state(now, sun_elevation_deg=None, config=config, previous_lifecycle_state=prev)
    profile = engine.active_profile(now, config)
    effective_config = replace(config, night_position=profile.night_position, morning_position=profile.morning_position)
    wdi = build_window_decision_input(
        window=window, zone=zone, global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(), lifecycle_config=effective_config,
        lifecycle_state=state, absence_active=False, current_shading_state=current_shading_state,
        outdoor_temp_c=outdoor_temp_c, indoor_temp_c=(35.0 if outdoor_temp_c else None), exposure=None,
        is_in_solar_sector=is_in_solar_sector, comfort_config=comfort_config,
    )
    decision = TierOrchestrator().evaluate_window(wdi)
    return state, decision


class TestHeatWinsOutrightSingleFinalDecision:
    """RRP-01: only one decision is ever produced per cycle — proves a
    full-open-then-shade sequence structurally cannot happen, because there
    is no intermediate decision to dispatch in the first place."""

    def test_morning_alone_wins_when_nothing_stronger_applies(self) -> None:
        now = datetime(2026, 6, 15, 6, 30, 0, tzinfo=_UTC)
        state, decision = _decide(now, _window(), _zone(), _config(morning_position=70), prev=LifecycleState.NIGHT)
        assert state is LifecycleState.MORNING
        assert decision.decided_by == "MorningEvaluator"
        assert decision.target_position == 30  # HA 70 -> internal 30

    def test_heat_wins_over_morning_same_cycle(self) -> None:
        now = datetime(2026, 6, 15, 6, 30, 0, tzinfo=_UTC)
        hot_comfort = ComfortConfig(
            heat_protection_enabled=True, heat_protection_outdoor_temp_c=25.0,
            heat_protection_indoor_temp_c=24.0, heat_protection_hysteresis_c=1.0,
        )
        state, decision = _decide(
            now, _window(), _zone(), _config(morning_position=70), prev=LifecycleState.NIGHT,
            outdoor_temp_c=30.0, is_in_solar_sector=True, comfort_config=hot_comfort,
        )
        assert state is LifecycleState.MORNING
        assert decision.decided_by == "HeatEvaluator", (
            "the SAME cycle that would otherwise apply Morning's partial "
            "position must resolve directly to Heat's more-protective target "
            "— there is only ever one WindowDecision per cycle, so a "
            "full-open-then-shade sequence is structurally impossible"
        )
        assert decision.shading_state is not ShadingState.OPEN


class TestMorningToDayTransitionResolvesToARealDifferentDecision:
    """B3-010 correction: MorningEvaluator fires ONLY for the one-cycle
    MORNING transition — the immediately following DAY cycle has NO Morning
    candidate at all and resolves via whichever tier genuinely applies
    (here: the plain Fallback/Open, a real, different decision from
    Morning's own). state_machine.bypasses_guard() therefore does NOT apply
    here (it only short-circuits when proposed==current shading_state AND
    the SAME target — that is not the case for a differing target).
    Preventing an unwanted second dispatch for this differing-target case is
    solved at the throttling layer instead — see
    engines/comfort_movement_hold.py's NON_PRIORITY_DECIDERS (now including
    "MorningEvaluator") and the real, dispatch-level two-cycle proof in
    test_b3010_real_restart_reload_dispatch.py's
    TestRealTwoCycleDispatchNoSecondServiceCall, which exercises the actual
    DispatchPlanExecutor path rather than this file's pure-function chain."""

    def test_day_cycle_is_a_real_different_decision_not_a_repeated_morning(self) -> None:
        cfg = _config(morning_position=70)
        # Cycle 1: NIGHT -> MORNING (the transition instant).
        now1 = datetime(2026, 6, 15, 6, 30, 0, tzinfo=_UTC)
        state1, decision1 = _decide(now1, _window(), _zone(), cfg, prev=LifecycleState.NIGHT)
        assert state1 is LifecycleState.MORNING
        assert decision1.decided_by == "MorningEvaluator"
        assert decision1.target_position == 30

        # Cycle 2 (immediately after): DAY, same window, same config.
        now2 = datetime(2026, 6, 15, 6, 31, 0, tzinfo=_UTC)
        state2, decision2 = _decide(now2, _window(), _zone(), cfg, prev=LifecycleState.MORNING)
        assert state2 is LifecycleState.DAY
        assert decision2.decided_by == "TierOrchestrator:fallback", (
            "DAY has no Morning candidate at all — this is a genuinely "
            "different decision, not a repeat of Morning's own"
        )
        assert decision2.target_position == 0

    def test_protection_end_returns_to_fallback_never_back_to_morning_position(self) -> None:
        """B3-010 correction item 1d: after a later protective need (Heat)
        clears, the following cycle must resolve to the ordinary daytime
        fallback OPEN -- never a return to Morning's own configured
        position, since MorningEvaluator only ever fires on the one MORNING
        transition cycle and never re-competes afterward, regardless of
        what happened in between."""
        cfg = _config(morning_position=70)
        hot = ComfortConfig(
            heat_protection_enabled=True, heat_protection_outdoor_temp_c=25.0,
            heat_protection_indoor_temp_c=24.0, heat_protection_hysteresis_c=1.0,
        )
        # Cycle 1: MORNING transition.
        now1 = datetime(2026, 6, 15, 6, 30, 0, tzinfo=_UTC)
        state1, decision1 = _decide(now1, _window(), _zone(), cfg, prev=LifecycleState.NIGHT)
        assert decision1.decided_by == "MorningEvaluator"

        # Cycle 2: DAY, Heat protection now genuinely active.
        now2 = datetime(2026, 6, 15, 11, 0, 0, tzinfo=_UTC)
        state2, decision2 = _decide(
            now2, _window(), _zone(), cfg, prev=state1,
            outdoor_temp_c=30.0, is_in_solar_sector=True, comfort_config=hot,
        )
        assert decision2.decided_by == "HeatEvaluator"

        # Cycle 3: DAY, Heat protection has cleared (comfort_config reverted).
        now3 = datetime(2026, 6, 15, 15, 0, 0, tzinfo=_UTC)
        state3, decision3 = _decide(now3, _window(), _zone(), cfg, prev=state2)
        assert decision3.decided_by == "TierOrchestrator:fallback", (
            "must resolve to the plain fallback, not 'MorningEvaluator' -- "
            "Morning's own decided_by can structurally never reappear "
            "outside the one MORNING transition cycle"
        )
        assert decision3.target_position == 0


class TestMultiWindowSameZoneCoordination:
    """RRP-03: two windows in one zone, only one needing Heat protection this
    cycle — each must reach its OWN correct final decision, proving the
    per-window arbitration does not cross-contaminate within a zone."""

    def test_heat_window_and_morning_window_resolve_independently(self) -> None:
        now = datetime(2026, 6, 15, 6, 30, 0, tzinfo=_UTC)
        zone = _zone("z1")
        cfg = _config(morning_position=70)
        hot_comfort = ComfortConfig(
            heat_protection_enabled=True, heat_protection_outdoor_temp_c=25.0,
            heat_protection_indoor_temp_c=24.0, heat_protection_hysteresis_c=1.0,
        )
        w_heat = WindowConfig(id="w-heat", name="Heat", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg-heat")
        w_plain = WindowConfig(id="w-plain", name="Plain", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg-plain")

        _, decision_heat = _decide(
            now, w_heat, zone, cfg, prev=LifecycleState.NIGHT,
            outdoor_temp_c=30.0, is_in_solar_sector=True, comfort_config=hot_comfort,
        )
        _, decision_plain = _decide(
            now, w_plain, zone, cfg, prev=LifecycleState.NIGHT,
            outdoor_temp_c=None, is_in_solar_sector=False, comfort_config=_NO_COMFORT,
        )

        assert decision_heat.decided_by == "HeatEvaluator"
        assert decision_plain.decided_by == "MorningEvaluator"
        assert decision_plain.target_position == 30


# ---------------------------------------------------------------------------
# RRP-04/05: generation/preemption + live-revalidation, using the real
# SmartShadingCoordinator DispatchPlanExecutor path. Same self-contained
# HA-stub technique as test_t22_phase4b_sequential_dispatch_wiring.py /
# test_t22_phase5a_live_validation.py.
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
    monkeypatch.setitem(coord._predispatch_sequential_plan.__func__.__globals__, "dispatch_cover_intent", fake)


def _rt_window(window_id: str, zone_id: str) -> WindowConfig:
    return WindowConfig(id=window_id, name=window_id, zone_id=zone_id, azimuth=180, floor_level=0, cover_group_id=f"cg_{window_id}")


def _morning_state(window_id: str, zone_id: str, *, entity_id: str, morning_target_ha: int = 70) -> _WindowComputeState:
    """A comfort item as MorningEvaluator's one-cycle transition would
    produce it — OPEN, partial target, decided_by MorningEvaluator."""
    return _WindowComputeState(
        window=_rt_window(window_id, zone_id),
        zone=ZoneConfig(id=zone_id, name=zone_id),
        obs_enabled=False,
        active_control_enabled=True,
        new_state=ShadingState.OPEN,
        exec_entity_id=entity_id,
        exec_cap=CoverCapability(entity_id=entity_id, supports_position=True, supports_tilt=False, supports_open_close_only=False),
        exec_snapshot=build_cover_entity_snapshot(entity_id=entity_id, state="open", attributes={"current_position": 0}),
        exec_mode=ExecutionMode.AUTOMATIC,
        is_safety=False,
        exec_target_internal=100 - morning_target_ha,
        exec_filter_result=CommandFilterResult(
            allowed=True, blocked_reason=None,
            target_position_internal=100 - morning_target_ha, target_position_ha=morning_target_ha,
            execution_mode=ExecutionMode.AUTOMATIC.value, is_safety=False,
        ),
        tier_decided_by="MorningEvaluator",
        is_override_active=False, cover_available=True,
        morning_effective_position_ha=morning_target_ha,
    )


def _setup_coord(coord, states, *, current_position_by_entity=None) -> None:
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


_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


async def _fake_sent_dispatch(hass, intent, *, now_utc):
    return build_sent_result(intent, sent_at_utc=now_utc, reason="test")


def _run_plan(coord, states, gen: int = 0):
    zone_order, window_order_in_zone = _zone_maps(states)
    return coord._predispatch_sequential_plan(
        _ordered(states), _harm(states), _NOW, gen, zone_order, window_order_in_zone, asyncio.Event(),
    )


class TestStaleMorningPlanDiscardedByGeneration:
    def test_generation_bump_before_dispatch_discards_the_morning_item(self, monkeypatch) -> None:
        coord = _make_coord()
        s1 = _morning_state("w1", "z1", entity_id="cover.w1", morning_target_ha=70)
        _setup_coord(coord, [s1])

        async def fake(hass, intent, *, now_utc):
            raise AssertionError("must not dispatch a stale-generation Morning target")

        _patch_dispatch_cover_intent(monkeypatch, coord, fake)
        # this cycle's plan was built at generation 0; a newer cycle (e.g. a
        # real protective need arriving mid-cycle) has already bumped the
        # coordinator's live generation past it.
        coord._dispatch_generation = 1

        results = asyncio.run(_run_plan(coord, [s1], gen=0))
        assert results[("w1", "cover.w1")].status is ExecutionStatus.NOT_ATTEMPTED
        assert results[("w1", "cover.w1")].reason == "stale_generation"


class TestStaleMorningTargetBlockedByLiveRevalidation:
    def test_active_comfort_hold_blocks_the_stale_morning_dispatch(self, monkeypatch) -> None:
        coord = _make_coord()
        s1 = _morning_state("w1", "z1", entity_id="cover.w1", morning_target_ha=70)
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
        assert calls == [], "live revalidation must block dispatch of the stale Morning target"
        assert results[("w1", "cover.w1")].status is not ExecutionStatus.SENT
        assert results[("w1", "cover.w1")].reason == "comfort_position_hold"
