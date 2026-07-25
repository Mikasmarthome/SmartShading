"""Tests for T12's recent_outcomes / recent_learning_transitions support
export sections — engines/support_export.py build_support_export_v3().

Before T12 these sections were hard-stubbed "not_recorded" even though the
underlying LearningStore ring buffers existed and were populated. T12 wires
them up: bounded, pseudonymized, newest-first.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartshading.engines.support_export import (
    MAX_SUPPORT_OUTCOMES_PER_ZONE,
    build_support_export_v3,
)
from custom_components.smartshading.engines.learning_store import LearningStore
from custom_components.smartshading.models.learning import (
    DecisionOutcome,
    StateTransitionRecord,
)
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.state_machine.states import ShadingState

_NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


class _Coord:
    """Minimal duck-typed coordinator exposing a real LearningStore, as the
    real SmartShadingCoordinator does via its `learning_store` property."""

    def __init__(self, *, store: LearningStore | None = None) -> None:
        self.zones = {"z1": object()}
        self.windows = {"w1": WindowConfig(
            id="w1", name="Window 1", zone_id="z1", azimuth=180, floor_level=0, cover_group_id="cg1",
        )}
        self.cover_groups = {}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._strategy_adoptions_active = {}
        self._pending_outcomes = type("PO", (), {"all_pending": lambda s: []})()
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._ring_records = []
        self.data = type("Data", (), {"execution_diagnostics": {}})()
        self.learning_store = store if store is not None else LearningStore()

    def decision_trace_snapshot(self):
        return {}

    def get_decisions(self, _wid):
        return []

    def storage_diagnostics(self):
        return {}


def _outcome(*, minutes_ago: int, score: float) -> DecisionOutcome:
    return DecisionOutcome(
        decision_timestamp=_NOW - timedelta(minutes=minutes_ago),
        window_id="w1",
        decided_state=ShadingState.NORMAL_SHADE,
        decided_by="HeatEvaluator",
        outcome_score=score,
        resolution_status="complete",
        evaluation_timestamp=_NOW - timedelta(minutes=minutes_ago) + timedelta(minutes=30),
    )


def _transition(*, minutes_ago: int) -> StateTransitionRecord:
    return StateTransitionRecord(
        timestamp=_NOW - timedelta(minutes=minutes_ago),
        window_id="w1",
        from_state=ShadingState.OPEN,
        to_state=ShadingState.NORMAL_SHADE,
        decided_by="HeatEvaluator",
        lifecycle_state="day",
        absence_active=False,
        is_in_solar_sector=True,
    )


class TestRecentOutcomesSection:
    def test_empty_store_yields_empty_records_not_stub(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        section = export["recent_outcomes"]
        assert section.get("section_status") != "not_recorded"
        assert section["records"] == []

    def test_outcomes_are_surfaced_newest_first(self) -> None:
        store = LearningStore()
        store.record_outcome(_outcome(minutes_ago=60, score=-0.5))
        store.record_outcome(_outcome(minutes_ago=10, score=0.5))
        export = build_support_export_v3(_Coord(store=store), now=_NOW)
        recs = export["recent_outcomes"]["records"]
        assert len(recs) == 2
        assert recs[0]["outcome_score"] == 0.5
        assert recs[1]["outcome_score"] == -0.5

    def test_window_id_is_pseudonymized(self) -> None:
        store = LearningStore()
        store.record_outcome(_outcome(minutes_ago=5, score=0.1))
        export = build_support_export_v3(_Coord(store=store), now=_NOW)
        rec = export["recent_outcomes"]["records"][0]
        assert rec["window_ref"] != "w1"
        assert "window_id" not in rec

    def test_multi_objective_breakdown_present_when_available(self) -> None:
        from custom_components.smartshading.models.multi_objective_outcome import (
            MultiObjectiveOutcome, ThermalOutcome,
        )
        outcome = _outcome(minutes_ago=5, score=0.3)
        outcome = type(outcome)(
            **{**outcome.__dict__, "multi_objective": MultiObjectiveOutcome(
                thermal=ThermalOutcome(available=True, score=0.6),
            )},
        )
        store = LearningStore()
        store.record_outcome(outcome)
        export = build_support_export_v3(_Coord(store=store), now=_NOW)
        rec = export["recent_outcomes"]["records"][0]
        assert rec["multi_objective"]["thermal_available"] is True
        assert rec["multi_objective"]["thermal_score"] == 0.6

    def test_capped_and_truncation_reported(self) -> None:
        store = LearningStore()
        for i in range(MAX_SUPPORT_OUTCOMES_PER_ZONE + 20):
            store.record_outcome(_outcome(minutes_ago=i, score=0.0))
        export = build_support_export_v3(_Coord(store=store), now=_NOW)
        section = export["recent_outcomes"]
        assert len(section["records"]) <= MAX_SUPPORT_OUTCOMES_PER_ZONE
        assert section["truncation"]["truncated"] is True

    def test_missing_learning_store_fails_open_with_honest_status(self) -> None:
        coord = _Coord()
        del coord.learning_store
        export = build_support_export_v3(coord, now=_NOW)
        assert export["recent_outcomes"]["section_status"] == "not_recorded"


class TestRecentLearningTransitionsSection:
    def test_empty_store_yields_empty_records_not_stub(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        section = export["recent_learning_transitions"]
        assert section.get("section_status") != "not_recorded"
        assert section["records"] == []

    def test_transitions_are_surfaced_newest_first(self) -> None:
        store = LearningStore()
        store.record_transition(_transition(minutes_ago=45))
        store.record_transition(_transition(minutes_ago=5))
        export = build_support_export_v3(_Coord(store=store), now=_NOW)
        recs = export["recent_learning_transitions"]["records"]
        assert len(recs) == 2
        assert recs[0]["timestamp_utc"] > recs[1]["timestamp_utc"]

    def test_window_id_is_pseudonymized(self) -> None:
        store = LearningStore()
        store.record_transition(_transition(minutes_ago=5))
        export = build_support_export_v3(_Coord(store=store), now=_NOW)
        rec = export["recent_learning_transitions"]["records"][0]
        assert rec["window_ref"] != "w1"

    def test_export_never_raises_on_malformed_store(self) -> None:
        class _BrokenStore:
            def get_outcomes(self, *a, **kw):
                raise RuntimeError("boom")

            def get_transitions(self, *a, **kw):
                raise RuntimeError("boom")

        export = build_support_export_v3(_Coord(store=_BrokenStore()), now=_NOW)
        assert export["recent_outcomes"]["records"] == []
        assert export["recent_learning_transitions"]["records"] == []


class TestAdaptationTraceSection:
    def test_no_trace_yet_is_honest_not_recorded(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        section = export["adaptation_trace"][next(iter(export["adaptation_trace"]))]
        assert section["section_status"] == "not_recorded"

    def test_trace_surfaces_old_new_confidence_and_strength(self) -> None:
        from custom_components.smartshading.engines.adaptation_application import AdaptationTrace

        coord = _Coord()
        coord._adaptation_traces = {
            "w1": AdaptationTrace(
                window_id="w1", computed_at_utc=_NOW,
                learning_active=True, confidence_level="very_high",
                adaptation_strength=0.4,
                heat_outdoor_original=28.0, heat_outdoor_adapted=27.0, heat_outdoor_factor=1.05,
                heat_indoor_original=24.0, heat_indoor_adapted=24.0, heat_indoor_factor=None,
                shade_position_original=60, shade_position_adapted=55, shade_position_factor=0.9,
                light_shade_threshold_original=150.0, light_shade_threshold_adapted=140.0,
                normal_shade_threshold_original=250.0, normal_shade_threshold_adapted=250.0,
                strong_shade_threshold_original=400.0, strong_shade_threshold_adapted=380.0,
                solar_escalation_factor_applied=1.05,
                exposure_factor_recorded=1.0, exposure_adaptation_applied=True,
                reason="very_high confidence, thresholds adapted",
            ),
        }
        export = build_support_export_v3(coord, now=_NOW)
        section = export["adaptation_trace"][next(iter(export["adaptation_trace"]))]
        assert section["confidence_level"] == "very_high"
        assert section["adaptation_strength"] == 0.4
        assert section["parameters"]["heat_outdoor_threshold_c"]["old"] == 28.0
        assert section["parameters"]["heat_outdoor_threshold_c"]["new"] == 27.0
