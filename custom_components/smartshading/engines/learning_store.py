"""In-memory Learning Store (Phase 9B / 9F4a).

Provides a bounded ring buffer and the central LearningStore that collects
per-window observation records for the future Learning Engine.

Architecture invariants:
  - No Home Assistant imports. No persistence. No coordinator coupling.
  - Learning data must never affect core shading decisions. If the store
    raises, the Coordinator must catch and continue — the store is additive.
  - All buffers are bounded: oldest records are silently evicted when a
    buffer reaches capacity. No unbounded memory growth.
  - Per-window isolation: records for window A are never mixed with window B.
  - Query results are newest-first throughout (most recent data is most
    relevant for display and for the Learning Engine).

Default capacities (documented in ARCHITECTURE.md §Learning):
  transitions : 500 per window  (~500 state changes; normal usage << this)
  overrides   : 200 per window  (manual interventions; high churn is a signal)
  snapshots   : 2000 per window (~20 days at a 15-minute snapshot interval)
  outcomes    : 5000 per window (~3 years of daily state-change outcomes)
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Generic, TypeVar

from ..models.learning import (
    DecisionOutcome,
    OverrideRecord,
    StateTransitionRecord,
    WindowCycleSnapshot,
)

T = TypeVar("T")

# Default per-window capacities.
_TRANSITIONS_CAPACITY: int = 500
_OVERRIDES_CAPACITY: int = 200
_SNAPSHOTS_CAPACITY: int = 2000
_OUTCOMES_CAPACITY: int = 5000

# How many coordinator cycles between periodic snapshots.
# At a 1-minute default cycle interval this yields one snapshot every 15 minutes
# and ~20 days of history at the _SNAPSHOTS_CAPACITY of 2000.
SNAPSHOT_CYCLE_INTERVAL: int = 15


class RingBuffer(Generic[T]):
    """Bounded FIFO buffer. When full, the oldest entry is evicted on append.

    All public methods are O(1) or O(n) in buffer size, never in total
    records ever written. `get_all()` returns a list, newest entry first.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(f"RingBuffer capacity must be >= 1, got {capacity!r}")
        self._buf: deque[T] = deque(maxlen=capacity)
        self._capacity = capacity

    def append(self, record: T) -> None:
        """Add a record. If the buffer is full, the oldest entry is dropped."""
        self._buf.append(record)

    def get_all(self) -> list[T]:
        """Return all records, newest first."""
        return list(reversed(self._buf))

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._buf)


class LearningStore:
    """Central in-memory store for Learning Foundation observation records.

    Holds four per-window ring buffers:
      transition_log  — StateTransitionRecord  (state-change events)
      override_log    — OverrideRecord         (manual intervention events)
      cycle_snapshots — WindowCycleSnapshot    (periodic state snapshots)
      outcomes        — DecisionOutcome        (resolved shading-decision outcomes)

    Buffers are created lazily on first write for each window_id. All query
    methods return an empty list for unknown window IDs, so callers never
    need to guard against KeyError.
    """

    def __init__(
        self,
        transitions_capacity: int = _TRANSITIONS_CAPACITY,
        overrides_capacity: int = _OVERRIDES_CAPACITY,
        snapshots_capacity: int = _SNAPSHOTS_CAPACITY,
        outcomes_capacity: int = _OUTCOMES_CAPACITY,
    ) -> None:
        self._transitions_capacity = transitions_capacity
        self._overrides_capacity = overrides_capacity
        self._snapshots_capacity = snapshots_capacity
        self._outcomes_capacity = outcomes_capacity

        self._transitions: dict[str, RingBuffer[StateTransitionRecord]] = {}
        self._overrides: dict[str, RingBuffer[OverrideRecord]] = {}
        self._snapshots: dict[str, RingBuffer[WindowCycleSnapshot]] = {}
        self._outcomes: dict[str, RingBuffer[DecisionOutcome]] = {}

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def record_transition(self, record: StateTransitionRecord) -> None:
        """Append a state-transition record for the window named in the record."""
        self._transition_buf(record.window_id).append(record)

    def record_override(self, record: OverrideRecord) -> None:
        """Append an override-lifecycle record for the window named in the record."""
        self._override_buf(record.window_id).append(record)

    def record_snapshot(self, record: WindowCycleSnapshot) -> None:
        """Append a periodic cycle snapshot for the window named in the record."""
        self._snapshot_buf(record.window_id).append(record)

    def record_outcome(self, record: DecisionOutcome) -> None:
        """Append a resolved decision outcome for the window named in the record."""
        self._outcome_buf(record.window_id).append(record)

    # ------------------------------------------------------------------
    # Query API — all return newest-first, empty list for unknown windows
    # ------------------------------------------------------------------

    def get_transitions(
        self,
        window_id: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[StateTransitionRecord]:
        """Return state-transition records for *window_id*, newest first.

        *since* filters to records with timestamp >= cutoff (inclusive).
        *limit* caps the returned list length after the since-filter.
        """
        buf = self._transitions.get(window_id)
        if buf is None:
            return []
        return _query(buf.get_all(), since=since, limit=limit)

    def get_overrides(
        self,
        window_id: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[OverrideRecord]:
        """Return override-lifecycle records for *window_id*, newest first."""
        buf = self._overrides.get(window_id)
        if buf is None:
            return []
        return _query(buf.get_all(), since=since, limit=limit)

    def get_snapshots(
        self,
        window_id: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[WindowCycleSnapshot]:
        """Return periodic snapshots for *window_id*, newest first."""
        buf = self._snapshots.get(window_id)
        if buf is None:
            return []
        return _query(buf.get_all(), since=since, limit=limit)

    def get_outcomes(
        self,
        window_id: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[DecisionOutcome]:
        """Return resolved decision outcomes for *window_id*, newest first."""
        buf = self._outcomes.get(window_id)
        if buf is None:
            return []
        return _query(buf.get_all(), since=since, limit=limit)

    def window_ids(self) -> set[str]:
        """Return the set of window IDs that have at least one record."""
        return (
            set(self._transitions)
            | set(self._overrides)
            | set(self._snapshots)
            | set(self._outcomes)
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _transition_buf(self, window_id: str) -> RingBuffer[StateTransitionRecord]:
        if window_id not in self._transitions:
            self._transitions[window_id] = RingBuffer(self._transitions_capacity)
        return self._transitions[window_id]

    def _override_buf(self, window_id: str) -> RingBuffer[OverrideRecord]:
        if window_id not in self._overrides:
            self._overrides[window_id] = RingBuffer(self._overrides_capacity)
        return self._overrides[window_id]

    def _snapshot_buf(self, window_id: str) -> RingBuffer[WindowCycleSnapshot]:
        if window_id not in self._snapshots:
            self._snapshots[window_id] = RingBuffer(self._snapshots_capacity)
        return self._snapshots[window_id]

    def _outcome_buf(self, window_id: str) -> RingBuffer[DecisionOutcome]:
        if window_id not in self._outcomes:
            self._outcomes[window_id] = RingBuffer(self._outcomes_capacity)
        return self._outcomes[window_id]


# ------------------------------------------------------------------
# Private utility
# ------------------------------------------------------------------

def _query(
    records: list[T],
    since: datetime | None,
    limit: int | None,
) -> list[T]:
    """Filter by timestamp >= since and cap to limit. Records are newest-first."""
    if since is not None:
        records = [r for r in records if r.timestamp >= since]  # type: ignore[attr-defined]
    if limit is not None:
        records = records[:limit]
    return records
