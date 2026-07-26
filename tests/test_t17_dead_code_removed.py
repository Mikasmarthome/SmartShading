"""T17 — final beta dead-code cleanup lock-in.

Protects the findings closed in T17 (a re-sweep of everything T15 deferred,
plus a fresh full-tree sweep). Two things are guarded:

  A-findings (confirmed dead, removed): must not silently come back.
  C/D-findings (backward-compat / deliberately reserved): must still exist —
  these were EXPLICITLY kept, so a future cleanup pass must not mistake them
  for dead code and remove them without re-reading their rationale.

Also covers cover_control/dispatch_orchestrator.py's resolve_pre_dispatch_wait
removal (zero callers — the coordinator's real dispatch loop computes the
remainder-against-elapsed-time subtraction inline instead) and the two dead
timeline event-type strings (contact_event, min_interval_bypass) that were
never produced by any decision path.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


def _source(relpath: str) -> str:
    return (_BASE / relpath).read_text(encoding="utf-8")


def _top_level_names(relpath: str) -> set[str]:
    tree = ast.parse(_source(relpath))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


# ---------------------------------------------------------------------------
# A-findings: confirmed-dead symbols must not silently come back
# ---------------------------------------------------------------------------

class TestDeadSymbolsRemainRemoved:
    @pytest.mark.parametrize("relpath,names", [
        ("engines/reason_codes.py", {
            "get", "all_codes", "CAT_DECISION", "CAT_AUTHORITY", "CAT_EXPERIMENT",
            "CAT_ROLLBACK", "CAT_DISPATCH", "CAT_OVERRIDE", "SEV_ERROR", "VIS_RESEARCH",
        }),
        ("models/bounded_experiment.py", {"EXPERIMENT_DELTA_HA", "EXPERIMENT_AGE_CAP_DAYS"}),
        ("models/forecast_learning.py", {"get_bucket_result", "get_variable_result"}),
        ("models/multi_objective_outcome.py", {"ATTRIBUTION_WINDOW_CANDIDATE", "RECON_EXACT"}),
        ("models/persistent_adoption.py", {"ADOPTION_AGE_CAP_DAYS"}),
        ("models/shading_strategy.py", {"STRATEGY_STATES"}),
        ("models/strategy_learning.py", {
            "ALL_FAMILIES", "THRESHOLD_FAMILIES", "AGE_CAP_DAYS",
            "EXPERIMENT_HISTORY_PER_KEY", "EVAL_UNAVAILABLE", "EVAL_CONFOUNDED", "TIER_ORDER",
        }),
        ("models/window_contribution.py", {"_clamp01"}),
        ("models/state.py", {"ShadeState", "StateLock", "LockReason"}),
        ("entities/button.py", {"_collect_zone_entries", "_resolve_zone_coordinator"}),
        ("evaluators/glare_evaluator.py", {"is_low_angle_direct_sun"}),
        ("state_machine/states.py", {"is_higher_priority"}),
        ("engines/diagnostics_privacy.py", {
            "NS_SENSOR", "NS_OUTCOME", "NS_HARMONIZATION", "NS_FORECAST_SOURCE", "iso_utc",
        }),
        ("engines/feature_normalizer.py", {"normalize_situations"}),
        ("engines/learning_trace_builder.py", {"S_INVALID", "S_STALE", "S_BLOCKED"}),
        ("engines/reference_validator.py", {"validate_experiments", "R_MISSING_DECISION_LINK"}),
        ("engines/restore_validation.py", {"R_INVALID_CONFIG_GENERATION"}),
        ("engines/similarity_calculator.py", {"calculate_distance"}),
        ("engines/storage_validation.py", {
            "safe_number", "normalise_timestamp", "is_valid_count",
            "delta_within_bounds", "dedupe_by_id",
        }),
        ("cover_control/dispatch_orchestrator.py", {"resolve_pre_dispatch_wait"}),
        ("coordinator.py", {
            "thermal_diagnostics", "experiment_diagnostics", "strategy_diagnostics",
            "solar_threshold_diagnostics", "tier_order_diagnostics",
            "window_contribution_diagnostics", "shadow_diagnostics",
            "_zone_experiment_locked",
        }),
    ])
    def test_dead_symbols_absent(self, relpath: str, names: set[str]) -> None:
        present = _top_level_names(relpath) & names
        assert not present, (
            f"{relpath}: T17-confirmed-dead symbol(s) {present} reappeared. "
            "These had zero callers anywhere and no reserved/compat comment "
            "when removed — re-introducing them silently reverses that audit."
        )

    def test_learning_export_module_does_not_exist(self) -> None:
        assert not (_BASE / "engines" / "learning_export.py").exists(), (
            "engines/learning_export.py was confirmed dead (superseded by "
            "support_export.py, zero importers anywhere) and removed in T17 — "
            "it must not be silently reintroduced."
        )

    def test_no_production_file_imports_learning_export(self) -> None:
        for path in _BASE.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            assert "learning_export" not in text or "zone_learning_export" in text, (
                f"{path.relative_to(_BASE)} references removed module 'learning_export'"
            )


class TestDeadTimelineEventTypesRemoved:
    """contact_event and min_interval_bypass were defined in two duplicated
    _CRITICAL_EVENT_TYPES/_CRITICAL_SE frozensets but never produced by any
    decision path (the full enumerated set of real event types was verified
    against every evt_type assignment site)."""

    @pytest.mark.parametrize("relpath", ["engines/support_export.py", "coordinator.py"])
    def test_dead_event_types_not_in_critical_set(self, relpath: str) -> None:
        source = _source(relpath)
        assert '"contact_event"' not in source
        assert '"min_interval_bypass"' not in source


# ---------------------------------------------------------------------------
# C/D-findings: explicitly kept — must NOT be mistaken for dead code later
# ---------------------------------------------------------------------------

class TestCompatAndReservedSymbolsStillPresent:
    """These look similar to the dead findings above (same file neighborhoods,
    same "zero production callers" pattern for the reserved ones) but were
    deliberately kept — a backward-compat requirement or an explicitly
    documented future-feature reservation. This test protects them from
    being swept up by a future, less careful cleanup pass."""

    def test_gate_position_compat_constant_kept(self) -> None:
        source = _source("engines/adaptation_application.py")
        assert "_GATE_POSITION" in source
        assert "backward compat" in source

    def test_legacy_v1_reconstruction_helper_kept(self) -> None:
        source = _source("engines/outcome_resolution.py")
        assert "def reconstruct_multi_objective_from_legacy" in source

    def test_decision_provenance_reserved_sources_kept(self) -> None:
        source = _source("models/decision_provenance.py")
        assert "SOURCE_PASSIVE_ADAPTATION" in source
        assert "SOURCE_SHADOW" in source

    def test_comfort_profile_reserved_dataclasses_kept(self) -> None:
        source = _source("models/comfort.py")
        assert "class ComfortGoal" in source
        assert "class ComfortProfile" in source

    def test_reason_code_reserved_members_kept(self) -> None:
        """models/state.py's ReasonCode enum itself (distinct from the three
        removed classes ShadeState/StateLock/LockReason) is reserved for
        future phases per its own docstring — must not be touched."""
        source = _source("models/state.py")
        assert "class ReasonCode" in source
        for member in ("MANUAL_OVERRIDE_CLEARED", "STORM_CLEARED", "PRESENCE_DETECTED"):
            assert member in source


# ---------------------------------------------------------------------------
# _maybe_adopt_strategy: documented architecture reserve, not removed
# ---------------------------------------------------------------------------

class TestStrategyAdoptionPromotionDocumentedNotRemoved:
    """T17 audited this path in depth: it's fully implemented but has zero
    callers because the entire P9B strategy-experiment INJECTION engine
    (mirroring _experiment_try_inject for P7) was never built — not a small
    wiring gap. Kept as a named, documented architecture reserve rather than
    removed or silently left stale."""

    def test_maybe_adopt_strategy_kept_with_honest_docstring(self) -> None:
        source = _source("coordinator.py")
        assert "def _maybe_adopt_strategy" in source
        idx = source.index("def _maybe_adopt_strategy")
        window = source[idx:idx + 2200]
        assert "T17" in window
        assert "architecture reserve" in window
