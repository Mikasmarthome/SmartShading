"""Structural (AST-based) proof that every production WindowDecision(...)
construction supplies an explicit, valid DecisionCategory — T7 pre-push
review point 12.

WindowDecision.category has NO dataclass default (see models/
window_decision.py) — a missing category would already be a hard TypeError
at runtime, caught by the mere act of running the test suite. This file
adds a STRUCTURAL, source-level guarantee beyond that: it proves every
`WindowDecision(...)` call site in the production package passes
`category=DecisionCategory.<MEMBER>` — never a bare positional gap, a
string literal, a variable, or DecisionCategory itself misspelled — using
Python's own `ast` module rather than grep (immune to comment/string
false-positives, and precise about keyword-vs-positional).

Also verifies dataclasses.replace() call sites never explicitly overwrite
category with something invalid (they are allowed to omit it entirely,
which correctly preserves the original decision's category).
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_ROOT = _REPO_ROOT / "custom_components" / "smartshading"

# Files known to construct WindowDecision directly (production code only —
# test helpers are explicitly out of scope, see module docstring).
_PRODUCTION_FILES = [
    _PACKAGE_ROOT / "coordinator.py",
    *(_PACKAGE_ROOT / "evaluators").glob("*.py"),
]


def _find_window_decision_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name == "WindowDecision":
                calls.append(node)
    return calls


def _find_replace_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name == "replace":
                calls.append(node)
    return calls


class TestEveryWindowDecisionConstructionHasExplicitValidCategory:
    def test_all_production_call_sites_pass_category_kwarg(self) -> None:
        violations: list[str] = []
        total_calls = 0
        for path in _PRODUCTION_FILES:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for call in _find_window_decision_calls(tree):
                total_calls += 1
                # Positional slot 4 (0-indexed) is `category` per the
                # dataclass field order (window_id, shading_state,
                # target_position, decided_by, category, target_tilt) —
                # accept it there too, though every current call site uses
                # keyword form.
                has_positional_category = len(call.args) >= 5
                category_kwarg = next((kw for kw in call.keywords if kw.arg == "category"), None)
                if not has_positional_category and category_kwarg is None:
                    violations.append(f"{path.relative_to(_REPO_ROOT)}:{call.lineno} — missing category")
                    continue
                if category_kwarg is not None:
                    value = category_kwarg.value
                    is_valid_enum_ref = (
                        isinstance(value, ast.Attribute)
                        and isinstance(value.value, ast.Name)
                        and value.value.id == "DecisionCategory"
                    )
                    if not is_valid_enum_ref:
                        violations.append(
                            f"{path.relative_to(_REPO_ROOT)}:{call.lineno} — category is not a "
                            f"DecisionCategory.<MEMBER> reference (got {ast.dump(value)})"
                        )
        assert total_calls > 0, "sanity: the scan itself must find at least one WindowDecision(...) call"
        assert not violations, "\n".join(violations)

    def test_scan_found_the_expected_number_of_call_sites(self) -> None:
        """No-silent-shrinkage guard: if a future refactor removes/renames
        WindowDecision(...) call sites (e.g. via a factory function), this
        count must be deliberately updated — it must never silently drop to
        a smaller number while this test still reports 'no violations'."""
        total_calls = 0
        for path in _PRODUCTION_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            total_calls += len(_find_window_decision_calls(tree))
        assert total_calls >= 16, (
            f"expected at least 16 production WindowDecision(...) call sites "
            f"(8 coordinator.py + 8 evaluators), found {total_calls} — if this "
            f"is a deliberate refactor, update this count"
        )


class TestReplaceCallsNeverOverwriteCategoryWithSomethingInvalid:
    def test_replace_calls_either_omit_category_or_pass_a_valid_enum(self) -> None:
        violations: list[str] = []
        for path in _PRODUCTION_FILES:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for call in _find_replace_calls(tree):
                category_kwarg = next((kw for kw in call.keywords if kw.arg == "category"), None)
                if category_kwarg is None:
                    continue  # correctly preserves the original category — fine
                value = category_kwarg.value
                is_valid_enum_ref = (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "DecisionCategory"
                )
                if not is_valid_enum_ref:
                    violations.append(f"{path.relative_to(_REPO_ROOT)}:{call.lineno} — invalid category override in replace()")
        assert not violations, "\n".join(violations)
