"""Safe retirement of the legacy System ConfigEntry (PA-2c.2).

Once EVERY zone entry (disabled ones included) is verifiably independent of the System
entry (see ``system_retirement``) the System entry is redundant and is removed through
the supported ``ConfigEntries.async_remove`` API.  Nothing else is touched: no zone entry,
no zone data, no storage; Home Assistant itself removes the System entry's own entities
and device.

Safety design
-------------
* A *fresh* assessment judges the stored zone data immediately before the removal, never a
  cached or start-up result, and independence is a property of that stored data (the
  effective values do not depend on the System entry at all), so a concurrent zone setup,
  reload or options save cannot make a verified zone depend on the System entry again.
* The System entry is unloaded first (after any setup in flight finished); the zones are
  assessed again WITHOUT an await and ``async_remove`` is called right away.  An unloaded
  entry is removed without suspending, so no zone change fits between that assessment and
  the removal.  If the assessment no longer passes, nothing is removed and the very same
  entry is set up again.
* One ``asyncio.Lock`` serializes concurrent retirement attempts (idempotent: the second
  finds no System entry).  Home Assistant offers no cross-entry transaction; this module
  never claims one.  A crash between the removal and the delayed storage save simply leaves
  the entry in place, and the next start repeats the (idempotent) retirement.
* Success is reported only after the entry is verifiably gone from the registry.  A failed
  removal keeps the entry, is logged with its traceback and raises a Repairs issue.
* Blocked zones raise one stable, non-sensitive Repairs issue each; resolved issues are
  deleted on the next assessment.  Repairs are informational (not fixable): no automatic
  repair action exists, and no malformed setting is ever changed.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState, OperationNotAllowed, UnknownEntry
from homeassistant.helpers import issue_registry as ir

from .const import (
    DATA_DEBUG_LOGGING,
    DATA_RETIREMENT_LOCK,
    DATA_ZONE_MIGRATION_FAILURES,
    DOMAIN,
)
from .system_retirement import (
    ENTRY_VERSION_UNSUPPORTED,
    MINOR_VERSION_UNKNOWN,
    RetirementAssessment,
)

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Outcomes.
ABSENT = "absent"        # there is no System entry (nothing to do)
BLOCKED = "blocked"      # a zone is not verified independent: the System entry stays
RETIRED = "retired"      # removed, and verified gone
FAILED = "failed"        # removal failed: the System entry stays

# Repairs issues.
ISSUE_ZONE_PREFIX = "system_retirement_zone_"
ISSUE_REMOVAL_FAILED = "system_retirement_removal_failed"
KEY_ZONE_BLOCKED = "system_retirement_zone_blocked"
KEY_ZONE_NOT_SAVED = "system_retirement_zone_not_saved"
KEY_ZONE_UNSUPPORTED = "system_retirement_zone_unsupported_version"
KEY_REMOVAL_FAILED = "system_retirement_removal_failed"

FAILURE_PERSISTENCE = "persistence"
FAILURE_UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class RetirementOps:
    """The integration's own enumeration helpers (reused, not duplicated here)."""

    zone_entries: Callable[["HomeAssistant"], list["ConfigEntry"]]
    system_entries: Callable[["HomeAssistant"], list["ConfigEntry"]]
    assess: Callable[["HomeAssistant"], RetirementAssessment]


