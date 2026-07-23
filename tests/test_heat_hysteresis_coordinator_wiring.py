"""Structural proof that the heat-hysteresis state (v1.2.0-beta.1, T9) is
wired correctly through coordinator.py — in particular that the per-cycle
hysteresis resolution happens AFTER _apply_window_behavior_mode() masks the
heat thresholds to None for ABSENCE_ONLY / ABSENCE_AND_SCHEDULE /
DISABLED_AUTOMATIC windows, not before.

Rationale: computing/persisting the hysteresis latch from the PRE-masking
thresholds would silently desynchronize self._heat_active from what
HeatEvaluator itself actually evaluates that cycle for non-FULLY_AUTOMATIC
windows — a real bug caught and fixed during this ticket's own
implementation (not merely a hypothetical), so it is pinned here as a
regression guard via source-order inspection rather than re-deriving via a
full live-coordinator simulation (this repo's existing precedent for
wiring-only proofs — see test_override_policy_wiring_contract.py).
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


def _coordinator_source() -> str:
    return (_INTEGRATION_ROOT / "coordinator.py").read_text(encoding="utf-8")


class TestHeatActiveLatchInitialization:
    def test_heat_active_dict_initialized_in_constructor(self) -> None:
        source = _coordinator_source()
        assert "self._heat_active: dict[str, bool] = {}" in source

    def test_heat_hysteresis_module_is_imported(self) -> None:
        source = _coordinator_source()
        assert "from .engines.heat_hysteresis import resolve_heat_needed" in source


class TestHeatHysteresisResolvedAfterBehaviorModeMasking:
    def test_resolve_heat_needed_call_is_after_apply_window_behavior_mode(self) -> None:
        source = _coordinator_source()
        masking_idx = source.index("wdi = _apply_window_behavior_mode(wdi, window.behavior_mode)")
        # The FIRST resolve_heat_needed(...) call site (the Coordinator's own
        # per-cycle state resolution — HeatEvaluator's internal call is a
        # different, later occurrence inside heat_evaluator.py, not this file).
        resolve_idx = source.index("_heat_result = resolve_heat_needed(")
        assert resolve_idx > masking_idx, (
            "Coordinator's heat-hysteresis resolution must run AFTER "
            "_apply_window_behavior_mode() — computing it from pre-masking "
            "thresholds would persist a stale/incorrect latch for "
            "ABSENCE_ONLY / ABSENCE_AND_SCHEDULE / DISABLED_AUTOMATIC windows."
        )

    def test_heat_previously_active_is_read_before_build_window_decision_input(self) -> None:
        source = _coordinator_source()
        read_idx = source.index("_heat_was_active = self._heat_active.get(window_id, False)")
        build_idx = source.index("wdi = build_window_decision_input(")
        assert read_idx < build_idx

    def test_build_window_decision_input_receives_heat_previously_active(self) -> None:
        source = _coordinator_source()
        build_start = source.index("wdi = build_window_decision_input(")
        build_end = source.index(")", build_start)
        call_text = source[build_start:build_end]
        assert "heat_previously_active=_heat_was_active" in call_text

    def test_heat_active_dict_is_updated_after_resolution(self) -> None:
        source = _coordinator_source()
        resolve_idx = source.index("_heat_result = resolve_heat_needed(")
        update_idx = source.index("self._heat_active[window_id] = _heat_result.active")
        tier_decision_idx = source.index("tier_decision = self._tier_orchestrator.evaluate_window(wdi)")
        assert resolve_idx < update_idx < tier_decision_idx, (
            "The latch must be updated between resolution and the tier "
            "decision call, so it reflects this cycle's own resolution."
        )


class TestHeatDiagnosticsWiring:
    def test_window_compute_state_carries_heat_fields(self) -> None:
        source = _coordinator_source()
        assert "heat_hysteresis_active: bool = False" in source
        assert "heat_hysteresis_reason: str | None = None" in source

    def test_window_compute_state_construction_passes_heat_fields(self) -> None:
        source = _coordinator_source()
        construct_start = source.index("_window_states[window_id] = _WindowComputeState(")
        construct_end = source.index("\n            )", construct_start)
        call_text = source[construct_start:construct_end]
        for field in (
            "heat_hysteresis_active=_heat_result.active",
            "heat_hysteresis_reason=_heat_diag_reason",
            "heat_outdoor_entry_c=_heat_outdoor_entry_c",
            "heat_outdoor_exit_c=_heat_outdoor_exit_c",
            "heat_indoor_entry_c=_heat_indoor_entry_c",
            "heat_indoor_exit_c=_heat_indoor_exit_c",
        ):
            assert field in call_text, f"missing {field!r} in _WindowComputeState(...) call"

    def test_execution_diagnostics_model_has_heat_fields(self) -> None:
        source = (_INTEGRATION_ROOT / "models" / "execution_diagnostics.py").read_text(encoding="utf-8")
        assert "heat_hysteresis_active: bool = False" in source
        assert "heat_hysteresis_reason: str | None = None" in source

    def test_overlaid_by_higher_priority_reason_is_derived_not_hardcoded(self) -> None:
        """The 'overlaid_by_higher_priority' diagnostics reason must depend on
        comparing tier_decision.decided_by against "HeatEvaluator" — not be a
        constant fired unconditionally."""
        source = _coordinator_source()
        assert '_heat_overlaid = _heat_result.active and tier_decision.decided_by != "HeatEvaluator"' in source
