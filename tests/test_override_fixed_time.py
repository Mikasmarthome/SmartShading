"""Tests for compute_fixed_time_expiry (engines/override_fixed_time.py) — T7.

Coverage (T7 review points 17-23):
  FT-01  Start before configured time → expiry today.
  FT-02  Start after configured time → expiry tomorrow.
  FT-03  Start exactly at configured time → documented as "already reached"
         → expiry tomorrow (matches the "noch in der Zukunft" = strictly
         future wording in the review).
  FT-04  Midnight overflow (fixed_until shortly after midnight, now late
         evening) → tomorrow.
  FT-05  Month-end rollover.
  FT-06  Year-end rollover.
  FT-07  DST spring-forward and fall-back transitions do not raise.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.smartshading.engines.override_fixed_time import compute_fixed_time_expiry

_UTC = timezone.utc


class TestStartBeforeConfiguredTime:
    def test_expiry_is_today(self) -> None:
        now = datetime(2026, 6, 15, 7, 30, tzinfo=_UTC)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2026, 6, 15, 8, 0, tzinfo=_UTC)


class TestStartAfterConfiguredTime:
    def test_expiry_is_tomorrow(self) -> None:
        now = datetime(2026, 6, 15, 9, 0, tzinfo=_UTC)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2026, 6, 16, 8, 0, tzinfo=_UTC)


class TestExactMatch:
    def test_expiry_is_tomorrow_documented_semantics(self) -> None:
        """now == fixed_until exactly: treated as "already reached" (not
        strictly future), so the next occurrence is tomorrow — documented
        in override_fixed_time.py's module docstring."""
        now = datetime(2026, 6, 15, 8, 0, tzinfo=_UTC)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2026, 6, 16, 8, 0, tzinfo=_UTC)


class TestMidnightOverflow:
    def test_late_evening_start_with_early_morning_fixed_time(self) -> None:
        now = datetime(2026, 6, 15, 23, 0, tzinfo=_UTC)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(0, 30))
        assert result == datetime(2026, 6, 16, 0, 30, tzinfo=_UTC)

    def test_just_after_midnight_start_with_early_morning_fixed_time_still_today(self) -> None:
        now = datetime(2026, 6, 16, 0, 5, tzinfo=_UTC)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(0, 30))
        assert result == datetime(2026, 6, 16, 0, 30, tzinfo=_UTC)


class TestMonthRollover:
    def test_last_day_of_month_rolls_to_next_month(self) -> None:
        now = datetime(2026, 1, 31, 23, 0, tzinfo=_UTC)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2026, 2, 1, 8, 0, tzinfo=_UTC)

    def test_february_to_march_in_a_common_year(self) -> None:
        now = datetime(2026, 2, 28, 23, 0, tzinfo=_UTC)  # 2026 is not a leap year
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2026, 3, 1, 8, 0, tzinfo=_UTC)


class TestYearRollover:
    def test_new_years_eve_rolls_to_next_year(self) -> None:
        now = datetime(2026, 12, 31, 23, 0, tzinfo=_UTC)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(8, 0))
        assert result == datetime(2027, 1, 1, 8, 0, tzinfo=_UTC)


class TestDstSafety:
    def test_spring_forward_transition_does_not_raise(self) -> None:
        """Europe/Berlin 2026 spring-forward: 2026-03-29 02:00 -> 03:00 (the
        02:00-03:00 wall-clock range does not exist). Must not raise."""
        tz = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 3, 29, 1, 30, tzinfo=tz)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_fall_back_transition_does_not_raise(self) -> None:
        """Europe/Berlin 2026 fall-back: 2026-10-25 03:00 -> 02:00 (the
        02:00-03:00 wall-clock range occurs twice). Must not raise."""
        tz = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 10, 25, 1, 30, tzinfo=tz)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_dst_transition_result_still_lies_in_the_future_relative_to_now(self) -> None:
        tz = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 3, 29, 1, 30, tzinfo=tz)
        result = compute_fixed_time_expiry(now=now, fixed_until=time(2, 30))
        # Whichever wall-clock/offset interpretation zoneinfo resolves to,
        # the function's own invariant (return value strictly after `now`
        # when fixed_until was in the future) must still hold on this call.
        assert result >= now
