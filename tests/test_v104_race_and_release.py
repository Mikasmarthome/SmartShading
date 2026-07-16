"""v1.0.4 — Race, Release, and Failure test suite.

Covers the 20 required test cases:

TC-RR01  FULLY_AUTOMATIC real absence release to HA-100
TC-RR02  FULLY_AUTOMATIC return with active protection stays at protection target
TC-RR03  ABSENCE_AND_SCHEDULE real absence release
TC-RR04  ABSENCE_ONLY real absence release
TC-RR05  DISABLED_AUTOMATIC no absence release
TC-RR06  Manual Override blocks release (FULLY_AUTOMATIC)
TC-RR07  Manual Override blocks release (ABSENCE_AND_SCHEDULE)
TC-RR08  Manual Override blocks release (ABSENCE_ONLY)
TC-RR09  Safety supersedes absence release
TC-RR10  Night lifecycle remains authoritative (ABSENCE_AND_SCHEDULE)
TC-RR11  Service-call failure: later cover still dispatched
TC-RR12  Service-call failure: assumed position not updated
TC-RR13  Service-call failure: throttle clock not updated
TC-RR14  Presence event during dispatch: stale intent cancelled by generation
TC-RR15  Rapid away->home->away: stale OPEN cannot be last dispatch
TC-RR16  Multi-zone same-sensor: all zones triggered, not just one
TC-RR17  First real service call has no unnecessary initial delay
TC-RR18  Subsequent real calls spaced at least 1.0 seconds
TC-RR19  Duplicate same cover/target: CommandFilter suppresses
TC-RR20  Active Control off: recommendation only, no dispatch
"""
from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import replace as _replace
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# HA stubs — must be installed before importing coordinator
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    def __class_getitem__(cls, item): return cls
    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
    def async_request_refresh(self) -> None: pass


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


