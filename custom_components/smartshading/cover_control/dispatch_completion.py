"""Tiered travel-completion detection for SEQUENTIAL dispatch mode
(v1.2.0-beta.1, T11).

Only ever consulted when DispatchConfig.mode is SEQUENTIAL — PARALLEL and
SPACED never call this module.

Tiered strategy, most-trustworthy signal first:
  1. Reliable position feedback: poll the cover's real HA state; completed
     once idle (not opening/closing) AND within position_tolerance of the
     target. Never assumed — an unreliable-feedback cover's raw state is not
     trustworthy enough to poll (same reasoning as the existing own-command
     guard's unreliable-feedback handling in engines/override_detector.py:
     a stale/misleading reported position must not drive logic).
  2. No reliable feedback: reuse the per-cover travel_time_open_s /
     travel_time_close_s already modeled in cover_capabilities.py (the same
     numbers cover_control/travel_tracker.py's estimator uses), scaled by
     the fraction of the 0-100 range actually being crossed when the start
     position is known, and simply wait that long.
  3. Timeout: both tiers are bounded by max_travel_wait_s — a cover that
     never reports completion (tier 1) or whose estimate somehow exceeds the
     bound (tier 2) is not waited on forever; the queue continues.

No busy-wait: uses asyncio.sleep(poll_interval_s) between checks (tier 1)
or a single bounded sleep (tier 2) — never a tight loop.

Trust model (A5): a reported target position is NOT proof of a finished travel --
some integrations set the attribute to the target the moment the command is
accepted while the motor still runs.  For covers with feedback the end of travel
only counts as observed ("position" / "movement_state") when real travel was seen
before it: a moving state (opening/closing) or a position strictly between start
and target.  If the target shows up without any observed travel (a jump), the
distance/direction-scaled travel-time estimate must elapse first and the result is
"combined" (never "position").  A cover that already was at the target needs no
wait ("already_at_target").  Covers without usable feedback use the estimate alone
("time_estimate").  The fixed settle pause that follows every intermediate travel
is applied by the dispatch executor.  A timeout is never a completion.

Abortable (A2): a caller may pass ``cancellation`` (anything with the
asyncio.Event interface) and/or ``abort_check`` (returns None while the wait
is still wanted, else the reason it is not).  Every sleep is then raced
against the cancellation, and the check runs before the first poll and after
every wake-up, so a wait never outlives a safety preemption, a superseded
plan or an unload by more than the in-flight state read.  Without those
inputs the behaviour is exactly the original one.  The result's ``status``
tells COMPLETED / TIMED_OUT / CANCELLED / STALE / UNLOADED apart; only
COMPLETED means the travel was actually seen to finish.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from .cover_entity_snapshot import build_cover_entity_snapshot

DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_POSITION_TOLERANCE = 5

MonoClock = Callable[[], float]
Sleeper = Callable[[float], "asyncio.Future"]


class CompletionMethod(Enum):
    POSITION = "position"
    MOVEMENT_STATE = "movement_state"
    TIME_ESTIMATE = "time_estimate"
    TIMEOUT = "timeout"
    ABORTED = "aborted"
    # A5: the position/state reported the target (idle) but no travel was ever
    # observed -- a "jump".  The finish is then the estimated travel time, so the
    # method names the combination and never claims a real observed end of travel.
    COMBINED = "combined"
    ALREADY_AT_TARGET = "already_at_target"


class CompletionStatus(Enum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    STALE = "stale"
    UNLOADED = "unloaded"


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...
    async def wait(self) -> None: ...


class AnyOfSignals:
    """Fires when ANY of the wrapped signals fires.  ``fired`` names the first
    wrapped signal (by label) that was observed set.  Satisfies the
    CancellationSignal protocol; ``wait()`` leaves no task behind."""

    def __init__(self, **signals: "CancellationSignal") -> None:
        self._signals = signals

    @property
    def fired(self) -> str | None:
        for label, sig in self._signals.items():
            if sig.is_set():
                return label
        return None

    def is_set(self) -> bool:
        return self.fired is not None

    async def wait(self) -> None:
        if self.is_set():
            return
        tasks = [asyncio.ensure_future(sig.wait()) for sig in self._signals.values()]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


AbortCheck = Callable[[], "tuple[CompletionStatus, str] | None"]


@dataclass(frozen=True)
class CompletionResult:
    method: CompletionMethod
    elapsed_s: float
    timed_out: bool
    status: CompletionStatus | None = None
    cancel_reason: str | None = None
    expected_s: float | None = None
    """The travel-time estimate this wait was planned with (scaled by distance and
    direction); None when the wait never needed one."""
    movement_observed: bool | None = None
    """True when real travel was seen (movement state or an intermediate position);
    False when the target was reported without any observed travel (a jump);
    None for waits that never watch the cover (no feedback)."""

    def __post_init__(self) -> None:
        if self.status is None:
            object.__setattr__(
                self, "status",
                CompletionStatus.TIMED_OUT if self.timed_out else CompletionStatus.COMPLETED,
            )

    @property
    def completed(self) -> bool:
        """True only when the travel was actually seen to finish."""
        return self.status is CompletionStatus.COMPLETED


async def _sleep_or_abort(sleep, seconds: float, cancellation: CancellationSignal | None) -> None:
    """``sleep(seconds)``, ended early when ``cancellation`` fires.  The loser
    is always cancelled and awaited, so no task outlives the call; an external
    CancelledError propagates normally."""
    if cancellation is None:
        await sleep(seconds)
        return
    if cancellation.is_set():
        return
    work = asyncio.ensure_future(sleep(seconds))
    watch = asyncio.ensure_future(cancellation.wait())
    try:
        await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (work, watch):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watch, return_exceptions=True)
    if work.done() and not work.cancelled() and work.exception() is not None:
        raise work.exception()


def estimate_travel_duration_s(
    *,
    start_position_ha: int | None,
    target_position_ha: int,
    travel_time_open_s: float,
    travel_time_close_s: float,
) -> float:
    """Seconds needed to travel from start to target, scaled by the fraction
    of the 0-100 range actually crossed. Falls back to the longer of the two
    directional durations when the start position is unknown — never
    under-estimate (and therefore never under-wait) when a real delta can't
    be computed."""
    if start_position_ha is None:
        return max(travel_time_open_s, travel_time_close_s)
    delta = abs(target_position_ha - start_position_ha)
    if delta <= 0:
        return 0.0
    direction_duration = (
        travel_time_open_s if target_position_ha > start_position_ha else travel_time_close_s
    )
    return direction_duration * (delta / 100.0)


def evaluate_travel_status(
    *,
    state: str | None,
    attributes: dict,
    invert: bool,
    has_reliable_position_feedback: bool,
    target_position_ha: int,
    position_tolerance: int = DEFAULT_POSITION_TOLERANCE,
) -> CompletionMethod | None:
    """Pure judgment: has this cover finished the current travel, per the
    most trustworthy signal available? Returns None while still in progress
    (or the entity is unavailable — never treated as "done")."""
    snap = build_cover_entity_snapshot(
        entity_id="_dispatch_completion_check",
        state=state,
        attributes=attributes,
        invert=invert,
        has_reliable_position_feedback=has_reliable_position_feedback,
    )
    if not snap.available or snap.is_moving:
        return None
    if has_reliable_position_feedback and snap.current_position_ha is not None:
        if abs(snap.current_position_ha - target_position_ha) <= position_tolerance:
            return CompletionMethod.POSITION
        return None
    if has_reliable_position_feedback:
        # Reliable feedback but position attribute absent this cycle (some
        # integrations omit it briefly) — idle state alone is still a real
        # signal for these covers.
        return CompletionMethod.MOVEMENT_STATE
    # No reliable feedback at all: real polled state is not trustworthy
    # enough to call "done" from — the time-estimate tier handles this case
    # entirely without ever reaching here (see wait_for_travel_completion).
    return None


async def wait_for_travel_completion(
    hass,
    *,
    entity_id: str,
    target_position_ha: int,
    invert_position: bool,
    has_reliable_position_feedback: bool,
    start_position_ha: int | None,
    travel_time_open_s: float,
    travel_time_close_s: float,
    max_wait_s: float,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    position_tolerance: int = DEFAULT_POSITION_TOLERANCE,
    mono_clock: MonoClock = time.monotonic,
    sleep=asyncio.sleep,
    cancellation: CancellationSignal | None = None,
    abort_check: AbortCheck | None = None,
) -> CompletionResult:
    """Wait for one cover's travel to complete, tiered per the module
    docstring. Always returns (never raises) — a timeout is a normal,
    expected outcome the caller (coordinator.py) records in diagnostics and
    continues the queue from, per the T11 spec's "robust: document and
    continue" default."""
    started = mono_clock()

    def _aborted() -> CompletionResult | None:
        verdict = abort_check() if abort_check is not None else None
        if verdict is None and cancellation is not None and cancellation.is_set():
            verdict = (CompletionStatus.CANCELLED, "cancellation_signal")
        if verdict is None:
            return None
        status, reason = verdict
        return CompletionResult(
            CompletionMethod.ABORTED, mono_clock() - started, False,
            status=status, cancel_reason=reason,
        )

    pre = _aborted()
    if pre is not None:
        return pre
    estimated_s = estimate_travel_duration_s(
        start_position_ha=start_position_ha,
        target_position_ha=target_position_ha,
        travel_time_open_s=travel_time_open_s,
        travel_time_close_s=travel_time_close_s,
    )

    if has_reliable_position_feedback:
        saw_moving = False
        saw_intermediate = False
        while True:
            elapsed = mono_clock() - started
            if elapsed >= max_wait_s:
                return CompletionResult(
                    CompletionMethod.TIMEOUT, elapsed, True,
                    expected_s=estimated_s, movement_observed=saw_moving or saw_intermediate)
            entity_state = hass.states.get(entity_id)
            state = entity_state.state if entity_state is not None else None
            attributes = entity_state.attributes if entity_state is not None else {}
            snap = build_cover_entity_snapshot(
                entity_id="_dispatch_completion_check", state=state, attributes=attributes,
                invert=invert_position, has_reliable_position_feedback=True)
            if snap.available:
                if snap.is_moving:
                    saw_moving = True
                pos = snap.current_position_ha
                if (
                    pos is not None
                    and abs(pos - target_position_ha) > position_tolerance
                    and (start_position_ha is None or abs(pos - start_position_ha) > position_tolerance)
                ):
                    saw_intermediate = True
            method = evaluate_travel_status(
                state=state,
                attributes=attributes,
                invert=invert_position,
                has_reliable_position_feedback=True,
                target_position_ha=target_position_ha,
                position_tolerance=position_tolerance,
            )
            if method is not None:
                now_elapsed = mono_clock() - started
                if saw_moving or saw_intermediate:
                    return CompletionResult(
                        method, now_elapsed, False, expected_s=estimated_s, movement_observed=True)
                # Target reported, but no travel was ever observed: a jump.
                if estimated_s <= 0:
                    return CompletionResult(
                        CompletionMethod.ALREADY_AT_TARGET, now_elapsed, False,
                        expected_s=estimated_s, movement_observed=False)
                if now_elapsed >= estimated_s:
                    return CompletionResult(
                        CompletionMethod.COMBINED, now_elapsed, False,
                        expected_s=estimated_s, movement_observed=False)
            remaining = max_wait_s - (mono_clock() - started)
            step = min(poll_interval_s, max(0.0, remaining))
            if method is not None:           # waiting out the estimate after a jump
                step = min(step, max(0.0, estimated_s - (mono_clock() - started)) or step)
            await _sleep_or_abort(sleep, step, cancellation)
            aborted = _aborted()
            if aborted is not None:
                return aborted

    # No reliable feedback: a single bounded wait for the estimated duration
    # — never poll real state for these covers (tier 2/3 of the module
    # docstring).
    wait_s = min(estimated_s, max_wait_s) if estimated_s > 0 else 0.0
    if wait_s > 0:
        await _sleep_or_abort(sleep, wait_s, cancellation)
        aborted = _aborted()
        if aborted is not None:
            return aborted
    timed_out = estimated_s > max_wait_s
    if timed_out:
        method = CompletionMethod.TIMEOUT
    elif estimated_s <= 0:
        method = CompletionMethod.ALREADY_AT_TARGET
    else:
        method = CompletionMethod.TIME_ESTIMATE
    return CompletionResult(method, mono_clock() - started, timed_out, expected_s=estimated_s)
