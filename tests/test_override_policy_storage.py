"""Storage round-trip and backwards-compatibility tests for
OverridePolicyConfig (config_entry_data.py) — T7.

Coverage (T7 review point 15, test list items 35-38):
  OPS-01  A ConfigEntry with no override_policy key at all (every pre-T7
          config) restores to full legacy defaults.
  OPS-02  Full round trip preserves every field.
  OPS-03  An unknown duration_mode value falls back to legacy.
  OPS-04  duration_mode="fixed_time" with a missing/invalid fixed_until
          falls back deterministically to legacy (not a crash, not a
          silent no-op FIXED_TIME with an unusable None boundary).
"""
from __future__ import annotations

from datetime import time

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    from_storage_dict,
    to_storage_dict,
)
from custom_components.smartshading.models.manual_override import OverrideDurationMode
from custom_components.smartshading.models.override_policy import OverridePolicyConfig


class TestMissingKeyFallsBackToLegacyDefaults:
    def test_no_override_policy_key_at_all(self) -> None:
        raw = {"name": "Test", "use_home_location": True}
        restored = from_storage_dict(raw)
        assert restored.override_policy == OverridePolicyConfig()
        assert restored.override_policy.duration_mode is OverrideDurationMode.LEGACY
        assert restored.override_policy.allow_comfort_actions is False
        assert restored.override_policy.allow_protection_actions is False
        assert restored.override_policy.duration_min == 120
        assert restored.override_policy.night_duration_min == 720
        assert restored.override_policy.detection_tolerance == 10
        assert restored.override_policy.break_on_lifecycle is True

    def test_empty_override_policy_dict(self) -> None:
        raw = {"name": "Test", "use_home_location": True, "override_policy": {}}
        restored = from_storage_dict(raw)
        assert restored.override_policy == OverridePolicyConfig()


class TestFullRoundTrip:
    def test_fixed_time_policy_survives_round_trip(self) -> None:
        policy = OverridePolicyConfig(
            duration_mode=OverrideDurationMode.FIXED_TIME,
            fixed_until=time(8, 30),
            allow_comfort_actions=True,
            allow_protection_actions=True,
            duration_min=90,
            night_duration_min=600,
            detection_tolerance=15,
            break_on_lifecycle=False,
        )
        data = SmartShadingConfigEntryData(name="Test", use_home_location=True, override_policy=policy)
        stored = to_storage_dict(data)
        assert stored["override_policy"]["duration_mode"] == "fixed_time"
        assert stored["override_policy"]["fixed_until"] == "08:30:00"
        restored = from_storage_dict(stored)
        assert restored.override_policy == policy


class TestInvalidValuesFallBackSafely:
    def test_unknown_duration_mode_falls_back_to_legacy(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {"duration_mode": "some_future_mode_v99"},
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.duration_mode is OverrideDurationMode.LEGACY

    def test_fixed_time_without_fixed_until_falls_back_to_legacy(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {"duration_mode": "fixed_time"},  # no fixed_until at all
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.duration_mode is OverrideDurationMode.LEGACY

    def test_fixed_time_with_malformed_fixed_until_falls_back_to_legacy(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {"duration_mode": "fixed_time", "fixed_until": "not-a-time"},
        }
        restored = from_storage_dict(raw)
        assert restored.override_policy.duration_mode is OverrideDurationMode.LEGACY

    def test_from_storage_dict_never_raises_on_garbage_override_policy(self) -> None:
        raw = {
            "name": "Test", "use_home_location": True,
            "override_policy": {
                "duration_mode": 12345,
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
