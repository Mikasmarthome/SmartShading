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

from ..models.decision_provenance import (
    LearningDecisionRecord,
    RETENTION_PINNED,
)
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

# LE 2.0 / P2 — hard absolute cap on decision records per window (count).
# Age/full-vs-summary demotion is applied separately by the persistence layer;
# this is the last-resort in-memory ceiling so RAM never grows unbounded.
_DECISIONS_HARD_CAP: int = 5000

# Pin caps (P2.8).  Pins protect referenced shadow/experiment records from
# pruning, but never grant unbounded lifetime (age cap still applies upstream).
_MAX_PINS_PER_WINDOW: int = 5
_MAX_PINS_PER_ZONE: int = 10
_ABSOLUTE_PIN_CAP: int = 50

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

        # LE 2.0 / P2 — decision records (provenance envelopes), oldest-first
        # per window.  Bounded by _DECISIONS_HARD_CAP + persistence retention.
        self._decisions: dict[str, list[LearningDecisionRecord]] = {}
        # Active pending decision id per window (max one observation per window).
        self._pending_decision_ids: dict[str, str] = {}

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
        """Return resolved decision outcomes for *window_id*, newest first.

        LE 2.0 merged view: outcomes embedded in v2 decision records PLUS the
        legacy v1 outcomes ring.  Deduplicated by (decision_timestamp): when a
        decision record carries an outcome, it is authoritative and any legacy
        ring entry with the same decision timestamp is dropped.  This keeps the
        existing analytical engines (SituationJoiner, SolarImpact, Similarity)
        working unchanged while moving the source of truth into records.
        """
        record_outcomes: list[DecisionOutcome] = [
            r.outcome
            for r in self._decisions.get(window_id, [])
            if r.outcome is not None
        ]
        record_keys = {o.decision_timestamp for o in record_outcomes}

        buf = self._outcomes.get(window_id)
        legacy = [
            o for o in (buf.get_all() if buf is not None else [])
            if o.decision_timestamp not in record_keys
        ]

        merged = record_outcomes + legacy
        merged.sort(key=lambda o: o.decision_timestamp, reverse=True)
        return _query(merged, since=since, limit=limit)

    def window_ids(self) -> set[str]:
        """Return the set of window IDs that have at least one record."""
        return (
            set(self._transitions)
            | set(self._overrides)
            | set(self._snapshots)
            | set(self._outcomes)
            | set(self._decisions)
        )

    # ------------------------------------------------------------------
    # LE 2.0 / P2 — Decision record API
    # ------------------------------------------------------------------

    def record_decision(self, record: LearningDecisionRecord) -> None:
        """Append a decision record (provenance envelope) for its window.

        Enforces the in-memory hard count cap by evicting the oldest
        NON-pinned record when the cap is exceeded.
        """
        lst = self._decisions.setdefault(record.window_id, [])
        lst.append(record)
        if len(lst) > _DECISIONS_HARD_CAP:
            for i, r in enumerate(lst):
                if not r.pinned:
                    del lst[i]
                    break
            else:
                # All pinned (pathological) — evict the very oldest anyway.
                del lst[0]

    def get_decisions(
        self,
        window_id: str,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[LearningDecisionRecord]:
        """Return decision records for *window_id*, newest first."""
        lst = self._decisions.get(window_id)
        if lst is None:
            return []
        return _query(list(reversed(lst)), since=since, limit=limit)

    def get_decision(self, window_id: str, decision_id: str) -> LearningDecisionRecord | None:
        for r in self._decisions.get(window_id, []):
            if r.decision_id == decision_id:
                return r
        return None

    def replace_decision(self, record: LearningDecisionRecord) -> bool:
        """Atomically replace a record (matched by decision_id).  Returns True
        when a matching record existed and was replaced."""
        lst = self._decisions.get(record.window_id)
        if lst is None:
            return False
        for i, r in enumerate(lst):
            if r.decision_id == record.decision_id:
                lst[i] = record
                return True
        return False

    def attach_outcome_by_decision_id(
        self, window_id: str, decision_id: str, outcome: DecisionOutcome, outcome_status: str
    ) -> bool:
        """AUTHORITATIVE v2 path: attach a resolved outcome to the record with
        the exact *decision_id*.  Returns False when no such record exists —
        the caller must NOT fall back to timestamp matching for v2 data."""
        return self.attach_outcome(window_id, decision_id, outcome, outcome_status)

    def get_decision_by_timestamp(self, window_id: str, ts: datetime) -> LearningDecisionRecord | None:
        for r in self._decisions.get(window_id, []):
            if r.decision_timestamp == ts:
                return r
        return None

    def attach_outcome_by_timestamp_legacy(
        self, outcome: DecisionOutcome, outcome_status: str
    ) -> bool:
        """LEGACY-ONLY fallback for v1 outcomes (decision_id is None): attach by
        (window_id, decision_timestamp).  Must never be used for v2 outcomes."""
        rec = self.get_decision_by_timestamp(outcome.window_id, outcome.decision_timestamp)
        if rec is None:
            return False
        return self.attach_outcome(outcome.window_id, rec.decision_id, outcome, outcome_status)

    def mark_decision_invalidated(
        self, window_id: str, decision_id: str, reason: str
    ) -> bool:
        """Mark a decision record as invalidated (no outcome).  Returns True when
        a matching record existed."""
        from dataclasses import replace as _replace
        from ..models.decision_provenance import OUTCOME_STATUS_INVALIDATED

        rec = self.get_decision(window_id, decision_id)
        if rec is None:
            return False
        self.replace_decision(_replace(
            rec, outcome_status=OUTCOME_STATUS_INVALIDATED, invalidation_reason=reason
        ))
        self.clear_pending_decision(window_id)
        return True

    def set_pending_decision(self, window_id: str, decision_id: str) -> None:
        """Mark *decision_id* as the active pending observation for the window."""
        self._pending_decision_ids[window_id] = decision_id

    def get_pending_decision_id(self, window_id: str) -> str | None:
        return self._pending_decision_ids.get(window_id)

    def clear_pending_decision(self, window_id: str) -> None:
        self._pending_decision_ids.pop(window_id, None)

    def pending_decision_ids(self) -> dict[str, str]:
        return dict(self._pending_decision_ids)

    def attach_outcome(
        self,
        window_id: str,
        decision_id: str,
        outcome: DecisionOutcome,
        outcome_status: str,
        *,
        observation_interrupted: bool = False,
        interruption_started_at: datetime | None = None,
        restored_at: datetime | None = None,
        interruption_duration_seconds: int | None = None,
        restart_count: int = 0,
        invalidation_reason: str | None = None,
    ) -> bool:
        """Atomically attach a resolved outcome to its decision record.

        Returns True when the record existed and was updated.  The outcome is
        the single source of truth (no duplicate write to the legacy ring).
        """
        from dataclasses import replace as _replace

        rec = self.get_decision(window_id, decision_id)
        if rec is None:
            return False
        updated = _replace(
            rec,
            outcome=outcome,
            outcome_status=outcome_status,
            observation_interrupted=observation_interrupted,
            interruption_started_at=interruption_started_at,
            restored_at=restored_at,
            interruption_duration_seconds=interruption_duration_seconds,
            restart_count=restart_count,
            invalidation_reason=invalidation_reason,
        )
        self.replace_decision(updated)
        self.clear_pending_decision(window_id)
        return True

    # ------------------------------------------------------------------
    # Pin management (P2.8)
    # ------------------------------------------------------------------

    def pin_decision(self, window_id: str, decision_id: str, zone_id_of: dict[str, str] | None = None) -> bool:
        """Pin a decision record so retention never demotes/deletes it.

        Enforces per-window, per-zone and absolute pin caps.  Returns True when
        the pin was applied, False when a cap blocked it.
        """
        from dataclasses import replace as _replace

        rec = self.get_decision(window_id, decision_id)
        if rec is None or rec.pinned:
            return rec is not None and rec.pinned

        if self._count_window_pins(window_id) >= _MAX_PINS_PER_WINDOW:
            return False
        if self._count_total_pins() >= _ABSOLUTE_PIN_CAP:
            return False

        self.replace_decision(_replace(rec, pinned=True, retention_class=RETENTION_PINNED))
        return True

    def unpin_decision(self, window_id: str, decision_id: str) -> None:
        from dataclasses import replace as _replace
        from ..models.decision_provenance import RETENTION_FULL

        rec = self.get_decision(window_id, decision_id)
        if rec is not None and rec.pinned:
            self.replace_decision(_replace(rec, pinned=False, retention_class=RETENTION_FULL))

    def _count_window_pins(self, window_id: str) -> int:
        return sum(1 for r in self._decisions.get(window_id, []) if r.pinned)

    def _count_total_pins(self) -> int:
        return sum(
            1 for lst in self._decisions.values() for r in lst if r.pinned
        )

    def pinned_decision_ids(self, window_id: str) -> set[str]:
        return {r.decision_id for r in self._decisions.get(window_id, []) if r.pinned}

    def set_decisions(self, window_id: str, records: list[LearningDecisionRecord]) -> None:
        """Replace the full decision list for a window (used by restore/retention)."""
        self._decisions[window_id] = list(records)

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
