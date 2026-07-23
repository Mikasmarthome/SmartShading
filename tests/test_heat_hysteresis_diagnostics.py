"""Diagnostics coverage for heat_protection_summary — v1.2.0-beta.1, T9.

engines/diagnostics_builder.py's build_consolidated_diagnostics() is
explicitly duck-typed (see its own docstring: "coordinator is duck-typed;
missing getters degrade to safe defaults"), so this exercises the real
function against a minimal fake coordinator — no HA stub machinery needed
(unlike tests that construct a real SmartShadingCoordinator).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from custom_components.smartshading.engines.diagnostics_builder import (
    build_consolidated_diagnostics,
)
from custom_components.smartshading.models.comfort import ComfortConfig


def _fake_coordinator(*, comfort_config=None, heat_active=None):
    return SimpleNamespace(
        zones={}, windows={}, cover_groups={},
        _comfort_config=comfort_config if comfort_config is not None else ComfortConfig(),
        _heat_active=heat_active if heat_active is not None else {},
    )


class TestHeatProtectionSummaryPresent:
    def test_key_exists_in_consolidated_contract(self) -> None:
        diag = build_consolidated_diagnostics(_fake_coordinator())
        assert "heat_protection_summary" in diag

    def test_default_config_reports_enabled_and_default_hysteresis(self) -> None:
        diag = build_consolidated_diagnostics(_fake_coordinator())
        summary = diag["heat_protection_summary"]
        assert summary["enabled"] is True
        assert summary["hysteresis_c"] == 1.0
        assert summary["active_window_count"] == 0

    def test_disabled_config_reflected(self) -> None:
        diag = build_consolidated_diagnostics(
            _fake_coordinator(comfort_config=ComfortConfig(heat_protection_enabled=False))
        )
        assert diag["heat_protection_summary"]["enabled"] is False

    def test_custom_hysteresis_value_reflected(self) -> None:
        diag = build_consolidated_diagnostics(
            _fake_coordinator(comfort_config=ComfortConfig(heat_protection_hysteresis_c=2.5))
        )
        assert diag["heat_protection_summary"]["hysteresis_c"] == 2.5

    def test_active_window_count_reflects_the_latch(self) -> None:
        diag = build_consolidated_diagnostics(
            _fake_coordinator(heat_active={"w1": True, "w2": False, "w3": True})
        )
        assert diag["heat_protection_summary"]["active_window_count"] == 2

    def test_no_window_ids_leak_into_the_summary(self) -> None:
        """PUBLIC_SAFE contract: aggregate count only, never a window_id."""
        diag = build_consolidated_diagnostics(
            _fake_coordinator(heat_active={"secret-window-id": True})
        )
        dumped = json.dumps(diag["heat_protection_summary"])
        assert "secret-window-id" not in dumped

    def test_missing_heat_active_attribute_degrades_safely(self) -> None:
        """No self._heat_active at all (e.g. an older/mocked coordinator)
        must not raise — active_window_count falls back to 0."""
        c = SimpleNamespace(zones={}, windows={}, cover_groups={}, _comfort_config=ComfortConfig())
        diag = build_consolidated_diagnostics(c)
        assert diag["heat_protection_summary"]["active_window_count"] == 0

    def test_result_is_json_safe(self) -> None:
        diag = build_consolidated_diagnostics(
            _fake_coordinator(heat_active={"w1": True})
        )
        json.dumps(diag)  # must not raise
