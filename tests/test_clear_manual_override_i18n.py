"""i18n completeness for the T10.1 clear_manual_override service.

Coverage:
  I18N-01  strings.json + all 24 translations/*.json contain a
           services.clear_manual_override.name/description entry.
  I18N-02  services.yaml exists and defines the target-only schema (no
           free-text fields requiring a name/description in strings.json
           beyond the top-level service name/description).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_INTEGRATION_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


def _all_i18n_files() -> list[Path]:
    files = [_INTEGRATION_ROOT / "strings.json"]
    files += sorted((_INTEGRATION_ROOT / "translations").glob("*.json"))
    return files


class TestServiceStringsPresentEverywhere:
    @pytest.mark.parametrize("path", _all_i18n_files(), ids=lambda p: p.name)
    def test_clear_manual_override_service_strings_present(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "services" in data, f"{path.name}: missing top-level 'services' key"
        services = data["services"]
        assert "clear_manual_override" in services, f"{path.name}: missing clear_manual_override"
        entry = services["clear_manual_override"]
        assert isinstance(entry.get("name"), str) and entry["name"].strip()
        assert isinstance(entry.get("description"), str) and entry["description"].strip()

    def test_all_25_files_covered(self) -> None:
        assert len(_all_i18n_files()) == 25


class TestServicesYaml:
    def test_services_yaml_exists_and_defines_clear_manual_override(self) -> None:
        # No PyYAML dependency in this test suite (see requirements-test.txt) —
        # a plain top-level-key/structure check is sufficient here; the YAML
        # itself is trivial and Hassfest already validates it as real YAML.
        path = _INTEGRATION_ROOT / "services.yaml"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert text.startswith("clear_manual_override:")
        assert "target:" in text
        # Regression guard: Hassfest rejects a `device:` filter directly
        # under `target` ("Services do not support device filters on
        # target, use a device selector instead") — device/area targeting
        # in the HA UI's target picker still works without this key; it
        # only narrows which entities are suggested, which `entity:` alone
        # already does.
        assert "device:" not in text
