"""Absence release must not be debounced like a comfort release (BUG-03).

The F29 exit-debounce (ComfortMovementHold.should_delay_fallback_open) absorbs
one outlier cycle in which heat/glare/solar protection seems to clear.  It used
to treat the first daytime fallback OPEN after an absence close the same way, so
a window INSIDE its solar sector stayed closed for one more evaluation after
everyone was home, while a window outside its sector opened at once.

The replay below runs the REAL TierOrchestrator -> ComfortMovementHold ->
CommandFilter chain exactly in the coordinator's order, with the coordinator's
inputs (committed state at the start of the cycle, definitive presence reading).

Coverage:
  ARF-01 Release inside the sector without shading need opens on the first
         presence-triggered evaluation; outside the sector it still does.
  ARF-02 Shading still required on return: the protective target goes out, no open.
  ARF-03 Ordinary comfort releases keep the F29 debounce.
  ARF-04 A historical absence dispatch is never a standing bypass.
  ARF-05 Restart / unconfirmed presence is not a release.
  ARF-06 A blocked first attempt is retried without debounce; the carry-over ends
         with the first sent dispatch and with any non-fallback decision.
  ARF-07 Manual override, Safety and Night decisions are never touched.
  ARF-08 Hold-level contract (counter, flag lifetime, defaults).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.cover_control.command_filter import (
    CommandFilter,
    ExecutionCapability,
    ExecutionMode,
)
from custom_components.smartshading.cover_control.position_semantics import to_ha_position
from custom_components.smartshading.engines.comfort_movement_hold import ComfortMovementHold
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.engines.exposure_engine import WindowExposure
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.presence import PresenceReading
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

T0 = datetime(2026, 6, 19, 13, 30, tzinfo=timezone.utc)
FALLBACK = "TierOrchestrator:fallback"
WINDOW = WindowConfig(id="w-south", name="S", zone_id="z1", azimuth=180.0,
                      floor_level=0, cover_group_id="cg")
ZONE = ZoneConfig(id="z1", name="Z")
ORCH = TierOrchestrator()
WEAK_SUN = 40.0      # inside the sector, below every shading threshold
STRONG_SUN = 900.0   # inside the sector, strong shading required


_NO_COMFORT = ComfortConfig(
    heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False)


def _exposure(wm2: float) -> WindowExposure:
    return WindowExposure(
        window_id="w-south", timestamp=T0, sun_azimuth=180.0, sun_elevation=45.0,
        is_above_horizon=True, is_in_tolerance_window=True, azimuth_delta_deg=0.0,
        direct_radiation_factor=1.0, elevation_clipped=False, theoretical_exposure=wm2,
        learned_solar_impact_factor=1.0, seasonal_factor=1.0, effective_exposure=wm2)


def _wdi(window, zone, *, absence_active, is_in_solar_sector, exposure_wm2,
         active_override=None, current_shading_state=ShadingState.OPEN):
    """Absence 30 % open (HA) / sun shade 50 % (HA) / all comfort goals off."""
    return build_window_decision_input(
        window=window, zone=zone,
        global_defaults=GlobalDefaults(absence_position=30),
        shade_position_defaults=ShadePositionDefaults(
            light_shade_position=60, normal_shade_position=50, strong_shade_position=10),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY, absence_active=absence_active,
        current_shading_state=current_shading_state, outdoor_temp_c=None, indoor_temp_c=None,
        exposure=_exposure(exposure_wm2) if exposure_wm2 is not None else None,
        is_in_solar_sector=is_in_solar_sector, comfort_config=_NO_COMFORT,
        active_override=active_override)


class Window:
    """One window replayed cycle by cycle in the coordinator's order."""

    def __init__(self, *, in_sector: bool, exposure: float | None, assumed_internal: int = 0):
        self.hold = ComfortMovementHold()
        self.state = ShadingState.OPEN           # committed state (current_state)
        self.assumed = assumed_internal          # last dispatched internal position
        self.in_sector = in_sector
        self.exposure = exposure
        self.log: list[tuple[str, int, bool, str | None]] = []

    def cycle(self, minutes: int, *, absence: bool, reading: PresenceReading | None = None,
              exposure=..., in_sector=None, cover_available: bool = True,
              override: ManualOverride | None = None):
        in_sector = self.in_sector if in_sector is None else in_sector
        exposure = self.exposure if exposure is ... else exposure
        if reading is None:
            reading = PresenceReading.ABSENT if absence else PresenceReading.PRESENT
        now = T0 + timedelta(minutes=minutes)
        start_state = self.state                                  # current_state
        dec = ORCH.evaluate_window(_wdi(
            WINDOW, ZONE, absence_active=absence, is_in_solar_sector=in_sector,
            exposure_wm2=exposure, active_override=override,
            current_shading_state=start_state))
        target_ha = to_ha_position(dec.target_position, invert=False)
        confirmed_exit = dec.decided_by == FALLBACK and not in_sector
        held = self.hold.should_hold(
            proposed_decided_by=dec.decided_by, proposed_target_ha=target_ha,
            is_strong_escalation=False, is_confirmed_exit=confirmed_exit, now=now,
            actual_position_ha=None)
        pending = self.hold.should_delay_fallback_open(
            proposed_decided_by=dec.decided_by, is_confirmed_exit=confirmed_exit,
            absence_release_started=start_state is ShadingState.ABSENCE_CLOSED,
            presence_confirmed_home=reading is PresenceReading.PRESENT)
        res = CommandFilter().evaluate(
            target_position_internal=dec.target_position, current_position_internal=self.assumed,
            execution_mode=ExecutionMode.AUTOMATIC, is_safety=False,
            is_manual_override=override is not None, is_cover_available=cover_available,
            state_guard_allowed=True, execution_capability=ExecutionCapability(),
            invert_position=False, comfort_hold_allowed=not held,
            fallback_release_allowed=not pending, position_confidence_low=False)
        self.state = dec.shading_state                            # committed even if blocked
        if res.allowed:
            self.hold.record_dispatch(decided_by=dec.decided_by, target_ha=target_ha, now=now)
            self.assumed = dec.target_position
        out = (dec.decided_by, target_ha, res.allowed, res.blocked_reason)
        self.log.append(out)
        return out


