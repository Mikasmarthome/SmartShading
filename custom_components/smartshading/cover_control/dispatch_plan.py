"""Target-aware Dispatch — Phase 2: pure DispatchPlan data model and
deterministic sorting/deduplication.

Turns already fully-decided and classified (Phase 1,
engines/dispatch_classification.py) cover items into a small, immutable
plan that a later executor phase can safely walk. This module recomputes
NOTHING: no shading decisions, no classification, no blocking authority,
no HA state reads, no service calls. It is a pure ordering + validation
layer over data that already exists by the time build_dispatch_plan() is
called.

Safety is explicitly OUT of scope here — safety dispatch keeps using its
existing, unmodified fastlane path (see cover_control/command_filter.py's
is_safety exemptions and coordinator.py's _dispatch_order_key safety-first
sort key). This module's DispatchPlan is a comfort-only model; the builder
rejects any item carrying a known safety-execution reason (see
_REJECTED_SAFETY_REASONS) rather than silently absorbing it.

No Home Assistant import. No asyncio. No coordinator import. No lock,
queue, or executor. No dispatch_cover_intent / wait_for_travel_completion /
GlobalSerialDispatch coupling (all Phase 3+).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..engines.dispatch_classification import DispatchTargetClass
from .position_semantics import HA_CLOSED, HA_OPEN

# ---------------------------------------------------------------------------
# Explicit, central classification priority — never alphabetical/enum-string
# sort order. FULL_OPEN/FULL_CLOSE first (fastest to start, no completion
# wait — B3-012), then INTERMEDIATE, then the two non-executable classes
# (kept sortable/present for diagnostics, but never mixed ahead of real
# movement items).
# ---------------------------------------------------------------------------
_CLASS_PRIORITY: dict[DispatchTargetClass, int] = {
    DispatchTargetClass.FULL_OPEN: 0,
    DispatchTargetClass.FULL_CLOSE: 0,
    DispatchTargetClass.INTERMEDIATE: 1,
    DispatchTargetClass.NO_MOVEMENT: 2,
    DispatchTargetClass.BLOCKED: 3,
}

_EXECUTABLE_CLASSES = frozenset({
    DispatchTargetClass.FULL_OPEN, DispatchTargetClass.FULL_CLOSE,
    DispatchTargetClass.INTERMEDIATE,
})

# Phase 2 never introduces a "safety class" — if a caller passes an item
# whose reason names a known safety-execution path, the builder rejects the
# whole plan rather than silently treating it as an ordinary comfort item.
# These are not re-derived authority logic, just the same literal markers
# Phase 1 already uses/would use for a safety-sourced reason string.
_REJECTED_SAFETY_REASONS = frozenset({"safety", "is_safety", "safety_priority"})


@dataclass(frozen=True, slots=True)
class DispatchPlanItem:
    """One cover's final, already-decided place in a comfort dispatch plan.

    Every field is justified against: sorting / later dispatch / later live
    revalidation / diagnostics — see the Phase 2 closing report for the
    field-by-field rationale. Deliberately excludes any runtime-only field
    (completion result, dispatch timestamp, timeout, retry count,
    cancellation status, mutable queue position, task, lock) — those belong
    to the Phase 3+ executor, not this data model.
    """

    zone_id: str
    zone_index: int
    cover_entity_id: str
    cover_index: int
    target_ha: int | None
    target_class: DispatchTargetClass
    decision_ref: str
    zone_generation: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchPlan:
    """An immutable, already-sorted, already-deduplicated comfort dispatch
    plan. ``plan_id``/``trigger``/``created_at`` are supplied by the caller
    (see build_dispatch_plan) — this module never generates an ID, a
    timestamp, or any other value from hidden module-level state."""

    plan_id: str
    trigger: str
    created_at: object
    items: tuple[DispatchPlanItem, ...] = field(default_factory=tuple)

    @property
    def zone_ids(self) -> tuple[str, ...]:
        """Derived, not stored — the set of zones actually represented,
        in the same stable order the items already carry."""
        seen: list[str] = []
        for item in self.items:
            if item.zone_id not in seen:
                seen.append(item.zone_id)
        return tuple(seen)

    @property
    def executable_items(self) -> tuple[DispatchPlanItem, ...]:
        return tuple(i for i in self.items if i.target_class in _EXECUTABLE_CLASSES)

    @property
    def non_executable_items(self) -> tuple[DispatchPlanItem, ...]:
        return tuple(i for i in self.items if i.target_class not in _EXECUTABLE_CLASSES)

    @property
    def full_open_items(self) -> tuple[DispatchPlanItem, ...]:
        return tuple(i for i in self.items if i.target_class is DispatchTargetClass.FULL_OPEN)

    @property
    def full_close_items(self) -> tuple[DispatchPlanItem, ...]:
        return tuple(i for i in self.items if i.target_class is DispatchTargetClass.FULL_CLOSE)

    @property
    def intermediate_items(self) -> tuple[DispatchPlanItem, ...]:
        return tuple(i for i in self.items if i.target_class is DispatchTargetClass.INTERMEDIATE)

    @property
    def no_movement_items(self) -> tuple[DispatchPlanItem, ...]:
        return tuple(i for i in self.items if i.target_class is DispatchTargetClass.NO_MOVEMENT)

    @property
    def blocked_items(self) -> tuple[DispatchPlanItem, ...]:
        return tuple(i for i in self.items if i.target_class is DispatchTargetClass.BLOCKED)


def _sort_key(item: DispatchPlanItem) -> tuple:
    """The full, explicit, deterministic sort key. Never depends on dict
    iteration, set order, HA entity-registry order, listener submission
    order, object id(), hash(), or a random UUID — every component is a
    plain, stable, caller-supplied value.

    B3-013: zone_index is now the PRIMARY key (was target_class priority).
    A plan spanning multiple zones must be zone-CONTIGUOUS — every item of
    zone N before any item of zone N+1 — so the executor (which walks
    plan.items in order) never interleaves two rooms. Before this, class
    priority was primary, so e.g. a FULL_OPEN item in zone B could sort
    ahead of an INTERMEDIATE item in zone A, splitting zone A's own package
    across the plan. class_priority stays the secondary key WITHIN one
    zone (endpoints before intermediates before non-executable items,
    unchanged from T22), then cover_index for the existing stable
    within-zone item order."""
    return (
        item.zone_index,
        _CLASS_PRIORITY[item.target_class],
        item.cover_index,
        item.zone_id,
        item.cover_entity_id,
    )


def _validate_target(target_ha, *, target_class: DispatchTargetClass) -> int | None:
    if target_class in _EXECUTABLE_CLASSES or target_class is DispatchTargetClass.NO_MOVEMENT:
        if target_ha is None:
            raise ValueError(f"{target_class.value} item requires a resolved target_ha")
    if target_ha is None:
        return None
    try:
        as_float = float(target_ha)
    except (TypeError, ValueError):
        raise ValueError(f"target_ha {target_ha!r} is not numeric") from None
    if math.isnan(as_float) or math.isinf(as_float):
        raise ValueError(f"target_ha {target_ha!r} is not finite")
    as_int = round(as_float)
    if not (HA_CLOSED <= as_int <= HA_OPEN):
        raise ValueError(f"target_ha {as_int} outside canonical range [{HA_CLOSED}, {HA_OPEN}]")
    # Semantic consistency check (see Phase 2 closing report for why this is
    # the only check possible here without re-deriving Phase 1's own logic):
    # an INTERMEDIATE item can never legitimately carry the exact open or
    # closed value. B3-012 made Phase 1's FULL_OPEN/FULL_CLOSE boundary
    # exact (never tolerance-blurred), so target_ha == HA_OPEN /
    # target_ha == HA_CLOSED is unambiguously FULL_OPEN/FULL_CLOSE
    # territory, already decided by Phase 1 before this item ever reached
    # the plan builder.
    if target_class is DispatchTargetClass.INTERMEDIATE and as_int == HA_OPEN:
        raise ValueError("INTERMEDIATE item cannot carry the fully-open target value")
    if target_class is DispatchTargetClass.INTERMEDIATE and as_int == HA_CLOSED:
        raise ValueError("INTERMEDIATE item cannot carry the fully-closed target value")
    return as_int


def _validate_item(item: DispatchPlanItem) -> None:
    if not item.zone_id:
        raise ValueError("zone_id must not be empty")
    if not item.cover_entity_id:
        raise ValueError("cover_entity_id must not be empty")
    if item.zone_index < 0:
        raise ValueError("zone_index must not be negative")
    if item.cover_index < 0:
        raise ValueError("cover_index must not be negative")
    if item.zone_generation < 0:
        raise ValueError("zone_generation must not be negative")
    if not item.decision_ref:
        raise ValueError("decision_ref must not be empty")
    if item.reason in _REJECTED_SAFETY_REASONS:
        raise ValueError(
            f"item reason {item.reason!r} names a safety-execution path — safety items must "
            "never enter a comfort DispatchPlan"
        )
    _validate_target(item.target_ha, target_class=item.target_class)


def _items_identical(a: DispatchPlanItem, b: DispatchPlanItem) -> bool:
    return (
        a.zone_id == b.zone_id
        and a.zone_index == b.zone_index
        and a.cover_entity_id == b.cover_entity_id
        and a.cover_index == b.cover_index
        and a.target_ha == b.target_ha
        and a.target_class == b.target_class
        and a.decision_ref == b.decision_ref
        and a.zone_generation == b.zone_generation
        and a.reason == b.reason
    )


def _deduplicate(items: list[DispatchPlanItem]) -> list[DispatchPlanItem]:
    """One cover must have exactly one final statement in a plan.

    Exact duplicates (every field identical) are deterministically collapsed
    to a single item — they carry no new information, so keeping only the
    first-seen occurrence is safe. Anything else sharing a cover_entity_id
    (different target, class, zone, generation, decision, or reason) is a
    planning error and rejected outright — this module never silently picks
    a winner between conflicting statements about the same cover.
    """
    by_entity: dict[str, list[DispatchPlanItem]] = {}
    order: list[str] = []
    for item in items:
        if item.cover_entity_id not in by_entity:
            order.append(item.cover_entity_id)
        by_entity.setdefault(item.cover_entity_id, []).append(item)

    result: list[DispatchPlanItem] = []
    for entity_id in order:
        group = by_entity[entity_id]
        canonical = group[0]
        for other in group[1:]:
            if not _items_identical(canonical, other):
                raise ValueError(
                    f"conflicting duplicate items for cover {entity_id!r}: "
                    f"{canonical!r} vs {other!r}"
                )
        result.append(canonical)
    return result


def build_dispatch_plan(
    *,
    plan_id: str,
    trigger: str,
    items,
    created_at,
) -> DispatchPlan:
    """Pure builder: validates, deduplicates, and deterministically sorts
    ``items`` into an immutable DispatchPlan.

    Never reads HA state, never recomputes a shading decision, never calls
    a service. ``items`` may be any iterable of DispatchPlanItem — the
    caller's original list/iterable is never mutated and has no further
    effect on the plan once this function returns (a defensive copy is
    taken before sorting/deduplication).
    """
    if not plan_id:
        raise ValueError("plan_id must not be empty")
    if not trigger:
        raise ValueError("trigger must not be empty")

    materialized = list(items)
    for item in materialized:
        _validate_item(item)

    deduplicated = _deduplicate(materialized)
    ordered = tuple(sorted(deduplicated, key=_sort_key))

    return DispatchPlan(plan_id=plan_id, trigger=trigger, created_at=created_at, items=ordered)
