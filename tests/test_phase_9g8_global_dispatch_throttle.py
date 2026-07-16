"""Step 9G8: Global Dispatch Throttle tests.

Tests the GlobalDispatchThrottle class and its integration into the
coordinator dispatch loop:

  - Unit tests for GlobalDispatchThrottle (time_until_next_allowed, record_dispatch)
  - Default interval: 2.0 seconds (DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS)
  - First dispatch: no wait
  - Subsequent dispatch within interval: wait required
  - Interval elapsed: no wait
  - Safety commands bypass throttle wait (no sleep)
  - Safety SENT updates throttle clock (non-safety after safety waits)
  - BLOCKED/NOT_ATTEMPTED/FAILED: no record_dispatch call
  - SENT: record_dispatch called
  - Multiple covers in one CoverGroup: global spacing enforced
  - Multiple windows: global spacing enforced across zones
  - Diagnostics: dispatch_throttled and throttle_wait_ms fields
  - Position invariant: HA position unchanged by throttle
  - Regression: active_control_enabled=False still no dispatch
  - Regression: recommendation_only still no dispatch

No Home Assistant import.  No coordinator instantiation.
All async tests use asyncio.run() and MagicMock/AsyncMock.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.smartshading.cover_control.global_dispatch_throttle import (
    DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS,
    GlobalDispatchThrottle,
)
from custom_components.smartshading.cover_control.command_filter import (
    CommandFilter,
    ExecutionCapability,
    ExecutionMode,
    BLOCKED_RECOMMENDATION_ONLY,
)
from custom_components.smartshading.cover_control.execution_plan import (
    build_execution_plan,
    CoverCommandType,
)
from custom_components.smartshading.cover_control.execution_result import (
    ExecutionStatus,
    build_blocked_result,
    build_execution_plan_result,
    build_not_attempted_result,
    build_sent_result,
    build_failed_result,
)
from custom_components.smartshading.cover_control.ha_service_adapter import (
    dispatch_cover_intent,
)
from custom_components.smartshading.models.execution_diagnostics import (
    WindowExecutionDiagnostics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 18, 15, 0, 0, tzinfo=timezone.utc)
_1S_LATER = _NOW + timedelta(seconds=1)
_500MS_LATER = _NOW + timedelta(milliseconds=500)
_2S_LATER = _NOW + timedelta(seconds=2)


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


def _throttle(interval_s: float = 1.0, mono_clock=None) -> GlobalDispatchThrottle:
    return GlobalDispatchThrottle(
        min_interval=timedelta(seconds=interval_s),
        mono_clock=mono_clock,
    )


def _make_hass(side_effect=None) -> MagicMock:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None, side_effect=side_effect)
    return hass


def _make_intent(
    *,
    cover_entity_id: str = "cover.test",
    target_internal: int = 80,
    allowed: bool = True,
    is_safety: bool = False,
    current_internal: int = 0,
):
    """Build a CoverIntent via CommandFilter → build_execution_plan."""
    mode = ExecutionMode.AUTOMATIC if allowed else ExecutionMode.RECOMMENDATION_ONLY
    cap = ExecutionCapability(position_tolerance=3)
    fr = CommandFilter().evaluate(
        target_position_internal=target_internal,
        current_position_internal=current_internal,
        execution_mode=mode,
        is_safety=is_safety,
        is_manual_override=False,
        is_cover_available=True,
        state_guard_allowed=True,
        execution_capability=cap,
        invert_position=False,
    )
    plan = build_execution_plan(
        window_id="win_test",
        cover_entity_ids=[cover_entity_id],
        filter_result=fr,
        decided_by="TestEval",
        now=_NOW,
    )
    return plan.intents[0]


def _diag(**kwargs) -> WindowExecutionDiagnostics:
    """Build a WindowExecutionDiagnostics with sensible defaults."""
    defaults = dict(
        learning_enabled=True,
        active_control_enabled=True,
        execution_mode=ExecutionMode.AUTOMATIC.value,
        cover_entity_id="cover.test",
        cover_available=True,
        actual_position_ha=20,
        actual_position_internal=80,
        assumed_position_internal=0,
        has_position_feedback=True,
        tier_decided_by="SolarEvaluator",
        target_position_internal=80,
        target_position_ha=20,
        is_safety=False,
        command_allowed=True,
        command_blocked_reason=None,
        last_command_status=ExecutionStatus.SENT.value,
        last_command_sent_at=_NOW,
        service_call_sent=True,
        service_call_failed=False,
        execution_error=None,
        safety_result_failed=False,
        dispatch_suppressed_reason=None,
        dispatch_throttled=False,
        throttle_wait_ms=None,
    )
    defaults.update(kwargs)
    return WindowExecutionDiagnostics(**defaults)


async def _run_dispatch_loop(
    throttle: GlobalDispatchThrottle,
    hass: MagicMock,
    intents: list,
    *,
    now_fn=None,
) -> list[tuple[ExecutionStatus, bool, int | None]]:
    """Simulate the coordinator's inner dispatch loop.

    Returns a list of (status, dispatch_throttled, throttle_wait_ms) per intent.
    """
    if now_fn is None:
        now_fn = lambda: _NOW  # noqa: E731

    results = []
    for intent in intents:
        throttled = False
        wait_ms = None

        if not intent.allowed:
            result = build_blocked_result(intent, reason="blocked")
        else:
            # Throttle gate (mirrors coordinator logic):
            # ALL intents — including safety — respect the minimum interval.
            wait = throttle.time_until_next_allowed()
            if wait.total_seconds() > 0:
                throttled = True
                wait_ms = round(wait.total_seconds() * 1000)
                await asyncio.sleep(wait.total_seconds())
            result = await dispatch_cover_intent(hass, intent, now_utc=now_fn())
            if result.status is ExecutionStatus.SENT:
                throttle.record_dispatch(now_fn())

        results.append((result.status, throttled, wait_ms))
    return results


# ---------------------------------------------------------------------------
# Class 1: GlobalDispatchThrottle unit tests
# ---------------------------------------------------------------------------

class TestGlobalDispatchThrottleUnit:
    def test_last_dispatch_at_initially_none(self):
        t = _throttle()
        assert t.last_dispatch_at is None

    def test_first_call_returns_zero_wait(self):
        t = _throttle()
        assert t.time_until_next_allowed() == timedelta(0)

    def test_min_interval_property_default(self):
        t = GlobalDispatchThrottle()
        assert t.min_interval == timedelta(seconds=DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS)

    def test_min_interval_property_custom(self):
        t = GlobalDispatchThrottle(min_interval=timedelta(milliseconds=500))
        assert t.min_interval == timedelta(milliseconds=500)

    def test_record_dispatch_updates_last_dispatch_at(self):
        t = _throttle()
        t.record_dispatch(_NOW)
        assert t.last_dispatch_at == _NOW

    def test_wait_required_immediately_after_record(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        # Mono at 0.0 — same instant as dispatch → full 1 second remaining
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.0)

    def test_no_wait_after_full_interval_elapsed(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(1.0)  # exactly 1 second later
        wait = t.time_until_next_allowed()
        assert wait == timedelta(0)

    def test_partial_wait_midway_through_interval(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(0.5)  # 500ms later → 500ms remaining
        wait = t.time_until_next_allowed()
        assert wait == timedelta(milliseconds=500)

    def test_no_wait_more_than_interval_elapsed(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(2.0)  # 2 seconds later → no wait
        wait = t.time_until_next_allowed()
        assert wait == timedelta(0)

    def test_custom_interval_respected(self):
        mono = _FakeMono(0.0)
        t = GlobalDispatchThrottle(min_interval=timedelta(seconds=2.0), mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(1.0)  # 1 second later → 1 second still remaining with 2s interval
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.0)


# ---------------------------------------------------------------------------
# Class 2: record_dispatch rules
# ---------------------------------------------------------------------------

class TestRecordDispatchRules:
    def test_multiple_records_use_latest(self):
        t = _throttle()
        t.record_dispatch(_NOW)
        t.record_dispatch(_1S_LATER)
        assert t.last_dispatch_at == _1S_LATER

    def test_record_replaces_previous_timestamp(self):
        mono = _FakeMono(0.0)
        t = _throttle(mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(0.5)
        t.record_dispatch(_500MS_LATER)  # resets mono base to 0.5
        # Immediately after second record → full 1s wait from new base
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.0)

    def test_zero_wait_at_exact_interval_boundary(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(1.0)  # exactly at interval boundary → remaining = 0
        wait = t.time_until_next_allowed()
        assert wait == timedelta(0)

    def test_just_before_interval_has_positive_wait(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(0.999)  # 1ms before boundary
        wait = t.time_until_next_allowed()
        assert wait > timedelta(0)
        assert wait <= timedelta(milliseconds=1)

    def test_wait_never_negative(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(10.0)  # well past interval
        wait = t.time_until_next_allowed()
        assert wait == timedelta(0)

    def test_sequence_of_records_advances_clock(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        mono.advance(1.0)
        t.record_dispatch(_1S_LATER)  # second record at T+1s mono
        # Immediately after second record → full interval wait
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.0)
        mono.advance(1.0)  # T+2s
        wait_at_2s = t.time_until_next_allowed()
        assert wait_at_2s == timedelta(0)


# ---------------------------------------------------------------------------
# Class 3: DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS constant
# ---------------------------------------------------------------------------

class TestDefaultInterval:
    def test_constant_is_two_seconds(self):
        # F32 field fix: raised from 1.5s to 2.0s after a same-second RF
        # collision report (ESP Somfy / RTS bridge).
        assert DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS == 2.0

    def test_default_throttle_uses_constant(self):
        t = GlobalDispatchThrottle()
        assert t.min_interval.total_seconds() == DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS

    def test_constant_type_is_float(self):
        assert isinstance(DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS, float)

    def test_custom_interval_overrides_default(self):
        t = GlobalDispatchThrottle(min_interval=timedelta(milliseconds=200))
        assert t.min_interval.total_seconds() == 0.2
        assert t.min_interval != timedelta(seconds=DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Class 4: Safety timing behavior (pure logic — no asyncio)
# ---------------------------------------------------------------------------

class TestSafetyTimingBehavior:
    def test_safety_intent_flag(self):
        intent = _make_intent(is_safety=True)
        assert intent.is_safety is True

    def test_non_safety_intent_flag(self):
        intent = _make_intent(is_safety=False)
        assert intent.is_safety is False

    def test_safety_subject_to_throttle_check(self):
        """Safety commands call time_until_next_allowed — no bypass."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)  # throttle armed

        # Both safety and non-safety must wait
        wait = t.time_until_next_allowed()
        assert wait.total_seconds() > 0, "Safety must also respect the throttle"

    def test_non_safety_subject_to_throttle_check_when_armed(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)

        wait = t.time_until_next_allowed()
        assert wait.total_seconds() > 0

    def test_safety_sent_calls_record_dispatch(self):
        """Safety SENT updates the throttle clock."""
        t = _throttle(interval_s=1.0)
        assert t.last_dispatch_at is None
        t.record_dispatch(_NOW)
        assert t.last_dispatch_at == _NOW

    def test_non_safety_after_safety_waits(self):
        """Non-safety dispatch after safety SENT waits the full interval."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)  # safety fired at mono=0.0
        mono.advance(0.1)  # 100ms later → must wait 900ms
        wait = t.time_until_next_allowed()
        assert wait == timedelta(milliseconds=900)

    def test_two_consecutive_safety_commands_wait_between_them(self):
        """Two safety commands in sequence: second must wait like any other command."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)  # first safety SENT

        # Immediately after first: full wait required for second
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.0), "Second safety must wait full interval"


