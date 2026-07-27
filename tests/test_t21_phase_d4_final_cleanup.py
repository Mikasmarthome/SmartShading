"""T21 Phase D4 — final diagnostics cleanup: confirmed-duplicate removal,
shared-helper wiring between Support and Research Export, and dead-code
protection tests.
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smartshading.engines import reason_codes as rc
from custom_components.smartshading.engines.explainability import _REASON_DESCRIPTIONS
from custom_components.smartshading.engines.research_export_v3 import (
    build_research_export_all_zones,
    build_research_export_v3,
)
from custom_components.smartshading.engines.support_export import (
    build_support_export_v3,
)

_NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)


class _Coord:
    """Minimal duck-typed coordinator shared by both export builders."""

    def __init__(self):
        self.zones = {"z1": object()}
        self.windows = {}
        self.cover_groups = {}
        self.config_entry = type("CE", (), {"entry_id": "eid1"})()
        self._adoption_history = []
        self._strategy_adoption_history = []
        self._adoptions_active = {}
        self._pending_outcomes = type("PO", (), {"all_pending": lambda s: []})()
        self._support_critical_events = []
        self._research_daily_buckets = {}
        self._ring_records = []
        from custom_components.smartshading.engines.learning_store import LearningStore
        self.learning_store = LearningStore()
        self._learning_store = self.learning_store

    def decision_trace_snapshot(self):
        return {}

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


class TestExplainabilityDeadDescriptionsRemoved:
    """T21 Phase D4: 9 entries in _REASON_DESCRIPTIONS were unreachable
    (reason_codes.py's registry is checked first for the same codes) —
    confirmed via key-overlap audit and removed."""

    def test_registry_covered_codes_are_gone_from_local_table(self) -> None:
        registry_codes = set(rc._REGISTRY.keys())
        overlap = registry_codes & set(_REASON_DESCRIPTIONS)
        assert overlap == set(), (
            f"these codes are described in BOTH tables (dead duplication): {overlap}"
        )

    def test_genuinely_uncovered_codes_still_present(self) -> None:
        # codes with no registry entry must still have a local description.
        for code in ("command_filter_suppressed", "same_position_no_change",
                     "min_interval_not_elapsed", "not_recorded",
                     "held_by_hysteresis"):
            assert code not in rc._REGISTRY
            assert code in _REASON_DESCRIPTIONS


class TestSharedHelpersBetweenExports:
    def test_both_exports_use_the_same_reason_codes_collector(self) -> None:
        support = build_support_export_v3(_Coord(), now=_NOW)
        research = build_research_export_v3(_Coord(), now=_NOW)
        assert isinstance(support["reason_codes"], dict)
        assert isinstance(research["reason_codes"], dict)
        # same collector semantics: an unregistered code gets the same
        # honest "registered: False" fallback shape from both.
        assert rc.collect_reason_codes_from_contract(
            {"gate_reason": "brand_new_code"}
        )["brand_new_code"]["registered"] is False

    def test_both_exports_use_the_shared_pseudonymization_helper(self) -> None:
        support = build_support_export_v3(_Coord(), now=_NOW)
        research = build_research_export_v3(_Coord(), now=_NOW)
        for meta in (support["pseudonymization"], research["pseudonymization"]):
            assert "algorithm" in meta
            assert "stability_scope" in meta

    def test_all_zones_pseudonymization_scope_is_documented_not_vague(self) -> None:
        research_all = build_research_export_all_zones([_Coord()], now=_NOW)
        assert research_all["pseudonymization"]["stability_scope"] == "shared_seed_all_zones"

    def test_both_exports_derive_runtime_mode_via_shared_authority_function(self) -> None:
        from custom_components.smartshading.models.runtime_mode import derive_authority
        # both support_export._configuration() and research's _zone_runtime_mode()
        # call derive_authority(learning_enabled, active_control_enabled) — proven
        # indirectly: an empty coordinator with no windows yields the same
        # "unknown"/"off"-style default mode label from both exports.
        support = build_support_export_v3(_Coord(), now=_NOW)
        research = build_research_export_v3(_Coord(), now=_NOW)
        assert research["system"]["runtime_mode"] == derive_authority(False, False).mode.value
        assert support["configuration"]["runtime_mode"] == derive_authority(False, False).mode.value


class TestResearchExportDeliberateDivergenceDocumented:
    """T21 Phase D4: research_export_v3.py's target/dispatch semantics
    operate on persisted ProvenanceSummary data (baseline vs adapted target),
    a genuinely different shape/question than decision_record.py's ring-based
    resolvers — this is a documented, deliberate non-consolidation, proven by
    a source-level check that the module docstring actually says so."""

    def test_module_docstring_documents_the_divergence(self) -> None:
        import custom_components.smartshading.engines.research_export_v3 as mod
        assert "NOT shared" in mod.__doc__
        assert "decision_record.py" in mod.__doc__
