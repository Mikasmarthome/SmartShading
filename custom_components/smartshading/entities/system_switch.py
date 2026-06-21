"""System-level switch entities for the SmartShading System entry (Step 10B).

DebugLoggingSwitch
------------------
A single integration-wide diagnostic switch that enables verbose debug logging.

When on:  Coordinator cycles log evaluator decisions, recommended targets,
          dispatched cover commands, guard blocks, harmonization results, and
          learning event summaries.  The export button logs export outcomes.
When off: Normal logging only (warnings + errors).

Privacy constraints
-------------------
Debug output must never include: raw payloads, local file paths, IP addresses,
person/device entity names, full entity lists, or raw learning history.

Safe to log: zone/window IDs (pseudonyms), evaluator names, HA position
values, state/reason names, booleans, HA-semantic dispatch targets.

Persistence
-----------
State is persisted in config_entry.options[CONF_DEBUG_LOGGING] under the
SmartShading System entry so it survives HA restarts.  The value is also
propagated to hass.data[DOMAIN][DATA_DEBUG_LOGGING] for fast per-cycle
reads in zone coordinators.

UI placement
------------
Visible only under the SmartShading System device (system config entry).
EntityCategory.DIAGNOSTIC: appears in the Diagnostic section of the device page.
Enabled by default so the switch is immediately visible under Diagnostics;
the switch state itself defaults to off (no debug output until explicitly enabled).
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..const import (
    CONF_DEBUG_LOGGING,
    DATA_DEBUG_LOGGING,
    DOMAIN,
    SYSTEM_DEVICE_IDENTIFIER,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up DebugLoggingSwitch for the SmartShading System entry."""
    async_add_entities([DebugLoggingSwitch(hass, entry)])


class DebugLoggingSwitch(SwitchEntity):
    """Debug Logging switch for the SmartShading System entry.

    Controls integration-wide verbose debug logging.  Switch state defaults to
    off (no extra log output); entity is enabled by default so it appears under
    the SmartShading System Diagnostics section without manual activation.
    Persisted in config_entry.options so the state survives HA restarts.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "debug_logging"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bug-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_debug_logging"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)},
        )

    @property
    def is_on(self) -> bool:
        """Return True when debug logging is enabled."""
        return self._entry.options.get(CONF_DEBUG_LOGGING, False)

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_debug_logging(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_debug_logging(False)

    async def _set_debug_logging(self, enabled: bool) -> None:
        new_options = {**self._entry.options, CONF_DEBUG_LOGGING: enabled}
        self._hass.config_entries.async_update_entry(self._entry, options=new_options)
        # Propagate immediately so zone coordinators pick it up next cycle
        # without waiting for the system entry to be reloaded.
        self._hass.data.setdefault(DOMAIN, {})[DATA_DEBUG_LOGGING] = enabled
        self.async_write_ha_state()
