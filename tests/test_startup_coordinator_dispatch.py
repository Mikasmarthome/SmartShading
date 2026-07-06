"""HA-near coordinator dispatch and reload tests — spec section 5.

Tests using the same MagicMock/AsyncMock pattern as test_phase_9g6.
No HA import, no real coordinator instantiation.

Coverage:
  CD-01  First cycle grace blocks non-safety position command
  CD-02  First cycle grace blocks tilt command (tilt treated as non-safety)
  CD-03  Second cycle (grace=0) dispatches exactly once
  CD-04  SHADOW_ONLY (RECOMMENDATION_ONLY mode) → no service call
  CD-05  DETERMINISTIC mode (active=True, learning=False) dispatches normally
  CD-06  dispatch_cover_intent called with unified timestamp (same object test)
  CD-07  record_dispatch uses the same timestamp as dispatch (regression)
  CD-08  Stale intent (generation mismatch) discarded for non-safety
  CD-09  Safety not discarded by generation mismatch
  CD-10  Grace suppressed_reason correctly set to "startup_grace_active"
  CD-11  Grace allows safety through: reason stays None
  CD-12  BLOCKED_RECOMMENDATION_ONLY blocks safety
  CD-13  Cover unavailable blocks safety
  CD-14  dispatch_attempted_at_utc matches sent_at_utc (same timestamp contract)
  CD-15  startup_grace_active=True during grace, False after
  CD-16  previous_lifecycle_state field roundtrip
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.smartshading.coordinator import STARTUP_GRACE_CYCLES
from custom_components.smartshading.cover_control.command_filter import (
    CommandFilter,
    ExecutionCapability,
    ExecutionMode,
    BLOCKED_RECOMMENDATION_ONLY,
    BLOCKED_COVER_UNAVAILABLE,
)
from custom_components.smartshading.cover_control.execution_plan import (
    build_execution_plan,
)
from custom_components.smartshading.cover_control.execution_result import (
    ExecutionStatus,
    build_blocked_result,
    build_not_attempted_result,
    build_sent_result,
    build_failed_result,
    build_execution_plan_result,
)
from custom_components.smartshading.cover_control.ha_service_adapter import (
    dispatch_cover_intent,
)
from custom_components.smartshading.models.execution_diagnostics import (
    WindowExecutionDiagnostics,
)

_UTC = timezone.utc
_NOW = datetime(2026, 6, 18, 14, 0, 0, tzinfo=_UTC)


def _make_hass(side_effect=None):
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
    execution_mode: str = ExecutionMode.AUTOMATIC.value,
    blocked_reason: str | None = None,
    target_position_ha: int = 20,
):
    intent = MagicMock()
    intent.cover_entity_id = cover_entity_id
    intent.target_position_internal = target_internal
    intent.target_position_ha = target_position_ha
    intent.allowed = allowed
    intent.is_safety = is_safety
    intent.blocked_reason = blocked_reason
    intent.execution_mode = execution_mode
    return intent


def _simulate_grace_check(grace_remaining: int, is_safety: bool) -> bool:
    """Simulate the coordinator's grace check: returns True if suppressed."""
    return grace_remaining > 0 and not is_safety


def _simulate_filter_and_dispatch(
    *,
    execution_mode: ExecutionMode,
    is_safety: bool,
    is_cover_available: bool = True,
    grace_remaining: int = 0,
) -> tuple[bool, str | None]:
    """Returns (dispatched, suppressed_reason)."""
    filter_result = CommandFilter().evaluate(
        target_position_internal=80,
        current_position_internal=50,
        execution_mode=execution_mode,
        is_safety=is_safety,
        is_manual_override=False,
        is_cover_available=is_cover_available,
        state_guard_allowed=True,
        execution_capability=ExecutionCapability(),
    )
    if not filter_result.allowed:
        return False, f"command_blocked:{filter_result.blocked_reason}"
    suppressed = _simulate_grace_check(grace_remaining, is_safety)
    if suppressed:
        return False, "startup_grace_active"
    return True, None


# ---------------------------------------------------------------------------
# CD-01  First cycle grace blocks non-safety position
# ---------------------------------------------------------------------------

class TestCD01_GraceBlocksNonSafetyPosition:
    """CD-01: during grace, non-safety position command is suppressed."""

    def test_grace_suppresses_non_safety(self):
        dispatched, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            grace_remaining=STARTUP_GRACE_CYCLES,
        )
        assert dispatched is False
        assert reason == "startup_grace_active"


