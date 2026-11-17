"""System-entry retirement eligibility (PA-2c.1): a pure, side-effect-free check.

The legacy System ConfigEntry may only ever be retired when NO zone depends on it any
more.  This module answers that question from the stored data alone; it never writes,
never touches the System entry and never raises for malformed stored values (every
unreadable or unverifiable state is a *reason* that keeps the gate closed).

A zone counts as independent only when ALL of the following hold:

* its data and ``minor_version`` are readable and migrated (``minor_version`` alone is
  never enough - it is one condition among the content checks);
* the override policy is an explicit mapping with a valid ``release_strategy`` (a policy
  without one is the implicit "never configured" sentinel that defers to the System),
  an explicit boolean ``use_system_default`` that is ``False`` and its own valid
  ``detection_tolerance``;
* the absence delay is an explicit non-negative integer and
  ``absence_delay_use_system_default`` is an explicit ``False``;
* the effective override policy and absence delay are identical whatever System data
  exists (the actual one, none at all, and a deliberately different probe), and the
  effective tolerance is the zone's own.

Reasons are short stable codes (no values, no entity names, no user data).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config_entry_data import (
    _override_policy_from_storage,
    resolve_zone_absence_delay_min,
    resolve_zone_override_policy,
)
from .models.manual_override import OverrideReleaseStrategy
from .zone_settings_migration import TARGET_MINOR_VERSION

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.config_entries import ConfigEntry

# Reason codes.
DATA_UNREADABLE = "data_unreadable"
MINOR_VERSION_INVALID = "minor_version_invalid"
MINOR_VERSION_UNMIGRATED = "minor_version_unmigrated"
MINOR_VERSION_UNKNOWN = "minor_version_unknown"
OVERRIDE_POLICY_MISSING = "override_policy_missing"
OVERRIDE_POLICY_MALFORMED = "override_policy_malformed"
OVERRIDE_POLICY_IMPLICIT_INHERITANCE = "override_policy_implicit_inheritance"
OVERRIDE_POLICY_INVALID = "override_policy_invalid"
OVERRIDE_FLAG_MISSING = "override_flag_missing"
OVERRIDE_FLAG_INVALID = "override_flag_invalid"
OVERRIDE_INHERITANCE_ACTIVE = "override_inheritance_active"
DETECTION_TOLERANCE_INVALID = "detection_tolerance_invalid"
ABSENCE_DELAY_INVALID = "absence_delay_invalid"
ABSENCE_FLAG_MISSING = "absence_flag_missing"
ABSENCE_FLAG_INVALID = "absence_flag_invalid"
ABSENCE_INHERITANCE_ACTIVE = "absence_inheritance_active"
OVERRIDE_EFFECTIVE_DEPENDS_ON_SYSTEM = "override_effective_depends_on_system"
ABSENCE_EFFECTIVE_DEPENDS_ON_SYSTEM = "absence_effective_depends_on_system"

# A System data set that differs from every plausible real one in each inherited field, so
# a zone that still reads from the System cannot look independent by coincidence.
_PROBE_SYSTEM: dict[str, Any] = {
    "system_override_policy": {
        "release_strategy": "fixed_time", "fixed_until": "03:17",
        "allow_comfort_actions": True, "allow_protection_actions": True,
        "duration_min": 7, "night_duration_min": 11,
        "detection_tolerance": 97, "safety_timeout_enabled": False,
    },
    "system_absence_delay_min": 1439,
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _override_reasons(zone_raw: Mapping[str, Any]) -> tuple[list[str], bool]:
    """(reasons, policy well formed enough to resolve)."""
    policy = zone_raw.get("override_policy")
    if policy is None:
        return [OVERRIDE_POLICY_MISSING], False
    if not isinstance(policy, Mapping):
        return [OVERRIDE_POLICY_MALFORMED], False
    reasons: list[str] = []
    strategy = policy.get("release_strategy")
    if "release_strategy" not in policy:
        reasons.append(OVERRIDE_POLICY_IMPLICIT_INHERITANCE)
    elif strategy not in {s.value for s in OverrideReleaseStrategy}:
        reasons.append(OVERRIDE_POLICY_INVALID)
    if "use_system_default" not in policy:
        reasons.append(OVERRIDE_FLAG_MISSING)
    elif not isinstance(policy["use_system_default"], bool):
        reasons.append(OVERRIDE_FLAG_INVALID)
    elif policy["use_system_default"]:
        reasons.append(OVERRIDE_INHERITANCE_ACTIVE)
    tolerance = policy.get("detection_tolerance")
    if not _is_int(tolerance) or tolerance < 1:
        reasons.append(DETECTION_TOLERANCE_INVALID)
    return reasons, True


def _absence_reasons(zone_raw: Mapping[str, Any]) -> tuple[list[str], bool]:
    reasons: list[str] = []
    delay = zone_raw.get("absence_delay_min")
    delay_ok = _is_int(delay) and delay >= 0
    if not delay_ok:
        reasons.append(ABSENCE_DELAY_INVALID)
    flag_ok = False
    if "absence_delay_use_system_default" not in zone_raw:
        reasons.append(ABSENCE_FLAG_MISSING)
    elif not isinstance(zone_raw["absence_delay_use_system_default"], bool):
        reasons.append(ABSENCE_FLAG_INVALID)
    elif zone_raw["absence_delay_use_system_default"]:
        reasons.append(ABSENCE_INHERITANCE_ACTIVE)
    else:
        flag_ok = True
    return reasons, delay_ok and flag_ok


def zone_independence_reasons(
    zone_raw: Any, minor_version: Any, system_raw: Mapping[str, Any] | None
) -> tuple[str, ...]:
    """Why this zone still (or possibly) depends on the System entry; () = independent."""
    if not isinstance(zone_raw, Mapping):
        return (DATA_UNREADABLE,)
    reasons: list[str] = []
    if not _is_int(minor_version):
        reasons.append(MINOR_VERSION_INVALID)
    elif minor_version < TARGET_MINOR_VERSION:
        reasons.append(MINOR_VERSION_UNMIGRATED)
    elif minor_version > TARGET_MINOR_VERSION:
        reasons.append(MINOR_VERSION_UNKNOWN)

    override, override_resolvable = _override_reasons(zone_raw)
    absence, absence_clean = _absence_reasons(zone_raw)
    reasons += override + absence

    # Effective independence, judged by the very resolvers the runtime uses.
    zone = dict(zone_raw)
    systems = (system_raw if isinstance(system_raw, Mapping) else None, None, _PROBE_SYSTEM)
    if override_resolvable and not override:
        own = _override_policy_from_storage(zone["override_policy"])
        if any(resolve_zone_override_policy(zone, s) != own for s in systems):
            reasons.append(OVERRIDE_EFFECTIVE_DEPENDS_ON_SYSTEM)
    if absence_clean:
        if any(resolve_zone_absence_delay_min(zone, s) != zone["absence_delay_min"] for s in systems):
            reasons.append(ABSENCE_EFFECTIVE_DEPENDS_ON_SYSTEM)
    return tuple(reasons)


@dataclass(frozen=True)
class ZoneRetirementStatus:
    entry_id: str
    enabled: bool
    minor_version: Any
    reasons: tuple[str, ...]

    @property
    def independent(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class RetirementAssessment:
    zones: tuple[ZoneRetirementStatus, ...]
    system_entry_present: bool

    @property
    def blocked(self) -> tuple[ZoneRetirementStatus, ...]:
        return tuple(z for z in self.zones if not z.independent)

    @property
    def eligible(self) -> bool:
        """True only if every zone (disabled ones included) is verifiably independent."""
        return not self.blocked

    def summary(self) -> dict[str, Any]:
        """Non-sensitive, serializable view (entry ids and reason codes only)."""
        return {
            "eligible": self.eligible,
            "zones_checked": len(self.zones),
            "system_entry_present": self.system_entry_present,
            "blocked": {z.entry_id: list(z.reasons) for z in self.blocked},
        }


def assess_system_retirement(
    zone_entries: Iterable["ConfigEntry"], system_raw: Mapping[str, Any] | None
) -> RetirementAssessment:
    """Assess ALL given zone entries (callers pass every zone, disabled included)."""
    zones = tuple(
        ZoneRetirementStatus(
            entry_id=entry.entry_id,
            enabled=entry.disabled_by is None,
            minor_version=entry.minor_version,
            reasons=zone_independence_reasons(entry.data, entry.minor_version, system_raw),
        )
        for entry in zone_entries
    )
    return RetirementAssessment(zones=zones, system_entry_present=system_raw is not None)
