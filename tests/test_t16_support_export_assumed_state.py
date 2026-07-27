"""T16 — support export now surfaces AssumedStateManager confidence/drift
per window (previously computed and used internally, but never exported —
a support case had no visibility into how trustworthy an assumed position
was)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartshading.cover_control.assumed_state_manager import (
    AssumedStateManager,
)
from custom_components.smartshading.engines.support_export import build_support_export_v3
from custom_components.smartshading.models.cover_group import CoverGroup, CoverHardwareType
from custom_components.smartshading.models.window import WindowConfig

_NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


class _Coord:
    def __init__(self, *, assumed_state_manager=None) -> None:
        self.zones = {"z1": object()}
        self.windows = {"w1": WindowConfig(
            id="w1", name="Window 1", zone_id="z1", azimuth=180, floor_level=0,
            cover_group_id="cg1",
        )}
        self.cover_groups = {"cg1": CoverGroup(
            id="cg1", window_id="w1", cover_ids=["cover.rts"],
            hardware_type=CoverHardwareType.GENERIC,
        )}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._pending_outcomes = type("PO", (), {"all_pending": lambda s: []})()
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._ring_records = []
        self.data = type("Data", (), {"execution_diagnostics": {}})()
        self.assumed_state_manager = assumed_state_manager or AssumedStateManager()
        from custom_components.smartshading.engines.learning_store import LearningStore
        self.learning_store = LearningStore()

    def decision_trace_snapshot(self):
        return {}

    def get_decisions(self, _wid):
        return []

    def storage_diagnostics(self):
        return {}


class TestAssumedStateSupportExportSection:
    def test_present_for_every_window(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        assert len(export["assumed_state"]) == 1

    def test_no_record_yet_reports_honest_unavailable(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        entry = next(iter(export["assumed_state"].values()))
        assert entry == {"available": False}

    def test_real_record_surfaces_confidence_and_uncertainty(self) -> None:
        mgr = AssumedStateManager()
        mgr.update("cover.rts", 42, _NOW, has_reliable_position_feedback=False)
        export = build_support_export_v3(_Coord(assumed_state_manager=mgr), now=_NOW)
        entry = next(iter(export["assumed_state"].values()))
        assert entry["available"] is True
        assert 0.0 <= entry["confidence"] <= 1.0
        assert entry["position_uncertainty_pct"] > 0.0
        assert entry["is_drift_suspected"] is False

    def test_stale_silence_surfaces_drift_suspected(self) -> None:
        mgr = AssumedStateManager()
        mgr.update("cover.rts", 42, _NOW, has_reliable_position_feedback=False)
        later = _NOW + timedelta(days=30)
        export = build_support_export_v3(_Coord(assumed_state_manager=mgr), now=later)
        entry = next(iter(export["assumed_state"].values()))
        assert entry["is_drift_suspected"] is True

    def test_never_raises_when_manager_missing(self) -> None:
        coord = _Coord()
        del coord.assumed_state_manager
        export = build_support_export_v3(coord, now=_NOW)
        entry = next(iter(export["assumed_state"].values()))
        assert entry is None
