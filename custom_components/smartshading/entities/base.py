"""Shared base entity for all SmartShading per-window entities
(ARCHITECTURE.md §2 `entities/base.py`).

All window entities are assigned to their zone's device rather than creating
a separate per-window device.  This produces a zone-centric UI where one
device per zone groups all controls, sensors, and diagnostics together.

Multi-window zones: when a zone has more than one window, entities use an
indexed translation key so HA renders the label in the user's locale
(e.g. "1 Empfehlung" in German, "1 Recommendation" in English).
Single-window zones keep the clean short names from translations.
"""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from ..coordinator import SmartShadingCoordinator, WindowObservation


class SmartShadingWindowEntity(CoordinatorEntity[SmartShadingCoordinator]):
    """Base for one entity belonging to one window."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SmartShadingCoordinator,
        window_id: str,
        window_name: str,
        entity_key: str,
        zone_id: str,
        is_multi_window_zone: bool = False,
        window_index: int | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._window_id = window_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{window_id}_{entity_key}"
        if is_multi_window_zone and window_index is not None:
            # Use indexed translation key with a numeric placeholder so HA renders
            # the label in the user's locale (e.g. "1 Empfehlung" in German).
            # unique_id is unchanged — window_id is the stable identity.
            self._attr_translation_key = f"{entity_key}_indexed"
            self._attr_translation_placeholders = {"index": str(window_index)}
        else:
            self._attr_translation_key = entity_key
        # Assign to zone device so all entities for a zone appear on one device.
        # zone.name is read from coordinator.zones to keep naming consistent
        # with zone-level entities in switch.py and zone_summary.py.
        zone = coordinator.zones.get(zone_id)
        zone_name = zone.name if zone is not None else zone_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_{zone_id}")},
            name=zone_name,
        )

    @property
    def _observation(self) -> WindowObservation | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.window_results.get(self._window_id)