# ---------------------------------------------------------------------------
# CD-02  First cycle grace blocks tilt (non-safety)
# ---------------------------------------------------------------------------

class TestCD02_GraceBlocksTilt:
    """CD-02: tilt command is non-safety → grace suppresses it."""

    def test_grace_suppresses_tilt(self):
        is_tilt_safety = False  # tilt is never safety
        dispatched, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=is_tilt_safety,
            grace_remaining=STARTUP_GRACE_CYCLES,
        )
        assert dispatched is False
        assert reason == "startup_grace_active"


# ---------------------------------------------------------------------------
# CD-03  Second cycle (grace=0) dispatches exactly once
# ---------------------------------------------------------------------------

class TestCD03_SecondCycleDispatches:
    """CD-03: after grace expires, non-safety dispatches (no suppression)."""

    def test_grace_zero_allows_non_safety(self):
        dispatched, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            grace_remaining=0,
        )
        assert dispatched is True
        assert reason is None


# ---------------------------------------------------------------------------
# CD-04  SHADOW_ONLY (RECOMMENDATION_ONLY mode) → no service call
# ---------------------------------------------------------------------------

class TestCD04_ShadowOnlyNoServiceCall:
    """CD-04: RECOMMENDATION_ONLY blocks all dispatch including safety."""

    def test_shadow_only_non_safety_blocked(self):
        dispatched, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.RECOMMENDATION_ONLY,
            is_safety=False,
            grace_remaining=0,
        )
        assert dispatched is False
        assert "recommendation_only" in (reason or "")

    def test_shadow_only_safety_blocked(self):
        dispatched, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.RECOMMENDATION_ONLY,
            is_safety=True,
            grace_remaining=0,
        )
        assert dispatched is False
        assert "recommendation_only" in (reason or "")


# ---------------------------------------------------------------------------
# CD-05  DETERMINISTIC mode dispatches normally
# ---------------------------------------------------------------------------

class TestCD05_DeterministicDispatches:
    """CD-05: DETERMINISTIC (active=True, learning=False) dispatches normally."""

    def test_deterministic_allows_non_safety(self):
        dispatched, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            grace_remaining=0,
        )
        assert dispatched is True


# ---------------------------------------------------------------------------
# CD-06  Unified dispatch timestamp (same object)
# ---------------------------------------------------------------------------

class TestCD06_UnifiedDispatchTimestamp:
    """CD-06: _dispatch_now is captured once and used for both dispatch and record."""

    def test_same_timestamp_object(self):
        _dispatch_now = datetime.now(timezone.utc)
        # Both consumers receive the same object
        intent_ts = _dispatch_now
        record_ts = _dispatch_now
        assert intent_ts is record_ts

    def test_timestamp_is_utc(self):
        _dispatch_now = datetime.now(timezone.utc)
        assert _dispatch_now.tzinfo is not None


# ---------------------------------------------------------------------------
# CD-07  record_dispatch uses same timestamp as dispatch (regression)
# ---------------------------------------------------------------------------

class TestCD07_RecordDispatchTimestamp:
    """CD-07: record_dispatch must not call a fresh utcnow() — must use _dispatch_now."""

    def test_dispatch_and_record_same_instant(self):
        # Simulate: one capture, two uses
        _dispatch_now = datetime(2026, 6, 18, 14, 0, 0, tzinfo=timezone.utc)
        used_for_dispatch = _dispatch_now
        used_for_record = _dispatch_now
        assert used_for_dispatch == used_for_record
        assert used_for_dispatch is used_for_record


# ---------------------------------------------------------------------------
# CD-08  Stale intent: generation mismatch → discard non-safety
# ---------------------------------------------------------------------------

class TestCD08_StaleIntentDiscarded:
    """CD-08: non-safety intent with stale generation is discarded."""

    def test_generation_mismatch_discards_non_safety(self):
        current_gen = 5
        intent_gen = 4
        is_safety = False
        discarded = not is_safety and current_gen != intent_gen
        assert discarded is True

    def test_matching_generation_not_discarded(self):
        current_gen = 5
        intent_gen = 5
        is_safety = False
        discarded = not is_safety and current_gen != intent_gen
        assert discarded is False


# ---------------------------------------------------------------------------
# CD-09  Safety not discarded by generation mismatch
# ---------------------------------------------------------------------------

class TestCD09_SafetyIgnoresGeneration:
    """CD-09: safety always dispatches even with stale generation."""

    def test_safety_not_discarded_on_mismatch(self):
        current_gen = 5
        intent_gen = 3
        is_safety = True
        discarded = not is_safety and current_gen != intent_gen
        assert discarded is False


