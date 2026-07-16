"""Comfort Movement Stability Hold — v1.1.1 field fix.

Real-world report: two west-facing living-room windows dispatched real cover
commands repeatedly every 5-15 minutes:

    15:04:42  SolarEvaluator  normal_shade  target_ha=30   dispatch_sent
    15:16:43  GlareEvaluator  light_shade   target_ha=50   dispatch_sent
    15:21:44  SolarEvaluator  normal_shade  target_ha=30   dispatch_sent
    15:26:45  GlareEvaluator  light_shade   target_ha=50   dispatch_sent
    ...

Root cause: PositionResolver.resolve() picks whichever of
{SolarEvaluator, HeatEvaluator, GlareEvaluator} currently produces the
highest (most-shaded) target_position each cycle. As measured exposure
hovers near both evaluators' entry thresholds, the WINNING evaluator
alternates cycle to cycle — NORMAL_SHADE (prio 50) <-> LIGHT_SHADE (prio 60).
LIGHT_SHADE -> NORMAL_SHADE is an escalation (state_machine/transitions.py
bypasses_guard()) and fires instantly; NORMAL_SHADE -> LIGHT_SHADE is a
de-escalation gated only by the existing 10-minute minimum_state_duration —
far shorter than the ~60 minute stability the user wants. Neither StateGuard
nor the v1.1.1 GlareEvaluator STRONG-exit hysteresis address this: it is a
cross-evaluator alternation between two different (non-STRONG) tiers, not a
GlareEvaluator-internal ratio flap.

This is a narrow, ADDITIONAL, independent hold (engines/comfort_movement_hold.py)
wired into CommandFilter via a new `comfort_hold_allowed` parameter and a new
BLOCKED_COMFORT_POSITION_HOLD reason code. It does not touch StateGuard,
minimum_state_duration, or bypasses_guard() — Safety, Night, Night Contact,
Absence, and Manual Override are structurally exempt because their
decided_by strings are never members of NON_PRIORITY_DECIDERS.

v1.1.2 field-fix follow-up (two loopholes closed after a second field
report of continued frequent movement):
  1. STRONG_SHADE no longer bypasses the hold unconditionally — the
     coordinator now always passes is_strong_escalation=False. A prior bare
     `shading_state is STRONG_SHADE` check let any threshold-boundary
     flicker escalate immediately (a real "30 -> 10" within minutes). The
     ComfortMovementHold class still HONORS is_strong_escalation=True as an
     API-level hook for a future evidence-based margin — it is simply never
     set True by the coordinator today.
  2. "TierOrchestrator:fallback" (daytime OPEN) is now itself a member of
     NON_PRIORITY_DECIDERS, so it is held/blocked like any other
     non-priority transition and no longer resets the hold for whatever
     comfort position it interrupted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.cover_control.command_filter import (
    BLOCKED_COMFORT_POSITION_HOLD,
    BLOCKED_FALLBACK_RELEASE_PENDING,
    BLOCKED_SAME_POSITION,
    CommandFilter,
    ExecutionCapability,
    ExecutionMode,
)
from custom_components.smartshading.engines.comfort_movement_hold import (
    COMFORT_MOVEMENT_MIN_HOLD_MINUTES,
    FALLBACK_OPEN_RELEASE_CYCLES,
    NON_PRIORITY_DECIDERS,
    ComfortMovementHold,
)

_T0 = datetime(2026, 7, 3, 15, 4, 42, tzinfo=timezone.utc)


def _hold() -> ComfortMovementHold:
    return ComfortMovementHold()


# ---------------------------------------------------------------------------
# 0. Constants / sanity
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_hold_is_sixty_minutes(self):
        # Matches the user's explicit target: "about once per hour".
        assert COMFORT_MOVEMENT_MIN_HOLD_MINUTES == 60.0

    def test_non_priority_deciders_are_solar_heat_glare_and_fallback(self):
        # v1.1.2 follow-up: TierOrchestrator:fallback joined the set.
        assert NON_PRIORITY_DECIDERS == frozenset({
            "SolarEvaluator", "HeatEvaluator", "GlareEvaluator",
            "TierOrchestrator:fallback",
        })

    def test_fallback_open_is_now_a_non_priority_decider(self):
        assert "TierOrchestrator:fallback" in NON_PRIORITY_DECIDERS

    def test_safety_night_absence_deciders_are_not_non_priority(self):
        for decider in (
            "StormEvaluator", "WindEvaluator", "RainEvaluator",
            "ManualOverrideEvaluator", "NightEvaluator", "AbsenceEvaluator",
            "NightContactVent", "NightContactReturnToNight",
            "NightContactCatchUp", "NightContactBlock",
        ):
            assert decider not in NON_PRIORITY_DECIDERS

    def test_fallback_open_release_requires_two_consecutive_cycles(self):
        # F29 field fix: a single free cycle is a possible threshold-hovering
        # outlier, not a confirmed release.
        assert FALLBACK_OPEN_RELEASE_CYCLES == 2


# ---------------------------------------------------------------------------
# 1. Scenario 1 — Solar 30 -> 5 min later Glare 50 -> blocked
# ---------------------------------------------------------------------------

class TestSolarToGlareBlocked:
    def test_solar_then_glare_five_minutes_later_is_held(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True


# ---------------------------------------------------------------------------
# 2. Scenario 2 — Glare 50 -> 5 min later Solar 30 -> blocked (no escalation)
# ---------------------------------------------------------------------------

class TestGlareToSolarBlockedWithoutEscalation:
    def test_glare_then_solar_five_minutes_later_is_held(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="SolarEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True

    def test_is_strong_escalation_hook_still_bypasses_at_the_class_level(self):
        # v1.1.2: the coordinator never passes is_strong_escalation=True
        # today (see TestStrongShadeIsNoLongerAnUnconditionalBypass below) —
        # this only verifies the ComfortMovementHold API-level hook itself
        # still works, kept for a future evidence-based margin.
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="SolarEvaluator",
            proposed_target_ha=10,
            is_strong_escalation=True,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is False


# ---------------------------------------------------------------------------
# 3. Scenario 3 — after 60 minutes, a new comfort dispatch is allowed
# ---------------------------------------------------------------------------

class TestHoldExpiresAfterSixtyMinutes:
    def test_still_held_just_before_sixty_minutes(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=59, seconds=59),
        )
        assert held is True

    def test_allowed_at_exactly_sixty_minutes(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=60),
        )
        assert held is False

    def test_allowed_well_after_sixty_minutes(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(hours=2),
        )
        assert held is False


# ---------------------------------------------------------------------------
# 4. Safety bypasses the comfort hold
# ---------------------------------------------------------------------------

class TestSafetyBypassesHold:
    def test_safety_decided_by_never_held(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        for safety_decider in ("StormEvaluator", "WindEvaluator", "RainEvaluator"):
            held = h.should_hold(
                proposed_decided_by=safety_decider,
                proposed_target_ha=90,
                is_strong_escalation=False,
                now=_T0 + timedelta(minutes=1),
            )
            assert held is False

    def test_manual_override_and_night_deciders_never_held(self):
        # Manual Override / Night / Morning (NightEvaluator) must remain
        # fully exempt, same as Safety.
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        for decider in ("ManualOverrideEvaluator", "NightEvaluator"):
            held = h.should_hold(
                proposed_decided_by=decider,
                proposed_target_ha=100,
                is_strong_escalation=False,
                now=_T0 + timedelta(minutes=1),
            )
            assert held is False

    def test_command_filter_safety_bypasses_comfort_position_hold(self):
        # End-to-end at the CommandFilter level: is_safety=True must dispatch
        # even when the coordinator computed comfort_hold_allowed=False.
        result = CommandFilter().evaluate(
            target_position_internal=75,
            current_position_internal=0,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=True,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(),
            comfort_hold_allowed=False,
        )
        assert result.allowed is True


# ---------------------------------------------------------------------------
# 5. Night Contact Option B bypasses the comfort hold
# ---------------------------------------------------------------------------

class TestNightContactBypassesHold:
    def test_night_contact_vent_never_held(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="NightContactVent",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=1),
        )
        assert held is False

    def test_night_contact_return_to_night_never_held(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="NightContactReturnToNight",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=1),
        )
        assert held is False


# ---------------------------------------------------------------------------
# 6. Absence activation/release bypasses the comfort hold
# ---------------------------------------------------------------------------

class TestAbsenceBypassesHold:
    def test_absence_evaluator_never_held(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="AbsenceEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=1),
        )
        assert held is False

    def test_comfort_resumes_immediately_after_absence_release(self):
        # A comfort decision arriving right after an Absence dispatch is a
        # fresh entry (previous dispatch was NOT a comfort decider) — must
        # not be held even though very little time has passed.
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        h.record_dispatch(
            decided_by="AbsenceEvaluator", target_ha=30, now=_T0 + timedelta(minutes=2),
        )
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=3),
        )
        assert held is False


# ---------------------------------------------------------------------------
# 7. First entry into shading (fallback/open -> comfort) is always allowed
# ---------------------------------------------------------------------------

class TestFirstEntryAfterGenuineResetIsAllowed:
    """A true "first entry" (nothing tracked yet, or the last recorded event
    was a genuinely prioritized reset — Safety/Night/Absence/Manual
    Override) is still never held. v1.1.2 follow-up: a RECORDED fallback/
    open dispatch is NOT such a reset anymore — see
    TestFallbackOpenIsNowHeldAndNoLongerResetsTheHold above; this class only
    covers the cases that remain a genuine fresh start."""

    def test_never_dispatched_before_is_allowed(self):
        h = _hold()  # no prior dispatch at all — e.g. right after a restart
        held = h.should_hold(
            proposed_decided_by="SolarEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=_T0,
        )
        assert held is False

    def test_comfort_after_a_genuine_safety_reset_is_allowed(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        h.record_dispatch(
            decided_by="StormEvaluator", target_ha=100, now=_T0 + timedelta(minutes=2),
        )
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=3),
        )
        assert held is False


# ---------------------------------------------------------------------------
# 8. Same-position remains a no-op and does not arm a new hold
# ---------------------------------------------------------------------------

class TestSamePositionNoOp:
    def test_identical_target_never_held(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="SolarEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=1),
        )
        assert held is False

    def test_command_filter_same_position_wins_over_comfort_hold_reason(self):
        # Blocking check ordering: same_position (check 5) must fire before
        # comfort_position_hold (check 6), so the reason code stays accurate
        # for a genuine no-op even if comfort_hold_allowed happens to be False.
        result = CommandFilter().evaluate(
            target_position_internal=75,
            current_position_internal=75,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(),
            comfort_hold_allowed=False,
        )
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_SAME_POSITION


# ---------------------------------------------------------------------------
# 9. Two harmonized west windows behave equally stable (independent holds)
# ---------------------------------------------------------------------------

class TestTwoHarmonizedWindowsEquallyStable:
    def test_both_windows_hold_identically_for_the_same_sequence(self):
        # Mirrors two ShadingGroup-harmonized west windows receiving the same
        # sequence of comfort decisions — each window's hold is independent
        # (coordinator keys _comfort_movement_holds by window_id) but must
        # produce IDENTICAL stability behavior for identical inputs.
        window_a = _hold()
        window_b = _hold()

        for h in (window_a, window_b):
            h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)

        for h in (window_a, window_b):
            held = h.should_hold(
                proposed_decided_by="GlareEvaluator",
                proposed_target_ha=50,
                is_strong_escalation=False,
                now=_T0 + timedelta(minutes=12),
            )
            assert held is True

        # Neither window's cover moves — the held decision does not record.
        assert window_a.last_target_ha == window_b.last_target_ha == 30

        # After the hold window elapses, both allow the new comfort dispatch.
        for h in (window_a, window_b):
            held = h.should_hold(
                proposed_decided_by="GlareEvaluator",
                proposed_target_ha=50,
                is_strong_escalation=False,
                now=_T0 + timedelta(minutes=61),
            )
            assert held is False


# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

class TestDiagnosticsHelpers:
    def test_age_minutes_none_when_never_dispatched(self):
        assert _hold().age_minutes(_T0) is None

    def test_age_minutes_reports_elapsed_time(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        assert h.age_minutes(_T0 + timedelta(minutes=12)) == pytest.approx(12.0)

    def test_hold_remaining_minutes_none_when_never_dispatched(self):
        assert _hold().hold_remaining_minutes(_T0) is None

    def test_hold_remaining_minutes_counts_down(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        remaining = h.hold_remaining_minutes(_T0 + timedelta(minutes=12))
        assert remaining == pytest.approx(48.0)

    def test_hold_remaining_minutes_none_after_expiry(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        assert h.hold_remaining_minutes(_T0 + timedelta(minutes=90)) is None


# ===========================================================================
# v1.1.2 field analysis: does the 60-minute hold really cover 30 -> 10 -> 50
# style sequences? These tests DOCUMENT current behavior (both what already
# works correctly and two confirmed loopholes) — no production logic in
# comfort_movement_hold.py or coordinator.py was changed by this analysis.
# See the v1.1.2 field-fix report for the full writeup and proposed (not yet
# applied) tightening.
# ===========================================================================

class TestWithinEvaluatorTransitionsAreHeldTooCurrently:
    """GlareEvaluator uses the SAME decided_by string for LIGHT/NORMAL/STRONG
    (glare_evaluator.py:156) — the hold is evaluator-agnostic, keyed only on
    decided_by + target_ha, so within-evaluator intensity changes ARE subject
    to it exactly like cross-evaluator changes, confirming analysis Q2/Q3."""

    def test_glare_light_to_normal_within_five_minutes_is_held(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True


class TestStrongShadeIsNoLongerAnUnconditionalBypass:
    """v1.1.2 FIX (was CONFIRMED GAP, analysis Q4/Q5): the coordinator now
    always passes is_strong_escalation=False, so a NORMAL_SHADE or
    LIGHT_SHADE -> STRONG_SHADE proposal is held exactly like any other
    non-priority movement. No robust numeric escalation margin is available
    at the coordinator call site without duplicating each evaluator's own
    threshold/ratio logic, so — per explicit product decision — a
    late-but-stable strong escalation is preferred over frequent, unstable
    movement. The RETURN direction (STRONG_SHADE -> NORMAL/LIGHT) was always
    correctly held (Q6/Q7, unchanged) since is_strong_escalation only ever
    reflected the PROPOSED state, never the departure state.
    """

    def test_normal_to_strong_within_five_minutes_is_now_held(self):
        # The exact "30 -> 10 within minutes" gap from the v1.1.2 field
        # report — now held, matching the coordinator's fixed wiring
        # (is_strong_escalation=False, always).
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=10,
            is_strong_escalation=False,  # coordinator always passes False now
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True

    def test_light_to_strong_within_five_minutes_is_now_held(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=10,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True

    def test_repeated_strong_at_the_same_target_is_a_natural_no_op(self):
        # A second STRONG proposal at the SAME already-dispatched target is
        # not a new "held" concern at the hold-class level — CommandFilter's
        # own same_position check (evaluated before comfort_position_hold)
        # prevents a real repeat dispatch either way.
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=10, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=10,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=1),
        )
        assert held is False  # not "held" — but same_position blocks the no-op


class TestStrongShadeReturnIsCorrectlyHeld:
    """The de-escalation direction (leaving STRONG_SHADE) is NOT exempt —
    is_strong_escalation only reflects the PROPOSED state, so 10 -> 50 and
    10 -> 30 are held exactly like any other comfort-to-comfort switch.
    Confirms analysis Q6/Q7: no gap in this direction."""

    def test_strong_to_light_within_five_minutes_is_held(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=10, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True

    def test_strong_to_normal_within_five_minutes_is_held(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=10, now=_T0)
        held = h.should_hold(
            proposed_decided_by="SolarEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True


class TestFallbackOpenIsNowHeldAndNoLongerResetsTheHold:
    """v1.1.2 FIX (was CONFIRMED GAP, analysis Q8/Q9): "TierOrchestrator:
    fallback" joined NON_PRIORITY_DECIDERS, so (a) a fallback/open proposal
    shortly after a real comfort dispatch IS now held, and (b) — because a
    HELD proposal never dispatches and therefore never calls
    record_dispatch() — a brief, noise-driven open interlude can no longer
    reset the hold for whatever comfort position it interrupted.
    """

    def test_comfort_then_fallback_open_is_now_held(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="TierOrchestrator:fallback",
            proposed_target_ha=100,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=2),
        )
        assert held is True  # <- the reported gap, now closed

    def test_held_fallback_open_never_records_so_it_cannot_reset_the_hold(self):
        # A HELD proposal is blocked by CommandFilter before any service call
        # — the coordinator only calls record_dispatch() after a confirmed
        # SENT dispatch, so a held fallback/open never overwrites
        # last_decided_by/last_target_ha. The comfort position from _T0
        # keeps counting down toward the full 60-minute hold.
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="TierOrchestrator:fallback",
            proposed_target_ha=100,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=2),
        )
        assert held is True
        # Simulating "no dispatch happened" (as CommandFilter would enforce):
        # no record_dispatch() call follows. The tracked state is unchanged.
        assert h.last_decided_by == "SolarEvaluator"
        assert h.last_target_ha == 30

        # A comfort proposal shortly after is STILL held — the interlude did
        # not reset anything.
        held_again = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=3),
        )
        assert held_again is True

    def test_fallback_open_outside_the_hold_window_is_allowed(self):
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)
        held = h.should_hold(
            proposed_decided_by="TierOrchestrator:fallback",
            proposed_target_ha=100,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=61),
        )
        assert held is False

    def test_fallback_open_same_position_remains_a_natural_no_op(self):
        # If the cover is already fully open (target_ha unchanged), this is
        # not a "held" concern at the hold-class level — CommandFilter's own
        # same_position check handles the no-op either way.
        h = _hold()
        h.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=_T0)
        held = h.should_hold(
            proposed_decided_by="TierOrchestrator:fallback",
            proposed_target_ha=100,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=1),
        )
        assert held is False


class TestConfirmedExitCarveOutForFallbackOpen:
    """v1.1.2 second follow-up: blocking every Fallback/Open unconditionally
    (previous class) risks artificially shading a window whose sun/glare
    exposure has genuinely and robustly ended. The coordinator passes
    `is_confirmed_exit=True` only for a Fallback/Open proposal on a window
    confirmed OUT of its effective solar sector this cycle — a geometric,
    hysteresis-free fact, never for Solar/Heat/Glare comfort proposals or
    STRONG_SHADE (see engines/comfort_movement_hold.py module docstring)."""

    def test_noisy_fallback_open_without_confirmed_exit_is_still_held(self):
        # Default is_confirmed_exit=False behaves exactly like the previous
        # (unconditional) fallback/open hold — the noisy case.
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="TierOrchestrator:fallback",
            proposed_target_ha=100,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True

    def test_confirmed_exit_fallback_open_is_allowed_within_the_hold_window(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="TierOrchestrator:fallback",
            proposed_target_ha=100,
            is_strong_escalation=False,
            is_confirmed_exit=True,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is False

    def test_confirmed_exit_open_then_prompt_lower_comfort_return_is_now_bypassed(self):
        # F27 accepted trade-off: the protective-shade-after-
        # fallback-open bypass (see engines/comfort_movement_hold.py) cannot
        # distinguish an ordinary fallback open from a confirmed-exit one —
        # both share decided_by="TierOrchestrator:fallback", and the fallback
        # always targets fully open (100), so any subsequent Solar/Heat/Glare
        # proposal is necessarily lower and now bypasses here too, same as
        # after any other fallback open. This intentionally narrows the
        # previous "any comfort return after a confirmed exit is held"
        # guarantee — accepted because the routine F27 morning scenario is
        # far more common than an immediate lower comfort return arriving
        # within a minute of a geometrically confirmed sector exit.
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="TierOrchestrator:fallback",
            proposed_target_ha=100,
            is_strong_escalation=False,
            is_confirmed_exit=True,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is False
        h.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=_T0 + timedelta(minutes=5))

        held_return = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=6),
        )
        assert held_return is False

    def test_confirmed_exit_never_applies_to_strong_shade_at_the_call_site(self):
        # The coordinator only ever computes is_confirmed_exit for a
        # "TierOrchestrator:fallback" proposal — Solar/Heat/Glare comfort
        # proposals (including any STRONG_SHADE escalation) always pass
        # is_confirmed_exit=False, so they remain held exactly as in
        # TestStrongShadeIsNoLongerAnUnconditionalBypass. Documented here for
        # discoverability; the coordinator wiring itself is a one-line
        # conditional, not a separate unit under test.
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=10,
            is_strong_escalation=False,
            is_confirmed_exit=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True

    def test_safety_manual_night_absence_remain_bypasses_regardless_of_confirmed_exit(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)
        for decider in (
            "StormSafeEvaluator", "ManualOverrideEvaluator", "NightEvaluator",
            "AbsenceEvaluator",
        ):
            held = h.should_hold(
                proposed_decided_by=decider,
                proposed_target_ha=0,
                is_strong_escalation=False,
                is_confirmed_exit=False,
                now=_T0 + timedelta(minutes=5),
            )
            assert held is False, decider


class TestProtectiveShadeAfterFallbackOpenBypassesHold:
    """F27 field fix: a window opened via the daytime OPEN fallback
    (nothing fired that cycle) must not have a subsequent, correctly detected
    Glare/Heat/Solar protective target held back for up to 60 minutes. The
    carve-out is narrow — see engines/comfort_movement_hold.py module
    docstring — and must not resurrect the removed unconditional STRONG_SHADE
    bypass or introduce a general "more shade always wins" rule."""

    def test_glare_after_fallback_open_is_not_held(self):
        h = _hold()
        h.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=40),
        )
        assert held is False

    def test_heat_after_fallback_open_is_not_held(self):
        h = _hold()
        h.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=_T0)
        held = h.should_hold(
            proposed_decided_by="HeatEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=40),
        )
        assert held is False

    def test_solar_after_fallback_open_is_not_held(self):
        h = _hold()
        h.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=_T0)
        held = h.should_hold(
            proposed_decided_by="SolarEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=40),
        )
        assert held is False

    def test_opening_direction_after_fallback_open_is_still_held(self):
        # The proposed target is HIGHER (more open) than the fallback's own
        # target — this is not a protective move, so the ordinary hold rule
        # still applies.
        h = _hold()
        h.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=50, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=100,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True

    def test_bypass_does_not_apply_between_comfort_evaluators(self):
        # The last dispatch was a genuine comfort tier (SolarEvaluator), not
        # the fallback open — this must remain held exactly like
        # TestStrongShadeIsNoLongerAnUnconditionalBypass.
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=100, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is True

    def test_bypass_does_not_apply_when_target_is_equal(self):
        # Not a protective move (no lower target proposed) — falls through to
        # the ordinary same-position early-return (CommandFilter's job, not
        # counted as held).
        h = _hold()
        h.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=_T0)
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=100,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert held is False


class TestCombinedSequencesAreNowStable:
    """End-to-end sequence checks mirroring the user's exact examples."""

    def test_solar_fallback_open_glare_sequence_produces_only_one_real_move(self):
        # 30 -> fallback/open -> 50 within 60 minutes: only the initial Solar
        # dispatch is real; both later proposals are held, so the window
        # never actually moves a second time.
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)

        fallback_held = h.should_hold(
            proposed_decided_by="TierOrchestrator:fallback",
            proposed_target_ha=100,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert fallback_held is True  # blocked — stays at 30, no record_dispatch call

        glare_held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=10),
        )
        assert glare_held is True  # still blocked — last_target_ha is still 30

    def test_thirty_ten_fifty_sequence_stays_at_thirty_throughout(self):
        # The user's exact "30 -> 10 -> 50" concern: with STRONG_SHADE no
        # longer an unconditional bypass, both later proposals are held —
        # the window stays at 30 for the whole 60-minute window.
        h = _hold()
        h.record_dispatch(decided_by="SolarEvaluator", target_ha=30, now=_T0)

        strong_held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=10,
            is_strong_escalation=False,  # coordinator always passes False now
            now=_T0 + timedelta(minutes=5),
        )
        assert strong_held is True

        light_held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=10),
        )
        assert light_held is True
        # No record_dispatch() calls happened for either held proposal, so
        # the window's real (dispatched) position remains 30 throughout.
        assert h.last_target_ha == 30

    def test_fifty_thirty_ten_sequence_stays_at_fifty_throughout(self):
        h = _hold()
        h.record_dispatch(decided_by="GlareEvaluator", target_ha=50, now=_T0)

        normal_held = h.should_hold(
            proposed_decided_by="SolarEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=5),
        )
        assert normal_held is True

        strong_held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=10,
            is_strong_escalation=False,
            now=_T0 + timedelta(minutes=10),
        )
        assert strong_held is True
        assert h.last_target_ha == 50


