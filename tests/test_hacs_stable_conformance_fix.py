"""Regression tests for the HACS PR #8691 stable-conformance fixes.

Covers, per the real maintainer review (frenck, 2026-08-27) on
https://github.com/hacs/default/pull/8691:
  - hacs.json declares a Home Assistant minimum version.
  - manifest.json declares integration_type "helper".
  - README's HA badge matches the declared minimum.
  - The Support/Research Export buttons no longer write an unauthenticated
    file to /config/www/ (entities/button.py).
  - The same export data is aggregated into the standard Home Assistant
    Diagnostics download instead (diagnostics.py), with the manifest read
    that used to block the event loop now run in the executor.
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


class TestManifestDeclaresHelperIntegrationType:
    def test_integration_type_is_helper(self) -> None:
        data = json.loads(
            (_INTEGRATION_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        assert data.get("integration_type") == "helper"


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


class TestSupportResearchExportButtonsNoLongerWriteFiles:
    """entities/button.py must never touch /config/www/ or the filesystem at
    all anymore -- the export data now lives in the standard HA Diagnostics
    download (diagnostics.py) instead of an unauthenticated public file."""

    def test_file_writing_symbols_are_gone(self) -> None:
        source = (_INTEGRATION_ROOT / "entities" / "button.py").read_text(encoding="utf-8")
        for forbidden in (
            "_write_export_file", "www_dir",
            "_export_filename", "_research_export_filename",
            "write_text(", ".mkdir(",
        ):
            assert forbidden not in source, f"{forbidden!r} must not appear in button.py anymore"

    def test_async_press_creates_only_a_notification(self) -> None:
        sys.modules.pop("custom_components.smartshading.entities.button", None)

        notifications: list[dict] = []

        def _fake_pn_create(hass, *, message, title, notification_id):
            notifications.append(
                {"message": message, "title": title, "notification_id": notification_id}
            )

        sys.modules["homeassistant.components.persistent_notification"] = _stub_mod(
            "homeassistant.components.persistent_notification",
            async_create=_fake_pn_create,
        )

        from custom_components.smartshading.entities import button as button_mod

        class _FakeConfig:
            language = "en"

        class _FakeHass:
            config = _FakeConfig()

        class _FakeEntry:
            entry_id = "sys1"

        btn = button_mod.SmartShadingExportButton(_FakeHass(), _FakeEntry())
        asyncio.run(btn.async_press())

        assert len(notifications) == 1
        assert "Diagnostics" in notifications[0]["message"] or (
            "Diagnose" in notifications[0]["message"]
        )
        # The message may explain (in prose) that the old path is gone --
        # what matters is that async_press() itself never touches the
        # filesystem, verified separately by test_file_writing_symbols_are_gone.

        research_btn = button_mod.SmartShadingResearchExportButton(_FakeHass(), _FakeEntry())
        asyncio.run(research_btn.async_press())
        assert len(notifications) == 2


class TestDiagnosticsIncludesSupportAndResearchExport:
    """diagnostics.py must aggregate the same privacy-safe export data the
    removed /config/www buttons used to write, and must never block the
    event loop reading manifest.json for the integration version."""

    def test_build_support_and_research_sections_uses_executor_for_version_read(self) -> None:
        sys.modules.pop("custom_components.smartshading.diagnostics", None)
        sys.modules.pop("custom_components.smartshading.entities.button", None)

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
