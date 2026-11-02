"""Home Assistant side of cover ownership: claims from the stored config
entries, the integration-wide registry, and the Repairs issues.

The decision logic is the pure ``engines.cover_ownership``; this module only
feeds it the configuration and mirrors its result into Home Assistant.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import CONF_ENTRY_TYPE, DATA_COVER_OWNERSHIP, DOMAIN, ENTRY_TYPE_ZONE
from .engines.cover_ownership import (
    CoverClaim,
    CoverOwnership,
    CoverOwnershipRegistry,
    KIND_CONFLICT,
    KIND_IDENTICAL,
    build_claims,
    normalize_cover_id,
    rewrite_cover_ids,
)

if TYPE_CHECKING:  # the Home Assistant helpers are imported lazily where needed
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

ISSUE_PREFIX_CONFLICT = "duplicate_cover_"
ISSUE_PREFIX_IDENTICAL = "duplicate_cover_identical_"


def get_registry(hass: "HomeAssistant") -> CoverOwnershipRegistry:
    domain_data = hass.data.setdefault(DOMAIN, {})
    registry = domain_data.get(DATA_COVER_OWNERSHIP)
    if registry is None:
        registry = domain_data[DATA_COVER_OWNERSHIP] = CoverOwnershipRegistry()
    return registry


def current_claims(hass: "HomeAssistant", *, exclude_entry_id: str | None = None) -> list[CoverClaim]:
    """Claims of every enabled zone entry, loaded or not: a conflict is a
    property of the configuration, so it does not flap while an entry reloads."""
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id or entry.disabled_by is not None:
            continue
        if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) != ENTRY_TYPE_ZONE:
            continue
        entries.append((entry.entry_id, entry.title, entry.data))
    return build_claims(entries)


def _issue_id(own: CoverOwnership) -> str:
    base = own.cover_id.replace(".", "_")
    return (ISSUE_PREFIX_IDENTICAL if own.kind == KIND_IDENTICAL else ISSUE_PREFIX_CONFLICT) + base


def _locations(own: CoverOwnership) -> str:
    labels: list[str] = []
    for party in own.parties:
        claim = next(c for c in own.claims if c.key == party)
        label = claim.label()
        if label not in labels:
            labels.append(label)
    return "; ".join(labels)


def _sync_issues(hass: "HomeAssistant", registry: CoverOwnershipRegistry) -> None:
    from homeassistant.helpers import issue_registry as ir

    wanted: dict[str, tuple[str, "ir.IssueSeverity", CoverOwnership]] = {}
    for own in registry.result.duplicates():
        if own.kind == KIND_CONFLICT:
            key = (
                "duplicate_cover_conflict_no_safety"
                if not own.safety_ok else "duplicate_cover_conflict"
            )
            wanted[_issue_id(own)] = (key, ir.IssueSeverity.ERROR, own)
        else:
            wanted[_issue_id(own)] = ("duplicate_cover_identical", ir.IssueSeverity.WARNING, own)

    for issue_id, (key, severity, own) in wanted.items():
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False, is_persistent=False, severity=severity,
            translation_key=key,
            translation_placeholders={
                "cover": own.cover_id,
                "locations": _locations(own),
                "count": str(len(own.parties)),
            },
        )
    issue_reg = ir.async_get(hass)
    for (domain, issue_id) in list(issue_reg.issues):
        if domain == DOMAIN and issue_id.startswith(ISSUE_PREFIX_CONFLICT) and issue_id not in wanted:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def async_sync_cover_ownership(
    hass: "HomeAssistant", *, exclude_entry_id: str | None = None
) -> CoverOwnershipRegistry:
    """Recompute ownership from the stored configuration, notify affected
    coordinators and bring the Repairs issues in line.  Synchronous within the
    event loop, so concurrent setups see one consistent result."""
    registry = get_registry(hass)
    registry.update(current_claims(hass, exclude_entry_id=exclude_entry_id))
    try:
        _sync_issues(hass, registry)
    except Exception:  # Repairs must never break setup
        _LOGGER.warning("SmartShading: could not update cover ownership repairs", exc_info=True)
    return registry


# ---------------------------------------------------------------------------
# Entity registry renames (A6)
# ---------------------------------------------------------------------------

DATA_COVER_RENAME_UNSUB = "cover_rename_unsub"
DATA_COVER_RENAME_STATS = "cover_rename_stats"


def get_rename_stats(hass: "HomeAssistant") -> dict:
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        DATA_COVER_RENAME_STATS,
        {"handled": 0, "entries_updated": 0, "reloads_scheduled": 0, "plans_invalidated": 0})


def async_handle_cover_rename(hass: "HomeAssistant", old_id: str, new_id: str) -> list[str]:
    """A cover entity was renamed in the entity registry: follow it.

    The stored configuration keeps the old id otherwise, which would silently
    leave the zone commanding an entity that no longer exists.  For every zone
    entry that lists the cover: plans built under the old identity end first
    (before anything is rewritten), the stored ids are replaced by Home
    Assistant's own config entry update, the ownership (and its Repairs) is
    recomputed from the new configuration, and the entry is reloaded so it
    starts from fresh state.  Returns the ids of the entries that were updated."""
    old, new = normalize_cover_id(old_id), normalize_cover_id(new_id)
    stats = get_rename_stats(hass)
    stats["handled"] += 1
    affected: list[str] = []
    for entry in list(hass.config_entries.async_entries(DOMAIN)):
        if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) != ENTRY_TYPE_ZONE:
            continue
        new_data, replaced = rewrite_cover_ids(entry.data, {old: new})
        if not replaced:
            continue
        coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
        if coordinator is not None:
            stats["plans_invalidated"] += coordinator.async_invalidate_for_cover_identity_change(old, new)
        hass.config_entries.async_update_entry(entry, data=new_data)
        affected.append(entry.entry_id)
    if not affected:
        return affected
    stats["entries_updated"] += len(affected)
    async_sync_cover_ownership(hass)
    from homeassistant.config_entries import ConfigEntryState

    for entry_id in affected:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.state != ConfigEntryState.LOADED:
            continue
        schedule = getattr(hass.config_entries, "async_schedule_reload", None)
        if schedule is not None:
            schedule(entry_id)
        else:  # older Home Assistant
            hass.async_create_task(hass.config_entries.async_reload(entry_id))
        stats["reloads_scheduled"] += 1
    return affected


def async_setup_cover_rename_listener(hass: "HomeAssistant") -> None:
    """Follow cover renames of the entity registry.  One listener per Home
    Assistant instance, however many zone entries exist; idempotent."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_COVER_RENAME_UNSUB) is not None:
        return
    from homeassistant.core import callback
    from homeassistant.helpers import entity_registry as er

    @callback
    def _on_registry_updated(event) -> None:
        data = event.data
        if data.get("action") != "update":
            return
        old_id, new_id = data.get("old_entity_id"), data.get("entity_id")
        if not old_id or not new_id or old_id == new_id:
            return
        if not str(old_id).startswith("cover.") or not str(new_id).startswith("cover."):
            return
        try:
            async_handle_cover_rename(hass, old_id, new_id)
        except Exception:  # a rename must never break the event loop
            _LOGGER.warning("SmartShading: could not follow the rename of %s", old_id, exc_info=True)

    domain_data[DATA_COVER_RENAME_UNSUB] = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, _on_registry_updated)


def async_teardown_cover_rename_listener(hass: "HomeAssistant", unloading_entry_id: str | None = None) -> None:
    """Remove the rename listener once no zone entry other than the one that is
    unloading remains loaded.  Idempotent."""
    domain_data = hass.data.get(DOMAIN, {})
    unsub = domain_data.get(DATA_COVER_RENAME_UNSUB)
    if unsub is None:
        return
    from homeassistant.config_entries import ConfigEntryState

    if any(
        e.entry_id != unloading_entry_id
        and e.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) == ENTRY_TYPE_ZONE
        and e.state is ConfigEntryState.LOADED
        for e in hass.config_entries.async_entries(DOMAIN)
    ):
        return
    domain_data.pop(DATA_COVER_RENAME_UNSUB, None)
    unsub()
