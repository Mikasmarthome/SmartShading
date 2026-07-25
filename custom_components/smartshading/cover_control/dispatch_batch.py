"""Batch grouping for genuine concurrent (PARALLEL) dispatch — v1.2.0-beta.1,
T11.1.

Pure, HA-free, no asyncio: given an already zone/safety-ordered list of
dispatchable items (produced by the SAME _dispatch_order_key sort T11
already uses — see coordinator.py), groups them into batches for concurrent
dispatch.

Grouping rule:
  - Every safety item forms ONE fast-lane batch, dispatched first — Safety
    must never wait behind a non-safety PARALLEL batch (T11.1 ticket §2).
  - Non-safety items:
      zone_batching=False: ALL non-safety items form ONE single batch — the
        ticket's "ohne Zonen-Batching: alle im aktuellen Dispatch-Plan
        enthaltenen geeigneten Kommandos dürfen gemeinsam gestartet werden."
      zone_batching=True: one batch PER ZONE, in the same stable zone order
        _dispatch_order_key already establishes — consecutive same-zone runs
        are grouped without re-sorting (the input is already sorted).

Each returned batch is dispatched by the caller via asyncio.gather() with
the shared GlobalSerialDispatch lock held for that batch's whole duration
(cross-coordinator/cross-zone mutual exclusion preserved — see
coordinator.py's _predispatch_parallel_batches docstring for why the lock
is still meaningful even though items WITHIN one batch run concurrently).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class DispatchItem(Generic[T]):
    """One dispatchable unit — generic over the caller's own per-intent
    context payload so this module has zero coupling to coordinator.py's
    internal state shapes."""

    is_safety: bool
    zone_id: str
    payload: T


@dataclass(frozen=True)
class DispatchBatch(Generic[T]):
    """One group of items to dispatch concurrently. zone_id is None for the
    safety fast-lane and for the single combined non-safety batch when
    zone_batching is disabled (both span potentially multiple real zones)."""

    zone_id: str | None
    items: tuple[DispatchItem[T], ...]
    is_safety_batch: bool = False


def group_into_batches(
    items: list[DispatchItem[T]], *, zone_batching: bool
) -> list[DispatchBatch[T]]:
    """Group already-ordered dispatch items into concurrent-dispatch batches.

    `items` must already be in the coordinator's established dispatch order
    (safety first, then zone-grouped — _dispatch_order_key) — this function
    does not re-sort, only groups consecutive runs.
    """
    safety_items = tuple(item for item in items if item.is_safety)
    non_safety_items = [item for item in items if not item.is_safety]

    batches: list[DispatchBatch[T]] = []
    if safety_items:
        batches.append(DispatchBatch(zone_id=None, items=safety_items, is_safety_batch=True))

    if not non_safety_items:
        return batches

    if not zone_batching:
        batches.append(DispatchBatch(zone_id=None, items=tuple(non_safety_items)))
        return batches

    # zone_batching=True: group consecutive same-zone runs. The input is
    # already zone-ordered (F18 / _dispatch_order_key), so a plain linear
    # scan is sufficient — no need to re-sort or use a dict keyed by zone
    # (which would silently merge non-consecutive same-zone runs and lose
    # the established ordering guarantee).
    current_zone: str | None = None
    current_group: list[DispatchItem[T]] = []
    for item in non_safety_items:
        if item.zone_id != current_zone:
            if current_group:
                batches.append(DispatchBatch(zone_id=current_zone, items=tuple(current_group)))
            current_group = []
            current_zone = item.zone_id
        current_group.append(item)
    if current_group:
        batches.append(DispatchBatch(zone_id=current_zone, items=tuple(current_group)))
    return batches