class TestRestartLosesHoldState:
    """CONFIRMED, LOW-URGENCY (analysis Q14/Q15) — in-memory only, no
    persistence. A fresh ComfortMovementHold() (as created after an HA
    restart or integration reload) has no memory of the pre-restart
    dispatch, so the first comfort decision after a restart is never held —
    even if a comfort dispatch happened moments before the restart. Reported
    as a recommendation for a future enhancement (persist alongside the
    existing Learning Store), not applied now: restarts are infrequent
    compared to the 60-minute hold window, so real-world impact is limited
    to at most one potentially-early move right after a restart."""

    def test_fresh_hold_after_simulated_restart_never_holds(self):
        h = _hold()  # simulates coordinator re-creation after HA restart
        held = h.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=_T0,
        )
        assert held is False


# ---------------------------------------------------------------------------
# F10 — Sensor Smoothing Decision (2026-07-08): combined-pipeline proof.
#
# All prior tests in this file exercise ComfortMovementHold in isolation.
# This class drives the REAL StateGuard + CommandFilter + ComfortMovementHold
# together across a boundary-straddling solar-exposure oscillation at the
# real 5-minute coordinator cadence, proving the F10 task's own example:
# "Solar-W/m^2-Spike loest nicht direkt dauernd neue Befehle aus, weil
# CommandFilter/Guard greift." A noisy sensor hovering across a
# NORMAL_SHADE(40) <-> STRONG_SHADE(60) decision boundary produces exactly
# ONE real dispatch across 6 cycles (60 minutes), not 6.
# ---------------------------------------------------------------------------

