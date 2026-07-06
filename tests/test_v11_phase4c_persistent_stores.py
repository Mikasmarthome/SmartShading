"""Phase 4c — Persistent Support & Long-Term Research Store tests.

Covers:
  - _prune_support_events: age and count pruning
  - _prune_daily_buckets: age and count pruning
  - serialize/deserialize roundtrip for support_critical_events
  - serialize/deserialize roundtrip for research_daily_buckets
  - Support export merges persisted events for pre-restart coverage
  - Research export long_term_summary section
  - data_availability label
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.engines.learning_persistence import (
    LearningPersistenceConfig,
    LearningStore,
    RestoreExtras,
    _prune_daily_buckets,
    _prune_support_events,
    deserialize_into_learning_store,
    serialize_learning_store,
)
from custom_components.smartshading.engines.support_export import (
    build_support_export_v3,
)
from custom_components.smartshading.engines.research_export_v3 import (
    build_research_export_v3,
    build_research_export_all_zones,
)

_NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
_CFG = LearningPersistenceConfig()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_evt(ts_offset_h: float, event_type: str = "dispatch_sent",
              window_id: str = "w1") -> dict:
    ts = (_NOW - timedelta(hours=ts_offset_h)).isoformat()
    # resolved_state and reason must match what _support_timeline reclassifies to
    # event_type (since persisted records are re-classified from raw fields).
    _STATE_MAP = {
        "dispatch_sent": "normal_shade",
        "safety": "rain_safe",
        "recommendation_only": "normal_shade",
        "absence": "normal_shade",
        "manual_override": "manual_override",
    }
    _REASON_MAP = {
        "recommendation_only": "active_control_off",
    }
    return {
        "ts": ts,
        "window_id": window_id,
        "event_type": event_type,
        "decided_by": "Absence" if event_type == "absence" else "Adaptive",
        "resolved_state": _STATE_MAP.get(event_type, "normal_shade"),
        "target_ha": 0.4,
        "reason": _REASON_MAP.get(event_type),
        "is_recommendation_only": (event_type == "recommendation_only"),
    }


def _roundtrip(support_events=None, daily_buckets=None):
    store = LearningStore()
    data = serialize_learning_store(
        store, _CFG, _NOW,
        support_critical_events=support_events,
        research_daily_buckets=daily_buckets,
    )
    store2 = LearningStore()
    extras = deserialize_into_learning_store(data, store2, _CFG, _NOW)
    return extras


class _Coord:
    """Minimal duck-typed coordinator for export tests."""

    def __init__(self, *, support_events=None, daily_buckets=None,
                 ring_records=None):
        self._support_critical_events = list(support_events or [])
        self._research_daily_buckets = dict(daily_buckets or {})
        self._ring_records = list(ring_records or [])
        self.zones = {"z1": object()}
        self.windows = {}
        self.cover_groups = {}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._strategy_adoptions_active = {}
        self._pending_outcomes = type("PO", (), {"all_pending": lambda s: []})()

    def decision_trace_snapshot(self):
        if not self._ring_records:
            return {}
        return {"z1": {"records": self._ring_records, "count": len(self._ring_records)}}

    def get_decisions(self, _wid):
        return []

    def get_transitions(self, _wid):
        return []

    def get_overrides(self, _wid):
        return []

    def get_snapshots(self, _wid):
        return []

    def get_outcomes(self, _wid):
        return []

    def storage_diagnostics(self):
        return {}


# ===========================================================================
# _prune_support_events
# ===========================================================================

class TestPruneSupportEvents:
    def test_keeps_events_within_48h(self):
        events = [_make_evt(h) for h in (1, 23, 47)]
        result = _prune_support_events(events, _NOW)
        assert len(result) == 3

    def test_drops_events_older_than_48h(self):
        events = [_make_evt(h) for h in (1, 49, 72)]
        result = _prune_support_events(events, _NOW)
        assert len(result) == 1
        assert result[0] == events[0]

    def test_cap_at_500(self):
        events = [_make_evt(i * 0.05) for i in range(600)]
        result = _prune_support_events(events, _NOW)
        assert len(result) == 500

    def test_empty_list(self):
        assert _prune_support_events([], _NOW) == []

    def test_drops_events_with_missing_ts(self):
        events = [{"event_type": "dispatch_sent", "window_id": "w1"}]
        result = _prune_support_events(events, _NOW)
        assert result == []

    def test_drops_events_with_bad_ts(self):
        events = [{"ts": "not-a-date", "event_type": "dispatch_sent"}]
        result = _prune_support_events(events, _NOW)
        assert result == []


# ===========================================================================
# _prune_daily_buckets
# ===========================================================================

class TestPruneDailyBuckets:
    def test_keeps_recent_buckets(self):
        buckets = {
            "2025-06-14": {"decisions": 10},
            "2025-06-15": {"decisions": 5},
        }
        result = _prune_daily_buckets(buckets, _NOW)
        assert "2025-06-14" in result
        assert "2025-06-15" in result

    def test_drops_buckets_older_than_365_days(self):
        old_date = (_NOW - timedelta(days=400)).date().isoformat()
        recent_date = (_NOW - timedelta(days=10)).date().isoformat()
        buckets = {old_date: {"decisions": 1}, recent_date: {"decisions": 2}}
        result = _prune_daily_buckets(buckets, _NOW)
        assert old_date not in result
        assert recent_date in result

    def test_cap_at_365_entries(self):
        base = _NOW - timedelta(days=500)
        buckets = {
            (base + timedelta(days=i)).date().isoformat(): {"decisions": i}
            for i in range(400)
        }
        result = _prune_daily_buckets(buckets, _NOW)
        assert len(result) <= 365

    def test_empty_dict(self):
        assert _prune_daily_buckets({}, _NOW) == {}

    def test_ignores_non_string_keys(self):
        buckets = {123: {"decisions": 1}, "2025-06-15": {"decisions": 2}}
        result = _prune_daily_buckets(buckets, _NOW)
        assert 123 not in result
        assert "2025-06-15" in result


# ===========================================================================
# Persistence roundtrip
# ===========================================================================

class TestSupportEventRoundtrip:
    def test_events_survive_roundtrip(self):
        events = [_make_evt(1.0, "dispatch_sent"), _make_evt(2.0, "safety")]
        extras = _roundtrip(support_events=events)
        assert isinstance(extras, RestoreExtras)
        assert len(extras.support_critical_events) == 2

    def test_missing_field_events_dropped_on_restore(self):
        bad = [{"ts": _NOW.isoformat()}]  # missing event_type
        extras = _roundtrip(support_events=bad)
        assert extras.support_critical_events == []

    def test_old_events_pruned_at_serialize(self):
        events = [_make_evt(1.0), _make_evt(50.0)]  # 50h > 48h cutoff
        extras = _roundtrip(support_events=events)
        assert len(extras.support_critical_events) == 1

    def test_empty_events_roundtrip(self):
        extras = _roundtrip(support_events=None)
        assert extras.support_critical_events == []


class TestDailyBucketRoundtrip:
    def test_buckets_survive_roundtrip(self):
        buckets = {"2025-06-14": {"decisions": 42, "dispatched": 10}}
        extras = _roundtrip(daily_buckets=buckets)
        assert "2025-06-14" in extras.research_daily_buckets
        assert extras.research_daily_buckets["2025-06-14"]["decisions"] == 42

    def test_non_dict_value_dropped_on_restore(self):
        buckets = {"2025-06-14": "bad", "2025-06-15": {"decisions": 1}}
        extras = _roundtrip(daily_buckets=buckets)
        assert "2025-06-14" not in extras.research_daily_buckets
        assert "2025-06-15" in extras.research_daily_buckets

    def test_old_buckets_pruned_at_serialize(self):
        old = (_NOW - timedelta(days=400)).date().isoformat()
        buckets = {old: {"decisions": 5}, "2025-06-14": {"decisions": 3}}
        extras = _roundtrip(daily_buckets=buckets)
        assert old not in extras.research_daily_buckets
        assert "2025-06-14" in extras.research_daily_buckets

    def test_empty_buckets_roundtrip(self):
        extras = _roundtrip(daily_buckets=None)
        assert extras.research_daily_buckets == {}


# ===========================================================================
# Support export: persisted event merging
# ===========================================================================

class TestSupportTimelinePersistedMerge:
    def test_timeline_present_without_persisted_events(self):
        c = _Coord()
        out = build_support_export_v3(c, now=_NOW)
        tl = out["support_timeline"]
        assert isinstance(tl, dict)
        assert tl["since_restart_only"] is True

    def test_persisted_events_shift_coverage_scope(self):
        # 3h-old persisted event, ring is empty (restart happened 0h ago)
        persisted = [_make_evt(3.0, "safety")]
        c = _Coord(support_events=persisted)
        out = build_support_export_v3(c, now=_NOW)
        tl = out["support_timeline"]
        assert tl["since_restart_only"] is False
        assert tl["coverage_scope"] == "24h_window"

    def test_persisted_critical_events_appear_in_timeline(self):
        # Ring has a recent dispatch; persisted has an older safety event
        ring_ts = (_NOW - timedelta(minutes=30)).isoformat()
        ring_rec = {
            "decision_id": "d1",
            "window_id": "w1",
            "decision_timestamp_utc": ring_ts,
            "resolved_state": "normal_shade",
            "decided_by": "Adaptive",
            "no_dispatch": {"command_sent": True, "primary_reason": None},
            "target_chain": {"final_dispatched_target_ha": 0.4,
                             "recommendation_position_ha": 0.4},
        }
        persisted = [_make_evt(5.0, "safety")]
        c = _Coord(ring_records=[ring_rec], support_events=persisted)
        out = build_support_export_v3(c, now=_NOW)
        tl = out["support_timeline"]
        types = [e["event_type"] for e in tl["events"]]
        assert "dispatch_sent" in types
        assert "safety" in types

    def test_persisted_events_not_duplicated_when_covered_by_ring(self):
        # Ring covers ts X; persisted has same ts X — should NOT duplicate
        ts = (_NOW - timedelta(minutes=30)).isoformat()
        ring_rec = {
            "decision_id": "d1",
            "window_id": "w1",
            "decision_timestamp_utc": ts,
            "resolved_state": "normal_shade",
            "decided_by": "Adaptive",
            "no_dispatch": {"command_sent": True, "primary_reason": None},
            "target_chain": {"final_dispatched_target_ha": 0.4,
                             "recommendation_position_ha": 0.4},
        }
        # persisted event with SAME timestamp → ring's oldest ≤ ts, so skipped
        persisted = [{"ts": ts, "window_id": "w1", "event_type": "dispatch_sent",
                      "decided_by": "Adaptive", "resolved_state": "normal_shade",
                      "target_ha": 0.4, "reason": None, "is_recommendation_only": False}]
        c = _Coord(ring_records=[ring_rec], support_events=persisted)
        out = build_support_export_v3(c, now=_NOW)
        tl = out["support_timeline"]
        # Only 1 dispatch_sent, not 2
        dispatch_evts = [e for e in tl["events"] if e["event_type"] == "dispatch_sent"]
        assert len(dispatch_evts) == 1

    def test_no_crash_with_malformed_persisted_events(self):
        bad = [{"ts": "bad", "event_type": "safety"}]
        c = _Coord(support_events=bad)
        out = build_support_export_v3(c, now=_NOW)
        assert "support_timeline" in out


# ===========================================================================
# Research export: long_term_summary
# ===========================================================================

class TestLongTermSummary:
    def test_section_present_in_single_zone_export(self):
        c = _Coord()
        out = build_research_export_v3(c, now=_NOW)
        assert "long_term_summary" in out

    def test_section_present_in_all_zones_export(self):
        c = _Coord()
        out = build_research_export_all_zones([c], now=_NOW)
        assert "long_term_summary" in out

    def test_available_false_when_no_buckets(self):
        c = _Coord()
        out = build_research_export_v3(c, now=_NOW)
        lts = out["long_term_summary"]
        assert lts["available"] is False
        assert lts["bucket_count"] == 0

    def test_available_true_with_buckets(self):
        c = _Coord(daily_buckets={"2025-06-14": {"decisions": 10, "dispatched": 3}})
        out = build_research_export_v3(c, now=_NOW)
        lts = out["long_term_summary"]
        assert lts["available"] is True
        assert lts["bucket_count"] == 1
        assert lts["buckets"][0]["date"] == "2025-06-14"
        assert lts["buckets"][0]["decisions"] == 10

    def test_all_zones_aggregates_counts(self):
        c1 = _Coord(daily_buckets={"2025-06-14": {"decisions": 10, "dispatched": 3}})
        c2 = _Coord(daily_buckets={"2025-06-14": {"decisions": 5, "dispatched": 1}})
        out = build_research_export_all_zones([c1, c2], now=_NOW)
        lts = out["long_term_summary"]
        assert lts["available"] is True
        assert lts["zone_count"] == 2
        bucket = lts["buckets"][0]
        assert bucket["decisions"] == 15
        assert bucket["dispatched"] == 4

    def test_buckets_sorted_oldest_first(self):
        c = _Coord(daily_buckets={
            "2025-06-15": {"decisions": 2},
            "2025-06-13": {"decisions": 4},
            "2025-06-14": {"decisions": 1},
        })
        out = build_research_export_v3(c, now=_NOW)
        dates = [b["date"] for b in out["long_term_summary"]["buckets"]]
        assert dates == sorted(dates)

    def test_data_availability_label_updated(self):
        c = _Coord()
        da = build_research_export_v3(c, now=_NOW)["data_availability"]
        assert da["long_term_decision_evolution"] == "available_from_daily_buckets"

    def test_all_zones_data_availability_label(self):
        c = _Coord()
        da = build_research_export_all_zones([c], now=_NOW)["data_availability"]
        assert da["long_term_decision_evolution"] == "available_from_daily_buckets"

    def test_json_safe(self):
        c = _Coord(daily_buckets={"2025-06-14": {"decisions": 10}})
        out = build_research_export_v3(c, now=_NOW)
        json.dumps(out)  # must not raise
