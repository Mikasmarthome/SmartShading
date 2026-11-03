"""Tests for engines/morning_reconciliation.py — B3-010 Morning Occurrence
tracking (pure module, no coordinator/HA dependency).

B3-010 R10 (scope correction): this module was reduced from a persistent
per-cover/per-axis satisfaction-confirmation state machine (R7/R8/R9) down
to a minimal per-WINDOW pending/dispatched/superseded/invalidated tracker.
The confirmed-arrival-tracking machinery (per-cover outcomes, tilt
tracking, reconcile_position_observation/record_cover_result) was removed
as B3-021 ("Completion-Neudefinition")/B3-060 (tilt/dual-axis) scope, not
B3-010's canonical acceptance -- see the R10 round report's audit. The two
R9 correctness fixes are preserved in spirit: (1) no confirmed-arrival
claim is made from a command echo, because no confirmed-arrival claim is
made at all any more -- `dispatched` is an honest "the command left
SmartShading" claim; (2) supersession is still classified via
DecisionCategory, never numeric target magnitude (unchanged, lives in
coordinator.py's _MORNING_RECONCILIATION_PROTECTIVE_CATEGORIES, re-tested
in test_morning_evaluator.py / the real-runtime-path tests).

B3-010 R12: the R11 revision's expire_if_stale() (a self.update_interval-
based time-window bound on `pending`) was identified as fachlich
unfounded and REMOVED again -- update_interval is a technical scheduling
detail, not a Morning validity duration. A `pending` occurrence is now
re-proposed every DAY cycle for as long as it is genuinely unresolved,
exactly like every other Tier candidate (Heat/Glare/Solar) already is --
see the module's own STATES docstring for the full reasoning.

Coverage:
  MR-01  start_occurrence creates a pending record.
  MR-02  pending_target_position_internal returns the target while pending
         AND lifecycle_state_is_day is True.
  MR-03  pending_target_position_internal returns None once dispatched.
  MR-04  pending_target_position_internal returns None for a stale
         (different day) occurrence -- no cross-day catch-up.
  MR-05  pending_target_position_internal returns None for a missing record.
  MR-05b pending_target_position_internal returns None when
         lifecycle_state_is_day is False, even for an otherwise-valid
         pending same-day record (never re-propose during NIGHT/EVENING).
  MR-06  mark_dispatched() is terminal -- occurrence becomes `dispatched`.
  MR-07  mark_dispatched() is permanent -- a later call is a no-op.
  MR-08  supersede() is terminal and permanent -- a later call is a no-op.
  MR-09  invalidate() is terminal and permanent -- a later call is a no-op.
  MR-10  supersede()/invalidate()/mark_dispatched() only apply to a pending
         record -- an already-resolved record is left unchanged.
  MR-11  critical bug regression (kept from R8): start_occurrence with an
         INTERNAL target, pending_target_position_internal returns that
         SAME internal value unchanged -- no accidental HA<->internal
         conversion anywhere in the module (the module never imports/calls
         to_ha_position or to_internal_position at all).
"""
from __future__ import annotations

from datetime import date, datetime

from custom_components.smartshading.cover_control.position_semantics import (
    to_ha_position,
    to_internal_position,
)
from custom_components.smartshading.engines.morning_reconciliation import (
    is_current_occurrence,
    invalidate,
    mark_dispatched,
    pending_target_position_internal,
    start_occurrence,
    supersede,
)

_TODAY = date(2026, 8, 10)
_YESTERDAY = date(2026, 8, 9)
_NOW = datetime(2026, 8, 10, 7, 5)


def _fresh(target=30):
    return start_occurrence(
        window_id="window1", occurrence_date=_TODAY, target_position_internal=target, now=_NOW,
    )


class TestStartOccurrence:
    def test_creates_pending(self):
        state = _fresh()
        assert state.status == "pending"
        assert state.window_id == "window1"
        assert state.occurrence_date == _TODAY


