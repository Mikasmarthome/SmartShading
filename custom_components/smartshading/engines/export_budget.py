"""Deterministic, linear-time size budgeting for the diagnostics export payloads.

The Home Assistant diagnostics download embeds the support export and the
research export.  Both used to enforce their size cap by removing one record at
a time and re-serialising the *whole* payload after every removal, which is
quadratic and blocked the event loop for minutes on large histories.

This module replaces that loop.  Sizes are additive: a JSON list serialises to
``"[" + ", ".join(items) + "]"``, so the size of a payload equals the size of the
payload with every history list emptied plus, per non-empty list, the sum of
its record sizes and two bytes per separator.  Every record is therefore
serialised exactly once, the records to drop are chosen by arithmetic on those
sizes (oldest first, identical to the old selection rule), and the result is
verified with a single full serialisation.  A bounded number of tightening
passes covers any residual mismatch, so the number of full serialisations is a
small constant instead of growing with the record count.

Everything here is pure (no Home Assistant objects), deterministic and free of
side effects other than the in-place removal of the oldest records from the
lists handed in as slots.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

# Official diagnostics download: support + research together (history *and*
# fixed overviews).  See diagnostics.py for the split between the two.
EMBEDDED_EXPORT_BUDGET_BYTES: int = 2_000_000

# Preferred split of the embedded budget (percent).  A side that needs less
# than its share leaves the remainder to the other side.
SUPPORT_BUDGET_WEIGHT: int = 60
RESEARCH_BUDGET_WEIGHT: int = 40

# Headroom kept free for the truncation records that are written *after* the
# trim (they carry the final numbers, so they cannot be measured beforehand).
TRUNCATION_META_RESERVE_BYTES: int = 1024
TRUNCATION_META_PER_ZONE_BYTES: int = 768

_MAX_TIGHTEN_PASSES = 4


def json_size(obj: Any) -> int:
    """Serialised size in bytes (compact separators, ``default=str``)."""
    try:
        return len(json.dumps(obj, default=str).encode("utf-8"))
    except Exception:
        return 0


def meta_reserve(zone_count: int = 0) -> int:
    return TRUNCATION_META_RESERVE_BYTES + TRUNCATION_META_PER_ZONE_BYTES * max(0, zone_count)


@dataclass
class Slot:
    """One history list inside a payload.

    ``records`` is the live list object; trimming removes from it in place.
    ``oldest_first`` says where the oldest records sit: at index 0 (the usual
    chronological order) or at the end (newest-first lists such as the support
    timeline).
    """

    name: str
    records: list
    oldest_first: bool = True
    sizes: list[int] = field(default_factory=list)  # oldest -> newest, filled by measure_slots

    def measure(self) -> None:
        recs = self.records if self.oldest_first else self.records[::-1]
        self.sizes = [json_size(r) for r in recs]

    def drop_oldest(self, n: int) -> None:
        if n <= 0:
            return
        if self.oldest_first:
            del self.records[:n]
        else:
            del self.records[len(self.records) - n:]
        del self.sizes[:n]


def slot_cost(sizes: Sequence[int]) -> int:
    """Bytes a non-empty list adds beyond its empty ``[]`` form."""
    return 0 if not sizes else sum(sizes) + 2 * (len(sizes) - 1)


def group_cost(slots: Sequence[Slot]) -> int:
    return sum(slot_cost(s.sizes) for s in slots)


def water_fill(needs: Sequence[int], budget: int) -> list[int]:
    """Max-min fair integer allocation of *budget* over consumers.

    ``alloc[i] <= needs[i]`` and ``sum(alloc) <= budget``.  Small consumers are
    served completely, the rest share what remains equally.  Ties are broken by
    index, so the result is a pure function of the inputs.
    """
    n = len(needs)
    alloc = [0] * n
    budget = max(0, int(budget))
    if n == 0:
        return alloc
    if sum(needs) <= budget:
        return [int(x) for x in needs]
    remaining = budget
    for pos, i in enumerate(sorted(range(n), key=lambda k: (needs[k], k))):
        share = remaining // (n - pos)
        alloc[i] = min(int(needs[i]), share)
        remaining -= alloc[i]
    return alloc


def weighted_fill(needs: Sequence[int], budget: int, weights: Sequence[int]) -> list[int]:
    """Like :func:`water_fill` but consumers carry integer weights."""
    n = len(needs)
    alloc = [0] * n
    remaining = max(0, int(budget))
    active = list(range(n))
    while active:
        wsum = sum(weights[i] for i in active)
        done = [i for i in active if (needs[i] - alloc[i]) * wsum <= remaining * weights[i]]
        if not done:
            for i in active:
                alloc[i] += (remaining * weights[i]) // wsum
            break
        for i in done:
            remaining -= needs[i] - alloc[i]
            alloc[i] = needs[i]
            active.remove(i)
    return alloc


def plan_removals(slot_sizes: Sequence[Sequence[int]], allowance: int) -> list[int]:
    """How many oldest records to drop from each slot to fit *allowance* bytes.

    ``slot_sizes[i]`` holds the record sizes of slot ``i``, oldest first; the
    slots are listed in shedding order.  Slot ``i + 1`` is only touched once
    slot ``i`` is empty.  This is exactly the selection the former
    ``while over: records.pop(0)`` loops made, computed without re-serialising.
    """
    costs = [slot_cost(s) for s in slot_sizes]
    total = sum(costs)
    removals = [0] * len(slot_sizes)
    for i, sizes in enumerate(slot_sizes):
        if total <= allowance:
            break
        m = len(sizes)
        if m == 0:
            continue
        rest = sum(sizes)
        r = 0
        cost_after = costs[i]
        while r < m and total - costs[i] + cost_after > allowance:
            rest -= sizes[r]
            r += 1
            cost_after = 0 if r == m else rest + 2 * (m - r - 1)
        removals[i] = r
        total = total - costs[i] + cost_after
    return removals


@dataclass
class GroupReport:
    allowance: int = 0
    history_bytes_original: int = 0
    history_bytes_final: int = 0
    sections: dict = field(default_factory=dict)  # name -> {original_count, final_count, removed_count}

    @property
    def removed_count(self) -> int:
        return sum(v["removed_count"] for v in self.sections.values())


@dataclass
class TrimReport:
    original_bytes: int = 0
    final_bytes: int = 0
    byte_cap: int = 0
    overhead_bytes: int = 0
    cap_met: bool = True
    measurements: int = 0
    groups: list[GroupReport] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return any(g.removed_count for g in self.groups)


def _apply(groups: Sequence[Sequence[Slot]], allowances: Sequence[int],
           fair_sections: bool = False) -> None:
    """Remove records so that every group fits its allowance.

    Default (cascade): the slots of a group are shed in order, the first one
    completely before the next is touched - the rule the exports always used.
    ``fair_sections``: the group's allowance is shared max-min fairly between its
    slots instead, so no section is wiped out while another one stays large.
    """
    for slots, allowance in zip(groups, allowances):
        if fair_sections and len(slots) > 1:
            shares = water_fill([slot_cost(s.sizes) for s in slots], allowance)
            for slot, share in zip(slots, shares):
                slot.drop_oldest(plan_removals([slot.sizes], share)[0])
        else:
            for slot, r in zip(slots, plan_removals([s.sizes for s in slots], allowance)):
                slot.drop_oldest(r)


def _start_report(groups: Sequence[Sequence[Slot]]) -> tuple[list[GroupReport], list[list[int]]]:
    reports, counts = [], []
    for slots in groups:
        reports.append(GroupReport(history_bytes_original=group_cost(slots)))
        counts.append([len(s.records) for s in slots])
    return reports, counts


def _finish_report(groups, reports, counts) -> None:
    for slots, rep, cnt in zip(groups, reports, counts):
        rep.history_bytes_final = group_cost(slots)
        for slot, before in zip(slots, cnt):
            after = len(slot.records)
            if after != before:
                rep.sections[slot.name] = {"original_count": before, "final_count": after,
                                           "removed_count": before - after}


def trim_to_cap(contract: Any, groups: Sequence[Sequence[Slot]], cap: int, *,
                reserve: int = 0, measure: Callable[[Any], int] = json_size,
                sync: Callable[[], None] | None = None,
                fair_sections: bool = False) -> TrimReport:
    """Shed the oldest history records until *contract* fits *cap* bytes.

    ``groups`` are the fairness domains (for example one per zone), each a list
    of :class:`Slot` in shedding order.  The history allowance is shared between
    the groups max-min fairly, so no zone can crowd out another and the newest
    records of every zone survive.  ``reserve`` is headroom kept free for the
    truncation metadata the caller writes afterwards.  ``sync`` is called before
    every measurement after the first when the slot lists are not the payload's
    own lists (it copies the trimmed state back into the payload).
    ``fair_sections`` shares a group's allowance between its slots instead of
    shedding them one after the other (see :func:`_apply`).
    """
    target = cap - reserve
    report = TrimReport(byte_cap=cap)
    report.original_bytes = measure(contract)
    report.measurements = 1
    for slots in groups:
        for s in slots:
            s.measure()
    reports, counts = _start_report(groups)
    report.groups = reports
    costs = [r.history_bytes_original for r in reports]
    report.overhead_bytes = report.original_bytes - sum(costs)
    if report.original_bytes <= target:
        for rep in reports:
            rep.allowance = rep.history_bytes_original
        report.final_bytes = report.original_bytes
        return report

    allowance = max(0, target - report.overhead_bytes)
    allocs = water_fill(costs, allowance)
    _apply(groups, allocs, fair_sections)
    for rep, a in zip(reports, allocs):
        rep.allowance = a

    if sync:
        sync()
    final = measure(contract)
    report.measurements += 1
    passes = 0
    while final > target and passes < _MAX_TIGHTEN_PASSES and any(s.records for g in groups for s in g):
        passes += 1
        now_costs = [group_cost(g) for g in groups]
        allowance = max(0, sum(now_costs) - (final - target))
        allocs = water_fill(now_costs, allowance)
        _apply(groups, allocs, fair_sections)
        for rep, a in zip(reports, allocs):
            rep.allowance = a
        if sync:
            sync()
        final = measure(contract)
        report.measurements += 1
    _finish_report(groups, reports, counts)
    report.final_bytes = final
    report.cap_met = final <= cap
    return report


def trim_jointly(parts: Sequence[tuple], total_cap: int,
                 weights: Sequence[int], *,
                 measure: Callable[[Any], int] = json_size,
                 fair_sections: bool = False) -> list[TrimReport]:
    """Fit several payloads under one shared byte budget.

    ``parts`` is a list of ``(contract, groups, reserve)`` or ``(contract, groups,
    reserve, sync)``; ``sync`` has the meaning described in :func:`trim_to_cap`.  The fixed parts of
    every payload (overviews, current state, error counters, learning status)
    are kept; only the history lists share what is left of *total_cap*.  The
    split between payloads follows *weights* but never starves one side and
    hands unused share to the other.  Within a payload the allowance is shared
    max-min fairly between its groups.
    """
    parts = [tuple(p) + (None,) * (4 - len(p)) for p in parts]
    reports = [TrimReport(byte_cap=total_cap) for _ in parts]
    for rep, (contract, groups, _reserve, _sync) in zip(reports, parts):
        rep.original_bytes = measure(contract)
        rep.measurements = 1
        for slots in groups:
            for s in slots:
                s.measure()
    started = [_start_report(groups) for _c, groups, _r, _s in parts]
    for rep, (grp_reports, _cnt) in zip(reports, started):
        rep.groups = grp_reports
    part_costs = [sum(g.history_bytes_original for g in grp) for grp, _ in started]
    for rep, c in zip(reports, part_costs):
        rep.overhead_bytes = rep.original_bytes - c
    reserve_total = sum(r for _c, _g, r, _s in parts)
    target = total_cap - reserve_total
    total_orig = sum(rep.original_bytes for rep in reports)
    if total_orig <= target:
        for rep in reports:
            for g in rep.groups:
                g.allowance = g.history_bytes_original
            rep.final_bytes = rep.original_bytes
        return reports

    history_budget = max(0, target - sum(rep.overhead_bytes for rep in reports))
    part_allow = weighted_fill(part_costs, history_budget, weights)
    for (contract, groups, _r, sync), (grp_reports, _cnt), allow, rep in zip(parts, started, part_allow, reports):
        costs = [g.history_bytes_original for g in grp_reports]
        allocs = water_fill(costs, allow)
        _apply(groups, allocs, fair_sections)
        for g, a in zip(grp_reports, allocs):
            g.allowance = a
        if sync:
            sync()
        rep.final_bytes = measure(contract)
        rep.measurements += 1

    passes = 0
    while sum(r.final_bytes for r in reports) > target and passes < _MAX_TIGHTEN_PASSES:
        if not any(s.records for _c, groups, _r, _s in parts for g in groups for s in g):
            break
        passes += 1
        deficit = sum(r.final_bytes for r in reports) - target
        now_costs = [sum(group_cost(g) for g in groups) for _c, groups, _r, _s in parts]
        part_allow = weighted_fill(now_costs, max(0, sum(now_costs) - deficit), weights)
        for (contract, groups, _r, sync), allow, rep in zip(parts, part_allow, reports):
            costs = [group_cost(g) for g in groups]
            allocs = water_fill(costs, allow)
            _apply(groups, allocs, fair_sections)
            for g, a in zip(rep.groups, allocs):
                g.allowance = a
            if sync:
                sync()
            rep.final_bytes = measure(contract)
            rep.measurements += 1

    for (_c, groups, _r, _s), (grp_reports, cnt) in zip(parts, started):
        _finish_report(groups, grp_reports, cnt)
    cap_met = sum(r.final_bytes for r in reports) <= total_cap
    for rep in reports:
        rep.cap_met = cap_met
    return reports
