"""Regression tests for the HACS PR #8691 stable-conformance fixes.

Covers, per the real maintainer review (frenck, 2026-08-27) on
https://github.com/hacs/default/pull/8691:
  - hacs.json declares a Home Assistant minimum version.
  - README's HA badge matches the declared minimum.
  - The Support/Research Export buttons no longer write an unauthenticated
    file to /config/www/ (and are gone altogether since PA-3).
  - The same export data is aggregated into the standard Home Assistant
    Diagnostics download instead (diagnostics.py), with the manifest read
    that used to block the event loop now run in the executor.

manifest.json's integration_type is covered separately below
(TestManifestDeclaresDeviceIntegrationType) -- its value changed again in
v1.1.11, after this review.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


class TestHacsJsonDeclaresMinimumHomeAssistantVersion:
    def test_homeassistant_key_present_and_matches_real_code_requirement(self) -> None:
        """__init__.py uses a PEP 695 `type` statement (Python 3.12+, shipped
        in Home Assistant 2024.6) and entry.runtime_data (introduced in
        2024.6) -- hacs.json must declare that real minimum so HACS never
        offers this integration to an HA version that cannot import it."""
        data = json.loads((_REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))
        assert data.get("homeassistant") == "2024.6.0"


class TestManifestDeclaresDeviceIntegrationType:
    def test_integration_type_is_device(self) -> None:
        """v1.1.11: a SmartShading config entry is treated as a logical
        shading controller, and its entity platforms (base.py, switch.py,
        zone_summary.py) already declare a real DeviceInfo --
        so integration_type is "device"."""
        data = json.loads(
            (_INTEGRATION_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        assert data.get("integration_type") == "device"


class TestReadmeBadgeMatchesDeclaredMinimum:
    def test_ha_badge_says_2024_6_plus_not_2024_1(self) -> None:
        readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "2024.1" not in readme
        assert "HA-2024.6" in readme


def _stub_mod(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class TestSupportResearchExportButtonsAreGone:
    """The Support/Research export buttons never wrote a file again after the HACS review
    and were removed together with the legacy System entry's UI (PA-3): the export data
    lives only in the standard, authenticated HA Diagnostics download (diagnostics.py)."""

    def test_button_modules_and_platform_are_gone(self) -> None:
        assert not (_INTEGRATION_ROOT / "entities" / "button.py").exists()
        assert not (_INTEGRATION_ROOT / "button.py").exists()
        assert not (_INTEGRATION_ROOT / "entities" / "system_switch.py").exists()
        from custom_components.smartshading.const import ZONE_PLATFORMS
        assert "button" not in ZONE_PLATFORMS
        import custom_components.smartshading.const as const
        assert not hasattr(const, "SYSTEM_PLATFORMS")

    def test_no_code_writes_export_files_to_www(self) -> None:
        for path in _INTEGRATION_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("_write_export_file", "_export_filename", "_research_export_filename"):
                assert forbidden not in source, f"{forbidden!r} in {path.name}"

    def test_diagnostics_aggregates_only_live_zone_coordinators(self) -> None:
        from types import SimpleNamespace

        from custom_components.smartshading import diagnostics as diagnostics_mod

        zone = SimpleNamespace(
            data={}, runtime_data=SimpleNamespace(coordinator="zone-coordinator"))
        legacy = SimpleNamespace(
            data={"entry_type": "system"},
            runtime_data=SimpleNamespace(coordinator="never-used"))
        idle = SimpleNamespace(data={}, runtime_data=None)
        hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_entries=lambda domain: [zone, legacy, idle]))
        assert diagnostics_mod._active_zone_coordinators(hass) == ["zone-coordinator"]


class TestDiagnosticsIncludesSupportAndResearchExport:
    """diagnostics.py must aggregate the same privacy-safe export data the
    removed /config/www buttons used to write, and must never block the
    event loop reading manifest.json for the integration version."""

    def test_build_support_and_research_sections_uses_executor_for_version_read(self) -> None:
        sys.modules.pop("custom_components.smartshading.diagnostics", None)
        
        executor_calls: list = []

        class _FakeHass:
            class config_entries:
                @staticmethod
                def async_entries(domain):
                    return []

            @staticmethod
            async def async_add_executor_job(func, *args):
                executor_calls.append(func)
                return func(*args)

        from custom_components.smartshading import diagnostics as diagnostics_mod

        result = asyncio.run(
            diagnostics_mod._build_support_and_research_sections(_FakeHass())
        )

        assert "support_export" in result
        assert "research_export" in result
        # integration_version() must be the ONE synchronous call routed
        # through async_add_executor_job -- never called directly on the
        # event loop (the exact HA-2026.8 review point).
        from custom_components.smartshading.const import integration_version
        assert integration_version in executor_calls

    def test_no_active_zone_degrades_honestly_not_silently(self) -> None:
        class _FakeHass:
            class config_entries:
                @staticmethod
                def async_entries(domain):
                    return []

            @staticmethod
            async def async_add_executor_job(func, *args):
                return func(*args)

        from custom_components.smartshading import diagnostics as diagnostics_mod

        result = asyncio.run(
            diagnostics_mod._build_support_and_research_sections(_FakeHass())
        )
        assert result["support_export"]["overall_status"] == "no_active_zone"
        assert result["research_export"]["overall_status"] == "no_active_zone"
