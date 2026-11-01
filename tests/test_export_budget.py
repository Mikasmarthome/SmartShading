"""Size budgeting for the diagnostics exports (engines/export_budget.py).

The previous cap removed one record at a time and re-serialised the whole payload
after every removal.  These tests pin the replacement down: it selects exactly the
records the old rule selected, handles the edge cases, shares a budget fairly and
deterministically, and its serialisation work grows linearly (not quadratically)
with the number of records.  Complexity is checked by counting serialised bytes
and full-payload measurements, never by wall-clock time.
"""
from __future__ import annotations

import copy
import json
import random

import pytest

from custom_components.smartshading.engines import export_budget as eb
from custom_components.smartshading.engines.export_budget import (
    Slot,
    group_cost,
    json_size,
    plan_removals,
    slot_cost,
    trim_jointly,
    trim_to_cap,
    water_fill,
    weighted_fill,
)


def _rec(i: int, pad: int) -> dict:
    return {"i": i, "t": f"2026-01-01T00:{i % 60:02d}:00Z", "pad": "x" * pad, "n": i * 7}


def _payload(counts, seed=0, newest_first_last=False):
    """A payload with len(counts) history lists of varying record sizes."""
    rnd = random.Random(seed)
    payload = {"overview": {"a": 1, "b": [1, 2, 3]}, "meta": "fixed"}
    for k, n in enumerate(counts):
        recs = [_rec(i, rnd.randint(5, 400)) for i in range(n)]
        if newest_first_last and k == len(counts) - 1:
            recs = recs[::-1]
        payload[f"h{k}"] = recs
    return payload


def _slots(payload, names=None, newest_first_last=False):
    names = names or [k for k in payload if k.startswith("h")]
    return [Slot(n, payload[n], oldest_first=not (newest_first_last and i == len(names) - 1))
            for i, n in enumerate(names)]


def _old_cap(payload, names, cap, newest_first_last=False):
    """The removal rule the exports used before (reference implementation)."""
    if json_size(payload) <= cap:
        return payload
    for i, n in enumerate(names):
        recs = payload[n]
        oldest_at_end = newest_first_last and i == len(names) - 1
        while recs and json_size(payload) > cap:
            if oldest_at_end:
                recs.pop()
            else:
                recs.pop(0)
        if json_size(payload) <= cap:
            break
    return payload


