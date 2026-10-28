"""Global Dispatch Throttle and Serial Dispatch — Step 9G8 / Step 10.

GlobalDispatchThrottle
----------------------
Tracks the last confirmed cover dispatch and computes how long the next
dispatch must wait to respect the minimum inter-dispatch gap.  Uses an
injectable monotonic clock (defaults to ``time.monotonic``) so elapsed-time
measurement is immune to wall-clock adjustments (NTP, timezone changes).

GlobalSerialDispatch
--------------------
Combines an asyncio.Lock with a GlobalDispatchThrottle.  Must be shared
across ALL zone coordinators so cover service calls from different zones are
serialised in addition to being throttled.

  Lock purpose:     Only one coordinator may dispatch at a time.  Without the
                    lock two coordinators can call time_until_next_allowed()
                    simultaneously, both see "wait=0", and both dispatch at the
                    same time — a burst.  The lock prevents that.

  Throttle purpose: Even with the lock, the lock holder must still sleep until
                    the minimum inter-dispatch interval has elapsed since the
                    *previous* dispatch (which may have come from a different
                    coordinator zone).

Correct usage in the coordinator dispatch loop::

    async with self._serial_dispatch.lock:
        wait = self._serial_dispatch.time_until_next_allowed()
        if wait.total_seconds() > 0:
            await asyncio.sleep(wait.total_seconds())
        # stale-intent guard (non-safety only)
        if not intent.is_safety:
            if generation_changed:
                continue
        result = await dispatch_cover_intent(hass, intent, now_utc=dt_util.utcnow())
        if result.status is ExecutionStatus.SENT:
            self._serial_dispatch.record_dispatch(dt_util.utcnow())

The asyncio.Lock is held for the full duration of the sleep + service call.
This means only one cover command is in-flight at any moment, which is the
desired behaviour for Somfy RTS and similar RF-based systems.

Safety behavior
---------------
Admission is priority-aware (``PriorityAdmission``): a Safety request
(STORM_SAFE / WIND_SAFE / RAIN_SAFE) is admitted before every normal request
that has not been admitted yet, independent of arrival order; among
themselves Safety requests and normal requests are admitted strictly in
arrival order.  Safety still waits for the throttle like every other command
-- the minimum interval is a timing gate, not a queue position -- and a
request that is already admitted is never revoked.  Safety is exempt from
stale-intent cancellation (it always dispatches even if the generation
changed).

A normal request that is admitted must not hold the admission through a long
wait while Safety queues up: waits performed under admission end as soon as
``safety_waiting`` is true (see ``safety_waiting_event``), and normal plans of
other zones that are waiting outside the admission are ended by the
integration-wide ``safety_epoch`` signal (see ``SafetyEpochSignal``).

Monotonic clock
---------------
Elapsed time is measured with a monotonic clock (``time.monotonic`` by
default) so NTP adjustments and wall-clock jumps cannot shorten the interval.
The ``last_dispatch_at`` property still returns a wall-clock ``datetime`` for
human-readable diagnostics.

This module has no Home Assistant dependency.  Pure Python.  Testable without HA.
"""
from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable


DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS: float = 2.0
"""Default minimum time between cover service calls (seconds).

Chosen for Somfy RTS / ESP-Somfy reliability: 2.0 seconds gives a single-
threaded RF gateway enough time to complete one transmission before the next
command arrives (F32 field fix — raised from 1.5s after a same-second RF
collision report).  Deliberately conservative — reliability over throughput.
Coordinator cycles are 5 minutes apart, so any intra-cycle burst is at most
a handful of covers, and the total additional wait is a few seconds.
"""

MonoClock = Callable[[], float]
"""Type alias for an injectable monotonic clock function (returns float seconds)."""


