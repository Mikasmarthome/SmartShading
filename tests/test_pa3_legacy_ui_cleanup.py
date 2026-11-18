"""PA-3 (stub suite): the legacy System UI / entities / debug switch are removed, nothing shared is.

Behavior against real Home Assistant (blocked-legacy recovery, no recreation, guards) lives in
tests/real_ha/test_real_ha_pa3_legacy_cleanup.py; this module pins the source- and
translation-level contracts and the pure behavior that needs no Home Assistant.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
FILES = [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]


def _src(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _func(name: str, source_file: str = "__init__.py") -> ast.AST:
    tree = ast.parse(_src(source_file))
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# System-only modules, platforms and entity registration are gone
# ---------------------------------------------------------------------------

class TestSystemOnlyCodeIsGone:
    def test_modules_are_deleted(self) -> None:
        for rel in ("button.py", "entities/button.py", "entities/system_switch.py"):
            assert not (ROOT / rel).exists(), rel

    def test_platform_lists(self) -> None:
        from custom_components.smartshading import const
        assert const.ZONE_PLATFORMS == ["sensor", "binary_sensor", "switch"]
        for name in ("SYSTEM_PLATFORMS", "PLATFORMS", "DATA_DEBUG_LOGGING", "CONF_DEBUG_LOGGING"):
            assert not hasattr(const, name), name
        assert const.SYSTEM_DEVICE_IDENTIFIER == "smartshading_system"   # kept for registry-cleanup checks

    def test_the_switch_platform_serves_zones_only(self) -> None:
        text = _src("switch.py")
        assert "system" not in text.lower().replace("legacy system entry", "")
        assert "entities.switch import async_setup_entry" in text

    def test_the_system_entry_forwards_and_unloads_no_platform(self) -> None:
        setup = ast.dump(_func("_async_setup_system_entry"))
        assert "async_forward_entry_setups" not in setup
        unload = _func("async_unload_entry")
        branch = next(n for n in unload.body if isinstance(n, ast.If))
        assert isinstance(branch.body[0], ast.Return) and ast.literal_eval(branch.body[0].value) is True

    def test_no_reference_to_removed_symbols_anywhere_in_the_integration(self) -> None:
        for path in ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for removed in ("SYSTEM_PLATFORMS", "DATA_DEBUG_LOGGING", "CONF_DEBUG_LOGGING",
                            "DebugLoggingSwitch", "SmartShadingExportButton",
                            "SmartShadingResearchExportButton", "async_step_system_",
                            "_save_system_default_and_reload"):
                assert removed not in text, f"{removed} in {path.name}"

    def test_the_options_flow_never_writes_system_default_keys(self) -> None:
        text = _src("config_flow.py")
        assert '"system_override_policy"' not in text and '"system_absence_delay_min"' not in text

    def test_the_resolvers_still_read_legacy_system_data(self) -> None:
        """Required legacy support: a zone that still defers keeps resolving from the System data."""
        from custom_components.smartshading.config_entry_data import (
            resolve_zone_absence_delay_min,
        )
        zone = {"absence_delay_use_system_default": True, "absence_delay_min": 5}
        assert resolve_zone_absence_delay_min(zone, {"system_absence_delay_min": 77}) == 77
        zone["absence_delay_use_system_default"] = False
        assert resolve_zone_absence_delay_min(zone, {"system_absence_delay_min": 77}) == 5


# ---------------------------------------------------------------------------
# Logging: the standard HA logger configuration decides, no integration flag
# ---------------------------------------------------------------------------

class TestStandardLogging:
    def _prop(self):
        """The property's own body, compiled in isolation (the coordinator module itself needs
        Home Assistant): the logger it consults is the coordinator module's logger."""
        import textwrap
        source = _src("coordinator.py")
        node = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.FunctionDef) and n.name == "_debug_logging_enabled")
        namespace = {"logging": logging,
                     "_LOGGER": logging.getLogger("custom_components.smartshading.coordinator")}
        exec(textwrap.dedent(ast.get_source_segment(source, node)), namespace)  # noqa: S102
        return namespace["_debug_logging_enabled"]

    def test_debug_output_follows_the_logger_level(self) -> None:
        logger = logging.getLogger("custom_components.smartshading.coordinator")
        previous = logger.level
        try:
            probe = SimpleNamespace()               # no hass, no flag: only the logger decides
            logger.setLevel(logging.WARNING)
            assert self._prop()(probe) is False
            logger.setLevel(logging.INFO)
            assert self._prop()(probe) is False
            logger.setLevel(logging.DEBUG)
            assert self._prop()(probe) is True
        finally:
            logger.setLevel(previous)

    def test_the_package_level_logger_setting_applies(self) -> None:
        """`logger: logs: custom_components.smartshading: debug` (the standard HA setting)."""
        package = logging.getLogger("custom_components.smartshading")
        coordinator_logger = logging.getLogger("custom_components.smartshading.coordinator")
        saved = package.level, coordinator_logger.level
        try:
            coordinator_logger.setLevel(logging.NOTSET)
            package.setLevel(logging.DEBUG)
            assert self._prop()(SimpleNamespace()) is True
            package.setLevel(logging.ERROR)
            assert self._prop()(SimpleNamespace()) is False
        finally:
            package.setLevel(saved[0]); coordinator_logger.setLevel(saved[1])

    def test_no_hass_data_flag_is_read(self) -> None:
        tree = ast.parse(_src("coordinator.py"))
        prop = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_debug_logging_enabled")
        assert "hass" not in ast.dump(prop) and "isEnabledFor" in ast.dump(prop)

    def test_every_verbose_log_site_is_still_guarded_by_the_same_property(self) -> None:
        text = _src("coordinator.py")
        assert text.count("if self._debug_logging_enabled:") == 8     # 8 `if` + 1 `elif` (the substring matches both)
        assert text.count("elif self._debug_logging_enabled:") == 1

    def test_no_code_changes_the_logger_configuration(self) -> None:
        for path in ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert ".setLevel(" not in text, path.name
            assert '"set_level"' not in text and "'set_level'" not in text, path.name


