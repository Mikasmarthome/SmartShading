"""Tests for cover_control/dispatch_batch.py — pure batch grouping for
genuine concurrent (PARALLEL) dispatch (v1.2.0-beta.1, T11.1).

Coverage:
  BAT-01  Safety items always form their own fast-lane batch, first, even
          when interleaved with non-safety items in the input.
  BAT-02  No zone_batching: all non-safety items form exactly one batch.
  BAT-03  zone_batching: consecutive same-zone runs group into one batch
          each, in input order (already zone-ordered upstream).
  BAT-04  Non-consecutive same-zone runs are NOT merged (would violate the
          established F18 ordering guarantee) — each run is its own batch.
  BAT-05  Empty input -> no batches.
  BAT-06  Only safety items -> exactly one (safety) batch, no empty
          non-safety batch appended.
  BAT-07  Only non-safety items, zone_batching off -> exactly one batch, no
          empty safety batch appended.
"""
from __future__ import annotations

from custom_components.smartshading.cover_control.dispatch_batch import (
    DispatchItem,
    group_into_batches,
)


def _item(zone_id: str, is_safety: bool = False, payload: str = "x") -> DispatchItem:
    return DispatchItem(is_safety=is_safety, zone_id=zone_id, payload=payload)


class TestSafetyFastLane:
    def test_safety_batch_is_first_and_separate(self) -> None:
        items = [
            _item("z1", payload="a"),
            _item("z1", is_safety=True, payload="safety1"),
            _item("z2", payload="b"),
        ]
        batches = group_into_batches(items, zone_batching=False)
        assert batches[0].is_safety_batch is True
        assert [i.payload for i in batches[0].items] == ["safety1"]

    def test_multiple_safety_items_grouped_together(self) -> None:
        items = [
            _item("z1", is_safety=True, payload="s1"),
            _item("z2", is_safety=True, payload="s2"),
        ]
        batches = group_into_batches(items, zone_batching=True)
        assert len(batches) == 1
        assert batches[0].is_safety_batch is True
        assert [i.payload for i in batches[0].items] == ["s1", "s2"]


class TestNoZoneBatching:
    def test_all_non_safety_in_one_batch(self) -> None:
        items = [_item("z1", payload="a"), _item("z2", payload="b"), _item("z1", payload="c")]
        batches = group_into_batches(items, zone_batching=False)
        assert len(batches) == 1
        assert batches[0].is_safety_batch is False
        assert [i.payload for i in batches[0].items] == ["a", "b", "c"]


class TestZoneBatching:
    def test_consecutive_same_zone_runs_group_separately(self) -> None:
        items = [
            _item("z1", payload="a1"), _item("z1", payload="a2"),
            _item("z2", payload="b1"),
        ]
        batches = group_into_batches(items, zone_batching=True)
        assert len(batches) == 2
        assert batches[0].zone_id == "z1"
        assert [i.payload for i in batches[0].items] == ["a1", "a2"]
        assert batches[1].zone_id == "z2"
        assert [i.payload for i in batches[1].items] == ["b1"]

    def test_non_consecutive_same_zone_not_merged(self) -> None:
        # Should not happen given upstream ordering, but must not silently
        # merge if it ever does — that would violate the established F18
        # ordering guarantee.
        items = [
            _item("z1", payload="a1"),
            _item("z2", payload="b1"),
            _item("z1", payload="a2"),  # z1 again, non-consecutive
        ]
        batches = group_into_batches(items, zone_batching=True)
        assert len(batches) == 3
        assert [b.zone_id for b in batches] == ["z1", "z2", "z1"]


class TestEmptyAndSingleTypeInputs:
    def test_empty_input_no_batches(self) -> None:
        assert group_into_batches([], zone_batching=False) == []
        assert group_into_batches([], zone_batching=True) == []

    def test_only_safety_no_empty_non_safety_batch(self) -> None:
        items = [_item("z1", is_safety=True, payload="s1")]
        batches = group_into_batches(items, zone_batching=True)
        assert len(batches) == 1
        assert batches[0].is_safety_batch is True

    def test_only_non_safety_no_empty_safety_batch(self) -> None:
        items = [_item("z1", payload="a")]
        batches = group_into_batches(items, zone_batching=False)
        assert len(batches) == 1
        assert batches[0].is_safety_batch is False
