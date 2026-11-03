"""Shared pacing of NORMAL cover travel -- pure Python, no Home Assistant.

One instance lives in :class:`GlobalSerialDispatch`; the plan executor reads and
updates it through the narrow methods below.  It holds only the earliest permitted
start of the next normal command and how that boundary came about.  Safety never
reads it.
"""
from __future__ import annotations


BASIS_ENDPOINT_START = "endpoint_start"
BASIS_OBSERVED = "observed"
BASIS_ESTIMATED = "estimated"
BASIS_TIMEOUT = "timeout_unconfirmed"
BASIS_FAILED = "completion_failed"
BASIS_UNCONFIRMED = "unconfirmed_travel_estimate"

_OBSERVED_METHODS = frozenset({"position", "movement_state"})
_ESTIMATED_METHODS = frozenset({"time_estimate", "combined", "already_at_target"})


class NormalPacing:
    """Earliest permitted start of the next NORMAL cover command.

    All times are monotonic seconds supplied by the caller (one clock for the
    whole plan), so a controlled clock drives it in tests.  The state outlives
    one package: the first command of the next zone waits for what the previous
    zone left behind.  Safety never reads it.

    * end position (0/100 %): the next start is ``interval`` after this start; no
      completion is awaited and none is claimed.
    * intermediate position: the next start is ``settle`` after the travel end.
      The end is *confirmed* (position/movement reported), *estimated* (derived
      from the travel-time model) or a *timeout* (no confirmation at all); the
      basis is recorded.  If the package ends while a travel is still unresolved
      (cancellation, replacement, unload), the pacing keeps a bounded,
      conservative boundary: the estimated end of that travel plus the settle.
      That is a model value, not a proof that the motor has stopped.
    """

    def __init__(self) -> None:
        self._not_before: float = 0.0
        self._basis: str | None = None
        self._pending: tuple[float, float] | None = None   # (expected_end, settle_s)
        self._resolved_unconfirmed_total = 0

    def remaining(self, now: float) -> float:
        return max(0.0, self._not_before - now)

    @property
    def basis(self) -> str | None:
        return self._basis

    @property
    def travel_pending(self) -> bool:
        return self._pending is not None

    def _extend(self, until: float, basis: str) -> None:
        if until >= self._not_before:
            self._not_before = until
            self._basis = basis

    def note_endpoint_start(self, now: float, interval_s: float) -> None:
        self._extend(now + interval_s, BASIS_ENDPOINT_START)

    def begin_travel(self, now: float, expected_s: float, settle_s: float) -> None:
        self._pending = (now + max(0.0, expected_s), settle_s)

    def finish_travel(self, now: float, settle_s: float, completion_method: str | None) -> None:
        self._pending = None
        if completion_method in _OBSERVED_METHODS:
            basis = BASIS_OBSERVED
        elif completion_method in _ESTIMATED_METHODS:
            basis = BASIS_ESTIMATED
        elif completion_method == "timeout":
            basis = BASIS_TIMEOUT
        else:
            basis = BASIS_FAILED
        self._extend(now + settle_s, basis)

    def resolve_pending(self, now: float) -> bool:
        """Keep a bounded conservative boundary for a travel that was started and
        never resolved.  Idempotent.  True when something was pending."""
        if self._pending is None:
            return False
        expected_end, settle_s = self._pending
        self._pending = None
        self._resolved_unconfirmed_total += 1
        self._extend(max(now, expected_end) + settle_s, BASIS_UNCONFIRMED)
        return True

    def stats(self, now: float | None = None) -> dict:
        return {
            "remaining_ms": None if now is None else round(self.remaining(now) * 1000.0),
            "basis": self._basis,
            "travel_pending": self._pending is not None,
            "unconfirmed_resolutions": self._resolved_unconfirmed_total,
        }
