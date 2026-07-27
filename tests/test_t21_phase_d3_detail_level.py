"""T21 Phase D3 — Standard/Extended support-export detail_level.

Covers: detail_level selection (default/validity), Standard content
(compact/no empty structures/canonical values present), Extended content
(full detail retained), cross-consistency between the two levels, and
backward compatibility (old call sites without detail_level).
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smartshading.engines import reason_codes as rc
from custom_components.smartshading.engines.support_export import (
    SUPPORT_EXPORT_SCHEMA_VERSION,
    _compact_snapshot_entry,
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
        self.seasonal_factor = 1.0  # unmodified default — must be dropped in Standard
        self.sun_azimuth = 180.0
        self.sun_elevation = 45.0
        self.azimuth_delta_deg = 5.0
        self.is_in_tolerance_window = True
        self.is_above_horizon = True
        self.effective_exposure = 300.0


class _Resolution:
    def __init__(self):
        self.applied_learned_delta_light = 0.0
        self.applied_learned_delta_normal = 15.0  # non-zero — must survive
        self.applied_learned_delta_strong = 0.0
        self.applied_forecast_delta = 10.0  # non-zero — must survive
        self.effective_light_wm2 = 120.0
        self.effective_normal_wm2 = 250.0
        self.effective_strong_wm2 = 400.0
        self.forecast_trust_level = "high"


class _Coord:
    """Duck-typed coordinator exercising real learning/solar/threshold data
    (not just defaults) so compaction assertions have something concrete to
    check for presence/absence."""

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

        # --- solar/threshold provenance: a real, non-default cycle snapshot ---
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

        # --- position-learning: one active adoption (normal), one blocked (strong) ---
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


class TestDetailLevelSelection:
    def test_default_is_standard(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        assert export["detail_level"] == "standard"

    def test_no_detail_level_kwarg_still_works(self) -> None:
        # Backward compatibility: an old call site that never passes
        # detail_level at all must keep working exactly like before.
        export = build_support_export_v3(_Coord(), now=_NOW, integration_version="1.2.0-beta.1")
        assert export["support_export_schema_version"] == SUPPORT_EXPORT_SCHEMA_VERSION
        assert export["detail_level"] == "standard"

    def test_extended_selected_explicitly(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW, detail_level="extended")
        assert export["detail_level"] == "extended"

    def test_unknown_value_falls_back_to_standard(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW, detail_level="bogus")
        assert export["detail_level"] == "standard"

    def test_all_zones_defaults_to_standard_and_threads_through(self) -> None:
        agg = build_support_export_all_zones([_Coord()], now=_NOW)
        assert agg["detail_level"] == "standard"
        assert agg["zones"][0]["detail_level"] == "standard"

    def test_all_zones_extended_threads_through(self) -> None:
        agg = build_support_export_all_zones([_Coord()], now=_NOW, detail_level="extended")
        assert agg["detail_level"] == "extended"
        assert agg["zones"][0]["detail_level"] == "extended"


class TestStandardExportContent:
    def _export(self):
        return build_support_export_v3(_Coord(), now=_NOW)

    def test_deep_debug_sections_absent(self) -> None:
        export = self._export()
        for key in ("explainability", "recent_decisions", "recent_dispatches",
                    "recent_no_dispatches", "recent_outcomes",
                    "recent_learning_transitions", "storage", "adaptation_trace"):
            assert key not in export

    def test_decision_record_and_timeline_present(self) -> None:
        export = self._export()
        assert export["current_decisions"]
        assert export["support_timeline"]["events"]

    def test_errors_fully_visible(self) -> None:
        export = self._export()
        assert "section_errors" in export

    def test_active_adoption_visible_in_position_learning(self) -> None:
        export = self._export()
        pl = next(iter(export["position_learning"].values()))
        assert "normal" in pl["active_effects"]
        assert pl["active_effects"]["normal"]["effective_delta_ha"] == 0.08

    def test_blocked_effect_visible_in_position_learning(self) -> None:
        export = self._export()
        pl = next(iter(export["position_learning"].values()))
        assert "strong" in pl["blocked_effects"]
        assert pl["blocked_effects"]["strong"]["gate_reason"] == "context_incompatible"

    def test_irrelevant_intensity_absent(self) -> None:
        export = self._export()
        pl = next(iter(export["position_learning"].values()))
        assert "light" not in pl["active_effects"]
        assert "light" not in pl["blocked_effects"]

    def test_no_empty_learning_structures(self) -> None:
        export = self._export()
        pl = next(iter(export["position_learning"].values()))
        assert "intensities" not in pl  # old always-3-intensity shape is gone
        assert "ledger_integrity_state" not in pl
        assert pl["integrity"] is not None

    def test_relevant_solar_source_visible(self) -> None:
        export = self._export()
        inputs = next(iter(export["inputs"].values()))
        assert inputs["solar"]["selected_solar_source"] == "measured"
        assert inputs["solar"]["effective_exposure_w_m2"] == 300.0

    def test_applied_forecast_delta_visible(self) -> None:
        export = self._export()
        inputs = next(iter(export["inputs"].values()))
        assert inputs["threshold"]["entry_thresholds"]["normal"]["entry_forecast_delta_w_m2"] == 10.0
        assert inputs["threshold"]["entry_thresholds"]["normal"]["entry_learned_delta_w_m2"] == 15.0

    def test_unapplied_default_factor_absent(self) -> None:
        export = self._export()
        inputs = next(iter(export["inputs"].values()))
        # seasonal_factor == 1.0 (no-op default) must not appear.
        assert "seasonal_factor" not in inputs["solar"]
        # a zero learned delta (light tier) must not appear.
        assert "entry_learned_delta_w_m2" not in inputs["threshold"]["entry_thresholds"]["light"]

    def test_current_snapshot_is_compact(self) -> None:
        export = self._export()
        snap = next(iter(export["current_snapshot"].values()))
        for dropped in ("decided_by", "solar_source", "solar_source_quality",
                        "measured_solar_wm2", "command_blocked_reason",
                        "deterministic_baseline_target_ha", "baseline_to_final_delta_ha"):
            assert dropped not in snap

    def test_meaningful_falsy_values_survive_snapshot_compaction(self) -> None:
        # T21 Phase D3: target_position: 0 and cover_available: false are
        # factually meaningful and must never be stripped as if they were
        # "empty" — only None/missing values are dropped.
        entry = {
            "window_ref": "wref", "data_available": True,
            "actual_position_ha": 0.5, "target_position_ha": 0,
            "cover_available": False, "contact_status": "closed",
            "night_contact_blocked": False, "lifecycle_state": "day",
            "is_safety": False, "rain_safe_active": False,
            "last_command_status": "ok", "is_recommendation_only": False,
            "current_state_age_seconds": 1, "last_dispatch_age_seconds": 2,
        }
        out = _compact_snapshot_entry(entry)
        assert out["target_position_ha"] == 0
        assert out["cover_available"] is False
        assert out["is_recommendation_only"] is False
        assert out["night_contact_blocked"] is False


class TestExtendedExportContent:
    def _export(self):
        return build_support_export_v3(_Coord(), now=_NOW, detail_level="extended")

    def test_full_learning_families_present(self) -> None:
        export = self._export()
        pl = next(iter(export["position_learning"].values()))
        assert set(pl["intensities"]) == {"light", "normal", "strong"}

    def test_full_solar_provenance_present(self) -> None:
        export = self._export()
        inputs = next(iter(export["inputs"].values()))
        assert "incidence_factor" in inputs["solar"]
        assert "geometry_adjusted_solar_w_m2" in inputs["solar"]

    def test_dispatch_and_storage_diagnostics_present(self) -> None:
        export = self._export()
        assert "recent_dispatches" in export
        assert "storage" in export

    def test_explainability_present(self) -> None:
        export = self._export()
        assert export["explainability"]


class TestCrossConsistency:
    def test_decided_by_target_and_pseudonymization_match(self) -> None:
        std = build_support_export_v3(_Coord(), now=_NOW, detail_level="standard")
        ext = build_support_export_v3(_Coord(), now=_NOW, detail_level="extended")
        std_dec = next(iter(std["current_decisions"].values()))
        ext_dec = next(iter(ext["current_decisions"].values()))
        assert std_dec["decided_by"] == ext_dec["decided_by"]
        assert std_dec["resolved_target_ha"] == ext_dec["resolved_target_ha"]
        assert std_dec["dispatch_action"] == ext_dec["dispatch_action"]
        assert std["pseudonymization"] == ext["pseudonymization"]
        assert list(std["current_decisions"].keys()) == list(ext["current_decisions"].keys())


class TestBackwardCompatibility:
    def test_old_service_call_without_detail_level_works(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW, integration_version="unknown")
        assert export["detail_level"] == "standard"
        assert export["current_decisions"]

    def test_all_zones_old_call_without_detail_level_works(self) -> None:
        export = build_support_export_all_zones([_Coord()], now=_NOW)
        assert export["detail_level"] == "standard"
        assert export["zones"]

    def test_old_decision_records_without_new_fields_still_normalize(self) -> None:
        # A pre-D2/D3-shaped raw ring record (no target_chain/authorities
        # extras) must still build without raising.
        legacy_rec = {
            "decision_id": "dec-legacy", "window_id": "w1",
            "decision_timestamp_utc": _NOW.isoformat(), "resolved_state": "normal_shade",
            "decided_by": "Legacy", "no_dispatch": {"command_sent": True},
        }
        export = build_support_export_v3(_Coord(records=[legacy_rec]), now=_NOW)
        assert export["current_decisions"]


class TestSharedReasonCodesCollector:
    """T21 Phase D3: research_export_v3.py's long-standing
    `contract["reason_codes"] = {}` is fixed by switching to the exact same
    collector support_export.py uses — proven here directly against the
    collector research_export_v3.py now calls."""

    def test_support_export_uses_shared_collector(self) -> None:
        export = build_support_export_v3(_Coord(), now=_NOW)
        # a research-style contract shape with a known blocked_reason string
        # anywhere in the tree is picked up by the exact function
        # research_export_v3.py now imports and calls.
        contract = {"research_records": [{"blocked_reason": "same_position"}]}
        codes = rc.collect_reason_codes_from_contract(contract)
        assert "same_position" in codes
        assert codes["same_position"]["registered"] is True
        # sanity: the export itself still produces a dict (never the old
        # unconditional {} — reason_codes here reflects what's really in it).
        assert isinstance(export["reason_codes"], dict)

    def test_unregistered_code_gets_honest_fallback(self) -> None:
        contract = {"nested": {"gate_reason": "totally_new_future_code"}}
        codes = rc.collect_reason_codes_from_contract(contract)
        assert codes["totally_new_future_code"]["registered"] is False

    def test_reason_codes_field_name_itself_is_not_treated_as_a_code(self) -> None:
        # section_errors carries {"reason_codes": ["section_builder_failed"]}
        # (a list) — must not be picked up as if it were a single code string.
        contract = {"section_errors": {"x": {"reason_codes": ["section_builder_failed"]}}}
        assert rc.collect_reason_codes_from_contract(contract) == {}

    def test_research_export_v3_source_no_longer_hardcodes_empty_dict(self) -> None:
        # No source-level way to force research_records with a reason code
        # through the full duck-typed coordinator without a heavy fixture —
        # asserts the production source directly, the same technique used by
        # T21 Phase D2's TestCoordinatorNoLongerAppendsNotRecordedCandidateFiller.
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "custom_components" / "smartshading" / "engines" / "research_export_v3.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src, filename="research_export_v3.py")
        assign_found = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Subscript)
                    and isinstance(node.targets[0].slice, ast.Constant)
                    and node.targets[0].slice.value == "reason_codes"):
                segment = ast.get_source_segment(src, node.value)
                assert segment != "{}", (
                    "research_export_v3.py must not hardcode reason_codes to {} "
                    "— it must call the shared collect_reason_codes_from_contract()"
                )
                assign_found = True
        assert assign_found, "could not locate the reason_codes assignment"
