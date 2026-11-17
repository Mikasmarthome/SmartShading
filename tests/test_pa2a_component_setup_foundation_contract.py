"""PA-2a structural contract (AST, no Home Assistant import): the component-level setup
foundation exists and its own code (setup, enumeration, guard) writes nothing; the settings
migration (PA-2b) is a separate, separately tested step.

Behavior is proven in ``tests/real_ha/test_real_ha_component_setup_foundation.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
_SRC = (_ROOT / "__init__.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC, filename="__init__.py")

_FORBIDDEN_CALLS = {
    "async_update_entry", "async_remove", "async_unload", "async_setup", "async_create_task",
    "async_schedule_reload", "async_reload", "async_init", "async_forward_entry_setups",
    "async_create_issue", "async_listen", "async_listen_once", "async_register",
}


def _func(name: str) -> ast.AST:
    node = next((n for n in ast.walk(_TREE)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    assert node is not None, f"{name} missing in __init__.py"
    return node


def _called_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def test_component_setup_exists_exactly_once_and_is_async() -> None:
    defs = [n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup"]
    assert len(defs) == 1


def test_component_setup_is_config_entry_only() -> None:
    assert "CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)" in _SRC


def test_preparation_code_performs_no_writes_or_lifecycle_calls() -> None:
    for name in ("async_setup", "_prepare_zone_entries", "_prepare_zone_entry"):
        bad = _called_names(_func(name)) & _FORBIDDEN_CALLS
        assert not bad, f"{name} must stay write-free, found {bad}"


def test_preparation_enumerates_through_the_shared_zone_helper() -> None:
    assert "_zone_entries" in _called_names(_func("_prepare_zone_entries"))


def test_only_the_dedicated_preparation_error_is_contained() -> None:
    handlers = [h for h in ast.walk(_func("_prepare_zone_entries")) if isinstance(h, ast.ExceptHandler)]
    assert len(handlers) == 1
    assert ast.unparse(handlers[0].type) == "SmartShadingPreparationError"
    assert not [h for h in ast.walk(_func("async_setup")) if isinstance(h, ast.ExceptHandler)]


def test_the_major_version_is_unchanged() -> None:
    flow = (_ROOT / "config_flow.py").read_text(encoding="utf-8")
    assert "    VERSION = 1\n" in flow
    assert "    MINOR_VERSION = 2\n" in flow  # PA-2b: explicit zone settings


def test_component_setup_does_not_touch_the_system_entry_or_shared_runtime() -> None:
    names = {n.id for n in ast.walk(_func("async_setup")) if isinstance(n, ast.Name)}
    names |= {n.id for n in ast.walk(_func("_prepare_zone_entries")) if isinstance(n, ast.Name)}
    for forbidden in ("ENTRY_TYPE_SYSTEM", "_system_entry_data", "_ensure_system_entry",
                      "GlobalSerialDispatch", "DATA_GLOBAL_DISPATCH", "async_sync_cover_ownership",
                      "async_setup_services"):
        assert forbidden not in names