class TestCombinedPipelineAbsorbsBoundaryStraddlingOscillation:
    def test_six_cycle_normal_strong_flap_produces_exactly_one_dispatch(self):
        from custom_components.smartshading.state_machine.guards import (
            StateGuard,
        )
        from custom_components.smartshading.state_machine.states import (
            ShadingState,
        )

        guard = StateGuard()
        hold = ComfortMovementHold()
        cmd_filter = CommandFilter()
        exec_cap = ExecutionCapability()
        window_id = "win_flap"

        # Alternating targets as a noisy sensor straddles the 40/60 boundary,
        # one cycle every 5 minutes (matches the real coordinator cadence).
        targets_ha = [60, 40, 60, 40, 60, 40]
        states = [
            ShadingState.STRONG_SHADE if t == 60 else ShadingState.NORMAL_SHADE
            for t in targets_ha
        ]

        current_position = 0  # cover starts fully open
        dispatched_positions: list[int] = []
        blocked_reasons: list[str | None] = []

        for i, (target, state) in enumerate(zip(targets_ha, states)):
            now = _T0 + timedelta(minutes=5 * i)

            comfort_hold_allowed = not hold.should_hold(
                proposed_decided_by="SolarEvaluator",
                proposed_target_ha=target,
                is_strong_escalation=False,  # coordinator always passes False
                now=now,
            )
            state_guard_allowed = guard.can_send_action(window_id, state, now)

            result = cmd_filter.evaluate(
                target_position_internal=target,
                current_position_internal=current_position,
                execution_mode=ExecutionMode.AUTOMATIC,
                is_safety=False,
                is_manual_override=False,
                is_cover_available=True,
                state_guard_allowed=state_guard_allowed,
                execution_capability=exec_cap,
                comfort_hold_allowed=comfort_hold_allowed,
            )
            blocked_reasons.append(result.blocked_reason)

            if result.allowed:
                dispatched_positions.append(target)
                current_position = target
                guard.record_action_sent(window_id, now)
                hold.record_dispatch(
                    decided_by="SolarEvaluator", target_ha=target, now=now,
                )

        # Exactly one real dispatch across the whole 60-minute flap window.
        assert dispatched_positions == [60]
        assert blocked_reasons[0] is None
        # Every subsequent cycle was blocked — either by the Comfort Hold
        # (target differs from the held 60) or by same-position tolerance
        # once a proposal happens to match the still-held real position.
        for reason in blocked_reasons[1:]:
            assert reason in (BLOCKED_COMFORT_POSITION_HOLD, BLOCKED_SAME_POSITION)

    def test_hold_expiry_after_sixty_minutes_allows_the_next_real_dispatch(self):
        from custom_components.smartshading.state_machine.guards import (
            StateGuard,
        )
        from custom_components.smartshading.state_machine.states import (
            ShadingState,
        )

        guard = StateGuard()
        hold = ComfortMovementHold()
        cmd_filter = CommandFilter()
        exec_cap = ExecutionCapability()
        window_id = "win_flap_expiry"

        # Initial real dispatch to STRONG_SHADE(60).
        t0 = _T0
        hold.record_dispatch(decided_by="SolarEvaluator", target_ha=60, now=t0)
        guard.record_action_sent(window_id, t0)

        # A proposal to flap back to 40, arriving just past the 60-minute
        # hold window, must be allowed through (StateGuard's 3-minute
        # action interval has long since elapsed too).
        t1 = t0 + timedelta(minutes=61)
        comfort_hold_allowed = not hold.should_hold(
            proposed_decided_by="SolarEvaluator",
            proposed_target_ha=40,
            is_strong_escalation=False,
            now=t1,
        )
        state_guard_allowed = guard.can_send_action(
            window_id, ShadingState.NORMAL_SHADE, t1,
        )
        result = cmd_filter.evaluate(
            target_position_internal=40,
            current_position_internal=60,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=state_guard_allowed,
            execution_capability=exec_cap,
            comfort_hold_allowed=comfort_hold_allowed,
        )
        assert result.allowed is True
        assert result.blocked_reason is None

    def test_f27_glare_after_fallback_open_is_not_blocked_by_comfort_hold(self):
        # Mirrors the F27 field report: a fallback open to 100 was the last
        # accepted decision; GlareEvaluator then proposes a protective 50.
        # The real StateGuard + CommandFilter + ComfortMovementHold pipeline
        # must dispatch this, not block it as BLOCKED_COMFORT_POSITION_HOLD.
        from custom_components.smartshading.state_machine.guards import (
            StateGuard,
        )
        from custom_components.smartshading.state_machine.states import (
            ShadingState,
        )

        guard = StateGuard()
        hold = ComfortMovementHold()
        cmd_filter = CommandFilter()
        exec_cap = ExecutionCapability()
        window_id = "win_dining_room"

        t0 = _T0
        hold.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=t0)
        guard.record_action_sent(window_id, t0)

        t1 = t0 + timedelta(minutes=40)  # well within the 60-minute hold window
        comfort_hold_allowed = not hold.should_hold(
            proposed_decided_by="GlareEvaluator",
            proposed_target_ha=50,
            is_strong_escalation=False,
            now=t1,
        )
        state_guard_allowed = guard.can_send_action(
            window_id, ShadingState.LIGHT_SHADE, t1,
        )
        result = cmd_filter.evaluate(
            target_position_internal=50,
            current_position_internal=100,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=state_guard_allowed,
            execution_capability=exec_cap,
            comfort_hold_allowed=comfort_hold_allowed,
        )
        assert result.allowed is True
        assert result.blocked_reason is None


