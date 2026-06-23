"""Zone control switch entities for SmartShading.

Exactly two switches per configured zone: SmartShadingLearningModeSwitch and
SmartShadingActiveControlSwitch.  Both write their state to the coordinator's
runtime zone-execution overrides and persist via config_entry.options so
values survive HA restarts.

Defaults (matching ZoneExecutionConfig):
  Learning Mode   — on  by default (safe to learn from first boot)
  Active Control  — off by default (no cover movement until user opts in)
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from ..coordinator import SmartShadingCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SmartShadingCoordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = []

    for zone_id, zone in coordinator.zones.items():
        entities.append(
            SmartShadingLearningModeSwitch(
                coordinator=coordinator,
                zone_id=zone_id,
                zone_name=zone.name,
            )
        )
        entities.append(
            SmartShadingActiveControlSwitch(
                coordinator=coordinator,
                zone_id=zone_id,
                zone_name=zone.name,
            )
        )

    async_add_entities(entities)


class _ZoneControlSwitch(CoordinatorEntity[SmartShadingCoordinator], SwitchEntity):
    """Base class for per-zone control switches."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: SmartShadingCoordinator,
        zone_id: str,
        zone_name: str,
        translation_key: str,
        unique_id_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._attr_translation_key = translation_key
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{zone_id}_{unique_id_suffix}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_{zone_id}")},
            name=zone_name,
        )


class SmartShadingLearningModeSwitch(_ZoneControlSwitch):
    """Learning Mode switch — controls learning_enabled for a zone.

    When on:  SmartShading evaluates decisions and outcomes, learns thermal
              responses and user preferences, generates shadow proposals,
              prepares improvements and — only with Active Control and all
              safety/eligibility gates — runs strictly-bounded learning
              experiments.
    When off: Learning, model updates, shadow proposals and experiments are
              paused; a running experiment is logically aborted.  No stored
              learning data is deleted.  Deterministic rule-based control may
              continue when Active Control is on.

    Default: on.
    """

    _attr_icon = "mdi:brain"

    def __init__(
        self,
        coordinator: SmartShadingCoordinator,
        zone_id: str,
        zone_name: str,
    ) -> None:
        super().__init__(
            coordinator,
            zone_id=zone_id,
            zone_name=zone_name,
            translation_key="learning_mode",
            unique_id_suffix="learning_mode",
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.effective_zone_execution(self._zone_id).learning_enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_zone_learning_enabled(self._zone_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_zone_learning_enabled(self._zone_id, False)


class SmartShadingActiveControlSwitch(_ZoneControlSwitch):
    """Active Control switch — controls active_control_enabled for a zone.

    When on:  SmartShading may issue cover service calls for this zone,
              subject to CommandFilter, StateGuard, and all safety checks.
    When off: SmartShading computes recommendations and diagnostics only.
              No cover is moved automatically.

    Default: off.  Enable only when the configured covers are safe to operate
    automatically.  Safety decisions (STORM_SAFE, WIND_SAFE) and manual
    override bypass this flag and remain unaffected.
    """

    def __init__(
        self,
        coordinator: SmartShadingCoordinator,
        zone_id: str,
        zone_name: str,
    ) -> None:
        super().__init__(
            coordinator,
            zone_id=zone_id,
            zone_name=zone_name,
            translation_key="active_control",
            unique_id_suffix="active_control",
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.effective_zone_execution(self._zone_id).active_control_enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_zone_active_control_enabled(self._zone_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_zone_active_control_enabled(self._zone_id, False)