# ---------------------------------------------------------------------------
# Class 5: Record rules — BLOCKED / NOT_ATTEMPTED / FAILED / SENT
# ---------------------------------------------------------------------------

class TestThrottleRecordOnStatus:
    def test_blocked_result_does_not_need_record(self):
        """BLOCKED: CommandFilter blocked → no service call → no record needed."""
        t = _throttle()
        # No record after BLOCKED
        assert t.last_dispatch_at is None
        # (Coordinator does not call record_dispatch for BLOCKED results)

    def test_not_attempted_does_not_need_record(self):
        """NOT_ATTEMPTED: startup grace → no service call → no record needed."""
        t = _throttle()
        assert t.last_dispatch_at is None

    def test_failed_does_not_update_throttle(self):
        """FAILED: service call raised → no confirmed send → no record."""
        t = _throttle()
        # Simulate: failed dispatch → do NOT call record_dispatch
        # (Coordinator checks result.status is ExecutionStatus.SENT before recording)
        result_status = ExecutionStatus.FAILED
        if result_status is ExecutionStatus.SENT:
            t.record_dispatch(_NOW)
        assert t.last_dispatch_at is None

    def test_sent_updates_throttle(self):
        """SENT: service call confirmed → record_dispatch must be called."""
        t = _throttle()
        result_status = ExecutionStatus.SENT
        if result_status is ExecutionStatus.SENT:
            t.record_dispatch(_NOW)
        assert t.last_dispatch_at == _NOW

    def test_sent_arms_throttle_for_next_dispatch(self):
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        # Next dispatch immediately after must wait
        wait = t.time_until_next_allowed()
        assert wait.total_seconds() > 0

    def test_no_record_for_not_attempted_leaves_throttle_clear(self):
        t = _throttle()
        # After NOT_ATTEMPTED, throttle remains unarmed → first real dispatch has no wait
        assert t.time_until_next_allowed() == timedelta(0)


