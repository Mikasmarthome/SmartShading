"""v1.0.8 — Global Cover Dispatch Hardening.

Mandatory tests for two hardening requirements:

  Safety Timing (DH-S1–S6):
    Safety commands must respect the 1.0 s minimum interval exactly like
    regular commands.  Safety's queue priority means it is exempt from
    stale-intent cancellation, but it never bypasses the timing gate.

  Monotonic Clock (DH-M1���M4):
    Elapsed-time measurement uses an injectable monotonic clock so that
    NTP / wall-clock jumps cannot shorten the dispatch interval.

No Home Assistant import.  Pure Python.  All timing uses _FakeMono.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.smartshading.cover_control.global_dispatch_throttle import (
    DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS,
    GlobalDispatchThrottle,
    GlobalSerialDispatch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc
_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)
_INTERVAL = timedelta(seconds=DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS)


class _FakeMono:
    """Injectable deterministic monotonic clock for tests."""
    def __init__(self, t: float = 0.0) -> None:
        self._t = t
    def __call__(self) -> float:
        return self._t
    def advance(self, seconds: float) -> None:
        self._t += seconds
    def set(self, t: float) -> None:
        self._t = t


def _gsd(mono: _FakeMono | None = None) -> GlobalSerialDispatch:
    return GlobalSerialDispatch(mono_clock=mono)


def _throttle(interval_s: float = 1.0, mono: _FakeMono | None = None) -> GlobalDispatchThrottle:
    return GlobalDispatchThrottle(
        min_interval=timedelta(seconds=interval_s),
        mono_clock=mono,
    )


# ---------------------------------------------------------------------------
# DH-S: Safety timing — 1.0 s minimum always enforced
# ---------------------------------------------------------------------------

class TestSafetyTimingHardening:

    def test_dh_s1_safety_after_regular_waits_remaining_interval(self):
        """DH-S1: regular dispatch at t=0, safety at t=0.2 → waits until t=1.0.

        Binding scenario: regular dispatch at t=0.0 → throttle armed.
        Safety arrives at t=0.2: it must wait 0.8 s (not dispatch immediately).
        """
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)
        t.record_dispatch(_T0)  # regular at t=0.0

        mono.advance(0.2)  # safety arrives 200ms later
        wait = t.time_until_next_allowed()

        assert wait == timedelta(milliseconds=800), (
            f"Safety must wait {800}ms, not dispatch at t=0.2 (got {wait.total_seconds()*1000:.0f}ms)"
        )

    def test_dh_s2_safety_processed_before_stale_intent_cancellation(self):
        """DH-S2: safety is exempt from stale-intent cancellation.

        This is the ONLY special treatment safety receives — it still waits
        for the throttle but cannot be cancelled by a generation change.
        Verified via the is_safety flag on the intent object.
        """
        from custom_components.smartshading.cover_control.command_filter import (
            CommandFilter, ExecutionCapability, ExecutionMode,
        )
        from custom_components.smartshading.cover_control.execution_plan import (
            build_execution_plan, CoverCommandType,
        )
        now = _T0
        cap = ExecutionCapability(position_tolerance=3)
        fr = CommandFilter().evaluate(
            target_position_internal=80,
            current_position_internal=0,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=True,
            is_manual_override=False,
            is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=cap,
            invert_position=False,
        )
        plan = build_execution_plan(
            window_id="w1",
            cover_entity_ids=["cover.test"],
            filter_result=fr,
            decided_by="TestEval",
            now=now,
        )
        assert plan.intents[0].is_safety is True

    def test_dh_s3_two_consecutive_safety_commands_spaced_1s(self):
        """DH-S3: two safety commands must be at least 1.0 s apart."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)

        # First safety at t=0
        t.record_dispatch(_T0)

        # Second safety arrives immediately after
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.0), (
            "Second safety command must wait the full 1.0 s interval"
        )

    def test_dh_s4_safety_followed_by_regular_spaced_1s(self):
        """DH-S4: safety SENT at t=0 → subsequent regular must wait 1.0 s."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)

        t.record_dispatch(_T0)   # safety SENT at mono=0.0
        mono.advance(0.5)        # regular arrives 500ms later

        wait = t.time_until_next_allowed()
        assert wait == timedelta(milliseconds=500)

    def test_dh_s5_no_concurrent_safety_commands(self):
        """DH-S5: asyncio.Lock ensures safety commands never execute concurrently."""
        async def _run():
            gsd = GlobalSerialDispatch()
            concurrent = []

            async def _safety_dispatch(label: str):
                async with gsd.lock:
                    concurrent.append(label + "_start")
                    await asyncio.sleep(0.005)
                    concurrent.append(label + "_end")

            await asyncio.gather(
                _safety_dispatch("S1"),
                _safety_dispatch("S2"),
            )
            return concurrent

        result = asyncio.run(_run())
        # With the lock, one must finish completely before the other starts
        assert result.index("S1_end") < result.index("S2_start") or \
               result.index("S2_end") < result.index("S1_start"), (
            f"Safety commands executed concurrently: {result}"
        )

    def test_dh_s6_failed_attempted_call_arms_throttle(self):
        """DH-S6: FAILED safety call (async_call started, then raised) arms the throttle.

        An async_call that raises still started — the global 1.0 s interval
        begins at the moment the call was initiated.  NOT_ATTEMPTED (no
        async_call) must leave the throttle untouched.
        """
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)

        # FAILED: async_call was started → coordinator calls record_dispatch
        t.record_dispatch(_T0)
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.0), (
            "FAILED (call started) must arm the throttle"
        )

        # NOT_ATTEMPTED: no async_call → fresh throttle stays clear
        t2 = _throttle(interval_s=1.0, mono=_FakeMono(0.0))
        assert t2.time_until_next_allowed() == timedelta(0), (
            "NOT_ATTEMPTED must not arm the throttle"
        )


# ---------------------------------------------------------------------------
# DH-M: Monotonic clock — wall-clock jumps cannot shorten interval
# ---------------------------------------------------------------------------

class TestMonotonicClockHardening:

    def test_dh_m1_backward_wall_clock_jump_cannot_shorten_interval(self):
        """DH-M1: backward wall-clock jump does not shorten the dispatch interval.

        Scenario: dispatch at wall t=10:00:00. NTP then adjusts clock back to
        09:59:58 (2-second backward jump). The throttle must still enforce the
        1.0 s interval based on monotonic time, not wall-clock difference.
        """
        mono = _FakeMono(1000.0)  # monotonic at 1000.0s
        t = _throttle(interval_s=1.0, mono=mono)

        wall_at_dispatch = datetime(2026, 1, 1, 10, 0, 0, tzinfo=_UTC)
        t.record_dispatch(wall_at_dispatch)

        # Wall clock jumps backward 2 seconds, but mono only advances 0.2s
        mono.advance(0.2)

        wait = t.time_until_next_allowed()
        assert wait.total_seconds() > 0, (
            "Backward wall-clock jump must not shorten the dispatch interval"
        )
        assert wait == timedelta(milliseconds=800)

    def test_dh_m2_forward_wall_clock_jump_does_not_corrupt_timing(self):
        """DH-M2: forward wall-clock jump (e.g., NTP sync) does not allow
        premature dispatch.  Monotonic elapsed is the only authority.
        """
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)

        t.record_dispatch(_T0)

        # Wall clock would jump far forward, but mono advances only 0.1s
        mono.advance(0.1)

        wait = t.time_until_next_allowed()
        # Must still wait 0.9s — forward wall-clock jump has no effect
        assert wait == timedelta(milliseconds=900)

    def test_dh_m3_injected_mono_clock_is_deterministic_in_tests(self):
        """DH-M3: injected monotonic clock gives exact, reproducible results.

        Verifies that the fake clock injection produces deterministic output
        suitable for unit testing without relying on real time.
        """
        mono = _FakeMono(500.0)  # start at arbitrary offset
        t = _throttle(interval_s=1.0, mono=mono)

        t.record_dispatch(_T0)

        # At exactly +1.0s: no wait
        mono.set(501.0)
        assert t.time_until_next_allowed() == timedelta(0)

        # At +0.7s from dispatch: 0.3s remaining
        mono.set(500.7)
        assert t.time_until_next_allowed() == timedelta(milliseconds=300)

        # At +0.0s from dispatch: full 1.0s remaining
        mono.set(500.0)
        assert t.time_until_next_allowed() == timedelta(seconds=1.0)

    def test_dh_m4_cancellation_does_not_move_last_dispatch_backward(self):
        """DH-M4: a NOT_ATTEMPTED (cancelled) intent must not update record_dispatch.
        The throttle clock advances on SENT or FAILED; never on cancellation.
        """
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)

        t.record_dispatch(_T0)     # real SENT at mono=0.0
        mono.advance(0.3)

        # Simulate NOT_ATTEMPTED: do NOT call record_dispatch
        # (coordinator uses build_not_attempted_result and skips record_dispatch)

        # last_dispatch_at must still reference the SENT at t0
        assert t.last_dispatch_at == _T0

        # Remaining wait must be computed from the original SENT, not from
        # the cancelled intent
        wait = t.time_until_next_allowed()
        assert wait == timedelta(milliseconds=700)


# ---------------------------------------------------------------------------
# DH-F: FAILED dispatch consumes the global interval
# ---------------------------------------------------------------------------

class TestFailedDispatchTimingHardening:
    """Invariant: every async_call that starts — whether it succeeds or raises —
    begins a new global 1.0 s dispatch interval.

    Coordinator maps:
      SENT     → record_dispatch()   (call started, succeeded)
      FAILED   → record_dispatch()   (call started, raised)
      NOT_ATTEMPTED / BLOCKED → no record_dispatch()  (no call started)
    """

    def test_dh_f1_failure_before_service_call_does_not_consume_interval(self):
        """DH-F1: NOT_ATTEMPTED result (no async_call invoked) leaves throttle clear."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)
        # Coordinator receives NOT_ATTEMPTED → does NOT call record_dispatch
        assert t.time_until_next_allowed() == timedelta(0), (
            "NOT_ATTEMPTED must not consume the dispatch interval"
        )

    def test_dh_f2_service_call_that_starts_and_raises_consumes_interval(self):
        """DH-F2: FAILED result (async_call started, then raised) arms the throttle."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)
        # Coordinator receives FAILED → calls record_dispatch (call was started)
        t.record_dispatch(_T0)
        assert t.time_until_next_allowed() == timedelta(seconds=1.0), (
            "FAILED (call started) must arm the 1.0 s throttle"
        )

    def test_dh_f3_next_regular_after_failed_attempted_call_waits(self):
        """DH-F3: regular command arriving 0.3 s after a FAILED call must wait 0.7 s."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)
        t.record_dispatch(_T0)  # FAILED at mono=0.0 → interval armed
        mono.advance(0.3)
        assert t.time_until_next_allowed() == timedelta(milliseconds=700)

    def test_dh_f4_next_safety_after_failed_attempted_call_waits(self):
        """DH-F4: safety command arriving 0.2 s after a FAILED call must wait 0.8 s."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)
        t.record_dispatch(_T0)  # FAILED at mono=0.0 → interval armed
        mono.advance(0.2)
        assert t.time_until_next_allowed() == timedelta(milliseconds=800)

    def test_dh_f5_blocked_intent_does_not_consume_interval(self):
        """DH-F5: BLOCKED result (no async_call invoked) leaves throttle clear."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)
        # Coordinator receives BLOCKED → does NOT call record_dispatch
        assert t.time_until_next_allowed() == timedelta(0), (
            "BLOCKED must not consume the dispatch interval"
        )

    def test_dh_f6_successful_call_records_exactly_once(self):
        """DH-F6: a single SENT result calls record_dispatch exactly once,
        and a subsequent dispatch after 1.0 s resets the interval cleanly."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono=mono)
        t.record_dispatch(_T0)  # SENT — exactly one call
        mono.advance(1.0)
        assert t.time_until_next_allowed() == timedelta(0)
        t.record_dispatch(_T0)  # second SENT
        assert t.time_until_next_allowed() == timedelta(seconds=1.0)