class TestPendingTargetPositionInternal:
    def test_returns_target_while_pending(self):
        state = _fresh(target=30)
        assert pending_target_position_internal(state, today=_TODAY, lifecycle_state_is_day=True) == 30

    def test_returns_none_once_dispatched(self):
        state = _fresh()
        state = mark_dispatched(state, now=_NOW)
        assert state.status == "dispatched"
        assert pending_target_position_internal(state, today=_TODAY, lifecycle_state_is_day=True) is None

    def test_returns_none_for_stale_occurrence(self):
        state = _fresh()
        assert pending_target_position_internal(state, today=date(2026, 8, 11), lifecycle_state_is_day=True) is None

    def test_returns_none_for_missing_record(self):
        assert pending_target_position_internal(None, today=_TODAY, lifecycle_state_is_day=True) is None

    def test_returns_none_when_not_day(self):
        """Never re-proposed during NIGHT/EVENING, even for an otherwise
        fully valid same-day pending record."""
        state = _fresh(target=30)
        assert pending_target_position_internal(state, today=_TODAY, lifecycle_state_is_day=False) is None

    def test_is_current_occurrence_false_for_yesterday(self):
        state = _fresh()
        assert is_current_occurrence(state, today=_TODAY) is True
        assert is_current_occurrence(state, today=_YESTERDAY) is False


class TestCriticalConversionBugFix:
    """Kept from B3-010 R8: direct regression test for the confirmed
    HA-vs-internal conversion defect. A prior revision stored an
    HA-convention value in what the coordinator/MorningEvaluator later
    consumed as an internal value, silently corrupting the re-proposed
    direction on the second-and-later cycle."""

    def test_internal_target_round_trips_unchanged_no_conversion(self):
        # HA 70 (partially open) -> internal 30. If the module ever
        # accidentally used the HA value (70) where internal (30) is
        # expected, this assertion would fail with 70 instead of 30.
        ha_value = 70
        internal_value = to_internal_position(ha_value)
        assert internal_value == 30

        state = start_occurrence(
            window_id="window1", occurrence_date=_TODAY,
            target_position_internal=internal_value, now=_NOW,
        )
        # The module must return EXACTLY the internal value passed in --
        # never the HA value, never a re-converted value.
        assert pending_target_position_internal(state, today=_TODAY, lifecycle_state_is_day=True) == 30
        assert state.target_position_internal == 30
        assert state.target_position_internal != ha_value

    def test_single_target_is_invert_agnostic(self):
        """The module stores exactly ONE target_position_internal per
        window occurrence -- invert conversion is the coordinator's
        dispatch-boundary responsibility (to_ha_position with each cover's
        OWN invert flag), never this module's."""
        state = start_occurrence(
            window_id="window1", occurrence_date=_TODAY, target_position_internal=30, now=_NOW,
        )
        assert state.target_position_internal == 30
        # Prove two covers' real dispatch payloads would legitimately
        # differ (normal vs inverted) despite sharing this single
        # canonical internal target -- demonstrating why HA-convention
        # storage here would have been wrong for at least one of them.
        assert to_ha_position(state.target_position_internal, invert=False) == 70
        assert to_ha_position(state.target_position_internal, invert=True) == 30


class TestMarkDispatched:
    def test_is_terminal(self):
        state = _fresh()
        state = mark_dispatched(state, now=_NOW)
        assert state.status == "dispatched"

    def test_is_permanent_second_call_is_noop(self):
        state = _fresh()
        state = mark_dispatched(state, now=_NOW)
        state2 = mark_dispatched(state, now=_NOW)
        assert state2.status == "dispatched"


class TestSupersedeAndInvalidate:
    def test_supersede_is_terminal(self):
        state = _fresh()
        state = supersede(state, now=_NOW, reason="stronger heat protection")
        assert state.status == "superseded"

    def test_supersede_is_permanent_second_call_is_noop(self):
        state = _fresh()
        state = supersede(state, now=_NOW, reason="stronger heat protection")
        state2 = supersede(state, now=_NOW, reason="different reason")
        assert state2.status == "superseded"
        assert state2.reason == "stronger heat protection"

    def test_invalidate_is_terminal(self):
        state = _fresh()
        state = invalidate(state, now=_NOW, reason="config changed")
        assert state.status == "invalidated"

    def test_invalidate_is_permanent_second_call_is_noop(self):
        state = _fresh()
        state = invalidate(state, now=_NOW, reason="config changed")
        state2 = invalidate(state, now=_NOW, reason="manual override")
        assert state2.status == "invalidated"
        assert state2.reason == "config changed"

    def test_supersede_does_not_apply_to_already_dispatched(self):
        state = _fresh()
        state = mark_dispatched(state, now=_NOW)
        assert state.status == "dispatched"
        state2 = supersede(state, now=_NOW, reason="too late")
        assert state2.status == "dispatched"

    def test_invalidate_does_not_apply_to_already_superseded(self):
        state = _fresh()
        state = supersede(state, now=_NOW, reason="heat")
        state2 = invalidate(state, now=_NOW, reason="config")
        assert state2.status == "superseded"
