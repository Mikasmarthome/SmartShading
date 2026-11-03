"""Tests for the R14 one-shot Morning entitlement semantics:
MorningOccurrence.dispatch_attempted, mark_dispatch_attempted(), and
pending_target_position_internal()'s use of that field.

This replaces the R13 event-triggered-gating mechanism
(_resolve_morning_repropose_target() / self._pending_event_triggered_refresh),
which was found to still "conserve historical Morning authority" indefinitely
by reactivating a stale occurrence on ANY later real runtime event
(presence/contact/learning-toggle/zone-config-change/manual-override-clear)
-- none of which are fachlich evidence to re-propose an OLD Morning
candidate. The corrected rule: a `pending` occurrence is re-proposable on
ANY cycle -- periodic or event-triggered alike -- until it has had exactly
ONE real dispatch-capable cycle (mark_dispatch_attempted()), bounded ONLY by
Startup Grace, never by which kind of event caused the cycle.
"""
from __future__ import annotations

from datetime import date, datetime

from custom_components.smartshading.engines.morning_reconciliation import (
    mark_dispatch_attempted,
    mark_dispatched,
    pending_target_position_internal,
    start_occurrence,
)

_TODAY = date(2026, 8, 10)
_NOW = datetime(2026, 8, 10, 7, 5)
_LATER = datetime(2026, 8, 10, 7, 20)


def _pending_occ():
    return start_occurrence(window_id="w1", occurrence_date=_TODAY, target_position_internal=30, now=_NOW)


class TestPendingTargetIgnoresCauseOfCycle:
    """A fresh `pending` occurrence (dispatch_attempted=False) is proposable
    on EVERY cycle -- periodic or event-triggered alike, since this module
    itself has no concept of "cycle cause" any more."""

    def test_first_cycle_returns_target(self):
        occ = _pending_occ()
        assert pending_target_position_internal(
            occ, today=_TODAY, lifecycle_state_is_day=True,
        ) == 30

    def test_many_periodic_style_calls_before_any_attempt_all_return_target(self):
        occ = _pending_occ()
        results = [
            pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=True)
            for _ in range(50)
        ]
        assert all(r == 30 for r in results), (
            "before its one real dispatch-capable cycle, an occurrence is "
            "proposable on every cycle regardless of what causes it"
        )

    def test_missing_occurrence_returns_none(self):
        assert pending_target_position_internal(None, today=_TODAY, lifecycle_state_is_day=True) is None

    def test_not_day_returns_none_even_if_not_attempted(self):
        occ = _pending_occ()
        assert pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=False) is None


class TestMarkDispatchAttemptedBoundsReproposal:
    def test_after_mark_dispatch_attempted_target_is_never_returned_again(self):
        occ = mark_dispatch_attempted(_pending_occ(), now=_NOW)
        assert occ.dispatch_attempted is True
        # status is still "pending" (the dispatch outcome itself was not
        # necessarily resolved by this call) -- but the entitlement is gone.
        assert occ.status == "pending"
        assert pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=True) is None

    def test_bound_holds_across_many_subsequent_periodic_cycles(self):
        occ = mark_dispatch_attempted(_pending_occ(), now=_NOW)
        results = [
            pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=True)
            for _ in range(50)
        ]
        assert all(r is None for r in results)

    def test_one_real_periodic_cycle_after_startup_grace_consumes_entitlement(self):
        """The explicit, previously-missing case: no event-flag is required
        at all -- a plain periodic cycle (simulated here simply as "any
        call, no event distinction exists any more") consumes the one real
        entitlement exactly like an event-triggered one would."""
        occ = _pending_occ()
        assert pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=True) == 30
        occ = mark_dispatch_attempted(occ, now=_NOW)
        assert pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=True) is None

    def test_idempotent(self):
        occ = mark_dispatch_attempted(_pending_occ(), now=_NOW)
        occ2 = mark_dispatch_attempted(occ, now=_LATER)
        assert occ2.dispatch_attempted is True
        # Idempotent call must not touch last_evaluated_at again.
        assert occ2.last_evaluated_at == occ.last_evaluated_at

    def test_independent_of_status_dispatched_stays_unproposable(self):
        occ = _pending_occ()
        occ = mark_dispatch_attempted(occ, now=_NOW)
        occ = mark_dispatched(occ, now=_LATER)
        assert occ.status == "dispatched"
        assert occ.dispatch_attempted is True
        assert pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=True) is None

    def test_dispatched_without_prior_mark_still_terminal_via_status(self):
        """Even if mark_dispatch_attempted() was somehow never called (e.g.
        a fully successful same-cycle dispatch resolves status directly),
        the pre-existing `status != "pending"` check alone already blocks
        re-proposal -- dispatch_attempted is an ADDITIONAL bound, not a
        replacement for the status check."""
        occ = mark_dispatched(_pending_occ(), now=_NOW)
        assert occ.dispatch_attempted is False
        assert pending_target_position_internal(occ, today=_TODAY, lifecycle_state_is_day=True) is None
