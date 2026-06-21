"""PendingOutcomeQueue — Phase 9F4b-1.

In-memory queue for PendingOutcome objects. Enforces the single-outcome-per-window
invariant: at most one PendingOutcome exists per window_id at any time.

Architecture invariants:
  - No Home Assistant imports.
  - No persistence. RAM only.
  - No resolution logic. No score computation.
  - All mutation methods are synchronous and safe to call from the Coordinator.
  - Unknown window_id never raises KeyError — get() returns None, remove() returns None.

Single-Outcome Guarantee:
  replace() atomically swaps old for new and returns the displaced outcome so the
  caller can resolve it before discarding. This makes the caller's pattern explicit:

      old = queue.replace(new_outcome)
      if old is not None:
          resolve(old)           # 9F4b-2 responsibility
      record_outcome(result)     # 9F4a responsibility
"""
from __future__ import annotations

from ..models.pending_outcome import PendingOutcome


class PendingOutcomeQueue:
    """RAM-only store enforcing at most one PendingOutcome per window.

    All methods are O(1). The internal store is a plain dict keyed by window_id.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingOutcome] = {}

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def create(self, outcome: PendingOutcome) -> None:
        """Store *outcome* for its window.

        Raises ValueError if a PendingOutcome already exists for that window.
        The caller must call replace() instead when an existing outcome must
        be displaced — this prevents accidental silent overwrites.
        """
        if outcome.window_id in self._pending:
            raise ValueError(
                f"PendingOutcome already exists for window {outcome.window_id!r}. "
                "Call replace() to atomically swap it."
            )
        self._pending[outcome.window_id] = outcome

    def remove(self, window_id: str) -> PendingOutcome | None:
        """Remove and return the PendingOutcome for *window_id*, or None if absent."""
        return self._pending.pop(window_id, None)

    def replace(self, outcome: PendingOutcome) -> PendingOutcome | None:
        """Atomically store *outcome*, returning the previously stored outcome (or None).

        Guarantees the single-outcome-per-window invariant: the displaced
        outcome is returned so the caller can resolve it before it is lost.
        """
        old = self._pending.get(outcome.window_id)
        self._pending[outcome.window_id] = outcome
        return old

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def get(self, window_id: str) -> PendingOutcome | None:
        """Return the active PendingOutcome for *window_id*, or None if absent."""
        return self._pending.get(window_id)

    def window_ids(self) -> set[str]:
        """Return the set of window IDs with an active PendingOutcome."""
        return set(self._pending)

    def count(self) -> int:
        """Return the number of active PendingOutcomes across all windows."""
        return len(self._pending)
