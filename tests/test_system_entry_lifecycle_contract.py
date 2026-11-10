"""Source contract of the System-entry lifecycle (GitHub issue 5).

The behaviour is covered against real Home Assistant in
tests/real_ha/test_real_ha_system_entry_lifecycle.py; this file only pins the
shape that keeps it safe where Home Assistant is not available: the supported
removal API only, no direct storage or file deletion, no recursion.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
        / "__init__.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _function(name: str):
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _segment(name: str) -> str:
    return ast.get_source_segment(_SRC, _function(name))


class TestSystemEntryLifecycleContract:
    def test_the_orphan_cleanup_uses_only_the_supported_removal_api(self):
        body = _segment("_async_remove_orphaned_system_entry")
        assert "hass.config_entries.async_remove(" in body
        for forbidden in (".storage", "unlink", "os.remove", "rmtree", "async_remove_device",
                          "async_remove_entity", "flow.async_init"):
            assert forbidden not in body, forbidden

    def test_the_system_entry_removal_hook_returns_before_touching_anything(self):
        body = _function("async_remove_entry")
        guard = next(n for n in body.body if isinstance(n, ast.If))
        assert "ENTRY_TYPE_SYSTEM" in ast.get_source_segment(_SRC, guard.test)
        assert isinstance(guard.body[0], ast.Return)

    def test_the_cleanup_is_triggered_only_from_the_zone_removal_and_the_system_setup(self):
        callers = [
            node.name for node in ast.walk(_TREE)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != "_async_remove_orphaned_system_entry"
            and "_async_remove_orphaned_system_entry" in ast.get_source_segment(_SRC, node)
        ]
        assert sorted(callers) == ["_async_setup_system_entry", "async_remove_entry"]

    def test_entry_creation_stays_in_the_zone_setup_path(self):
        callers = [
            node.name for node in ast.walk(_TREE)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != "_ensure_system_entry"
            and "_ensure_system_entry(" in ast.get_source_segment(_SRC, node)
        ]
        # the zone setup, and the one-time heal after a removal raced with a new zone
        assert sorted(callers) == ["_async_remove_orphaned_system_entry", "_async_setup_zone_entry"]

    def test_zones_are_counted_without_ignored_entries(self):
        body = _segment("_zone_entries")
        assert "SOURCE_IGNORE" in body and "ENTRY_TYPE_ZONE" in body
