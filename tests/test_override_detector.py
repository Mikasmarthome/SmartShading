"""Tests for OverrideDetector.

OverrideDetector contract:
  - get() returns None for unknown windows and clears expired overrides.
  - tick() detects overrides after the warmup period (_WARMUP_CYCLES_REQUIRED cycles).
  - tick() does not detect during warmup, regardless of delta.
  - tick() does not detect when observed_position is None.
  - tick() does not detect when delta <= tolerance.
  - tick() detects when delta > tolerance after warmup.
  - tick() renews an active override (new position, reset timer) when the
    user moves the cover again while override is already active.
  - clear() explicitly removes an active override.
  - Overrides from different windows are independent.
  - overridden_state and overridden_position are recorded on creation.
  - On renewal, the original overridden context is preserved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.engines.override_detector import (
    OverrideDetector,
    _WARMUP_CYCLES_REQUIRED,
)
from custom_components.smartshading.models.manual_override import (
    ManualOverride,
    OverrideReleaseStrategy,
)
from custom_components.smartshading.state_machine.states import ShadingState

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)
_TOLERANCE = 10
_DURATION_MIN = 240
_WINDOW = "w-south"
_PREV_STATE = ShadingState.NORMAL_SHADE
_TARGET = 75  # SmartShading target (internal)

# Number of warmup cycles that must pass before detection is active.
_WARMUP = _WARMUP_CYCLES_REQUIRED


# ---------------------------------------------------------------------------
# Helper: advance detector past warmup for a window
# ---------------------------------------------------------------------------

def _warmup(detector: OverrideDetector, window_id: str = _WINDOW) -> None:
    """Run enough tick() calls (with matching positions) to leave warmup."""
    for _ in range(_WARMUP):
        detector.tick(
            window_id=window_id,
            observed_position=_TARGET,        # no delta → no override
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def detector() -> OverrideDetector:
    return OverrideDetector()


# ---------------------------------------------------------------------------
# get() basics
# ---------------------------------------------------------------------------

class TestOverrideDetectorGet:
    def test_get_unknown_window_returns_none(self, detector: OverrideDetector) -> None:
        assert detector.get("never-seen", _NOW) is None

    def test_get_clears_expired_override(self, detector: OverrideDetector) -> None:
        _warmup(detector)
        # Create override
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is not None

        # Travel past expiry
        expired_now = _NOW + timedelta(minutes=_DURATION_MIN + 1)
        assert detector.get(_WINDOW, expired_now) is None

    def test_get_returns_active_override(self, detector: OverrideDetector) -> None:
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        result = detector.get(_WINDOW, _NOW)
        assert result is not None
        assert result.window_id == _WINDOW


# ---------------------------------------------------------------------------
# Warmup guard
# ---------------------------------------------------------------------------

class TestOverrideDetectorWarmup:
    def test_no_detection_in_first_cycle(self, detector: OverrideDetector) -> None:
        # Large delta — but still in warmup
        detector.tick(
            window_id=_WINDOW,
            observed_position=0,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is None

    def test_no_detection_in_second_cycle(self, detector: OverrideDetector) -> None:
        """Two cycles: warmup in cycle 1, guard fires in cycle 2 (assumed=observed)."""
        for _ in range(2):
            detector.tick(
                window_id=_WINDOW,
                observed_position=0,
                smartshading_target=_TARGET,
                smartshading_assumed=0,  # assumed = observed → guard fires → no override
                prev_state=_PREV_STATE,
                tolerance=_TOLERANCE,
                duration_min=_DURATION_MIN,
                now=_NOW,
            )
        assert detector.get(_WINDOW, _NOW) is None

    def test_no_detection_at_warmup_boundary(self, detector: OverrideDetector) -> None:
        """Cycle index _WARMUP - 1 is still inside warmup (0-indexed counter)."""
        for _ in range(_WARMUP):
            detector.tick(
                window_id=_WINDOW,
                observed_position=0,
                smartshading_target=_TARGET,
                prev_state=_PREV_STATE,
                tolerance=_TOLERANCE,
                duration_min=_DURATION_MIN,
                now=_NOW,
            )
        # The _WARMUP-th call (index _WARMUP) was the last warmup cycle;
        # the next call (index _WARMUP) should finally detect.
        assert detector.get(_WINDOW, _NOW) is None

    def test_detection_after_warmup(self, detector: OverrideDetector) -> None:
        """One more tick after the warmup boundary → detection fires."""
        _warmup(detector)  # 3 no-op ticks (matching positions)
        detector.tick(
            window_id=_WINDOW,
            observed_position=0,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is not None

    def test_warmup_is_per_window(self, detector: OverrideDetector) -> None:
        """Warmup counter is independent per window."""
        _warmup(detector, window_id="w-A")
        # w-B has not yet warmed up
        detector.tick(
            window_id="w-B",
            observed_position=0,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get("w-B", _NOW) is None


# ---------------------------------------------------------------------------
# Detection threshold
# ---------------------------------------------------------------------------

class TestOverrideDetectorThreshold:
    def test_no_detection_within_tolerance(self, detector: OverrideDetector) -> None:
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=_TARGET + _TOLERANCE,  # exactly at boundary: NOT > tolerance
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is None

    def test_detection_just_above_tolerance(self, detector: OverrideDetector) -> None:
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=_TARGET + _TOLERANCE + 1,  # strictly > tolerance
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is not None

    def test_no_detection_when_position_unknown(self, detector: OverrideDetector) -> None:
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=None,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is None

    def test_delta_below_zero_also_detected(self, detector: OverrideDetector) -> None:
        """abs() is used; negative delta behaves identically."""
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=_TARGET - _TOLERANCE - 1,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is not None


# ---------------------------------------------------------------------------
# Override metadata
# ---------------------------------------------------------------------------

class TestOverrideDetectorMetadata:
    def _detect(self, detector: OverrideDetector, observed: int = 10) -> None:
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=observed,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )

    def test_override_position_matches_observed(self, detector: OverrideDetector) -> None:
        self._detect(detector, observed=20)
        result = detector.get(_WINDOW, _NOW)
        assert result is not None
        assert result.override_position == 20

    def test_overridden_state_recorded(self, detector: OverrideDetector) -> None:
        self._detect(detector)
        result = detector.get(_WINDOW, _NOW)
        assert result is not None
        assert result.overridden_state is _PREV_STATE

    def test_overridden_position_recorded(self, detector: OverrideDetector) -> None:
        self._detect(detector)
        result = detector.get(_WINDOW, _NOW)
        assert result is not None
        assert result.overridden_position == _TARGET

    def test_source_is_position_delta(self, detector: OverrideDetector) -> None:
        self._detect(detector)
        result = detector.get(_WINDOW, _NOW)
        assert result is not None
        assert result.source == "position_delta"

    def test_expires_at_is_started_plus_duration(self, detector: OverrideDetector) -> None:
        self._detect(detector)
        result = detector.get(_WINDOW, _NOW)
        assert result is not None
        assert result.expires_at == _NOW + timedelta(minutes=_DURATION_MIN)

    def test_started_at_matches_detection_time(self, detector: OverrideDetector) -> None:
        self._detect(detector)
        result = detector.get(_WINDOW, _NOW)
        assert result is not None
        assert result.started_at == _NOW


# ---------------------------------------------------------------------------
# Override renewal (user moves cover again while override is active)
# ---------------------------------------------------------------------------

class TestOverrideDetectorRenewal:
    def test_renewal_on_position_change(self, detector: OverrideDetector) -> None:
        """If user moves cover again, override is renewed at new position."""
        _warmup(detector)
        # Initial override: user moved to 20
        detector.tick(
            window_id=_WINDOW,
            observed_position=20,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        first = detector.get(_WINDOW, _NOW)
        assert first is not None
        assert first.override_position == 20

        # Two hours later user moves again to 50 — renewal
        later = _NOW + timedelta(hours=2)
        detector.tick(
            window_id=_WINDOW,
            observed_position=50,
            smartshading_target=_TARGET,
            prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=later,
        )
        renewed = detector.get(_WINDOW, later)
        assert renewed is not None
        assert renewed.override_position == 50

    def test_renewal_resets_expiry(self, detector: OverrideDetector) -> None:
        """Renewed override gets a fresh expiry from the renewal time.

        v1.2.0-beta.1, T10: extends-on-renewal is now specific to the
        DURATION release strategy (see engines/override_release.
        extends_on_renewal()) rather than the detector's own default —
        explicitly requested here since that is exactly the behavior this
        test verifies."""
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=20,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
            release_strategy=OverrideReleaseStrategy.DURATION,
        )
        later = _NOW + timedelta(hours=2)
        detector.tick(
            window_id=_WINDOW,
            observed_position=50,
            smartshading_target=_TARGET,
            prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=later,
            release_strategy=OverrideReleaseStrategy.DURATION,
        )
        renewed = detector.get(_WINDOW, later)
        assert renewed is not None
        assert renewed.expires_at == later + timedelta(minutes=_DURATION_MIN)

    def test_renewal_preserves_original_overridden_context(
        self, detector: OverrideDetector
    ) -> None:
        """On renewal, overridden_state/overridden_position keep original context."""
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=20,
            smartshading_target=_TARGET,
            prev_state=ShadingState.STRONG_SHADE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        later = _NOW + timedelta(hours=1)
        detector.tick(
            window_id=_WINDOW,
            observed_position=50,
            smartshading_target=_TARGET,
            prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=later,
        )
        renewed = detector.get(_WINDOW, later)
        assert renewed is not None
        # Original context preserved — not overwritten with MANUAL_OVERRIDE
        assert renewed.overridden_state is ShadingState.STRONG_SHADE

    def test_no_renewal_within_tolerance(self, detector: OverrideDetector) -> None:
        """Small position change within tolerance → no renewal."""
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=20,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        original = detector.get(_WINDOW, _NOW)
        assert original is not None

        later = _NOW + timedelta(hours=1)
        # Small drift: within tolerance of override_position=20
        detector.tick(
            window_id=_WINDOW,
            observed_position=25,  # |25-20|=5 <= tolerance(10)
            smartshading_target=_TARGET,
            prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=later,
        )
        # Still the original override
        after = detector.get(_WINDOW, later)
        assert after is not None
        assert after.override_position == 20  # unchanged


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------

class TestOverrideDetectorClear:
    def test_clear_removes_active_override(self, detector: OverrideDetector) -> None:
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is not None
        detector.clear(_WINDOW)
        assert detector.get(_WINDOW, _NOW) is None

    def test_clear_unknown_window_does_not_raise(
        self, detector: OverrideDetector
    ) -> None:
        detector.clear("unknown-window")  # must not raise

    def test_clear_does_not_affect_other_windows(
        self, detector: OverrideDetector
    ) -> None:
        _warmup(detector, window_id="w-A")
        _warmup(detector, window_id="w-B")
        for wid in ("w-A", "w-B"):
            detector.tick(
                window_id=wid,
                observed_position=10,
                smartshading_target=_TARGET,
                prev_state=_PREV_STATE,
                tolerance=_TOLERANCE,
                duration_min=_DURATION_MIN,
                now=_NOW,
            )
        detector.clear("w-A")
        assert detector.get("w-A", _NOW) is None
        assert detector.get("w-B", _NOW) is not None


# ---------------------------------------------------------------------------
# Multiple windows are independent
# ---------------------------------------------------------------------------

class TestOverrideDetectorMultipleWindows:
    def test_windows_have_independent_overrides(
        self, detector: OverrideDetector
    ) -> None:
        _warmup(detector, "w-north")
        _warmup(detector, "w-south")

        # Override on south only
        detector.tick(
            window_id="w-south",
            observed_position=10,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        detector.tick(
            window_id="w-north",
            observed_position=_TARGET,  # matching — no override
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )

        assert detector.get("w-south", _NOW) is not None
        assert detector.get("w-north", _NOW) is None

    def test_windows_have_independent_warmup(
        self, detector: OverrideDetector
    ) -> None:
        _warmup(detector, "w-A")  # w-A warmed up
        # w-B: only _WARMUP ticks (just at warmup boundary, no detection tick yet)
        for _ in range(_WARMUP):
            detector.tick(
                window_id="w-B",
                observed_position=10,
                smartshading_target=_TARGET,
                prev_state=_PREV_STATE,
                tolerance=_TOLERANCE,
                duration_min=_DURATION_MIN,
                now=_NOW,
            )
        detector.tick(
            window_id="w-A",
            observed_position=10,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get("w-A", _NOW) is not None
        assert detector.get("w-B", _NOW) is None


# ---------------------------------------------------------------------------
# F30 field fix — natural timeout clear must suppress the very next
# detection tick, mirroring the existing Active-Control-enable and
# lifecycle-transition clear paths (both already pair clear()+
# suppress_next_override_tick()). Without this, a window whose cover has not
# physically moved away from the just-expired override position gets an
# immediately reborn override the instant the automatic decision differs
# from that unmoved position — which is precisely the field-reported "stuck
# override survives an entire day" symptom.
# ---------------------------------------------------------------------------

class TestOverrideDetectorTimeoutSuppression:
    def test_get_expiry_suppresses_the_next_tick(self, detector: OverrideDetector) -> None:
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        expired_now = _NOW + timedelta(minutes=_DURATION_MIN + 1)
        assert detector.get(_WINDOW, expired_now) is None  # natural timeout clear

        # The cover is still at the old override position (10); the
        # automatic target has since moved on to something else (30). A
        # tick() right after the expiry must NOT reinterpret this unmoved
        # position as a brand-new override.
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=30,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=expired_now,
        )
        assert detector.get(_WINDOW, expired_now) is None

    def test_ticks_own_redundant_expiry_check_suppresses_the_same_call(
        self, detector: OverrideDetector
    ) -> None:
        # Exercises tick()'s own internal expiry check (not get()) — the
        # override expires and a fresh, differing target is proposed within
        # the very same tick() call.
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        expired_now = _NOW + timedelta(minutes=_DURATION_MIN + 1)
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,       # cover has not moved
            smartshading_target=30,    # automatic decision now differs
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=expired_now,
        )
        assert detector.get(_WINDOW, expired_now) is None

    def test_no_reborn_override_on_the_cycle_right_after_timeout(
        self, detector: OverrideDetector
    ) -> None:
        # Regression for the exact field report shape: override at 100
        # expires, automatic target becomes 30, cover position stays 100
        # (never actually moved) — the cycle immediately following the
        # expiry must not resurrect a new override. This is a one-shot
        # grace cycle (matching the existing Active-Control-enable and
        # lifecycle-transition suppression precedent, not a new multi-cycle
        # grace mechanism): it gives the automatic decision one real chance
        # to dispatch. If the cover genuinely still has not moved several
        # cycles later, a fresh (shorter, daytime-scoped) override is
        # expected again — that is unchanged, pre-existing behavior, not
        # part of this fix's scope.
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=100,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is not None

        expired_now = _NOW + timedelta(minutes=_DURATION_MIN + 1)
        assert detector.get(_WINDOW, expired_now) is None

        detector.tick(
            window_id=_WINDOW,
            observed_position=100,   # cover never actually moved
            smartshading_target=30,  # automatic decision now wants 30
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=expired_now,
        )
        assert detector.get(_WINDOW, expired_now) is None

    def test_genuine_new_manual_move_after_suppression_is_still_detected(
        self, detector: OverrideDetector
    ) -> None:
        # The one-shot suppression must not permanently disable detection —
        # a later, real manual move must still be recognized as an override.
        _warmup(detector)
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=_TARGET,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        expired_now = _NOW + timedelta(minutes=_DURATION_MIN + 1)
        assert detector.get(_WINDOW, expired_now) is None

        # Suppressed tick: consumed here, no detection yet.
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=30,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=expired_now,
        )
        assert detector.get(_WINDOW, expired_now) is None

        # Automatic decision succeeds and the cover moves to 30 — no
        # mismatch, still no override.
        settled_now = expired_now + timedelta(minutes=5)
        detector.tick(
            window_id=_WINDOW,
            observed_position=30,
            smartshading_target=30,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=settled_now,
        )
        assert detector.get(_WINDOW, settled_now) is None

        # Later, the user genuinely moves the cover again — this MUST be
        # detected as a fresh override.
        moved_now = settled_now + timedelta(minutes=5)
        detector.tick(
            window_id=_WINDOW,
            observed_position=90,
            smartshading_target=30,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=moved_now,
        )
        result = detector.get(_WINDOW, moved_now)
        assert result is not None
        assert result.override_position == 90

    def test_stale_persisted_override_dropped_on_restore_does_not_immediately_resurrect(
        self, detector: OverrideDetector
    ) -> None:
        # F30: a persisted override that had already expired before restart
        # is dropped by restore_active_overrides() without ever entering
        # _active_overrides — the cover is presumably still at that old
        # position, so the first post-restart tick() must not immediately
        # recreate a "new" override from the still-unmoved position.
        stale = ManualOverride(
            window_id=_WINDOW,
            override_position=10,
            started_at=_NOW - timedelta(minutes=_DURATION_MIN + 30),
            expires_at=_NOW - timedelta(minutes=30),  # already expired
            source="position_delta",
            overridden_state=_PREV_STATE,
            overridden_position=_TARGET,
            scope="daytime",
        )
        restored = detector.restore_active_overrides([stale.to_dict()], _NOW)
        assert restored == []  # dropped, not resurrected
        assert detector.get(_WINDOW, _NOW) is None

        # First post-restart tick: cover still at the old override position
        # (10), automatic target now differs (30). Must not immediately
        # recreate an override (warmup guard + suppression both apply here).
        detector.tick(
            window_id=_WINDOW,
            observed_position=10,
            smartshading_target=30,
            prev_state=_PREV_STATE,
            tolerance=_TOLERANCE,
            duration_min=_DURATION_MIN,
            now=_NOW,
        )
        assert detector.get(_WINDOW, _NOW) is None