class GlobalDispatchThrottle:
    """Integration-wide minimum inter-dispatch interval.

    Tracks the monotonic timestamp of the most recent confirmed SENT dispatch
    and computes how long the next dispatch must wait.

    The wall-clock ``last_dispatch_at`` is kept separately for diagnostics only
    and is never used for elapsed-time calculations.

    Usage pattern (coordinator dispatch loop)::

        wait = throttle.time_until_next_allowed()
        if wait.total_seconds() > 0:
            await asyncio.sleep(wait.total_seconds())

        result = await dispatch_cover_intent(hass, intent, now_utc=dt_util.utcnow())

        if result.status is ExecutionStatus.SENT:
            throttle.record_dispatch(dt_util.utcnow())
    """

    def __init__(
        self,
        min_interval: timedelta | None = None,
        mono_clock: MonoClock | None = None,
    ) -> None:
        self._min_interval: timedelta = (
            min_interval
            if min_interval is not None
            else timedelta(seconds=DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS)
        )
        self._mono_clock: MonoClock = (
            mono_clock if mono_clock is not None else time.monotonic
        )
        self._last_dispatch_mono: float | None = None
        self._last_dispatch_at: datetime | None = None

    @property
    def min_interval(self) -> timedelta:
        """Configured minimum interval between dispatches."""
        return self._min_interval

    @property
    def last_dispatch_at(self) -> datetime | None:
        """Wall-clock UTC timestamp of the most recent recorded dispatch.
        None when no dispatch has been recorded yet.  For diagnostics only —
        not used for elapsed-time calculation."""
        return self._last_dispatch_at

    def time_until_next_allowed(
        self, min_interval_override: timedelta | None = None
    ) -> timedelta:
        """Return the remaining wait before the next dispatch is allowed.

        Uses the injectable monotonic clock so the result is immune to
        wall-clock jumps.

        Returns ``timedelta(0)`` when:

        - No dispatch has been recorded yet (first dispatch is always immediate).
        - The minimum interval has fully elapsed since the last dispatch.

        Returns a positive ``timedelta`` when the minimum interval has not yet
        elapsed and the caller must sleep before dispatching.

        min_interval_override (v1.2.0-beta.1, T11): when given, used instead
        of the interval configured at construction — lets the caller apply a
        per-dispatch interval computed from the configured DispatchMode
        (cover_control/dispatch_orchestrator.py) without needing a
        differently-configured GlobalDispatchThrottle instance per mode.
        The tracked last-dispatch timestamp (shared, cross-zone) is
        unaffected either way.
        """
        if self._last_dispatch_mono is None:
            return timedelta(0)
        effective_interval = (
            min_interval_override if min_interval_override is not None else self._min_interval
        )
        elapsed = self._mono_clock() - self._last_dispatch_mono
        remaining = effective_interval.total_seconds() - elapsed
        return timedelta(seconds=remaining) if remaining > 0 else timedelta(0)

    def record_dispatch(self, now: datetime) -> None:
        """Record that a dispatch occurred.

        Must be called after every SENT result — for both safety and non-safety
        commands.  BLOCKED, NOT_ATTEMPTED, and FAILED results must NOT call
        this method, as no service call was confirmed sent.

        Parameters
        ----------
        now:
            Wall-clock UTC timestamp of the dispatch.  Stored in
            ``last_dispatch_at`` for diagnostics.  Elapsed time is always
            measured with the injected monotonic clock, not from this value.
        """
        self._last_dispatch_mono = self._mono_clock()
        self._last_dispatch_at = now

ADMISSION_SAFETY: int = 0
"""Admission priority of a Safety request (lower value = admitted first)."""
ADMISSION_NORMAL: int = 1
"""Admission priority of every other request."""


@dataclass(order=True)
class _Waiter:
    """One queued admission request.  Ordered by (priority, arrival)."""

    priority: int
    seq: int
    future: asyncio.Future = field(compare=False)
    queued_mono: float = field(compare=False, default=0.0)


@dataclass(frozen=True, slots=True)
class AdmissionInfo:
    """What one admission request experienced (diagnostics only)."""

    priority: int
    wait_ms: float
    overtaken: int
    """How many normal requests that arrived earlier were admitted AFTER this
    (Safety) request -- 0 for normal requests."""


class SafetyEpochSignal:
    """Edge-triggered, integration-wide "a Safety request entered admission
    after this signal was created" signal.

    Created by a normal plan when it starts; ``is_set()`` turns true -- and
    ``wait()`` returns -- as soon as ANY zone's Safety request enters the
    admission afterwards.  A plan that starts later is not affected by an
    earlier Safety request, so an aborted plan being re-evaluated does not
    abort itself again.  Satisfies the executor's ``CancellationSignal``.
    """

    def __init__(self, admission: "PriorityAdmission") -> None:
        self._admission = admission
        self._since = admission.safety_epoch

    def is_set(self) -> bool:
        return self._admission.safety_epoch != self._since

    async def wait(self) -> None:
        while not self.is_set():
            await self._admission._wait_for_epoch_change()


