"""OverrideDetector.tick() — T7 fixed-time renewal semantics and the
extended own-command guard for allowed Protection/Comfort pass-through.

Coverage (T7 review points 8, 10, 11, and the numbered test list items
24/25/31-34):
  ODF-01  Legacy mode: a real manual renewal before natural expiry still
          extends expires_at exactly as before T7 (unchanged legacy path).
  ODF-02  Fixed-time mode: a real manual renewal BEFORE the fixed boundary
          does NOT move expires_at — the boundary stays fixed.
  ODF-03  Fixed-time mode: a genuinely NEW override created after the
          previous one naturally expired gets the NEXT fixed_until
          occurrence (today or tomorrow, per compute_fixed_time_expiry).
  ODF-04  Own-command guard extended to the "existing override" branch: an
          allowed pass-through dispatch (observed position lands on
          smartshading_assumed, SmartShading's own last-commanded position)
          does not get misread as a manual renewal — override_position and
          expires_at stay untouched.
  ODF-05  A genuine manual move while an override is active (NOT matching
          smartshading_assumed) still renews normally — the guard does not
          suppress real overrides.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_WARMUP_NOW = datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)


def _detector_past_warmup(window_id: str = "w1") -> OverrideDetector:
    det = OverrideDetector()
    det.tick(
        window_id=window_id, observed_position=0, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_WARMUP_NOW,
    )
    return det


class TestLegacyRenewalUnchanged:
    def test_manual_renewal_before_expiry_extends_expires_at(self) -> None:
        # DURATION is T7's "legacy" mode, renamed — only DURATION extends
        # expires_at on renewal (extends_on_renewal(), override_release.py);
        # every other strategy (including the tick()-default LIFECYCLE) keeps
        # the original boundary, so this legacy-renewal-extends behavior must
        # be pinned explicitly rather than relying on tick()'s own default.
        det = _detector_past_warmup()
        t0 = _WARMUP_NOW + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=20, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.DURATION,
        )
        ov1 = det.get("w1", t0)
        assert ov1 is not None
        assert ov1.expires_at == t0 + timedelta(minutes=120)

        t1 = t0 + timedelta(minutes=5)
        det.tick(
            window_id="w1", observed_position=60, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            release_strategy=OverrideReleaseStrategy.DURATION,
        )
        ov2 = det.get("w1", t1)
        assert ov2 is not None
        assert ov2.override_position == 60
        assert ov2.expires_at == t1 + timedelta(minutes=120)  # extended, as before T7


class TestFixedTimeRenewalDoesNotMoveBoundary:
    def test_renewal_before_fixed_boundary_keeps_original_expires_at(self) -> None:
        det = _detector_past_warmup()
        t0 = datetime(2026, 6, 15, 7, 0, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=20, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t0,
        )
        ov1 = det.get("w1", t0)
        assert ov1 is not None
        assert ov1.expires_at == datetime(2026, 6, 15, 8, 0, tzinfo=_UTC)

        t1 = datetime(2026, 6, 15, 7, 45, tzinfo=_UTC)  # still before 08:00
        det.tick(
            window_id="w1", observed_position=60, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t1,
        )
        ov2 = det.get("w1", t1)
        assert ov2 is not None
        assert ov2.override_position == 60  # position DOES update
        assert ov2.expires_at == datetime(2026, 6, 15, 8, 0, tzinfo=_UTC)  # boundary UNCHANGED


class TestFixedTimeNewOverrideAfterExpiryGetsNextBoundary:
    def test_new_override_after_natural_expiry_gets_next_days_boundary(self) -> None:
        det = _detector_past_warmup()
        t0 = datetime(2026, 6, 15, 7, 0, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=20, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t0,
        )
        ov1 = det.get("w1", t0)
        assert ov1.expires_at == datetime(2026, 6, 15, 8, 0, tzinfo=_UTC)

        # Move to just after 08:00 — the override naturally expires.  This
        # same tick() call also suppresses its OWN detection this cycle
        # (F30 field fix — the cover has not had any chance to move away
        # from the just-expired override position yet), so the "new
        # override" check must happen on a LATER cycle.
        t_expired = datetime(2026, 6, 15, 8, 1, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=20, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t_expired,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t_expired,
        )
        assert det.get("w1", t_expired) is None

        # A brand-new manual move on the NEXT cycle creates a NEW override —
        # the boundary must be recomputed (next occurrence: tomorrow 08:00,
        # since 08:00 today has already passed).
        t_new_move = t_expired + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=55, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t_new_move,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=t_new_move,
        )
        ov2 = det.get("w1", t_new_move)
        assert ov2 is not None
        assert ov2.expires_at == datetime(2026, 6, 16, 8, 0, tzinfo=_UTC)


class TestOwnCommandGuardCoversAllowedPassthroughRenewal:
    def test_own_dispatch_matching_last_commanded_does_not_renew(self) -> None:
        """T7 review point 11: SmartShading dispatches an ALLOWED Protection/
        Comfort position while an override remains active. The cover
        physically settles there; the next tick() observes that position.
        Since it matches smartshading_assumed (SmartShading's own last
        commanded position), this must NOT be read as a fresh manual
        movement — override_position and expires_at stay exactly as they
        were."""
        det = _detector_past_warmup()
        t0 = _WARMUP_NOW + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=20, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
        )
        original = det.get("w1", t0)
        assert original is not None
        assert original.override_position == 20

        # SmartShading dispatches an allowed protection action to position 70;
        # the cover settles there, and smartshading_assumed now reflects it.
        t1 = t0 + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=70, smartshading_target=70,
            smartshading_assumed=70,  # SmartShading's own last-commanded position
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=10, duration_min=120, now=t1,
        )
        after = det.get("w1", t1)
        assert after is not None
        assert after.override_position == 20  # unchanged — not renewed
        assert after.expires_at == original.expires_at  # unchanged — not renewed
        assert after.started_at == original.started_at  # same override instance, not a new one


class TestGenuineRenewalStillDetectedThroughTheGuard:
    def test_real_manual_move_not_matching_assumed_still_renews(self) -> None:
        det = _detector_past_warmup()
        t0 = _WARMUP_NOW + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=20, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
        )
        original = det.get("w1", t0)
        assert original is not None

        # A genuine manual move to 90 — does not match any SmartShading-
        # commanded position (smartshading_assumed still reflects the old
        # override reference, e.g. 20).
        t1 = t0 + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=90, smartshading_target=0,
            smartshading_assumed=20,
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=10, duration_min=120, now=t1,
        )
        after = det.get("w1", t1)
        assert after is not None
        assert after.override_position == 90  # renewed to the new manual position
        assert after.started_at == t1  # a real renewal — started_at advances
