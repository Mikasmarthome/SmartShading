"""Structural guard: coordinator.py's shadow-candidate confidence computation
must use the shared ConfidenceEngine evidence-maturity ramp (T12), not a
re-diverged ad-hoc formula.

Why structural: exercising the real ``_maybe_shadow`` path behaviorally
requires a fully wired zone with contribution models, attribution gates and
multiple resolved thermal outcomes across several days — deeper fixture
machinery than this codebase's established test depth for the P6 shadow
engine (see test_coordinator_parallel_dispatch.py's own structural/behavioral
split precedent). A structural check on the call site is the established
alternative for this kind of hard-to-behaviorally-test invariant (see
test_dispatch_safety_generation_exemption_structural.py).
"""
from __future__ import annotations

import ast
from pathlib import Path

_COORDINATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "smartshading" / "coordinator.py"
)


def _tree() -> ast.Module:
    return ast.parse(_COORDINATOR_PATH.read_text(encoding="utf-8"))


def _maybe_shadow() -> ast.FunctionDef:
    functions = [
        node for node in ast.walk(_tree())
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_maybe_shadow"
    ]
    assert len(functions) == 1
    return functions[0]


def _maturity_calls(root: ast.AST) -> list[ast.Call]:
    return [
        node for node in ast.walk(root)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", "")) == "compute_sample_maturity_confidence"
    ]


class TestShadowConfidenceUsesSharedFormula:
    def test_maybe_shadow_calls_compute_sample_maturity_confidence(self) -> None:
        assert _maturity_calls(_maybe_shadow()), (
            "_maybe_shadow must call the shared evidence-maturity ramp from "
            "confidence_engine — a hand-rolled ad-hoc formula here "
            "would re-introduce a duplicate, divergeable confidence concept."
        )

    def test_call_uses_documented_targets(self) -> None:
        calls = _maturity_calls(_maybe_shadow())
        assert len(calls) == 1
        call = calls[0]
        assert [ast.unparse(arg) for arg in call.args] == ["neg_count"]
        keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert keywords == {
            "count_target": "8.0",
            "distinct_days": "days",
            "day_target": "3.0",
        }, (
            "The shadow-candidate maturity call must use count_target=8.0 / "
            "day_target=3.0 (the historical constants) via the shared "
            "helper — a changed target would silently alter shadow-proposal "
            "maturity behavior without any test noticing outside this guard."
        )

    def test_no_hand_rolled_min_ratio_formula_remains_in_shadow_block(self) -> None:
        for node in ast.walk(_maybe_shadow()):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                assert not (
                    isinstance(node.left, ast.Name) and node.left.id == "neg_count"
                    and isinstance(node.right, ast.Constant) and node.right.value == 8.0
                ), (
                    "A literal 'neg_count / 8.0' inside _maybe_shadow indicates the "
                    "ad-hoc formula was reintroduced instead of the shared helper."
                )
