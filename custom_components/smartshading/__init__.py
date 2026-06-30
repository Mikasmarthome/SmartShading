"""SmartShading - local Home Assistant integration for intelligent,
state-based control of shading systems (ARCHITECTURE.md §1).

Entry type architecture (v1.0 System Entry Separation)
-------------------------------------------------------
SmartShading uses two distinct config entry types:

  ENTRY_TYPE_ZONE   — one entry per zone; owns the zone coordinator, all
                       window/zone sensor entities, and zone switches.
                       Platforms: sensor, binary_sensor, switch.

  ENTRY_TYPE_SYSTEM — exactly one entry for the whole integration; owns
                       global entities (export button) and future system-
                       level entities.  Created automatically when the first
                       zone entry is set up.
                       Platforms: button.

Backward compatibility: zone entries created before this change have no
CONF_ENTRY_TYPE in their data — they are treated as ENTRY_TYPE_ZONE entries.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .config_entry_data import from_storage_dict
from .const import (
    CONF_DEBUG_LOGGING,
    CONF_ENTRY_TYPE,
    DATA_DEBUG_LOGGING,
    DATA_GLOBAL_DISPATCH,
    DOMAIN,
    ENTRY_TYPE_SYSTEM,
    ENTRY_TYPE_ZONE,
    SYSTEM_PLATFORMS,
    ZONE_PLATFORMS,
)
from .cover_control.global_dispatch_throttle import GlobalSerialDispatch
from .coordinator import SmartShadingCoordinator, SmartShadingRuntimeData
from .cover_control.cover_controller import CoverController
from .cover_control.travel_tracker import TravelTracker
from .engines.forecast_orchestrator import async_run_startup_matching
from .engines.forecast_persistence import (
    STORAGE_KEY as FORECAST_STORAGE_KEY,
    STORAGE_VERSION as FORECAST_STORAGE_VERSION,
    ForecastPersistenceAdapter,
)
from .engines.forecast_scheduler import async_setup_forecast_learning
from .engines.learning_persistence import (
    LEARNING_STORAGE_KEY,
    LEARNING_STORE_VERSION,
)
from .models.forecast_store import ForecastLearningStore

_LOGGER = logging.getLogger(__name__)

type SmartShadingConfigEntry = ConfigEntry[SmartShadingRuntimeData]


# ---------------------------------------------------------------------------
# Entry dispatch
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: SmartShadingConfigEntry) -> bool:
    """Dispatch to the correct setup path based on entry type."""
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE)
    if entry_type == ENTRY_TYPE_SYSTEM:
        return await _async_setup_system_entry(hass, entry)
    return await _async_setup_zone_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: SmartShadingConfigEntry) -> bool:
    """Unload a SmartShading config entry."""
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE)
    if entry_type == ENTRY_TYPE_SYSTEM:
        return await hass.config_entries.async_unload_platforms(entry, SYSTEM_PLATFORMS)

    # Zone entry: cancel forecast timers and flush pending learning data.
    if cancel := getattr(entry.runtime_data, "forecast_cancel", None):
        cancel[0]()
        cancel[1]()
    coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
    if coordinator is not None:
        # Explicit teardown as a safety net; also called via entry.async_on_unload
        # registered in async_setup_presence_listeners / async_setup_contact_listeners.
        # Idempotent.
        coordinator.async_teardown_presence_listeners()
        coordinator.async_teardown_contact_listeners()
        coordinator.async_teardown_lifecycle_boundary_timer()
        await coordinator.async_flush_learning()
    return await hass.config_entries.async_unload_platforms(entry, ZONE_PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SmartShadingConfigEntry) -> None:
    """Clean up SmartShading-owned persistent storage on entry removal.

    System entry: no scoped storage — returns immediately.
    Zone entry: removes scoped learning and forecast storage files.
    """
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_ZONE)
    if entry_type == ENTRY_TYPE_SYSTEM:
        return

    from homeassistant.helpers.storage import Store  # lazy HA import

    for version, base_key in (
        (LEARNING_STORE_VERSION, LEARNING_STORAGE_KEY),
        (FORECAST_STORAGE_VERSION, FORECAST_STORAGE_KEY),
    ):
        key = f"{base_key}_{entry.entry_id}"
        try:
            await Store(hass, version, key).async_remove()
        except Exception:
            _LOGGER.warning(
                "SmartShading: failed to remove storage %s during entry removal", key
            )


# ---------------------------------------------------------------------------
# System entry setup
# ---------------------------------------------------------------------------


async def _async_setup_system_entry(
    hass: HomeAssistant, entry: SmartShadingConfigEntry
) -> bool:
    """Set up the SmartShading System entry.

    The system entry owns only global system entities (export button, debug
    logging switch).  It has no coordinator and does not parse zone/window data.

    Propagates the persisted debug_logging flag to hass.data so zone
    coordinators can read it without re-looking up the system entry each cycle.
    """
    # Propagate the debug flag to hass.data so zone coordinators can read it.
    hass.data.setdefault(DOMAIN, {})[DATA_DEBUG_LOGGING] = entry.options.get(
        CONF_DEBUG_LOGGING, False
    )
    # Run export retention cleanup on every startup/reload.
    import pathlib
    from .engines.export_retention import cleanup_old_exports as _cleanup_exports
    _www_dir = pathlib.Path(hass.config.config_dir) / "www"
    try:
        await hass.async_add_executor_job(_cleanup_exports, _www_dir)
    except Exception:
        _LOGGER.warning("SmartShading: startup export retention cleanup failed")

    await hass.config_entries.async_forward_entry_setups(entry, SYSTEM_PLATFORMS)
    return True


# ---------------------------------------------------------------------------
# Zone entry setup
# ---------------------------------------------------------------------------


def _ensure_system_entry(hass: HomeAssistant) -> None:
    """Schedule creation of the SmartShading System entry if it doesn't exist.

    Safe to call multiple times — the config flow's async_step_system aborts
    immediately if a system entry already exists, so concurrent calls from
    multiple zone entries on startup produce exactly one system entry.
    """
    existing = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SYSTEM
    ]
    if not existing:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "system"}
            )
        )


async def _async_setup_zone_entry(
    hass: HomeAssistant, entry: SmartShadingConfigEntry
) -> bool:
    """Set up a SmartShading zone entry.

    Parses zone/window/cover-group config, starts the coordinator, sets up
    forecast learning, and registers zone/window entities.  Also ensures the
    SmartShading System entry exists (creates it if missing).
    """
    entry_data = from_storage_dict(entry.data)
    zones = {zone.id: zone for zone in entry_data.zones}
    windows = {window.id: window for window in entry_data.windows}
    cover_groups = {cover_group.id: cover_group for cover_group in entry_data.cover_groups}

    # ------------------------------------------------------------------
    # Forecast Learning setup — must not abort async_setup_entry.
    # ------------------------------------------------------------------
    try:
        from .engines.forecast_persistence import compute_provider_fingerprint
        _fl_fp = compute_provider_fingerprint(
            forecast_entity=entry_data.weather_entity_id,
            solar_entity=entry_data.solar_radiation_sensor_id,
            owner=entry.entry_id)
        _fl_zone = entry_data.zones[0].id if getattr(entry_data, "zones", None) else None
        _fl_adapter = ForecastPersistenceAdapter.create(
            hass, entry.entry_id, owner_zone_id=_fl_zone, provider_fingerprint=_fl_fp)
        _fl_store   = await _fl_adapter.async_restore()
        # Write an initial schema-valid storage file on first setup so that
        # smartshading_forecast_learning_<id> appears in /config/.storage/
        # immediately rather than waiting for the first collection cycle.
        if _fl_adapter.fresh_start:
            await _fl_adapter.async_save(_fl_store)
        await async_run_startup_matching(_fl_store, _fl_adapter)
        _fl_cancel  = await async_setup_forecast_learning(
            hass,
            _fl_store,
            _fl_adapter,
            forecast_entity_id=entry_data.weather_entity_id,
            temp_entity_id=entry_data.outdoor_temperature_sensor_id,
            cloud_entity_id=entry_data.cloud_cover_sensor_id,
            solar_entity_id=entry_data.solar_radiation_sensor_id,
        )
    except Exception:
        _LOGGER.error(
            "SmartShading: Forecast Learning setup failed — continuing without it"
        )
        _fl_store   = ForecastLearningStore.empty()
        _fl_adapter = None
        _fl_cancel  = None

    # ------------------------------------------------------------------
    # Integration-wide shared state in hass.data[DOMAIN]
    # ------------------------------------------------------------------
    # GlobalSerialDispatch: shared across all zone coordinators so cover
    # service calls from different zones are serialised globally.  Created on
    # the first zone entry setup; subsequent zone entries reuse the same instance.
    hass.data.setdefault(DOMAIN, {})
    if DATA_GLOBAL_DISPATCH not in hass.data[DOMAIN]:
        hass.data[DOMAIN][DATA_GLOBAL_DISPATCH] = GlobalSerialDispatch()
    serial_dispatch: GlobalSerialDispatch = hass.data[DOMAIN][DATA_GLOBAL_DISPATCH]

    # Debug logging flag: default False until the system entry sets it.
    hass.data[DOMAIN].setdefault(DATA_DEBUG_LOGGING, False)

    # ------------------------------------------------------------------
    # Coordinator setup
    # ------------------------------------------------------------------
    coordinator = SmartShadingCoordinator(
        hass,
        entry,
        windows=windows,
        zones=zones,
        cover_groups=cover_groups,
        shade_position_defaults=entry_data.shade_position_defaults,
        weather_entity_id=entry_data.weather_entity_id,
        solar_radiation_sensor_id=entry_data.solar_radiation_sensor_id,
        outdoor_temperature_sensor_id=entry_data.outdoor_temperature_sensor_id,
        cloud_cover_sensor_id=entry_data.cloud_cover_sensor_id,
        wind_speed_sensor_id=entry_data.wind_speed_sensor_id,
        rain_sensor_id=entry_data.rain_sensor_id,
        lifecycle_config=entry_data.lifecycle_config,
        presence_entity_ids=entry_data.presence_entity_ids,
        absence_delay_min=entry_data.absence_delay_min,
        indoor_temperature_sensor_ids=entry_data.indoor_temperature_sensor_ids,
        comfort_config=entry_data.comfort_config,
        global_serial_dispatch=serial_dispatch,
    )
    # Inject the ForecastLearningStore so the ForecastStrategyModifier can access
    # trust data and current forecast snapshots starting from the first cycle.
    coordinator.set_forecast_store(_fl_store)

    await coordinator.async_config_entry_first_refresh()

    # Register immediate state-change listeners for presence entities so that
    # an away→home transition triggers all affected zones at once rather than
    # waiting up to 5 minutes for the next polling cycle.
    coordinator.async_setup_presence_listeners(entry)

    # Likewise for window contacts: opening/closing a window at night must drive
    # Option A (block/catch-up) and Option B (ventilation/return) promptly rather
    # than waiting for the next periodic cycle.
    coordinator.async_setup_contact_listeners(entry)

    # Time-based lifecycle boundaries (night start / morning release) must fire at
    # the configured minute, not up to a full periodic cycle later: schedule a
    # point-in-time timer at the next boundary.
    coordinator.async_setup_lifecycle_boundary_timer(entry)

    entry.runtime_data = SmartShadingRuntimeData(
        coordinator=coordinator,
        windows=coordinator.windows,
        zones=coordinator.zones,
        cover_groups=coordinator.cover_groups,
        covers={},
        global_defaults=coordinator.global_defaults,
        shade_position_defaults=coordinator.shade_position_defaults,
        assumed_state_manager=coordinator.assumed_state_manager,
        cover_controller=CoverController(TravelTracker(), coordinator.assumed_state_manager),
        learning_store=coordinator.learning_store,
        forecast_store=_fl_store,
        forecast_adapter=_fl_adapter,
        forecast_cancel=_fl_cancel,
        target_position_adapter=coordinator.target_position_adapter,
    )

    # Ensure the global SmartShading System entry exists.  On first-time setup
    # this triggers its creation.  On subsequent reloads it is a no-op.
    _ensure_system_entry(hass)

    await hass.config_entries.async_forward_entry_setups(entry, ZONE_PLATFORMS)
    return True