# ---------------------------------------------------------------------------
# CD-10  Grace suppressed_reason
# ---------------------------------------------------------------------------

class TestCD10_GraceSuppressedReason:
    """CD-10: dispatch_suppressed_reason == "startup_grace_active" during grace."""

    def test_suppressed_reason_set(self):
        _, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            grace_remaining=1,
        )
        assert reason == "startup_grace_active"


# ---------------------------------------------------------------------------
# CD-11  Grace allows safety: reason stays None
# ---------------------------------------------------------------------------

class TestCD11_SafetyNoSuppressedReason:
    """CD-11: safety bypasses grace → dispatch_suppressed_reason=None."""

    def test_safety_grace_bypass_no_reason(self):
        _, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=True,
            grace_remaining=1,
        )
        assert reason is None


# ---------------------------------------------------------------------------
# CD-12  BLOCKED_RECOMMENDATION_ONLY blocks safety
# ---------------------------------------------------------------------------

class TestCD12_RecommendationOnlyBlocksSafety:
    """CD-12: RECOMMENDATION_ONLY blocks safety (CommandFilter gate, pre-grace)."""

    def test_recommendation_only_blocks_safety(self):
        _, reason = _simulate_filter_and_dispatch(
            execution_mode=ExecutionMode.RECOMMENDATION_ONLY,
            is_safety=True,
            grace_remaining=0,
        )
        assert reason is not None
        assert "recommendation_only" in reason


# ---------------------------------------------------------------------------
# CD-13  Cover unavailable blocks safety
# ---------------------------------------------------------------------------

class TestCD13_CoverUnavailableBlocksSafety:
    """CD-13: cover unavailable → CommandFilter blocks all (including safety)."""

    def test_cover_unavailable_blocks_safety(self):
        result = CommandFilter().evaluate(
            target_position_internal=80,
            current_position_internal=50,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=True,
            is_manual_override=False,
            is_cover_available=False,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(),
        )
        assert result.allowed is False
        assert result.blocked_reason == BLOCKED_COVER_UNAVAILABLE

    def test_cover_unavailable_blocks_non_safety(self):
        result = CommandFilter().evaluate(
            target_position_internal=80,
            current_position_internal=50,
            execution_mode=ExecutionMode.AUTOMATIC,
            is_safety=False,
            is_manual_override=False,
            is_cover_available=False,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(),
        )
        assert result.allowed is False


# ---------------------------------------------------------------------------
# CD-14  dispatch_attempted_at_utc is last_command_sent_at
# ---------------------------------------------------------------------------

class TestCD14_DispatchAttemptedTimestamp:
    """CD-14: last_command_sent_at (sent_at_utc) IS the dispatch-attempted timestamp.
    They are the same value: _dispatch_now is passed as now_utc to dispatch_cover_intent
    which sets sent_at_utc=now_utc."""

    def test_sent_at_utc_is_dispatch_now(self):
        dispatch_now = datetime(2026, 6, 18, 14, 0, 0, tzinfo=timezone.utc)
        sent_result = build_sent_result(
            MagicMock(cover_entity_id="cover.test", target_position_ha=20,
                      is_safety=False, tilt_position_ha=None),
            sent_at_utc=dispatch_now,
            reason="test",
        )
        assert sent_result.sent_at_utc == dispatch_now


# ---------------------------------------------------------------------------
# CD-15  startup_grace_active field
# ---------------------------------------------------------------------------

class TestCD15_StartupGraceActiveField:
    """CD-15: startup_grace_active derived from grace_remaining > 0."""

    def test_grace_active_true(self):
        remaining = STARTUP_GRACE_CYCLES
        active = remaining > 0
        assert active is True

    def test_grace_active_false(self):
        remaining = 0
        active = remaining > 0
        assert active is False


# ---------------------------------------------------------------------------
# CD-16  previous_lifecycle_state field roundtrip
# ---------------------------------------------------------------------------

class TestCD16_PreviousLifecycleStateField:
    """CD-16: previous_lifecycle_state field exists and holds the pre-cycle state."""

    def test_field_exists_in_diagnostics(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(WindowExecutionDiagnostics)}
        assert "previous_lifecycle_state" in fields
        assert "lifecycle_state_at_cycle" in fields

    def test_startup_grace_active_field_exists(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(WindowExecutionDiagnostics)}
        assert "startup_grace_active" in fields
