"""OverridePolicyConfig — global Manual Override policy configuration.

v1.2.0-beta.1, T7 (initial), T10 (release-strategy architecture). Mirrors
the existing ComfortConfig pattern: one ConfigEntry-level dataclass, stored/
restored via config_entry_data.py, resolved once into SmartShadingCoordinator
constructor kwargs.

release_strategy defaults to LIFECYCLE, which reproduces T7's default
break_on_lifecycle=True behavior exactly — a fresh ConfigEntry (or one
missing these keys entirely) behaves identically to a pre-T10 installation
that never touched the Manual Override step. See config_entry_data.py's
`_override_policy_from_storage()` for the full migration from T7's
duration_mode/break_on_lifecycle pair.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from .manual_override import OverrideReleaseStrategy


@dataclass(frozen=True)
class OverridePolicyConfig:
    release_strategy: OverrideReleaseStrategy = OverrideReleaseStrategy.LIFECYCLE
    fixed_until: time | None = None
    allow_comfort_actions: bool = False
    allow_protection_actions: bool = False
    duration_min: int = 120
    night_duration_min: int = 720
    detection_tolerance: int = 10
    # Whether duration_min/night_duration_min also apply as a defensive
    # maximum for LIFECYCLE / FIRST_COMFORT / FIRST_PROTECTION /
    # FIRST_ANY_DECISION / MANUAL (T10) — irrelevant for DURATION (which
    # always uses them as the actual duration) and FIXED_TIME (which uses
    # fixed_until instead). See engines/override_release.py.
    safety_timeout_enabled: bool = True
