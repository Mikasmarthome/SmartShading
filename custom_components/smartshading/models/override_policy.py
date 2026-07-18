"""OverridePolicyConfig — global Manual Override policy configuration.

v1.2.0-beta.1, T7. Mirrors the existing ComfortConfig pattern: one
ConfigEntry-level dataclass, stored/restored via config_entry_data.py,
resolved once into SmartShadingCoordinator constructor kwargs.

All fields default to exactly the pre-T7 legacy behavior — a fresh
ConfigEntry (or one missing these keys entirely) behaves identically to
before T7 (see config_entry_data.py's `_override_policy_from_storage()`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from .manual_override import OverrideDurationMode


@dataclass(frozen=True)
class OverridePolicyConfig:
    duration_mode: OverrideDurationMode = OverrideDurationMode.LEGACY
    fixed_until: time | None = None
    allow_comfort_actions: bool = False
    allow_protection_actions: bool = False
    duration_min: int = 120
    night_duration_min: int = 720
    detection_tolerance: int = 10
    break_on_lifecycle: bool = True
