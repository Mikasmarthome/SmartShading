"""SmartShading binary sensors (ARCHITECTURE.md §8.2, simplified
observability scope, 2026-06-16).

Observability cleanup (2026-06-16): renamed from "Sun Active" to "Window
In Solar Sector" - the underlying computation (azimuth_delta <= tolerance
AND elevation > 0) is purely geometric and says nothing about whether the
sun is currently strong enough to matter for shading. "Active" implied an
effect; this entity only ever reports a position. See
the project's internal design review notes for the full rationale. Pure rename - no change to what is
computed, no change to the State Machine, no new entity.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..coordinator import SmartShadingCoordinator
from .base import SmartShadingWindowEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SmartShadingCoordinator = entry.runtime_data.coordinator
    entities = []
    _zone_window_count: dict[str, int] = {}
    _window_index: dict[str, int] = {}
    _zone_counter: dict[str, int] = {}
    for window_id, window in coordinator.windows.items():
        _zone_window_count[window.zone_id] = _zone_window_count.get(window.zone_id, 0) + 1
        count = _zone_counter.get(window.zone_id, 0) + 1
        _zone_counter[window.zone_id] = count
        _window_index[window_id] = count
    for window_id, window in coordinator.windows.items():
        is_multi = _zone_window_count.get(window.zone_id, 1) > 1
        idx = _window_index.get(window_id)
        entities.append(SmartShadingWindowInSolarSectorBinarySensor(coordinator, window_id, window.name, window.zone_id, is_multi, idx))
        entities.append(SmartShadingOverrideActiveBinarySensor(coordinator, window_id, window.name, window.zone_id, is_multi, idx))
    async_add_entities(entities)


class SmartShadingWindowInSolarSectorBinarySensor(SmartShadingWindowEntity, BinarySensorEntity):
    """Is the window geometrically within the current solar sector
    (azimuth_delta <= tolerance and sun above horizon)?

    This is Level 1 of the three-level exposure model (geometry only). It
    is NOT a statement about whether the sun is currently strong enough to
    trigger shading - that is `effective_exposure` (Level 3, on the
    Exposure sensor). Marked DIAGNOSTIC because this is a technical geometry
    detail rather than an actionable user-facing status.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SmartShadingCoordinator, window_id: str, window_name: str, zone_id: str, is_multi_window_zone: bool = False, window_index: int | None = None) -> None:
        super().__init__(coordinator, window_id, window_name, "window_in_solar_sector", zone_id, is_multi_window_zone, window_index)

    @property
    def is_on(self) -> bool | None:
        observation = self._observation
        if observation is None or observation.exposure is None:
            return None
        return observation.exposure.is_in_tolerance_window

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        observation = self._observation
        if observation is None or observation.exposure is None:
            return None
        return {"azimuth_delta_deg": round(observation.exposure.azimuth_delta_deg, 1)}


class SmartShadingOverrideActiveBinarySensor(SmartShadingWindowEntity, BinarySensorEntity):
    """Is a manual override currently holding this window's cover position?

    True when the user has manually moved the cover and SmartShading is
    deliberately not issuing commands to respect that choice.  False when
    SmartShading evaluates and dispatches normally.

    override_position, override_expires_at, and override_source from the
    ManualOverride model are exposed as attributes so the user can see what
    was overridden and when automatic control resumes.

    override_position is in HA convention (0=closed, 100=open) — already
    converted at the WindowObservation level (ManualOverride stores internal
    convention; the conversion happens in the coordinator's observability path).
    """

    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: SmartShadingCoordinator, window_id: str, window_name: str, zone_id: str, is_multi_window_zone: bool = False, window_index: int | None = None) -> None:
        super().__init__(coordinator, window_id, window_name, "override_active", zone_id, is_multi_window_zone, window_index)

    @property
    def is_on(self) -> bool | None:
        observation = self._observation
        if observation is None:
            return None
        return observation.override_active

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        observation = self._observation
        if observation is None:
            return None
        return {
            "override_position": observation.override_position,
            "override_expires_at": observation.override_expires_at,
            "override_source": observation.override_source,
        }
