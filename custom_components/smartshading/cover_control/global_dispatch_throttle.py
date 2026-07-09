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
Safety commands (STORM_SAFE / WIND_SAFE) acquire the lock and wait for the
throttle exactly like non-safety commands — the minimum interval
(DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS) is always enforced.  Safety's queue
priority means it is exempt from stale-intent
cancellation (it always dispatches even if the generation changed), but it
never bypasses the timing gate.

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
import time
from datetime import datetime, timedelta
from typing import Callable


DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS: float = 1.5
"""Default minimum time between cover service calls (seconds).

Chosen for Somfy RTS / ESP-Somfy reliability: 1.5 seconds gives a single-
threaded RF gateway enough time to complete one transmission before the next
command arrives.  Deliberately conservative — reliability over throughput.
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

    def time_until_next_allowed(self) -> timedelta:
        """Return the remaining wait before the next dispatch is allowed.

        Uses the injectable monotonic clock so the result is immune to
        wall-clock jumps.

        Returns ``timedelta(0)`` when:

        - No dispatch has been recorded yet (first dispatch is always immediate).
        - The minimum interval has fully elapsed since the last dispatch.

        Returns a positive ``timedelta`` when the minimum interval has not yet
        elapsed and the caller must sleep before dispatching.
        """
        if self._last_dispatch_mono is None:
            return timedelta(0)
        elapsed = self._mono_clock() - self._last_dispatch_mono
        remaining = self._min_interval.total_seconds() - elapsed
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


class GlobalSerialDispatch:
    """Integration-wide serial cover dispatch: asyncio.Lock + GlobalDispatchThrottle.

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
        # asyncio.Lock must be created inside the running event loop.
        # Create it lazily on first access, or call ensure_lock() from an
        # async context during setup.
        self._lock: asyncio.Lock | None = None
        self._throttle: GlobalDispatchThrottle = GlobalDispatchThrottle(
            min_interval, mono_clock
        )

    @property
    def lock(self) -> asyncio.Lock:
        """Return the asyncio.Lock, creating it if needed.

        The Lock is always accessed from an async context (coordinator dispatch
        loop), so the event loop is always running at access time.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def time_until_next_allowed(self) -> timedelta:
        """Return the remaining wait before the next dispatch is allowed."""
        return self._throttle.time_until_next_allowed()

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
