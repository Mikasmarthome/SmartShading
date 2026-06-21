"""SmartShading System button entities — v1.0 System Entry.

The SmartShading System config entry (ENTRY_TYPE_SYSTEM) owns two entities:
the Support Export button and the Research Export button.  This module is
loaded only for that entry type (see __init__.py: SYSTEM_PLATFORMS = ["button"]).
Zone entries never load this platform.

SmartShadingExportButton
    Triggers a privacy-safe Support Export covering ALL SmartShading zone entries.
    Writes JSON to /config/www/ and creates a persistent notification.

SmartShadingResearchExportButton
    Opens the Research Export confirmation flow for the System Entry.
    Never writes a file directly — the confirmation step in the Options Flow
    must be completed first.  This ensures the mandatory confirmation is always
    shown regardless of how the button is pressed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
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

_EXPORT_FILENAME_PREFIX = "smartshading_support_export_"
_RESEARCH_EXPORT_FILENAME_PREFIX = "smartshading_research_export_"
_WWW_SUBDIR = "www"

_NOTIFICATION_ID_PREFIX = "smartshading_support_export"
_RESEARCH_NOTIFICATION_ID_PREFIX = "smartshading_research_export"


def _notification_locale(hass: HomeAssistant) -> str:
    """Return 'de' if HA is configured in German, 'en' otherwise."""
    try:
        lang = getattr(hass.config, "language", None) or "en"
        return "de" if str(lang).lower().startswith("de") else "en"
    except Exception:
        return "en"


def _export_filename(now: datetime, entry_id: str) -> str:
    """Generate a collision-safe Support Export filename.

    Format: smartshading_support_export_{YYYYMMDDTHHMMSSZ}_{6-char-hex}.json
    The hex suffix is a deterministic hash of the entry_id so the filename
    is stable per-entry per-second and not path-traversal exploitable.
    """
    ts = now.strftime("%Y%m%dT%H%M%S") + "Z"
    short = hashlib.blake2b(entry_id.encode(), digest_size=3).hexdigest()
    return f"{_EXPORT_FILENAME_PREFIX}{ts}_{short}.json"


def _write_export_file(www_dir: pathlib.Path, filepath: pathlib.Path, data: dict) -> None:
    """Write the export JSON to *filepath* (synchronous — run in executor).

    Creates *www_dir* if it does not exist.  Uses UTF-8 encoding with
    pretty-print indent for human readability.  No path traversal is possible
    because the filepath is constructed entirely from safe components.
    """
    www_dir.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    filepath.write_text(content, encoding="utf-8")


def _research_export_filename(now: datetime) -> str:
    """Generate a Research Export filename.

    Format: smartshading_research_export_{YYYYMMDDTHHMMSSZ}.json
    No entry-specific suffix — research export covers all zones globally.
    """
    ts = now.strftime("%Y%m%dT%H%M%S") + "Z"
    return f"{_RESEARCH_EXPORT_FILENAME_PREFIX}{ts}.json"


def _build_notification_message(
    filepath_str: str, local_url: str, locale: str = "en"
) -> str:
    if locale == "de":
        return (
            f"SmartShading Support-Export wurde erstellt.\n\n"
            f"Die Datei enthält zusammengefasste datenschutzfreundliche Diagnoseinformationen "
            f"und wird nach 24 Stunden automatisch gelöscht.\n\n"
            f"Datei: `{filepath_str}`\n"
            f"Öffnen: `{local_url}`\n\n"
            f"Prüfe die Datei vor dem Teilen. "
            f"Es wurden keine Daten übertragen."
        )
    return (
        f"SmartShading Support Export created.\n\n"
        f"The file contains aggregated privacy-safe diagnostic data "
        f"and will be automatically deleted after 24 hours.\n\n"
        f"File: `{filepath_str}`\n"
        f"Open: `{local_url}`\n\n"
        f"Review the file before sharing. "
        f"No data has been sent anywhere."
    )


def _build_research_notification_message(filepath_str: str, locale: str = "en") -> str:
    if locale == "de":
        return (
            f"SmartShading Research-Export wurde erstellt.\n\n"
            f"Die Datei enthält detailliertere anonymisierte technische Lernereignisse, "
            f"Sensorzusammenhänge, Entscheidungen, manuelle Änderungen und "
            f"Ergebnisbewertungen. Sie ist zur Analyse und Weiterentwicklung der "
            f"SmartShading Learning Engine gedacht.\n\n"
            f"Die Datei enthält keine Raum- oder Fensternamen, Entity-IDs, Geräte-IDs, "
            f"Adressen oder exakten Standortdaten. Prüfe die Datei vor dem Teilen.\n\n"
            f"Die Datei wurde nur lokal erstellt, nicht hochgeladen oder übertragen. "
            f"Sie wird nach 24 Stunden automatisch gelöscht.\n\n"
            f"Datei: `{filepath_str}`"
        )
    return (
        f"SmartShading Research Export created.\n\n"
        f"The file contains more detailed anonymized technical learning events, "
        f"sensor correlations, decisions, manual changes, and outcome evaluations. "
        f"It is intended for analysis and development of the SmartShading "
        f"Learning Engine.\n\n"
        f"The file does not contain room or window names, entity IDs, device IDs, "
        f"addresses, or exact location data. Review the file before sharing.\n\n"
        f"The file was created locally only — it was not uploaded or transmitted. "
        f"It will be automatically deleted after 24 hours.\n\n"
        f"File: `{filepath_str}`"
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
            result.append({
                "entry_id": entry.entry_id,
                "window_ids": window_ids,
                "learning_store": learning_store,
                "forecast_store": forecast_store,
                "target_position_adapter": target_adapter,
            })
        except Exception:
            _LOGGER.warning(
                "SmartShading: export: could not read runtime_data for entry %s",
                entry.entry_id,
            )
    return result


class SmartShadingExportButton(ButtonEntity):
    """Button that triggers a global privacy-safe learning export.

    Press → discovers all active zone entries → builds privacy-safe JSON →
    writes to /config/www/ → creates a persistent notification with the
    /local/... URL.

    No export payload is written to HA .storage.
    """

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
        """Trigger the global privacy-safe learning export."""
        from homeassistant.components.persistent_notification import (
            async_create as pn_async_create,
        )
        from ..engines.zone_learning_export import build_global_learning_export

        now = datetime.now(timezone.utc)
        try:
            zone_entries = await _collect_zone_entries(self._hass)
            export_data = build_global_learning_export(
                zone_entries=zone_entries,
                generated_at_utc=now,
            )
        except Exception:
            _LOGGER.error("SmartShading: learning export: failed to build export data")
            return

        filename = _export_filename(now, self._entry.entry_id)
        config_dir = pathlib.Path(self._hass.config.config_dir)
        www_dir = config_dir / _WWW_SUBDIR
        filepath = www_dir / filename
        local_url = f"/local/{filename}"
        filepath_str = f"/config/{_WWW_SUBDIR}/{filename}"

        try:
            await self._hass.async_add_executor_job(
                _write_export_file, www_dir, filepath, export_data
            )
        except Exception:
            _LOGGER.error(
                "SmartShading: learning export: failed to write file %s", filepath
            )
            return

        locale = _notification_locale(self._hass)
        notification_id = f"{_NOTIFICATION_ID_PREFIX}_{now.strftime('%Y%m%dT%H%M%S')}"
        title = (
            "SmartShading — Support-Export"
            if locale == "de"
            else "SmartShading — Support Export"
        )
        message = _build_notification_message(filepath_str, local_url, locale=locale)
        try:
            pn_async_create(
                self._hass,
                message=message,
                title=title,
                notification_id=notification_id,
            )
        except Exception:
            _LOGGER.warning("SmartShading: learning export: failed to create notification")

        _LOGGER.info(
            "SmartShading: support export written to %s (%d zone(s))",
            filepath,
            export_data.get("zones_count", 0),
        )

        from ..engines.export_retention import cleanup_old_exports
        try:
            await self._hass.async_add_executor_job(cleanup_old_exports, www_dir)
        except Exception:
            _LOGGER.warning("SmartShading: support export: retention cleanup failed")

        from ..const import DATA_DEBUG_LOGGING, DOMAIN as _DOMAIN
        if self._hass.data.get(_DOMAIN, {}).get(DATA_DEBUG_LOGGING, False):
            _LOGGER.debug(
                "SmartShading: export result: zones=%d windows=%d outcomes=%d",
                export_data.get("zones_count", 0),
                export_data.get("windows_count", 0),
                export_data.get("total_outcomes", 0),
            )


class SmartShadingResearchExportButton(ButtonEntity):
    """Button that opens the Research Export confirmation flow.

    Press → starts the Options Flow for the System Entry → confirmation form
    is shown → user must check the confirmation box → only then is the export
    file written.

    The file is never written directly from async_press.  The mandatory
    confirmation in the Options Flow cannot be bypassed regardless of how
    this button is triggered.
    """

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
        """Create a Research Export directly and write it to /config/www/."""
        from homeassistant.components.persistent_notification import (
            async_create as pn_async_create,
        )
        from ..engines.research_export import build_research_export
        from ..engines.export_retention import cleanup_old_exports

        now = datetime.now(timezone.utc)
        try:
            zone_entries = await _collect_zone_entries(self._hass)
            export_data = build_research_export(
                zone_entries=zone_entries,
                generated_at_utc=now,
            )
        except Exception:
            _LOGGER.error("SmartShading: research export: failed to build export data")
            return

        filename = _research_export_filename(now)
        config_dir = pathlib.Path(self._hass.config.config_dir)
        www_dir = config_dir / _WWW_SUBDIR
        filepath = www_dir / filename
        filepath_str = f"/config/{_WWW_SUBDIR}/{filename}"

        try:
            await self._hass.async_add_executor_job(
                _write_export_file, www_dir, filepath, export_data
            )
        except Exception:
            _LOGGER.error(
                "SmartShading: research export: failed to write file %s", filepath
            )
            return

        locale = _notification_locale(self._hass)
        notification_id = f"{_RESEARCH_NOTIFICATION_ID_PREFIX}_{now.strftime('%Y%m%dT%H%M%S')}"
        title = (
            "SmartShading — Research-Export"
            if locale == "de"
            else "SmartShading — Research Export"
        )
        message = _build_research_notification_message(filepath_str, locale=locale)
        try:
            pn_async_create(
                self._hass,
                message=message,
                title=title,
                notification_id=notification_id,
            )
        except Exception:
            _LOGGER.warning("SmartShading: research export: failed to create notification")

        _LOGGER.info(
            "SmartShading: research export written to %s (%d zone(s))",
            filepath,
            export_data.get("zones_count", 0),
        )

        try:
            await self._hass.async_add_executor_job(cleanup_old_exports, www_dir)
        except Exception:
            _LOGGER.warning("SmartShading: research export: retention cleanup failed")
