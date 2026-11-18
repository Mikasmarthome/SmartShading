"""Removal of the registry rows the legacy System entry left behind (PA-4).

Older versions registered three entities (two export buttons, a debug-logging switch) and
one device for the System ConfigEntry.  That entry no longer forwards any platform, so these
rows can never come alive again; they only show up as unavailable entities and an empty
"SmartShading System" device.

Home Assistant removes them together with the System entry (``async_remove``).  Two cases
need this module:

* the System entry is kept because a zone still depends on it (it may stay for a long time);
* a removal was interrupted: the entry is already gone from the stored config entries but the
  entity registry (saved separately, delayed) still lists its rows.  Home Assistant's own
  start-up clean-up only drops orphaned *devices*, never orphaned entities.

Only the supported registry APIs are used and only rows that are unmistakably legacy System
rows are removed: platform ``smartshading``, one of the three known (domain, unique id) pairs
built from the very ``config_entry_id`` the row belongs to, and a config entry that is a System
entry or no longer exists.  Anything that belongs to a zone entry, to another integration, or
to no config entry at all is never touched, and zone entities are never matched.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SYSTEM, SYSTEM_DEVICE_IDENTIFIER

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# (entity domain, unique id suffix) of the entities the System entry used to own; the full
# unique id was f"{system_entry_id}{suffix}".
_LEGACY_ENTITIES: frozenset[tuple[str, str]] = frozenset({
    ("button", "_system_export_support_data"),
    ("button", "_system_export_research_data"),
    ("switch", "_debug_logging"),
})


def _is_legacy_system_row(hass: "HomeAssistant", row: er.RegistryEntry) -> bool:
    if row.platform != DOMAIN or not row.config_entry_id:
        return False
    owner = hass.config_entries.async_get_entry(row.config_entry_id)
    if owner is not None and owner.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SYSTEM:
        return False                       # a zone entry's (or foreign) row: never ours to drop
    return any(
        row.domain == domain and row.unique_id == f"{row.config_entry_id}{suffix}"
        for domain, suffix in _LEGACY_ENTITIES
    )


def async_remove_legacy_system_registry_rows(hass: "HomeAssistant") -> tuple[int, int]:
    """Remove the legacy System entities and, once empty, the legacy System device.

    Returns (entities removed, devices removed).  Synchronous (registry calls only) and
    idempotent: a second call finds nothing.  Never raises for an absent row.
    """
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    removed_entities = 0
    for row in [r for r in entity_registry.entities.values() if _is_legacy_system_row(hass, r)]:
        entity_registry.async_remove(row.entity_id)
        removed_entities += 1

    removed_devices = 0
    device = device_registry.async_get_device(identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)})
    if device is not None and not er.async_entries_for_device(
        entity_registry, device.id, include_disabled_entities=True
    ):
        device_registry.async_remove_device(device.id)
        removed_devices = 1

    if removed_entities or removed_devices:
        _LOGGER.info(
            "SmartShading: removed %d legacy System entity row(s) and %d legacy System device row(s)",
            removed_entities, removed_devices)
    return removed_entities, removed_devices