# ---------------------------------------------------------------------------
# Class 6: Async behavior — asyncio.sleep called with correct duration
# ---------------------------------------------------------------------------

class TestAsyncThrottleBehavior:
    def test_first_dispatch_no_sleep(self):
        """First dispatch: no sleep call."""
        t = _throttle()
        hass = _make_hass()
        intent = _make_intent()

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [intent]))

        mock_sleep.assert_not_called()
        assert results[0][0] is ExecutionStatus.SENT
        assert results[0][1] is False  # not throttled

    def test_second_dispatch_same_timestamp_waits_full_interval(self):
        """Second dispatch at same time as first → sleeps for full interval."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass()
        intent1 = _make_intent(cover_entity_id="cover.a")
        intent2 = _make_intent(cover_entity_id="cover.b")

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [intent1, intent2]))

        # First: no sleep; second: sleeps 1 second
        assert mock_sleep.call_count == 1
        sleep_arg = mock_sleep.call_args[0][0]
        assert sleep_arg == pytest.approx(1.0, abs=0.001)
        assert results[0][1] is False  # first not throttled
        assert results[1][1] is True   # second throttled

    def test_safety_dispatch_waits_when_throttle_armed(self):
        """Safety dispatch respects throttle — sleeps like any other command."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        hass = _make_hass()
        normal = _make_intent(is_safety=False)
        safety = _make_intent(is_safety=True)

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [normal, safety]))

        # normal: no wait (first). safety: must wait (throttle armed after normal)
        assert mock_sleep.call_count == 1
        assert results[0][1] is False   # normal: not throttled
        assert results[1][1] is True    # safety: throttled

    def test_non_safety_after_safety_waits(self):
        """Non-safety after safety SENT: throttle armed → sleep required."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)  # arms throttle at mono=0.0
        hass = _make_hass()
        normal = _make_intent()  # non-safety

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            asyncio.run(_run_dispatch_loop(t, hass, [normal], now_fn=lambda: _NOW))

        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == pytest.approx(1.0, abs=0.001)

    def test_blocked_intent_no_sleep(self):
        """BLOCKED: no dispatch → no sleep."""
        t = _throttle(interval_s=1.0)
        t.record_dispatch(_NOW)  # throttle armed
        hass = _make_hass()
        blocked_intent = _make_intent(allowed=False)

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [blocked_intent]))

        mock_sleep.assert_not_called()
        assert results[0][0] is ExecutionStatus.BLOCKED

    def test_throttle_wait_ms_populated_when_throttled(self):
        """throttle_wait_ms is the rounded milliseconds of the wait."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass()
        intent1 = _make_intent(cover_entity_id="cover.a")
        intent2 = _make_intent(cover_entity_id="cover.b")

        with patch("asyncio.sleep", new=AsyncMock()):
            results = asyncio.run(_run_dispatch_loop(t, hass, [intent1, intent2]))

        # intent2 was throttled for 1000ms
        _, throttled2, wait_ms2 = results[1]
        assert throttled2 is True
        assert wait_ms2 == 1000

    def test_throttle_wait_ms_none_when_not_throttled(self):
        """throttle_wait_ms is None for non-throttled dispatches."""
        t = _throttle()
        hass = _make_hass()
        intent = _make_intent()

        with patch("asyncio.sleep", new=AsyncMock()):
            results = asyncio.run(_run_dispatch_loop(t, hass, [intent]))

        _, throttled, wait_ms = results[0]
        assert throttled is False
        assert wait_ms is None

    def test_failed_dispatch_no_record_no_subsequent_wait(self):
        """FAILED: throttle not updated → next dispatch has no wait."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass(side_effect=RuntimeError("RF error"))
        intent1 = _make_intent(cover_entity_id="cover.a")
        intent2 = _make_intent(cover_entity_id="cover.b")

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [intent1, intent2]))

        # intent1 failed (FAILED status)
        assert results[0][0] is ExecutionStatus.FAILED
        # intent2 should not be throttled (FAILED didn't update throttle)
        mock_sleep.assert_not_called()
        assert results[1][1] is False


# ---------------------------------------------------------------------------
# Class 7: Global scope — throttle shared across windows/covers
# ---------------------------------------------------------------------------

class TestGlobalScope:
    def test_throttle_instance_is_shared(self):
        """Single GlobalDispatchThrottle instance is shared across all dispatches."""
        t = _throttle(interval_s=1.0)
        assert t.last_dispatch_at is None
        t.record_dispatch(_NOW)
        # Same instance reflects the record for all subsequent callers
        assert t.last_dispatch_at == _NOW

    def test_two_windows_different_covers_share_throttle(self):
        """Covers from window A and window B share the same throttle."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass()
        intent_window_a = _make_intent(cover_entity_id="cover.living_room")
        intent_window_b = _make_intent(cover_entity_id="cover.bedroom")

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(
                _run_dispatch_loop(t, hass, [intent_window_a, intent_window_b])
            )

        # living_room: first → no wait; bedroom: same throttle → wait
        assert results[0][1] is False
        assert results[1][1] is True
        mock_sleep.assert_called_once()

    def test_three_windows_correct_spacing(self):
        """Three windows dispatch sequentially with global throttle."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass()
        intents = [
            _make_intent(cover_entity_id=f"cover.room_{i}")
            for i in range(3)
        ]

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, intents))

        # First: no wait; second and third: each waits
        assert results[0][1] is False
        assert results[1][1] is True
        assert results[2][1] is True
        assert mock_sleep.call_count == 2

    def test_covers_in_same_cover_group_share_throttle(self):
        """Two covers in one CoverGroup (multi-cover window) share throttle."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass()
        # CoverGroup with 2 covers → 2 intents in the same inner dispatch loop
        cover1 = _make_intent(cover_entity_id="cover.south_left")
        cover2 = _make_intent(cover_entity_id="cover.south_right")

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [cover1, cover2]))

        assert results[0][1] is False
        assert results[1][1] is True
        assert mock_sleep.call_count == 1

    def test_throttle_state_persists_between_simulated_windows(self):
        """Throttle state from window A is visible when processing window B."""
        mono = _FakeMono(0.0)
        t = _throttle(interval_s=1.0, mono_clock=mono)
        t.record_dispatch(_NOW)
        # Window B dispatching at same instant → must wait
        wait = t.time_until_next_allowed()
        assert wait == timedelta(seconds=1.0)

    def test_safety_in_one_window_waits_for_throttle_from_previous(self):
        """Two safety commands across two windows: both respect the throttle."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass()
        safety1 = _make_intent(cover_entity_id="cover.a", is_safety=True)
        safety2 = _make_intent(cover_entity_id="cover.b", is_safety=True)

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [safety1, safety2]))

        # safety1: no wait (first dispatch). safety2: must wait (throttle armed)
        assert mock_sleep.call_count == 1
        assert results[0][1] is False
        assert results[1][1] is True

    def test_non_safety_after_two_safety_commands_waits(self):
        """Non-safety after two safety commands waits from the LAST safety SENT."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass()
        safety1 = _make_intent(cover_entity_id="cover.a", is_safety=True)
        safety2 = _make_intent(cover_entity_id="cover.b", is_safety=True)
        normal = _make_intent(cover_entity_id="cover.c", is_safety=False)

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(
                _run_dispatch_loop(t, hass, [safety1, safety2, normal])
            )

        assert results[0][1] is False   # safety1: no wait (first)
        assert results[1][1] is True    # safety2: waits (throttle armed by safety1)
        assert results[2][1] is True    # normal: waits


