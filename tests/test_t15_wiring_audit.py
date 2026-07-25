"""T15 — dead-code removal lock-in, persistence-schema-authority consistency,
and config-to-runtime wiring tests.

This is the pre-beta completeness audit ticket. These tests protect the
specific findings fixed in T15:

  A-findings (confirmed dead, removed): engines/research_export.py,
  engines/zone_learning_export.py, models/threshold_timing.py,
  WeatherSnapshot/WeatherEngine.get_snapshot()/is_rain_condition,
  ForecastResolution, and a handful of orphaned const.py entries.

  C-finding (competing schema authorities, consolidated): learning_migration.py
  previously advertised CURRENT_PAYLOAD_SCHEMA=3 while the actual reader
  (learning_persistence.deserialize_into_learning_store) only ever accepted
  versions 1/2 — a hypothetical v3 payload would have passed the migration
  gate and then been silently rejected by the real deserializer (empty-store
  fallback). Capped at 2 to match the real reader/writer.

  B-finding (untracked tasks, fixed): the three event-driven refresh tasks
  (presence/contact/lifecycle-boundary) now use
  entry.async_create_background_task(...) instead of a bare
  hass.async_create_task(...), so HA actually cancels them on unload.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


def _source(relpath: str) -> str:
    return (_BASE / relpath).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A-findings: dead modules/symbols must not silently come back
# ---------------------------------------------------------------------------

class TestDeadModulesRemainRemoved:
    @pytest.mark.parametrize("relpath", [
        "engines/research_export.py",
        "engines/zone_learning_export.py",
        "models/threshold_timing.py",
    ])
    def test_dead_module_file_does_not_exist(self, relpath: str) -> None:
        assert not (_BASE / relpath).exists(), (
            f"{relpath} was confirmed dead in the T15 audit (superseded, zero "
            "production callers) and removed — it must not be silently "
            "reintroduced."
        )

    def test_no_production_file_imports_removed_modules(self) -> None:
        forbidden = ("research_export", "zone_learning_export", "threshold_timing")
        for path in _BASE.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                assert f"import {name}" not in text and f"from .{name}" not in text \
                    and f".{name} import" not in text, (
                    f"{path.relative_to(_BASE)} references removed module '{name}'"
                )


class TestDeadSymbolsRemainRemoved:
    def test_weather_snapshot_and_get_snapshot_removed(self) -> None:
        source = _source("engines/weather_engine.py")
        assert "class WeatherSnapshot" not in source
        assert "def get_snapshot" not in source
        assert "def is_rain_condition" not in source

    def test_forecast_resolution_enum_removed(self) -> None:
        source = _source("models/forecast_learning.py")
        assert "class ForecastResolution" not in source

    def test_target_adaptation_export_summary_removed(self) -> None:
        source = _source("engines/target_position_adapter.py")
        assert "build_target_adaptation_export_summary" not in source

    @pytest.mark.parametrize("name", [
        "LEARNING_EXPORT_STORAGE_KEY",
        "LEARNING_EXPORT_STORAGE_VERSION",
        "CONF_RAIN_SAFE_POSITION",
        "CONF_RAIN_RELEASE_DELAY_MIN",
    ])
    def test_orphaned_const_removed(self, name: str) -> None:
        source = _source("const.py")
        assert name not in source, f"Orphaned constant '{name}' should stay removed"


# ---------------------------------------------------------------------------
# Weather engine still works after WeatherSnapshot/is_rain_condition removal
# ---------------------------------------------------------------------------

class TestWeatherEngineStillFunctionalAfterCleanup:
    def test_is_storm_condition_still_importable_and_correct(self) -> None:
        from custom_components.smartshading.engines.weather_engine import (
            WeatherCondition, WeatherEngine,
        )
        assert WeatherEngine.is_storm_condition(WeatherCondition.STORM, 5.0) is True
        assert WeatherEngine.is_storm_condition(WeatherCondition.CLEAR, 5.0) is False

    def test_calculate_effective_radiation_still_works(self) -> None:
        from custom_components.smartshading.engines.weather_engine import WeatherEngine
        assert WeatherEngine.calculate_effective_radiation(45.0, 0.0) > 0.0

    def test_parse_weather_condition_still_works(self) -> None:
        from custom_components.smartshading.engines.weather_engine import (
            WeatherCondition, WeatherEngine,
        )
        assert WeatherEngine.parse_weather_condition("sunny") == WeatherCondition.CLEAR


# ---------------------------------------------------------------------------
# C-finding: learning_migration schema ceiling matches the real reader/writer
# ---------------------------------------------------------------------------

class TestLearningMigrationSchemaAuthorityConsistency:
    def test_current_payload_schema_matches_the_real_writer_and_reader(self) -> None:
        from custom_components.smartshading.engines.learning_migration import (
            CURRENT_PAYLOAD_SCHEMA,
        )
        from custom_components.smartshading.engines.learning_persistence import (
            PAYLOAD_SCHEMA_V2,
        )
        # The migration module's notion of "current" must not exceed what the
        # real deserializer (learning_persistence.deserialize_into_learning_store)
        # actually accepts — otherwise a payload could pass the migration
        # gate's accept_authority check and then be silently rejected
        # (empty-store fallback) by the real reader.
        assert CURRENT_PAYLOAD_SCHEMA == PAYLOAD_SCHEMA_V2

    def test_a_payload_at_the_ceiling_is_accepted_by_the_real_reader(self) -> None:
        from datetime import datetime, timezone
        from custom_components.smartshading.engines.learning_migration import (
            CURRENT_PAYLOAD_SCHEMA, migrate_payload,
        )
        from custom_components.smartshading.engines.learning_persistence import (
            LearningPersistenceConfig, deserialize_into_learning_store,
        )
        from custom_components.smartshading.engines.learning_store import LearningStore

        payload = {"version": 1, "schema_version": CURRENT_PAYLOAD_SCHEMA, "windows": {}}
        result = migrate_payload(payload, owner_entry_id="e1")
        assert result.accept_authority is True

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Must not raise and must not silently fall back to an empty store
        # reason — proving the migration gate and the real reader agree.
        extras = deserialize_into_learning_store(
            result.data, LearningStore(), LearningPersistenceConfig(), now,
        )
        assert extras is not None

    def test_a_payload_above_the_ceiling_is_rejected_at_the_gate(self) -> None:
        from custom_components.smartshading.engines.learning_migration import (
            CURRENT_PAYLOAD_SCHEMA, migrate_payload,
        )
        payload = {"version": 1, "schema_version": CURRENT_PAYLOAD_SCHEMA + 1, "windows": {}}
        result = migrate_payload(payload, owner_entry_id="e1")
        assert result.accept_authority is False
        assert result.reason == "unknown_newer_schema"

    def test_v1_payload_migrates_to_the_ceiling(self) -> None:
        from custom_components.smartshading.engines.learning_migration import (
            CURRENT_PAYLOAD_SCHEMA, migrate_payload,
        )
        payload = {"version": 1, "windows": {}}  # no schema_version -> v1
        result = migrate_payload(payload, owner_entry_id="e1")
        assert result.accept_authority is True
        assert result.payload_schema_version == CURRENT_PAYLOAD_SCHEMA
        assert result.data["schema_version"] == CURRENT_PAYLOAD_SCHEMA


# ---------------------------------------------------------------------------
# Config -> Runtime wiring: dispatch config values genuinely change behavior
# ---------------------------------------------------------------------------

class TestDispatchConfigFromStorageWiring:
    def test_zone_batching_true_is_read_into_runtime_config(self) -> None:
        from custom_components.smartshading.config_entry_data import (
            _dispatch_config_from_storage,
        )
        cfg = _dispatch_config_from_storage({"mode": "spaced", "zone_batching": True})
        assert cfg.zone_batching is True

    def test_zone_batching_false_is_read_into_runtime_config(self) -> None:
        from custom_components.smartshading.config_entry_data import (
            _dispatch_config_from_storage,
        )
        cfg = _dispatch_config_from_storage({"mode": "spaced", "zone_batching": False})
        assert cfg.zone_batching is False

    def test_dispatch_mode_is_read_into_runtime_config(self) -> None:
        from custom_components.smartshading.config_entry_data import (
            _dispatch_config_from_storage,
        )
        from custom_components.smartshading.models.dispatch_config import DispatchMode
        cfg = _dispatch_config_from_storage({"mode": "parallel"})
        assert cfg.mode is DispatchMode.PARALLEL

    def test_missing_config_falls_back_to_documented_defaults(self) -> None:
        from custom_components.smartshading.config_entry_data import (
            _dispatch_config_from_storage,
        )
        from custom_components.smartshading.models.dispatch_config import DispatchConfig
        cfg = _dispatch_config_from_storage(None)
        assert cfg == DispatchConfig()


# ---------------------------------------------------------------------------
# B-finding: event-driven refresh tasks are entry-tracked, not bare hass tasks
# ---------------------------------------------------------------------------

class TestEventDrivenRefreshTasksAreEntryTracked:
    """Structural guard: coordinator.py's three event-listener callbacks
    (_on_presence_change / _on_contact_change / _on_boundary) must schedule
    their refresh via *.async_create_background_task(...) (config-entry
    tracked, cancelled by HA on unload) — not a bare hass.async_create_task(...)
    (untracked, invisible to unload, confirmed by the T15 audit)."""

    def test_no_bare_hass_async_create_task_in_listener_callbacks(self) -> None:
        source = _source("coordinator.py")
        tree = ast.parse(source)
        offending: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in ("_on_presence_change", "_on_contact_change", "_on_boundary"):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "async_create_task"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "hass"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "self"
                ):
                    offending.append(node.name)
        assert not offending, (
            f"Found bare self.hass.async_create_task(...) inside listener "
            f"callback(s) {offending} — this task would be untracked and "
            f"never cancelled on unload."
        )

    def test_all_three_callbacks_use_async_create_background_task(self) -> None:
        source = _source("coordinator.py")
        tree = ast.parse(source)
        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in ("_on_presence_change", "_on_contact_change", "_on_boundary"):
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "async_create_background_task"
                ):
                    found.add(node.name)
        assert found == {"_on_presence_change", "_on_contact_change", "_on_boundary"}
