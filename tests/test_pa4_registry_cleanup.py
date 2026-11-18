"""PA-4 (stub suite): legacy System registry clean-up and the start-up restore guard.

Behavior against the real registries lives in tests/real_ha/test_real_ha_pa4_registry_integrity.py;
this module pins the matching rules with fake registries and the source-level contracts.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


def _src(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _func(source_file: str, name: str) -> ast.AST:
    tree = ast.parse(_src(source_file))
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _row(entity_id, domain, unique_id, config_entry_id, platform="smartshading"):
    return SimpleNamespace(entity_id=entity_id, domain=domain, unique_id=unique_id,
                           config_entry_id=config_entry_id, platform=platform)


class _FakeEntities(dict):
    def values(self):
        return super().values()


class _World:
    """Fake hass + registries; records what is removed."""

    def __init__(self, entries, rows, device=True, device_entities=()):
        self.entries = entries
        self.rows = _FakeEntities({r.entity_id: r for r in rows})
        self.removed_entities: list[str] = []
        self.removed_devices: list[str] = []
        self.device = SimpleNamespace(id="dev1") if device else None
        self.device_entities = list(device_entities)
        world = self
        self.hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_get_entry=lambda entry_id: world.entries.get(entry_id)))

    def entity_registry(self):
        world = self

        class Reg:
            entities = world.rows

            def async_remove(self, entity_id):
                world.removed_entities.append(entity_id)
                world.rows.pop(entity_id, None)
        return Reg()

    def device_registry(self):
        world = self

        class Reg:
            def async_get_device(self, identifiers):
                world.seen_identifiers = identifiers
                return world.device

            def async_remove_device(self, device_id):
                world.removed_devices.append(device_id)
        return Reg()


def _entry(kind):
    return SimpleNamespace(data={"entry_type": kind} if kind else {})


@pytest.fixture
def module(monkeypatch):
    from custom_components.smartshading import legacy_registry_cleanup as mod
    return mod


def _run(mod, monkeypatch, world: _World):
    monkeypatch.setattr(mod.er, "async_get", lambda hass: world.entity_registry(), raising=False)
    monkeypatch.setattr(mod.dr, "async_get", lambda hass: world.device_registry(), raising=False)
    monkeypatch.setattr(
        mod.er, "async_entries_for_device",
        lambda reg, device_id, include_disabled_entities=False: world.device_entities, raising=False)
    return mod.async_remove_legacy_system_registry_rows(world.hass)


SYSTEM = _entry("system")
ZONE = _entry("zone")
LEGACY_ROWS = [
    _row("button.a", "button", "sys_system_export_support_data", "sys"),
    _row("button.b", "button", "sys_system_export_research_data", "sys"),
    _row("switch.c", "switch", "sys_debug_logging", "sys"),
]


class TestMatching:
    def test_the_three_legacy_rows_of_a_system_entry_are_removed(self, module, monkeypatch) -> None:
        world = _World({"sys": SYSTEM}, list(LEGACY_ROWS))
        assert _run(module, monkeypatch, world) == (3, 1)
        assert sorted(world.removed_entities) == ["button.a", "button.b", "switch.c"]
        assert world.removed_devices == ["dev1"]

    def test_rows_of_an_entry_that_no_longer_exists_are_removed(self, module, monkeypatch) -> None:
        world = _World({}, list(LEGACY_ROWS))
        assert _run(module, monkeypatch, world)[0] == 3

    @pytest.mark.parametrize("row", [
        _row("switch.x", "switch", "sys_debug_logging", "sys", platform="other"),        # foreign platform
        _row("switch.x", "switch", "sys_debug_logging", "zone1"),                         # a zone entry's row
        _row("switch.x", "switch", "sys_debug_logging_other", "sys"),                     # other unique id
        _row("sensor.x", "sensor", "sys_debug_logging", "sys"),                           # other entity domain
        _row("button.x", "button", "sys_debug_logging", "sys"),                           # domain/suffix mismatch
        _row("switch.x", "switch", "sys_debug_logging", None),                            # no config entry
        _row("switch.x", "switch", "other_debug_logging", "sys"),                         # not the entry's own id
        _row("switch.x", "switch", "zone1_zone_a_learning", "zone1"),                     # a normal zone switch
    ])
    def test_similar_rows_are_never_removed(self, module, monkeypatch, row) -> None:
        world = _World({"sys": SYSTEM, "zone1": ZONE}, [row], device=False)
        assert _run(module, monkeypatch, world) == (0, 0)
        assert world.removed_entities == [] and world.removed_devices == []

    def test_an_entry_without_a_type_is_a_zone_and_protected(self, module, monkeypatch) -> None:
        legacy_zone = _entry(None)           # created before the System separation: a zone
        world = _World({"sys": legacy_zone}, list(LEGACY_ROWS), device=False)
        assert _run(module, monkeypatch, world) == (0, 0)

    def test_the_device_is_kept_while_an_entity_references_it(self, module, monkeypatch) -> None:
        world = _World({"sys": SYSTEM}, list(LEGACY_ROWS), device_entities=[object()])
        assert _run(module, monkeypatch, world) == (3, 0)
        assert world.removed_devices == []

    def test_the_device_is_found_by_the_legacy_identifier_only(self, module, monkeypatch) -> None:
        world = _World({"sys": SYSTEM}, [])
        _run(module, monkeypatch, world)
        assert world.seen_identifiers == {("smartshading", "smartshading_system")}

    def test_a_second_pass_finds_nothing(self, module, monkeypatch) -> None:
        world = _World({"sys": SYSTEM}, list(LEGACY_ROWS), device=False)
        assert _run(module, monkeypatch, world) == (3, 0)
        assert _run(module, monkeypatch, world) == (0, 0)


class TestSourceContracts:
    def test_only_supported_registry_apis_are_used(self) -> None:
        text = _src("legacy_registry_cleanup.py")
        for forbidden in (".storage", "hass_storage", "json.", "open(", "Store(", "_registry.entities.pop",
                          "async_clear_config_entry", "data[", "async_update_entry"):
            assert forbidden not in text, forbidden
        assert "async_remove(row.entity_id)" in text and "async_remove_device(device.id)" in text

    def test_it_runs_at_component_setup_after_the_zones_are_prepared_and_before_the_retirement(self) -> None:
        node = _func("__init__.py", "async_setup")
        order = [ast.unparse(n.value if isinstance(n, ast.Expr) else n) for n in node.body[1:]]
        calls = [s for s in order if "prepare_zone_entries" in s or "cleanup_legacy_system_registry_rows" in s
                 or "retire_system_entry" in s]
        assert calls[0].startswith("_prepare_zone_entries")
        assert calls[1].startswith("_cleanup_legacy_system_registry_rows")
        assert "retire_system_entry" in calls[2]

    def test_a_failure_of_the_clean_up_is_only_logged(self) -> None:
        node = _func("__init__.py", "_cleanup_legacy_system_registry_rows")
        handler = next(n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler))
        assert "_LOGGER" in ast.dump(handler) and not any(isinstance(s, ast.Raise) for s in handler.body)

    def test_the_restore_never_sets_the_entry_up_while_the_component_setup_is_running(self) -> None:
        text = ast.unparse(_func("system_retirement_lifecycle.py", "_restore_loaded"))
        assert "DOMAIN not in hass.config.components" in text
        assert text.index("DOMAIN not in hass.config.components") < text.index("async_setup(")

    def test_every_restore_in_the_lifecycle_goes_through_the_guarded_helper(self) -> None:
        tree = ast.parse(_src("system_retirement_lifecycle.py"))
        offenders = []
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name != "_restore_loaded":
                if "config_entries.async_setup(" in ast.unparse(fn):
                    offenders.append(fn.name)
        assert offenders == []