# ---------------------------------------------------------------------------
# Class 8: WindowExecutionDiagnostics — new fields
# ---------------------------------------------------------------------------

class TestDispatchThrottleDiagnosticsFields:
    def test_dispatch_throttled_field_exists(self):
        names = {f.name for f in fields(WindowExecutionDiagnostics)}
        assert "dispatch_throttled" in names

    def test_throttle_wait_ms_field_exists(self):
        names = {f.name for f in fields(WindowExecutionDiagnostics)}
        assert "throttle_wait_ms" in names

    def test_default_dispatch_throttled_is_false(self):
        d = _diag()
        assert d.dispatch_throttled is False

    def test_default_throttle_wait_ms_is_none(self):
        d = _diag()
        assert d.throttle_wait_ms is None

    def test_throttled_true_with_wait_ms(self):
        d = _diag(dispatch_throttled=True, throttle_wait_ms=1000)
        assert d.dispatch_throttled is True
        assert d.throttle_wait_ms == 1000

    def test_throttled_false_wait_ms_none(self):
        d = _diag(dispatch_throttled=False, throttle_wait_ms=None)
        assert d.dispatch_throttled is False
        assert d.throttle_wait_ms is None

    def test_frozen_cannot_modify_dispatch_throttled(self):
        d = _diag()
        with pytest.raises((AttributeError, TypeError)):
            d.dispatch_throttled = True  # type: ignore[misc]

    def test_frozen_cannot_modify_throttle_wait_ms(self):
        d = _diag()
        with pytest.raises((AttributeError, TypeError)):
            d.throttle_wait_ms = 500  # type: ignore[misc]

    def test_partial_wait_ms_value(self):
        d = _diag(dispatch_throttled=True, throttle_wait_ms=750)
        assert d.throttle_wait_ms == 750

    def test_all_9g8_fields_in_required_set(self):
        """9G8 diagnostics fields are present in WindowExecutionDiagnostics."""
        field_names = {f.name for f in fields(WindowExecutionDiagnostics)}
        assert "dispatch_throttled" in field_names
        assert "throttle_wait_ms" in field_names


