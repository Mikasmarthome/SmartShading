"""AST-based structural proof that every override_policy-derived value is
genuinely wired from `entry_data.override_policy.<field>` into the
SmartShadingCoordinator(...) constructor call inside
_async_setup_zone_entry() — T7 pre-push review point 18 (wiring bug
injection target).

Rather than a substring `in source` check (which a subtly different but
still-"present" expression could fool), this parses __init__.py with Python's
own `ast` module, finds the SmartShadingCoordinator(...) call, and asserts
each override_* keyword argument's VALUE expression is EXACTLY
`entry_data.override_policy.<expected_attr>` (or `.value` for the enum
field) — a hard-coded literal (e.g. `False`, `120`) in that position is
exactly the wiring-bug shape this test exists to catch.

Bug-injection performed and verified during this review round: replaced
`override_allow_protection_actions=entry_data.override_policy.allow_protection_actions,`
with `override_allow_protection_actions=False,  # BUG-INJECTED` — this test
failed reliably (AssertionError naming the exact wrong-shaped keyword);
original restored, full suite re-verified green afterward.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"

# keyword -> expected attribute path on entry_data.override_policy
_EXPECTED_WIRING = {
    "override_duration_min": "duration_min",
    "override_night_duration_min": "night_duration_min",
    "override_detection_tolerance": "detection_tolerance",
    "override_safety_timeout_enabled": "safety_timeout_enabled",
    "override_release_strategy": "release_strategy",  # passed as the enum itself, no .value
    "override_fixed_until": "fixed_until",
    "override_allow_comfort_actions": "allow_comfort_actions",
    "override_allow_protection_actions": "allow_protection_actions",
}


def _find_coordinator_call() -> ast.Call:
    source = (_INTEGRATION_ROOT / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="__init__.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name == "SmartShadingCoordinator":
                return node
    raise AssertionError("SmartShadingCoordinator(...) call site not found in __init__.py")


def _expr_to_dotted_path(node: ast.AST) -> str | None:
    """Render a simple attribute-chain expression (a.b.c) back to a dotted
    string, or None if it isn't one (e.g. a literal)."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


class TestEveryOverridePolicyFieldIsWiredNotHardcoded:
    def test_all_override_policy_kwargs_reference_entry_data_override_policy(self) -> None:
        call = _find_coordinator_call()
        by_kw = {kw.arg: kw.value for kw in call.keywords if kw.arg in _EXPECTED_WIRING}
        missing = set(_EXPECTED_WIRING) - set(by_kw)
        assert not missing, f"SmartShadingCoordinator(...) call is missing kwargs: {missing}"

        violations: list[str] = []
        for kwarg_name, expected_attr in _EXPECTED_WIRING.items():
            value_node = by_kw[kwarg_name]
            dotted = _expr_to_dotted_path(value_node)
            if dotted is None:
                violations.append(
                    f"{kwarg_name}= is not an attribute-chain expression at all "
                    f"(got {ast.dump(value_node)}) — looks like a hardcoded literal"
                )
                continue
            expected_full = f"entry_data.override_policy.{expected_attr}"
            if dotted != expected_full:
                violations.append(
                    f"{kwarg_name}={dotted!r}, expected {expected_full!r}"
                )
        assert not violations, "\n".join(violations)
