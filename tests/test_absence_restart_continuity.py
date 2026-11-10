"""A restart or reload must not release an absence by itself.

After a restart while the household is still away, the restored window state is
ABSENCE_CLOSED but the runtime-only PresenceDebouncer starts its activation delay
over, so ``absence_active`` read False for ``absence_delay_min`` and the daytime
fallback could open the cover.  The coordinator now asks the debouncer to continue
the absence (``resume_absence``) on the first definitive AWAY reading after a restore
that brought back an ABSENCE_CLOSED window.

Coverage:
  ARC-01 PresenceDebouncer.resume_absence: semantics, one-shot, no effect on a tracked absence.
  ARC-02 Decision replay with the real TierOrchestrator -> ComfortMovementHold ->
         CommandFilter chain: without the resume the first fallback open goes out after
         the F29 confirmation; with it the absence holds.
  ARC-03 The resumed absence is exactly an ordinary active absence, so every
         higher-priority decision keeps its priority (safety, manual override, night,
         night window contact) and the morning/day boundary is unchanged.
  ARC-04 Per-window absence positions (including 0 %) and the solar sector.
  ARC-05 The coordinator wiring (source contract; the behaviour itself is covered by
         tests/real_ha/test_real_ha_absence_restart.py).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.smartshading.cover_control.command_filter import (
    CommandFilter,
    ExecutionCapability,
    ExecutionMode,
)
from custom_components.smartshading.cover_control.position_semantics import to_ha_position
from custom_components.smartshading.engines.comfort_movement_hold import ComfortMovementHold
from custom_components.smartshading.engines.contact_engine import ContactStatus
from custom_components.smartshading.engines.exposure_engine import WindowExposure
from custom_components.smartshading.engines.lifecycle_engine import PresenceDebouncer
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

T0 = datetime(2026, 6, 19, 13, 30, tzinfo=timezone.utc)
FALLBACK = "TierOrchestrator:fallback"
ORCH = TierOrchestrator()
DELAY = 30
_NO_COMFORT = ComfortConfig(
    heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False)


def _window(**kw) -> WindowConfig:
    return WindowConfig(id="w-south", name="S", zone_id="z1", azimuth=180.0, floor_level=0,
                        cover_group_id="cg", **kw)


def _exposure(wm2: float) -> WindowExposure:
    return WindowExposure(
        window_id="w-south", timestamp=T0, sun_azimuth=180.0, sun_elevation=45.0,
        is_above_horizon=True, is_in_tolerance_window=True, azimuth_delta_deg=0.0,
        direct_radiation_factor=1.0, elevation_clipped=False, theoretical_exposure=wm2,
        learned_solar_impact_factor=1.0, seasonal_factor=1.0, effective_exposure=wm2)


def _wdi(*, absence_active, window=None, zone=None, in_sector=True, exposure=40.0,
         lifecycle=LifecycleState.DAY, state=ShadingState.ABSENCE_CLOSED, override=None,
         wind=None, contact=None, block_on_open=False):
    return build_window_decision_input(
        window=window or _window(), zone=zone or ZoneConfig(id="z1", name="Z"),
        global_defaults=GlobalDefaults(absence_position=30),
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=60, normal_shade_position=50, strong_shade_position=10),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=lifecycle, absence_active=absence_active, current_shading_state=state,
        outdoor_temp_c=None, indoor_temp_c=None,
        exposure=_exposure(exposure) if exposure is not None else None,
        is_in_solar_sector=in_sector, comfort_config=_NO_COMFORT, active_override=override,
        wind_speed_ms=wind, storm_protection_enabled=wind is not None, wind_protection_enabled=True,
        wind_threshold_ms=10.0, contact_status=contact, night_block_on_window_open=block_on_open)


# ---------------------------------------------------------------------------
# ARC-01
# ---------------------------------------------------------------------------


class TestDebouncerResume:
    def test_without_resume_a_restarted_debouncer_waits_for_the_delay(self):
        d = PresenceDebouncer()
        assert d.is_absence_active(False, T0, DELAY) is False
        assert d.is_absence_active(False, T0 + timedelta(minutes=DELAY - 1), DELAY) is False
        assert d.is_absence_active(False, T0 + timedelta(minutes=DELAY), DELAY) is True

    @pytest.mark.parametrize("delay", [0, 1, 30, 240])
    def test_resume_makes_the_absence_active_at_once(self, delay):
        d = PresenceDebouncer()
        d.resume_absence(T0, delay)
        assert d.is_absence_active(False, T0, delay) is True

    def test_present_ends_a_resumed_absence_at_once(self):
        d = PresenceDebouncer()
        d.resume_absence(T0, DELAY)
        assert d.is_absence_active(True, T0 + timedelta(minutes=1), DELAY) is False

    def test_a_later_absence_waits_the_full_delay_again(self):
        d = PresenceDebouncer()
        d.resume_absence(T0, DELAY)
        d.is_absence_active(True, T0 + timedelta(minutes=1), DELAY)
        later = T0 + timedelta(hours=2)
        assert d.is_absence_active(False, later, DELAY) is False
        assert d.is_absence_active(False, later + timedelta(minutes=DELAY), DELAY) is True

    def test_resume_leaves_an_absence_that_is_already_tracked_untouched(self):
        d = PresenceDebouncer()
        d.is_absence_active(False, T0, DELAY)                 # tracked since T0, not yet active
        d.resume_absence(T0 + timedelta(minutes=5), DELAY)
        assert d.is_absence_active(False, T0 + timedelta(minutes=5), DELAY) is False

    def test_resume_without_a_following_absent_reading_does_nothing(self):
        d = PresenceDebouncer()
        d.resume_absence(T0, DELAY)
        assert d.is_absence_active(True, T0, DELAY) is False


# ---------------------------------------------------------------------------
# ARC-02  decision replay of the coordinator's per-cycle chain
# ---------------------------------------------------------------------------


class Replay:
    """A window restored as ABSENCE_CLOSED, replayed cycle by cycle after the restart."""

    def __init__(self, *, resume: bool, delay=DELAY, in_sector=True, window=None, zone=None,
                 startup_grace_cycles=1):
        self.debouncer = PresenceDebouncer()
        self.hold = ComfortMovementHold()
        self.state = ShadingState.ABSENCE_CLOSED
        self.assumed = 70                                   # the absence close (internal)
        self.pending_resume = resume
        self.delay, self.in_sector = delay, in_sector
        self.window, self.zone = window, zone
        self.grace = startup_grace_cycles
        self.log = []

    def cycle(self, minute, *, reading="ABSENT", exposure=40.0):
        now = T0 + timedelta(minutes=minute)
        if self.pending_resume:                              # the coordinator's one-shot
            if reading == "ABSENT":
                self.debouncer.resume_absence(now, self.delay)
                self.pending_resume = False
            elif reading == "PRESENT":
                self.pending_resume = False
        absence = self.debouncer.is_absence_active(reading != "ABSENT", now, self.delay)
        dec = ORCH.evaluate_window(_wdi(absence_active=absence, in_sector=self.in_sector,
                                        exposure=exposure, state=self.state, window=self.window,
                                        zone=self.zone))
        target_ha = to_ha_position(dec.target_position, invert=False)
        confirmed_exit = dec.decided_by == FALLBACK and not self.in_sector
        held = self.hold.should_hold(
            proposed_decided_by=dec.decided_by, proposed_target_ha=target_ha,
            is_strong_escalation=False, is_confirmed_exit=confirmed_exit, now=now,
            actual_position_ha=None)
        pending = self.hold.should_delay_fallback_open(
            proposed_decided_by=dec.decided_by, is_confirmed_exit=confirmed_exit,
            absence_release_started=self.state is ShadingState.ABSENCE_CLOSED,
            presence_confirmed_home=reading == "PRESENT")
        graced, self.grace = self.grace > 0, max(0, self.grace - 1)
        res = CommandFilter().evaluate(
            target_position_internal=dec.target_position, current_position_internal=self.assumed,
            execution_mode=ExecutionMode.AUTOMATIC, is_safety=False, is_manual_override=False,
            is_cover_available=not graced, state_guard_allowed=True,
            execution_capability=ExecutionCapability(), invert_position=False,
            comfort_hold_allowed=not held, fallback_release_allowed=not pending,
            position_confidence_low=False)
        self.state = dec.shading_state
        if res.allowed:
            self.hold.record_dispatch(decided_by=dec.decided_by, target_ha=target_ha, now=now)
            self.assumed = dec.target_position
        out = (dec.decided_by, target_ha, res.allowed)
        self.log.append(out)
        return out

    def opened(self):
        return [m for m in self.log if m[2] and m[1] == 100]


class TestRestartReplay:
    @pytest.mark.parametrize("in_sector", [True, False])
    def test_without_the_resume_the_cover_opens_while_still_away(self, in_sector):
        """The original defect, kept as the counter-proof of the replay."""
        r = Replay(resume=False, in_sector=in_sector)
        for minute in range(6):
            r.cycle(minute)
        assert r.opened(), r.log

    @pytest.mark.parametrize("in_sector", [True, False])
    @pytest.mark.parametrize("delay", [0, 1, 30])
    def test_with_the_resume_the_absence_holds_over_many_cycles(self, in_sector, delay):
        r = Replay(resume=True, in_sector=in_sector, delay=delay)
        for minute in range(40):
            r.cycle(minute)
        assert not r.opened(), r.log
        assert all(m[0] != FALLBACK for m in r.log)

    def test_confirmed_presence_after_the_restart_releases_on_the_first_evaluation(self):
        r = Replay(resume=True)
        for minute in range(3):
            r.cycle(minute)
        decided_by, target_ha, allowed = r.cycle(3, reading="PRESENT")
        assert decided_by == FALLBACK and target_ha == 100 and allowed

    def test_presence_unknown_keeps_the_resume_pending_without_resuming_early(self):
        r = Replay(resume=True)
        r.cycle(0, reading="INDETERMINATE")
        assert r.pending_resume is True
        r.cycle(1, reading="ABSENT")
        assert r.pending_resume is False
        for minute in range(2, 40):
            r.cycle(minute)
        assert not r.opened(), r.log

    def test_a_present_reading_ends_the_pending_resume_for_good(self):
        r = Replay(resume=True)
        r.cycle(0, reading="PRESENT")
        assert r.pending_resume is False
        r.cycle(10, reading="ABSENT")                       # a new departure: the delay applies again
        assert r.debouncer.is_absence_active(False, T0 + timedelta(minutes=10), DELAY) is False

    def test_a_newly_absent_household_without_a_restored_close_is_not_resumed(self):
        r = Replay(resume=False)
        r.state = ShadingState.OPEN
        r.assumed = 0
        for minute in range(DELAY - 1):
            r.cycle(minute)
        assert r.debouncer.is_absence_active(False, T0 + timedelta(minutes=DELAY - 1), DELAY) is False
        assert not [m for m in r.log if m[0] == "AbsenceEvaluator"]


# ---------------------------------------------------------------------------
# ARC-03 / ARC-04  the resumed absence is an ordinary active absence
# ---------------------------------------------------------------------------


def _sig(dec):
    return (dec.decided_by, dec.shading_state, dec.target_position)


class TestPrioritiesAndPositions:
    def test_the_resumed_absence_decides_like_an_ordinary_active_absence(self):
        d = ORCH.evaluate_window(_wdi(absence_active=True))
        assert d.decided_by != FALLBACK and d.shading_state is ShadingState.ABSENCE_CLOSED

    @pytest.mark.parametrize("name,kwargs", [
        ("wind safety", {"wind": 30.0}),
        ("night", {"lifecycle": LifecycleState.NIGHT}),
        ("night with an open window contact",
         {"lifecycle": LifecycleState.NIGHT, "contact": ContactStatus.OPEN, "block_on_open": True}),
    ])
    def test_higher_priorities_decide_the_same_with_and_without_active_absence(self, name, kwargs):
        with_absence = ORCH.evaluate_window(_wdi(absence_active=True, **kwargs))
        without = ORCH.evaluate_window(_wdi(absence_active=False, **kwargs))
        assert _sig(with_absence) == _sig(without), name
        assert with_absence.shading_state is not ShadingState.ABSENCE_CLOSED

    def test_manual_override_decides_the_same_with_and_without_active_absence(self):
        override = ManualOverride(
            window_id="w-south", override_position=40, started_at=T0,
            expires_at=T0 + timedelta(hours=2), source="position_delta",
            overridden_state=ShadingState.ABSENCE_CLOSED, overridden_position=70)
        a = ORCH.evaluate_window(_wdi(absence_active=True, override=override))
        b = ORCH.evaluate_window(_wdi(absence_active=False, override=override))
        assert _sig(a) == _sig(b)
        assert a.shading_state is ShadingState.MANUAL_OVERRIDE

    def test_morning_boundary_is_decided_by_the_lifecycle_not_by_the_restart(self):
        morning = ORCH.evaluate_window(_wdi(absence_active=True, lifecycle=LifecycleState.MORNING))
        plain = ORCH.evaluate_window(_wdi(absence_active=True, lifecycle=LifecycleState.DAY))
        assert morning.decided_by != FALLBACK and plain.decided_by != FALLBACK

    @pytest.mark.parametrize("position_ha", [0, 1, 20, 100])
    def test_window_specific_absence_positions_are_respected_including_zero(self, position_ha):
        window = _window(absence_position=position_ha)
        d = ORCH.evaluate_window(_wdi(absence_active=True, window=window))
        assert d.shading_state is ShadingState.ABSENCE_CLOSED
        assert to_ha_position(d.target_position, invert=False) == position_ha

    @pytest.mark.parametrize("in_sector", [True, False])
    def test_the_solar_sector_does_not_change_the_absence_decision(self, in_sector):
        d = ORCH.evaluate_window(_wdi(absence_active=True, in_sector=in_sector))
        assert d.shading_state is ShadingState.ABSENCE_CLOSED

    def test_replay_with_a_zero_percent_window_never_opens(self):
        r = Replay(resume=True, window=_window(absence_position=0))
        for minute in range(30):
            r.cycle(minute)
        assert not r.opened()


# ---------------------------------------------------------------------------
# ARC-05  coordinator wiring (source contract)
# ---------------------------------------------------------------------------

_SRC = (Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
        / "coordinator.py").read_text(encoding="utf-8")


class TestCoordinatorWiring:
    def test_the_flag_is_armed_only_by_a_restored_absence_closed_state(self):
        arm = re.findall(r"_current_states\[_wid\] is ShadingState\.ABSENCE_CLOSED:\s*\n\s*"
                         r"self\._absence_resume_pending = True", _SRC)
        assert len(arm) == 1
        assert _SRC.count("self._absence_resume_pending = True") == 1

    def test_the_resume_happens_before_the_debouncer_is_asked(self):
        resume = _SRC.index("self._presence_debouncer.resume_absence(")
        ask = _SRC.index("self._presence_debouncer.is_absence_active(")
        assert resume < ask

    def test_the_resume_needs_a_definitive_absent_reading(self):
        block = _SRC[_SRC.index("if self._absence_resume_pending:"):_SRC.index("resume_absence(") + 120]
        assert "PresenceReading.ABSENT" in block

    def test_the_flag_is_runtime_only(self):
        assert "_absence_resume_pending" not in _SRC[_SRC.index('"current_states": {'):
                                                      _SRC.index('"current_states": {') + 400]
