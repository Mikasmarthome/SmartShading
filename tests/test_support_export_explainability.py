"""Tests for T13's `explainability` support export section and the new
`dispatch_failed` timeline event classification —
engines/support_export.py build_support_export_v3().
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smartshading.engines.adaptation_application import AdaptationTrace
from custom_components.smartshading.engines.support_export import build_support_export_v3
from custom_components.smartshading.models.window import WindowConfig

_NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


def _decision_record(**overrides) -> dict:
    base = {
        "decision_id": "dec-1",
        "window_id": "w1",
        "decision_timestamp_utc": _NOW.isoformat(),
        "resolved_state": "open",
        "decided_by": "HeatEvaluator",
        "authorities": {
            "safety_authority": {"active": False},
            "manual_override_authority": {"active": False},
            "lifecycle_authority": {"active": False},
            "absence_authority": {"active": False},
            "position_learning_authority": {"applied": False},
            "strategy_learning_authority": {"applied": False},
            "harmonization_authority": {"applied": False},
            "command_filter_authority": {"blocked": False, "reason_code": None},
            "dispatch_authority": {"applied": False, "blocked": False, "reason_code": None},
        },
        "no_dispatch": {
            "recommendation_exists": True, "command_sent": True,
            "primary_reason": None, "contributing_reasons": [],
        },
        "target_chain": {},
        "dispatch_context": {},
    }
    base.update(overrides)
    return base


class _Coord:
    def __init__(self, *, records=None, diag=None, adaptation_traces=None) -> None:
        self.zones = {"z1": object()}
        self.windows = {"w1": WindowConfig(
            id="w1", name="Window 1", zone_id="z1", azimuth=180, floor_level=0, cover_group_id="cg1",
        )}
        self.cover_groups = {}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._pending_outcomes = type("PO", (), {"all_pending": lambda s: []})()
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._ring_records = []
        self._records = records if records is not None else [_decision_record()]
        self.data = type("Data", (), {"execution_diagnostics": diag or {}})()
        self._adaptation_traces = adaptation_traces or {}
        from custom_components.smartshading.engines.learning_store import LearningStore
        self.learning_store = LearningStore()

    def decision_trace_snapshot(self):
        return {"z1": {"records": self._records, "count": len(self._records)}}

    def get_decisions(self, _wid):
        return []

    def storage_diagnostics(self):
        return {}


class TestExplainabilitySection:
    def test_present_for_every_window(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        assert "w1" not in export["explainability"]  # keys are pseudonymized
        assert len(export["explainability"]) == 1

    def test_reflects_latest_decision_record(self) -> None:
        rec = _decision_record(decided_by="StormEvaluator", resolved_state="storm_safe")
        export = build_support_export_v3(_Coord(records=[rec]), now=_NOW)
        entry = next(iter(export["explainability"].values()))
        assert entry["winning_rule"] == "StormEvaluator"
        assert entry["decided_state"] == "storm_safe"

    def test_safety_influence_surfaced(self) -> None:
        rec = _decision_record()
        rec["authorities"]["safety_authority"]["active"] = True
        export = build_support_export_v3(_Coord(records=[rec]), now=_NOW)
        entry = next(iter(export["explainability"].values()))
        assert entry["influences"]["safety"] is True

    def test_heat_diag_feeds_influence_and_why_not(self) -> None:
        diag = {"w1": type("D", (), {
            "heat_hysteresis_active": False, "heat_hysteresis_reason": "not_needed",
        })()}
        export = build_support_export_v3(_Coord(diag=diag), now=_NOW)
        entry = next(iter(export["explainability"].values()))
        assert entry["influences"]["heat_protection"] is False
        assert any(r["category"] == "heat_protection" for r in entry["why_not"])

    def test_adaptation_trace_feeds_influence(self) -> None:
        trace = AdaptationTrace(
            window_id="w1", computed_at_utc=_NOW,
            learning_active=True, confidence_level="very_high", adaptation_strength=0.3,
            heat_outdoor_original=28.0, heat_outdoor_adapted=27.0, heat_outdoor_factor=1.05,
            heat_indoor_original=24.0, heat_indoor_adapted=24.0, heat_indoor_factor=None,
            shade_position_original=60, shade_position_adapted=55, shade_position_factor=0.9,
            light_shade_threshold_original=150.0, light_shade_threshold_adapted=140.0,
            normal_shade_threshold_original=250.0, normal_shade_threshold_adapted=250.0,
            strong_shade_threshold_original=400.0, strong_shade_threshold_adapted=380.0,
            solar_escalation_factor_applied=1.05,
            exposure_factor_recorded=1.0, exposure_adaptation_applied=True,
            reason="very_high confidence",
        )
        export = build_support_export_v3(_Coord(adaptation_traces={"w1": trace}), now=_NOW)
        entry = next(iter(export["explainability"].values()))
        assert entry["influences"]["adaptation"] is True
        assert not any(r["category"] == "adaptation" for r in entry["why_not"])

    def test_why_not_surfaces_no_dispatch_reason(self) -> None:
        rec = _decision_record(no_dispatch={
            "recommendation_exists": True, "command_sent": False,
            "primary_reason": "min_interval_not_elapsed", "contributing_reasons": [],
        })
        export = build_support_export_v3(_Coord(records=[rec]), now=_NOW)
        entry = next(iter(export["explainability"].values()))
        assert entry["dispatched"] is False
        assert any(r["code"] == "min_interval_not_elapsed" for r in entry["why_not"])

    def test_decision_ref_pseudonymized_not_raw_id(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        entry = next(iter(export["explainability"].values()))
        assert entry.get("decision_ref") != "dec-1"
        assert "decision_id" not in entry

    def test_no_records_yields_honest_empty_explanation(self) -> None:
        export = build_support_export_v3(_Coord(records=[]), now=_NOW)
        entry = next(iter(export["explainability"].values()))
        assert entry["winning_rule"] is None
        assert entry["dispatched"] is False

    def test_never_raises_on_sparse_record(self) -> None:
        # Minimal record missing most optional keys — must not raise, and
        # must yield defensive (all-False) influences rather than crash.
        export = build_support_export_v3(
            _Coord(records=[{"window_id": "w1"}]), now=_NOW,
        )
        entry = next(iter(export["explainability"].values()))
        assert entry["influences"]["safety"] is False


class TestDispatchFailedTimelineEvent:
    def test_attempted_but_not_succeeded_classified_as_dispatch_failed(self) -> None:
        rec = _decision_record(no_dispatch={
            "recommendation_exists": True, "command_sent": False,
            "primary_reason": "service_call_error", "contributing_reasons": [],
        })
        rec["authorities"]["dispatch_authority"] = {
            "applied": False, "blocked": False, "reason_code": "service_call_error",
        }
        export = build_support_export_v3(_Coord(records=[rec]), now=_NOW)
        events = export["support_timeline"]["events"]
        assert any(e["event_type"] == "dispatch_failed" for e in events)

    def test_blocked_dispatch_is_not_classified_as_failed(self) -> None:
        rec = _decision_record(no_dispatch={
            "recommendation_exists": True, "command_sent": False,
            "primary_reason": "active_control_off", "contributing_reasons": [],
        })
        export = build_support_export_v3(_Coord(records=[rec]), now=_NOW)
        events = export["support_timeline"]["events"]
        assert not any(e["event_type"] == "dispatch_failed" for e in events)
        assert any(e["event_type"] == "recommendation_only" for e in events)

    def test_successful_dispatch_is_not_classified_as_failed(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        events = export["support_timeline"]["events"]
        assert not any(e["event_type"] == "dispatch_failed" for e in events)
        assert any(e["event_type"] == "dispatch_sent" for e in events)

    def test_idle_cycle_with_no_recommendation_is_not_classified_as_failed(self) -> None:
        # Nothing to dispatch at all this cycle (no recommendation exists) —
        # must never be mistaken for a genuine dispatch failure.
        rec = _decision_record(no_dispatch={
            "recommendation_exists": False, "command_sent": False,
            "primary_reason": None, "contributing_reasons": [],
        })
        export = build_support_export_v3(_Coord(records=[rec]), now=_NOW)
        events = export["support_timeline"]["events"]
        assert not any(e["event_type"] == "dispatch_failed" for e in events)
