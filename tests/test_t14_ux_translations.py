"""T14: structural tests for translation completeness, menu labels, error
message symmetry, and German terminology consistency.

These are structural/regression tests, not full manual-review substitutes —
per T14's own guidance, exact-full-text comparisons are avoided in favor of
key-presence, key-parity, and targeted substring assertions that stay robust
to future wording tweaks while still catching the specific regressions this
ticket fixed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
_STRINGS_PATH = _BASE / "strings.json"
_TRANSLATIONS_DIR = _BASE / "translations"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten_keys(d: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= _flatten_keys(v, full)
        else:
            keys.add(full)
    return keys


def _all_translation_files() -> list[Path]:
    return sorted(_TRANSLATIONS_DIR.glob("*.json"))


class TestTranslationCompleteness:
    """15: 'Vollständigkeit aller Übersetzungen' / 'keine verwaisten Schlüssel'."""

    def test_strings_json_is_valid_json(self) -> None:
        _load(_STRINGS_PATH)  # raises on invalid JSON

    def test_all_translation_files_are_valid_json(self) -> None:
        for path in _all_translation_files():
            _load(path)  # raises on invalid JSON

    def test_every_translation_file_matches_strings_json_key_set(self) -> None:
        canonical = _flatten_keys(_load(_STRINGS_PATH))
        for path in _all_translation_files():
            keys = _flatten_keys(_load(path))
            missing = canonical - keys
            orphaned = keys - canonical
            assert not missing, f"{path.name} is missing keys: {sorted(missing)[:10]}"
            assert not orphaned, f"{path.name} has orphaned keys: {sorted(orphaned)[:10]}"

    def test_en_json_matches_strings_json_exactly(self) -> None:
        # en.json is the canonical English translation and must mirror
        # strings.json's key set (HA's own convention for this integration).
        canonical = _flatten_keys(_load(_STRINGS_PATH))
        en_keys = _flatten_keys(_load(_TRANSLATIONS_DIR / "en.json"))
        assert canonical == en_keys

    def test_at_least_twenty_translation_files_present(self) -> None:
        # Sanity floor so an accidental bulk-delete doesn't silently pass.
        assert len(_all_translation_files()) >= 20


class TestOrphanedErrorKeyRemoved:
    """T14 fix: 'night_lift_requires_night_block' was never raised by any
    config_flow.py code path — a genuinely orphaned translation key, removed
    from strings.json and every translation file."""

    def test_orphaned_key_absent_from_strings_json(self) -> None:
        data = _load(_STRINGS_PATH)
        assert "night_lift_requires_night_block" not in data.get("options", {}).get("error", {})
        assert "night_lift_requires_night_block" not in data.get("config", {}).get("error", {})

    def test_orphaned_key_absent_from_every_translation_file(self) -> None:
        for path in _all_translation_files():
            data = _load(path)
            assert "night_lift_requires_night_block" not in data.get("options", {}).get("error", {}), path.name
            assert "night_lift_requires_night_block" not in data.get("config", {}).get("error", {}), path.name

    def test_orphaned_key_never_referenced_in_config_flow_source(self) -> None:
        source = (_BASE / "config_flow.py").read_text(encoding="utf-8")
        assert "night_lift_requires_night_block" not in source


class TestOptionsErrorBlockSymmetry:
    """T14 fix: async_step_add_window/edit_window_basics (options flow) can
    raise invalid_custom_azimuth/no_covers_selected, but options.error was
    missing both translations — the raw key would have been shown instead
    of a message."""

    @pytest.mark.parametrize("key", ["invalid_custom_azimuth", "no_covers_selected"])
    def test_key_present_in_options_error_block(self, key: str) -> None:
        data = _load(_STRINGS_PATH)
        assert key in data["options"]["error"], (
            f"options.error is missing '{key}', but it can be raised by the "
            "options-flow window steps."
        )

    @pytest.mark.parametrize("key", ["invalid_custom_azimuth", "no_covers_selected"])
    def test_key_present_in_every_translation_file(self, key: str) -> None:
        for path in _all_translation_files():
            data = _load(path)
            assert key in data["options"]["error"], f"{path.name} options.error missing '{key}'"


class TestMenuOptionsCompleteness:
    """T14 fix: the options-flow init menu offers a 'dispatch' entry
    (config_flow.py's menu_options list) but strings.json's
    options.step.init.menu_options never had a label for it."""

    # T21 Phase C: the top-level "init" menu was reduced from 11 flat items
    # to 7 by grouping window CRUD under "windows" and the less-frequently
    # used sections under "advanced" (both themselves menu steps).
    _EXPECTED_INIT_MENU_KEYS = {
        "weather", "lifecycle", "presence", "comfort", "behavior",
        "windows", "advanced",
    }
    _EXPECTED_WINDOWS_MENU_KEYS = {"add_window", "edit_window", "remove_window"}
    _EXPECTED_ADVANCED_MENU_KEYS = {"lifecycle_profiles", "manual_override", "dispatch"}

    def test_strings_json_menu_options_has_all_expected_keys(self) -> None:
        data = _load(_STRINGS_PATH)
        step = data["options"]["step"]
        assert self._EXPECTED_INIT_MENU_KEYS <= set(step["init"]["menu_options"].keys())
        assert self._EXPECTED_WINDOWS_MENU_KEYS <= set(step["windows"]["menu_options"].keys())
        assert self._EXPECTED_ADVANCED_MENU_KEYS <= set(step["advanced"]["menu_options"].keys())

    def test_every_menu_option_has_a_non_empty_label_in_every_language(self) -> None:
        for path in _all_translation_files():
            data = _load(path)
            step = data["options"]["step"]
            for step_id, expected_keys in (
                ("init", self._EXPECTED_INIT_MENU_KEYS),
                ("windows", self._EXPECTED_WINDOWS_MENU_KEYS),
                ("advanced", self._EXPECTED_ADVANCED_MENU_KEYS),
            ):
                menu = step[step_id]["menu_options"]
                for key in expected_keys:
                    assert key in menu, f"{path.name} {step_id}.menu_options missing '{key}'"
                    assert isinstance(menu[key], str) and menu[key].strip(), (
                        f"{path.name} {step_id}.menu_options['{key}'] is empty"
                    )

    def test_dispatch_menu_step_id_matches_a_real_step(self) -> None:
        # "dispatch" must actually be a defined options.step (not just a
        # menu label pointing nowhere).
        data = _load(_STRINGS_PATH)
        assert "dispatch" in data["options"]["step"]


class TestGermanOverrideTerminologyConsistency:
    """T14: de.json previously mixed the English loanword 'Override' (in
    config/options-flow labels) with the native 'Übersteuerung' (in entity
    state/name labels) for the same concept. Standardized on
    'Übersteuerung' throughout the visible flow/service text."""

    def test_no_bare_override_string_in_de_json_values(self) -> None:
        de = _load(_TRANSLATIONS_DIR / "de.json")

        def _scan(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    _scan(v, f"{path}.{k}" if path else k)
            elif isinstance(node, str):
                assert "Override" not in node, (
                    f"de.json[{path}] still contains the English loanword "
                    f"'Override' instead of 'Übersteuerung': {node!r}"
                )

        _scan(de)

    def test_manual_override_step_title_uses_uebersteuerung(self) -> None:
        de = _load(_TRANSLATIONS_DIR / "de.json")
        assert de["options"]["step"]["manual_override"]["title"] == "Manuelle Übersteuerung"

    def test_service_name_uses_uebersteuerung(self) -> None:
        de = _load(_TRANSLATIONS_DIR / "de.json")
        assert "Übersteuerung" in de["services"]["clear_manual_override"]["name"]


class TestServiceDescriptionsComplete:
    """15: 'Service-Beschreibungen' — every services.yaml service has a
    non-empty name+description in every translation file."""

    def test_clear_manual_override_has_name_and_description_everywhere(self) -> None:
        for path in [_STRINGS_PATH, *_all_translation_files()]:
            data = _load(path)
            svc = data["services"]["clear_manual_override"]
            assert svc.get("name", "").strip()
            assert svc.get("description", "").strip()