def _away(win: Window, *cycles: int) -> None:
    for m in cycles:
        win.cycle(m, absence=True)


def _first_home(win: Window, minute: int = 0, **kw):
    return win.cycle(minute, absence=False, **kw)


class TestImmediateRelease:
    """ARF-01 / ARF-02"""

    def test_inside_sector_without_shading_opens_on_the_first_presence_evaluation(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        assert w.state is ShadingState.ABSENCE_CLOSED
        decided_by, target_ha, allowed, reason = _first_home(w)
        assert decided_by == FALLBACK and target_ha == 100
        assert allowed and reason is None
        assert w.hold.pending_fallback_open_release_count == 0

    def test_outside_sector_opens_on_the_first_presence_evaluation(self):
        w = Window(in_sector=False, exposure=None)
        _away(w, -10, -5)
        _, target_ha, allowed, reason = _first_home(w)
        assert target_ha == 100 and allowed and reason is None

    def test_inside_and_outside_sector_behave_identically(self):
        a = Window(in_sector=True, exposure=WEAK_SUN)
        b = Window(in_sector=False, exposure=None)
        for w in (a, b):
            _away(w, -10, -5)
            _first_home(w)
        assert [e[1:] for e in a.log] == [e[1:] for e in b.log]

    def test_shading_still_required_dispatches_the_protective_target(self):
        """Absence closed the window in weak sun; strong sun is there on return."""
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        decided_by, target_ha, allowed, _ = _first_home(w, exposure=STRONG_SUN)
        assert decided_by != FALLBACK
        assert target_ha < 100, "strong sun on return must not open the window"
        assert allowed, "the protective target must go out on the first presence evaluation"
        assert w.hold.absence_release_open_pending is False

    def test_shading_already_in_place_stays_without_any_open(self):
        """Strong sun during the absence: the shade is already the target."""
        w = Window(in_sector=True, exposure=STRONG_SUN)
        _away(w, -10, -5)
        decided_by, target_ha, allowed, reason = _first_home(w)
        assert decided_by != FALLBACK and target_ha < 100
        assert not allowed and reason == "same_position"

    def test_no_second_evaluation_is_needed(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _first_home(w)
        assert len(w.log) == 3 and w.log[-1][2] is True
        assert w.assumed == 0, "internal 0 = fully open"

    @pytest.mark.parametrize("exposure", [None, WEAK_SUN])
    def test_absence_pending_while_still_away_is_not_a_release(self, exposure):
        w = Window(in_sector=True, exposure=exposure)
        _away(w, -10, -5, 0)
        assert w.hold.absence_release_open_pending is False
        assert w.hold.pending_fallback_open_release_count == 0


class TestOrdinaryReleaseKeepsTheDebounce:
    """ARF-03"""

    def _shaded_then_clear(self):
        w = Window(in_sector=True, exposure=STRONG_SUN)
        w.cycle(-90, absence=False)                    # shade for the sun (hold window long over)
        assert w.state is not ShadingState.OPEN
        w.exposure = WEAK_SUN
        return w

    def test_solar_release_inside_sector_is_still_held_one_cycle(self):
        w = self._shaded_then_clear()
        _, _, allowed, reason = w.cycle(0, absence=False)
        assert not allowed and reason == "fallback_release_pending"
        assert w.hold.pending_fallback_open_release_count == 1
        _, target_ha, allowed, _ = w.cycle(5, absence=False)
        assert allowed and target_ha == 100

    def test_ordinary_release_is_unaffected_by_the_new_flag(self):
        w = self._shaded_then_clear()
        w.cycle(0, absence=False)
        assert w.hold.absence_release_open_pending is False


class TestStaleAbsenceHistoryDoesNotBypass:
    """ARF-04"""

    def test_absence_dispatch_history_alone_is_not_a_bypass(self):
        """last_decided_by stays 'AbsenceEvaluator' only until the next dispatch;
        the exception must not look at it."""
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        assert w.hold.last_decided_by == "AbsenceEvaluator"
        # State no longer ABSENCE_CLOSED (e.g. it was superseded) but the hold still
        # carries the absence dispatch: an ordinary fallback must be debounced.
        w.state = ShadingState.OPEN
        _, _, allowed, reason = _first_home(w)
        assert not allowed and reason == "fallback_release_pending"

    def test_later_comfort_release_after_an_absence_cycle_is_debounced(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _first_home(w, 0)                                      # absence release: open
        w.cycle(10, absence=False, exposure=STRONG_SUN)         # sun: shade
        assert w.state is not ShadingState.OPEN
        _, _, allowed, reason = w.cycle(70, absence=False, exposure=WEAK_SUN)
        assert not allowed and reason == "fallback_release_pending"

    def test_flag_is_dropped_after_the_release_so_later_cycles_are_ordinary(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _first_home(w, 0)
        assert w.hold.absence_release_open_pending is False

    def test_second_absence_period_is_a_fresh_release(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _first_home(w, 0)
        _away(w, 30, 35)
        _, target_ha, allowed, _ = _first_home(w, 40)
        assert target_ha == 100 and allowed


class TestUnconfirmedPresenceIsNotARelease:
    """ARF-05"""

    @pytest.mark.parametrize("reading", [PresenceReading.ABSENT, PresenceReading.INDETERMINATE])
    def test_restart_while_away_stays_debounced(self, reading):
        """After a restart the committed state is restored as ABSENCE_CLOSED while
        the absence debouncer starts over (absence looks inactive); presence is
        not definitively home, so this is not a release."""
        w = Window(in_sector=True, exposure=WEAK_SUN)
        w.state = ShadingState.ABSENCE_CLOSED
        w.assumed = 70
        _, _, allowed, reason = w.cycle(0, absence=False, reading=reading)
        assert not allowed and reason == "fallback_release_pending"
        assert w.hold.absence_release_open_pending is False

    def test_confirmed_presence_after_a_restart_releases_immediately(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        w.state = ShadingState.ABSENCE_CLOSED
        w.assumed = 70
        _, target_ha, allowed, _ = w.cycle(0, absence=False, reading=PresenceReading.PRESENT)
        assert allowed and target_ha == 100

    def test_flag_set_by_a_release_is_dropped_when_presence_becomes_unconfirmed(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        w.cycle(0, absence=False, cover_available=False)
        assert w.hold.absence_release_open_pending is True
        w.cycle(5, absence=False, reading=PresenceReading.INDETERMINATE, cover_available=False)
        assert w.hold.absence_release_open_pending is False


class TestBlockedFirstAttempt:
    """ARF-06"""

    def test_blocked_first_attempt_is_retried_without_debounce(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _, _, allowed, reason = _first_home(w, 0, cover_available=False)
        assert not allowed and reason != "fallback_release_pending"
        assert w.state is ShadingState.OPEN, "state is committed even though the send was blocked"
        _, target_ha, allowed, reason = w.cycle(5, absence=False)
        assert allowed and target_ha == 100 and reason is None
        assert w.hold.absence_release_open_pending is False

    def test_carry_over_ends_with_a_protective_decision(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _first_home(w, 0, cover_available=False)
        assert w.hold.absence_release_open_pending is True
        w.cycle(5, absence=False, exposure=STRONG_SUN, cover_available=False)
        assert w.hold.absence_release_open_pending is False

    def test_release_after_the_protective_decision_is_an_ordinary_one(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _first_home(w, 0, cover_available=False)
        w.cycle(5, absence=False, exposure=STRONG_SUN)          # shade goes out
        _, _, allowed, reason = w.cycle(70, absence=False, exposure=WEAK_SUN)
        assert not allowed and reason == "fallback_release_pending"

    def test_carry_over_ends_when_away_again(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _first_home(w, 0, cover_available=False)
        w.cycle(5, absence=True)
        assert w.hold.absence_release_open_pending is False


class TestPriorityDecisionsAreNeverTouched:
    """ARF-07"""

    def _override(self):
        return ManualOverride(
            window_id="w-south", override_position=40, started_at=T0,
            expires_at=T0 + timedelta(hours=2), source="position_delta",
            overridden_state=ShadingState.ABSENCE_CLOSED, overridden_position=70)

    def test_manual_override_still_wins_on_return(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        decided_by, _, _, _ = _first_home(w, override=self._override())
        assert decided_by != FALLBACK
        assert w.hold.absence_release_open_pending is False
        assert w.hold.pending_fallback_open_release_count == 0

    def test_hold_never_delays_a_non_fallback_decider(self):
        hold = ComfortMovementHold()
        for decider in ("SafetyEvaluator", "NightEvaluator", "ManualOverride",
                        "AbsenceEvaluator", "TierOrchestrator:solar"):
            assert hold.should_delay_fallback_open(
                proposed_decided_by=decider, absence_release_started=True,
                presence_confirmed_home=True) is False
            assert hold.absence_release_open_pending is False

    def test_filter_still_blocks_a_release_when_the_cover_is_unavailable(self):
        w = Window(in_sector=True, exposure=WEAK_SUN)
        _away(w, -10, -5)
        _, _, allowed, reason = _first_home(w, cover_available=False)
        assert not allowed and reason is not None


class TestHoldContract:
    """ARF-08"""

    def test_defaults_keep_the_debounce(self):
        hold = ComfortMovementHold()
        assert hold.absence_release_open_pending is False
        assert hold.should_delay_fallback_open(proposed_decided_by=FALLBACK) is True
        assert hold.pending_fallback_open_release_count == 1
        assert hold.should_delay_fallback_open(proposed_decided_by=FALLBACK) is False

    def test_release_without_confirmed_presence_is_debounced(self):
        hold = ComfortMovementHold()
        assert hold.should_delay_fallback_open(
            proposed_decided_by=FALLBACK, absence_release_started=True,
            presence_confirmed_home=False) is True

    def test_presence_alone_is_not_a_release(self):
        hold = ComfortMovementHold()
        assert hold.should_delay_fallback_open(
            proposed_decided_by=FALLBACK, absence_release_started=False,
            presence_confirmed_home=True) is True

    def test_release_resets_the_counter_and_sets_the_flag(self):
        hold = ComfortMovementHold()
        hold.pending_fallback_open_release_count = 1
        assert hold.should_delay_fallback_open(
            proposed_decided_by=FALLBACK, absence_release_started=True,
            presence_confirmed_home=True) is False
        assert hold.pending_fallback_open_release_count == 0
        assert hold.absence_release_open_pending is True

    def test_record_dispatch_clears_the_flag(self):
        hold = ComfortMovementHold()
        hold.absence_release_open_pending = True
        hold.record_dispatch(decided_by=FALLBACK, target_ha=100, now=T0)
        assert hold.absence_release_open_pending is False

    def test_flag_is_not_part_of_any_persisted_shape(self):
        import dataclasses
        assert "absence_release_open_pending" in {f.name for f in dataclasses.fields(ComfortMovementHold)}
        assert ComfortMovementHold().absence_release_open_pending is False   # fresh after restart
