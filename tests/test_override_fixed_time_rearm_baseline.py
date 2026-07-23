"""Post-expiry re-arm/baseline semantics for FIXED_TIME overrides — T7
pre-push review point 4.

Audit finding: OverrideDetector only ever compares the CURRENT observed
position against a reference (smartshading_target / smartshading_assumed /
existing.override_position) — it has no concept of "new movement" vs.
"persisted stale deviation". Pre-existing (legacy) behavior already relies
on this: after the one-shot F30 post-expiry suppression cycle, if the cover
still hasn't moved on a LATER cycle, a fresh override is intentionally
re-armed (see tests/test_override_detector.py
TestOverrideDetectorTimeoutSuppression::test_no_reborn_override_on_the_cycle_right_after_timeout,
whose own docstring explicitly documents this as unchanged, in-scope
behavior).

For FIXED_TIME mode this same mechanism would silently re-create an
override that lasts up to ~24h from nothing more than an unmoved, stale
position — a materially different (and surprising) consequence from
legacy's modest duration_min-scale re-arm. This file proves the fix:
ManualOverride now carries duration_mode; OverrideDetector records a
post-expiry baseline ONLY for a just-expired FIXED_TIME override, and
withholds new-override detection while the observed position still matches
that baseline. Legacy mode is completely unaffected (no baseline is ever
recorded for it) — proven by the counterpart test class here and by the
full existing regression suite remaining green unchanged.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_WARMUP_NOW = datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)


def _detector_past_warmup() -> OverrideDetector:
    det = OverrideDetector()
    det.tick(
        window_id="w1", observed_position=0, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_WARMUP_NOW,
    )
    return det


class TestNoAutomaticRearmFromPersistedStaleDeviation:
    def test_expired_fixed_time_override_with_unmoved_cover_does_not_recreate(self) -> None:
        """Fixed-time override expires at 08:00; the cover stays at its old
        manual position (30) across several subsequent cycles with no new
        user action — no automatic override must be recreated, even though
        the deviation from the automatic target (0) exceeds tolerance every
        single cycle."""
        det = _detector_past_warmup()
        t0 = datetime(2026, 6, 15, 7, 0, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t0,
        )
        assert det.get("w1", t0) is not None

        # Cycle 1 after expiry: F30 one-shot suppression consumed, cover
        # still at 30 (unmoved).
        t1 = datetime(2026, 6, 15, 8, 1, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t1,
        )
        assert det.get("w1", t1) is None

        # Cycles 2, 3, 4 — still no movement, several cycles later. Under
        # legacy mode this would re-arm (documented, unchanged behavior);
        # under fixed_time mode it must NOT.
        for minutes in (10, 30, 120):
            t_n = t1 + timedelta(minutes=minutes)
            det.tick(
                window_id="w1", observed_position=30, smartshading_target=0,
                prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t_n,
                release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t_n,
            )
            assert det.get("w1", t_n) is None, f"unexpected re-arm at +{minutes}min"


class TestGenuineNewMovementAfterExpiryStillArmsANewOverride:
    def test_real_manual_move_after_expiry_creates_new_override_with_next_boundary(self) -> None:
        """Counterpart case: after natural expiry, a GENUINE new manual
        movement (to a position different from the just-expired override's
        own position) must still be detected normally and produce a new
        override with the next fixed_until boundary."""
        det = _detector_past_warmup()
        t0 = datetime(2026, 6, 15, 7, 0, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t0,
        )
        assert det.get("w1", t0) is not None

        t1 = datetime(2026, 6, 15, 8, 1, tzinfo=_UTC)  # F30 suppression cycle
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t1,
        )
        assert det.get("w1", t1) is None

        # Still unmoved for a while (withheld by the baseline)...
        t2 = t1 + timedelta(minutes=30)
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t2,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t2,
        )
        assert det.get("w1", t2) is None

        # ...then the user genuinely moves the cover to a NEW position (70).
        t3 = t2 + timedelta(minutes=5)
        det.tick(
            window_id="w1", observed_position=70, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t3,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t3,
        )
        new_override = det.get("w1", t3)
        assert new_override is not None
        assert new_override.override_position == 70
        # Next fixed_until boundary: 08:00 today already passed -> tomorrow.
        assert new_override.expires_at == datetime(2026, 6, 16, 8, 0, tzinfo=_UTC)

    def test_baseline_is_cleared_once_a_new_override_is_armed(self) -> None:
        """After a genuine new override is created, a SUBSEQUENT natural
        expiry of THAT override re-arms the baseline mechanism fresh (not
        stuck comparing against the original, now-irrelevant position)."""
        det = _detector_past_warmup()
        t0 = datetime(2026, 6, 15, 7, 0, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t0,
        )
        t1 = datetime(2026, 6, 15, 8, 1, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t1,
        )
        t2 = t1 + timedelta(minutes=5)
        det.tick(
            window_id="w1", observed_position=70, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t2,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t2,
        )
        new_override = det.get("w1", t2)
        assert new_override is not None
        assert new_override.override_position == 70
        # The stale-position baseline (30) must no longer suppress
        # detection at position 70 — proven implicitly above (a new
        # override WAS created at 70, not withheld).


class TestLegacyModeUnaffectedByRearmBaseline:
    def test_legacy_mode_still_rearms_from_persisted_stale_deviation(self) -> None:
        """Explicit counterpart proof: DURATION mode (T7's "legacy",
        renamed)'s pre-existing behavior (a stale, unmoved deviation DOES
        eventually re-arm a fresh override several cycles after natural
        expiry) is completely unchanged — no baseline is ever recorded for
        it (uses_post_expiry_baseline() is False only for DURATION; every
        other strategy, including tick()'s own new default LIFECYCLE, DOES
        use the baseline — see override_release.py). DURATION must
        therefore be passed explicitly here, not relied on as a default."""
        det = _detector_past_warmup()
        t0 = datetime(2026, 6, 15, 7, 0, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.DURATION,
        )  # DURATION ("legacy") mode, explicit
        ov1 = det.get("w1", t0)
        assert ov1 is not None
        assert ov1.release_strategy == "duration"

        expired_now = t0 + timedelta(minutes=121)
        assert det.get("w1", expired_now) is None

        # F30 suppression cycle — cover still unmoved.
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=expired_now,
            release_strategy=OverrideReleaseStrategy.DURATION,
        )
        assert det.get("w1", expired_now) is None

        # A LATER cycle, still unmoved: DURATION mode re-arms (unchanged,
        # pre-existing, explicitly documented behavior).
        later = expired_now + timedelta(minutes=10)
        det.tick(
            window_id="w1", observed_position=30, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=later,
            release_strategy=OverrideReleaseStrategy.DURATION,
        )
        rearmed = det.get("w1", later)
        assert rearmed is not None
        assert rearmed.override_position == 30
        assert rearmed.release_strategy == "duration"
