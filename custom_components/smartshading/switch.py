"""Platform entry point for the switch platform.

Dispatches to the correct implementation based on entry type:
  - System entry → DebugLoggingSwitch (entities/system_switch.py)
  - Zone entry   → Learning Mode + Active Control switches (entities/switch.py)
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_SYSTEM
from .entities.switch import async_setup_entry as _zone_switch_setup
from .entities.system_switch import async_setup_entry as _system_switch_setup


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Dispatch to system or zone switch setup based on entry type."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM:
        await _system_switch_setup(hass, entry, async_add_entities)
    else:
        await _zone_switch_setup(hass, entry, async_add_entities)