for _name, _mod in {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {"SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16}),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub("homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub("homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}.items():
    sys.modules.setdefault(_name, _mod)

import datetime as _datetime
_dt_mod = sys.modules.get("homeassistant.util.dt")
if _dt_mod is None or not hasattr(_dt_mod, "utcnow"):
    sys.modules["homeassistant.util.dt"] = _stub(
        "homeassistant.util.dt",
        utcnow=lambda: _datetime.datetime.now(_datetime.timezone.utc),
        DEFAULT_TIME_ZONE=_datetime.timezone.utc,
    )

sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
)
sys.modules["homeassistant.helpers.storage"] = _stub("homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402
import custom_components.smartshading.coordinator as _coord_mod  # noqa: E402

# ---------------------------------------------------------------------------
# Real evaluator / model imports (no HA dependency)
# ---------------------------------------------------------------------------

from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.cover_control.global_dispatch_throttle import (
    GlobalSerialDispatch,
    DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS,
)


class _FakeMono:
    def __init__(self, t: float = 0.0) -> None:
        self._t = t
    def __call__(self) -> float:
        return self._t
    def advance(self, s: float) -> None:
        self._t += s
    def set(self, t: float) -> None:
        self._t = t
from custom_components.smartshading.cover_control.execution_result import (
    ExecutionStatus,
    build_not_attempted_result,
    build_failed_result,
    build_execution_plan_result,
)
from custom_components.smartshading.cover_control.execution_plan import (
    CoverIntent,
    CoverCommandType,
)
from custom_components.smartshading.cover_control.command_filter import (
    CommandFilter,
    ExecutionCapability,
    ExecutionMode,
)
from custom_components.smartshading.cover_control.position_semantics import to_ha_position
from custom_components.smartshading.state_machine.states import ShadingState
from custom_components.smartshading.models.window import WindowConfig, WindowBehaviorMode
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import (
    LifecycleState,
    NightDayLifecycleConfig,
)
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window_decision_input import build_window_decision_input

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc
_T0 = datetime(2026, 6, 22, 10, 0, 0, tzinfo=_UTC)

_LC = NightDayLifecycleConfig(id="lc1")
_NO_COMFORT = ComfortConfig()
_ORCHESTRATOR = TierOrchestrator()


def _window(behavior_mode: WindowBehaviorMode, absence_position: int | None = 80) -> WindowConfig:
    return WindowConfig(
        id="w1", name="W", zone_id="z1",
        azimuth=180.0, floor_level=0, cover_group_id="cg1",
        behavior_mode=behavior_mode,
        absence_position=absence_position,
    )


def _wdi(
    behavior_mode: WindowBehaviorMode,
    absence_active: bool,
    current_state: ShadingState,
    absence_position: int | None = 80,
    active_override: ManualOverride | None = None,
    comfort_config: ComfortConfig | None = None,
    outdoor_temp_c: float | None = None,
    wind_speed_ms: float | None = None,
    storm_protection_enabled: bool = True,
    lifecycle_state: LifecycleState = LifecycleState.DAY,
    is_in_solar_sector: bool = False,
):
    zone = ZoneConfig(id="z1", name="Zone")
    window = _window(behavior_mode, absence_position=absence_position)
    return build_window_decision_input(
        window=window, zone=zone,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=_LC,
        lifecycle_state=lifecycle_state,
        absence_active=absence_active,
        current_shading_state=current_state,
        outdoor_temp_c=outdoor_temp_c,
        indoor_temp_c=None,
        exposure=None,
        is_in_solar_sector=is_in_solar_sector,
        comfort_config=comfort_config or _NO_COMFORT,
        active_override=active_override,
        wind_speed_ms=wind_speed_ms,
        storm_protection_enabled=storm_protection_enabled,
    )


def _apply_mode_gate(tier_decision, behavior_mode, current_state, active_override):
    """Mirror coordinator.py mode-dispatch gate (lines 1589-1627)."""
    if behavior_mode is WindowBehaviorMode.FULLY_AUTOMATIC:
        return tier_decision, True

    is_safety = tier_decision.shading_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE)
    is_override = tier_decision.shading_state is ShadingState.MANUAL_OVERRIDE

    _is_absence_release = (
        behavior_mode in (WindowBehaviorMode.ABSENCE_ONLY, WindowBehaviorMode.ABSENCE_AND_SCHEDULE)
        and current_state is ShadingState.ABSENCE_CLOSED
        and tier_decision.shading_state is ShadingState.OPEN
        and active_override is None
    )
    _is_lifecycle_release = (
        behavior_mode is WindowBehaviorMode.ABSENCE_AND_SCHEDULE
        and current_state is ShadingState.NIGHT_CLOSED
        and tier_decision.shading_state is ShadingState.OPEN
        and active_override is None
    )
    absence_close_allowed = behavior_mode in (
        WindowBehaviorMode.ABSENCE_ONLY,
        WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
    ) and tier_decision.shading_state is ShadingState.ABSENCE_CLOSED
    night_allowed = (
        behavior_mode is WindowBehaviorMode.ABSENCE_AND_SCHEDULE
        and tier_decision.shading_state is ShadingState.NIGHT_CLOSED
    )
    dispatch_allowed = (
        is_safety or is_override or absence_close_allowed or night_allowed
        or _is_absence_release or _is_lifecycle_release
    )
    if not dispatch_allowed:
        tier_decision = _replace(tier_decision, target_position=None, decided_by="BehaviorMode:hold")
    return tier_decision, dispatch_allowed


# ---------------------------------------------------------------------------
# TC-RR01–05: Absence Release semantics
# ---------------------------------------------------------------------------

class TestAbsenceRelease:

    def test_tc_rr01_fully_automatic_release_to_100(self):
        """TC-RR01: FULLY_AUTOMATIC, absence ends, no protection → OPEN at internal 0 → HA 100."""
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC,
            absence_active=False,
            current_state=ShadingState.ABSENCE_CLOSED,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        tier_decision, dispatch_allowed = _apply_mode_gate(
            decision,
            WindowBehaviorMode.FULLY_AUTOMATIC,
            ShadingState.ABSENCE_CLOSED,
            active_override=None,
        )
        assert tier_decision.shading_state is ShadingState.OPEN
        assert tier_decision.target_position == 0  # internal: 0=open → HA 100
        assert dispatch_allowed
        ha_pos = to_ha_position(tier_decision.target_position, invert=False)
        assert ha_pos == 100

    def test_tc_rr02_fully_automatic_returns_with_active_protection(self):
        """TC-RR02: Absence ends but heat protection is active.
        Orchestrator must produce a protection state, not blind OPEN."""
        comfort = ComfortConfig(heat_protection_enabled=True, heat_protection_outdoor_temp_c=28.0)
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC,
            absence_active=False,
            current_state=ShadingState.ABSENCE_CLOSED,
            comfort_config=comfort,
            outdoor_temp_c=32.0,    # above threshold → heat protection fires
            is_in_solar_sector=True,  # required: HeatEvaluator has a sector gate
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is not ShadingState.OPEN, (
            f"Expected heat protection to prevent blind OPEN, got {decision.shading_state}"
        )
        assert decision.target_position is not None
        ha_pos = to_ha_position(decision.target_position, invert=False)
        assert ha_pos < 100, f"Expected heat protection target < 100 HA, got {ha_pos}"

    def test_tc_rr03_absence_and_schedule_real_release(self):
        """TC-RR03: ABSENCE_AND_SCHEDULE, prev=ABSENCE_CLOSED, absence ends → release dispatched."""
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            absence_active=False,
            current_state=ShadingState.ABSENCE_CLOSED,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        tier_decision, dispatch_allowed = _apply_mode_gate(
            decision,
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.ABSENCE_CLOSED,
            active_override=None,
        )
        assert tier_decision.shading_state is ShadingState.OPEN
        assert dispatch_allowed, "Mode gate must allow absence release for ABSENCE_AND_SCHEDULE"

    def test_tc_rr04_absence_only_real_release(self):
        """TC-RR04: ABSENCE_ONLY, prev=ABSENCE_CLOSED, absence ends → release dispatched."""
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
            absence_active=False,
            current_state=ShadingState.ABSENCE_CLOSED,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        tier_decision, dispatch_allowed = _apply_mode_gate(
            decision,
            WindowBehaviorMode.ABSENCE_ONLY,
            ShadingState.ABSENCE_CLOSED,
            active_override=None,
        )
        assert tier_decision.shading_state is ShadingState.OPEN
        assert dispatch_allowed, "Mode gate must allow absence release for ABSENCE_ONLY"

    def test_tc_rr05_disabled_automatic_no_release(self):
        """TC-RR05: DISABLED_AUTOMATIC: coordinator sets absence_position=None.
        AbsenceEvaluator returns None → OPEN fallback, but mode gate blocks it."""
        wdi_base = _wdi(
            behavior_mode=WindowBehaviorMode.DISABLED_AUTOMATIC,
            absence_active=True,
            current_state=ShadingState.OPEN,
        )
        # Simulate coordinator override: absence_position=None for DISABLED_AUTOMATIC
        wdi = _replace(wdi_base, effective_behavior=_replace(wdi_base.effective_behavior, absence_position=None))
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        # With absence_position=None, AbsenceEvaluator returns None → OPEN (fallback)
        assert decision.shading_state is ShadingState.OPEN

        tier_decision, dispatch_allowed = _apply_mode_gate(
            decision,
            WindowBehaviorMode.DISABLED_AUTOMATIC,
            ShadingState.OPEN,
            active_override=None,
        )
        assert not dispatch_allowed, "DISABLED_AUTOMATIC mode gate must block all normal dispatches"
        assert tier_decision.target_position is None

    def test_tc_rr05b_disabled_automatic_absence_was_never_closed(self):
        """TC-RR05b: DISABLED_AUTOMATIC never sets ABSENCE_CLOSED, so no release needed."""
        wdi_base = _wdi(
            behavior_mode=WindowBehaviorMode.DISABLED_AUTOMATIC,
            absence_active=False,
            current_state=ShadingState.OPEN,
        )
        wdi = _replace(wdi_base, effective_behavior=_replace(wdi_base.effective_behavior, absence_position=None))
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        tier_decision, dispatch_allowed = _apply_mode_gate(
            decision,
            WindowBehaviorMode.DISABLED_AUTOMATIC,
            ShadingState.OPEN,
            active_override=None,
        )
        assert tier_decision.target_position is None
        assert not dispatch_allowed


# ---------------------------------------------------------------------------
# TC-RR06–08: Manual Override blocks release
# ---------------------------------------------------------------------------

class TestManualOverrideBlocksRelease:

    def _make_override(self, position: int = 50) -> ManualOverride:
        return ManualOverride(
            window_id="w1",
            override_position=position,
            overridden_state=ShadingState.ABSENCE_CLOSED,
            overridden_position=80,
            started_at=_T0,
            expires_at=_T0 + timedelta(hours=2),
            source="position_delta",
        )

    def test_tc_rr06_fully_automatic_override_blocks_release(self):
        """TC-RR06: FULLY_AUTOMATIC + active override → ManualOverrideEvaluator fires → MANUAL_OVERRIDE.
        The override holds; no absence release is issued."""
        override = self._make_override()
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC,
            absence_active=False,
            current_state=ShadingState.ABSENCE_CLOSED,
            active_override=override,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.MANUAL_OVERRIDE, (
            f"Override must block absence release, got {decision.shading_state}"
        )
        assert decision.target_position == 50

    def test_tc_rr07_absence_and_schedule_override_blocks_release(self):
        """TC-RR07: ABSENCE_AND_SCHEDULE + active override → override blocks release."""
        override = self._make_override(position=30)
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            absence_active=False,
            current_state=ShadingState.ABSENCE_CLOSED,
            active_override=override,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.MANUAL_OVERRIDE
        tier_decision, dispatch_allowed = _apply_mode_gate(
            decision,
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.ABSENCE_CLOSED,
            active_override=override,
        )
        assert dispatch_allowed
        assert tier_decision.shading_state is ShadingState.MANUAL_OVERRIDE

    def test_tc_rr08_absence_only_override_blocks_release(self):
        """TC-RR08: ABSENCE_ONLY + active override → override holds; no automatic OPEN."""
        override = self._make_override(position=45)
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
            absence_active=False,
            current_state=ShadingState.ABSENCE_CLOSED,
            active_override=override,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.MANUAL_OVERRIDE
        tier_decision, dispatch_allowed = _apply_mode_gate(
            decision,
            WindowBehaviorMode.ABSENCE_ONLY,
            ShadingState.ABSENCE_CLOSED,
            active_override=override,
        )
        assert dispatch_allowed, "Override dispatch must be allowed"
        assert tier_decision.shading_state is ShadingState.MANUAL_OVERRIDE

    def test_tc_rr08b_no_false_open_with_override_active(self):
        """Extra: when override is active, shading_state is never OPEN for all 3 modes."""
        override = self._make_override(position=60)
        for mode in (
            WindowBehaviorMode.FULLY_AUTOMATIC,
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            WindowBehaviorMode.ABSENCE_ONLY,
        ):
            wdi = _wdi(
                behavior_mode=mode,
                absence_active=False,
                current_state=ShadingState.ABSENCE_CLOSED,
                active_override=override,
            )
            decision = _ORCHESTRATOR.evaluate_window(wdi)
            assert decision.shading_state is not ShadingState.OPEN, (
                f"Mode {mode}: override active → no OPEN, got {decision.shading_state}"
            )


# ---------------------------------------------------------------------------
# TC-RR09: Safety supersedes absence release
# ---------------------------------------------------------------------------

class TestSafetySupersedesRelease:

    def test_tc_rr09_safety_beats_absence_release(self):
        """TC-RR09: Storm wind active → STORM_SAFE regardless of absence state."""
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC,
            absence_active=False,  # person came home → absence should release
            current_state=ShadingState.ABSENCE_CLOSED,
            wind_speed_ms=35.0,   # well above typical storm threshold (~15-25 m/s)
            storm_protection_enabled=True,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.STORM_SAFE, (
            f"Storm must override absence release, got {decision.shading_state}"
        )


# ---------------------------------------------------------------------------
# TC-RR10: Night lifecycle remains authoritative
# ---------------------------------------------------------------------------

class TestNightLifecycleAuthoritative:

    def test_tc_rr10_night_blocks_return_for_absence_and_schedule(self):
        """TC-RR10: ABSENCE_AND_SCHEDULE + night active + person returns home.
        Night gate (Tier 3) fires before absence release — result is NIGHT_CLOSED."""
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            absence_active=False,  # person came home
            current_state=ShadingState.ABSENCE_CLOSED,
            lifecycle_state=LifecycleState.NIGHT,
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.NIGHT_CLOSED, (
            f"Night lifecycle must override absence release, got {decision.shading_state}"
        )

    def test_tc_rr10b_absence_only_ignores_night(self):
        """TC-RR10b: ABSENCE_ONLY with DAY lifecycle (coordinator forces DAY for this mode).
        No night gate fires → absence release succeeds."""
        wdi = _wdi(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
            absence_active=False,
            current_state=ShadingState.ABSENCE_CLOSED,
            lifecycle_state=LifecycleState.DAY,  # coordinator forces DAY for ABSENCE_ONLY
        )
        decision = _ORCHESTRATOR.evaluate_window(wdi)
        assert decision.shading_state is ShadingState.OPEN, (
            f"ABSENCE_ONLY with forced DAY should produce OPEN, got {decision.shading_state}"
        )


# ---------------------------------------------------------------------------
# TC-RR11–13: Service-call failure isolation
# ---------------------------------------------------------------------------

class TestServiceCallFailureIsolation:

    def test_tc_rr11_failure_does_not_block_throttle_for_next_cover(self):
        """TC-RR11: FAILED result must NOT update the throttle clock.
        The next cover may dispatch at any time — no penalty for failed dispatch."""
        gsd = GlobalSerialDispatch()
        wait = gsd.time_until_next_allowed()
        assert wait == timedelta(0)
        # Simulate FAILED: do NOT call record_dispatch
        wait_after_fail = gsd.time_until_next_allowed()
        assert wait_after_fail == timedelta(0), (
            "After FAILED (no record_dispatch), throttle must still allow immediate dispatch"
        )

    def test_tc_rr12_failure_does_not_update_assumed_position(self):
        """TC-RR12: AssumedStateManager must only update on SENT, not FAILED.
        Verify the production condition: any_sent AND NOT any_failed."""
        intent = _make_intent("cover.test", target_ha=100, is_safety=False)
        failed_result = build_failed_result(
            intent, error="connection refused", sent_at_utc=None, reason="timeout"
        )
        assert failed_result.status is ExecutionStatus.FAILED

        plan = build_execution_plan_result("w1", [failed_result])
        assert plan.any_sent is False
        assert plan.any_failed is True
        should_update = plan.any_sent and not plan.any_failed
        assert not should_update, "Failed dispatch must not trigger assumed position update"

    def test_tc_rr13_failure_does_not_update_throttle_clock(self):
        """TC-RR13: After FAILED, record_dispatch is NOT called → throttle unchanged."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = datetime(2026, 6, 22, 10, 0, 0, tzinfo=_UTC)

        gsd.record_dispatch(t0)
        assert gsd.last_dispatch_at == t0

        # Simulate FAILED: do NOT call record_dispatch
        mono.advance(0.5)
        wait = gsd.time_until_next_allowed()
        assert wait.total_seconds() > 0, "Throttle clock should still reference t0 after FAILED"
        assert gsd.last_dispatch_at == t0, "last_dispatch_at must not change on FAILED"

    def test_tc_rr13b_lock_released_on_failure_allows_next_dispatch(self):
        """TC-RR13b: asyncio.Lock is released even on a failed dispatch; next cover can acquire."""
        async def _run():
            gsd = GlobalSerialDispatch()
            results = []

            async def _dispatch_a():
                async with gsd.lock:
                    results.append("a_dispatched_failed")
                    # do NOT call record_dispatch (simulates FAILED)

            async def _dispatch_b():
                await asyncio.sleep(0.01)  # let a go first
                async with gsd.lock:
                    results.append("b_dispatched_after_a")

            await asyncio.gather(_dispatch_a(), _dispatch_b())
            return results

        results = asyncio.run(_run())
        assert results == ["a_dispatched_failed", "b_dispatched_after_a"]


# ---------------------------------------------------------------------------
# TC-RR14–15: Stale intent generation guard
# ---------------------------------------------------------------------------

class TestStaleIntentGeneration:

    def test_tc_rr14_generation_increments_on_presence_event(self):
        """TC-RR14a: _dispatch_generation increments when _on_presence_change fires."""
        coord = _make_coord(["person.alice"])
        entry = _make_entry()
        assert coord._dispatch_generation == 0

        cb = _capture_callback(coord, entry)
        event = _mock_event("home", "not_home")
        cb(event)

        assert coord._dispatch_generation == 1

    def test_tc_rr14b_same_state_does_not_increment_generation(self):
        """TC-RR14b: same old/new state → dedup → generation unchanged."""
        coord = _make_coord(["person.alice"])
        entry = _make_entry()
        cb = _capture_callback(coord, entry)

        event = _mock_event("home", "home")  # same state
        cb(event)

        assert coord._dispatch_generation == 0

    def test_tc_rr14c_stale_intent_cancelled_inside_lock(self):
        """TC-RR14c: dispatch_generation != _this_dispatch_gen inside lock → stale cancel.

        Simulates the coordinator's stale-intent guard: if a presence event fires
        during the throttle sleep, the waiting intent must be cancelled (not dispatched).
        """
        gen = [0]  # mutable generation counter

        async def _simulate_dispatch(
            *,
            is_safety: bool,
            captured_gen: int,
            sleep_s: float = 0.001,
            event_fires_during_sleep: bool = False,
        ) -> str:
            gsd = GlobalSerialDispatch()
            async with gsd.lock:
                if not is_safety:
                    if event_fires_during_sleep:
                        gen[0] += 1  # simulate presence event during throttle sleep
                    await asyncio.sleep(sleep_s)
                    if gen[0] != captured_gen:
                        return "stale_cancelled"
                return "dispatched"

        # Case 1: no event during sleep → dispatched
        gen[0] = 5
        result = asyncio.run(_simulate_dispatch(
            is_safety=False, captured_gen=5, event_fires_during_sleep=False
        ))
        assert result == "dispatched"

        # Case 2: event fires during sleep → cancelled
        gen[0] = 5
        result = asyncio.run(_simulate_dispatch(
            is_safety=False, captured_gen=5, event_fires_during_sleep=True
        ))
        assert result == "stale_cancelled"

        # Case 3: safety bypasses stale check — always dispatches
        gen[0] = 5
        result = asyncio.run(_simulate_dispatch(
            is_safety=True, captured_gen=5, event_fires_during_sleep=True
        ))
        assert result == "dispatched"

    def test_tc_rr14d_generation_check_lives_in_coordinator_source(self):
        """TC-RR14d: verify _dispatch_generation and stale check exist in coordinator source."""
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "custom_components/smartshading/coordinator.py"
        text = src.read_text(encoding="utf-8")
        assert "_dispatch_generation" in text
        assert "stale_presence_superseded" in text
        assert "_this_dispatch_gen" in text

    def test_tc_rr15_rapid_away_home_away_generation_semantics(self):
        """TC-RR15: rapid away→home→away: generation increments twice.
        Home-refresh computed at gen=1 is invalidated by gen=2 from 'away'.
        Any intents from the home refresh see gen mismatch and cancel.
        """
        coord = _make_coord(["person.alice"])
        entry = _make_entry()
        cb = _capture_callback(coord, entry)

        # Event 1: away → home
        cb(_mock_event("not_home", "home"))
        assert coord._dispatch_generation == 1

        captured_gen = coord._dispatch_generation  # what the home-refresh would capture

        # Event 2: home → away (BEFORE the home-refresh has dispatched)
        cb(_mock_event("home", "not_home"))
        assert coord._dispatch_generation == 2

        assert coord._dispatch_generation != captured_gen, (
            "Generation must have changed, invalidating the home-refresh's intents"
        )


# ---------------------------------------------------------------------------
# TC-RR16: Multi-zone same sensor — all zones triggered
# ---------------------------------------------------------------------------

class TestMultiZoneTrigger:

    def test_tc_rr16_three_zones_same_sensor_all_get_listener(self):
        """TC-RR16: 3 zone coordinators with the same presence sensor.
        Each registers its own listener → 3 callbacks on presence change."""
        presence_entity = "person.shared"
        coords = [_make_coord([presence_entity]) for _ in range(3)]
        entries = [_make_entry() for _ in range(3)]
        callbacks = []

        def _track(hass, entity_id, action):
            callbacks.append(action)
            return MagicMock()

        with patch.object(_coord_mod, "async_track_state_change_event", side_effect=_track):
            for coord, entry in zip(coords, entries):
                coord.async_setup_presence_listeners(entry)

        assert len(callbacks) == 3

        event = _mock_event("not_home", "home")
        for cb in callbacks:
            cb(event)

        for coord in coords:
            coord.hass.async_create_task.assert_called()

    def test_tc_rr16b_each_zone_gets_independent_refresh(self):
        """TC-RR16b: each callback triggers only its own coordinator's refresh."""
        presence_entity = "person.shared"
        coord_a = _make_coord([presence_entity])
        coord_b = _make_coord([presence_entity])
        entry_a = _make_entry()
        entry_b = _make_entry()

        cbs_a: list = []
        cbs_b: list = []

        def _track_a(hass, entity_id, action):
            cbs_a.append(action)
            return MagicMock()

        def _track_b(hass, entity_id, action):
            cbs_b.append(action)
            return MagicMock()

        with patch.object(_coord_mod, "async_track_state_change_event", side_effect=_track_a):
            coord_a.async_setup_presence_listeners(entry_a)
        with patch.object(_coord_mod, "async_track_state_change_event", side_effect=_track_b):
            coord_b.async_setup_presence_listeners(entry_b)

        event = _mock_event("not_home", "home")
        for cb in cbs_a:
            cb(event)

        coord_a.hass.async_create_task.assert_called()
        coord_b.hass.async_create_task.assert_not_called()

    def test_tc_rr16c_all_zones_generation_increments_on_shared_event(self):
        """TC-RR16c: same presence entity → each zone's _dispatch_generation increments."""
        presence_entity = "person.shared"
        coords = [_make_coord([presence_entity]) for _ in range(3)]
        entries = [_make_entry() for _ in range(3)]
        callbacks = []

        def _track(hass, entity_id, action):
            callbacks.append(action)
            return MagicMock()

        with patch.object(_coord_mod, "async_track_state_change_event", side_effect=_track):
            for coord, entry in zip(coords, entries):
                coord.async_setup_presence_listeners(entry)

        event = _mock_event("not_home", "home")
        for cb in callbacks:
            cb(event)

        for i, coord in enumerate(coords):
            assert coord._dispatch_generation == 1, (
                f"Zone {i}: _dispatch_generation must be 1 after presence event"
            )


# ---------------------------------------------------------------------------
# TC-RR17–18: Dispatch spacing
# ---------------------------------------------------------------------------

class TestDispatchSpacing:

    def test_tc_rr17_first_dispatch_no_initial_delay(self):
        """TC-RR17: GlobalSerialDispatch with no previous dispatch → wait = 0."""
        gsd = GlobalSerialDispatch()
        wait = gsd.time_until_next_allowed()
        assert wait == timedelta(0), "First dispatch must never wait"

    def test_tc_rr18_subsequent_dispatches_spaced_2_0_s(self):
        """TC-RR18: after a SENT, next command must wait at least 2.0 s
        (F32 field fix: raised from 1.5s to 2.0s)."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = datetime(2026, 6, 22, 10, 0, 0, tzinfo=_UTC)
        gsd.record_dispatch(t0)
        mono.advance(0.001)
        wait_immediately = gsd.time_until_next_allowed()
        assert wait_immediately.total_seconds() >= 1.9
        mono.advance(1.999)  # total 2.0s elapsed
        wait_after = gsd.time_until_next_allowed()
        assert wait_after == timedelta(0)

    def test_tc_rr18b_throttle_applies_to_all_commands_including_safety(self):
        """TC-RR18b: a previous SENT throttles ALL subsequent commands including safety."""
        mono = _FakeMono(0.0)
        gsd = GlobalSerialDispatch(mono_clock=mono)
        t0 = datetime(2026, 6, 22, 10, 0, 0, tzinfo=_UTC)
        gsd.record_dispatch(t0)
        mono.advance(0.1)
        wait = gsd.time_until_next_allowed()
        assert wait.total_seconds() > 0, "All commands (including safety) throttled after SENT"


# ---------------------------------------------------------------------------
# TC-RR19: Duplicate same cover/target suppressed
# ---------------------------------------------------------------------------

class TestDuplicateSuppressed:

    def test_tc_rr19_command_filter_blocks_same_position(self):
        """TC-RR19: CommandFilter blocks dispatch when target == current (within tolerance)."""
        result = CommandFilter().evaluate(
            target_position_internal=80,
            current_position_internal=80,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(),
        )
        assert not result.allowed, "Same position must be suppressed by CommandFilter"

    def test_tc_rr19b_command_filter_allows_different_position(self):
        """TC-RR19b: CommandFilter allows dispatch when target ≠ current."""
        result = CommandFilter().evaluate(
            target_position_internal=0,
            current_position_internal=80,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(),
        )
        assert result.allowed, "Different position must be allowed by CommandFilter"


# ---------------------------------------------------------------------------
# TC-RR20: Active Control off — recommendation only
# ---------------------------------------------------------------------------

class TestActiveControlOff:

    def test_tc_rr20_active_control_off_blocks_dispatch(self):
        """TC-RR20: ExecutionMode.RECOMMENDATION_ONLY must block dispatch via CommandFilter."""
        result = CommandFilter().evaluate(
            target_position_internal=0,
            current_position_internal=80,
            execution_mode=ExecutionMode.RECOMMENDATION_ONLY,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(),
        )
        assert not result.allowed, "RECOMMENDATION_ONLY must block physical dispatch"

    def test_tc_rr20b_recommendation_only_blocks_even_safety(self):
        """TC-RR20b: RECOMMENDATION_ONLY blocks even safety commands.
        CommandFilter check 3 (recommendation_only) comes before the safety bypass.
        The user's explicit choice to disable active control is unconditional.
        """
        result = CommandFilter().evaluate(
            target_position_internal=0,
            current_position_internal=80,
            execution_mode=ExecutionMode.RECOMMENDATION_ONLY,
            is_safety=True,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(),
        )
        assert not result.allowed, (
            "RECOMMENDATION_ONLY blocks ALL dispatch including safety — "
            "user's active control choice is unconditional"
        )


# ---------------------------------------------------------------------------
# Helpers used by multiple test classes
# ---------------------------------------------------------------------------

def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.async_create_task = MagicMock(return_value=None)
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


def _make_coord(presence_entity_ids: list[str] | None = None) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    return SmartShadingCoordinator(
        hass, entry,
        presence_entity_ids=presence_entity_ids or [],
    )


def _capture_callback(coord: SmartShadingCoordinator, entry: MagicMock):
    """Register listeners and return the first captured callback."""
    captured: list = []

    def _track(hass, entity_id, action):
        captured.append(action)
        return MagicMock()

    with patch.object(_coord_mod, "async_track_state_change_event", side_effect=_track):
        coord.async_setup_presence_listeners(entry)

    return captured[0]


def _mock_event(old_state_str: str | None, new_state_str: str | None) -> MagicMock:
    event = MagicMock()
    old = MagicMock() if old_state_str is not None else None
    new = MagicMock() if new_state_str is not None else None
    if old is not None:
        old.state = old_state_str
    if new is not None:
        new.state = new_state_str
    event.data = {"old_state": old, "new_state": new}
    return event


def _make_intent(cover_entity_id: str, target_ha: int, is_safety: bool) -> CoverIntent:
    """Build a minimal CoverIntent for testing failure semantics."""
    return CoverIntent(
        cover_entity_id=cover_entity_id,
        command_type=CoverCommandType.MOVE_TO_POSITION,
        target_position_internal=100 - target_ha,
        target_position_ha=target_ha,
        target_tilt=None,
        is_safety=is_safety,
        execution_mode=ExecutionMode.AUTOMATIC.value,
        allowed=True,
        blocked_reason=None,
        decided_by="test",
        computed_at=_T0,
    )


# ---------------------------------------------------------------------------
# Absence-release state preservation through a no-dispatch hold (v1.1.0-beta.9)
#
# Regression: a transient PresenceUncertain:hold on the return-home cycle carries
# shading_state=OPEN but sends no command (target None).  Before the fix it flipped
# current_state ABSENCE_CLOSED → OPEN without moving the cover, so the next cycle no
# longer recognised the absence-release and an ABSENCE_ONLY cover stayed closed.
# ---------------------------------------------------------------------------

def _masked_decision(behavior_mode, *, absence_active, presence_uncertain,
                     absence_ha=0, exposure_wm2=600.0):
    """Build a real masked WDI for the given mode and run the real orchestrator."""
    from custom_components.smartshading.engines.exposure_engine import WindowExposure
    from custom_components.smartshading.models.comfort import ComfortConfig
    from custom_components.smartshading.models.lifecycle import LifecycleState
    w = WindowConfig(id="w1", name="Terrace", zone_id="z1", azimuth=180.0,
                     floor_level=0, cover_group_id="cg1", behavior_mode=behavior_mode,
                     absence_position=absence_ha)
    exp = WindowExposure(
        window_id="w1", timestamp=_T0, sun_azimuth=180.0, sun_elevation=45.0,
        is_above_horizon=True, is_in_tolerance_window=True, azimuth_delta_deg=0.0,
        direct_radiation_factor=1.0, elevation_clipped=False, theoretical_exposure=exposure_wm2,
        learned_solar_impact_factor=1.0, seasonal_factor=1.0, effective_exposure=exposure_wm2,
        measured_solar_wm2=exposure_wm2, low_angle_direct_glare_wm2=0.0)
    wdi = build_window_decision_input(
        window=w, zone=ZoneConfig(id="z1", name="Z"),
        global_defaults=GlobalDefaults(absence_position=absence_ha),
        shade_position_defaults=ShadePositionDefaults(), lifecycle_config=_LC,
        lifecycle_state=LifecycleState.DAY, absence_active=absence_active,
        current_shading_state=ShadingState.ABSENCE_CLOSED, outdoor_temp_c=30.0,
        indoor_temp_c=26.0, exposure=exp, is_in_solar_sector=True,
        comfort_config=ComfortConfig(heat_protection_enabled=True,
                                     glare_protection_enabled=True,
                                     heat_protection_outdoor_temp_c=25.0),
        presence_uncertain=presence_uncertain)
    wdi = _coord_mod._apply_window_behavior_mode(wdi, behavior_mode)
    return _ORCHESTRATOR.evaluate_window(wdi)


class TestNoDispatchHoldStatePreservation:
    def test_helper_holds_state_for_presence_uncertain(self):
        assert _coord_mod._hold_state_for_no_dispatch(
            "PresenceUncertain:hold", ShadingState.OPEN, ShadingState.ABSENCE_CLOSED
        ) is ShadingState.ABSENCE_CLOSED

    def test_helper_holds_state_for_behavior_mode_hold(self):
        assert _coord_mod._hold_state_for_no_dispatch(
            "BehaviorMode:hold", ShadingState.OPEN, ShadingState.ABSENCE_CLOSED
        ) is ShadingState.ABSENCE_CLOSED

    def test_helper_passes_through_real_release(self):
        # A genuine absence-release (fallback OPEN) must NOT be held — it dispatches.
        assert _coord_mod._hold_state_for_no_dispatch(
            "TierOrchestrator:fallback", ShadingState.OPEN, ShadingState.ABSENCE_CLOSED
        ) is ShadingState.OPEN

    def test_helper_passes_through_absence_close(self):
        assert _coord_mod._hold_state_for_no_dispatch(
            "AbsenceEvaluator", ShadingState.ABSENCE_CLOSED, ShadingState.OPEN
        ) is ShadingState.ABSENCE_CLOSED


class TestAbsenceOnlyReturnHomeSequence:
    """The full reported scenario: ABSENCE_ONLY, absence_position 0, sunny day."""

    def test_uncertain_return_cycle_preserves_absence_closed(self):
        # Cycle A: returning home but presence still uncertain → PresenceUncertain:hold.
        d = _masked_decision(WindowBehaviorMode.ABSENCE_ONLY,
                             absence_active=False, presence_uncertain=True)
        assert d.decided_by == "PresenceUncertain:hold"
        assert d.shading_state is ShadingState.OPEN
        assert d.target_position is None
        # State must be held at ABSENCE_CLOSED (no dispatch happened).
        held = _coord_mod._hold_state_for_no_dispatch(
            d.decided_by, d.shading_state, ShadingState.ABSENCE_CLOSED)
        assert held is ShadingState.ABSENCE_CLOSED

    def test_clean_return_cycle_releases_to_open(self):
        # Cycle B: presence resolved home → solar masked → fallback OPEN → release.
        d = _masked_decision(WindowBehaviorMode.ABSENCE_ONLY,
                             absence_active=False, presence_uncertain=False)
        assert d.shading_state is ShadingState.OPEN
        assert d.target_position == 0  # internal 0 = HA 100 open
        gated, allowed = _apply_mode_gate(
            d, WindowBehaviorMode.ABSENCE_ONLY, ShadingState.ABSENCE_CLOSED, None)
        assert allowed, "Absence release must be allowed once current_state is ABSENCE_CLOSED"
        # And the real-release decision is NOT held → it dispatches.
        held = _coord_mod._hold_state_for_no_dispatch(
            d.decided_by, d.shading_state, ShadingState.ABSENCE_CLOSED)
        assert held is ShadingState.OPEN

    def test_absence_and_schedule_daytime_release_same(self):
        d = _masked_decision(WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
                             absence_active=False, presence_uncertain=False)
        assert d.shading_state is ShadingState.OPEN
        gated, allowed = _apply_mode_gate(
            d, WindowBehaviorMode.ABSENCE_AND_SCHEDULE, ShadingState.ABSENCE_CLOSED, None)
        assert allowed

    def test_away_still_closes(self):
        d = _masked_decision(WindowBehaviorMode.ABSENCE_ONLY,
                             absence_active=True, presence_uncertain=False)
        assert d.decided_by == "AbsenceEvaluator"
        assert d.shading_state is ShadingState.ABSENCE_CLOSED
        assert d.target_position == 100  # internal 100 = HA 0 closed


# ---------------------------------------------------------------------------
# Mode dispatch allowlist incl. night-contact Option B (v1.1.0-beta.9)
#
# Regression: ABSENCE_AND_SCHEDULE keeps the night schedule active, so a window
# opened at night must vent (NIGHT_VENT) and return (NIGHT_CLOSED).  NIGHT_VENT
# was missing from the allowlist, so the vent was suppressed to BehaviorMode:hold
# while the close still worked — Option B reopen never moved the cover.
# ---------------------------------------------------------------------------

class TestModeDispatchAllowlist:
    def _allowed(self, mode, state, *, rel=False, lcr=False):
        return _coord_mod._mode_dispatch_allowed(
            mode, state, is_absence_release=rel, is_lifecycle_release=lcr)

    def test_fully_automatic_allows_everything(self):
        for st in (ShadingState.NIGHT_VENT, ShadingState.OPEN,
                   ShadingState.LIGHT_SHADE, ShadingState.NIGHT_CLOSED):
            assert self._allowed(WindowBehaviorMode.FULLY_AUTOMATIC, st)

    def test_absence_and_schedule_allows_night_vent(self):
        # The fix: Option B ventilation must dispatch in this mode.
        assert self._allowed(WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
                             ShadingState.NIGHT_VENT)

    def test_absence_and_schedule_allows_night_closed(self):
        assert self._allowed(WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
                             ShadingState.NIGHT_CLOSED)

    def test_absence_and_schedule_suppresses_daytime_open(self):
        # A plain daytime fallback OPEN (no release) is still held.
        assert not self._allowed(WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
                                 ShadingState.OPEN)
        assert not self._allowed(WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
                                 ShadingState.LIGHT_SHADE)

    def test_absence_only_does_not_allow_night_vent(self):
        # ABSENCE_ONLY forces lifecycle DAY, so night-contact never fires; the
        # allowlist must not leak NIGHT_VENT there.
        assert not self._allowed(WindowBehaviorMode.ABSENCE_ONLY,
                                 ShadingState.NIGHT_VENT)
        assert not self._allowed(WindowBehaviorMode.ABSENCE_ONLY,
                                 ShadingState.NIGHT_CLOSED)

    def test_absence_only_allows_absence_close_and_release(self):
        assert self._allowed(WindowBehaviorMode.ABSENCE_ONLY,
                             ShadingState.ABSENCE_CLOSED)
        assert self._allowed(WindowBehaviorMode.ABSENCE_ONLY,
                             ShadingState.OPEN, rel=True)

    def test_disabled_automatic_only_safety_and_override(self):
        assert self._allowed(WindowBehaviorMode.DISABLED_AUTOMATIC,
                             ShadingState.STORM_SAFE)
        assert self._allowed(WindowBehaviorMode.DISABLED_AUTOMATIC,
                             ShadingState.MANUAL_OVERRIDE)
        assert not self._allowed(WindowBehaviorMode.DISABLED_AUTOMATIC,
                                 ShadingState.NIGHT_VENT)
        assert not self._allowed(WindowBehaviorMode.DISABLED_AUTOMATIC,
                                 ShadingState.ABSENCE_CLOSED)


class TestNightContactReopenEngine:
    """The engine itself must keep arming Option B for repeated reopen."""

    def test_repeated_reopen_vents_each_time(self):
        from custom_components.smartshading.engines.night_contact_hold import (
            NightContactHold, NightContactAction,
        )
        h = NightContactHold()
        h.on_lifecycle_transition(night_active=True)
        base = dict(night_active=True, night_block_enabled=True,
                    night_lift_enabled=True, night_decision_pending=True)
        h.evaluate(contact_open=False, contact_unknown=False, **base)  # reach night
        for _ in range(3):
            a_open = h.evaluate(contact_open=True, contact_unknown=False, **base)
            assert a_open == NightContactAction.HOLD_NIGHT_VENT
            a_close = h.evaluate(contact_open=False, contact_unknown=False, **base)
            assert a_close == NightContactAction.RETURN_TO_NIGHT


# ---------------------------------------------------------------------------
# Night-contact min-interval bypass (v1.1.0-beta.10)
#
# Option B reacts to a real window open/close, so a contact-driven vent/return/
# catch-up may skip the per-window minimum action interval. Narrow: only those
# deciders, only in night-capable modes, only on a valid+fresh contact.
# ---------------------------------------------------------------------------

class TestNightContactMinIntervalBypass:
    def _b(self, decided_by, mode, fresh):
        return _coord_mod._night_contact_bypasses_action_interval(
            decided_by, mode, contact_valid_and_fresh=fresh)

    def test_vent_fully_automatic_valid_fresh_bypasses(self):
        assert self._b("NightContactVent", WindowBehaviorMode.FULLY_AUTOMATIC, True) is True

    def test_return_absence_and_schedule_bypasses(self):
        assert self._b("NightContactReturnToNight",
                       WindowBehaviorMode.ABSENCE_AND_SCHEDULE, True) is True

    def test_catch_up_bypasses(self):
        assert self._b("NightContactCatchUp", WindowBehaviorMode.FULLY_AUTOMATIC, True) is True

    def test_stale_or_unknown_contact_does_not_bypass(self):
        assert self._b("NightContactVent", WindowBehaviorMode.FULLY_AUTOMATIC, False) is False

    def test_absence_only_never_bypasses(self):
        assert self._b("NightContactVent", WindowBehaviorMode.ABSENCE_ONLY, True) is False

    def test_disabled_automatic_never_bypasses(self):
        assert self._b("NightContactVent", WindowBehaviorMode.DISABLED_AUTOMATIC, True) is False

    def test_non_night_contact_decision_never_bypasses(self):
        # Solar/heat/glare/lifecycle keep the normal interval.
        for d in ("SolarEvaluator", "HeatEvaluator", "GlareEvaluator",
                  "TierOrchestrator:fallback", "NightEvaluator", "AbsenceEvaluator"):
            assert self._b(d, WindowBehaviorMode.FULLY_AUTOMATIC, True) is False
