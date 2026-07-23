"""DST and local-timezone correctness for compute_fixed_time_expiry() and
its wiring into OverrideDetector.tick() — T7 pre-push review points 2 and 3.

All tests use real Europe/Berlin (zoneinfo), not a fixed-offset stand-in,
so DST transitions are genuine, not simulated.

Explicit, deterministic DST rule (documented in engines/override_fixed_time.py):
  - Nonexistent local time (spring-forward gap): fold=0 resolves using the
    pre-transition offset — equivalent to the configured time shifted
    forward by the gap duration once converted to an absolute instant.
  - Ambiguous local time (fall-back overlap): fold=0 always selects the
    FIRST (earlier, pre-transition-offset) occurrence.

Coverage:
  DST-01  Normal day, start before the configured time -> expiry today.
  DST-02  Normal day, start after the configured time -> expiry tomorrow.
  DST-03  Start exactly at the configured time -> expiry tomorrow
          (documented "already reached" semantics).
  DST-04  Spring-forward gap: configuring 02:30 (nonexistent) does not raise
          and resolves deterministically per the fold=0 rule.
  DST-05  Fall-back overlap: configuring 02:30 (ambiguous) does not raise
          and deterministically selects the first occurrence.
  DST-06  The result is always an aware datetime (never naive).
  DST-07  The result's UTC offset matches the actually-chosen local instant
          (not the offset `now` happened to have).
  DST-08  The result always lies strictly after `now`.
  DST-09  No TypeError/ValueError, and no naive/aware comparison error,
          across all of the above.

Additionally verifies the end-to-end OverrideDetector wiring (point 3):
  TZ-01  A non-UTC now_local produces the correctly-converted UTC
         expires_at — i.e. the local-timezone offset genuinely matters and
         is not silently dropped.
  TZ-02  coordinator.py passes local_now (not the UTC `now`) as now_local
         to OverrideDetector.tick() (source-level wiring proof).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.engines.override_fixed_time import compute_fixed_time_expiry
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.state_machine.states import ShadingState

_BERLIN = ZoneInfo("Europe/Berlin")
_UTC = timezone.utc
_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


class TestNormalDayBerlin:
    def test_before_configured_time(self) -> None:
        now = datetime(2026, 6, 15, 7, 30, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2026, 6, 15, 8, 0, tzinfo=_BERLIN)

    def test_after_configured_time(self) -> None:
        now = datetime(2026, 6, 15, 9, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2026, 6, 16, 8, 0, tzinfo=_BERLIN)

    def test_exact_match_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 6, 15, 8, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2026, 6, 16, 8, 0, tzinfo=_BERLIN)


class TestSpringForwardGap:
    """Europe/Berlin 2026-03-29: 02:00 local jumps to 03:00 local (CET+1 ->
    CEST+2). 02:00-02:59 does not exist as a local wall-clock time."""

    def test_configuring_a_nonexistent_time_does_not_raise(self) -> None:
        now = datetime(2026, 3, 29, 1, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        assert isinstance(result, datetime)

    def test_resolution_matches_documented_fold_zero_rule(self) -> None:
        """fold=0 resolves the nonexistent 02:30 using the pre-transition
        (+01:00) offset. This produces a well-defined, deterministic
        absolute instant (2026-03-29 01:30 UTC) — this is the value that
        matters functionally, since the OverrideDetector wiring converts to
        UTC via .astimezone(timezone.utc) before storing/comparing (see
        TestDetectorEndToEndWithRealTimezone below)."""
        now = datetime(2026, 3, 29, 1, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        assert result.utcoffset() == timedelta(hours=1)  # pre-transition offset (fold=0)
        assert result.astimezone(_UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=_UTC)

    def test_result_strictly_after_now(self) -> None:
        now = datetime(2026, 3, 29, 1, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        assert result > now

    def test_day_rollover_landing_on_gap_day_does_not_raise(self) -> None:
        """Start the day BEFORE the transition, already past 02:30 that day
        -> rollover to the transition day itself at 02:30 (nonexistent)."""
        now = datetime(2026, 3, 28, 3, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        assert result.date().isoformat() == "2026-03-29"
        assert result > now


class TestFallBackOverlap:
    """Europe/Berlin 2026-10-25: 03:00 CEST local falls back to 02:00 CET
    local. 02:00-02:59 occurs twice (first at +02:00, then again at +01:00)."""

    def test_configuring_an_ambiguous_time_does_not_raise(self) -> None:
        now = datetime(2026, 10, 25, 1, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        assert isinstance(result, datetime)

    def test_deterministically_selects_first_occurrence(self) -> None:
        now = datetime(2026, 10, 25, 1, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        # fold=0 (Python's default, never overridden by this module) ->
        # the FIRST 02:30, at the pre-transition +02:00 offset.
        assert result.fold == 0
        assert result.utcoffset() == timedelta(hours=2)
        assert result.astimezone(_UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=_UTC)

    def test_result_strictly_after_now(self) -> None:
        now = datetime(2026, 10, 25, 1, 0, tzinfo=_BERLIN)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        assert result > now


class TestAwareInAwareOutAndNoCrash:
    @pytest.mark.parametrize(
        "now,fixed_until",
        [
            (datetime(2026, 6, 15, 7, 30, tzinfo=_BERLIN), time(8, 0)),
            (datetime(2026, 3, 29, 1, 0, tzinfo=_BERLIN), time(2, 30)),
            (datetime(2026, 10, 25, 1, 0, tzinfo=_BERLIN), time(2, 30)),
            (datetime(2026, 12, 31, 23, 30, tzinfo=_BERLIN), time(0, 0)),
        ],
    )
    def test_result_is_aware_with_correct_offset_and_strictly_future(self, now, fixed_until) -> None:
        result = compute_fixed_time_expiry(now=now, fixed_until=fixed_until)
        assert result.tzinfo is not None
        assert result.utcoffset() is not None
        assert result > now
        # No naive/aware comparison error: this line itself would raise
        # TypeError if either side were naive.
        assert (result - now) > timedelta(0)


class TestDetectorEndToEndWithRealTimezone:
    """TZ-01: proves the local-offset conversion genuinely matters — using
    Europe/Berlin (UTC+2 in June) produces a DIFFERENT (and correct)
    expires_at than naively treating fixed_until as a UTC time would."""

    def test_non_utc_now_local_produces_correctly_converted_utc_expiry(self) -> None:
        det = OverrideDetector()
        # Warmup cycle (both now/now_local use the same UTC-vs-local pairing
        # a real coordinator cycle would produce).
        warmup_utc = datetime(2026, 6, 15, 4, 0, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=warmup_utc,
        )
        # 06:01 local (Europe/Berlin, UTC+2 in June) = 04:01 UTC.
        now_utc = datetime(2026, 6, 15, 4, 1, tzinfo=_UTC)
        now_local = now_utc.astimezone(_BERLIN)
        assert now_local.hour == 6  # sanity: genuinely a different wall-clock hour than UTC

        det.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now_utc,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0), now_local=now_local,
        )
        override = det.get("w1", now_utc)
        assert override is not None
        # 08:00 Europe/Berlin (UTC+2 in June) == 06:00 UTC — NOT 08:00 UTC,
        # which is what a naive (UTC-based) computation would have produced.
        assert override.expires_at == datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)
        assert override.expires_at != datetime(2026, 6, 15, 8, 0, tzinfo=_UTC)

    def test_missing_now_local_falls_back_to_legacy_duration_not_a_crash(self) -> None:
        """If duration_mode='fixed_time' but now_local is not supplied
        (defensive fallback — see tick()'s docstring), the detector must
        not crash; it falls back to the legacy duration_min computation."""
        det = OverrideDetector()
        warmup = datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)
        det.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=warmup,
        )
        t0 = warmup + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=time(8, 0),  # now_local omitted
        )
        override = det.get("w1", t0)
        assert override is not None
        assert override.expires_at == t0 + timedelta(minutes=120)  # legacy fallback, not a crash


class TestCoordinatorWiresLocalTimeNotUtc:
    """TZ-02: source-level proof that coordinator.py passes local_now (HA's
    configured timezone), not the UTC `now`, as now_local."""

    def test_tick_call_site_passes_local_now_as_now_local(self) -> None:
        source = (_INTEGRATION_ROOT / "coordinator.py").read_text(encoding="utf-8")
        assert "now_local=local_now," in source
        # The plain `now` used everywhere else in this method is UTC
        # (dt_util.utcnow()) — confirm local_now is a distinct, explicitly
        # localized variable, not an alias for it.
        assert "local_now = dt_util.as_local(now)" in source