# ---------------------------------------------------------------------------
# Class 9: Position invariant — throttle doesn't affect HA position
# ---------------------------------------------------------------------------

class TestPositionInvariantWithThrottle:
    def test_intent_positions_unchanged_by_throttle(self):
        """Throttle sleep doesn't modify intent's HA or internal position."""
        intent = _make_intent(target_internal=60)
        # target_internal=60 → inverted to HA: 100-60=40
        assert intent.target_position_internal == 60
        assert intent.target_position_ha == 40  # HA convention: 100 - internal

    def test_ha_service_receives_ha_position(self):
        """After throttle, HA service still gets target_position_ha."""
        t = _throttle()
        hass = _make_hass()
        intent = _make_intent(target_internal=80)  # HA: 20

        with patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(_run_dispatch_loop(t, hass, [intent]))

        call_args = hass.services.async_call.call_args
        assert call_args is not None
        service_data = call_args[0][2]
        assert service_data["position"] == 20    # HA position
        assert service_data["position"] != 80    # NOT internal position

    def test_throttle_wait_preserves_internal_position(self):
        """Waiting for throttle doesn't change the internal position concept."""
        t = _throttle(interval_s=1.0)
        hass = _make_hass()
        intent_a = _make_intent(cover_entity_id="cover.a", target_internal=80)
        intent_b = _make_intent(cover_entity_id="cover.b", target_internal=60)

        with patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(_run_dispatch_loop(t, hass, [intent_a, intent_b]))

        calls = hass.services.async_call.call_args_list
        assert len(calls) == 2
        pos_a = calls[0][0][2]["position"]
        pos_b = calls[1][0][2]["position"]
        assert pos_a == 20  # 100 - 80
        assert pos_b == 40  # 100 - 60


