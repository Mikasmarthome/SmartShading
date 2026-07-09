"""Tests for v1.0 — Manual override regression fix.

Covers:
- AssumedPositionState.last_commanded_position: set only by update() / on_reference_travel(),
  never by observe().
- For reliable covers: observe() after update() preserves last_commanded_position.
- Override detection after SmartShading shade: user open → detected; own command → not detected.
- Fallback to assumed_internal when last_commanded_position is None (pre-dispatch).
- Unavailable cover → no false override.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.cover_control.assumed_state_manager import (
    AssumedStateManager,
    AssumedPositionState,
    AssumedStateManagerConfig,
)
from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.state_machine.states import ShadingState


UTC = timezone.utc


def _now() -> datetime:
    return datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC)


def _manager() -> AssumedStateManager:
    return AssumedStateManager(AssumedStateManagerConfig())


# ---------------------------------------------------------------------------
# 1. AssumedPositionState.last_commanded_position field
# ---------------------------------------------------------------------------

class TestLastCommandedPositionField:

    def test_default_is_none(self):
        state = AssumedPositionState(
            cover_id="c1",
            assumed_position=0,
            assumed_tilt=None,
            last_commanded_at=None,
            last_known_good_at=_now(),
            confidence=1.0,
            position_uncertainty_pct=0.0,
            is_drift_suspected=False,
            interrupted_travel=False,
        )
        assert state.last_commanded_position is None

    def test_can_be_set(self):
        state = AssumedPositionState(
            cover_id="c1",
            assumed_position=80,
            assumed_tilt=None,
            last_commanded_at=_now(),
            last_known_good_at=_now(),
            confidence=1.0,
            position_uncertainty_pct=0.0,
            is_drift_suspected=False,
            interrupted_travel=False,
            last_commanded_position=80,
        )
        assert state.last_commanded_position == 80


# ---------------------------------------------------------------------------
# 2. update() sets last_commanded_position
# ---------------------------------------------------------------------------

class TestUpdateSetsLastCommandedPosition:

    def test_reliable_cover_update_sets_field(self):
        mgr = _manager()
        mgr.update("c1", 80, _now(), has_reliable_position_feedback=True)
        st = mgr.get_state("c1", _now())
        assert st is not None
        assert st.last_commanded_position == 80

    def test_unreliable_cover_update_sets_field(self):
        mgr = _manager()
        mgr.update("c1", 60, _now(), has_reliable_position_feedback=False)
        st = mgr.get_state("c1", _now())
        assert st is not None
        assert st.last_commanded_position == 60

    def test_update_overrides_previous_last_commanded(self):
        mgr = _manager()
        mgr.update("c1", 80, _now(), has_reliable_position_feedback=True)
        mgr.update("c1", 50, _now(), has_reliable_position_feedback=True)
        st = mgr.get_state("c1", _now())
        assert st.last_commanded_position == 50

    def test_on_reference_travel_sets_field(self):
        mgr = _manager()
        mgr.on_reference_travel("c1", "min", 0, _now())
        st = mgr.get_state("c1", _now())
        assert st.last_commanded_position == 0


# ---------------------------------------------------------------------------
# 3. observe() does NOT overwrite last_commanded_position
# ---------------------------------------------------------------------------

class TestObservePreservesLastCommandedPosition:

    def test_reliable_cover_observe_after_update_preserves(self):
        """The real-world bug: reliable cover, user opens after SmartShading shades.
        observe() must NOT overwrite last_commanded_position=80 with user's actual=0."""
        mgr = _manager()
        # SmartShading commands to 80 (shade)
        mgr.update("c1", 80, _now(), has_reliable_position_feedback=True)
        assert mgr.get_state("c1", _now()).last_commanded_position == 80

        # User opens cover → HA reports 0 (internal). observe() runs.
        mgr.observe("c1", 0, _now(), has_reliable_position_feedback=True)
        st = mgr.get_state("c1", _now())

        # assumed_position follows actual (observe updates it) → 0
        assert st.assumed_position == 0
        # last_commanded_position must still be 80 (NOT overwritten by observe)
        assert st.last_commanded_position == 80

    def test_reliable_cover_observe_without_prior_update_leaves_none(self):
        """Before any dispatch, last_commanded_position stays None after observe."""
        mgr = _manager()
        mgr.observe("c1", 0, _now(), has_reliable_position_feedback=True)
        st = mgr.get_state("c1", _now())
        assert st.assumed_position == 0
        assert st.last_commanded_position is None

    def test_unreliable_cover_observe_preserves(self):
        mgr = _manager()
        mgr.update("c1", 80, _now(), has_reliable_position_feedback=False)
        mgr.observe("c1", 0, _now(), has_reliable_position_feedback=False)
        st = mgr.get_state("c1", _now())
        assert st.last_commanded_position == 80

    def test_multiple_observes_never_overwrite(self):
        mgr = _manager()
        mgr.update("c1", 80, _now(), has_reliable_position_feedback=True)
        for pos in (70, 50, 30, 0):
            mgr.observe("c1", pos, _now(), has_reliable_position_feedback=True)
        assert mgr.get_state("c1", _now()).last_commanded_position == 80


