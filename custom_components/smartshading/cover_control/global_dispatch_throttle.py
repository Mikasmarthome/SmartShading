"""Global Dispatch Throttle and Serial Dispatch — Step 9G8 / Step 10.

GlobalDispatchThrottle
----------------------
Tracks the timestamp of the last confirmed cover dispatch and computes how
long the next non-safety dispatch must wait to respect the minimum inter-
dispatch gap.  Stateful, not thread/task safe on its own.

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
        if not intent.is_safety:
            wait = self._serial_dispatch.time_until_next_allowed(now)
            if wait.total_seconds() > 0:
                await asyncio.sleep(wait.total_seconds())
        result = await dispatch_cover_intent(hass, intent, now_utc=now)
        if result.status is ExecutionStatus.SENT:
            self._serial_dispatch.record_dispatch(dt_util.utcnow())

The asyncio.Lock is held for the full duration of the sleep + service call.
This means only one cover command is in-flight at any moment, which is the
desired behaviour for Somfy RTS and similar RF-based systems.

Safety behavior
---------------
Safety commands (STORM_SAFE / WIND_SAFE) acquire the lock but skip the
throttle sleep so they reach the cover as fast as possible within the serial
queue.  Safety SENT still updates the throttle clock so subsequent non-safety
commands wait the full interval from the safety dispatch time.

This module has no Home Assistant dependency.  Pure Python.  Testable without HA.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta


DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS: float = 1.0
"""Default minimum time between cover service calls (seconds).

Chosen for Somfy RTS / ESP-Somfy reliability: 1.0 seconds gives a single-
threaded RF gateway enough time to complete one transmission before the next
command arrives.  Deliberately conservative — reliability over throughput.
Coordinator cycles are 5 minutes apart, so any intra-cycle burst is at most
a handful of covers, and the total additional wait is a few seconds.
"""


class GlobalDispatchThrottle:
    """Integration-wide minimum inter-dispatch interval.

    Tracks the timestamp of the most recent confirmed SENT dispatch and
    computes how long the next non-safety dispatch must wait.

    Usage pattern (coordinator dispatch loop)::

        if not intent.is_safety:
            wait = throttle.time_until_next_allowed(dt_util.utcnow())
            if wait.total_seconds() > 0:
                await asyncio.sleep(wait.total_seconds())

        result = await dispatch_cover_intent(hass, intent, now_utc=dt_util.utcnow())

        if result.status is ExecutionStatus.SENT:
            throttle.record_dispatch(dt_util.utcnow())
    """

    def __init__(
        self,
        min_interval: timedelta | None = None,
    ) -> None:
        self._min_interval: timedelta = (
            min_interval
            if min_interval is not None
            else timedelta(seconds=DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS)
        )
        self._last_dispatch_at: datetime | None = None

    @property
    def min_interval(self) -> timedelta:
        """Configured minimum interval between dispatches."""
        return self._min_interval

    @property
    def last_dispatch_at(self) -> datetime | None:
        """UTC timestamp of the most recent recorded dispatch.
        None when no dispatch has been recorded yet."""
        return self._last_dispatch_at

    def time_until_next_allowed(self, now: datetime) -> timedelta:
        """Return the remaining wait before the next dispatch is allowed.

        Returns ``timedelta(0)`` when:

        - No dispatch has been recorded yet (first dispatch is always immediate).
        - The minimum interval has fully elapsed since the last dispatch.

        Returns a positive ``timedelta`` when the minimum interval has not yet
        elapsed and the caller must wait before dispatching.

        Parameters
        ----------
        now:
            Current UTC timestamp.  Must be timezone-aware.
        """
        if self._last_dispatch_at is None:
            return timedelta(0)
        elapsed = now - self._last_dispatch_at
        remaining = self._min_interval - elapsed
        return remaining if remaining > timedelta(0) else timedelta(0)

    def record_dispatch(self, now: datetime) -> None:
        """Record that a dispatch occurred at *now*.

        Must be called after every SENT result — for both safety and non-safety
        commands.  BLOCKED, NOT_ATTEMPTED, and FAILED results must NOT call
        this method, as no service call was confirmed sent.

        Parameters
        ----------
        now:
            UTC timestamp of the dispatch.  Must be timezone-aware.
        """
        self._last_dispatch_at = now


class GlobalSerialDispatch:
    """Integration-wide serial cover dispatch: asyncio.Lock + GlobalDispatchThrottle.

    A single instance must be shared across ALL SmartShading zone coordinators
    (stored in hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]) so cover service calls
    from different zones are serialised.

    See module docstring for correct usage.
    """

    def __init__(self, min_interval: timedelta | None = None) -> None:
        # asyncio.Lock must be created inside the running event loop.
        # Create it lazily on first access, or call ensure_lock() from an
        # async context during setup.
        self._lock: asyncio.Lock | None = None
        self._throttle: GlobalDispatchThrottle = GlobalDispatchThrottle(min_interval)

    @property
    def lock(self) -> asyncio.Lock:
        """Return the asyncio.Lock, creating it if needed.

        The Lock is always accessed from an async context (coordinator dispatch
        loop), so the event loop is always running at access time.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def time_until_next_allowed(self, now: datetime) -> timedelta:
        """Return the remaining wait before the next dispatch is allowed."""
        return self._throttle.time_until_next_allowed(now)

    def record_dispatch(self, now: datetime) -> None:
        """Record a confirmed SENT dispatch at *now*."""
        self._throttle.record_dispatch(now)

    @property
    def min_interval(self) -> timedelta:
        """Configured minimum interval between dispatches."""
        return self._throttle.min_interval

    @property
    def last_dispatch_at(self) -> datetime | None:
        """UTC timestamp of the most recent confirmed dispatch."""
        return self._throttle.last_dispatch_at
