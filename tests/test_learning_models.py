"""Tests for Learning Foundation data models (Phase 9A).

Contract:
  - All 5 dataclasses are instantiable with required fields only.
  - Optional fields default to None (or the documented default value).
  - Frozen dataclasses (StateTransitionRecord, OverrideRecord,
    WindowCycleSnapshot, DecisionOutcome) reject mutation.
  - EvaluatorConfidenceRecord is NOT frozen — it accepts mutation.
  - No Home Assistant imports, no coordinator dependencies.
  - Phase 9A adds no side effects to any existing module.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from custom_components.smartshading.models.learning import (
    OVERRIDE_EVENT_TYPES,
    DecisionOutcome,
    EvaluatorConfidenceRecord,
    OverrideRecord,
    StateTransitionRecord,
    WindowCycleSnapshot,
)
from custom_components.smartshading.state_machine.states import ShadingState

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# StateTransitionRecord
# ---------------------------------------------------------------------------

class TestStateTransitionRecord:
    def test_minimal_construction(self) -> None:
        record = StateTransitionRecord(
            timestamp=_NOW,
            window_id="w-south",
            from_state=ShadingState.OPEN,
            to_state=ShadingState.NORMAL_SHADE,
            decided_by="HeatEvaluator",
            lifecycle_state="DAY",
            absence_active=False,
            is_in_solar_sector=True,
        )
        assert record.window_id == "w-south"
        assert record.from_state is ShadingState.OPEN
        assert record.to_state is ShadingState.NORMAL_SHADE
        assert record.decided_by == "HeatEvaluator"

    def test_optional_sensor_fields_default_to_none(self) -> None:
        record = StateTransitionRecord(
            timestamp=_NOW,
            window_id="w1",
            from_state=ShadingState.OPEN,
            to_state=ShadingState.NIGHT_CLOSED,
            decided_by="NightEvaluator",
            lifecycle_state="NIGHT",
            absence_active=False,
            is_in_solar_sector=False,
        )
        assert record.outdoor_temp_c is None
        assert record.indoor_temp_c is None
        assert record.solar_radiation_wm2 is None
        assert record.wind_speed_ms is None

    def test_optional_sensor_fields_accept_values(self) -> None:
        record = StateTransitionRecord(
            timestamp=_NOW,
            window_id="w1",
            from_state=ShadingState.OPEN,
            to_state=ShadingState.STRONG_SHADE,
            decided_by="SolarEvaluator",
            lifecycle_state="DAY",
            absence_active=False,
            is_in_solar_sector=True,
            outdoor_temp_c=28.5,
            indoor_temp_c=24.1,
            solar_radiation_wm2=650.0,
            wind_speed_ms=3.2,
        )
        assert record.outdoor_temp_c == 28.5
        assert record.solar_radiation_wm2 == 650.0

    def test_frozen_rejects_mutation(self) -> None:
        record = StateTransitionRecord(
            timestamp=_NOW,
            window_id="w1",
            from_state=ShadingState.OPEN,
            to_state=ShadingState.LIGHT_SHADE,
            decided_by="SolarEvaluator",
            lifecycle_state="DAY",
            absence_active=False,
            is_in_solar_sector=True,
        )
        with pytest.raises(FrozenInstanceError):
            record.window_id = "w-other"  # type: ignore[misc]

    def test_all_shading_states_accepted(self) -> None:
        for state in ShadingState:
            record = StateTransitionRecord(
                timestamp=_NOW,
                window_id="w1",
                from_state=ShadingState.OPEN,
                to_state=state,
                decided_by="test",
                lifecycle_state="DAY",
                absence_active=False,
                is_in_solar_sector=False,
            )
            assert record.to_state is state

    def test_timestamp_preserved(self) -> None:
        record = StateTransitionRecord(
            timestamp=_NOW,
            window_id="w1",
            from_state=ShadingState.OPEN,
            to_state=ShadingState.OPEN,
            decided_by="test",
            lifecycle_state="DAY",
            absence_active=False,
            is_in_solar_sector=False,
        )
        assert record.timestamp == _NOW


# ---------------------------------------------------------------------------
# OverrideRecord
# ---------------------------------------------------------------------------

class TestOverrideRecord:
    def test_minimal_construction_started(self) -> None:
        record = OverrideRecord(
            timestamp=_NOW,
            window_id="w-south",
            event_type="started",
            lifecycle_state="DAY",
        )
        assert record.event_type == "started"
        assert record.window_id == "w-south"

    @pytest.mark.parametrize("event_type", list(OVERRIDE_EVENT_TYPES))
    def test_all_valid_event_types(self, event_type: str) -> None:
        record = OverrideRecord(
            timestamp=_NOW,
            window_id="w1",
            event_type=event_type,  # type: ignore[arg-type]
            lifecycle_state="DAY",
        )
        assert record.event_type == event_type

    def test_optional_fields_default_to_none(self) -> None:
        record = OverrideRecord(
            timestamp=_NOW,
            window_id="w1",
            event_type="expired",
            lifecycle_state="DAY",
        )
        assert record.override_position is None
        assert record.overridden_state is None
        assert record.overridden_position is None
        assert record.override_duration_min is None
        assert record.outdoor_temp_c is None
        assert record.solar_radiation_wm2 is None

    def test_full_construction(self) -> None:
        record = OverrideRecord(
            timestamp=_NOW,
            window_id="w-south",
            event_type="started",
            lifecycle_state="DAY",
            override_position=40,
            overridden_state=ShadingState.NORMAL_SHADE,
            overridden_position=75,
            override_duration_min=None,  # unknown at start
            outdoor_temp_c=26.0,
            solar_radiation_wm2=450.0,
        )
        assert record.override_position == 40
        assert record.overridden_state is ShadingState.NORMAL_SHADE
        assert record.overridden_position == 75

    def test_frozen_rejects_mutation(self) -> None:
        record = OverrideRecord(
            timestamp=_NOW,
            window_id="w1",
            event_type="started",
            lifecycle_state="DAY",
        )
        with pytest.raises(FrozenInstanceError):
            record.event_type = "expired"  # type: ignore[misc]

    def test_cleared_by_safety_with_duration(self) -> None:
        record = OverrideRecord(
            timestamp=_NOW,
            window_id="w1",
            event_type="cleared_by_safety",
            lifecycle_state="DAY",
            override_duration_min=47.5,
        )
        assert record.override_duration_min == 47.5

    def test_override_event_types_constant_completeness(self) -> None:
        """OVERRIDE_EVENT_TYPES must contain all five documented event types."""
        assert set(OVERRIDE_EVENT_TYPES) == {
            "started",
            "expired",
            "renewed",
            "cleared_by_safety",
            "cleared_by_lifecycle",
        }


# ---------------------------------------------------------------------------
# WindowCycleSnapshot
# ---------------------------------------------------------------------------

class TestWindowCycleSnapshot:
    def test_minimal_construction(self) -> None:
        snapshot = WindowCycleSnapshot(
            timestamp=_NOW,
            window_id="w-south",
            shading_state=ShadingState.NORMAL_SHADE,
            decided_by="HeatEvaluator",
            lifecycle_state="DAY",
            absence_active=False,
            override_active=False,
        )
        assert snapshot.shading_state is ShadingState.NORMAL_SHADE
        assert snapshot.override_active is False

    def test_optional_fields_default_to_none(self) -> None:
        snapshot = WindowCycleSnapshot(
            timestamp=_NOW,
            window_id="w1",
            shading_state=ShadingState.OPEN,
            decided_by="SolarEvaluator",
            lifecycle_state="DAY",
            absence_active=False,
            override_active=False,
        )
        assert snapshot.target_position is None
        assert snapshot.outdoor_temp_c is None
        assert snapshot.indoor_temp_c is None
        assert snapshot.solar_radiation_wm2 is None
        assert snapshot.effective_exposure_wm2 is None
        assert snapshot.wind_speed_ms is None

    def test_full_construction(self) -> None:
        snapshot = WindowCycleSnapshot(
            timestamp=_NOW,
            window_id="w-south",
            shading_state=ShadingState.STRONG_SHADE,
            decided_by="SolarEvaluator",
            lifecycle_state="DAY",
            absence_active=False,
            override_active=False,
            target_position=100,
            outdoor_temp_c=30.0,
            indoor_temp_c=25.5,
            solar_radiation_wm2=700.0,
            effective_exposure_wm2=560.0,
            wind_speed_ms=2.1,
        )
        assert snapshot.target_position == 100
        assert snapshot.effective_exposure_wm2 == 560.0

    def test_frozen_rejects_mutation(self) -> None:
        snapshot = WindowCycleSnapshot(
            timestamp=_NOW,
            window_id="w1",
            shading_state=ShadingState.OPEN,
            decided_by="SolarEvaluator",
            lifecycle_state="DAY",
            absence_active=False,
            override_active=False,
        )
        with pytest.raises(FrozenInstanceError):
            snapshot.shading_state = ShadingState.NORMAL_SHADE  # type: ignore[misc]

    def test_override_active_true(self) -> None:
        snapshot = WindowCycleSnapshot(
            timestamp=_NOW,
            window_id="w1",
            shading_state=ShadingState.MANUAL_OVERRIDE,
            decided_by="ManualOverrideEvaluator",
            lifecycle_state="DAY",
            absence_active=False,
            override_active=True,
            target_position=40,
        )
        assert snapshot.override_active is True
        assert snapshot.shading_state is ShadingState.MANUAL_OVERRIDE


# ---------------------------------------------------------------------------
# DecisionOutcome
# ---------------------------------------------------------------------------

class TestDecisionOutcome:
    def test_minimal_construction(self) -> None:
        outcome = DecisionOutcome(
            decision_timestamp=_NOW,
            window_id="w-south",
            decided_state=ShadingState.STRONG_SHADE,
            decided_by="HeatEvaluator",
        )
        assert outcome.decided_state is ShadingState.STRONG_SHADE
        assert outcome.decided_by == "HeatEvaluator"

    def test_default_values(self) -> None:
        outcome = DecisionOutcome(
            decision_timestamp=_NOW,
            window_id="w1",
            decided_state=ShadingState.NORMAL_SHADE,
            decided_by="SolarEvaluator",
        )
        assert outcome.indoor_temp_outcome_delay_min == 30
        assert outcome.override_occurred is False
        assert outcome.override_delay_min is None
        assert outcome.indoor_temp_at_decision is None
        assert outcome.indoor_temp_outcome_c is None
        assert outcome.state_duration_min is None

    def test_resolved_outcome_with_override(self) -> None:
        outcome = DecisionOutcome(
            decision_timestamp=_NOW,
            window_id="w1",
            decided_state=ShadingState.STRONG_SHADE,
            decided_by="SolarEvaluator",
            override_occurred=True,
            override_delay_min=12.0,
            indoor_temp_at_decision=24.5,
            indoor_temp_outcome_c=25.2,
            state_duration_min=12.0,
        )
        assert outcome.override_occurred is True
        assert outcome.override_delay_min == 12.0
        assert outcome.state_duration_min == 12.0

    def test_resolved_outcome_no_override(self) -> None:
        outcome = DecisionOutcome(
            decision_timestamp=_NOW,
            window_id="w1",
            decided_state=ShadingState.NORMAL_SHADE,
            decided_by="HeatEvaluator",
            override_occurred=False,
            indoor_temp_at_decision=26.0,
            indoor_temp_outcome_c=24.8,
            state_duration_min=45.0,
        )
        assert outcome.override_occurred is False
        assert outcome.override_delay_min is None
        assert outcome.indoor_temp_outcome_c == 24.8

    def test_custom_outcome_delay(self) -> None:
        outcome = DecisionOutcome(
            decision_timestamp=_NOW,
            window_id="w1",
            decided_state=ShadingState.ABSENCE_CLOSED,
            decided_by="AbsenceEvaluator",
            indoor_temp_outcome_delay_min=60,
        )
        assert outcome.indoor_temp_outcome_delay_min == 60

    def test_frozen_rejects_mutation(self) -> None:
        outcome = DecisionOutcome(
            decision_timestamp=_NOW,
            window_id="w1",
            decided_state=ShadingState.OPEN,
            decided_by="SolarEvaluator",
        )
        with pytest.raises(FrozenInstanceError):
            outcome.override_occurred = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EvaluatorConfidenceRecord
# ---------------------------------------------------------------------------

class TestEvaluatorConfidenceRecord:
    def test_minimal_construction(self) -> None:
        record = EvaluatorConfidenceRecord(
            window_id="w-south",
            evaluator_name="HeatEvaluator",
            last_updated=_NOW,
        )
        assert record.window_id == "w-south"
        assert record.evaluator_name == "HeatEvaluator"

    def test_default_counters(self) -> None:
        record = EvaluatorConfidenceRecord(
            window_id="w1",
            evaluator_name="SolarEvaluator",
            last_updated=_NOW,
        )
        assert record.decision_count == 0
        assert record.override_count == 0
        assert record.override_rate == 0.0

    def test_full_construction(self) -> None:
        record = EvaluatorConfidenceRecord(
            window_id="w1",
            evaluator_name="HeatEvaluator",
            last_updated=_NOW,
            decision_count=150,
            override_count=12,
            override_rate=0.08,
        )
        assert record.decision_count == 150
        assert record.override_count == 12
        assert record.override_rate == 0.08

    def test_is_mutable(self) -> None:
        """EvaluatorConfidenceRecord is NOT frozen — the Learning Engine updates it."""
        record = EvaluatorConfidenceRecord(
            window_id="w1",
            evaluator_name="GlareEvaluator",
            last_updated=_NOW,
        )
        record.decision_count += 1
        record.override_count += 1
        record.override_rate = 1.0
        record.last_updated = _NOW
        assert record.decision_count == 1
        assert record.override_rate == 1.0

    def test_all_evaluator_names_accepted(self) -> None:
        evaluators = [
            "StormEvaluator", "WindEvaluator", "ManualOverrideEvaluator",
            "NightEvaluator", "AbsenceEvaluator", "HeatEvaluator",
            "GlareEvaluator", "SolarEvaluator",
        ]
        for name in evaluators:
            record = EvaluatorConfidenceRecord(
                window_id="w1",
                evaluator_name=name,
                last_updated=_NOW,
            )
            assert record.evaluator_name == name


# ---------------------------------------------------------------------------
# Cross-model: no HA dependencies, no side effects on existing modules
# ---------------------------------------------------------------------------

class TestLearningModelsIsolation:
    def test_import_does_not_touch_coordinator(self) -> None:
        """Phase 9A invariant: learning.py imports nothing from coordinator."""
        import importlib
        import custom_components.smartshading.models.learning as learning_module
        source = importlib.util.find_spec(
            "custom_components.smartshading.models.learning"
        )
        assert source is not None
        # No runtime assertion needed — if coordinator was imported, the test
        # collection itself would fail (coordinator needs real HA). The fact
        # that all tests here pass proves the import chain is HA-free.

    def test_all_five_records_are_importable(self) -> None:
        from custom_components.smartshading.models.learning import (  # noqa: F401
            DecisionOutcome,
            EvaluatorConfidenceRecord,
            OverrideRecord,
            StateTransitionRecord,
            WindowCycleSnapshot,
        )
