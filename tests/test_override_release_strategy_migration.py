"""Backward-compatibility migration tests for the T10 Manual Override
release-strategy rename (v1.2.0-beta.1).

Covers the two places a pre-T10 stored value must be transparently migrated
without ever rewriting storage or surprising an existing user:
  - config_entry_data._override_policy_from_storage() — the ConfigEntry's
    override_policy dict (duration_mode/break_on_lifecycle -> release_strategy).
  - ManualOverride.from_dict() — a persisted, still-active override surviving
    an HA restart across the T10 upgrade (duration_mode -> release_strategy).

Coverage:
  MIG-01  No stored override_policy at all -> defaults to LIFECYCLE (the T7
          "legacy" default's effective behavior).
  MIG-02  T7-era stored dict with break_on_lifecycle=True -> LIFECYCLE,
          regardless of duration_mode.
  MIG-03  T7-era stored dict with break_on_lifecycle=False, duration_mode=
          "legacy" -> DURATION.
  MIG-04  T7-era stored dict with break_on_lifecycle=False, duration_mode=
          "fixed_time" -> FIXED_TIME, fixed_until preserved.
  MIG-05  A T10+ stored release_strategy key always wins over any legacy keys
          present alongside it.
  MIG-06  safety_timeout_enabled defaults to True when absent (pre-T10
          storage never had this key).
  MIG-07  ManualOverride.from_dict() migrates a persisted pre-T10 entry
          (duration_mode key, no release_strategy key) the same way.
  MIG-08  ManualOverride.from_dict() prefers a persisted release_strategy key
          when present (T10+ persisted entry).
  MIG-09  ManualOverride.to_dict()/from_dict() round-trips a T10 entry
          losslessly (restart-safe persistence).
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smartshading.config_entry_data import _override_policy_from_storage
from custom_components.smartshading.models.manual_override import (
    ManualOverride,
    OverrideReleaseStrategy,
)
from custom_components.smartshading.state_machine.states import ShadingState


class TestOverridePolicyStorageMigration:
    def test_empty_storage_defaults_to_lifecycle(self) -> None:
        policy = _override_policy_from_storage(None)
        assert policy.release_strategy is OverrideReleaseStrategy.LIFECYCLE

    def test_t7_break_on_lifecycle_true_migrates_to_lifecycle_regardless_of_duration_mode(self) -> None:
        for duration_mode in ("legacy", "fixed_time"):
            policy = _override_policy_from_storage({
                "duration_mode": duration_mode, "break_on_lifecycle": True,
                "duration_min": 120, "night_duration_min": 720,
            })
            assert policy.release_strategy is OverrideReleaseStrategy.LIFECYCLE
            assert policy.duration_min == 120
            assert policy.night_duration_min == 720

    def test_break_on_lifecycle_false_legacy_migrates_to_duration(self) -> None:
        policy = _override_policy_from_storage({
            "duration_mode": "legacy", "break_on_lifecycle": False, "duration_min": 90,
        })
        assert policy.release_strategy is OverrideReleaseStrategy.DURATION
        assert policy.duration_min == 90

    def test_break_on_lifecycle_false_fixed_time_migrates_to_fixed_time(self) -> None:
        policy = _override_policy_from_storage({
            "duration_mode": "fixed_time", "break_on_lifecycle": False, "fixed_until": "08:00:00",
        })
        assert policy.release_strategy is OverrideReleaseStrategy.FIXED_TIME
        assert policy.fixed_until is not None and policy.fixed_until.hour == 8

    def test_stored_release_strategy_wins_over_legacy_keys(self) -> None:
        policy = _override_policy_from_storage({
            "release_strategy": "first_comfort",
            "duration_mode": "legacy", "break_on_lifecycle": True,
        })
        assert policy.release_strategy is OverrideReleaseStrategy.FIRST_COMFORT

    def test_safety_timeout_enabled_defaults_true_when_absent(self) -> None:
        policy = _override_policy_from_storage({
            "duration_mode": "legacy", "break_on_lifecycle": False,
        })
        assert policy.safety_timeout_enabled is True

    def test_safety_timeout_enabled_read_when_present(self) -> None:
        policy = _override_policy_from_storage({
            "release_strategy": "lifecycle", "safety_timeout_enabled": False,
        })
        assert policy.safety_timeout_enabled is False


class TestManualOverrideFromDictMigration:
    def _base_dict(self, **overrides) -> dict:
        base = {
            "window_id": "w1",
            "override_position": 20,
            "started_at": datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc).isoformat(),
            "expires_at": datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc).isoformat(),
            "source": "position_delta",
            "overridden_state": ShadingState.OPEN.value,
            "overridden_position": 0,
            "scope": "daytime",
        }
        base.update(overrides)
        return base

    def test_pre_t10_persisted_entry_migrates_legacy_to_duration(self) -> None:
        d = self._base_dict(duration_mode="legacy")
        ov = ManualOverride.from_dict(d)
        assert ov.release_strategy == "duration"

    def test_pre_t10_persisted_entry_migrates_fixed_time_unchanged(self) -> None:
        d = self._base_dict(duration_mode="fixed_time")
        ov = ManualOverride.from_dict(d)
        assert ov.release_strategy == "fixed_time"

    def test_missing_duration_mode_key_entirely_defaults_to_duration(self) -> None:
        d = self._base_dict()
        ov = ManualOverride.from_dict(d)
        assert ov.release_strategy == "duration"

    def test_t10_persisted_release_strategy_key_wins(self) -> None:
        d = self._base_dict(release_strategy="first_protection", duration_mode="legacy")
        ov = ManualOverride.from_dict(d)
        assert ov.release_strategy == "first_protection"

    def test_to_dict_from_dict_round_trip_lossless(self) -> None:
        original = ManualOverride(
            window_id="w9", override_position=33,
            started_at=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc),
            source="position_delta",
            overridden_state=ShadingState.NORMAL_SHADE,
            overridden_position=70,
            scope="night",
            release_strategy="first_any_decision",
        )
        restored = ManualOverride.from_dict(original.to_dict())
        assert restored == original
