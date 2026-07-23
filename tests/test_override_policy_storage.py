"""Storage round-trip and backwards-compatibility tests for
OverridePolicyConfig (config_entry_data.py) — T7, renamed for T10's
release-strategy architecture.

Coverage (T7 review point 15, test list items 35-38; migrated for T10):
  OPS-01  A ConfigEntry with no override_policy key at all (every pre-T7
          config) restores to full legacy defaults (release_strategy=
          LIFECYCLE, safety_timeout_enabled=True — T10's default
          reproduces T7's break_on_lifecycle=True default exactly).
  OPS-02  Full round trip preserves every field.
  OPS-03  An unknown release_strategy value falls back to LIFECYCLE (the
          T10 default fallback — see config_entry_data.py
          _override_policy_from_storage()).
  OPS-04  release_strategy="fixed_time" with a missing/invalid fixed_until
          falls back deterministically to DURATION (T7's "legacy",
          renamed) — not a crash, not a silent no-op FIXED_TIME with an
          unusable None boundary.
"""
from __future__ import annotations

from datetime import time

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    from_storage_dict,
    to_storage_dict,
)
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.models.override_policy import OverridePolicyConfig


class TestMissingKeyFallsBackToLegacyDefaults:
    def test_no_override_policy_key_at_all(self) -> None:
        raw = {"name": "Test", "use_home_location": True}
        restored = from_storage_dict(raw)
        assert restored.override_policy == OverridePolicyConfig()
        assert restored.override_policy.release_strategy is OverrideReleaseStrategy.LIFECYCLE
        assert restored.override_policy.allow_comfort_actions is False
        assert restored.override_policy.allow_protection_actions is False
        assert restored.override_policy.duration_min == 120
        assert restored.override_policy.night_duration_min == 720
        assert restored.override_policy.detection_tolerance == 10
        assert restored.override_policy.safety_timeout_enabled is True

    def test_empty_override_policy_dict(self) -> None:
        raw = {"name": "Test", "use_home_location": True, "override_policy": {}}
        restored = from_storage_dict(raw)
        assert restored.override_policy == OverridePolicyConfig()


class TestFullRoundTrip:
    def test_fixed_time_policy_survives_round_trip(self) -> None:
        policy = OverridePolicyConfig(
            release_strategy=OverrideReleaseStrategy.FIXED_TIME,
            fixed_until=time(8, 30),
            allow_comfort_actions=True,
            allow_protection_actions=True,
            duration_min=90,
            night_duration_min=600,
            detection_tolerance=15,
            safety_timeout_enabled=False,
        )
        data = SmartShadingConfigEntryData(name="Test", use_home_location=True, override_policy=policy)
        stored = to_storage_dict(data)
        assert stored["override_policy"]["release_strategy"] == "fixed_time"
        assert stored["override_policy"]["fixed_until"] == "08:30:00"
        restored = from_storage_dict(stored)
        assert restored.override_policy == policy


class TestInvalidValuesFallBackSafely:
    def test_unknown_release_strategy_falls_back_to_lifecycle(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {"release_strategy": "some_future_mode_v99"},
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.release_strategy is OverrideReleaseStrategy.LIFECYCLE

    def test_fixed_time_without_fixed_until_falls_back_to_duration(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {"release_strategy": "fixed_time"},  # no fixed_until at all
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.release_strategy is OverrideReleaseStrategy.DURATION

    def test_fixed_time_with_malformed_fixed_until_falls_back_to_duration(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {"release_strategy": "fixed_time", "fixed_until": "not-a-time"},
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.release_strategy is OverrideReleaseStrategy.DURATION

    def test_from_storage_dict_never_raises_on_garbage_override_policy(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {
                "release_strategy": 12345,
                "fixed_until": ["not", "a", "string"],
                "allow_comfort_actions": "yes",
                "duration_min": "abc",
            },
        }
        # Must not raise — either falls back safely or coerces; the
        # important invariant is "never crashes the whole ConfigEntry".
        try:
            from_storage_dict(raw)
        except (ValueError, TypeError) as exc:
            raise AssertionError(f"from_storage_dict() must never raise on malformed data: {exc}")


class TestOldFormatStillMigratesOnLoad:
    """T10 backward compatibility: a ConfigEntry stored by T7-T9 (before the
    release_strategy key existed) carries "duration_mode"/"break_on_lifecycle"
    instead. config_entry_data._override_policy_from_storage() must still
    migrate it in-memory on every load — this is the actual regression this
    file's OPS items were originally guarding, so it is kept as a distinct,
    explicit case rather than folded into the new-format tests above (which
    exercise a different, newer code path — an explicit stored
    release_strategy key always wins over duration_mode/break_on_lifecycle)."""

    def test_pre_t10_legacy_duration_mode_with_break_on_lifecycle_true_migrates_to_lifecycle(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {"duration_mode": "legacy", "break_on_lifecycle": True},
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.release_strategy is OverrideReleaseStrategy.LIFECYCLE

    def test_pre_t10_legacy_duration_mode_with_break_on_lifecycle_false_migrates_to_duration(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {"duration_mode": "legacy", "break_on_lifecycle": False},
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.release_strategy is OverrideReleaseStrategy.DURATION

    def test_pre_t10_fixed_time_duration_mode_with_break_on_lifecycle_false_migrates_to_fixed_time(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {
                "duration_mode": "fixed_time", "fixed_until": "08:30:00",
                "break_on_lifecycle": False,
            },
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.release_strategy is OverrideReleaseStrategy.FIXED_TIME
