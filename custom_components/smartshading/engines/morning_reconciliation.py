"""Morning Occurrence tracking — B3-010 (R10, scope-corrected).

Pure, coordinator-independent per-WINDOW state (no per-cover, no per-axis,
no tilt, no completion/arrival confirmation -- see the R10 round report's
audit for why each of those was removed as out-of-scope for B3-010's
canonical acceptance).

Canonical B3-010 acceptance is exactly:
  "Die konfigurierte Morgenposition wirkt im Runtime-Pfad. Bei morgens
   bereits erforderlicher Beschattung wird unmittelbar das finale Ziel
   angefahren."

This module exists to satisfy ONLY the part of that acceptance the
existing per-cycle Tier/Resolver arbitration cannot express by itself: a
window's Morning candidate must keep being re-proposed across DAY cycles
while nothing has resolved it yet (so a transient technical block does not
silently drop the configured morning position for the whole day), but must
STOP being re-proposed, permanently for that day's occurrence, the moment
it is genuinely resolved -- successfully dispatched, superseded by a real
protective win, or invalidated by a changed fact. Everything else (which
target wins a given cycle, whether an already-active protection applies
directly, whether a later protection displaces Morning) is handled by the
EXISTING TierOrchestrator/PositionResolver arbitration -- this module never
duplicates that decision, it only remembers whether Morning's OWN
candidate for today is still open.

CANONICAL CONVENTION
---------------------
target_position_internal is SmartShading's INTERNAL convention (0=open,
100=shaded -- see cover_control/position_semantics.py), never HA
convention -- exactly like every other target-position value on the
WindowDecisionInput/WindowDecision path. HA conversion happens only at the
coordinator's dispatch boundary, per-cover, with that cover's own invert
flag -- this module never calls to_ha_position/to_internal_position.

STATES
------
pending      -- today's occurrence exists and is not yet resolved.
                MorningEvaluator keeps re-proposing its target_position_internal
                every DAY cycle while pending (see evaluators/
                morning_evaluator.py) -- never during NIGHT/EVENING, and
                never for a stale (different-day) occurrence.
dispatched   -- SmartShading successfully SENT the configured morning
                target for every cover in this window this cycle (a real
                ExecutionStatus.SENT with no ExecutionStatus.FAILED in the
                same result set) -- i.e. "the configured morning position
                acted in the runtime path" per the canonical acceptance
                text. This deliberately does NOT wait for or claim
                confirmed physical arrival at the target -- proving actual
                travel completion is B3-021's (Completion-Neudefinition)
                scope, not B3-010's. Terminal for this occurrence.
superseded   -- a decider carrying real protective authority
                (DecisionCategory.SAFETY or .PROTECTION, or SolarEvaluator
                specifically within the otherwise-ambiguous .COMFORT
                category -- see coordinator.py's
                _MORNING_RECONCILIATION_PROTECTIVE_CATEGORIES for the exact
                classification) won arbitration for this window, either
                already at the MORNING transition itself or on a later DAY
                cycle while still pending. Deliberately NOT a numeric
                "target more shaded than Morning's own" comparison -- a
                genuine protective need can legitimately require a
                numerically SMALLER (less shaded) target than Morning's
                own (e.g. Storm/Wind safety opening covers fully), so only
                which tier/category won arbitration can decide this, never
                target magnitude. Terminal and permanent for this
                occurrence: ending the protection later does NOT revert to
                re-proposing the old morning target -- the next real
                decision is whatever tier genuinely applies then (the
                existing TierOrchestrator arbitration, unchanged). A
                technical non-execution (StateGuard/CommandFilter block,
                cover unavailable, service error, stale generation, live-
                revalidation skip) is explicitly NOT a supersession -- the
                occurrence simply stays `pending` unchanged, but is NOT
                automatically re-proposed forever. R14: this module itself
                has no time-window or cycle-count bound (an R11 revision's
                update_interval-based expiry was removed as fachlich
                unfounded; an R13 revision's event-triggered gating was
                also removed -- reacting to ANY later real runtime event
                such as presence/contact/learning-switch/zone-config-change/
                manual-override-clear was itself found to "conserve
                historical Morning authority" indefinitely, which is not
                fachlich justified either). The actual bound is
                `dispatch_attempted` (see that field's own docstring and
                mark_dispatch_attempted()): a `pending` occurrence is
                re-proposable (via pending_target_position_internal()) on
                ANY cycle -- periodic or event-triggered alike -- UNTIL its
                ONE real dispatch-capable cycle happens, i.e. the first
                cycle Morning's candidate reaches the real dispatch
                pipeline without being suppressed by Startup Grace. After
                that one cycle, dispatch_attempted is permanently True and
                the occurrence is NEVER re-proposed again, regardless of
                whether that one attempt fully resolved (dispatched/
                superseded/invalidated) or left some covers technically
                blocked (a disclosed limitation -- general technical-
                failure recovery is B3-040 scope). This is exactly the
                fachlich-required distinction between Morning (a single
                one-time-per-day lifecycle event, entitled to exactly one
                real decision) and Heat/Glare/Solar (continuously current
                environmental conditions, legitimately re-evaluated every
                periodic tick) -- without relying on which kind of runtime
                event happened to cause the cycle.
invalidated  -- a real changed fact (an active Manual Override -- including
                one already active AT the MORNING transition itself, or
                blocking the dispatch attempt itself) ended the
                occurrence's validity before it was ever dispatched or
                superseded, or the window's cover group was empty/removed
                at dispatch time. Terminal.

Occurrences are identified by (window_id, occurrence_date). A record whose
occurrence_date does not match "today" when read is treated as stale and
ignored (no cross-day catch-up) -- the coordinator is responsible for
creating a fresh record on the next genuine MORNING transition, or (B3-010
canonical requirement: a missed Morning event must not be silently
dropped) via its own separate missed-Morning-event check (config/
lifecycle/month/trigger-driven, see coordinator.py -- deliberately NOT
based on the technical startup-grace-cycle counter, which carries no
fachlich meaning about whether Morning is still due). Neither path blindly
resumes a stale PRIOR-day record, and neither path re-creates an
occurrence this module itself just expired via expire_if_stale() within
the same calendar day without a genuine changed-fact justification (see
coordinator.py's missed-event check for the exact conditions).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal

MorningOccurrenceStatus = Literal["pending", "dispatched", "superseded", "invalidated"]


@dataclass(frozen=True)
class MorningOccurrence:
    """One window's Morning candidate for one calendar occurrence, in
    SmartShading INTERNAL convention throughout."""

    window_id: str
    occurrence_date: date
    target_position_internal: int
    status: MorningOccurrenceStatus = "pending"
    created_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    #: Human-readable reason the record became dispatched/superseded/
    #: invalidated, for diagnostics only -- never consulted for logic.
    reason: str | None = None
    #: R14: True once this occurrence has had its ONE real dispatch-capable
    #: cycle -- see mark_dispatch_attempted()'s own docstring. Independent
    #: of `status`: a `pending` occurrence with dispatch_attempted=True is
    #: an honest, permanent "we tried once, the outcome was not fully
    #: resolved" record -- it is NEVER re-proposed again, exactly like a
    #: terminal one, but its diagnostic status stays truthful rather than
    #: being forced into a misleading terminal label.
    #:
    #: Deliberately narrower than three OTHER claims this name must never be
    #: read as making:
    #:   - NOT "the decision was evaluated" -- MorningEvaluator/TierOrchestrator
    #:     evaluate a fresh WindowDecision EVERY cycle regardless (that is
    #:     just normal per-cycle arbitration, carries no persisted state at
    #:     all). This field only becomes True once that decision's real
    #:     per-cover ExecutionResults, for every expected cover, are no
    #:     longer ExecutionStatus.NOT_ATTEMPTED (see
    #:     coordinator.py's own call site for the exact check) -- i.e. the
    #:     dispatch/CommandFilter pipeline was genuinely REACHED and
    #:     RESOLVED for this window this cycle, not merely that a decision
    #:     was computed.
    #:   - NOT "a command was sent" -- a cover technically BLOCKED by
    #:     CommandFilter (a real, resolved outcome, distinct from
    #:     NOT_ATTEMPTED) still sets this True; only `status` becoming
    #:     "dispatched" claims a real ExecutionStatus.SENT happened (see
    #:     mark_dispatched()).
    #:   - NOT "physically completed" -- see this module's own docstring;
    #:     confirmed physical arrival is B3-021 scope, never claimed here.
    dispatch_attempted: bool = False


def start_occurrence(
    *, window_id: str, occurrence_date: date, target_position_internal: int, now: datetime,
) -> MorningOccurrence:
    """Create a fresh `pending` occurrence. Always creates a NEW record --
    the coordinator calls this only on a genuine MORNING transition cycle,
    or once, on missed-event detection (see module docstring).
    target_position_internal must already be in SmartShading internal
    convention (e.g. wdi.effective_behavior.morning_position) -- never
    HA-converted."""
    return MorningOccurrence(
        window_id=window_id, occurrence_date=occurrence_date,
        target_position_internal=target_position_internal,
        status="pending", created_at=now, last_evaluated_at=now,
    )


def is_current_occurrence(occ: MorningOccurrence | None, *, today: date) -> bool:
    """False for a missing record OR one belonging to a different day (no
    cross-day catch-up)."""
    return occ is not None and occ.occurrence_date == today


def pending_target_position_internal(
    occ: MorningOccurrence | None, *, today: date, lifecycle_state_is_day: bool,
) -> int | None:
    """The target MorningEvaluator should keep re-proposing THIS cycle, in
    SmartShading internal convention, or None if there is nothing to
    re-propose. R14 one-shot semantics: this is None whenever
    `occ.dispatch_attempted` is True -- an occurrence gets re-proposed
    ONLY until its ONE real dispatch-capable cycle happens (see
    mark_dispatch_attempted()), NEVER afterward, regardless of whether
    that one attempt fully resolved (dispatched/superseded/invalidated) or
    left some covers technically blocked. Also None for
    dispatched/superseded/invalidated/stale/missing, OR when the current
    lifecycle_state is not DAY -- Morning must never re-propose during
    NIGHT/EVENING; the genuine MORNING transition cycle itself is handled
    separately by MorningEvaluator's own MORNING branch, not this
    function."""
    if not lifecycle_state_is_day:
        return None
    if not is_current_occurrence(occ, today=today):
        return None
    if occ.status != "pending":
        return None
    if occ.dispatch_attempted:
        return None
    return occ.target_position_internal


def mark_dispatch_attempted(occ: MorningOccurrence, *, now: datetime) -> MorningOccurrence:
    """R14: record that this occurrence has now had its ONE real
    dispatch-capable cycle -- the coordinator calls this exactly once per
    occurrence, on the first cycle where every one of THIS window's real
    per-cover ExecutionResults is no longer ExecutionStatus.NOT_ATTEMPTED
    (i.e. each cover's intent genuinely reached dispatch/CommandFilter
    resolution this cycle -- SENT, FAILED, or a real BLOCKED outcome, not a
    scheduling deferral). NOT_ATTEMPTED covers EVERY reason a cover's
    intent never reached real resolution -- Startup Grace
    ("startup_grace_active"), a newer cycle already superseding this one
    ("stale_generation" / "stale_presence_superseded"), or this cycle's own
    comfort dispatch being deferred behind an executable safety intent
    ("safety_preempted") -- checked via the ExecutionStatus itself (see
    coordinator.py's own call site), never a second, parallel "is dispatch
    capable" check duplicating the dispatch loop's own per-reason logic.
    Idempotent and independent of `status`/terminal state -- safe to call
    even if the SAME cycle's dispatch outcome ALSO transitions status via
    mark_dispatched()/supersede()/invalidate().

    This is the sole mechanism bounding Morning's re-propose window: once
    set, pending_target_position_internal() returns None forever for this
    occurrence, regardless of whether the one real attempt fully
    succeeded. A cover technically BLOCKED on that one attempt is a real,
    disclosed limitation -- general technical-failure recovery is B3-040
    scope, not B3-010's. A cover left NOT_ATTEMPTED (any reason) is NOT
    such a limitation -- it never reached real resolution at all, so this
    field stays False and a later dispatch-capable cycle still gets the
    one real decision; nothing is lost, only deferred.
    """
    if occ.dispatch_attempted:
        return occ
    return replace(occ, dispatch_attempted=True, last_evaluated_at=now)


def mark_dispatched(occ: MorningOccurrence, *, now: datetime) -> MorningOccurrence:
    """Terminal transition: SmartShading successfully sent the configured
    morning target for every cover in this window this cycle. Does not
    wait for or claim confirmed physical arrival (see module docstring --
    that is B3-021 scope)."""
    if occ.status != "pending":
        return occ
    return replace(occ, status="dispatched", last_evaluated_at=now)


def supersede(occ: MorningOccurrence, *, now: datetime, reason: str) -> MorningOccurrence:
    """Terminal, permanent transition -- a genuinely stronger protective
    tier won arbitration. Never reverts. The CALLER is responsible for
    only invoking this for a real protective win (see coordinator.py's
    DecisionCategory check) -- never for a technical non-execution or a
    plain fallback/no-op decider."""
    if occ.status != "pending":
        return occ
    return replace(occ, status="superseded", last_evaluated_at=now, reason=reason)


def invalidate(occ: MorningOccurrence, *, now: datetime, reason: str) -> MorningOccurrence:
    """Terminal transition for a changed fact that ends the occurrence's
    validity before it was dispatched or superseded."""
    if occ.status != "pending":
        return occ
    return replace(occ, status="invalidated", last_evaluated_at=now, reason=reason)
