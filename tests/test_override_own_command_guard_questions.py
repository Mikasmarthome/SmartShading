"""Own-command-guard audit answers and required tests — T7 pre-push review
point 5.

Audit answers (verified against engines/override_detector.py and
cover_control/assumed_state_manager.py):

  1. How long does a command count as "own command"?
     Indefinitely, until superseded by the NEXT actual dispatch.
     `smartshading_assumed` is sourced each cycle from
     AssumedStateManager.get_state(cover_id, now).last_commanded_position,
     which (per its own field comment) is "set ONLY by update() /
     on_reference_travel(). Never overwritten by observe()" — i.e. it holds
     whatever SmartShading last actually commanded until a new command
     replaces it. There is no separate timeout inside the override-detector
     guard itself.
  2. What data is compared?
     `observed_position` (HA-reported cover position, converted to internal
     0=open/100=shaded convention) vs. `smartshading_assumed` (last
     commanded position), within `tolerance`
     (override_detection_tolerance).
  3. Can a real manual counter-movement shortly after a SmartShading
     command be falsely suppressed?
     Only in the narrow case where the user's new position happens to fall
     WITHIN tolerance of SmartShading's own last-commanded position — an
     inherent, pre-existing limitation of position-delta detection (not
     introduced or worsened by T7; T7 only extends the SAME existing guard
     logic from the "new override" branch to the "renewal" branch). Any
     movement beyond tolerance is still detected normally — proven by
     TestRealMovementStillDetected below.
  4. Exact target position or a tolerance span?
     A tolerance span — the same `tolerance` value used for all override
     detection in this class, not a stricter/separate exact-match check.
  5. Is the own-command state consumed / time-limited after detection?
     No explicit consumption or timeout. It is re-evaluated fresh every
     cycle directly from AssumedStateManager's CURRENT last-commanded
     value — so delayed feedback (RTS lag, slow cover) still correctly
     matches as long as no newer command has been dispatched in the
     meantime. This is the intended behavior (it is the same mechanism the
     pre-existing "unreliable feedback" fix relies on — see
     tests/test_v10_override_fix.py TestSettleWindowGuardAfterUnreliableFeedbackDispatch).
  6. Does the change also affect legacy mode?
     Yes — the guard extension to the renewal branch is unconditional
     (applies to both legacy and fixed_time). This is intentional: the
     guard's purpose (never misread SmartShading's own dispatch as a user
     action) is duration-mode-independent. Verified not to change any
     pre-existing legacy-mode test outcome (full regression suite,
     unchanged).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_WARMUP_NOW = datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)


def _detector_with_active_override(duration_mode: str = "legacy", fixed_until: time | None = None) -> tuple[OverrideDetector, datetime]:
    det = OverrideDetector()
    det.tick(
        window_id="w1", observed_position=0, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_WARMUP_NOW,
    )
    t0 = _WARMUP_NOW + timedelta(minutes=1)
    if duration_mode == "fixed_time":
        # DURATION is tick()'s "legacy" mode, renamed (T10) — explicit here
        # since tick()'s own default changed from that to LIFECYCLE.
        kwargs = {"release_strategy": OverrideReleaseStrategy.FIXED_TIME, "fixed_until": fixed_until, "now_local": t0}
    else:
        kwargs = {"release_strategy": OverrideReleaseStrategy.DURATION}
    det.tick(
        window_id="w1", observed_position=20, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0, **kwargs,
    )
    return det, t0


class TestAllowedCommandDoesNotRenewOverride:
    def test_allowed_comfort_dispatch_does_not_renew(self) -> None:
        det, t0 = _detector_with_active_override()
        original = det.get("w1", t0)
        t1 = t0 + timedelta(minutes=2)
        # SmartShading dispatches an allowed Comfort action to 55.
        det.tick(
            window_id="w1", observed_position=55, smartshading_target=55,
            smartshading_assumed=55, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t1,
        )
        after = det.get("w1", t1)
        assert after is not None
        assert after.override_position == original.override_position
        assert after.expires_at == original.expires_at
        assert after.started_at == original.started_at

    def test_allowed_protection_dispatch_does_not_renew(self) -> None:
        det, t0 = _detector_with_active_override()
        original = det.get("w1", t0)
        t1 = t0 + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=70, smartshading_target=70,
            smartshading_assumed=70, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t1,
        )
        after = det.get("w1", t1)
        assert after is not None
        assert after.override_position == original.override_position
        assert after.expires_at == original.expires_at


class TestRealMovementStillDetected:
    def test_manual_move_to_different_position_detected_despite_prior_own_command(self) -> None:
        """A prior own-command (dispatch to 55) does not permanently blind
        detection — a genuine manual move to yet another position (90,
        beyond tolerance of BOTH 55 and the original override) is caught."""
        det, t0 = _detector_with_active_override()
        t1 = t0 + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=55, smartshading_target=55,
            smartshading_assumed=55, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t1,
        )
        assert det.get("w1", t1).override_position == 20  # unaffected by own-command dispatch

        t2 = t1 + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=90, smartshading_target=55,
            smartshading_assumed=55,  # still SmartShading's last command — 90 is NOT it
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=10, duration_min=120, now=t2,
        )
        renewed = det.get("w1", t2)
        assert renewed is not None
        assert renewed.override_position == 90
        assert renewed.started_at == t2  # a genuine renewal


class TestIdenticalFeedbackIgnored:
    def test_exact_match_to_own_position_ignored(self) -> None:
        det, t0 = _detector_with_active_override()
        t1 = t0 + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=42, smartshading_target=42,
            smartshading_assumed=42, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t1,
        )
        after = det.get("w1", t1)
        assert after.override_position == 20  # unchanged — exact match recognized as own command


class TestDelayedFeedbackStillRecognizedAsOwnCommand:
    def test_late_feedback_several_cycles_after_dispatch_does_not_spuriously_renew(self) -> None:
        """The guard has no separate timeout: as long as no NEWER command
        has been dispatched meanwhile, delayed cover feedback (e.g. a slow
        or RTS-lagging cover) catching up to the ORIGINAL dispatched
        position several cycles later still correctly matches
        smartshading_assumed and must not renew the override."""
        det, t0 = _detector_with_active_override()
        t1 = t0 + timedelta(minutes=2)
        # Dispatch happens; feedback has not arrived yet (still shows 20).
        det.tick(
            window_id="w1", observed_position=20, smartshading_target=55,
            smartshading_assumed=55, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t1,
        )
        assert det.get("w1", t1).override_position == 20  # renewal check: 20 vs existing 20 -> no delta, no renewal anyway

        # Several cycles later, the delayed feedback finally arrives (55) —
        # no new dispatch happened in between, smartshading_assumed is
        # still 55.
        t2 = t1 + timedelta(minutes=15)
        det.tick(
            window_id="w1", observed_position=55, smartshading_target=55,
            smartshading_assumed=55, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t2,
        )
        after = det.get("w1", t2)
        assert after is not None
        assert after.override_position == 20  # still unchanged — no spurious renewal
        assert after.started_at == t0  # same original override instance, never renewed


class TestLegacyRenewalByRealMovementStillWorks:
    def test_legacy_renewal_unaffected_by_guard_extension(self) -> None:
        """Baseline regression proof: the own-command-guard extension to
        the renewal branch does not impede a genuine manual renewal in
        legacy mode (no dispatch has happened — smartshading_assumed
        reflects something else entirely, or is None)."""
        det, t0 = _detector_with_active_override()
        t1 = t0 + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=75, smartshading_target=0,
            smartshading_assumed=None,  # no dispatch reference available
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=10, duration_min=120, now=t1,
        )
        renewed = det.get("w1", t1)
        assert renewed is not None
        assert renewed.override_position == 75
        assert renewed.expires_at == t1 + timedelta(minutes=120)  # legacy extension, unchanged