class PriorityAdmission:
    """Integration-wide serial admission with an explicit priority queue.

    API-compatible with the ``asyncio.Lock`` it replaces (``acquire`` /
    ``release`` / ``locked`` / ``async with``), so every existing user keeps
    working; a bare ``acquire()`` is a NORMAL request.

    Invariants
    ----------
    * At most one holder.  Ownership is handed from ``release()`` straight to
      the best queued waiter -- the admission never becomes free in between,
      so a newcomer cannot barge past a queued request.
    * The next holder is the waiter with the smallest ``(priority, arrival)``:
      Safety before every not-yet-admitted normal request regardless of
      arrival order; FIFO within one priority class.  Nothing here ranks
      Safety kinds against each other.
    * A waiter cancelled while queued is removed immediately.  A waiter that
      was handed the admission but is cancelled before it resumed passes the
      admission on, so ownership is never leaked.
    * Normal requests cannot starve: only a Safety request ever overtakes
      them, and a Safety request is bounded by its own dispatch.
    """

    def __init__(self, mono_clock: Callable[[], float] | None = None) -> None:
        self._mono: Callable[[], float] = mono_clock or time.monotonic
        self._held = False
        self._holder_priority: int | None = None
        self._queue: list[_Waiter] = []
        self._seq = 0
        self._safety_epoch = 0
        self._epoch_waiters: list[asyncio.Future] = []
        self._safety_waiting_event: asyncio.Event | None = None
        # Counters (diagnostics)
        self._admitted_total = 0
        self._admitted_safety = 0
        self._safety_overtook_total = 0
        self._cancelled_waiting_total = 0
        self._last_info: AdmissionInfo | None = None

    # -- lock-compatible surface ------------------------------------------

    def locked(self) -> bool:
        return self._held

    async def acquire(self, priority: int = ADMISSION_NORMAL) -> bool:
        return (await self.acquire_with_info(priority)) is not None

    async def acquire_with_info(self, priority: int = ADMISSION_NORMAL) -> AdmissionInfo:
        started = self._mono()
        if priority == ADMISSION_SAFETY:
            self._safety_epoch += 1
            self._notify_epoch()
        self._seq += 1
        waiter = _Waiter(priority, self._seq, None)  # type: ignore[arg-type]
        if not self._held and not self._queue:
            self._held = True
            self._holder_priority = priority
            return self._admitted(waiter, started, overtaken=0)
        loop = asyncio.get_running_loop()
        waiter.future = loop.create_future()
        waiter.queued_mono = started
        heapq.heappush(self._queue, waiter)
        self._sync_safety_waiting()
        try:
            await waiter.future
        except asyncio.CancelledError:
            if waiter.future.done() and not waiter.future.cancelled():
                # Handed the admission and cancelled before resuming: pass it on.
                self.release()
            else:
                self._remove(waiter)
                self._cancelled_waiting_total += 1
            raise
        overtaken = 0
        if priority == ADMISSION_SAFETY:
            overtaken = waiter.future.result()
        return self._admitted(waiter, started, overtaken=overtaken)

    def release(self) -> None:
        if not self._held:
            raise RuntimeError("PriorityAdmission is not held")
        self._held = False
        self._holder_priority = None
        self._grant_next()

    async def __aenter__(self) -> "PriorityAdmission":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()

    def admit(self, *, safety: bool) -> "_Admission":
        """Async context manager that admits a request of the given class and
        exposes its :class:`AdmissionInfo` as ``.info`` once entered."""
        return _Admission(self, ADMISSION_SAFETY if safety else ADMISSION_NORMAL)

    # -- signals ------------------------------------------------------------

    @property
    def safety_epoch(self) -> int:
        """Counts Safety requests that ever entered admission (monotonic)."""
        return self._safety_epoch

    @property
    def safety_waiting(self) -> bool:
        """True while at least one Safety request is queued (not yet admitted)."""
        return any(w.priority == ADMISSION_SAFETY for w in self._queue)

    @property
    def safety_waiting_event(self) -> asyncio.Event:
        """Set while ``safety_waiting`` is true.  Created lazily inside the loop."""
        if self._safety_waiting_event is None:
            self._safety_waiting_event = asyncio.Event()
            self._sync_safety_waiting()
        return self._safety_waiting_event

    # -- diagnostics ----------------------------------------------------------

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    @property
    def queued_safety_count(self) -> int:
        return sum(1 for w in self._queue if w.priority == ADMISSION_SAFETY)

    @property
    def holder_priority(self) -> int | None:
        return self._holder_priority

    @property
    def last_info(self) -> AdmissionInfo | None:
        return self._last_info

    def stats(self) -> dict:
        return {
            "held": self._held,
            "holder_is_safety": self._holder_priority == ADMISSION_SAFETY,
            "queued": len(self._queue),
            "queued_safety": self.queued_safety_count,
            "admitted_total": self._admitted_total,
            "admitted_safety": self._admitted_safety,
            "safety_overtook_total": self._safety_overtook_total,
            "cancelled_while_waiting_total": self._cancelled_waiting_total,
            "safety_epoch": self._safety_epoch,
        }

    # -- internals ------------------------------------------------------------

    def _admitted(self, waiter: _Waiter, started: float, *, overtaken: int) -> AdmissionInfo:
        info = AdmissionInfo(
            priority=waiter.priority,
            wait_ms=max(0.0, (self._mono() - started) * 1000.0),
            overtaken=overtaken,
        )
        self._admitted_total += 1
        if waiter.priority == ADMISSION_SAFETY:
            self._admitted_safety += 1
            self._safety_overtook_total += overtaken
        self._last_info = info
        return info

    def _grant_next(self) -> None:
        while self._queue:
            waiter = heapq.heappop(self._queue)
            if waiter.future.done():          # cancelled while queued
                continue
            self._held = True
            self._holder_priority = waiter.priority
            overtaken = 0
            if waiter.priority == ADMISSION_SAFETY:
                overtaken = sum(
                    1 for w in self._queue
                    if w.priority == ADMISSION_NORMAL and w.seq < waiter.seq
                )
            waiter.future.set_result(overtaken)
            break
        self._sync_safety_waiting()

    def _remove(self, waiter: _Waiter) -> None:
        try:
            self._queue.remove(waiter)
        except ValueError:
            return
        heapq.heapify(self._queue)
        self._sync_safety_waiting()

    def _sync_safety_waiting(self) -> None:
        ev = self._safety_waiting_event
        if ev is None:
            return
        if self.safety_waiting:
            ev.set()
        else:
            ev.clear()

    def _notify_epoch(self) -> None:
        waiters, self._epoch_waiters = self._epoch_waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    async def _wait_for_epoch_change(self) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._epoch_waiters.append(fut)
        try:
            await fut
        finally:
            try:
                self._epoch_waiters.remove(fut)
            except ValueError:
                pass


