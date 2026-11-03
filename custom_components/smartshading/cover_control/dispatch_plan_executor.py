"""Target-aware Dispatch — Phase 3: isolated DispatchPlanExecutor.

Walks an already fully-built, already-sorted, already-deduplicated
DispatchPlan (Phase 2) and orchestrates the EXISTING dispatch primitives
through injected ports — it computes no shading decision, no
classification, and no ordering of its own.

Lock ownership: this executor never acquires a lock itself.  Locking (the
global admission) is entirely the concern of the ``dispatch_item`` port for its
own single call: the executor calls ``dispatch_item`` and, as a SEPARATE later
call, ``wait_for_completion``; nothing spans an admission across both.  A cover
that travels (or times out) therefore never blocks another zone's dispatch --
including Safety.

Fixed rule (not configurable): 0/100 % items start ``FULL_OPEN_START_INTERVAL_S``
apart and do not wait for the end of travel; intermediate items run strictly one
at a time per group -- completion wait, then ``INTERMEDIATE_POST_COMPLETION_PAUSE_S``
settle -- and a timeout is never a success: the rest of the group does not start.

Pacing is ONE shared state (``NormalPacing``): before every normal start the
executor waits until the earliest permitted start recorded there, and records
what the command it actually sent requires from the next one (start + interval
for an end position; completion + settle for an intermediate position).  The
settle is therefore never slept twice, it also holds after the last cover of a
plan, and a following plan -- of this or of another zone -- starts only after it.
The coordinator passes the integration-wide instance; without one the executor
uses a private one (a single plan on its own).

No Home Assistant import. No coordinator import. No asyncio.Lock owned by
this module. No presence/solar logic. No re-sorting, re-grouping, or
re-deduplication of the plan (Phase 2 is the sole authority for order).
No global plan queue, no hass.data, no background worker, no singleton.
Safety is never dispatched by this module — only preempted.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Protocol

from .dispatch_completion import MAX_TRAVEL_WAIT_S
from .dispatch_plan import DispatchPlan, DispatchPlanItem
from .normal_pacing import NormalPacing
from ..engines.dispatch_classification import DispatchTargetClass

# Fixed, non-configurable pacing constants (T22 design decision — no user
# option, matches the finalized product policy from the design phase).
FULL_OPEN_START_INTERVAL_S = 2.0
INTERMEDIATE_POST_COMPLETION_PAUSE_S = 2.0

_EXECUTABLE_CLASSES = frozenset({
    DispatchTargetClass.FULL_OPEN, DispatchTargetClass.FULL_CLOSE,
    DispatchTargetClass.INTERMEDIATE,
})

# B3-012: FULL_CLOSE (0%) is dispatched exactly like FULL_OPEN already was —
# fire with the fixed start-to-start interval, never wait for completion.
# Reuses the SAME existing fast path (no new timing behavior): before this,
# a 0% target was misclassified INTERMEDIATE and incorrectly ran the full
# completion-wait chain below. B3-013 (not this ticket) is where the actual
# pacing/queue/zone-package rules themselves may change.
_NO_COMPLETION_WAIT_CLASSES = frozenset({
    DispatchTargetClass.FULL_OPEN, DispatchTargetClass.FULL_CLOSE,
})


# ---------------------------------------------------------------------------
# Port contracts — the executor knows nothing about HA, only these shapes.
# ---------------------------------------------------------------------------

class ValidationAction(str, Enum):
    EXECUTE = "execute"
    SKIP = "skip"
    ABORT_ZONE = "abort_zone"
    ABORT_PLAN = "abort_plan"


@dataclass(frozen=True, slots=True)
class ExecutionValidation:
    """Result of the injected live-revalidation port. The executor never
    interprets WHY — that judgment (CommandFilter-equivalent) belongs to
    the caller-supplied ``validate_item`` port, not this module."""

    action: ValidationAction
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """Result of the injected dispatch port for one item."""

    success: bool
    error_type: str | None = None
    attempted: bool | None = None
    """Whether a service call was actually started.  ``None`` (not reported) is
    treated as attempted when the dispatch did not succeed, as before."""
    expected_travel_s: float | None = None
    """Model estimate of the travel that was started (intermediate targets);
    bounds the conservative pacing if the plan ends before the travel is resolved."""


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    """Result of the injected completion-wait port for one item."""

    status: str  # "completed" | "timed_out" | "failed" | "cancelled"
    completion_method: str | None = None
    error_type: str | None = None


class CancellationSignal(Protocol):
    """asyncio.Event's own interface — a real asyncio.Event satisfies this
    Protocol structurally; no global instance is hardcoded anywhere in this
    module, the caller always injects one."""

    def is_set(self) -> bool: ...
    async def wait(self) -> None: ...


DispatchItemPort = Callable[[DispatchPlanItem], Awaitable[DispatchOutcome]]
WaitForCompletionPort = Callable[
    [DispatchPlanItem, "CancellationSignal"], Awaitable[CompletionOutcome]
]
ValidateItemPort = Callable[[DispatchPlanItem], "Awaitable[ExecutionValidation] | ExecutionValidation"]
IsGenerationCurrentPort = Callable[[DispatchPlanItem], bool]
SleepPort = Callable[[float], Awaitable[None]]
ClockPort = Callable[[], float]


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DispatchItemExecutionResult:
    item: DispatchPlanItem
    status: str
    dispatch_started: bool
    completion_method: str | None = None
    reason: str | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchPlanExecutionResult:
    plan_id: str
    status: str
    item_results: tuple[DispatchItemExecutionResult, ...]
    started_at: float
    finished_at: float
    preempted: bool
    abort_reason: str | None = None


async def _race_against_cancellation(awaitable, cancellation: CancellationSignal):
    """Runs ``awaitable`` racing it against ``cancellation.wait()``. Returns
    (result_or_None, was_cancelled). The loser is cancelled and awaited so
    no task leaks. Never swallows a genuine external CancelledError raised
    while awaiting the race itself — that propagates normally."""
    work = asyncio.ensure_future(awaitable)
    watch = asyncio.ensure_future(cancellation.wait())
    try:
        done, pending = await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        work.cancel()
        watch.cancel()
        raise
    for task in pending:
        task.cancel()
    if watch in done:
        return None, True
    # work finished first — surface its exception (if any) to the caller.
    return work.result(), False


class DispatchPlanExecutor:
    """Thin, stateless-between-calls execution orchestrator. Holds no
    coordinator reference, no config entry, no lock, no queue. One instance
    can run any number of plans sequentially (never concurrently — this is
    not itself a scheduler)."""

    async def execute_plan(
        self,
        *,
        plan: DispatchPlan,
        dispatch_item: DispatchItemPort,
        wait_for_completion: WaitForCompletionPort,
        validate_item: ValidateItemPort,
        is_generation_current: IsGenerationCurrentPort,
        cancellation: CancellationSignal,
        sleep: SleepPort,
        clock: ClockPort,
        pacing: NormalPacing | None = None,
    ) -> DispatchPlanExecutionResult:
        pacing = pacing if pacing is not None else NormalPacing()
        try:
            return await self._execute(
                plan=plan, dispatch_item=dispatch_item,
                wait_for_completion=wait_for_completion, validate_item=validate_item,
                is_generation_current=is_generation_current, cancellation=cancellation,
                sleep=sleep, clock=clock, pacing=pacing,
            )
        finally:
            # A travel that was started and never resolved (cancellation, Safety,
            # unload, an error) keeps a bounded conservative boundary; nothing is
            # forgotten just because this plan ended.
            pacing.resolve_pending(clock())

    async def _execute(
        self, *, plan, dispatch_item, wait_for_completion, validate_item,
        is_generation_current, cancellation, sleep, clock, pacing,
    ) -> DispatchPlanExecutionResult:
        started_at = clock()
        results: list[DispatchItemExecutionResult] = []
        aborted_zones: set[str] = set()
        # A5: zones whose last completion timed out.  A timeout is not a success,
        # so the rest of that group is NOT started on the strength of it; the next
        # fresh evaluation decides again.  Other zones are unaffected.
        timeout_zones: set[str] = set()
        abort_plan_reason: str | None = None
        preempted = False

        had_error = False

        for item in plan.items:
            if preempted or abort_plan_reason is not None:
                break

            if item.target_class not in _EXECUTABLE_CLASSES:
                # T22 Phase 3 §4: NO_MOVEMENT/BLOCKED are never dispatched,
                # never validated, never generation-checked — the item's own
                # `reason` (set by Phase 1/2) is passed through purely
                # diagnostically, never reinterpreted.
                results.append(DispatchItemExecutionResult(
                    item=item, status="skipped", dispatch_started=False,
                    reason=item.reason,
                ))
                continue

            if item.zone_id in aborted_zones:
                results.append(DispatchItemExecutionResult(
                    item=item, status="skipped", dispatch_started=False,
                    reason="stale_generation",
                ))
                continue

            if item.zone_id in timeout_zones:
                results.append(DispatchItemExecutionResult(
                    item=item, status="skipped", dispatch_started=False,
                    reason="previous_completion_timed_out",
                ))
                continue

            # --- pacing: wait until the earliest permitted start of a normal
            # command (shared with every other zone) ---
            remaining = pacing.remaining(clock())
            if remaining > 0:
                _, was_preempted = await _race_against_cancellation(sleep(remaining), cancellation)
                if was_preempted:
                    preempted = True
                    break

            if cancellation.is_set():
                preempted = True
                break

            # --- live revalidation: generation first, then validator ---
            if not is_generation_current(item):
                aborted_zones.add(item.zone_id)
                results.append(DispatchItemExecutionResult(
                    item=item, status="skipped", dispatch_started=False,
                    reason="stale_generation",
                ))
                continue

            validation = validate_item(item)
            if asyncio.iscoroutine(validation):
                validation = await validation
            if validation.action is ValidationAction.SKIP:
                results.append(DispatchItemExecutionResult(
                    item=item, status="skipped", dispatch_started=False,
                    reason=validation.reason,
                ))
                continue
            if validation.action is ValidationAction.ABORT_ZONE:
                aborted_zones.add(item.zone_id)
                results.append(DispatchItemExecutionResult(
                    item=item, status="skipped", dispatch_started=False,
                    reason=validation.reason or "abort_zone",
                ))
                continue
            if validation.action is ValidationAction.ABORT_PLAN:
                abort_plan_reason = validation.reason or "abort_plan"
                results.append(DispatchItemExecutionResult(
                    item=item, status="skipped", dispatch_started=False,
                    reason=abort_plan_reason,
                ))
                break

            if cancellation.is_set():
                preempted = True
                break

            # --- dispatch ---
            dispatch_start_time = clock()
            try:
                outcome = await dispatch_item(item)
            except Exception as exc:  # noqa: BLE001 — the port's own reported failure path
                had_error = True
                results.append(DispatchItemExecutionResult(
                    item=item, status="dispatch_failed", dispatch_started=False,
                    error_type=type(exc).__name__,
                ))
                if item.target_class in _NO_COMPLETION_WAIT_CLASSES:
                    pacing.note_endpoint_start(dispatch_start_time, FULL_OPEN_START_INTERVAL_S)
                continue

            if not outcome.success:
                had_error = True
                results.append(DispatchItemExecutionResult(
                    item=item, status="dispatch_failed", dispatch_started=False,
                    error_type=outcome.error_type,
                ))
                # A call that was started but failed still used the radio, so
                # the end-position spacing applies; one that was never started
                # (refused before the send) creates no pacing at all.
                if (
                    item.target_class in _NO_COMPLETION_WAIT_CLASSES
                    and outcome.attempted is not False
                ):
                    pacing.note_endpoint_start(dispatch_start_time, FULL_OPEN_START_INTERVAL_S)
                continue

            if item.target_class in _NO_COMPLETION_WAIT_CLASSES:
                pacing.note_endpoint_start(dispatch_start_time, FULL_OPEN_START_INTERVAL_S)
                results.append(DispatchItemExecutionResult(
                    item=item, status="dispatched", dispatch_started=True,
                ))
                continue

            pacing.begin_travel(
                dispatch_start_time,
                outcome.expected_travel_s if outcome.expected_travel_s is not None
                else MAX_TRAVEL_WAIT_S,
                INTERMEDIATE_POST_COMPLETION_PAUSE_S,
            )

            # --- INTERMEDIATE: wait for completion, then fixed pause ---
            completion, was_preempted = await _race_against_cancellation(
                wait_for_completion(item, cancellation), cancellation,
            )
            if was_preempted:
                preempted = True
                results.append(DispatchItemExecutionResult(
                    item=item, status="preempted", dispatch_started=True,
                ))
                break

            if completion.status == "completed":
                pacing.finish_travel(
                    clock(), INTERMEDIATE_POST_COMPLETION_PAUSE_S, completion.completion_method)
                results.append(DispatchItemExecutionResult(
                    item=item, status="completed", dispatch_started=True,
                    completion_method=completion.completion_method,
                ))
            elif completion.status == "timed_out":
                pacing.finish_travel(clock(), INTERMEDIATE_POST_COMPLETION_PAUSE_S, "timeout")
                timeout_zones.add(item.zone_id)
                results.append(DispatchItemExecutionResult(
                    item=item, status="timed_out", dispatch_started=True,
                    completion_method=completion.completion_method,
                ))
            elif completion.status == "cancelled":
                preempted = True
                results.append(DispatchItemExecutionResult(
                    item=item, status="preempted", dispatch_started=True,
                ))
                break
            else:  # "failed"
                had_error = True
                pacing.finish_travel(clock(), INTERMEDIATE_POST_COMPLETION_PAUSE_S, None)
                results.append(DispatchItemExecutionResult(
                    item=item, status="completion_failed", dispatch_started=True,
                    error_type=completion.error_type,
                ))
            # The settle after this travel is recorded in the shared pacing above
            # (whatever the completion outcome) and enforced before the NEXT
            # normal start -- also across plans and zones, never slept twice.

        finished_at = clock()
        if preempted:
            status = "preempted"
        elif abort_plan_reason is not None:
            status = "aborted"
        elif had_error:
            status = "completed_with_errors"
        else:
            status = "completed"

        return DispatchPlanExecutionResult(
            plan_id=plan.plan_id,
            status=status,
            item_results=tuple(results),
            started_at=started_at,
            finished_at=finished_at,
            preempted=preempted,
            abort_reason=abort_plan_reason,
        )
