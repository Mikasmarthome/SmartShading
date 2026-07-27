"""T21 Final Correction — exactly one Support Export (no detail_level, no
standard/extended split) and one Research Export. Covers: API surface
(no detail_level anywhere), Support Export content required for short-term
fault diagnosis, exclusion of long-term-only data, and Support/Research
separation of concerns.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

from custom_components.smartshading.engines import reason_codes as rc
from custom_components.smartshading.engines.research_export_v3 import (
    build_research_export_v3,
)
from custom_components.smartshading.engines.support_export import (
    SUPPORT_EXPORT_SCHEMA_VERSION,
    build_support_export_all_zones,
    build_support_export_v3,
)
from custom_components.smartshading.models.window import WindowConfig

_NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


def _decision_record(**overrides) -> dict:
    base = {
        "decision_id": "dec-1",
        "window_id": "w1",
        "decision_timestamp_utc": _NOW.isoformat(),
        "resolved_state": "normal_shade",
        "decided_by": "Adaptive",
        "authorities": {
            "safety_authority": {"active": False},
            "manual_override_authority": {"active": False},
            "lifecycle_authority": {"active": False},
            "absence_authority": {"active": False},
            "position_learning_authority": {"applied": True},
            "harmonization_authority": {"applied": False},
            "command_filter_authority": {"blocked": False, "reason_code": None},
            "dispatch_authority": {"applied": True, "blocked": False, "reason_code": None},
        },
        "no_dispatch": {
            "recommendation_exists": True, "command_sent": True,
            "primary_reason": None, "contributing_reasons": [],
        },
        "target_chain": {
            "recommendation_position_ha": 0.4,
            "resolved_target_position_ha": 0.6,
            "final_dispatched_target_ha": 0.6,
        },
        "dispatch_context": {},
    }
    base.update(overrides)
    return base


class _SolarSelection:
    def __init__(self):
        self.source = "measured"
        self.quality = "good"
        self.measured_wm2 = 350.0
        self.measured_valid = True
        self.estimated_wm2 = None
        self.fallback_reason = None
        self.cloud_not_applied_reason = None


class _Exposure:
    def __init__(self):
        self.direct_radiation_factor = 0.9
        self.theoretical_exposure = 315.0
        self.learned_solar_impact_factor = 1.1
        self.seasonal_factor = 1.0
        self.sun_azimuth = 180.0
        self.sun_elevation = 45.0
        self.azimuth_delta_deg = 5.0
        self.is_in_tolerance_window = True
        self.is_above_horizon = True
        self.effective_exposure = 300.0


class _Resolution:
    def __init__(self):
        self.applied_learned_delta_light = 0.0
        self.applied_learned_delta_normal = 15.0
        self.applied_learned_delta_strong = 0.0
        self.applied_forecast_delta = 10.0
        self.effective_light_wm2 = 120.0
        self.effective_normal_wm2 = 250.0
        self.effective_strong_wm2 = 400.0
        self.forecast_trust_level = "high"


class _Coord:
    def __init__(self, *, records=None):
        self.zones = {"z1": object()}
        self.windows = {"w1": WindowConfig(
            id="w1", name="Window 1", zone_id="z1", azimuth=180, floor_level=0,
            cover_group_id="cg1",
        )}
        self.cover_groups = {}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._pending_outcomes = type("PO", (), {"all_pending": lambda s: []})()
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._records = records if records is not None else [_decision_record()]
        self.data = type("Data", (), {"execution_diagnostics": {}})()
        from custom_components.smartshading.engines.learning_store import LearningStore
        self.learning_store = LearningStore()
        self._learning_store = self.learning_store

        self._cycle_solar_provenance = {"w1": {
            "exposure": _Exposure(),
            "solar_source": "measured",
            "solar_selection": _SolarSelection(),
            "base_solar_wm2": 330.0,
            "glare_min_exposure_wm2": 200.0,
            "glare_protection_enabled": True,
            "configured_light_wm2": 100.0,
            "configured_normal_wm2": 200.0,
            "configured_strong_wm2": 350.0,
        }}
        self._cycle_solar_resolution = {"w1": _Resolution()}
        self._cycle_adoption_applied = {"w1": {"normal": {"adoption_id": "adopt-1"}}}

    def decision_trace_snapshot(self):
        return {"z1": {"records": self._records, "count": len(self._records)}}

    def get_decisions(self, _wid):
        return []

    def storage_diagnostics(self):
        return {}

    def adoption_diagnostics(self, _wid):
        return {
            "intensities": {
                "light": {},
                "normal": {
                    "adoption_id": "adopt-1", "adoption_status": "active",
                    "adopted_delta_ha": 0.08, "adoption_confidence": 0.9,
                    "adoption_reliability": 0.85, "source_experiment_count": 3,
                },
                "strong": {
                    "adoption_id": "adopt-2", "adoption_status": "suspended",
                    "suspended": True, "current_gate_reason": "context_incompatible",
                    "rollback_reason": None,
                },
            },
        }


class TestExportCountAndApi:
    def test_no_detail_level_parameter_on_builder(self) -> None:
        sig = inspect.signature(build_support_export_v3)
        assert "detail_level" not in sig.parameters

    def test_no_detail_level_parameter_on_all_zones_builder(self) -> None:
        sig = inspect.signature(build_support_export_all_zones)
        assert "detail_level" not in sig.parameters

    def test_export_never_carries_a_detail_level_key(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        assert "detail_level" not in export

    def test_all_zones_export_never_carries_a_detail_level_key(self) -> None:
        export = build_support_export_all_zones([_Coord()], now=_NOW)
        assert "detail_level" not in export
        assert "detail_level" not in export["zones"][0]

    def test_passing_detail_level_raises_typeerror(self) -> None:
        # proves there is no silently-ignored legacy kwarg still accepted.
        try:
            build_support_export_v3(_Coord(), now=_NOW, detail_level="extended")
        except TypeError:
            pass
        else:
            raise AssertionError("detail_level must no longer be an accepted kwarg")

    def test_existing_call_without_any_new_kwargs_still_works(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW, integration_version="1.2.0-beta.1")
        assert export["support_export_schema_version"] == SUPPORT_EXPORT_SCHEMA_VERSION

    def test_research_call_still_works(self) -> None:
        export = build_research_export_v3(_Coord(), now=_NOW)
        assert "research_export_schema_version" in export

    def test_exactly_two_export_functions_in_module_namespace(self) -> None:
        import custom_components.smartshading.engines.support_export as se
        import custom_components.smartshading.engines.research_export_v3 as re_mod
        support_builders = [n for n in dir(se) if n.startswith("build_support_export")]
        research_builders = [n for n in dir(re_mod) if n.startswith("build_research_export")]
        # each module exposes a per-zone + all-zones variant of exactly ONE
        # export kind — no third "compact"/"basic"/"debug" build function.
        assert set(support_builders) == {"build_support_export_v3", "build_support_export_all_zones"}
        assert set(research_builders) == {"build_research_export_v3", "build_research_export_all_zones"}


class TestSupportExportContent:
    def _export(self):
        return build_support_export_v3(_Coord(), now=_NOW)

    def test_current_decision_records_present(self) -> None:
        export = self._export()
        assert export["current_decisions"]
        assert export["recent_decisions"]

    def test_timeline_present_and_aggregating(self) -> None:
        export = self._export()
        assert export["support_timeline"]["events"]
        assert "repeated_events_aggregated" in export["support_timeline"]

    def test_dispatch_events_and_failures_present(self) -> None:
        export = self._export()
        assert "recent_dispatches" in export
        rec = _decision_record(no_dispatch={
            "recommendation_exists": True, "command_sent": False,
            "primary_reason": "service_call_error", "contributing_reasons": [],
        })
        rec["authorities"]["dispatch_authority"] = {
            "applied": False, "blocked": False, "reason_code": "service_call_error",
        }
        export2 = build_support_export_v3(_Coord(records=[rec]), now=_NOW)
        events = export2["support_timeline"]["events"]
        assert any(e["event_type"] == "dispatch_failed" for e in events)

    def test_blocked_commands_visible(self) -> None:
        export = self._export()
        assert "recent_no_dispatches" in export

    def test_health_and_errors_visible(self) -> None:
        export = self._export()
        assert "health" in export
        assert "section_errors" in export

    def test_relevant_solar_data_visible(self) -> None:
        export = self._export()
        inputs = next(iter(export["inputs"].values()))
        assert inputs["solar"]["selected_solar_source"] == "measured"

    def test_active_learning_effect_visible(self) -> None:
        export = self._export()
        pl = next(iter(export["position_learning"].values()))
        assert "normal" in pl["active_effects"]

    def test_blocked_learning_effect_visible(self) -> None:
        export = self._export()
        pl = next(iter(export["position_learning"].values()))
        assert "strong" in pl["blocked_effects"]

    def test_assumed_state_present(self) -> None:
        export = self._export()
        assert "assumed_state" in export

    def test_target_position_zero_survives(self) -> None:
        from custom_components.smartshading.engines.support_export import (
            _compact_snapshot_entry,
        )
        entry = {
            "window_ref": "w", "data_available": True, "actual_position_ha": 0.5,
            "target_position_ha": 0, "cover_available": False,
        }
        out = _compact_snapshot_entry(entry)
        assert out["target_position_ha"] == 0

    def test_cover_available_false_survives(self) -> None:
        from custom_components.smartshading.engines.support_export import (
            _compact_snapshot_entry,
        )
        entry = {
            "window_ref": "w", "data_available": True, "actual_position_ha": 0.5,
            "target_position_ha": 0.5, "cover_available": False,
        }
        out = _compact_snapshot_entry(entry)
        assert out["cover_available"] is False

    def test_no_long_term_learning_history_section(self) -> None:
        export = self._export()
        assert "recent_learning_transitions" not in export

    def test_no_large_empty_learning_structures(self) -> None:
        export = self._export()
        pl = next(iter(export["position_learning"].values()))
        assert "intensities" not in pl
        assert "light" not in pl["active_effects"]

    def test_no_redundant_target_chain_without_transformation(self) -> None:
        rec = _decision_record()
        rec["target_chain"] = {
            "recommendation_position_ha": 0.4,
            "resolved_target_position_ha": 0.4,
            "final_dispatched_target_ha": 0.4,
        }
        export = build_support_export_v3(_Coord(records=[rec]), now=_NOW)
        cur = next(iter(export["current_decisions"].values()))
        assert cur["target_chain"] is None


class TestResearchExportContent:
    def _export(self):
        return build_research_export_v3(_Coord(), now=_NOW)

    def test_reason_codes_present_and_shared_collector(self) -> None:
        export = self._export()
        assert isinstance(export["reason_codes"], dict)

    def test_pseudonymization_present(self) -> None:
        export = self._export()
        assert "algorithm" in export["pseudonymization"]

    def test_long_term_sections_present(self) -> None:
        export = self._export()
        for key in ("development_summary", "long_term_summary", "survivorship",
                    "adoption_timeline", "aggregations", "history_metadata"):
            assert key in export


class TestSupportResearchSeparation:
    def test_support_lacks_long_term_research_sections(self) -> None:
        support = build_support_export_v3(_Coord(), now=_NOW)
        for key in ("development_summary", "long_term_summary", "survivorship",
                    "aggregations", "research_records"):
            assert key not in support

    def test_research_lacks_short_term_support_sections(self) -> None:
        research = build_research_export_v3(_Coord(), now=_NOW)
        for key in ("current_snapshot", "support_timeline", "explainability"):
            assert key not in research

    def test_no_hidden_third_export_path(self) -> None:
        import custom_components.smartshading.engines as engines_pkg
        import pkgutil
        names = [m.name for m in pkgutil.iter_modules(engines_pkg.__path__)]
        # modules whose name starts with "..._export" (the two real export
        # builders) — export_retention.py is a shared config helper, not a
        # third export path, so it must not match this stricter prefix check.
        export_modules = [n for n in names if n.endswith("_export") or n.endswith("_export_v3")]
        assert set(export_modules) == {"support_export", "research_export_v3"}
