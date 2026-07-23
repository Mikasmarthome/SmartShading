"""Home Assistant services for SmartShading (v1.2.0-beta.1, T10.1).

Registers `smartshading.clear_manual_override` — the user-facing entry
point for ending an active Manual Override on demand. This is the missing
piece flagged at the end of T10: SmartShadingCoordinator.
async_clear_manual_override() already exists and is fully functional, but
had no way to be triggered from Home Assistant itself (dashboard button
press, or an automation).

Works for a Manual Override under ANY release strategy, not only MANUAL —
there is no good reason to restrict it, and a user may reasonably want to
end e.g. a long DURATION or LIFECYCLE override early.

Target resolution: standard Home Assistant entity/device/area targeting
(`cv.make_entity_service_schema`), resolved against each window's "Manual
Override Active" binary sensor — the one entity that already uniquely and
stably identifies a window (see entities/binary_sensor.py
SmartShadingOverrideActiveBinarySensor, entities/base.py
SmartShadingWindowEntity for the unique_id format this module parses
against). This lets a user target a window via its own entity picker, its
zone device (clearing every window in that zone), or an area — all without
ever typing an internal window id. No new entity is created for this.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_extract_referenced_entity_ids

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE

_LOGGER = logging.getLogger(__name__)

SERVICE_CLEAR_MANUAL_OVERRIDE = "clear_manual_override"

# The unique_id suffix SmartShadingOverrideActiveBinarySensor is constructed
# with (entities/base.py: f"{entry_id}_{window_id}_{entity_key}") — used to
# recognize which of a target's resolved entities are override-active
# sensors, and to recover which window each one belongs to.
_OVERRIDE_ACTIVE_ENTITY_KEY = "override_active"

_CLEAR_MANUAL_OVERRIDE_SCHEMA = vol.Schema(cv.make_entity_service_schema({}))


def _iter_zone_coordinators(hass: HomeAssistant):
    """Yield (config_entry, coordinator) for every loaded SmartShading zone entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) != ENTRY_TYPE_ZONE:
            continue
        coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
        if coordinator is not None:
            yield entry, coordinator


def _resolve_window_for_entity(hass: HomeAssistant, entity_id: str):
    """Return (coordinator, window_id) for a target entity_id, or None.

    Only entity_ids whose registry unique_id matches a live window's
    "Manual Override Active" sensor resolve to anything — an entity_id
    belonging to a different integration, a different SmartShading entity,
    or a removed/unloaded window is simply not a match (never raises).
    """
    entity_entry = er.async_get(hass).async_get(entity_id)
    if entity_entry is None or not entity_entry.unique_id:
        return None
    for entry, coordinator in _iter_zone_coordinators(hass):
        for window_id in coordinator.windows:
            if entity_entry.unique_id == f"{entry.entry_id}_{window_id}_{_OVERRIDE_ACTIVE_ENTITY_KEY}":
                return coordinator, window_id
    return None


async def _async_handle_clear_manual_override(hass: HomeAssistant, call: ServiceCall) -> None:
    selected = async_extract_referenced_entity_ids(hass, call)
    entity_ids = sorted(set(selected.referenced) | set(selected.indirectly_referenced))
    if not entity_ids:
        raise ServiceValidationError(
            "No target was selected. Choose the window(s), zone device(s), or "
            "area to clear a manual override for."
        )

    # Deduplicated (window_id, coordinator) pairs — a device/area target can
    # resolve to multiple entities of the SAME window (e.g. other platforms
    # sharing the zone device do not match _resolve_window_for_entity, but a
    # future additional per-window entity might), so this stays a set keyed
    # by window_id, never by entity_id count.
    resolved: dict[str, tuple] = {}
    unmatched: list[str] = []
    for entity_id in entity_ids:
        match = _resolve_window_for_entity(hass, entity_id)
        if match is None:
            unmatched.append(entity_id)
            continue
        coordinator, window_id = match
        resolved[f"{id(coordinator)}:{window_id}"] = (coordinator, window_id)

    if not resolved:
        raise ServiceValidationError(
            "None of the targeted entities belong to a SmartShading window. "
            f"Checked: {', '.join(entity_ids)}"
        )
    if unmatched:
        _LOGGER.warning(
            "SmartShading: clear_manual_override: ignoring %d targeted "
            "entity/entities that do not belong to a SmartShading window: %s",
            len(unmatched), ", ".join(unmatched),
        )

    cleared = 0
    for coordinator, window_id in resolved.values():
        try:
            if await coordinator.async_clear_manual_override(window_id):
                cleared += 1
        except Exception:
            _LOGGER.exception(
                "SmartShading: clear_manual_override service failed for window %s",
                window_id,
            )
    _LOGGER.info(
        "SmartShading: clear_manual_override service cleared %d/%d targeted window(s)",
        cleared, len(resolved),
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register SmartShading's services, once per Home Assistant instance.

    Called from every zone entry's setup (async_setup_entry runs once PER
    config entry, not once globally) — the has_service guard makes repeat
    calls for a second/third zone entry a no-op, so exactly one registration
    survives regardless of how many zone entries exist.
    """
    if hass.services.has_service(DOMAIN, SERVICE_CLEAR_MANUAL_OVERRIDE):
        return

    async def _handle_clear_manual_override(call: ServiceCall) -> None:
        await _async_handle_clear_manual_override(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_MANUAL_OVERRIDE,
        _handle_clear_manual_override,
        schema=_CLEAR_MANUAL_OVERRIDE_SCHEMA,
    )


@callback
def async_unload_services_if_no_zone_entries_remain(
    hass: HomeAssistant, unloading_entry_id: str
) -> None:
    """Remove SmartShading's services once no zone entry remains loaded.

    Called from async_unload_entry, itself called while `unloading_entry_id`
    is still technically present in hass.config_entries (its own state
    transition happens after async_unload_entry returns) — so that entry is
    excluded explicitly rather than relied upon to already be gone.
    """
    from homeassistant.config_entries import ConfigEntryState

    remaining = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != unloading_entry_id
        and entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) == ENTRY_TYPE_ZONE
        and entry.state == ConfigEntryState.LOADED
    ]
    if remaining:
        return
    if hass.services.has_service(DOMAIN, SERVICE_CLEAR_MANUAL_OVERRIDE):
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_MANUAL_OVERRIDE)