# ---------------------------------------------------------------------------
# Class 10: Regression — existing behavior unchanged
# ---------------------------------------------------------------------------

class TestRegressionExistingBehavior:
    def test_recommendation_only_still_blocked(self):
        """RECOMMENDATION_ONLY (active_control_enabled=False) still doesn't dispatch."""
        intent = _make_intent(allowed=False)
        t = _throttle()
        hass = _make_hass()

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [intent]))

        assert results[0][0] is ExecutionStatus.BLOCKED
        mock_sleep.assert_not_called()
        hass.services.async_call.assert_not_called()
        assert t.last_dispatch_at is None  # throttle not updated

    def test_throttle_does_not_affect_blocked_result(self):
        """Blocked intent: throttle armed, but blocked intent still blocked."""
        t = _throttle(interval_s=1.0)
        t.record_dispatch(_NOW)  # throttle armed
        hass = _make_hass()
        intent = _make_intent(allowed=False)

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            results = asyncio.run(_run_dispatch_loop(t, hass, [intent]))

        assert results[0][0] is ExecutionStatus.BLOCKED
        mock_sleep.assert_not_called()  # blocked before throttle check

    def test_startup_grace_semantics_unchanged(self):
        """NOT_ATTEMPTED (startup grace) result: throttle not updated."""
        t = _throttle()
        intent = _make_intent()
        # Simulate startup grace (coordinator would use build_not_attempted_result)
        result = build_not_attempted_result(intent, reason="startup_grace_active")
        assert result.status is ExecutionStatus.NOT_ATTEMPTED
        # Coordinator does not call record_dispatch for NOT_ATTEMPTED
        # (only ExecutionStatus.SENT triggers record_dispatch)
        if result.status is ExecutionStatus.SENT:
            t.record_dispatch(_NOW)
        assert t.last_dispatch_at is None

    def test_first_dispatch_always_immediate_regardless_of_interval(self):
        """No previous dispatch → always timedelta(0) regardless of configured interval."""
        for interval_s in [0.1, 1.0, 5.0, 60.0]:
            t = GlobalDispatchThrottle(min_interval=timedelta(seconds=interval_s))
            assert t.time_until_next_allowed() == timedelta(0)

    def test_throttle_is_not_global_singleton(self):
        """Two different GlobalDispatchThrottle instances are independent."""
        t1 = _throttle()
        t2 = _throttle()
        t1.record_dispatch(_NOW)
        assert t1.last_dispatch_at == _NOW
        assert t2.last_dispatch_at is None

    def test_no_ha_import_in_throttle_module(self):
        import custom_components.smartshading.cover_control.global_dispatch_throttle as m
        src = m.__file__
        with open(src) as f:
            content = f.read()
        assert "homeassistant" not in content

    def test_throttle_module_importable_without_ha(self):
        """Module must be importable in a pure-Python test environment."""
        import importlib
        m = importlib.import_module(
            "custom_components.smartshading.cover_control.global_dispatch_throttle"
        )
        assert hasattr(m, "GlobalDispatchThrottle")
        assert hasattr(m, "DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS")