def migration_failures(hass: "HomeAssistant") -> dict[str, str]:
    """Transient, per-start record of zones whose last conversion failed (never persisted)."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(DATA_ZONE_MIGRATION_FAILURES, {})


def _zone_issue_key(reasons: tuple[str, ...], failure: str | None) -> str:
    if ENTRY_VERSION_UNSUPPORTED in reasons or MINOR_VERSION_UNKNOWN in reasons:
        return KEY_ZONE_UNSUPPORTED
    if failure == FAILURE_PERSISTENCE:
        return KEY_ZONE_NOT_SAVED
    return KEY_ZONE_BLOCKED


def sync_retirement_issues(
    hass: "HomeAssistant",
    ops: RetirementOps,
    assessment: RetirementAssessment,
    *,
    removal_failed: bool = False,
) -> None:
    """Bring the Repairs issues in line with ``assessment`` (create, update, clear)."""
    titles = {e.entry_id: e.title for e in ops.zone_entries(hass)}
    failures = migration_failures(hass)
    wanted: set[str] = set()
    for zone in assessment.blocked:
        issue_id = ISSUE_ZONE_PREFIX + zone.entry_id
        wanted.add(issue_id)
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
            translation_key=_zone_issue_key(zone.reasons, failures.get(zone.entry_id)),
            translation_placeholders={"zone": titles.get(zone.entry_id) or zone.entry_id},
        )
    if removal_failed:
        wanted.add(ISSUE_REMOVAL_FAILED)
        ir.async_create_issue(
            hass, DOMAIN, ISSUE_REMOVAL_FAILED,
            is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
            translation_key=KEY_REMOVAL_FAILED,
        )
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if (
            domain == DOMAIN
            and (issue_id.startswith(ISSUE_ZONE_PREFIX) or issue_id == ISSUE_REMOVAL_FAILED)
            and issue_id not in wanted
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _gone(hass: "HomeAssistant", entry_id: str) -> bool:
    return hass.config_entries.async_get_entry(entry_id) is None


async def _restore_loaded(hass: "HomeAssistant", system: "ConfigEntry") -> None:
    """A kept System entry that was unloaded for the attempt is set up again."""
    if _gone(hass, system.entry_id) or system.state is not ConfigEntryState.NOT_LOADED:
        return
    try:
        await hass.config_entries.async_setup(system.entry_id)
    except Exception:  # noqa: BLE001 - nothing more can be done; the failure is reported
        _LOGGER.error("SmartShading: setting the kept System entry up again failed", exc_info=True)


async def _remove_system_entry(
    hass: "HomeAssistant", ops: RetirementOps, system: "ConfigEntry"
) -> str:
    entry_id = system.entry_id
    try:
        # async_unload refuses an entry whose setup is still running: wait for it first.
        async with system.setup_lock:
            pass
        try:
            await hass.config_entries.async_unload(entry_id)
        except OperationNotAllowed:
            pass  # not unloadable (e.g. failed unload): removal handles that state itself
        # ---- no await between this assessment and the removal call ---------------
        if not ops.assess(hass).eligible:
            if system.state is ConfigEntryState.NOT_LOADED:
                await hass.config_entries.async_setup(entry_id)
            return BLOCKED
        result = await hass.config_entries.async_remove(entry_id)
    except UnknownEntry:
        return RETIRED if _gone(hass, entry_id) else FAILED   # removed concurrently
    except Exception:  # noqa: BLE001 - the HA API / the entry's own hooks failed: keep and report
        _LOGGER.error(
            "SmartShading: removing the legacy System entry failed; it is kept", exc_info=True)
        await _restore_loaded(hass, system)
        return FAILED
    if not _gone(hass, entry_id):
        _LOGGER.error("SmartShading: the System entry is still registered after its removal")
        return FAILED
    if isinstance(result, dict) and result.get("require_restart"):
        _LOGGER.warning(
            "SmartShading: the System entry was removed, but it could not be unloaded cleanly; "
            "a restart of Home Assistant completes the clean-up")
    return RETIRED


async def async_retire_system_entry(hass: "HomeAssistant", ops: RetirementOps) -> str:
    """Retire the System entry if (and only if) every zone is verified independent."""
    lock = hass.data.setdefault(DOMAIN, {}).setdefault(DATA_RETIREMENT_LOCK, asyncio.Lock())
    async with lock:
        assessment = ops.assess(hass)
        systems = ops.system_entries(hass)
        if not systems:
            sync_retirement_issues(hass, ops, assessment)
            return ABSENT
        if not assessment.eligible:
            sync_retirement_issues(hass, ops, assessment)
            _LOGGER.info(
                "SmartShading: System entry kept, zones still depending on it: %s",
                assessment.summary()["blocked"])
            return BLOCKED
        outcome = RETIRED
        for system in systems:
            outcome = await _remove_system_entry(hass, ops, system)
            if outcome != RETIRED:
                break
        sync_retirement_issues(hass, ops, ops.assess(hass), removal_failed=outcome == FAILED)
        if outcome == RETIRED:
            # The persisted debug option went with the entry: the runtime flag follows it.
            hass.data[DOMAIN][DATA_DEBUG_LOGGING] = False
            _LOGGER.info("SmartShading: the legacy System entry was retired")
        return outcome
