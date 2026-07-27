"""T21 Phase D — Support Export timeline noise aggregation + severity model.

Covers:
  - _classify_severity(): the 5-level event_type -> severity mapping.
  - _aggregate_repeated_events(): consecutive-identical-key collapsing into
    occurrence_count/first_seen/last_seen, run-breaking on any key change,
    and the never-aggregate exemption for dispatch_sent/dispatch_failed.
  - End-to-end wiring through build_support_export_v3()'s support_timeline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartshading.engines.support_export import (
    _GUARANTEED_EVENT_TYPES,
    _aggregate_repeated_events,
    _classify_severity,
    build_support_export_v3,
)

_NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


class _Coord:
    """Minimal duck-typed coordinator for export tests (same shape as
    test_v11_phase4c_persistent_stores.py's _Coord)."""

    def __init__(self, *, ring_records=None):
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._ring_records = list(ring_records or [])
        self.zones = {"z1": object()}
        self.windows = {}
        self.cover_groups = {}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
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


def _hold_ring_rec(ts, *, reason="behavior_mode_hold", target=0.4):
    return {
        "decision_id": f"d-{ts}",
        "window_id": "w1",
        "decision_timestamp_utc": ts,
        "resolved_state": "normal_shade",
        "decided_by": "Adaptive",
        "no_dispatch": {"command_sent": False, "primary_reason": reason},
        "target_chain": {"resolved_target_position_ha": target},
    }


def _dispatch_ring_rec(ts, *, target=0.4):
    return {
        "decision_id": f"d-{ts}",
        "window_id": "w1",
        "decision_timestamp_utc": ts,
        "resolved_state": "normal_shade",
        "decided_by": "Adaptive",
        "no_dispatch": {"command_sent": True, "primary_reason": None},
        "target_chain": {"final_dispatched_target_ha": target,
                          "recommendation_position_ha": target},
    }


class TestClassifySeverity:
    def test_dispatch_sent_is_info(self):
        assert _classify_severity("dispatch_sent") == "info"

    def test_dispatch_failed_is_error(self):
        assert _classify_severity("dispatch_failed") == "error"

    def test_safety_manual_override_absence_night_are_state_change(self):
        for event_type in ("safety", "manual_override", "absence", "night_transition"):
            assert _classify_severity(event_type) == "state_change"

    def test_command_blocked_is_warning(self):
        assert _classify_severity("command_blocked") == "warning"

    def test_expected_holds_are_info_not_critical(self):
        # T21 Phase D's core complaint: an expected hold (behavior_mode:hold,
        # no_target_position) must not read as a critical support event.
        for event_type in ("behavior_hold", "presence_hold", "min_interval", "startup_grace", "no_change"):
            assert _classify_severity(event_type) == "info"

    def test_unknown_event_type_defaults_to_info_not_raise(self):
        assert _classify_severity("some_future_event_type_v99") == "info"


class TestAggregateRepeatedEvents:
    def _evt(self, ts, **overrides):
        base = {
            "ts": ts, "local_ts": ts, "event_type": "behavior_hold",
            "window_ref": "w1", "shading_state": "normal_shade",
            "decided_by": "Adaptive", "reason": "behavior_mode_hold",
            "target_ha": 0.4, "is_critical": False, "severity": "info",
        }
        base.update(overrides)
        return base

    def test_identical_consecutive_events_collapse_to_one(self):
        events = [self._evt(f"t{i}") for i in range(5, 0, -1)]  # newest-first
        out = _aggregate_repeated_events(events)
        assert len(out) == 1
        assert out[0]["occurrence_count"] == 5
        assert out[0]["last_seen"] == "t5"
        assert out[0]["first_seen"] == "t1"

    def test_single_event_is_not_wrapped_in_aggregate_fields(self):
        out = _aggregate_repeated_events([self._evt("t1")])
        assert len(out) == 1
        assert "occurrence_count" not in out[0]

    def test_state_change_breaks_the_run(self):
        events = [
            self._evt("t3", shading_state="rain_safe", event_type="safety",
                      reason=None, decided_by="Safety"),
            self._evt("t2"),
            self._evt("t1"),
        ]
        out = _aggregate_repeated_events(events)
        assert len(out) == 2
        assert out[0]["event_type"] == "safety"
        assert "occurrence_count" not in out[0]
        assert out[1]["occurrence_count"] == 2

    def test_different_window_does_not_merge_into_same_aggregate(self):
        events = [
            self._evt("t2", window_ref="w2"),
            self._evt("t1", window_ref="w1"),
        ]
        out = _aggregate_repeated_events(events)
        assert len(out) == 2
        assert all("occurrence_count" not in e for e in out)

    def test_dispatch_events_are_never_aggregated_even_with_identical_keys(self):
        events = [
            self._evt("t2", event_type="dispatch_sent", reason=None),
            self._evt("t1", event_type="dispatch_sent", reason=None),
        ]
        out = _aggregate_repeated_events(events)
        assert len(out) == 2
        assert all("occurrence_count" not in e for e in out)

    def test_empty_list(self):
        assert _aggregate_repeated_events([]) == []


class TestEndToEndTimelineWiring:
    def test_repeated_identical_holds_are_aggregated_in_the_export(self):
        recs = [
            _hold_ring_rec((_NOW - timedelta(minutes=5 * i)).isoformat())
            for i in range(6)
        ]
        c = _Coord(ring_records=recs)
        out = build_support_export_v3(c, now=_NOW)
        tl = out["support_timeline"]
        hold_events = [e for e in tl["events"] if e["event_type"] == "behavior_hold"]
        assert len(hold_events) == 1
        assert hold_events[0]["occurrence_count"] == 6
        assert tl["repeated_events_aggregated"] == 5

    def test_real_dispatch_is_never_aggregated_away(self):
        recs = [
            _hold_ring_rec((_NOW - timedelta(minutes=15)).isoformat()),
            _dispatch_ring_rec((_NOW - timedelta(minutes=10)).isoformat()),
            _hold_ring_rec((_NOW - timedelta(minutes=5)).isoformat()),
        ]
        c = _Coord(ring_records=recs)
        out = build_support_export_v3(c, now=_NOW)
        tl = out["support_timeline"]
        dispatch_events = [e for e in tl["events"] if e["event_type"] == "dispatch_sent"]
        assert len(dispatch_events) == 1
        assert "occurrence_count" not in dispatch_events[0]

    def test_expected_hold_event_severity_is_info(self):
        recs = [_hold_ring_rec(_NOW.isoformat())]
        c = _Coord(ring_records=recs)
        out = build_support_export_v3(c, now=_NOW)
        tl = out["support_timeline"]
        hold_events = [e for e in tl["events"] if e["event_type"] == "behavior_hold"]
        assert hold_events[0]["severity"] == "info"
        assert hold_events[0]["is_critical"] is False

    def test_guaranteed_event_types_are_exactly_the_documented_set(self):
        assert _GUARANTEED_EVENT_TYPES == frozenset({
            "dispatch_sent", "dispatch_failed", "safety", "manual_override",
            "absence", "night_transition",
        })