class _Admission:
    """``async with admission.admit(safety=...) as adm`` -> ``adm.info``."""

    def __init__(self, admission: PriorityAdmission, priority: int) -> None:
        self._admission = admission
        self._priority = priority
        self.info: AdmissionInfo | None = None

    async def __aenter__(self) -> "_Admission":
        self.info = await self._admission.acquire_with_info(self._priority)
        return self

    async def __aexit__(self, *exc) -> None:
        self._admission.release()


class GlobalSerialDispatch:
    """Integration-wide serial cover dispatch: PriorityAdmission + GlobalDispatchThrottle.

    A single instance must be shared across ALL SmartShading zone coordinators
    (stored in hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]) so cover service calls
    from different zones are serialised.

    See module docstring for correct usage.
    """

    def __init__(
        self,
        min_interval: timedelta | None = None,
        mono_clock: MonoClock | None = None,
    ) -> None:
        # The admission holds no loop-bound object until a waiter queues, so it
        # can be created eagerly; it survives config-entry reloads with this
        # (shared) instance.
        self._admission: PriorityAdmission = PriorityAdmission()
        self._throttle: GlobalDispatchThrottle = GlobalDispatchThrottle(
            min_interval, mono_clock
        )

    @property
    def lock(self) -> PriorityAdmission:
        """The admission, usable like the asyncio.Lock it replaces (a bare
        ``async with`` is a NORMAL request)."""
        return self._admission

    @property
    def admission(self) -> PriorityAdmission:
        return self._admission

    def admit(self, *, safety: bool) -> "_Admission":
        """Admit one request; Safety is selected before all queued normals."""
        return self._admission.admit(safety=safety)

    def safety_epoch_signal(self) -> SafetyEpochSignal:
        """A signal that fires when any zone's Safety enters admission from now on."""
        return SafetyEpochSignal(self._admission)

    def time_until_next_allowed(
        self, min_interval_override: timedelta | None = None
    ) -> timedelta:
        """Return the remaining wait before the next dispatch is allowed.

        min_interval_override: see GlobalDispatchThrottle.time_until_next_allowed().
        """
        return self._throttle.time_until_next_allowed(min_interval_override)

    def record_dispatch(self, now: datetime) -> None:
        """Record a confirmed SENT dispatch."""
        self._throttle.record_dispatch(now)

    @property
    def min_interval(self) -> timedelta:
        """Configured minimum interval between dispatches."""
        return self._throttle.min_interval

    @property
    def last_dispatch_at(self) -> datetime | None:
        """Wall-clock UTC timestamp of the most recent confirmed dispatch."""
        return self._throttle.last_dispatch_at
