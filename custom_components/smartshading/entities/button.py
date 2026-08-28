"""SmartShading System button entities — v1.0 System Entry.

The SmartShading System config entry (ENTRY_TYPE_SYSTEM) owns two entities:
the Support Export button and the Research Export button.  This module is
loaded only for that entry type (see __init__.py: SYSTEM_PLATFORMS = ["button"]).
Zone entries never load this platform.

HACS stable-conformance fix: both buttons used to write the Support/Research
Export JSON to /config/www/, which Home Assistant serves unauthenticated at
/local/... and whose cleanup timer did not survive a restart. The same
privacy-safe export data (see support_export.py / research_export_v3.py —
no room/window names, no entity IDs, no device IDs) is now aggregated into
the standard, authenticated Home Assistant Diagnostics download instead
(diagnostics.py) — no new export mechanism, just the existing one moved to
the existing, safer home. Pressing either button now only points the user
at that download; neither button writes a file anymore.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_ZONE,
    SYSTEM_DEVICE_IDENTIFIER,
)

_LOGGER = logging.getLogger(__name__)

_NOTIFICATION_ID_PREFIX = "smartshading_support_export"
_RESEARCH_NOTIFICATION_ID_PREFIX = "smartshading_research_export"


def _notification_locale(hass: HomeAssistant) -> str:
    """Return 'de' if HA is configured in German, 'en' otherwise."""
    try:
        lang = getattr(hass.config, "language", None) or "en"
        return "de" if str(lang).lower().startswith("de") else "en"
    except Exception:
        return "en"


def _build_diagnostics_pointer_message(locale: str = "en") -> str:
    """Message shown when either export button is pressed — see this
    module's own docstring for why these buttons no longer write a file."""
    if locale == "de":
        return (
            f"Der Support- und Research-Export ist jetzt Teil des normalen "
            f"Home-Assistant-Diagnostics-Downloads.\n\n"
            f"Öffne Einstellungen → Geräte & Dienste → SmartShading → "
            f"das System-Gerät → Diagnose herunterladen. Die Datei enthält "
            f"dieselben datenschutzfreundlichen, aggregierten Daten wie "
            f"zuvor — ohne Raum-/Fensternamen, Entity-IDs oder Geräte-IDs — "
            f"und wird nicht mehr unauthentifiziert unter /config/www/ "
            f"abgelegt."
        )
    return (
        f"Support and Research Export data is now part of the standard "
        f"Home Assistant Diagnostics download.\n\n"
        f"Open Settings → Devices & Services → SmartShading → the System "
        f"device → Download diagnostics. The download contains the same "
        f"privacy-safe, aggregated data as before — no room/window names, "
        f"entity IDs, or device IDs — and is no longer written "
        f"unauthenticated to /config/www/."
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Register the export buttons for the SmartShading System entry.

    This function is only called for ENTRY_TYPE_SYSTEM entries because the
    button platform is listed in SYSTEM_PLATFORMS (not ZONE_PLATFORMS).
    No deduplication guard is needed — there is exactly one system entry.
    """
    async_add_entities([
        SmartShadingExportButton(hass, entry),
        SmartShadingResearchExportButton(hass, entry),
    ])


async def _collect_zone_entries(hass: HomeAssistant) -> list[dict]:
    """Collect learning and forecast stores from all active SmartShading zone entries.

    Only ENTRY_TYPE_ZONE entries are included; the system entry itself is
    excluded.  Returns a list of per-zone dicts suitable for
    build_global_learning_export().
    """
    result: list[dict] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        # Skip the system entry and any future non-zone entries.
        if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) != ENTRY_TYPE_ZONE:
            continue
        try:
            rd = entry.runtime_data
            learning_store = getattr(rd, "learning_store", None)
            forecast_store = getattr(rd, "forecast_store", None)
            target_adapter = getattr(rd, "target_position_adapter", None)
            coordinator = getattr(rd, "coordinator", None)
            window_ids = list(coordinator.windows.keys()) if coordinator else []
            # Runtime execution diagnostics for dispatch-path debugging.
            exec_diag: dict = {}
            zone_runtime: dict = {}
            if coordinator is not None:
                try:
                    data = coordinator.data
                    exec_diag = dict(data.execution_diagnostics) if data is not None else {}
                    # Collect zone-level runtime state.
                    zone_runtime = {
                        "startup_grace_remaining": coordinator._startup_cycles_remaining,
                        "dispatch_generation": coordinator._dispatch_generation,
                        "last_update_success": coordinator.last_update_success,
                        "last_global_dispatch_at": (
                            coordinator._serial_dispatch.last_dispatch_at.isoformat()
                            if getattr(coordinator._serial_dispatch, "last_dispatch_at", None) is not None
                            else None
                        ),
                    }
                except Exception:
                    _LOGGER.debug(
                        "SmartShading: export: zone runtime data collection failed"
                        " for entry %s (non-fatal, defaults used)",
                        entry.entry_id, exc_info=True)
            result.append({
                "entry_id": entry.entry_id,
                "window_ids": window_ids,
                "window_configs": coordinator.windows if coordinator is not None else {},
                "coordinator_data": coordinator.data if coordinator is not None else None,
                "learning_store": learning_store,
                "forecast_store": forecast_store,
                "target_position_adapter": target_adapter,
                "execution_diagnostics": exec_diag,
                "zone_runtime": zone_runtime,
            })
        except Exception:
            _LOGGER.warning(
                "SmartShading: export: could not read runtime_data for entry %s",
                entry.entry_id,
            )
    return result


def _resolve_zone_coordinator(hass: HomeAssistant):
    """Return the live coordinator of the (first) active SmartShading zone entry.

    The export buttons live on the SYSTEM entry, which deliberately has NO
    coordinator — the real zone/window/sensor context lives on the
    ENTRY_TYPE_ZONE entries.  Reading ``system_entry.runtime_data.coordinator``
    therefore yields None and produces an empty export (zone_count 0, sensors
    false, runtime_mode inactive).  Resolve the actual zone coordinator instead.

    Returns ``(coordinator, zone_entry, zone_count)``.  ``coordinator``/``zone_entry``
    are None when no active zone entry exists.  ``zone_count`` is how many active
    zone coordinators were found (the v3 builders are single-zone-scoped; the
    primary zone is used and the count is surfaced for transparency/logging).
    """
    zone_coords: list = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) != ENTRY_TYPE_ZONE:
            continue
        coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
        if coordinator is not None:
            zone_coords.append((coordinator, entry))
    if not zone_coords:
        return None, None, 0
    coordinator, zone_entry = zone_coords[0]
    return coordinator, zone_entry, len(zone_coords)


def _active_zone_coordinators(hass: HomeAssistant) -> list:
    """Return the live coordinators of ALL active SmartShading zone entries.

    The system export buttons must aggregate every zone (a real install has many
    zones/windows), never just the first.  Skips the system entry and any zone
    entry without a live coordinator.
    """
    coords: list = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE) != ENTRY_TYPE_ZONE:
            continue
        coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
        if coordinator is not None:
            coords.append(coordinator)
    return coords


class SmartShadingExportButton(ButtonEntity):
    """Button that points the user at the Support Export section of the
    standard Home Assistant Diagnostics download — see this module's own
    docstring for why it no longer writes a file to /config/www/ itself."""

    _attr_has_entity_name = True
    _attr_translation_key = "export_support_data"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:database-export"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_system_export_support_data"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)},
            name="SmartShading System",
            manufacturer="SmartShading",
        )

    async def async_press(self) -> None:
        """Point the user at the Diagnostics download — never writes a file."""
        from homeassistant.components.persistent_notification import (
            async_create as pn_async_create,
        )

        now = datetime.now(timezone.utc)
        locale = _notification_locale(self._hass)
        try:
            pn_async_create(
                self._hass,
                message=_build_diagnostics_pointer_message(locale=locale),
                title=(
                    "SmartShading — Support-Export"
                    if locale == "de"
                    else "SmartShading — Support Export"
                ),
                notification_id=f"{_NOTIFICATION_ID_PREFIX}_{now.strftime('%Y%m%dT%H%M%S')}",
            )
        except Exception as exc:
            _LOGGER.warning(
                "SmartShading: support export: failed to create notification (%s: %s)",
                type(exc).__name__, exc,
            )


class SmartShadingResearchExportButton(ButtonEntity):
    """Button that points the user at the Research Export section of the
    standard Home Assistant Diagnostics download — see this module's own
    docstring for why it no longer writes a file to /config/www/ itself."""

    _attr_has_entity_name = True
    _attr_translation_key = "export_research_data"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:database-search"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_system_export_research_data"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, SYSTEM_DEVICE_IDENTIFIER)},
            name="SmartShading System",
            manufacturer="SmartShading",
        )

    async def async_press(self) -> None:
        """Point the user at the Diagnostics download — never writes a file."""
        from homeassistant.components.persistent_notification import (
            async_create as pn_async_create,
        )

        now = datetime.now(timezone.utc)
        locale = _notification_locale(self._hass)
        try:
            pn_async_create(
                self._hass,
                message=_build_diagnostics_pointer_message(locale=locale),
                title=(
                    "SmartShading — Research-Export"
                    if locale == "de"
                    else "SmartShading — Research Export"
                ),
                notification_id=(
                    f"{_RESEARCH_NOTIFICATION_ID_PREFIX}_{now.strftime('%Y%m%dT%H%M%S')}"
                ),
            )
        except Exception as exc:
            _LOGGER.warning(
                "SmartShading: research export: failed to create notification (%s: %s)",
                type(exc).__name__, exc,
            )