# ---------------------------------------------------------------------------
# 4. OverrideDetector with corrected own-command guard logic
# ---------------------------------------------------------------------------

def _advance_warmup(det: OverrideDetector, window_id: str, n: int = 3):
    """Advance override detector past warmup without triggering an override."""
    from datetime import timedelta
    from custom_components.smartshading.engines.override_detector import _WARMUP_CYCLES_REQUIRED
    now = _now()
    for i in range(_WARMUP_CYCLES_REQUIRED):
        det.tick(
            window_id=window_id,
            observed_position=0,
            smartshading_target=0,
            smartshading_assumed=0,
            prev_state=ShadingState.OPEN,
            tolerance=5,
            duration_min=60,
            now=now,
        )


class TestOverrideDetectorWithLastCommanded:

    def test_user_opens_after_shade_detected(self):
        """Core regression: SmartShading shades to 80, user opens to 0 → override detected."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        # After warmup: last_commanded=80, observed=0 (user opened), target=80
        det.tick(
            window_id="w1",
            observed_position=0,
            smartshading_target=80,
            smartshading_assumed=80,  # last_commanded=80 (what the fix provides)
            prev_state=ShadingState.NORMAL_SHADE,
            tolerance=5,
            duration_min=60,
            now=_now(),
        )
        assert det.get("w1", _now()) is not None

    def test_own_command_no_false_override(self):
        """SmartShading's own command: cover at 80 (what was commanded), target changed to 60.
        Own-command guard fires when observed == last_commanded (cover at last position)."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        # Cover is at last_commanded=80, new target=60
        det.tick(
            window_id="w1",
            observed_position=80,
            smartshading_target=60,
            smartshading_assumed=80,  # last_commanded=80 → guard fires → no override
            prev_state=ShadingState.NORMAL_SHADE,
            tolerance=5,
            duration_min=60,
            now=_now(),
        )
        assert det.get("w1", _now()) is None

    def test_none_assumed_no_detection_when_at_target(self):
        """Pre-dispatch: smartshading_assumed=None (prev_observed also None, first cycle), cover at target → no override."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        det.tick(
            window_id="w1",
            observed_position=0,
            smartshading_target=0,
            smartshading_assumed=None,  # no prior dispatch → guard skipped
            prev_state=ShadingState.OPEN,
            tolerance=5,
            duration_min=60,
            now=_now(),
        )
        # No override (observed == target, delta = 0 ≤ tolerance)
        assert det.get("w1", _now()) is None

    def test_observed_none_no_detection(self):
        """Unavailable cover position → no override (fail-safe)."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        det.tick(
            window_id="w1",
            observed_position=None,
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.NORMAL_SHADE,
            tolerance=5,
            duration_min=60,
            now=_now(),
        )
        assert det.get("w1", _now()) is None

    def test_assumed_none_with_delta_uses_tolerance(self):
        """When assumed=None and observed≠target, override is detected (no guard active).
        smartshading_assumed=None represents the case where prev_observed is also None
        (very first cycle after HA restart, no previous observation recorded yet).
        The own-command guard is skipped and detection relies on target comparison.
        Warmup prevents false positives in the first 3 cycles."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        det.tick(
            window_id="w1",
            observed_position=0,
            smartshading_target=80,
            smartshading_assumed=None,
            prev_state=ShadingState.NORMAL_SHADE,
            tolerance=5,
            duration_min=60,
            now=_now(),
        )
        # With assumed=None, guard doesn't fire. delta=80 > tolerance → override.
        assert det.get("w1", _now()) is not None

    def test_warmup_prevents_early_detection(self):
        """First 3 cycles must not detect override even when delta is large."""
        det = OverrideDetector()
        from custom_components.smartshading.engines.override_detector import _WARMUP_CYCLES_REQUIRED

        for i in range(_WARMUP_CYCLES_REQUIRED):
            det.tick(
                window_id="w1",
                observed_position=0,
                smartshading_target=80,
                smartshading_assumed=80,
                prev_state=ShadingState.OPEN,
                tolerance=5,
                duration_min=60,
                now=_now(),
            )
        assert det.get("w1", _now()) is None


# ---------------------------------------------------------------------------
# 4b. target_position=None (BehaviorMode-suppressed dispatch, e.g. an
# ABSENCE_ONLY/DISABLED_AUTOMATIC hold, or an ABSENCE_AND_SCHEDULE
# BehaviorMode:hold on a suppressed daytime fallback) must never itself be
# mistaken for a manual override, however large the position delta looks.
# ---------------------------------------------------------------------------

class TestNoTargetMeansNoNewOverrideDetection:

    def test_none_target_skips_new_override_detection_despite_large_delta(self):
        """No reference to compare against → no new override, even when the
        observed position is far from where an unsuppressed target would be."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        det.tick(
            window_id="w1",
            observed_position=0,
            smartshading_target=None,  # BehaviorMode suppressed dispatch this cycle
            smartshading_assumed=80,   # last real dispatch was to 80 — far from 0
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=_now(),
        )
        assert det.get("w1", _now()) is None

    def test_detection_resumes_once_a_real_target_reappears(self):
        """The None-target skip is per-cycle only — once BehaviorMode allows
        a real target again, override detection must work normally."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        t0 = _now()
        det.tick(
            window_id="w1",
            observed_position=0,
            smartshading_target=None,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=t0,
        )
        assert det.get("w1", t0) is None

        t1 = t0 + timedelta(minutes=5)
        det.tick(
            window_id="w1",
            observed_position=0,   # still far from both target and last_commanded
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=t1,
        )
        assert det.get("w1", t1) is not None


# ---------------------------------------------------------------------------
# 5. AssumedStateManager integration — reliable cover full lifecycle
# ---------------------------------------------------------------------------

class TestReliableCoverFullLifecycle:

    def test_shade_then_user_open_last_commanded_preserved(self):
        """Full lifecycle: shade dispatched → cover shades → user opens.
        last_commanded_position must remain at 80 through all observe() calls."""
        mgr = _manager()

        # Cover starts open (HA=100, internal=0)
        mgr.observe("c1", 0, _now(), has_reliable_position_feedback=True)
        assert mgr.get_state("c1", _now()).last_commanded_position is None

        # SmartShading shades → dispatch (internal=80)
        mgr.update("c1", 80, _now(), has_reliable_position_feedback=True)
        assert mgr.get_state("c1", _now()).last_commanded_position == 80

        # Cover travels and arrives at 80 (HA=20, internal=80) — observe()
        mgr.observe("c1", 80, _now(), has_reliable_position_feedback=True)
        assert mgr.get_state("c1", _now()).assumed_position == 80
        assert mgr.get_state("c1", _now()).last_commanded_position == 80

        # User manually opens → cover at internal=0
        mgr.observe("c1", 0, _now(), has_reliable_position_feedback=True)
        st = mgr.get_state("c1", _now())
        assert st.assumed_position == 0        # observe updated it
        assert st.last_commanded_position == 80  # NOT overwritten by user action


# ---------------------------------------------------------------------------
# 6. RTS/unreliable-feedback settle-window guard (real-world bug report:
# ABSENCE_AND_SCHEDULE terrace-door window falsely flagged manual_override).
#
# Root cause: for a cover WITHOUT reliable position feedback (Somfy RTS etc.),
# coordinator._build_cover_position_observation() still uses a raw
# `current_position` HA attribute as `observed_position` for override
# detection whenever the entity happens to expose one (e.g. a bridge/
# integration doing its own optimistic position tracking) — this is a
# deliberate, tested display convention (test_v10_position_feedback.py
# TestActualPositionPriority / TestHasPositionFeedbackSemantics), NOT a bug
# in itself. But that bridge-side tracking runs on its OWN timing, which can
# genuinely lag SmartShading's own just-sent command by more than the
# existing single-cycle own-command guard (observed vs last_commanded) in
# OverrideDetector.tick() can cover.  When that happens, the still-stale
# observed position looks like a manual override on the very next
# coordinator cycle, and — because ManualOverrideEvaluator (Tier 2) then
# outranks Absence/Night/Schedule (Tier 4) — the window stops reacting to
# Absence/Night/Schedule for up to override_duration_min (daytime) or until
# the next lifecycle transition, exactly matching the field report.
#
# Fix: after a successful dispatch to a cover WITHOUT reliable feedback, the
# coordinator now calls the existing (already-tested)
# suppress_next_override_tick() one-shot suppression — previously only used
# after Active-Control-enable and lifecycle-clear — to give the bridge one
# extra cycle to catch up before override detection resumes.  These tests
# exercise the OverrideDetector-level mechanism the coordinator now relies
# on; they do not re-test the (already-covered) capability/observation
# plumbing above.
# ---------------------------------------------------------------------------

class TestSettleWindowGuardAfterUnreliableFeedbackDispatch:

    def test_without_suppression_a_lagging_rts_reading_creates_false_override(self):
        """Reproduces the bug: RTS bridge still reports the pre-dispatch
        position one cycle after SmartShading's own command — with no
        suppression, this is indistinguishable from a real manual override."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        # SmartShading dispatched target=80; last_commanded is therefore 80,
        # but the RTS bridge's own current_position attribute is still
        # reporting the pre-dispatch value (20) on the very next cycle.
        det.tick(
            window_id="w1",
            observed_position=20,
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=_now(),
        )
        # Bug reproduced: a false override is created from bridge lag alone.
        assert det.get("w1", _now()) is not None

    def test_suppress_next_override_tick_prevents_the_false_positive(self):
        """Same lagging-bridge scenario, but with the coordinator's new
        post-dispatch suppression (unreliable-feedback covers only)."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        # Coordinator calls this right after a successful dispatch to a
        # cover with has_reliable_position_feedback=False.
        det.suppress_next_override_tick("w1")

        det.tick(
            window_id="w1",
            observed_position=20,  # bridge hasn't caught up yet
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=_now(),
        )
        # Suppressed: no override created despite the apparent large delta.
        assert det.get("w1", _now()) is None

    def test_detection_resumes_normally_the_cycle_after_suppression(self):
        """The suppression is one-shot: once consumed, a genuine manual
        override on the FOLLOWING cycle must still be detected normally."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        det.suppress_next_override_tick("w1")
        t0 = _now()
        det.tick(
            window_id="w1",
            observed_position=20,  # suppressed cycle — bridge still lagging
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=t0,
        )
        assert det.get("w1", t0) is None  # confirmed suppressed

        # Next cycle: the bridge has caught up (observed == last_commanded),
        # so this is correctly recognised as SmartShading's own command,
        # not a manual override.
        t1 = t0 + timedelta(minutes=5)
        det.tick(
            window_id="w1",
            observed_position=80,
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=t1,
        )
        assert det.get("w1", t1) is None

    def test_real_override_still_detected_one_cycle_after_suppression(self):
        """A genuine manual override that happens during/after the
        suppressed cycle must still be caught once the suppression is
        consumed — the fix must not create a permanent blind spot."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        det.suppress_next_override_tick("w1")
        t0 = _now()
        det.tick(
            window_id="w1",
            observed_position=20,  # suppressed cycle
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=t0,
        )
        assert det.get("w1", t0) is None

        # Next cycle: the user genuinely moved the cover away from both the
        # target AND last_commanded — a real override, not bridge lag.
        t1 = t0 + timedelta(minutes=5)
        det.tick(
            window_id="w1",
            observed_position=0,
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=t1,
        )
        assert det.get("w1", t1) is not None

    def test_reliable_feedback_cover_is_unaffected_by_this_mechanism(self):
        """Sanity check: the coordinator only calls suppress_next_override_tick
        for unreliable-feedback covers.  A reliable-feedback cover without
        the suppression call must keep detecting overrides exactly as
        before (regression guard for the existing own-command-guard path)."""
        det = OverrideDetector()
        _advance_warmup(det, "w1")

        # No suppress_next_override_tick call — mirrors a reliable-feedback
        # cover's dispatch, where the coordinator intentionally does not
        # suppress (real feedback is trusted immediately).
        det.tick(
            window_id="w1",
            observed_position=0,  # user moved it, no lag involved
            smartshading_target=80,
            smartshading_assumed=80,
            prev_state=ShadingState.OPEN,
            tolerance=10,
            duration_min=120,
            now=_now(),
        )
        assert det.get("w1", _now()) is not None
