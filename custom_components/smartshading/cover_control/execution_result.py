"""Execution Result — Phase 9G3b.

Immutable result model for one cover command execution attempt.
No Home Assistant dependency.  No service calls.  No async.

PURPOSE
-------
ExecutionResult closes the audit loop: after CoverIntent says "we want to do
this", and CoverController translates it into a low-level CoverCommand,
ExecutionResult records what actually happened.  It is the primary source
for diagnostics, logging, and RuntimeData.

In 9G3b this module is result *structure only* — no real HA service calls
are made yet.  The HAServiceAdapter (Step 9G4) will produce ExecutionResult
instances after dispatching commands.

LIFECYCLE
---------
For each CoverIntent in an ExecutionPlan, exactly one ExecutionResult is
produced per coordinator cycle:

    CoverIntent (allowed=False) → build_blocked_result()     → BLOCKED
    CoverIntent (allowed=True)  → HAServiceAdapter dispatches
                                 → service call succeeds     → SENT / SUCCEEDED
                                 → service call throws       → FAILED
    No intent (empty plan)      → build_not_attempted_result() → NOT_ATTEMPTED

EXECUTION STATUS SEMANTICS
--------------------------
    NOT_ATTEMPTED   No dispatch was attempted.  Reasons: the execution pipeline
                    was not reached (dry-run, coordinator early exit), or the
                    CoverGroup had no cover entities.

    BLOCKED         CommandFilter (or another pre-execution gate) prevented the
                    command.  blocked_reason carries the BLOCKED_* constant.
                    target positions are still populated for diagnostics.

    SKIPPED         The intent was allowed, but execution was deliberately
                    deferred this cycle (e.g. TravelTracker reports the cover
                    is still moving from a previous command).  Reserved for
                    Step 9G4 — no factory provided in 9G3b.

    SENT            The HA service call was dispatched (fire-and-forget mode).
                    No confirmation of physical movement.  Default for this version.

    SUCCEEDED       The service call completed and confirmation was received
                    (e.g. the entity's position attribute updated as expected).
                    Reserved for future confirmation logic — no factory in 9G3b.

    FAILED          The service call raised an exception or HA returned an
                    error.  error field contains the exception text.

POSITION CONVENTIONS
--------------------
ExecutionResult carries both conventions from the originating CoverIntent:
    target_position_internal  SmartShading internal (0=open, 100=shaded)
    target_position_ha        HA cover convention (0=closed, 100=open)

No conversion is performed here.  These are pass-throughs from CoverIntent,
which already holds both values (populated by CommandFilter via
to_ha_position()).

UTC POLICY
----------
sent_at_utc must always be a timezone-aware datetime when set.  Naive
datetimes are rejected with ValueError in all factory functions — consistent
with the project-wide UTC-only policy (all other timestamp fields in the
coordinator, AdaptationTrace, CoverIntent, etc., are UTC-aware).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Sequence

from .execution_plan import CoverCommandType, CoverIntent


# ---------------------------------------------------------------------------
# ExecutionStatus
# ---------------------------------------------------------------------------

class ExecutionStatus(Enum):
    """Outcome of one cover command execution attempt.

    Values are ordered from "nothing happened" to "something failed":
        NOT_ATTEMPTED → BLOCKED → SKIPPED → SENT → SUCCEEDED → FAILED

    Only BLOCKED, SENT, and FAILED have factory functions in 9G3b.
    SKIPPED and SUCCEEDED are reserved for Step 9G4 and beyond.
    """

    NOT_ATTEMPTED = "not_attempted"
    """No dispatch attempt was made (see module docstring)."""

    BLOCKED = "blocked"
    """A pre-execution gate prevented the command.
    blocked_reason carries the BLOCKED_* constant from CommandFilter."""

    SKIPPED = "skipped"
    """Allowed but deferred this cycle (cover still moving, etc.).
    Reserved — no factory in 9G3b."""

    SENT = "sent"
    """HA service call dispatched (fire-and-forget).  Default status for this version."""

    SUCCEEDED = "succeeded"
    """Service call completed with confirmation.  Reserved for future use."""

    FAILED = "failed"
    """Service call raised an exception or HA returned an error."""


# ---------------------------------------------------------------------------
# ExecutionResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionResult:
    """Immutable record of one cover command execution attempt.

    One ExecutionResult is produced per CoverIntent per coordinator cycle.
    All fields are either derived from the originating CoverIntent or set by
    the execution layer (HAServiceAdapter, Step 9G4).

    FIELD NOTES
    -----------
    execution_mode
        ExecutionMode.value string — same type as CoverIntent.execution_mode.
        'automatic' or 'recommendation_only'.

    blocked_reason
        BLOCKED_* string constant from command_filter.py.
        None unless status is BLOCKED.

    error
        Exception text or HA error message.
        None unless status is FAILED.

    sent_at_utc
        UTC timestamp when the service call was dispatched.
        None when the command was never sent (NOT_ATTEMPTED, BLOCKED).
        Always timezone-aware when set — naive datetimes are rejected.

    reason
        Human-readable summary for logging and diagnostics.
        Set by the factory function or the HAServiceAdapter.
    """

    entity_id: str
    """HA entity_id of the cover this result is for."""

    status: ExecutionStatus
    """Outcome of the execution attempt."""

    command_type: CoverCommandType
    """Semantic command type from the originating CoverIntent."""

    execution_mode: str
    """ExecutionMode.value: 'automatic' or 'recommendation_only'."""

    target_position_internal: int | None
    """Target position in SmartShading internal convention (0=open, 100=shaded).
    Preserved from CoverIntent even when blocked — for diagnostics."""

    target_position_ha: int | None
    """Target position in HA cover convention (0=closed, 100=open).
    Preserved from CoverIntent even when blocked — for diagnostics."""

    target_tilt: int | None
    """Target tilt position — Phase 2 only.  Always None in this version."""

    is_safety: bool
    """True when triggered by STORM_SAFE or WIND_SAFE (Tier 1 Safety).
    Failed safety results can be identified by status==FAILED and is_safety==True."""

    blocked_reason: str | None
    """BLOCKED_* constant explaining why the command was prevented.
    None unless status is BLOCKED."""

    error: str | None
    """Exception text or HA error description.
    None unless status is FAILED."""

    sent_at_utc: datetime | None
    """UTC timestamp when the HA service call was dispatched.
    Always timezone-aware when not None."""

    reason: str
    """Human-readable summary for logging and diagnostics."""

    tilt_sent: bool = False
    """True when cover.set_cover_tilt_position was successfully dispatched.
    False for position-only commands, blocked commands, or when tilt dispatch failed."""

    tilt_error: str | None = None
    """Exception text when the tilt service call failed.
    None when tilt was not attempted, succeeded, or was not part of this command."""

    # --- P11 Increment 2-rest: read-only service-boundary trace (additive) ---
    service_started_monotonic: float | None = None
    """time.monotonic() captured immediately BEFORE hass.services.async_call.
    None when no service call was made (BLOCKED/NOT_ATTEMPTED).  Diagnostics only."""

    service_completed_monotonic: float | None = None
    """time.monotonic() captured immediately AFTER async_call returns/raises.
    None when no service call was made.  Diagnostics only."""

    service_duration_ms: float | None = None
    """service_completed_monotonic − service_started_monotonic, in ms.  Never
    negative; None when unavailable.  Diagnostics only."""

    failure_exception_type: str | None = None
    """The exception CLASS NAME only (privacy-safe, no message/payload) when the
    service call raised.  None on success.  Diagnostics only."""


# ---------------------------------------------------------------------------
# ExecutionPlanResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionPlanResult:
    """Aggregated result for all covers in one window per coordinator cycle.

    One ExecutionPlanResult wraps the ExecutionResult for every CoverIntent
    in the corresponding ExecutionPlan.  It provides pre-computed aggregates
    so the Coordinator and diagnostic sensors do not need to iterate results.
    """

    window_id: str
    """The window this result belongs to."""

    results: tuple[ExecutionResult, ...]
    """One ExecutionResult per cover entity in the window's CoverGroup.
    Empty when the ExecutionPlan had no cover entities."""

    any_sent: bool
    """True when at least one result has status SENT or SUCCEEDED.
    Used by the Coordinator to decide whether to call StateGuard.record_action_sent()."""

    any_failed: bool
    """True when at least one result has status FAILED.
    Used to trigger retries or alert logging in later steps."""

    all_blocked: bool
    """True when results is non-empty and every result has status BLOCKED.
    Distinguishes "nothing allowed" (all_blocked=True) from "nothing attempted"
    (empty results) and "some sent" (any_sent=True)."""

    contains_safety_result: bool
    """True when at least one result has is_safety=True.
    Used for priority dispatch and safety-specific diagnostics."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_timezone_aware(dt: datetime, field_name: str = "sent_at_utc") -> None:
    """Raise ValueError if *dt* is a naive (timezone-unaware) datetime.

    All timestamps in SmartShading are UTC-aware.  Rejecting naive datetimes
    early prevents silent timezone bugs — the same policy as AdaptationTrace,
    CoverIntent, and all coordinator timestamps.
    """
    if dt.tzinfo is None:
        raise ValueError(
            f"{field_name} must be timezone-aware (UTC). "
            f"Got a naive datetime: {dt!r}. "
            f"Use datetime.now(timezone.utc) or dt_util.utcnow() from the coordinator."
        )


