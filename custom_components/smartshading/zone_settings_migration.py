"""Materialization of the System-derived zone settings into explicit zone data (PA-2b).

Two zone settings may defer to the legacy System entry: the Manual Override policy
(``resolve_zone_override_policy``) and the absence delay
(``resolve_zone_absence_delay_min``).  Materializing them means writing the value a zone
EFFECTIVELY runs with into the zone's own stored data and switching its inheritance flags
off, so the zone no longer depends on the System entry.

Rules (all enforced here):

* the effective values are computed with the existing resolvers - never with a typed
  loader round trip - and must be identical before and after (checked against the System
  data and against no System data at all);
* historical values that cannot be verified are never replaced by defaults: the zone is
  left unmigrated (``ZoneMigrationError``); a zone that merely cannot be judged yet
  (it defers to a System entry that does not exist) is ``ZoneMigrationDeferred``;
* the data and ``minor_version`` are written in ONE ``async_update_entry`` call and the
  persisted result is verified; a failed or unverifiable write is rolled back and the
  zone is never marked migrated;
* nothing here touches the System entry, other entries or any shared runtime state.
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .config_entry_data import (
    _safe_int,
    override_policy_to_storage,
    resolve_zone_absence_delay_min,
    resolve_zone_override_policy,
)

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

TARGET_MINOR_VERSION = 2

_UNPARSABLE = -(10**9)
_OWN_KEYS = ("override_policy", "absence_delay_min", "absence_delay_use_system_default")


class ZoneMigrationError(Exception):
    """A zone's settings could not be materialized; the zone stays unmigrated."""


class ZoneMigrationPersistenceError(ZoneMigrationError):
    """The verified settings could not be persisted (the write failed or did not verify);
    the zone was rolled back to exactly what it was."""


class ZoneMigrationDeferred(ZoneMigrationError):
    """Not an error: the zone cannot be judged yet (its System defaults are not available)."""


def _check_flag(value: Any, what: str) -> None:
    if not isinstance(value, bool):
        raise ZoneMigrationError(f"{what} is {value!r}, not a boolean")


def _check_delay(value: Any, what: str) -> None:
    """None/missing is the documented default; anything else must parse like the resolver does."""
    if value is None:
        return
    parsed = _safe_int(value, _UNPARSABLE)
    if parsed == _UNPARSABLE:
        raise ZoneMigrationError(f"{what} is {value!r}, not a number")
    if parsed < 0:
        raise ZoneMigrationError(f"{what} is negative ({parsed})")


def _defers(zone_raw: Mapping[str, Any]) -> tuple[bool, bool]:
    """(override policy defers, absence delay defers) - exactly as the resolvers decide."""
    stored = zone_raw.get("override_policy") or {}
    return (
        bool(stored.get("use_system_default", "release_strategy" not in stored)),
        bool(zone_raw.get("absence_delay_use_system_default", False)),
    )


def validate_legacy_zone_settings(
    zone_raw: Mapping[str, Any], system_raw: Mapping[str, Any] | None
) -> None:
    """Raise unless the zone's effective values are well defined and verifiable."""
    policy = zone_raw.get("override_policy")
    if policy is not None and not isinstance(policy, Mapping):
        raise ZoneMigrationError(f"override_policy is {type(policy).__name__}, not a mapping")
    if isinstance(policy, Mapping) and "use_system_default" in policy:
        _check_flag(policy["use_system_default"], "override_policy.use_system_default")
    if "absence_delay_use_system_default" in zone_raw:
        _check_flag(zone_raw["absence_delay_use_system_default"], "absence_delay_use_system_default")
    _check_delay(zone_raw.get("absence_delay_min"), "absence_delay_min")

    defers_policy, defers_delay = _defers(zone_raw)
    if not (defers_policy or defers_delay):
        return
    if system_raw is None:
        raise ZoneMigrationDeferred("the zone defers to the System entry, which does not exist")
    if defers_policy:
        system_policy = system_raw.get("system_override_policy")
        if system_policy is not None and not isinstance(system_policy, Mapping):
            raise ZoneMigrationError(
                f"system_override_policy is {type(system_policy).__name__}, not a mapping")
    if defers_delay:
        _check_delay(system_raw.get("system_absence_delay_min"), "system_absence_delay_min")