# ---------------------------------------------------------------------------
# F29 field fix — exit-debounce / confirmed release for Fallback/Open.
#
# Real-world report: HeatEvaluator (and, by the same shape, Solar/Glare) has
# no temporal smoothing of its inputs — a single free cycle where nothing in
# Tier 4/5 fired (a threshold-hovering reading, not a genuine, durable
# clearing of protection) let "TierOrchestrator:fallback" dispatch a full
# OPEN, only for the very next cycle to re-trigger HeatEvaluator and shade
# back down: a visible open-then-close flap five minutes apart. This is
# UNRELATED to and does not touch the F27 fix above (F27 concerns a comfort
# re-target AFTER an open has already been dispatched).
# ---------------------------------------------------------------------------

class TestFallbackOpenReleaseDebounce:
    """Unit tests for ComfortMovementHold.should_delay_fallback_open()."""

    def test_single_fallback_cycle_is_delayed(self):
        h = _hold()
        delayed = h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        assert delayed is True
        assert h.pending_fallback_open_release_count == 1

    def test_second_consecutive_fallback_cycle_is_allowed(self):
        h = _hold()
        h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        delayed = h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        assert delayed is False
        assert h.pending_fallback_open_release_count == 2

    def test_third_and_later_consecutive_cycles_remain_allowed(self):
        h = _hold()
        for _ in range(4):
            delayed = h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        assert delayed is False

    def test_counter_resets_on_heat_evaluator(self):
        h = _hold()
        h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        assert h.pending_fallback_open_release_count == 1
        delayed = h.should_delay_fallback_open(proposed_decided_by="HeatEvaluator")
        assert delayed is False
        assert h.pending_fallback_open_release_count == 0
        # A fresh fallback cycle right after a real Heat decision must start
        # the debounce over, not be treated as already-confirmed.
        delayed_again = h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        assert delayed_again is True
        assert h.pending_fallback_open_release_count == 1

    def test_counter_resets_on_glare_evaluator(self):
        h = _hold()
        h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        delayed = h.should_delay_fallback_open(proposed_decided_by="GlareEvaluator")
        assert delayed is False
        assert h.pending_fallback_open_release_count == 0

    def test_counter_resets_on_solar_evaluator(self):
        h = _hold()
        h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        delayed = h.should_delay_fallback_open(proposed_decided_by="SolarEvaluator")
        assert delayed is False
        assert h.pending_fallback_open_release_count == 0

    def test_safety_and_other_priority_deciders_never_delayed_and_reset_counter(self):
        # None of these are "TierOrchestrator:fallback", so the gate never
        # delays them and always resets the counter — this debounce touches
        # only the fallback-open proposal itself.
        for decider in (
            "StormSafeEvaluator", "WindSafeEvaluator", "ManualOverrideEvaluator",
            "NightEvaluator", "AbsenceEvaluator", "NightContactVent",
            "NightContactReturnToNight",
        ):
            h = _hold()
            h.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
            delayed = h.should_delay_fallback_open(proposed_decided_by=decider)
            assert delayed is False, decider
            assert h.pending_fallback_open_release_count == 0, decider

    def test_confirmed_exit_skips_the_debounce_immediately(self):
        # Same hysteresis-free geometric fact used by should_hold()'s
        # is_confirmed_exit carve-out: a confirmed sector exit is trusted on
        # the first cycle, no debounce needed.
        h = _hold()
        delayed = h.should_delay_fallback_open(
            proposed_decided_by="TierOrchestrator:fallback",
            is_confirmed_exit=True,
        )
        assert delayed is False
        assert h.pending_fallback_open_release_count == 0


