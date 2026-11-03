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
regression guard via AST inspection of the coordinator (statement order and
call keywords, immune to formatting, comments and nested parentheses) rather
than re-deriving via a full live-coordinator simulation (this repo's existing precedent for
wiring-only proofs — see test_override_policy_wiring_contract.py).
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


@lru_cache(maxsize=None)
def _tree(relative: str) -> ast.Module:
    return ast.parse((_INTEGRATION_ROOT / relative).read_text(encoding="utf-8"))


def _coordinator_tree() -> ast.Module:
    return _tree("coordinator.py")


def _expr(text: str) -> str:
    """Canonical dump of an expression, so comparisons ignore formatting."""
    return ast.dump(ast.parse(text, mode="eval").body)


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _assignments(tree: ast.AST, target: str, call: str | None = None) -> list[ast.stmt]:
    """Assign / AnnAssign statements writing *target*, optionally from a call to *call*."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            node_target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            node_target, value = node.target, node.value
        else:
            continue
        if ast.unparse(node_target) != target:
            continue
        if call is not None and not (isinstance(value, ast.Call) and _call_name(value) == call):
            continue
        found.append(node)
    return found


def _first(tree: ast.AST, target: str, call: str | None = None) -> ast.stmt:
    nodes = _assignments(tree, target, call)
    assert nodes, f"no assignment to {target!r}" + (f" from {call}()" if call else "")
    return min(nodes, key=lambda node: node.lineno)


def _keywords(statement: ast.stmt) -> dict[str, str]:
    call = statement.value
    assert isinstance(call, ast.Call)
    return {kw.arg: ast.unparse(kw.value) for kw in call.keywords if kw.arg}


def _class_fields(tree: ast.AST, class_name: str) -> dict[str, str | None]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id: ast.unparse(stmt.value) if stmt.value is not None else None
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"class {class_name} not found")


class TestHeatActiveLatchInitialization:
    def test_heat_active_dict_initialized_in_constructor(self) -> None:
        init = next(
            node for node in ast.walk(_coordinator_tree())
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
            and _assignments(node, "self._heat_active")
        )
        statement = _first(init, "self._heat_active")
        assert isinstance(statement, ast.AnnAssign)
        assert ast.unparse(statement.annotation) == "dict[str, bool]"
        assert ast.unparse(statement.value) == "{}"

    def test_heat_hysteresis_module_is_imported(self) -> None:
        imports = [
            node for node in ast.walk(_coordinator_tree())
            if isinstance(node, ast.ImportFrom) and node.module == "engines.heat_hysteresis"
            and node.level == 1
        ]
        assert any(alias.name == "resolve_heat_needed" for node in imports for alias in node.names)


class TestHeatHysteresisResolvedAfterBehaviorModeMasking:
    def test_resolve_heat_needed_call_is_after_apply_window_behavior_mode(self) -> None:
        tree = _coordinator_tree()
        masking = _first(tree, "wdi", "_apply_window_behavior_mode")
        # The FIRST resolve_heat_needed(...) call site (the Coordinator's own
        # per-cycle state resolution — HeatEvaluator's internal call is a
        # different, later occurrence inside heat_evaluator.py, not this file).
        resolve = _first(tree, "_heat_result", "resolve_heat_needed")
        assert resolve.lineno > masking.lineno, (
            "Coordinator's heat-hysteresis resolution must run AFTER "
            "_apply_window_behavior_mode() — computing it from pre-masking "
            "thresholds would persist a stale/incorrect latch for "
            "ABSENCE_ONLY / ABSENCE_AND_SCHEDULE / DISABLED_AUTOMATIC windows."
        )

    def test_heat_previously_active_is_read_before_build_window_decision_input(self) -> None:
        tree = _coordinator_tree()
        read = _first(tree, "_heat_was_active")
        assert ast.dump(read.value) == _expr("self._heat_active.get(window_id, False)")
        build = _first(tree, "wdi", "build_window_decision_input")
        assert read.lineno < build.lineno

    def test_build_window_decision_input_receives_heat_previously_active(self) -> None:
        build = _first(_coordinator_tree(), "wdi", "build_window_decision_input")
        assert _keywords(build).get("heat_previously_active") == "_heat_was_active"

    def test_heat_active_dict_is_updated_after_resolution(self) -> None:
        tree = _coordinator_tree()
        resolve = _first(tree, "_heat_result", "resolve_heat_needed")
        update = _first(tree, "self._heat_active[window_id]")
        assert ast.unparse(update.value) == "_heat_result.active"
        tier_decision = _first(tree, "tier_decision", "evaluate_window")
        assert resolve.lineno < update.lineno < tier_decision.lineno, (
            "The latch must be updated between resolution and the tier "
            "decision call, so it reflects this cycle's own resolution."
        )


class TestHeatDiagnosticsWiring:
    def test_window_compute_state_carries_heat_fields(self) -> None:
        fields = _class_fields(_coordinator_tree(), "_WindowComputeState")
        assert fields.get("heat_hysteresis_active") == "False"
        assert fields.get("heat_hysteresis_reason") == "None"

    def test_window_compute_state_construction_passes_heat_fields(self) -> None:
        construct = _first(_coordinator_tree(), "_window_states[window_id]")
        assert _call_name(construct.value) == "_WindowComputeState"
        passed = _keywords(construct)
        for field, value in {
            "heat_hysteresis_active": "_heat_result.active",
            "heat_hysteresis_reason": "_heat_diag_reason",
            "heat_outdoor_entry_c": "_heat_outdoor_entry_c",
            "heat_outdoor_exit_c": "_heat_outdoor_exit_c",
            "heat_indoor_entry_c": "_heat_indoor_entry_c",
            "heat_indoor_exit_c": "_heat_indoor_exit_c",
        }.items():
            assert passed.get(field) == value, f"{field!r} not passed as {value!r} in _WindowComputeState(...)"

    def test_execution_diagnostics_model_has_heat_fields(self) -> None:
        tree = _tree("models/execution_diagnostics.py")
        fields: dict[str, str | None] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                fields.update(_class_fields(tree, node.name))
        assert fields.get("heat_hysteresis_active") == "False"
        assert fields.get("heat_hysteresis_reason") == "None"

    def test_overlaid_by_higher_priority_reason_is_derived_not_hardcoded(self) -> None:
        """The 'overlaid_by_higher_priority' diagnostics reason must depend on
        comparing tier_decision.decided_by against "HeatEvaluator" — not be a
        constant fired unconditionally."""
        overlaid = _first(_coordinator_tree(), "_heat_overlaid")
        assert ast.dump(overlaid.value) == _expr(
            '_heat_result.active and tier_decision.decided_by != "HeatEvaluator"'
        )
