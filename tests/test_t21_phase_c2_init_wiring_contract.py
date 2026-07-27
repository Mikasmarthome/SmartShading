"""AST-based structural proof of two T21 Phase C2 __init__.py wiring
invariants — no HomeAssistant import needed (same technique as
test_override_policy_wiring_contract.py), so this runs everywhere including
under the plain system Python that can't import pytest-homeassistant-custom-
component.

Bug-injection performed and verified during this review round: changed
_system_entry_data()'s loop to `return e.data` unconditionally (dropping the
`if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM` guard) — a zone entry
processed before the System entry would then be misread as the System entry
itself. test_system_entry_data_filters_by_entry_type() failed reliably;
original restored, full suite re-verified green afterward.
"""
from __future__ import annotations

import ast
from pathlib import Path

_INTEGRATION_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


def _source() -> str:
    return (_INTEGRATION_ROOT / "__init__.py").read_text(encoding="utf-8")


def test_system_entry_data_filters_by_entry_type() -> None:
    """_system_entry_data() must actually check CONF_ENTRY_TYPE ==
    ENTRY_TYPE_SYSTEM before returning an entry's data — otherwise the
    System entry lookup would treat any (e.g. a zone) entry as the system
    entry, and a zone's own settings could leak in as "global defaults"."""
    src = _source()
    tree = ast.parse(src, filename="__init__.py")
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_system_entry_data"),
        None,
    )
    assert func is not None, "_system_entry_data() not found in __init__.py"
    has_type_check = any(
        isinstance(node, ast.Compare)
        and (segment := ast.get_source_segment(src, node)) is not None
        and "CONF_ENTRY_TYPE" in segment
        and "ENTRY_TYPE_SYSTEM" in segment
        for node in ast.walk(func)
    )
    assert has_type_check, (
        "_system_entry_data() has no CONF_ENTRY_TYPE == ENTRY_TYPE_SYSTEM check — "
        "would treat any entry, including a zone, as the System entry"
    )


def test_zone_setup_wires_resolved_policy_not_raw_entry_data() -> None:
    """_async_setup_zone_entry() must pass the RESOLVED values
    (_effective_override_policy / _effective_dispatch_config, computed via
    resolve_zone_override_policy()/resolve_zone_dispatch_config() using both
    the zone's own data AND the System entry's) into SmartShadingCoordinator
    — not entry_data.override_policy/.dispatch_config directly, which would
    silently ignore the System entry's global defaults entirely."""
    src = _source()
    assert "_effective_override_policy = resolve_zone_override_policy(" in src
    assert "_effective_dispatch_config = resolve_zone_dispatch_config(" in src
    assert "dispatch_config=_effective_dispatch_config," in src
    assert "dispatch_config=entry_data.dispatch_config," not in src