class TestFallbackOpenReleasePipelineIntegration:
    """End-to-end: real StateGuard + CommandFilter + ComfortMovementHold,
    reproducing the F29 field report (fallback-open outlier -> Heat re-close
    5 minutes later) and confirming the debounce absorbs it without a
    visible dispatch, while a genuine two-cycle release still opens, and the
    F27 protective-shade-after-open bypass keeps working unmodified."""

    @staticmethod
    def _fallback_release_allowed(hold, *, now, is_confirmed_exit=False):
        return not hold.should_delay_fallback_open(
            proposed_decided_by="TierOrchestrator:fallback",
            is_confirmed_exit=is_confirmed_exit,
        )

    def test_single_outlier_cycle_produces_no_visible_dispatch(self):
        from custom_components.smartshading.state_machine.guards import StateGuard
        from custom_components.smartshading.state_machine.states import ShadingState

        guard = StateGuard()
        hold = ComfortMovementHold()
        cmd_filter = CommandFilter()
        exec_cap = ExecutionCapability()
        window_id = "win_f29_single_outlier"

        # Baseline: window is currently shaded via HeatEvaluator at HA 30.
        t0 = _T0
        hold.record_dispatch(decided_by="HeatEvaluator", target_ha=30, now=t0)
        guard.record_action_sent(window_id, t0)

        # 5 minutes later: a single free cycle, fallback proposes OPEN/100.
        t1 = t0 + timedelta(minutes=5)
        fallback_release_allowed = self._fallback_release_allowed(hold, now=t1)
        state_guard_allowed = guard.can_send_action(window_id, ShadingState.OPEN, t1)
        result = cmd_filter.evaluate(
            target_position_internal=0,   # internal OPEN
            current_position_internal=70,  # HA 30 shaded -> internal 70
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=state_guard_allowed,
            execution_capability=exec_cap,
            fallback_release_allowed=fallback_release_allowed,
        )
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_FALLBACK_RELEASE_PENDING

    def test_two_consecutive_free_cycles_allow_the_open_dispatch(self):
        from custom_components.smartshading.state_machine.guards import StateGuard
        from custom_components.smartshading.state_machine.states import ShadingState

        guard = StateGuard()
        hold = ComfortMovementHold()
        cmd_filter = CommandFilter()
        exec_cap = ExecutionCapability()
        window_id = "win_f29_genuine_release"

        t0 = _T0
        hold.record_dispatch(decided_by="HeatEvaluator", target_ha=30, now=t0)
        guard.record_action_sent(window_id, t0)

        # Cycle 1 (t0+5min): free cycle, held back — matches the test above.
        t1 = t0 + timedelta(minutes=5)
        assert self._fallback_release_allowed(hold, now=t1) is False

        # Cycle 2 (t0+10min): second consecutive free cycle, now allowed.
        t2 = t0 + timedelta(minutes=10)
        fallback_release_allowed = self._fallback_release_allowed(hold, now=t2)
        assert fallback_release_allowed is True
        state_guard_allowed = guard.can_send_action(window_id, ShadingState.OPEN, t2)
        result = cmd_filter.evaluate(
            target_position_internal=0,
            current_position_internal=70,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=state_guard_allowed,
            execution_capability=exec_cap,
            fallback_release_allowed=fallback_release_allowed,
        )
        assert result.allowed is True
        assert result.blocked_reason is None
        guard.record_action_sent(window_id, t2)
        hold.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=t2)

    def test_heat_between_two_fallback_cycles_resets_the_debounce(self):
        # Cycle 1: free (held). Cycle 2: HeatEvaluator wins again (resets
        # counter). Cycle 3: free again — must be held, NOT immediately
        # allowed, exactly matching the F29 field report shape.
        hold = ComfortMovementHold()
        hold.record_dispatch(decided_by="HeatEvaluator", target_ha=30, now=_T0)

        cycle1_delayed = hold.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        assert cycle1_delayed is True

        cycle2_delayed = hold.should_delay_fallback_open(proposed_decided_by="HeatEvaluator")
        assert cycle2_delayed is False  # not a fallback proposal, gate is a no-op
        assert hold.pending_fallback_open_release_count == 0

        cycle3_delayed = hold.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        assert cycle3_delayed is True

    def test_f27_protective_shade_after_genuine_release_still_bypasses_hold(self):
        # After a genuine two-cycle fallback release dispatches OPEN, a
        # subsequent real HeatEvaluator protective re-target must still be
        # let through immediately by should_hold()'s F27 bypass — the F29
        # debounce only gates the fallback-open proposal itself and must not
        # interfere with should_hold()'s own, separate decision.
        hold = ComfortMovementHold()
        t0 = _T0
        hold.record_dispatch(decided_by="HeatEvaluator", target_ha=30, now=t0)

        hold.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        t1 = t0 + timedelta(minutes=5)
        released = not hold.should_delay_fallback_open(proposed_decided_by="TierOrchestrator:fallback")
        assert released is True
        hold.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=t1)

        t2 = t1 + timedelta(minutes=5)
        held = hold.should_hold(
            proposed_decided_by="HeatEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=t2,
        )
        assert held is False  # F27 bypass: protective move after fallback-open is never held

    def test_f29_field_report_reproduction_no_visible_open_close_flap(self):
        # Full 3-cycle reproduction of the real field report: window shaded
        # at HA 30 via HeatEvaluator, one free/outlier cycle where nothing
        # fired (fallback proposes OPEN), then HeatEvaluator re-activates
        # the very next cycle. Drives the real StateGuard + CommandFilter +
        # ComfortMovementHold pipeline and tracks the assumed cover position
        # across all three cycles: it must never move, i.e. no visible
        # open-then-close flap.
        from custom_components.smartshading.state_machine.guards import StateGuard
        from custom_components.smartshading.state_machine.states import ShadingState

        guard = StateGuard()
        hold = ComfortMovementHold()
        cmd_filter = CommandFilter()
        exec_cap = ExecutionCapability()
        window_id = "win_f29_field_report"

        current_position = 70  # internal convention: HA 30 shaded -> internal 70
        dispatched_positions: list[int] = []

        # Cycle 0 (t0): baseline, HeatEvaluator holds HA 30 (internal 70).
        t0 = _T0
        hold.record_dispatch(decided_by="HeatEvaluator", target_ha=30, now=t0)
        guard.record_action_sent(window_id, t0)

        # Cycle 1 (t0+5min): single free/outlier cycle — fallback proposes
        # OPEN (internal 0).
        t1 = t0 + timedelta(minutes=5)
        fallback_release_allowed_1 = not hold.should_delay_fallback_open(
            proposed_decided_by="TierOrchestrator:fallback",
        )
        result1 = cmd_filter.evaluate(
            target_position_internal=0,
            current_position_internal=current_position,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=guard.can_send_action(window_id, ShadingState.OPEN, t1),
            execution_capability=exec_cap,
            fallback_release_allowed=fallback_release_allowed_1,
        )
        assert result1.allowed is False
        assert result1.blocked_reason == BLOCKED_FALLBACK_RELEASE_PENDING
        if result1.allowed:
            dispatched_positions.append(0)
            current_position = 0
            guard.record_action_sent(window_id, t1)
            hold.record_dispatch(decided_by="TierOrchestrator:fallback", target_ha=100, now=t1)

        # Cycle 2 (t0+10min): HeatEvaluator re-activates, proposes HA 30
        # (internal 70) again — same position, resets the fallback counter.
        t2 = t0 + timedelta(minutes=10)
        fallback_release_allowed_2 = not hold.should_delay_fallback_open(
            proposed_decided_by="HeatEvaluator",
        )
        comfort_hold_allowed_2 = not hold.should_hold(
            proposed_decided_by="HeatEvaluator",
            proposed_target_ha=30,
            is_strong_escalation=False,
            now=t2,
        )
        result2 = cmd_filter.evaluate(
            target_position_internal=70,
            current_position_internal=current_position,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=guard.can_send_action(window_id, ShadingState.NORMAL_SHADE, t2),
            execution_capability=exec_cap,
            comfort_hold_allowed=comfort_hold_allowed_2,
            fallback_release_allowed=fallback_release_allowed_2,
        )
        # Position never moved, so this is a natural same-position no-op —
        # either way, nothing dispatches and the window never visibly moved.
        if result2.allowed:
            dispatched_positions.append(70)
            current_position = 70
            guard.record_action_sent(window_id, t2)
            hold.record_dispatch(decided_by="HeatEvaluator", target_ha=30, now=t2)

        # No visible open-then-close flap: zero real dispatches across the
        # whole 3-cycle sequence, and the assumed position never left HA 30.
        assert dispatched_positions == []
        assert current_position == 70