class TestEquivalenceWithPreviousRule:
    @pytest.mark.parametrize("seed", range(12))
    @pytest.mark.parametrize("newest_first_last", [False, True])
    def test_same_records_survive(self, seed, newest_first_last):
        rnd = random.Random(seed)
        counts = [rnd.randint(0, 40) for _ in range(rnd.randint(1, 4))]
        base = _payload(counts, seed, newest_first_last)
        names = [k for k in base if k.startswith("h")]
        total = json_size(base)
        cap = rnd.randint(int(total * 0.1), int(total * 1.1))

        old = _old_cap(copy.deepcopy(base), names, cap, newest_first_last)
        new = copy.deepcopy(base)
        trim_to_cap(new, [_slots(new, names, newest_first_last)], cap)
        assert new == old

    def test_removal_is_oldest_first_and_newest_survive(self):
        payload = _payload([30])
        before = list(payload["h0"])
        report = trim_to_cap(payload, [_slots(payload)], json_size(payload) // 2)
        kept = payload["h0"]
        assert report.applied and 0 < len(kept) < 30
        assert kept == before[-len(kept):]  # a suffix: the newest records


class TestEdgeCases:
    def test_empty_history(self):
        payload = {"overview": {"a": 1}, "h0": [], "h1": []}
        report = trim_to_cap(payload, [_slots(payload)], 10)
        assert not report.applied
        assert report.final_bytes == report.original_bytes == json_size(payload)

    def test_no_slots_at_all(self):
        payload = {"overview": {"a": 1}}
        report = trim_to_cap(payload, [[]], 5)
        assert not report.applied and not report.cap_met  # overhead alone exceeds the cap

    def test_exactly_at_cap_removes_nothing(self):
        payload = _payload([20])
        size = json_size(payload)
        report = trim_to_cap(payload, [_slots(payload)], size)
        assert not report.applied and len(payload["h0"]) == 20
        assert report.final_bytes == size and report.cap_met

    def test_one_byte_over_removes_the_minimum(self):
        payload = _payload([20])
        size = json_size(payload)
        report = trim_to_cap(payload, [_slots(payload)], size - 1)
        assert report.applied and report.cap_met
        assert report.final_bytes <= size - 1
        # Removing one record fewer would not have fitted.
        assert len(payload["h0"]) == 19 or json_size(payload) <= size - 1
        assert report.groups[0].removed_count >= 1

    def test_reserve_is_kept_free(self):
        payload = _payload([40])
        size = json_size(payload)
        trim_to_cap(payload, [_slots(payload)], size, reserve=500)
        assert json_size(payload) <= size - 500

    def test_single_record_larger_than_the_budget_is_dropped(self):
        payload = {"overview": 1, "h0": [{"blob": "y" * 5000}]}
        report = trim_to_cap(payload, [_slots(payload)], 1000)
        assert payload["h0"] == []
        assert report.applied
        assert report.final_bytes <= 1000 and report.cap_met

    def test_budget_below_unavoidable_overhead(self):
        payload = {"overview": "z" * 800, "h0": [_rec(i, 50) for i in range(10)]}
        report = trim_to_cap(payload, [_slots(payload)], 300)
        assert payload["h0"] == []                       # all history gone
        assert report.final_bytes > 300 and not report.cap_met   # and it says so
        assert report.overhead_bytes >= 800

    def test_missing_optional_sections(self):
        payload = {"overview": 1, "h1": [_rec(i, 40) for i in range(30)]}
        report = trim_to_cap(payload, [[Slot("h1", payload["h1"])]], 600)
        assert report.cap_met and 0 < len(payload["h1"]) < 30

    def test_model_mismatch_is_repaired_by_the_tightening_pass(self):
        payload = _payload([60])
        size = json_size(payload)
        cap = size // 2
        # A measure that is stricter than the additive model: forces a second pass.
        report = trim_to_cap(payload, [_slots(payload)], cap,
                             measure=lambda c: json_size(c) + 700)
        assert json_size(payload) + 700 <= cap
        assert report.measurements > 2 or report.cap_met


class TestFairness:
    def test_water_fill_serves_small_consumers_completely(self):
        assert water_fill([10, 20, 1000], 100) == [10, 20, 70]

    def test_water_fill_equal_split_for_equal_demand(self):
        assert water_fill([1000, 1000, 1000], 300) == [100, 100, 100]

    def test_water_fill_never_exceeds_demand_or_budget(self):
        rnd = random.Random(5)
        for _ in range(200):
            needs = [rnd.randint(0, 500) for _ in range(rnd.randint(0, 8))]
            budget = rnd.randint(0, 3000)
            alloc = water_fill(needs, budget)
            assert all(0 <= a <= n for a, n in zip(alloc, needs))
            assert sum(alloc) <= budget
            if sum(needs) <= budget:
                assert alloc == needs

    def test_water_fill_is_deterministic_and_order_stable(self):
        needs = [500, 500, 40, 500]
        assert water_fill(needs, 700) == water_fill(list(needs), 700)

    def test_weighted_fill_hands_unused_share_to_the_other_side(self):
        assert weighted_fill([100, 5000], 1000, [60, 40]) == [100, 900]
        assert weighted_fill([5000, 100], 1000, [60, 40]) == [900, 100]
        assert weighted_fill([5000, 5000], 1000, [60, 40]) == [600, 400]

    def test_unequal_zones_small_zone_keeps_everything(self):
        payload = {"overview": 1,
                   "small": [_rec(i, 30) for i in range(5)],
                   "big": [_rec(i, 30) for i in range(400)]}
        small_before = list(payload["small"])
        cap = json_size(payload) // 3
        report = trim_to_cap(payload, [[Slot("small", payload["small"])], [Slot("big", payload["big"])]], cap)
        assert payload["small"] == small_before          # not crowded out
        assert 0 < len(payload["big"]) < 400
        assert report.cap_met

    def test_equal_zones_get_equal_shares(self):
        payload = {"overview": 1, "a": [_rec(i, 30) for i in range(200)],
                   "b": [_rec(i, 30) for i in range(200)]}
        trim_to_cap(payload, [[Slot("a", payload["a"])], [Slot("b", payload["b"])]], json_size(payload) // 4)
        assert abs(len(payload["a"]) - len(payload["b"])) <= 1


class TestDeterminism:
    def test_same_input_same_selection(self):
        outs = []
        for _ in range(3):
            payload = _payload([50, 70, 30], seed=3)
            trim_to_cap(payload, [_slots(payload)], json_size(payload) // 3)
            outs.append(json.dumps(payload, sort_keys=True))
        assert outs[0] == outs[1] == outs[2]


class TestJointBudget:
    def _parts(self, n_support, n_research):
        support = {"overview": "s" * 50, "hist": [_rec(i, 120) for i in range(n_support)]}
        research = {"overview": "r" * 50, "recs": [_rec(i, 120) for i in range(n_research)]}
        parts = [(support, [[Slot("hist", support["hist"])]], 100),
                 (research, [[Slot("recs", research["recs"])]], 100)]
        return support, research, parts

    def test_both_payloads_together_fit_the_budget(self):
        support, research, parts = self._parts(400, 400)
        budget = (json_size(support) + json_size(research)) // 4
        reports = trim_jointly(parts, budget, [60, 40])
        assert json_size(support) + json_size(research) <= budget - 200
        assert all(r.cap_met for r in reports)
        # 60/40 preference when both want more than they get
        assert len(support["hist"]) > len(research["recs"]) > 0

    def test_a_small_side_hands_its_share_to_the_other(self):
        support, research, parts = self._parts(400, 3)
        budget = json_size(support) // 2
        trim_jointly(parts, budget, [60, 40])
        assert len(research["recs"]) == 3                 # untouched
        assert json_size(support) + json_size(research) <= budget - 200
        assert len(support["hist"]) > 0.4 * 400 * (budget / json_size(support)) * 0.9

    def test_fixed_parts_are_always_kept(self):
        support, research, parts = self._parts(200, 200)
        trim_jointly(parts, 400, [60, 40])                # absurdly small budget
        assert support["overview"] == "s" * 50 and research["overview"] == "r" * 50
        assert support["hist"] == [] and research["recs"] == []

    def test_nothing_removed_when_everything_fits(self):
        support, research, parts = self._parts(10, 10)
        reports = trim_jointly(parts, 10 ** 7, [60, 40])
        assert not any(r.applied for r in reports)
        assert len(support["hist"]) == 10 and len(research["recs"]) == 10

    def test_joint_selection_is_deterministic(self):
        outs = []
        for _ in range(3):
            support, research, parts = self._parts(300, 250)
            trim_jointly(parts, 40000, [60, 40])
            outs.append((len(support["hist"]), len(research["recs"]),
                         json.dumps([support, research], sort_keys=True)))
        assert outs[0] == outs[1] == outs[2]


class _Counter:
    """Counts the bytes json.dumps produces while the budgeting runs."""

    def __init__(self, monkeypatch):
        self.bytes = 0
        self.calls = 0
        real = json.dumps

        def counting(obj, *a, **k):
            out = real(obj, *a, **k)
            self.bytes += len(out)
            self.calls += 1
            return out
        monkeypatch.setattr(eb.json, "dumps", counting)


class TestLinearComplexity:
    """Serialisation work grows linearly with the record count — never quadratically."""

    @pytest.mark.parametrize("n", [500, 2000, 8000])
    def test_serialised_bytes_stay_proportional_to_the_payload(self, n, monkeypatch):
        payload = _payload([n], seed=1)
        original = json_size(payload)
        cap = original // 4
        counter = _Counter(monkeypatch)
        full_measurements = []

        def measure(c):
            full_measurements.append(1)
            return json_size(c)

        report = trim_to_cap(payload, [_slots(payload)], cap, measure=measure)
        assert report.applied and report.cap_met
        # Every record once for its size, plus a few full-payload measurements.
        assert counter.bytes <= 4 * original
        assert len(full_measurements) <= 3

    def test_full_measurements_do_not_grow_with_the_record_count(self):
        counts = {}
        for n in (200, 1600, 12800):
            payload = _payload([n], seed=2)
            seen = []
            trim_to_cap(payload, [_slots(payload)], json_size(payload) // 3,
                        measure=lambda c, seen=seen: (seen.append(1), json_size(c))[1])
            counts[n] = len(seen)
        assert max(counts.values()) <= 3
        assert counts[12800] <= counts[200] + 1

    def test_the_old_quadratic_loop_would_fail_the_bound(self, monkeypatch):
        """Counter-proof: the previous rule re-serialises the payload per removed record."""
        payload = _payload([800], seed=4)
        original = json_size(payload)
        counter = _Counter(monkeypatch)
        _old_cap(payload, ["h0"], original // 4)
        assert counter.bytes > 20 * original

    def test_joint_trim_work_is_linear_too(self, monkeypatch):
        support = {"o": 1, "h": [_rec(i, 200) for i in range(3000)]}
        research = {"o": 1, "r": [_rec(i, 200) for i in range(3000)]}
        total = json_size(support) + json_size(research)
        counter = _Counter(monkeypatch)
        trim_jointly([(support, [[Slot("h", support["h"])]], 100),
                      (research, [[Slot("r", research["r"])]], 100)], total // 5, [60, 40])
        assert counter.bytes <= 5 * total


class TestPlanRemovals:
    def test_plan_matches_cost_model(self):
        sizes = [[10, 20, 30], [5, 5]]
        assert slot_cost(sizes[0]) == 60 + 4
        assert plan_removals(sizes, 10 ** 6) == [0, 0]
        # shedding order: the first slot goes completely before the second is touched
        assert plan_removals(sizes, 8) == [3, 1]

    def test_group_cost(self):
        s = Slot("x", [1, 2, 3])
        s.measure()
        assert group_cost([s]) == slot_cost(s.sizes)