# ---------------------------------------------------------------------------
# The retention cleanup of old public export files no longer depends on the System entry
# ---------------------------------------------------------------------------

class TestExportRetentionMoved:
    def test_component_setup_runs_it_and_the_system_entry_does_not(self) -> None:
        assert "_async_cleanup_legacy_exports" in ast.dump(_func("async_setup"))
        assert "cleanup" not in ast.dump(_func("_async_setup_system_entry")).lower()

    def test_it_runs_in_the_executor_and_a_failure_is_only_logged(self) -> None:
        node = _func("_async_cleanup_legacy_exports")
        dumped = ast.dump(node)
        assert "async_add_executor_job" in dumped and "cleanup_old_exports" in dumped and "'www'" in dumped
        handler = next(n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler))
        assert "_LOGGER" in ast.dump(handler) and not any(isinstance(s, ast.Raise) for s in handler.body)


# ---------------------------------------------------------------------------
# Retirement lifecycle documentation matches what is guaranteed (follow-up A / D)
# ---------------------------------------------------------------------------

class TestLifecycleDocumentation:
    def test_no_atomicity_or_no_await_claim(self) -> None:
        text = _src("system_retirement_lifecycle.py")
        lowered = text.lower()
        for claim in ("without an await", "no await between", "removed without suspending",
                      "no zone change fits"):
            assert claim not in lowered, claim
        assert "not an atomic" in lowered
        assert "_refuse_reinheritance" in text and "setup_in_progress" in lowered

    def test_the_pre_wait_and_the_gone_check_are_still_there(self) -> None:
        text = ast.dump(_func("_remove_system_entry", "system_retirement_lifecycle.py"))
        assert "setup_lock" in text and "_gone" in text and "_restore_loaded" in text


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------

class TestTranslations:
    def test_there_are_25_files(self) -> None:
        assert len(FILES) == 25

    @pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
    def test_removed_system_strings_are_gone(self, path: Path) -> None:
        data = _load(path)
        steps = data["options"]["step"]
        for gone in ("system_init", "system_manual_override", "system_presence"):
            assert gone not in steps
        assert "button" not in data["entity"]
        assert "debug_logging" not in data["entity"]["switch"]
        assert set(data["entity"]["switch"]) == {"learning_mode", "active_control"}
        assert set(data["options"]["abort"]) == {"legacy_system_entry"}
        assert data["options"]["abort"]["legacy_system_entry"].strip()

    @pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
    def test_the_repair_guidance_points_to_actions_that_exist(self, path: Path) -> None:
        """The blocked-zone text names the zone's own Manual Override and Presence options and the
        'System default' switch exactly as this language's UI shows them, and keeps the zone
        placeholder."""
        data = _load(path)
        text = data["issues"]["system_retirement_zone_blocked"]["description"]
        steps = data["options"]["step"]
        assert "{zone}" in text
        for label in (steps["manual_override"]["title"], steps["presence"]["title"],
                      steps["manual_override"]["data"]["override_use_system_default"],
                      steps["presence"]["data"]["absence_delay_use_system_default"]):
            assert label in text, f"{path.name}: {label!r} is not quoted in the repair text"

    def test_deleting_the_zone_is_only_offered_as_a_qualified_last_resort(self) -> None:
        en = _load(ROOT / "translations" / "en.json")["issues"]["system_retirement_zone_blocked"]["description"]
        assert "last resort" in en
        assert "permanently discards" in en and "learned data" in en and "entity identities" in en
        # the delete instruction comes after the safe steps and the restart, never first
        assert en.index("switch off") < en.index("restart Home Assistant") < en.index("last resort")
        assert "If that is not possible, remove the zone" not in en

    @pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
    def test_all_four_repair_issues_keep_their_shape(self, path: Path) -> None:
        issues = _load(path)["issues"]
        for key, placeholder in (("system_retirement_zone_blocked", True),
                                 ("system_retirement_zone_not_saved", True),
                                 ("system_retirement_zone_unsupported_version", True),
                                 ("system_retirement_removal_failed", False)):
            entry = issues[key]
            assert entry["title"].strip() and entry["description"].strip()
            assert ("{zone}" in entry["description"]) is placeholder, (path.name, key)

    def test_non_english_files_are_translated(self) -> None:
        en = _load(ROOT / "translations" / "en.json")
        for path in FILES:
            if path.name in ("en.json", "strings.json"):
                continue
            data = _load(path)
            assert data["options"]["abort"]["legacy_system_entry"] != en["options"]["abort"]["legacy_system_entry"], path.name
            assert data["issues"]["system_retirement_zone_blocked"]["description"] != \
                en["issues"]["system_retirement_zone_blocked"]["description"], path.name

    def test_the_files_stay_byte_stable_json(self) -> None:
        for path in FILES:
            raw = path.read_bytes().decode("utf-8")
            crlf = "\r\n" in raw
            text = raw.replace("\r\n", "\n")
            again = json.dumps(json.loads(text), indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")
            assert again == text, path.name
            assert (("\r\n" in raw) == crlf)