def materialize_zone_settings(
    zone_raw: Mapping[str, Any], system_raw: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The zone data with the effective override policy and absence delay made explicit."""
    effective_policy = resolve_zone_override_policy(dict(zone_raw), system_raw)
    effective_delay = resolve_zone_absence_delay_min(dict(zone_raw), system_raw)
    migrated = copy.deepcopy(dict(zone_raw))
    stored = migrated.get("override_policy")
    kept = dict(stored) if isinstance(stored, Mapping) else {}
    migrated["override_policy"] = {
        **kept,                                  # unknown/legacy keys stay (no configuration loss)
        **override_policy_to_storage(effective_policy),
        "use_system_default": False,
    }
    migrated["absence_delay_min"] = effective_delay
    migrated["absence_delay_use_system_default"] = False
    return migrated


def verify_materialization(
    original: Mapping[str, Any],
    system_raw: Mapping[str, Any] | None,
    migrated: Mapping[str, Any],
) -> None:
    """Raise unless ``migrated`` runs exactly like ``original`` and no longer needs the System."""
    expected_policy = resolve_zone_override_policy(dict(original), system_raw)
    expected_delay = resolve_zone_absence_delay_min(dict(original), system_raw)
    for label, system in (("with the System data", system_raw), ("without System data", None)):
        if resolve_zone_override_policy(dict(migrated), system) != expected_policy:
            raise ZoneMigrationError(f"effective override policy differs {label}")
        if resolve_zone_absence_delay_min(dict(migrated), system) != expected_delay:
            raise ZoneMigrationError(f"effective absence delay differs {label}")
    if _defers(migrated) != (False, False):
        raise ZoneMigrationError("the migrated zone still defers to the System entry")
    others = lambda raw: {k: v for k, v in raw.items() if k not in _OWN_KEYS}  # noqa: E731
    if others(migrated) != others(original):
        raise ZoneMigrationError("an unrelated zone setting changed")
    old_policy = original.get("override_policy")
    if isinstance(old_policy, Mapping) and not set(old_policy) <= set(migrated["override_policy"]):
        raise ZoneMigrationError("a stored override_policy key was lost")


def _is_migrated(
    entry: "ConfigEntry", original: Mapping[str, Any], system_raw: Mapping[str, Any] | None
) -> bool:
    if entry.minor_version != TARGET_MINOR_VERSION:
        return False
    try:
        verify_materialization(original, system_raw, dict(entry.data))
    except ZoneMigrationError:
        return False
    return not _depends_on_system(entry, system_raw)


def _restore(
    hass: "HomeAssistant", entry: "ConfigEntry", original: dict[str, Any], minor_version: int
) -> None:
    """Last-resort rollback to the exact pre-migration state."""
    if dict(entry.data) == original and entry.minor_version == minor_version:
        return
    try:
        hass.config_entries.async_update_entry(entry, data=original, minor_version=minor_version)
    except Exception:  # noqa: BLE001 - nothing more can be done; the caller reports the failure
        _LOGGER.error(
            "SmartShading: rolling back the settings migration of %s failed", entry.entry_id,
            exc_info=True)


def _depends_on_system(entry: "ConfigEntry", system_raw: Mapping[str, Any] | None) -> bool:
    # Local import: system_retirement builds on this module's TARGET_MINOR_VERSION.
    from .system_retirement import zone_independence_reasons

    return bool(zone_independence_reasons(entry.data, entry.minor_version, system_raw))


def migrate_zone_entry(
    hass: "HomeAssistant", entry: "ConfigEntry", system_raw: Mapping[str, Any] | None
) -> bool:
    """Materialize one zone entry.  True if it was written, False if it was already independent.

    Raises ZoneMigrationError (ZoneMigrationDeferred when the zone cannot be judged yet);
    in every failure case the entry is left exactly as it was.
    """
    if entry.minor_version > TARGET_MINOR_VERSION:
        return False  # written by a newer version: never touched here (and never judged independent)
    if entry.minor_version == TARGET_MINOR_VERSION and not _depends_on_system(entry, system_raw):
        return False
    # A v1 entry is migrated; a v2 entry that depends on the System again (re-enabled
    # inheritance, dropped flags) is recovered through the very same verified path.
    original = copy.deepcopy(dict(entry.data))
    original_minor = entry.minor_version
    validate_legacy_zone_settings(original, system_raw)
    candidate = materialize_zone_settings(original, system_raw)
    verify_materialization(original, system_raw, candidate)
    try:
        # ONE call: the data and the version change together or not at all.
        hass.config_entries.async_update_entry(
            entry, data=candidate, minor_version=TARGET_MINOR_VERSION)
    except Exception as err:  # noqa: BLE001 - only this call; the state is judged below
        if _is_migrated(entry, original, system_raw):
            _LOGGER.warning(
                "SmartShading: %s was migrated although the update reported %r", entry.entry_id, err)
            return True
        _restore(hass, entry, original, original_minor)
        raise ZoneMigrationPersistenceError(f"writing the migrated settings failed: {err!r}") from err
    if not _is_migrated(entry, original, system_raw):
        _restore(hass, entry, original, original_minor)
        raise ZoneMigrationPersistenceError("the persisted settings did not verify; rolled back")
    return True