def _result_from_intent(
    intent: CoverIntent,
    *,
    status: ExecutionStatus,
    blocked_reason: str | None,
    error: str | None,
    sent_at_utc: datetime | None,
    reason: str,
) -> ExecutionResult:
    """Internal factory — all public builders delegate to this."""
    return ExecutionResult(
        entity_id=intent.cover_entity_id,
        status=status,
        command_type=intent.command_type,
        execution_mode=intent.execution_mode,
        target_position_internal=intent.target_position_internal,
        target_position_ha=intent.target_position_ha,
        target_tilt=intent.target_tilt,
        is_safety=intent.is_safety,
        blocked_reason=blocked_reason,
        error=error,
        sent_at_utc=sent_at_utc,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Public factory functions
# ---------------------------------------------------------------------------

def build_not_attempted_result(
    intent: CoverIntent,
    *,
    reason: str,
) -> ExecutionResult:
    """Produce an ExecutionResult for an intent that was never dispatched.

    Use when the execution pipeline was not reached — e.g. the cover group
    had no entities this cycle, or a coordinator-level early exit occurred.

    Parameters
    ----------
    intent:
        The CoverIntent that was not attempted.  All diagnostic fields
        (positions, command_type, is_safety) are preserved from it.
    reason:
        Human-readable explanation for logging.
    """
    return _result_from_intent(
        intent,
        status=ExecutionStatus.NOT_ATTEMPTED,
        blocked_reason=None,
        error=None,
        sent_at_utc=None,
        reason=reason,
    )


def build_blocked_result(
    intent: CoverIntent,
    *,
    reason: str,
    blocked_reason: str | None = None,
) -> ExecutionResult:
    """Produce an ExecutionResult for an intent that was blocked pre-execution.

    Typically used when CommandFilter returned allowed=False.  The
    blocked_reason is taken from intent.blocked_reason by default; supply an
    explicit value when a different gate (e.g. TravelTracker.is_moving) blocks
    an otherwise-allowed intent.

    Target positions are preserved from the intent so diagnostics can show
    "would have moved to position X, but was blocked by Y".

    Parameters
    ----------
    intent:
        The CoverIntent that was blocked.
    reason:
        Human-readable explanation for logging.
    blocked_reason:
        BLOCKED_* constant.  Defaults to intent.blocked_reason when None.
    """
    return _result_from_intent(
        intent,
        status=ExecutionStatus.BLOCKED,
        blocked_reason=blocked_reason if blocked_reason is not None else intent.blocked_reason,
        error=None,
        sent_at_utc=None,
        reason=reason,
    )


def build_sent_result(
    intent: CoverIntent,
    *,
    sent_at_utc: datetime,
    reason: str,
) -> ExecutionResult:
    """Produce an ExecutionResult for an intent that was dispatched to HA.

    Fire-and-forget mode: the HA service call was issued, but no confirmation
    of physical movement is awaited.  This is the default status for this version.

    Parameters
    ----------
    intent:
        The CoverIntent that was dispatched.
    sent_at_utc:
        UTC timestamp when the service call was issued.  Must be timezone-aware.
    reason:
        Human-readable explanation for logging.

    Raises
    ------
    ValueError
        If sent_at_utc is a naive (timezone-unaware) datetime.
    """
    _require_timezone_aware(sent_at_utc)
    return _result_from_intent(
        intent,
        status=ExecutionStatus.SENT,
        blocked_reason=None,
        error=None,
        sent_at_utc=sent_at_utc,
        reason=reason,
    )


def build_failed_result(
    intent: CoverIntent,
    *,
    error: str,
    sent_at_utc: datetime | None,
    reason: str,
) -> ExecutionResult:
    """Produce an ExecutionResult for an intent whose dispatch failed.

    The HA service call was attempted but raised an exception, or HA returned
    an error response.  StateGuard.record_action_sent() must NOT be called
    when this result is produced — the cover may not have moved.

    Parameters
    ----------
    intent:
        The CoverIntent that failed.
    error:
        Exception text or HA error description for logging.
    sent_at_utc:
        UTC timestamp when the service call was attempted.  May be None when
        the exception was raised before the call was dispatched.
        Must be timezone-aware when not None.
    reason:
        Human-readable explanation for logging.

    Raises
    ------
    ValueError
        If sent_at_utc is not None and is a naive (timezone-unaware) datetime.
    """
    if sent_at_utc is not None:
        _require_timezone_aware(sent_at_utc)
    return _result_from_intent(
        intent,
        status=ExecutionStatus.FAILED,
        blocked_reason=None,
        error=error,
        sent_at_utc=sent_at_utc,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Aggregation factory
# ---------------------------------------------------------------------------

_SENT_STATUSES: frozenset[ExecutionStatus] = frozenset({
    ExecutionStatus.SENT,
    ExecutionStatus.SUCCEEDED,
})


def build_execution_plan_result(
    window_id: str,
    results: Sequence[ExecutionResult],
) -> ExecutionPlanResult:
    """Aggregate a sequence of ExecutionResults into an ExecutionPlanResult.

    Parameters
    ----------
    window_id:
        The window identifier for the corresponding ExecutionPlan.
    results:
        All ExecutionResult objects for the window's cover entities this cycle.
        An empty sequence produces a plan result with all aggregate flags False.
    """
    results_tuple = tuple(results)
    return ExecutionPlanResult(
        window_id=window_id,
        results=results_tuple,
        any_sent=any(r.status in _SENT_STATUSES for r in results_tuple),
        any_failed=any(r.status is ExecutionStatus.FAILED for r in results_tuple),
        all_blocked=(
            bool(results_tuple)
            and all(r.status is ExecutionStatus.BLOCKED for r in results_tuple)
        ),
        contains_safety_result=any(r.is_safety for r in results_tuple),
    )
