"""DataUpdateCoordinator for SmartShading (ARCHITECTURE.md §9).

Drives the Tier 1-5 evaluation pipeline per window and per cycle:
  Tier 1 — Safety Guards           (StormEvaluator, WindEvaluator)
  Tier 2 — Manual Override         (ManualOverrideEvaluator + OverrideDetector)
  Tier 3 — Lifecycle Phase Gate    (NightEvaluator)
  Tier 4 — Protection Floors       (AbsenceEvaluator, HeatEvaluator, GlareEvaluator)
  Tier 5 — Comfort Pipeline        (SolarEvaluator)
  StateGuard — hysteresis          (bypasses_guard + StateGuard.is_locked)

build_window_decision_input() is the single config resolution boundary
(INV-18). No cover commands are sent in this phase; read-only output via
entities/sensor.py and entities/binary_sensor.py.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engines.forecast_persistence import ForecastPersistenceAdapter
    from .models.forecast_store import ForecastLearningStore

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .capability_detector import CapabilityDetector
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN
from .cover_control.assumed_state_manager import AssumedStateManager, confidence_level
from .cover_control.cover_capabilities import CoverCapability
from .cover_control.cover_controller import CoverController
from .engines.comfort_engine import ComfortEngine
from .engines.exposure_engine import ExposureEngine
from .engines.learning_persistence import (
    LearningPersistenceAdapter,
    LearningPersistenceConfig,
    PERSISTENCE_INTERVAL_MINUTES,
)
from .engines.learning_store import LearningStore, SNAPSHOT_CYCLE_INTERVAL
from .engines.outcome_resolution import (
    OutcomeResolutionInput,
    OutcomeResolutionTrigger,
    resolve_outcome,
)
from .engines.pending_outcome_queue import PendingOutcomeQueue
from .models.pending_outcome import PendingOutcome
from .engines.lifecycle_engine import LifecycleEngine, PresenceDebouncer, check_night_interval_active
from .engines.lifecycle_guard import lifecycle_should_break_override
from .models.learning import OverrideRecord, StateTransitionRecord, WindowCycleSnapshot
from .engines.override_detector import OverrideDetector
from .engines.observability_evaluator import (
    HYSTERESIS_THRESHOLDS,
    CoverPositionObservation,
    WindowObservation,
    build_next_action,
    build_reason,
    compute_learning_diagnostics,
)
from .engines.sun_engine import SunEngine, SunPosition
from .engines.weather_engine import WeatherCondition, WeatherEngine
from .models.comfort import ComfortConfig
from .models.config import ConfigResolver, GlobalDefaults, ShadePositionDefaults
from .models.cover_group import CoverGroup, CoverHardwareType, default_hardware_settings
from .models.lifecycle import LifecycleState, NightDayLifecycleConfig
from .models.window import WindowBehaviorMode, WindowConfig
from .models.zone import ZoneConfig
from .models.zone_execution_config import ZoneExecutionConfig
from .evaluators.tier_orchestrator import TierOrchestrator
from .models.window_decision import WindowDecision
from .models.window_decision_input import build_window_decision_input
from .state_machine.guards import StateGuard, StateGuardConfig
from .state_machine.states import ShadingState
from .state_machine.transitions import bypasses_guard
from .engines.adaptation_application import AdaptationTrace, apply_adaptive_profile
from .engines.forecast_strategy_modifier import (
    ForecastStrategyModifier,
    apply_forecast_modifier,
    compute_forecast_strategy_modifier,
)
from .engines.safety_hold import (
    HARDWARE_SAFE_POSITIONS as _HARDWARE_SAFE_POSITIONS,
    SafetyHold as _SafetyHold,
    WIND_HOLD_S as _WIND_HOLD_S,
    STORM_HOLD_S as _STORM_HOLD_S,
)
from .engines.sun_sector import azimuth_in_sector
from .engines.adaptation_layer import AdaptationInput, AdaptiveProfile, compute_adaptive_profile
from .engines.confidence_engine import ConfidenceInput, compute_confidence
from .engines.target_position_adapter import TargetPositionAdapter
from .engines.learning_signal_aggregator import LearningAggregateInput, aggregate_learning_signals
from .engines.override_learning import OverrideLearningInput, compute_override_learning
from .engines.similarity_pipeline import compute_similarity_result
from .engines.situation_joiner import SituationRecord, build_situations
from .engines.solar_impact_learning import SolarImpactInput, compute_solar_impact
from .cover_control.command_filter import (
    CommandFilter,
    CommandFilterResult,
    ExecutionCapability,
    ExecutionMode,
)
from .cover_control.cover_entity_snapshot import CoverEntitySnapshot, build_cover_entity_snapshot
from .cover_control.execution_plan import build_execution_plan
from .cover_control.execution_result import (
    ExecutionStatus,
    build_blocked_result,
    build_execution_plan_result,
    build_not_attempted_result,
)
from .cover_control.global_dispatch_throttle import GlobalDispatchThrottle, GlobalSerialDispatch
from .cover_control.ha_service_adapter import dispatch_cover_intent
from .cover_control.position_semantics import to_ha_position, to_internal_position
from .cover_control.daytime_min_open import (
    DAYTIME_CLAMP_EXEMPT_STATES,
    apply_daytime_min_open,
)
from .cover_control.anti_heat_buildup import (
    ANTI_HEAT_BUILDUP_EXEMPT_STATES,
    ANTI_HEAT_BUILDUP_MIN_EXPOSURE_WM2,
    apply_anti_heat_buildup,
)
from .cover_control.tilt_calculation import calculate_simple_tilt_target
from .cover_control.shading_group_harmonizer import (
    HarmonizationResult,
    ShadingGroupCandidate,
    compute_harmonization,
)
from .models.execution_diagnostics import WindowExecutionDiagnostics
from .const import DATA_DEBUG_LOGGING, DOMAIN

_LOGGER = logging.getLogger(__name__)

# States that never produce a PendingOutcome (safety tier + user action).
_NO_OUTCOME_STATES: frozenset[ShadingState] = frozenset({
    ShadingState.STORM_SAFE,
    ShadingState.WIND_SAFE,
    ShadingState.MANUAL_OVERRIDE,
})

# Minutes after a state decision before the outcome observation window closes.
# No config parameter exists yet — constant mirrors DecisionOutcome default.
_OUTCOME_OBSERVATION_DELAY_MIN: int = 30

# Returned by _run_learning_pipeline when the store has no data or an error occurs.
# All factors are 1.0 (neutral — no adjustment), learning is inactive.
_NEUTRAL_ADAPTIVE_PROFILE = AdaptiveProfile(
    learning_active=False,
    confidence_level="very_low",
    heat_sensitivity_factor=1.0,
    exposure_sensitivity_factor=1.0,
    preferred_shade_position_factor=1.0,
    solar_escalation_factor=1.0,
    adaptation_strength=0.0,
)

# ARCHITECTURE.md §4.3 minimum_state_duration defaults.
_DEFAULT_MINIMUM_STATE_DURATION = {
    ShadingState.LIGHT_SHADE: timedelta(minutes=10),
    ShadingState.NORMAL_SHADE: timedelta(minutes=10),
    ShadingState.STRONG_SHADE: timedelta(minutes=10),
}

# Startup Grace Period (Step 9G5): number of coordinator cycles after HA
# restart during which cover dispatch is suppressed.  Gives HA time to
# hydrate all entity states before SmartShading can move a cover.
# Safety exceptions (STORM_SAFE/WIND_SAFE) will bypass this in 9G5b/9G6.
STARTUP_GRACE_CYCLES: int = 3


@dataclass(frozen=True)
class _WeatherInputs:
    """One cycle's worth of optional weather/solar readings (2026-06-16
    weather-input round) - shared across all windows, since there is one
    weather source for the whole house."""

    outdoor_temperature: float | None
    solar_radiation: float | None
    cloud_cover: float | None
    wind_speed: float | None
    wind_gust: float | None                         # Step 7: from optional gust sensor
    weather_condition: str | None                   # raw HA state string (for WindowObservation)
    weather_condition_enum: WeatherCondition | None  # Step 7: parsed enum for Tier 1 evaluators


@dataclass
class SmartShadingData:
    """Per-cycle result of SmartShadingCoordinator._async_update_data()
    (ARCHITECTURE.md §9)."""

    window_results: dict[str, WindowObservation] = field(default_factory=dict)
    adaptive_profiles: dict[str, AdaptiveProfile] = field(default_factory=dict)
    adaptation_traces: dict[str, AdaptationTrace] = field(default_factory=dict)
    execution_diagnostics: dict[str, WindowExecutionDiagnostics] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=dt_util.utcnow)


@dataclass
class SmartShadingRuntimeData:
    """Stored on ConfigEntry.runtime_data (ARCHITECTURE.md §10).

    `comfort_profiles` / `lifecycle_configs` / `learning_store` /
    `state_history` / `explanation_log` from the documented §10 shape are
    intentionally not included yet - they depend on the Comfort Engine,
    Lifecycle Engine and Learning Engine, all out of scope for this phase.
    """

    coordinator: SmartShadingCoordinator
    windows: dict[str, WindowConfig]
    zones: dict[str, ZoneConfig]
    cover_groups: dict[str, CoverGroup]
    covers: dict[str, CoverCapability]
    global_defaults: GlobalDefaults
    shade_position_defaults: ShadePositionDefaults
    assumed_state_manager: AssumedStateManager
    cover_controller: CoverController
    # Learning Store — read-only public reference for global export.
    # Always present; coordinator initialises it unconditionally.
    learning_store: LearningStore
    # Forecast Learning (9F12k-7) — all three may be None when FL is inactive
    # or when setup failed; runtime code must not assume they are non-None.
    forecast_store: ForecastLearningStore
    forecast_adapter: ForecastPersistenceAdapter | None
    forecast_cancel: tuple[Callable[[], None], Callable[[], None]] | None
    # Step 6: per-window, per-intensity learned target position adapter.
    target_position_adapter: TargetPositionAdapter


@dataclass
class _WindowComputeState:
    """Per-window state collected in the first (synchronous) loop pass.

    Carries everything needed for ShadingGroup harmonization and the
    subsequent async dispatch pass.  Built after CommandFilter runs;
    consumed by _async_update_data's second loop.
    """

    window: WindowConfig
    zone: ZoneConfig
    obs_enabled: bool
    active_control_enabled: bool
    new_state: ShadingState
    exec_entity_id: str | None
    exec_cap: CoverCapability | None
    exec_snapshot: CoverEntitySnapshot | None
    exec_mode: ExecutionMode
    is_safety: bool
    exec_target_internal: int | None
    exec_filter_result: CommandFilterResult | None
    tier_decided_by: str | None
    # pre-tick override state — matches what CommandFilter saw this cycle.
    is_override_active: bool
    cover_available: bool | None
    # Daytime Minimum Open Position (Step 9G10f-b): clamp tracking.
    daytime_min_open_applied: bool = False
    pre_daytime_min_target_position_ha: int | None = None
    # Anti-Heat-Buildup (Step 9G10f-c): clamp tracking.
    anti_heat_buildup_applied: bool = False
    pre_anti_heat_buildup_target_position_ha: int | None = None
    # Combined floor for ShadingGroup harmonization (Steps 9G10f-b/c):
    # max(daytime_min_floor, ahb_floor) prevents the group from forcing
    # any window below its hardware-derived minimum open position.
    min_position_floor_ha: int = 0
    # Solar-sector gate (Step 7): windows outside their azimuth tolerance window
    # are excluded from harmonization — they must not be pulled into shade by
    # another window whose sun IS within sector.
    in_solar_sector: bool = True
    # Night Hard Hold: True when the hard-hold gate overrode the tier decision
    # to keep the cover at night_position instead of dispatching an OPEN command.
    night_hard_hold_applied: bool = False
    # Tilt target (Steps 9G10f-d/e): None for non-tilt covers and non-shading states.
    # Set by calculate_simple_tilt_target(); gated by exec_cap.supports_tilt
    # and tilt_control_enabled; only VENETIAN_BLIND covers produce a value.
    target_tilt_ha: int | None = None


class SmartShadingCoordinator(DataUpdateCoordinator[SmartShadingData]):
    """ARCHITECTURE.md §9, observability-only phase - see module docstring."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        windows: dict[str, WindowConfig] | None = None,
        zones: dict[str, ZoneConfig] | None = None,
        cover_groups: dict[str, CoverGroup] | None = None,
        global_defaults: GlobalDefaults | None = None,
        shade_position_defaults: ShadePositionDefaults | None = None,
        weather_entity_id: str | None = None,
        solar_radiation_sensor_id: str | None = None,
        outdoor_temperature_sensor_id: str | None = None,
        cloud_cover_sensor_id: str | None = None,
        wind_speed_sensor_id: str | None = None,
        wind_gust_sensor_id: str | None = None,
        storm_protection_enabled: bool = True,
        wind_protection_enabled: bool = False,
        wind_threshold_ms: float = 14.0,
        override_duration_min: int = 240,
        override_detection_tolerance: int = 10,
        override_break_on_lifecycle: bool = True,
        lifecycle_config: NightDayLifecycleConfig | None = None,
        presence_entity_ids: list[str] | None = None,
        absence_delay_min: int = 30,
        indoor_temperature_sensor_ids: list[str] | None = None,
        comfort_config: ComfortConfig | None = None,
        global_serial_dispatch: GlobalSerialDispatch | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )

        self.windows: dict[str, WindowConfig] = windows or {}
        self.zones: dict[str, ZoneConfig] = zones or {}
        self.cover_groups: dict[str, CoverGroup] = cover_groups or {}
        self.global_defaults: GlobalDefaults = global_defaults or GlobalDefaults()
        self.shade_position_defaults: ShadePositionDefaults = (
            shade_position_defaults or ShadePositionDefaults()
        )

        # Optional weather/solar sensors (2026-06-16 weather-input round).
        # All may be None - every read below tolerates that, plus a missing
        # or unavailable/unknown entity, without ever raising.
        self._weather_entity_id = weather_entity_id
        self._solar_radiation_sensor_id = solar_radiation_sensor_id
        self._outdoor_temperature_sensor_id = outdoor_temperature_sensor_id
        self._cloud_cover_sensor_id = cloud_cover_sensor_id
        self._wind_speed_sensor_id = wind_speed_sensor_id
        self._wind_gust_sensor_id = wind_gust_sensor_id
        self._storm_protection_enabled = storm_protection_enabled
        self._wind_protection_enabled = wind_protection_enabled
        self._wind_threshold_ms = wind_threshold_ms
        self._override_duration_min = override_duration_min
        self._override_detection_tolerance = override_detection_tolerance
        self._override_break_on_lifecycle = override_break_on_lifecycle

        self.sun_engine = SunEngine()
        self.exposure_engine = ExposureEngine()
        self.weather_engine = WeatherEngine()

        # Capability Detection / Position Awareness (2026-06-16). Owned
        # here (not __init__.py) so SmartShadingRuntimeData can simply
        # reference coordinator.assumed_state_manager - same "coordinator
        # owns it, runtime_data just points at it" pattern already used
        # for windows/zones/cover_groups.
        self._capability_detector = CapabilityDetector()
        self._cover_capabilities: dict[str, CoverCapability] = {}
        self.assumed_state_manager = AssumedStateManager()

        self._current_states: dict[str, ShadingState] = {}
        self._override_detector = OverrideDetector()

        # Phase 9C/9D: learning collection + persistence.
        # LearningStore capacities are persistence-aligned (not the smaller defaults)
        # so the ring buffers can hold the full retention window between restarts.
        self._learning_store = LearningStore(
            transitions_capacity=5000,
            overrides_capacity=1000,
            snapshots_capacity=2000,
            outcomes_capacity=5000,
        )
        # Phase 9D: persistence adapter — wraps hass.storage.Store.
        # HA import deferred inside LearningPersistenceAdapter.__init__; safe here.
        self._learning_persistence = LearningPersistenceAdapter(
            hass, LearningPersistenceConfig(), config_entry.entry_id
        )
        self._learning_restored: bool = False
        # Forecast Learning Store reference — injected via set_forecast_store()
        # after the coordinator is constructed (the store is set up separately in
        # async_setup_entry and passed in before the first refresh cycle).
        # None = no forecast data available; modifier computation is skipped.
        self._forecast_learning_store: object | None = None
        # Timestamp of the last periodic learning persistence save.
        # None until the first save occurs.  Used for the elapsed-time check:
        # SmartShading persists pending learning changes at least hourly and
        # on important learning events (override signals, outcome resolution).
        self._persistence_last_save_at: datetime | None = None
        # Set True on important learning events (override signals, outcome resolution)
        # so a save is triggered at the end of that cycle instead of waiting for
        # the next hourly interval.
        self._learning_dirty: bool = False
        # Step 6: per-window, per-intensity learned target position adapter.
        self._target_position_adapter = TargetPositionAdapter()
        # Phase 9F4b-3: RAM-only pending outcome queue (no persistence, no restore).
        self._pending_outcomes = PendingOutcomeQueue()
        # Per-window override state from the previous cycle — used to detect
        # natural expiry ("expired") and renewal ("renewed") events.
        self._prev_overrides: dict[str, object] = {}
        # Per-window cycle counter for periodic snapshot scheduling.
        self._snapshot_counters: dict[str, int] = {}
        self.guard = StateGuard(
            StateGuardConfig(
                minimum_state_duration=dict(_DEFAULT_MINIMUM_STATE_DURATION),
                hysteresis=dict(HYSTERESIS_THRESHOLDS),
            )
        )
        self._tier_orchestrator = TierOrchestrator()

        # Comfort Engine (2026-06-17). Both fields are optional - old
        # ConfigEntries without comfort keys get the documented defaults,
        # same backward-compatibility pattern as the weather inputs above.
        self._indoor_temperature_sensor_ids: list[str] = indoor_temperature_sensor_ids or []
        self._comfort_config: ComfortConfig = comfort_config or ComfortConfig()

        # Lifecycle Engine (2026-06-16). `lifecycle_config`/`presence_entity_ids`
        # are not collected by the Config Flow yet (deferred, see final
        # report) - sensible hardcoded defaults for now, same approach
        # already used for ShadePositionDefaults.
        self.lifecycle_engine = LifecycleEngine()
        self._lifecycle_config: NightDayLifecycleConfig = lifecycle_config or NightDayLifecycleConfig(
            id="default"
        )
        self._lifecycle_state: LifecycleState = LifecycleState.DAY
        self._presence_entity_ids: list[str] = presence_entity_ids or []
        self._absence_delay_min: int = absence_delay_min
        self._presence_debouncer = PresenceDebouncer()
        # Learning Loop Closure (9F15): per-window AdaptiveProfile cache.
        # Updated each sun-path cycle; carries the last computed profile for
        # windows that hit the no-sun path.
        self._adaptive_profiles: dict[str, AdaptiveProfile] = {}
        # Adaptation Application (9F17): per-window AdaptationTrace cache.
        # Populated in the sun path alongside the WDI adaptation call.
        self._adaptation_traces: dict[str, AdaptationTrace] = {}
        # Startup Grace Period (9G5): suppress dispatch for the first
        # STARTUP_GRACE_CYCLES cycles after HA restart.  Prevents unexpected
        # cover movement while entity states hydrate.  Safety exceptions will
        # be handled in Step 9G5b/9G6 when actual dispatch is enabled.
        self._startup_cycles_remaining: int = STARTUP_GRACE_CYCLES
        # Serial dispatch (Step 10): shared asyncio.Lock + throttle across ALL
        # zone coordinators so cover commands from different zones are serialised.
        # When a GlobalSerialDispatch is provided (normal production path via
        # hass.data[DOMAIN]), we use its shared lock and throttle.  If None
        # (test fallback), we create a per-coordinator lock + throttle — still
        # sequential within this zone, but not across zones.
        if global_serial_dispatch is not None:
            self._serial_dispatch: GlobalSerialDispatch = global_serial_dispatch
        else:
            self._serial_dispatch = GlobalSerialDispatch()

        # Keep a reference to the (now-deprecated) per-coordinator throttle name
        # so that callers/tests that reference _global_dispatch_throttle still
        # resolve to the throttle object inside the serial dispatch.
        self._global_dispatch_throttle = self._serial_dispatch

        # Zone control overrides (Step 9G11): zone switch entities write here;
        # effective_zone_execution() reads them with fallback to zone.execution.
        # Populated from config_entry.options["zone_controls"] so values survive restart.
        self._zone_execution_overrides: dict[str, ZoneExecutionConfig] = {}

        # Wind/Storm release-hysteresis holds (Part 3 — Wind Debounce).
        # After the safety evaluator fires, the latch persists for _*_HOLD_S
        # seconds even if wind drops between scan cycles.  Per-window because
        # wind thresholds can differ (though in practice they share one sensor).
        self._wind_holds: dict[str, _SafetyHold] = {}
        self._storm_holds: dict[str, _SafetyHold] = {}

        _zone_controls_raw = config_entry.options.get("zone_controls", {})
        # Defensive: a corrupted/old options blob may store None or a non-dict
        # here (or per-zone non-dict entries).  Never crash setup on stored
        # data — fall back to the safe defaults (observation on, control off).
        if isinstance(_zone_controls_raw, dict):
            for _zone_id, _ctrl in _zone_controls_raw.items():
                if not isinstance(_ctrl, dict):
                    continue
                self._zone_execution_overrides[_zone_id] = ZoneExecutionConfig(
                    observation_enabled=_ctrl.get("observation_enabled", True),
                    active_control_enabled=_ctrl.get("active_control_enabled", False),
                )

    @property
    def _debug_logging_enabled(self) -> bool:
        """Return True when the system-level debug logging switch is active.

        Reads from hass.data[DOMAIN][DATA_DEBUG_LOGGING] — a bool set by the
        DebugLoggingSwitch entity and propagated by async_setup_system_entry.
        Defaults to False when the key is absent (no system entry loaded yet,
        or system entry was not set up before the first zone cycle).
        """
        return bool(
            self.hass.data.get(DOMAIN, {}).get(DATA_DEBUG_LOGGING, False)
        )

    @property
    def learning_store(self) -> LearningStore:
        """Public read access to the in-memory LearningStore for this zone entry.

        Used by the global learning export (button entity) to collect per-entry
        data without accessing private attributes.
        """
        return self._learning_store

    @property
    def target_position_adapter(self) -> TargetPositionAdapter:
        """Public read access to the TargetPositionAdapter for this zone entry.

        Used by the recommendation sensor for diagnostic attributes and by
        the global learning export for aggregate target adaptation summaries.
        """
        return self._target_position_adapter

    def set_forecast_store(self, store: object) -> None:
        """Inject the ForecastLearningStore after coordinator construction.

        Called by async_setup_entry after the store is restored from
        persistence.  Must be called before async_config_entry_first_refresh()
        to make the forecast modifier available on the first update cycle.
        """
        self._forecast_learning_store = store

    async def async_flush_learning(self) -> None:
        """Flush any pending learning data to persistent storage immediately.

        Called by async_unload_entry to avoid losing data accumulated since the
        last periodic save.  Swallows all errors so unload is never blocked.
        """
        now = dt_util.utcnow()
        await self._learning_persistence.async_save(
            self._learning_store,
            set(self.windows.keys()),
            now,
            target_adapter=self._target_position_adapter,
        )
        self._learning_dirty = False

    # ------------------------------------------------------------------
    # Zone execution config API (Step 9G11)
    # ------------------------------------------------------------------

    def effective_zone_execution(self, zone_id: str) -> ZoneExecutionConfig:
        """Return the current (possibly switch-overridden) ZoneExecutionConfig.

        Priority: runtime switch override > zone.execution from config.
        """
        override = self._zone_execution_overrides.get(zone_id)
        if override is not None:
            return override
        zone = self.zones.get(zone_id)
        if zone is not None:
            return zone.execution
        return ZoneExecutionConfig()

    async def async_set_zone_observation_enabled(
        self, zone_id: str, enabled: bool
    ) -> None:
        """Toggle observation_enabled for a zone; persist and refresh."""
        current = self.effective_zone_execution(zone_id)
        self._zone_execution_overrides[zone_id] = ZoneExecutionConfig(
            observation_enabled=enabled,
            active_control_enabled=current.active_control_enabled,
        )
        self._persist_zone_controls()
        await self.async_request_refresh()

    async def async_set_zone_active_control_enabled(
        self, zone_id: str, enabled: bool
    ) -> None:
        """Toggle active_control_enabled for a zone; persist and refresh."""
        current = self.effective_zone_execution(zone_id)
        self._zone_execution_overrides[zone_id] = ZoneExecutionConfig(
            observation_enabled=current.observation_enabled,
            active_control_enabled=enabled,
        )
        if enabled:
            # When Active Control is turned ON, clear any existing manual
            # override AND suppress detection for the first cycle.
            #
            # WHY clear(): while AC was off, the override detector still ran
            # every coordinator cycle (there is no AC gate on tick()).  If the
            # cover was at a position that differed from SmartShading's target,
            # a ManualOverride was written to _active_overrides.  That stale
            # override would survive suppress_next_override_tick() — which only
            # prevents NEW detection; it does not touch _active_overrides — and
            # would be returned by detector.get() on the very first AC-on cycle,
            # causing the shading-state sensor to show MANUAL_OVERRIDE
            # immediately after the user enables AC.
            #
            # WHY suppress(): after clear(), the cover is likely still at a
            # non-target position.  On the first cycle SmartShading will issue a
            # move command but the cover has not moved yet.  Without suppress(),
            # the delta on that cycle would be detected as a NEW override.  The
            # one-shot suppression bridges that gap.
            for window_id, window in self.windows.items():
                if window.zone_id == zone_id:
                    self._override_detector.clear(window_id)
                    self._override_detector.suppress_next_override_tick(window_id)
        self._persist_zone_controls()
        await self.async_request_refresh()

    def _persist_zone_controls(self) -> None:
        """Write current zone execution overrides into config_entry.options."""
        zone_controls = {
            zone_id: {
                "observation_enabled": cfg.observation_enabled,
                "active_control_enabled": cfg.active_control_enabled,
            }
            for zone_id, cfg in self._zone_execution_overrides.items()
        }
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            options={**self.config_entry.options, "zone_controls": zone_controls},
        )

    def _get_or_detect_capability(self, cover_entity_id: str) -> CoverCapability:
        """Detect once per cover entity and cache. Capabilities essentially
        never change at runtime - the one known gap is a cover that was
        unavailable at first detection (see final report's open risks);
        not retried automatically yet."""
        capability = self._cover_capabilities.get(cover_entity_id)
        if capability is None:
            capability = self._capability_detector.detect(self.hass, cover_entity_id)
            self._cover_capabilities[cover_entity_id] = capability
        return capability

    def _read_current_position(self, cover_entity_id: str) -> int | None:
        """Aufgabe 2: never raises - missing entity, missing attribute,
        `unknown`/`unavailable`, or a non-numeric value all become None.
        """
        state = self.hass.states.get(cover_entity_id)
        if state is None:
            return None
        value = WeatherEngine.parse_numeric_state(state.attributes.get("current_position"))
        if value is None:
            return None
        return max(0, min(100, round(value)))

    def _read_presence(self) -> bool:
        """Lifecycle Engine round, Aufgabe 3: True if at least one
        configured `person.*` entity is "home". Never raises - a missing
        entity or one in `unknown`/`unavailable` is simply skipped. If
        none are configured, or none could be read at all, the safe
        default is "present" (never falsely trigger absence from missing
        data).
        """
        if not self._presence_entity_ids:
            return True

        any_usable_reading = False
        for entity_id in self._presence_entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            any_usable_reading = True
            if state.state == "home":
                return True

        if not any_usable_reading:
            return True  # nothing readable at all - safe default: present

        return False  # every readable person reported a non-"home" state

    def _read_indoor_temperature(self) -> float | None:
        """Read indoor temperature as average of all configured sensors.
        Returns None when no sensors are configured or all readings are invalid.
        Never raises: missing, unknown, unavailable, or non-numeric → skipped.
        """
        if not self._indoor_temperature_sensor_ids:
            return None
        readings: list[float] = []
        for sensor_id in self._indoor_temperature_sensor_ids:
            state = self.hass.states.get(sensor_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            value = WeatherEngine.parse_numeric_state(state.state)
            if value is not None:
                readings.append(value)
        if not readings:
            return None
        return sum(readings) / len(readings)

    def _build_cover_position_observation(self, window: WindowConfig, now: datetime) -> CoverPositionObservation:
        cover_group = self.cover_groups.get(window.cover_group_id)
        if cover_group is None or not cover_group.cover_ids:
            return CoverPositionObservation.unknown()

        # Simplification: one representative cover per window
        # (the first cover_id in its CoverGroup). Multi-cover divergence
        # within a SYNCHRONOUS group is an accepted open question
        # (ARCHITECTURE.md §12), not solved in this round.
        cover_entity_id = cover_group.cover_ids[0]
        capability = self._get_or_detect_capability(cover_entity_id)
        actual_position = self._read_current_position(cover_entity_id)

        # Aufgabe 3: passive observation only - observe() never increments
        # position_uncertainty_pct, unlike update() (reserved for once
        # SmartShading itself sends a command).
        if actual_position is not None:
            # AssumedStateManager uses internal convention (0=open, 100=shaded).
            # actual_position is raw HA attribute → convert before storing.
            self.assumed_state_manager.observe(
                cover_entity_id,
                to_internal_position(actual_position, invert=capability.invert_position),
                now,
                capability.has_reliable_position_feedback,
            )

        assumed_state = self.assumed_state_manager.get_state(cover_entity_id, now)
        assumed_position_internal = assumed_state.assumed_position if assumed_state is not None else None

        # Convert assumed_position from internal→HA (standard convention, invert=False)
        # for display. The Cover Position sensor always shows standard HA convention.
        assumed_position_ha = (
            to_ha_position(assumed_position_internal, invert=False)
            if assumed_position_internal is not None else None
        )

        if actual_position is not None:
            best_known_position = actual_position
            position_source = "actual"
        elif assumed_position_ha is not None:
            best_known_position = assumed_position_ha
            position_source = "assumed"
        else:
            best_known_position = None
            position_source = "unknown"

        confidence = assumed_state.confidence if assumed_state is not None else None
        return CoverPositionObservation(
            actual_position=actual_position,
            assumed_position=assumed_position_ha,
            best_known_position=best_known_position,
            position_source=position_source,
            position_confidence=confidence,
            position_confidence_level=confidence_level(confidence) if confidence is not None else None,
            position_uncertainty_pct=(
                assumed_state.position_uncertainty_pct if assumed_state is not None else None
            ),
            capability_type=capability.cover_profile.value,
            supports_position=capability.supports_position,
            supports_stop=capability.supports_stop,
            supports_open=capability.supports_open,
            supports_close=capability.supports_close,
            assumed_position_required=not capability.has_reliable_position_feedback,
        )

    def _build_cover_entity_snapshot_for_window(
        self,
        window: WindowConfig,
        now: datetime,
    ) -> tuple[str | None, CoverCapability | None, CoverEntitySnapshot | None]:
        """Build a CoverEntitySnapshot from live HA state for the window's first cover.

        Returns (cover_entity_id, capability, snapshot).
        All three are None when the window has no cover group or no covers.
        Uses the existing capability cache (_get_or_detect_capability) and
        AssumedStateManager — no additional HA reads beyond hass.states.get.

        Convention note:
          snapshot.current_position_ha   — HA convention (0=closed, 100=open)
          snapshot.current_position_internal — SmartShading internal (0=open, 100=shaded)
          snapshot.assumed_position_internal — SmartShading internal (from AssumedStateManager)
        """
        cover_group = self.cover_groups.get(window.cover_group_id)
        if cover_group is None or not cover_group.cover_ids:
            return None, None, None

        cover_entity_id = cover_group.cover_ids[0]
        capability = self._get_or_detect_capability(cover_entity_id)

        state_obj = self.hass.states.get(cover_entity_id)
        state_str = state_obj.state if state_obj is not None else None
        attributes = dict(state_obj.attributes) if state_obj is not None else {}

        assumed_state = self.assumed_state_manager.get_state(cover_entity_id, now)
        assumed_pos = assumed_state.assumed_position if assumed_state is not None else None

        snapshot = build_cover_entity_snapshot(
            entity_id=cover_entity_id,
            state=state_str,
            attributes=attributes,
            invert=capability.invert_position,
            has_reliable_position_feedback=capability.has_reliable_position_feedback,
            assumed_position_internal=assumed_pos,
        )
        return cover_entity_id, capability, snapshot

    def _read_value(self, sensor_entity_id: str | None, weather_attribute: str | None) -> float | None:
        """Dedicated sensor > weather-entity attribute > None
        (ARCHITECTURE.md §5.3 "Multi-tier sensor fallback"). Never raises:
        a missing entity, or one in `unknown`/`unavailable`, is simply
        treated as "no value yet".
        """
        if sensor_entity_id is not None:
            state = self.hass.states.get(sensor_entity_id)
            if state is not None:
                value = WeatherEngine.parse_numeric_state(state.state)
                if value is not None:
                    return value

        if weather_attribute is not None and self._weather_entity_id is not None:
            weather_state = self.hass.states.get(self._weather_entity_id)
            if weather_state is not None:
                value = WeatherEngine.parse_numeric_state(weather_state.attributes.get(weather_attribute))
                if value is not None:
                    return value

        return None

    def _read_weather_inputs(self) -> _WeatherInputs:
        weather_condition: str | None = None
        if self._weather_entity_id is not None:
            weather_state = self.hass.states.get(self._weather_entity_id)
            if weather_state is not None and weather_state.state not in ("unknown", "unavailable"):
                weather_condition = weather_state.state

        return _WeatherInputs(
            outdoor_temperature=self._read_value(self._outdoor_temperature_sensor_id, "temperature"),
            solar_radiation=self._read_value(self._solar_radiation_sensor_id, None),
            cloud_cover=self._read_value(self._cloud_cover_sensor_id, "cloud_coverage"),
            wind_speed=self._read_value(self._wind_speed_sensor_id, "wind_speed"),
            wind_gust=self._read_value(self._wind_gust_sensor_id, "wind_gust_speed"),
            weather_condition=weather_condition,
            weather_condition_enum=WeatherEngine.parse_weather_condition(weather_condition),
        )

    async def _async_update_data(self) -> SmartShadingData:
        # Phase 9D: restore persisted learning data on the very first cycle.
        # Any failure in async_restore is caught internally — safe to await here.
        if not self._learning_restored:
            _restore_now = dt_util.utcnow()
            self._target_position_adapter = await self._learning_persistence.async_restore(
                self._learning_store, _restore_now
            )
            self._learning_restored = True
            # Write a schema-valid storage file immediately on first setup so
            # /config/.storage/smartshading_learning_<id> is visible right away,
            # even before any learning data has been collected.
            if self._learning_persistence.fresh_start:
                await self._learning_persistence.async_save(
                    self._learning_store,
                    set(self.windows.keys()),
                    _restore_now,
                    target_adapter=self._target_position_adapter,
                )
                self._persistence_last_save_at = _restore_now

        sun_state = self.hass.states.get("sun.sun")
        sun_position: SunPosition | None = None
        if sun_state is not None:
            try:
                sun_position = SunPosition(
                    azimuth=float(sun_state.attributes["azimuth"]),
                    elevation=float(sun_state.attributes["elevation"]),
                )
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("sun.sun has no usable azimuth/elevation attributes yet")

        weather_inputs = self._read_weather_inputs()

        # Comfort Engine round (2026-06-17): read indoor temperature once per
        # cycle (house-wide, not per-window). is_in_solar_sector is per-window
        # and is computed inside the loop below.
        indoor_temperature = self._read_indoor_temperature()

        now = dt_util.utcnow()
        local_now = dt_util.now()

        # Forecast Strategy Modifier (v1.0): compute once per cycle from the
        # live ForecastLearningStore.  Returns a no-op modifier when data is
        # unavailable, trust is insufficient, or no current forecast exists.
        # Applied per-window AFTER AdaptationApplication (see window loop below).
        _forecast_modifier: ForecastStrategyModifier | None = None
        if self._forecast_learning_store is not None:
            try:
                _forecast_modifier = compute_forecast_strategy_modifier(
                    self._forecast_learning_store,  # type: ignore[arg-type]
                    now,
                )
            except Exception:
                _forecast_modifier = None

        # Lifecycle Engine (2026-06-16): only recomputed when sun_position
        # is actually available this cycle - night_fixed_time-only configs
        # could technically still be evaluated without it, but keeping the
        # previous lifecycle_state unchanged on missing elevation is the
        # safer, simpler choice (never guess from absent data).
        _prev_lifecycle_state = self._lifecycle_state
        if sun_position is not None:
            self._lifecycle_state = self.lifecycle_engine.get_lifecycle_state(
                local_now, sun_position.elevation, self._lifecycle_config, self._lifecycle_state
            )

        # Night Hard Hold: pre-computed once per cycle for O(1) per-window check.
        # Dual condition: cached lifecycle state OR independent fresh evaluation.
        # The independent check catches stale-state cases (first cycle after restart
        # when cached state is still DAY) and windows using ABSENCE_ONLY behavior
        # mode (their WDI has lifecycle_state forced to DAY, defeating NightEvaluator).
        # Safety and Manual Override are exempt — they are checked per-window.
        _night_interval_active: bool = (
            self._lifecycle_state is LifecycleState.NIGHT
            or (
                self._lifecycle_config.night_enabled
                and sun_position is not None
                and check_night_interval_active(
                    local_now, sun_position.elevation, self._lifecycle_config
                )
            )
        )

        # Presence/absence (2026-06-16) does not depend on sun data at all.
        presence_present = self._read_presence()
        absence_active = self._presence_debouncer.is_absence_active(
            presence_present, now, self._absence_delay_min
        )

        window_results: dict[str, WindowObservation] = {}
        execution_diagnostics: dict[str, WindowExecutionDiagnostics] = {}
        # Intermediate state from pass 1, consumed by the dispatch pass below.
        _window_states: dict[str, _WindowComputeState] = {}

        for window_id, window in self.windows.items():
            # Resolve zone and execution config at the very top of the loop so
            # obs_enabled is available for both the pre-sun and sun paths.
            # effective_zone_execution() applies any runtime switch override (Step 9G11).
            zone = self.zones.get(window.zone_id, ZoneConfig(id=window.zone_id, name=window.zone_id))
            _exec = self.effective_zone_execution(window.zone_id)
            obs_enabled = _exec.observation_enabled

            # Capability/Position observation (2026-06-16) is independent
            # of sun data - a cover's position is worth reading even when
            # sun.sun is temporarily unavailable.
            cover_position = self._build_cover_position_observation(window, now)

            # Phase 9C: snapshot prev override state for event detection this cycle.
            _prev_override = self._prev_overrides.get(window_id)

            # Override expiry check runs before WDI construction so the WDI
            # always carries a fresh (or None) active_override this cycle.
            active_override = self._override_detector.get(window_id, now)

            # Phase 9C: detect natural expiry (detector.get() returned None
            # because expires_at < now, not because of an explicit clear).
            if obs_enabled and _prev_override is not None and active_override is None:
                try:
                    self._learning_store.record_override(OverrideRecord(
                        timestamp=now,
                        window_id=window_id,
                        event_type="expired",
                        lifecycle_state=self._lifecycle_state.value,
                        override_position=_prev_override.override_position,  # type: ignore[union-attr]
                        overridden_state=_prev_override.overridden_state,  # type: ignore[union-attr]
                        overridden_position=_prev_override.overridden_position,  # type: ignore[union-attr]
                        override_duration_min=(
                            (_prev_override.expires_at - _prev_override.started_at).total_seconds() / 60  # type: ignore[union-attr]
                        ),
                        outdoor_temp_c=weather_inputs.outdoor_temperature,
                        solar_radiation_wm2=weather_inputs.solar_radiation,
                        sun_azimuth=sun_position.azimuth if sun_position is not None else None,
                        sun_elevation=sun_position.elevation if sun_position is not None else None,
                        solar_relative_azimuth=(
                            sun_position.azimuth - window.azimuth if sun_position is not None else None
                        ),
                        weather_condition=weather_inputs.weather_condition,
                        cloud_cover_pct=weather_inputs.cloud_cover,
                        raw_solar_radiation_wm2=None,   # exposure not yet computed (pre-sun-branch)
                        effective_exposure_wm2=None,
                        learned_solar_impact_factor=None,
                        decided_by=None,  # tier_decision not available (pre-sun-branch)
                    ))
                except Exception:
                    _LOGGER.warning("Learning: override 'expired' record failed for %s", window_id)

                # Step 6: record position preference signal for target adaptation.
                try:
                    _overridden_state_str = (
                        _prev_override.overridden_state.value  # type: ignore[union-attr]
                        if _prev_override.overridden_state is not None  # type: ignore[union-attr]
                        else ""
                    )
                    self._target_position_adapter.record_override_signal(
                        window_id=window_id,
                        overridden_state_str=_overridden_state_str,
                        override_position_internal=_prev_override.override_position,  # type: ignore[union-attr]
                        overridden_position_internal=_prev_override.overridden_position,  # type: ignore[union-attr]
                        duration_min=(
                            (_prev_override.expires_at - _prev_override.started_at).total_seconds() / 60  # type: ignore[union-attr]
                        ),
                        now=now,
                    )
                    self._learning_dirty = True
                except Exception:
                    pass  # never block the update cycle

            # Step 8c: lifecycle transition clears active override so the new
            # phase takes effect immediately without waiting for expiry.
            if lifecycle_should_break_override(
                prev=_prev_lifecycle_state,
                new=self._lifecycle_state,
                break_enabled=self._override_break_on_lifecycle,
            ) and active_override is not None:
                # Phase 9C: record lifecycle clear before removing the override.
                # Learning write is gated — functional clear always runs.
                if obs_enabled:
                    try:
                        self._learning_store.record_override(OverrideRecord(
                            timestamp=now,
                            window_id=window_id,
                            event_type="cleared_by_lifecycle",
                            lifecycle_state=self._lifecycle_state.value,
                            override_position=active_override.override_position,
                            overridden_state=active_override.overridden_state,
                            overridden_position=active_override.overridden_position,
                            override_duration_min=(
                                (now - active_override.started_at).total_seconds() / 60
                            ),
                            outdoor_temp_c=weather_inputs.outdoor_temperature,
                            solar_radiation_wm2=weather_inputs.solar_radiation,
                            sun_azimuth=sun_position.azimuth if sun_position is not None else None,
                            sun_elevation=sun_position.elevation if sun_position is not None else None,
                            solar_relative_azimuth=(
                                sun_position.azimuth - window.azimuth if sun_position is not None else None
                            ),
                            weather_condition=weather_inputs.weather_condition,
                            cloud_cover_pct=weather_inputs.cloud_cover,
                            raw_solar_radiation_wm2=None,   # exposure not yet computed (pre-sun-branch)
                            effective_exposure_wm2=None,
                            learned_solar_impact_factor=None,
                            decided_by=None,  # tier_decision not available (pre-sun-branch)
                        ))
                    except Exception:
                        _LOGGER.warning("Learning: override 'cleared_by_lifecycle' record failed for %s", window_id)

                    # Step 6: record position preference signal for target adaptation.
                    try:
                        _lc_state_str = (
                            active_override.overridden_state.value
                            if active_override.overridden_state is not None
                            else ""
                        )
                        self._target_position_adapter.record_override_signal(
                            window_id=window_id,
                            overridden_state_str=_lc_state_str,
                            override_position_internal=active_override.override_position,
                            overridden_position_internal=active_override.overridden_position,
                            duration_min=(now - active_override.started_at).total_seconds() / 60,
                            now=now,
                        )
                        self._learning_dirty = True
                    except Exception:
                        pass  # never block the update cycle

                self._override_detector.clear(window_id)
                active_override = None

            # Phase 9C: persist override state for the no-sun path; overwritten
            # with current_override at end of the normal path below.
            self._prev_overrides[window_id] = active_override

            # Phase 9F4b-3: lifecycle state change resolves any active pending outcome.
            # Gated behind obs_enabled: pending outcomes only exist when obs_enabled=True.
            if obs_enabled and _prev_lifecycle_state != self._lifecycle_state:
                _lc_pending = self._pending_outcomes.remove(window_id)
                if _lc_pending is not None:
                    try:
                        _outcome = resolve_outcome(
                            _lc_pending,
                            OutcomeResolutionInput(
                                trigger=OutcomeResolutionTrigger.LIFECYCLE,
                                resolution_timestamp=now,
                                indoor_temp_outcome_c=indoor_temperature,
                            ),
                        )
                        self._learning_store.record_outcome(_outcome)
                    except Exception:
                        _LOGGER.warning(
                            "Learning: outcome resolution (lifecycle) failed for %s", window_id
                        )

            # Phase 9E: learning diagnostics — computed once per window per cycle,
            # used in both the no-sun path and the normal path below.
            _learn_diag = compute_learning_diagnostics(self._learning_store, window_id, now)

            if sun_position is None:
                window_results[window_id] = WindowObservation.unavailable(
                    self._current_states.get(window_id, ShadingState.OPEN),
                    cover_position=cover_position,
                    lifecycle_state=self._lifecycle_state.value,
                    night_active=self._lifecycle_state is LifecycleState.NIGHT,
                    absence_active=absence_active,
                    override_active=active_override is not None,
                    override_expires_at=active_override.expires_at if active_override is not None else None,
                    override_source=active_override.source if active_override is not None else None,
                    override_position=(
                        100 - active_override.override_position
                        if active_override is not None else None
                    ),
                    learning_diagnostics=_learn_diag,
                )
                # Minimal execution diagnostics for the no-sun path.
                _no_sun_exec_mode = (
                    ExecutionMode.AUTOMATIC.value
                    if _exec.active_control_enabled
                    else ExecutionMode.RECOMMENDATION_ONLY.value
                )
                execution_diagnostics[window_id] = WindowExecutionDiagnostics(
                    observation_enabled=obs_enabled,
                    active_control_enabled=_exec.active_control_enabled,
                    execution_mode=_no_sun_exec_mode,
                    cover_entity_id=None,
                    cover_available=None,
                    actual_position_ha=None,
                    actual_position_internal=None,
                    assumed_position_internal=None,
                    has_position_feedback=None,
                    tier_decided_by=None,
                    target_position_internal=None,
                    target_position_ha=None,
                    is_safety=False,
                    command_allowed=None,
                    command_blocked_reason=None,
                    last_command_status=None,
                    last_command_sent_at=None,
                    service_call_sent=False,
                    service_call_failed=False,
                    execution_error=None,
                    safety_result_failed=False,
                    dispatch_suppressed_reason=None,
                    dispatch_throttled=False,
                    throttle_wait_ms=None,
                )
                continue

            # zone is already resolved at the top of the loop.
            tolerance_start = ConfigResolver.resolve(window, zone, self.global_defaults, "tolerance_start")
            tolerance_end = ConfigResolver.resolve(window, zone, self.global_defaults, "tolerance_end")

            sun_geometry = self.sun_engine.calculate(
                sun_position=sun_position,
                window_azimuth=window.azimuth,
                tolerance_start=tolerance_start,
                tolerance_end=tolerance_end,
                floor_level=window.floor_level,
                overhang_depth_m=window.overhang_depth_m,
            )
            # Dedicated solar radiation sensor (if configured) is used
            # as-is; otherwise fall back to the geometry/cloud-cover-based
            # estimate (ARCHITECTURE.md §5.3).  Track which source was used
            # so the trace fields in WindowObservation are accurate.
            if weather_inputs.solar_radiation is not None:
                effective_radiation = weather_inputs.solar_radiation
                _solar_source = "sensor"
            else:
                effective_radiation = self.weather_engine.calculate_effective_radiation(
                    sun_elevation_deg=sun_position.elevation,
                    cloud_cover_pct=weather_inputs.cloud_cover or 0.0,
                )
                _solar_source = "estimate"
            exposure = self.exposure_engine.calculate(
                sun_geometry=sun_geometry,
                effective_solar_radiation_wm2=effective_radiation,
                window_id=window_id,
                timestamp=now,
            )

            current_state = self._current_states.get(window_id, ShadingState.OPEN)

            # Effective solar sector: start from automatic geometry, then apply
            # per-window manual sector override and obstruction zones.
            _effective_in_solar_sector = sun_geometry.is_in_tolerance_window
            _manual_sun_sector_active = False
            _obstruction_blocked = False

            # Manual sun sector override: replaces the automatic azimuth sector
            # for this window when both start and end degrees are configured.
            if (
                window.manual_sun_sector_start_deg is not None
                and window.manual_sun_sector_end_deg is not None
            ):
                _manual_sun_sector_active = True
                _effective_in_solar_sector = (
                    sun_geometry.is_above_horizon
                    and azimuth_in_sector(
                        sun_position.azimuth,
                        window.manual_sun_sector_start_deg,
                        window.manual_sun_sector_end_deg,
                    )
                )

            # Obstruction zones: if sun is inside an enabled obstruction zone's
            # azimuth range AND inside its optional elevation range, exposure is
            # blocked.  OR-style: any single blocking zone suppresses direct solar
            # exposure.  elevation_blocks() handles the optional range semantics —
            # both bounds None = blocks at every elevation inside the azimuth range.
            if _effective_in_solar_sector and window.obstruction_zones:
                for _oz in window.obstruction_zones:
                    if not _oz.enabled:
                        continue
                    if (
                        _oz.elevation_blocks(sun_position.elevation)
                        and azimuth_in_sector(
                            sun_position.azimuth,
                            _oz.azimuth_start_deg,
                            _oz.azimuth_end_deg,
                        )
                    ):
                        _effective_in_solar_sector = False
                        _obstruction_blocked = True
                        break

            # Comfort Engine round (2026-06-17): per-window assessment because
            # is_in_solar_sector depends on the window's azimuth tolerance.
            # outdoor/indoor temperatures are house-wide (computed outside loop).
            comfort_assessment = ComfortEngine.assess(
                outdoor_temp=weather_inputs.outdoor_temperature,
                indoor_temp=indoor_temperature,
                is_in_solar_sector=_effective_in_solar_sector,
                sun_elevation=sun_position.elevation,
                config=self._comfort_config,
            )

            # Tier orchestration: build_window_decision_input() is the single
            # resolution point for config inheritance and HA-convention conversion
            # (INV-18).  TierOrchestrator runs Storm/Wind (Tier 1) → Night
            # (Tier 3) → Absence/Heat/Glare (Tier 4) → Solar (Tier 5) →
            # PositionResolver → fallback OPEN.
            # StateGuard is applied after the orchestrator returns.
            #
            # Weekday/Weekend: derive the active schedule profile from local_now
            # and patch lifecycle_config with the active night/morning positions
            # before passing it to build_window_decision_input.  This ensures
            # Night and Morning evaluators use the correct target position for
            # the current day of week without modifying the stored config.
            _active_lc_profile = self.lifecycle_engine.active_profile(
                local_now, self._lifecycle_config
            )
            _effective_lifecycle_config = replace(
                self._lifecycle_config,
                night_position=_active_lc_profile.night_position,
                morning_position=_active_lc_profile.morning_position,
            )
            wdi = build_window_decision_input(
                window=window,
                zone=zone,
                global_defaults=self.global_defaults,
                shade_position_defaults=self.shade_position_defaults,
                lifecycle_config=_effective_lifecycle_config,
                lifecycle_state=self._lifecycle_state,
                absence_active=absence_active,
                current_shading_state=current_state,
                outdoor_temp_c=weather_inputs.outdoor_temperature,
                indoor_temp_c=indoor_temperature,
                exposure=exposure,
                is_in_solar_sector=_effective_in_solar_sector,
                comfort_config=self._comfort_config,
                wind_speed_ms=weather_inputs.wind_speed,
                wind_gust_ms=weather_inputs.wind_gust,
                weather_condition=weather_inputs.weather_condition_enum,
                storm_protection_enabled=self._storm_protection_enabled,
                wind_protection_enabled=self._wind_protection_enabled,
                wind_threshold_ms=self._wind_threshold_ms,
                active_override=active_override,
                override_duration_min=self._override_duration_min,
                override_detection_tolerance=self._override_detection_tolerance,
                override_break_on_lifecycle=self._override_break_on_lifecycle,
            )
            # Adaptation Application (9F17): apply the last-cycle AdaptiveProfile
            # to the resolved BehaviorConfig.  Only when obs_enabled=True — when
            # observation is disabled, _NEUTRAL_ADAPTIVE_PROFILE is always used
            # and the WDI remains as resolved from config (no learning-based change).
            if obs_enabled:
                _adapt_profile = self._adaptive_profiles.get(window_id, _NEUTRAL_ADAPTIVE_PROFILE)
                _adapted_bc, _adapt_trace = apply_adaptive_profile(
                    wdi.effective_behavior,
                    _adapt_profile,
                    window_id=window_id,
                    now=now,
                )
                if _adapted_bc is not wdi.effective_behavior:
                    wdi = replace(wdi, effective_behavior=_adapted_bc)
                self._adaptation_traces[window_id] = _adapt_trace

                # Forecast Strategy Modifier: applied after AdaptationApplication,
                # before the TierOrchestrator.  Adjusts solar entry thresholds
                # conservatively when forecast trust is high and a current forecast
                # is available.  No-op otherwise (modifier.applied = False).
                if _forecast_modifier is not None and _forecast_modifier.applied:
                    _fc_bc = apply_forecast_modifier(wdi.effective_behavior, _forecast_modifier)
                    if _fc_bc is not wdi.effective_behavior:
                        wdi = replace(wdi, effective_behavior=_fc_bc)

                # Step 6: per-window learned target position adaptation.
                # Runs after heat threshold adaptation so position deltas are
                # applied to the final BehaviorConfig before tier evaluation.
                _light_cfg_ha = to_ha_position(wdi.effective_behavior.light_shade_position)
                _normal_cfg_ha = to_ha_position(wdi.effective_behavior.normal_shade_position)
                _strong_cfg_ha = to_ha_position(wdi.effective_behavior.strong_shade_position)
                _light_eff_ha, _normal_eff_ha, _strong_eff_ha, _any_pos_adapted = (
                    self._target_position_adapter.get_effective_targets(
                        window_id=window_id,
                        light_ha=_light_cfg_ha,
                        normal_ha=_normal_cfg_ha,
                        strong_ha=_strong_cfg_ha,
                        confidence_level=_adapt_profile.confidence_level,
                    )
                )
                if _any_pos_adapted:
                    _adapted_eb = replace(
                        wdi.effective_behavior,
                        light_shade_position=to_internal_position(_light_eff_ha),
                        normal_shade_position=to_internal_position(_normal_eff_ha),
                        strong_shade_position=to_internal_position(_strong_eff_ha),
                    )
                    wdi = replace(wdi, effective_behavior=_adapted_eb)
            # else: wdi stays as resolved from config; neutral profile is implied.

            # Per-window behavior mode (v1.0): restrict which tiers are active.
            # Safety (Tier 1: Storm/Wind) is never suppressed regardless of mode.
            _window_behavior = window.behavior_mode
            if _window_behavior is WindowBehaviorMode.ABSENCE_ONLY:
                wdi = replace(
                    wdi,
                    lifecycle_state=LifecycleState.DAY,
                    effective_behavior=replace(
                        wdi.effective_behavior,
                        heat_outdoor_threshold_c=None,
                        heat_indoor_threshold_c=None,
                        solar_gain_suppresses_shading=True,
                        glare_protection_enabled=False,
                    ),
                )
            elif _window_behavior is WindowBehaviorMode.ABSENCE_AND_SCHEDULE:
                # Night/morning lifecycle remains active (no lifecycle_state override).
                # Absence shading is active.  Only solar/heat/glare are suppressed.
                wdi = replace(
                    wdi,
                    effective_behavior=replace(
                        wdi.effective_behavior,
                        heat_outdoor_threshold_c=None,
                        heat_indoor_threshold_c=None,
                        solar_gain_suppresses_shading=True,
                        glare_protection_enabled=False,
                    ),
                )
            elif _window_behavior is WindowBehaviorMode.DISABLED_AUTOMATIC:
                wdi = replace(
                    wdi,
                    lifecycle_state=LifecycleState.DAY,
                    effective_behavior=replace(
                        wdi.effective_behavior,
                        heat_outdoor_threshold_c=None,
                        heat_indoor_threshold_c=None,
                        solar_gain_suppresses_shading=True,
                        glare_protection_enabled=False,
                        absence_position=None,
                    ),
                )

            tier_decision = self._tier_orchestrator.evaluate_window(wdi)

            # Hardware type — needed for safety position correction and Night Hard Hold.
            # Resolved from CoverGroup early so both guards can use it before the
            # Execution Pipeline (which also computes _hw_type at line ~1804).
            _early_cg = self.cover_groups.get(window.cover_group_id)
            _early_hw_type = (
                _early_cg.hardware_type
                if _early_cg is not None
                else CoverHardwareType.GENERIC
            )

            # --- Hardware-aware Safety Position Correction -------------------------
            # The evaluators (WindEvaluator, StormEvaluator) produce
            # target_position=0 (internal: 0=open).  For ROLLER_SHUTTER and
            # VENETIAN_BLIND, internal 0 → HA 100 (retracted/raised) — correct.
            # For AWNING and EXTERIOR_SCREEN, "safe" means retracted which is
            # HA 0 (= internal 100).  Without this correction those covers would
            # be sent to HA 100% (fully deployed/extended) during a wind/storm
            # event — the most exposed position possible.
            if tier_decision.shading_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):
                _hw_safe_pos = _HARDWARE_SAFE_POSITIONS.get(_early_hw_type, 0)
                if tier_decision.target_position != _hw_safe_pos:
                    tier_decision = replace(
                        tier_decision,
                        target_position=_hw_safe_pos,
                    )

            # --- Wind/Storm Release Hold (Hysteresis) ------------------------------
            # Prevent premature safety-state release when wind drops between scan
            # cycles.  Each safety state is held for _*_HOLD_S seconds after the
            # evaluator last fired — covers stay retracted through momentary lulls.
            #
            # Priority: STORM_SAFE > WIND_SAFE.  If the storm hold fires, it
            # overrides a WIND_SAFE decision from the current evaluator run.
            # Night Hard Hold and Manual Override are exempt — they are applied
            # after this block and carry their own exemption checks.
            _hw_safe_pos = _HARDWARE_SAFE_POSITIONS.get(_early_hw_type, 0)
            _eval_is_storm = tier_decision.shading_state is ShadingState.STORM_SAFE
            _eval_is_wind  = tier_decision.shading_state is ShadingState.WIND_SAFE

            # Sensor-unavailable flags: True when the wind sensor (and, for
            # storm, the weather condition source) cannot provide a reading.
            # Passed to the holds so they extend rather than release when data
            # disappears while a safety latch is active (Part 4).
            _wind_sensor_unavailable = (
                weather_inputs.wind_speed is None
                and weather_inputs.wind_gust is None
            )
            _storm_sensor_unavailable = (
                weather_inputs.wind_speed is None
                and weather_inputs.wind_gust is None
                and weather_inputs.weather_condition is None
            )

            _storm_h = self._storm_holds.setdefault(window_id, _SafetyHold(_hold_s=_STORM_HOLD_S))
            _wind_h  = self._wind_holds.setdefault(window_id,  _SafetyHold(_hold_s=_WIND_HOLD_S))

            _storm_held = _storm_h.update(
                evaluator_triggered=_eval_is_storm,
                now=now,
                sensor_unavailable=_storm_sensor_unavailable,
            )
            _wind_held = _wind_h.update(
                evaluator_triggered=_eval_is_wind,
                now=now,
                sensor_unavailable=_wind_sensor_unavailable,
            )

            if _storm_held and not _eval_is_storm:
                # Storm hold still active but evaluator no longer returns STORM_SAFE.
                tier_decision = WindowDecision(
                    window_id=window_id,
                    shading_state=ShadingState.STORM_SAFE,
                    target_position=_hw_safe_pos,
                    decided_by="StormSafeHold",
                )
            elif _wind_held and not _eval_is_wind and tier_decision.shading_state is not ShadingState.STORM_SAFE:
                # Wind hold active, not in any storm state.
                tier_decision = WindowDecision(
                    window_id=window_id,
                    shading_state=ShadingState.WIND_SAFE,
                    target_position=_hw_safe_pos,
                    decided_by="WindSafeHold",
                )

            if self._debug_logging_enabled:
                _tier_ha = (
                    to_ha_position(tier_decision.target_position)
                    if tier_decision.target_position is not None else None
                )
                _LOGGER.debug(
                    "SmartShading: evaluator: window=%s decided_by=%s state=%s ha_target=%s",
                    window_id,
                    tier_decision.decided_by,
                    tier_decision.shading_state.value if hasattr(tier_decision.shading_state, "value") else tier_decision.shading_state,
                    _tier_ha,
                )

            # --- Night Hard Hold ---------------------------------------------------
            # Block non-safety commands that would move a night-configured cover
            # more open than night_position during the active night interval.
            #
            # Only applies to FULLY_AUTOMATIC and ABSENCE_AND_SCHEDULE windows.
            # ABSENCE_ONLY and DISABLED_AUTOMATIC force lifecycle_state=DAY, so
            # NightEvaluator skips and TierOrchestrator falls back to OPEN —
            # applying the hold there would incorrectly drive the cover to
            # night_position against the user's explicit mode choice.
            # ABSENCE_AND_SCHEDULE keeps the night lifecycle active, so the hold
            # must guard it the same way as FULLY_AUTOMATIC.
            #
            # Priority: Safety (STORM_SAFE / WIND_SAFE) > Manual Override > Night Hard Hold.
            # Covers without a configured night_position are not guarded (night shading
            # is intentionally disabled for those windows).
            _night_hard_hold_applied = False
            if (
                _night_interval_active
                and _window_behavior in (
                    WindowBehaviorMode.FULLY_AUTOMATIC,
                    WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
                )
                and tier_decision.shading_state not in (
                    ShadingState.STORM_SAFE,
                    ShadingState.WIND_SAFE,
                    ShadingState.MANUAL_OVERRIDE,
                )
                and tier_decision.target_position is not None
                and wdi.effective_behavior.night_position is not None
                and tier_decision.target_position < wdi.effective_behavior.night_position
            ):
                _night_pos = wdi.effective_behavior.night_position
                _blocked_decided_by = tier_decision.decided_by
                tier_decision = WindowDecision(
                    window_id=window_id,
                    shading_state=ShadingState.NIGHT_CLOSED,
                    target_position=_night_pos,
                    decided_by="NightHardHold",
                )
                _night_hard_hold_applied = True
                _LOGGER.debug(
                    "SmartShading: NightHardHold: window=%s blocked opening (was %s) → NIGHT_CLOSED at %d",
                    window_id,
                    _blocked_decided_by,
                    _night_pos,
                )

            # --- Behavior Mode Dispatch Suppression --------------------------------
            # For non-FULLY_AUTOMATIC windows, automatic cover commands are limited
            # to specific allowed states per mode.
            #
            # ABSENCE_AND_SCHEDULE allows: Safety, Manual Override, NIGHT_CLOSED,
            # ABSENCE_CLOSED, absence-release (ABSENCE_CLOSED → OPEN), and
            # lifecycle-release (NIGHT_CLOSED → OPEN when night ends).
            # Daytime OPEN fallback and all solar/heat/glare decisions are suppressed.
            #
            # ABSENCE_ONLY allows: Safety, Manual Override, ABSENCE_CLOSED, and
            # absence-release.  All lifecycle tiers (night/morning) are skipped via
            # lifecycle_state=DAY in the WDI.
            #
            # DISABLED_AUTOMATIC allows: Safety and Manual Override only.
            #
            # Absence-release: previous state was ABSENCE_CLOSED, no active override,
            # tier returns OPEN → one controlled OPEN dispatch is allowed so
            # SmartShading retracts the cover it moved to absence_position.
            #
            # Lifecycle-release (ABSENCE_AND_SCHEDULE only): previous state was
            # NIGHT_CLOSED, no active override, tier returns OPEN → morning or
            # post-night OPEN dispatch is allowed to retract the cover from night pos.
            #
            # All other tier results are suppressed: target_position=None so
            # CommandFilter blocks (BLOCKED_NO_TARGET_POSITION), and
            # decided_by="BehaviorMode:hold" prevents false learning outcomes and
            # signals the state machine to stay at current_state (see below).
            if _window_behavior is not WindowBehaviorMode.FULLY_AUTOMATIC:
                _is_absence_release = (
                    _window_behavior in (
                        WindowBehaviorMode.ABSENCE_ONLY,
                        WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
                    )
                    and current_state is ShadingState.ABSENCE_CLOSED
                    and tier_decision.shading_state is ShadingState.OPEN
                    and active_override is None
                )
                _is_lifecycle_release = (
                    _window_behavior is WindowBehaviorMode.ABSENCE_AND_SCHEDULE
                    and current_state is ShadingState.NIGHT_CLOSED
                    and tier_decision.shading_state is ShadingState.OPEN
                    and active_override is None
                )
                _mode_dispatch_allowed = (
                    tier_decision.shading_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE)
                    or tier_decision.shading_state is ShadingState.MANUAL_OVERRIDE
                    or (
                        _window_behavior in (
                            WindowBehaviorMode.ABSENCE_ONLY,
                            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
                        )
                        and tier_decision.shading_state is ShadingState.ABSENCE_CLOSED
                    )
                    or (
                        _window_behavior is WindowBehaviorMode.ABSENCE_AND_SCHEDULE
                        and tier_decision.shading_state is ShadingState.NIGHT_CLOSED
                    )
                    or _is_absence_release
                    or _is_lifecycle_release
                )
                if not _mode_dispatch_allowed:
                    tier_decision = replace(
                        tier_decision,
                        target_position=None,
                        decided_by="BehaviorMode:hold",
                    )

            # Override lifecycle: Safety beats override → clear it.
            # Otherwise, run override detection (position delta comparison).
            # Detection result is effective from the NEXT cycle (max 1-cycle delay).
            if tier_decision.shading_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):
                # Phase 9C: record safety clear before removing the override.
                # Learning write is gated; functional clear always runs.
                if obs_enabled and active_override is not None:
                    try:
                        self._learning_store.record_override(OverrideRecord(
                            timestamp=now,
                            window_id=window_id,
                            event_type="cleared_by_safety",
                            lifecycle_state=self._lifecycle_state.value,
                            override_position=active_override.override_position,
                            overridden_state=active_override.overridden_state,
                            overridden_position=active_override.overridden_position,
                            override_duration_min=(
                                (now - active_override.started_at).total_seconds() / 60
                            ),
                            outdoor_temp_c=weather_inputs.outdoor_temperature,
                            solar_radiation_wm2=weather_inputs.solar_radiation,
                            sun_azimuth=sun_position.azimuth,
                            sun_elevation=sun_position.elevation,
                            solar_relative_azimuth=sun_position.azimuth - window.azimuth,
                            weather_condition=weather_inputs.weather_condition,
                            cloud_cover_pct=weather_inputs.cloud_cover,
                            raw_solar_radiation_wm2=effective_radiation,
                            effective_exposure_wm2=exposure.effective_exposure,
                            learned_solar_impact_factor=exposure.learned_solar_impact_factor,
                            decided_by=tier_decision.decided_by,
                        ))
                    except Exception:
                        _LOGGER.warning("Learning: override 'cleared_by_safety' record failed for %s", window_id)
                self._override_detector.clear(window_id)
                # Phase 9F4b-3: safety event resolves any active pending outcome.
                # Gated: pending outcomes only exist when obs_enabled=True.
                if obs_enabled:
                    _safety_pending = self._pending_outcomes.remove(window_id)
                    if _safety_pending is not None:
                        try:
                            _outcome = resolve_outcome(
                                _safety_pending,
                                OutcomeResolutionInput(
                                    trigger=OutcomeResolutionTrigger.SAFETY,
                                    resolution_timestamp=now,
                                    indoor_temp_outcome_c=indoor_temperature,
                                ),
                            )
                            self._learning_store.record_outcome(_outcome)
                        except Exception:
                            _LOGGER.warning(
                                "Learning: outcome resolution (safety) failed for %s", window_id
                            )
            else:
                # Convert observed position HA→internal for delta comparison.
                observed_internal = (
                    100 - cover_position.best_known_position
                    if cover_position.best_known_position is not None
                    else None
                )
                assumed_internal = (
                    100 - cover_position.assumed_position
                    if cover_position.assumed_position is not None
                    else None
                )
                # For the own-command guard, prefer the last position SmartShading
                # actually commanded (never overwritten by passive observation).
                # Fall back to assumed_internal (observe-based) when SmartShading
                # has never dispatched to this cover — prevents a false override on
                # the very first shade decision before any command is sent.
                _cover_group = self.cover_groups.get(window.cover_group_id)
                _cov_id = _cover_group.cover_ids[0] if _cover_group and _cover_group.cover_ids else None
                _assumed_st = self.assumed_state_manager.get_state(_cov_id, now) if _cov_id else None
                _last_commanded = _assumed_st.last_commanded_position if _assumed_st is not None else None
                _override_assumed = _last_commanded if _last_commanded is not None else assumed_internal
                self._override_detector.tick(
                    window_id=window_id,
                    observed_position=observed_internal,
                    smartshading_target=tier_decision.target_position,
                    smartshading_assumed=_override_assumed,
                    prev_state=current_state,
                    tolerance=self._override_detection_tolerance,
                    duration_min=self._override_duration_min,
                    now=now,
                )

            proposed_state = tier_decision.shading_state
            # BehaviorMode:hold: no command was sent, no state transition should be
            # recorded.  The suppressed orchestrator result (OPEN fallback) must not
            # become a published state change or a misleading snapshot entry — e.g.
            # "State=OPEN, cover=20%" would be factually wrong when no dispatch ran.
            # Hold the state machine at current_state so no StateTransitionRecord or
            # PendingOutcome is created for this suppressed cycle.
            if tier_decision.decided_by == "BehaviorMode:hold":
                proposed_state = current_state

            # StateGuard — same logic as the former DecisionEngine.decide().
            # bypasses_guard() covers: no-ops, escalations, lifecycle-direct
            # exits (NIGHT→OPEN, ABSENCE→OPEN), MANUAL_OVERRIDE exits,
            # STORM_SAFE/WIND_SAFE exits.
            if bypasses_guard(current_state, proposed_state):
                new_state, guard_blocked = proposed_state, False
            elif self.guard.is_locked(window_id, current_state, now):
                new_state, guard_blocked = current_state, True
                if self._debug_logging_enabled:
                    _LOGGER.debug(
                        "SmartShading: guard blocked: window=%s current=%s proposed=%s",
                        window_id,
                        current_state.value if hasattr(current_state, "value") else current_state,
                        proposed_state.value if hasattr(proposed_state, "value") else proposed_state,
                    )
            else:
                new_state, guard_blocked = proposed_state, False

            if new_state != current_state:
                # record_state_entered always runs (StateGuard needs it).
                self.guard.record_state_entered(window_id, now)
                # Learning writes gated behind obs_enabled.
                if obs_enabled:
                    # Phase 9C: record state transition (only on actual change).
                    try:
                        self._learning_store.record_transition(StateTransitionRecord(
                            timestamp=now,
                            window_id=window_id,
                            from_state=current_state,
                            to_state=new_state,
                            decided_by=tier_decision.decided_by,
                            lifecycle_state=self._lifecycle_state.value,
                            absence_active=absence_active,
                            is_in_solar_sector=sun_geometry.is_in_tolerance_window,
                            outdoor_temp_c=weather_inputs.outdoor_temperature,
                            indoor_temp_c=indoor_temperature,
                            solar_radiation_wm2=weather_inputs.solar_radiation,
                            wind_speed_ms=weather_inputs.wind_speed,
                            sun_azimuth=sun_position.azimuth,
                            sun_elevation=sun_position.elevation,
                            solar_relative_azimuth=sun_position.azimuth - window.azimuth,
                            weather_condition=weather_inputs.weather_condition,
                            cloud_cover_pct=weather_inputs.cloud_cover,
                            raw_solar_radiation_wm2=effective_radiation,
                            effective_exposure_wm2=exposure.effective_exposure,
                            learned_solar_impact_factor=exposure.learned_solar_impact_factor,
                        ))
                    except Exception:
                        _LOGGER.warning("Learning: state transition record failed for %s", window_id)
                    else:
                        if self._debug_logging_enabled:
                            _LOGGER.debug(
                                "SmartShading: learning: transition window=%s %s→%s decided_by=%s",
                                window_id,
                                current_state.value if hasattr(current_state, "value") else current_state,
                                new_state.value if hasattr(new_state, "value") else new_state,
                                tier_decision.decided_by,
                            )

                    # Phase 9F4b-3: create PendingOutcome for evaluator-driven states.
                    # MANUAL_OVERRIDE / safety states are excluded — they are not
                    # evaluator decisions and must not generate outcome observations.
                    # BehaviorMode:hold is also excluded: no command was sent, so
                    # there is no evaluator outcome to observe or score.
                    # When a new pending is created, replace() returns the old one
                    # (if any) so it can be resolved as STATE_CHANGE before the new
                    # observation window starts.
                    if new_state not in _NO_OUTCOME_STATES and tier_decision.decided_by != "BehaviorMode:hold":
                        try:
                            _new_pending = PendingOutcome(
                                window_id=window_id,
                                decision_timestamp=now,
                                from_state=current_state,
                                to_state=new_state,
                                decided_by=tier_decision.decided_by,
                                target_position=tier_decision.target_position,
                                lifecycle_state=self._lifecycle_state.value,
                                indoor_temp_at_decision=indoor_temperature,
                                outdoor_temp_at_decision=weather_inputs.outdoor_temperature,
                                indoor_temp_outcome_delay_min=_OUTCOME_OBSERVATION_DELAY_MIN,
                            )
                            _old_pending = self._pending_outcomes.replace(_new_pending)
                            if _old_pending is not None:
                                _outcome = resolve_outcome(
                                    _old_pending,
                                    OutcomeResolutionInput(
                                        trigger=OutcomeResolutionTrigger.STATE_CHANGE,
                                        resolution_timestamp=now,
                                        indoor_temp_outcome_c=indoor_temperature,
                                    ),
                                )
                                self._learning_store.record_outcome(_outcome)
                        except Exception:
                            _LOGGER.warning(
                                "Learning: pending outcome create/resolve (state_change) failed for %s",
                                window_id,
                            )
                    # If new_state == MANUAL_OVERRIDE: leave the existing pending in
                    # the queue so the override "started" block below can resolve it
                    # with the correct OVERRIDE trigger.

            self._current_states[window_id] = new_state

            # Re-fetch the override after tick() — might have been newly
            # detected this cycle (effective next cycle for the evaluator,
            # but reflected in the observation immediately).
            current_override = self._override_detector.get(window_id, now)

            # Phase 9C: detect "started" and "renewed" from tick() outcome.
            # Safety path clears the override — no started/renewed possible there.
            # Learning writes gated behind obs_enabled.
            if obs_enabled and tier_decision.shading_state not in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):
                if active_override is None and current_override is not None:
                    try:
                        self._learning_store.record_override(OverrideRecord(
                            timestamp=now,
                            window_id=window_id,
                            event_type="started",
                            lifecycle_state=self._lifecycle_state.value,
                            override_position=current_override.override_position,
                            overridden_state=current_override.overridden_state,
                            overridden_position=current_override.overridden_position,
                            override_duration_min=None,
                            outdoor_temp_c=weather_inputs.outdoor_temperature,
                            solar_radiation_wm2=weather_inputs.solar_radiation,
                            sun_azimuth=sun_position.azimuth,
                            sun_elevation=sun_position.elevation,
                            solar_relative_azimuth=sun_position.azimuth - window.azimuth,
                            weather_condition=weather_inputs.weather_condition,
                            cloud_cover_pct=weather_inputs.cloud_cover,
                            raw_solar_radiation_wm2=effective_radiation,
                            effective_exposure_wm2=exposure.effective_exposure,
                            learned_solar_impact_factor=exposure.learned_solar_impact_factor,
                            decided_by=tier_decision.decided_by,
                        ))
                    except Exception:
                        _LOGGER.warning("Learning: override 'started' record failed for %s", window_id)
                    # Phase 9F4b-3: override "started" resolves the pending outcome.
                    _ov_pending = self._pending_outcomes.remove(window_id)
                    if _ov_pending is not None:
                        try:
                            _delay_min = (now - _ov_pending.decision_timestamp).total_seconds() / 60
                            _outcome = resolve_outcome(
                                _ov_pending,
                                OutcomeResolutionInput(
                                    trigger=OutcomeResolutionTrigger.OVERRIDE,
                                    resolution_timestamp=now,
                                    indoor_temp_outcome_c=indoor_temperature,
                                    override_delay_min=_delay_min,
                                    override_event_type="started",
                                ),
                            )
                            self._learning_store.record_outcome(_outcome)
                        except Exception:
                            _LOGGER.warning(
                                "Learning: outcome resolution (override started) failed for %s",
                                window_id,
                            )
                elif (
                    active_override is not None
                    and current_override is not None
                    and current_override.started_at != active_override.started_at
                ):
                    try:
                        self._learning_store.record_override(OverrideRecord(
                            timestamp=now,
                            window_id=window_id,
                            event_type="renewed",
                            lifecycle_state=self._lifecycle_state.value,
                            override_position=current_override.override_position,
                            overridden_state=current_override.overridden_state,
                            overridden_position=current_override.overridden_position,
                            override_duration_min=None,
                            outdoor_temp_c=weather_inputs.outdoor_temperature,
                            solar_radiation_wm2=weather_inputs.solar_radiation,
                            sun_azimuth=sun_position.azimuth,
                            sun_elevation=sun_position.elevation,
                            solar_relative_azimuth=sun_position.azimuth - window.azimuth,
                            weather_condition=weather_inputs.weather_condition,
                            cloud_cover_pct=weather_inputs.cloud_cover,
                            raw_solar_radiation_wm2=effective_radiation,
                            effective_exposure_wm2=exposure.effective_exposure,
                            learned_solar_impact_factor=exposure.learned_solar_impact_factor,
                            decided_by=tier_decision.decided_by,
                        ))
                    except Exception:
                        _LOGGER.warning("Learning: override 'renewed' record failed for %s", window_id)
                    # Phase 9F4b-3: override "renewed" resolves the pending outcome.
                    _ren_pending = self._pending_outcomes.remove(window_id)
                    if _ren_pending is not None:
                        try:
                            _delay_min = (now - _ren_pending.decision_timestamp).total_seconds() / 60
                            _outcome = resolve_outcome(
                                _ren_pending,
                                OutcomeResolutionInput(
                                    trigger=OutcomeResolutionTrigger.OVERRIDE,
                                    resolution_timestamp=now,
                                    indoor_temp_outcome_c=indoor_temperature,
                                    override_delay_min=_delay_min,
                                    override_event_type="renewed",
                                ),
                            )
                            self._learning_store.record_outcome(_outcome)
                        except Exception:
                            _LOGGER.warning(
                                "Learning: outcome resolution (override renewed) failed for %s",
                                window_id,
                            )

            # Phase 9C: update prev_override for next cycle (normal path).
            self._prev_overrides[window_id] = current_override

            # Phase 9C: periodic snapshot every SNAPSHOT_CYCLE_INTERVAL cycles.
            # Gated: snapshots are learning data; only written when obs_enabled=True.
            if obs_enabled:
                _snap_count = self._snapshot_counters.get(window_id, 0) + 1
                self._snapshot_counters[window_id] = _snap_count
                if _snap_count % SNAPSHOT_CYCLE_INTERVAL == 0:
                    try:
                        self._learning_store.record_snapshot(WindowCycleSnapshot(
                            timestamp=now,
                            window_id=window_id,
                            shading_state=new_state,
                            decided_by=tier_decision.decided_by,
                            lifecycle_state=self._lifecycle_state.value,
                            absence_active=absence_active,
                            override_active=current_override is not None,
                            target_position=tier_decision.target_position,
                            outdoor_temp_c=weather_inputs.outdoor_temperature,
                            indoor_temp_c=indoor_temperature,
                            solar_radiation_wm2=weather_inputs.solar_radiation,
                            effective_exposure_wm2=(
                                exposure.effective_exposure if exposure is not None else None
                            ),
                            wind_speed_ms=weather_inputs.wind_speed,
                            sun_azimuth=sun_position.azimuth,
                            sun_elevation=sun_position.elevation,
                            solar_relative_azimuth=sun_position.azimuth - window.azimuth,
                            weather_condition=weather_inputs.weather_condition,
                            cloud_cover_pct=weather_inputs.cloud_cover,
                            raw_solar_radiation_wm2=effective_radiation,
                            learned_solar_impact_factor=exposure.learned_solar_impact_factor,
                        ))
                    except Exception:
                        _LOGGER.warning("Learning: snapshot record failed for %s", window_id)

            # Phase 9F4b-3: timeout — observation window elapsed without other trigger.
            # Gated: pending outcomes only exist when obs_enabled=True.
            if obs_enabled:
                try:
                    _to_pending = self._pending_outcomes.get(window_id)
                    if _to_pending is not None:
                        _elapsed_min = (now - _to_pending.decision_timestamp).total_seconds() / 60
                        if _elapsed_min >= _to_pending.indoor_temp_outcome_delay_min:
                            _to_pending = self._pending_outcomes.remove(window_id)
                            if _to_pending is not None:
                                _outcome = resolve_outcome(
                                    _to_pending,
                                    OutcomeResolutionInput(
                                        trigger=OutcomeResolutionTrigger.TIMEOUT,
                                        resolution_timestamp=now,
                                        indoor_temp_outcome_c=indoor_temperature,
                                    ),
                                )
                                self._learning_store.record_outcome(_outcome)
                except Exception:
                    _LOGGER.warning(
                        "Learning: outcome resolution (timeout) failed for %s", window_id
                    )

            # Learning Loop Closure (9F15) — run the full Learning Pipeline
            # for this window.  Purely observational: no evaluator is touched,
            # no threshold is modified.  Any failure retains the previous or
            # neutral profile and never interrupts the Coordinator cycle.
            # Gated: the full Learning Pipeline only runs when obs_enabled=True.
            if obs_enabled:
                try:
                    _current_situation = SituationRecord(
                        window_id=window_id,
                        decision_timestamp=now,
                        from_state=current_state,
                        decided_state=new_state,
                        decided_by=tier_decision.decided_by or "unknown",
                        lifecycle_state=self._lifecycle_state.value,
                        outcome_score=0.0,
                        override_occurred=current_override is not None,
                        override_delay_min=None,
                        resolution_status="none",
                        effective_exposure_wm2=exposure.effective_exposure,
                        sun_elevation=sun_position.elevation,
                        solar_relative_azimuth=sun_position.azimuth - window.azimuth,
                        indoor_temp_at_decision=indoor_temperature,
                        outdoor_temp_c=weather_inputs.outdoor_temperature,
                        absence_active=absence_active,
                    )
                    self._adaptive_profiles[window_id] = self._run_learning_pipeline(
                        window_id=window_id,
                        current_situation=_current_situation,
                        decided_by=tier_decision.decided_by,
                    )
                except Exception:
                    _LOGGER.warning(
                        "SmartShading: Learning Loop: SituationRecord build failed for %s", window_id
                    )
                    self._adaptive_profiles.setdefault(window_id, _NEUTRAL_ADAPTIVE_PROFILE)
            else:
                self._adaptive_profiles[window_id] = _NEUTRAL_ADAPTIVE_PROFILE

            # Execution Pipeline — Pass 1 (synchronous): snapshot + CommandFilter only.
            # Dispatch is deferred to the second loop pass after ShadingGroup
            # harmonization has aligned targets across windows in the same group.
            _exec_entity_id, _exec_cap, _exec_snapshot = (
                self._build_cover_entity_snapshot_for_window(window, now)
            )
            _exec_mode = (
                ExecutionMode.AUTOMATIC
                if _exec.active_control_enabled
                else ExecutionMode.RECOMMENDATION_ONLY
            )
            _is_safety = new_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE)
            _exec_target_internal = tier_decision.target_position
            _exec_filter_result: CommandFilterResult | None = None

            if _exec_entity_id is not None and _exec_cap is not None and _exec_snapshot is not None:
                _guard_action_allowed = self.guard.can_send_action(window_id, new_state, now)
                _exec_filter_result = CommandFilter().evaluate(
                    target_position_internal=_exec_target_internal,
                    current_position_internal=_exec_snapshot.assumed_position_internal,
                    execution_mode=_exec_mode,
                    is_safety=_is_safety,
                    is_manual_override=active_override is not None,
                    is_cover_available=_exec_snapshot.available,
                    state_guard_allowed=_guard_action_allowed,
                    execution_capability=ExecutionCapability(),
                    invert_position=_exec_cap.invert_position,
                )

            # Cover group + hardware settings — resolved once and shared between
            # Daytime Minimum Open Position and Anti-Heat-Buildup blocks.
            _cg = self.cover_groups.get(window.cover_group_id)
            _hw_settings = (
                default_hardware_settings(_cg.hardware_type) if _cg is not None else {}
            )
            _hw_type = _cg.hardware_type if _cg is not None else CoverHardwareType.GENERIC

            # Daytime Minimum Open Position (Step 9G10f-b): clamp the target
            # before ShadingGroup harmonization so no group can force a
            # position below this window's hardware-type minimum.
            _daytime_min_applied = False
            _pre_daytime_min_target_ha: int | None = None
            _hw_min: int | None = _hw_settings.get("daytime_min_open_position_ha")
            if (
                _exec_filter_result is not None
                and _exec_filter_result.target_position_ha is not None
                and _exec_cap is not None
            ):
                _clamped_ha, _daytime_min_applied, _pre_daytime_min_target_ha = (
                    apply_daytime_min_open(
                        target_position_ha=_exec_filter_result.target_position_ha,
                        daytime_min_ha=_hw_min,
                        new_state=new_state,
                    )
                )
                if _daytime_min_applied:
                    _exec_filter_result = replace(
                        _exec_filter_result,
                        target_position_ha=_clamped_ha,
                        target_position_internal=to_internal_position(
                            _clamped_ha, invert=_exec_cap.invert_position
                        ),
                    )

            # Anti-Heat-Buildup (Step 9G10f-c): raise the target when a roller
            # shutter is nearly closed under direct strong solar radiation.
            # Runs AFTER daytime clamp so both protections stack correctly.
            _ahb_applied = False
            _pre_ahb_target_ha: int | None = None
            _ahb_enabled: bool = _hw_settings.get("anti_heat_buildup_enabled", False)
            _ahb_position: int | None = _hw_settings.get("anti_heat_buildup_position_ha")
            _allow_during_absence: bool = _hw_settings.get(
                "allow_anti_heat_buildup_during_absence", False
            )
            if (
                _exec_filter_result is not None
                and _exec_filter_result.target_position_ha is not None
                and _exec_cap is not None
            ):
                _clamped_ha, _ahb_applied, _pre_ahb_target_ha = apply_anti_heat_buildup(
                    target_position_ha=_exec_filter_result.target_position_ha,
                    ahb_position_ha=_ahb_position,
                    enabled=_ahb_enabled,
                    hardware_type=_hw_type,
                    new_state=new_state,
                    in_solar_sector=_effective_in_solar_sector,
                    effective_exposure_wm2=exposure.effective_exposure,
                    allow_during_absence=_allow_during_absence,
                )
                if _ahb_applied:
                    _exec_filter_result = replace(
                        _exec_filter_result,
                        target_position_ha=_clamped_ha,
                        target_position_internal=to_internal_position(
                            _clamped_ha, invert=_exec_cap.invert_position
                        ),
                    )

            # Combined floor for ShadingGroup harmonization.
            # Daytime floor: minimum open position that is never negotiable during
            # normal shading states (exemptions mirror DAYTIME_CLAMP_EXEMPT_STATES).
            # AHB floor: set when solar conditions warrant heat-buildup protection,
            # regardless of whether the current target already meets the minimum.
            _daytime_floor: int = (
                _hw_min
                if (_hw_min is not None and new_state not in DAYTIME_CLAMP_EXEMPT_STATES)
                else 0
            )
            _ahb_floor: int = (
                _ahb_position
                if (
                    _ahb_enabled
                    and _ahb_position is not None
                    and _hw_type is CoverHardwareType.ROLLER_SHUTTER
                    and new_state not in ANTI_HEAT_BUILDUP_EXEMPT_STATES
                    and not (
                        new_state is ShadingState.ABSENCE_CLOSED
                        and not _allow_during_absence
                    )
                    and sun_geometry.is_in_tolerance_window
                    and exposure.effective_exposure >= ANTI_HEAT_BUILDUP_MIN_EXPOSURE_WM2
                )
                else 0
            )
            _min_position_floor_ha: int = max(_daytime_floor, _ahb_floor)

            # Tilt Execution (Step 9G10f-d/e): derive target_tilt_ha for this window.
            # Gate: only VENETIAN_BLIND with supports_tilt=True and
            # tilt_control_enabled=True in hardware settings qualifies.
            # Calculation: sun-elevation bands via calculate_simple_tilt_target()
            # (Step 9G10f-e) — returns None for all non-tilt covers and
            # non-shading states, ensuring position-only behavior is unchanged.
            _tilt_control_enabled: bool = _hw_settings.get("tilt_control_enabled", False)
            _tilt_supported: bool = (
                _exec_cap is not None
                and _exec_cap.supports_tilt
                and _tilt_control_enabled
            )
            _exec_target_tilt_ha: int | None = calculate_simple_tilt_target(
                hardware_type=_hw_type,
                supports_tilt=_exec_cap.supports_tilt if _exec_cap is not None else False,
                tilt_control_enabled=_tilt_control_enabled,
                in_solar_sector=_effective_in_solar_sector,
                sun_elevation_deg=(
                    sun_position.elevation if sun_position is not None else None
                ),
                new_state=new_state,
            )

            # Only forward a tilt target when the cover physically supports it.
            _effective_tilt_ha: int | None = (
                _exec_target_tilt_ha if _tilt_supported else None
            )

            # Patch the filter result to carry the tilt target so CoverIntent
            # and ExecutionPlan can pick it up without extra parameters.
            if _exec_filter_result is not None and _effective_tilt_ha is not None:
                _exec_filter_result = replace(
                    _exec_filter_result,
                    target_tilt_ha=_effective_tilt_ha,
                )

            # Store per-window state for harmonization + dispatch pass.
            # is_override_active uses active_override (pre-tick), matching CommandFilter.
            _window_states[window_id] = _WindowComputeState(
                window=window,
                zone=zone,
                obs_enabled=obs_enabled,
                active_control_enabled=_exec.active_control_enabled,
                new_state=new_state,
                exec_entity_id=_exec_entity_id,
                exec_cap=_exec_cap,
                exec_snapshot=_exec_snapshot,
                exec_mode=_exec_mode,
                is_safety=_is_safety,
                exec_target_internal=_exec_target_internal,
                exec_filter_result=_exec_filter_result,
                tier_decided_by=tier_decision.decided_by,
                is_override_active=active_override is not None,
                cover_available=(
                    _exec_snapshot.available if _exec_snapshot is not None else None
                ),
                daytime_min_open_applied=_daytime_min_applied,
                pre_daytime_min_target_position_ha=_pre_daytime_min_target_ha,
                anti_heat_buildup_applied=_ahb_applied,
                pre_anti_heat_buildup_target_position_ha=_pre_ahb_target_ha,
                min_position_floor_ha=_min_position_floor_ha,
                target_tilt_ha=_effective_tilt_ha,
                in_solar_sector=_effective_in_solar_sector,
                night_hard_hold_applied=_night_hard_hold_applied,
            )

            # Build window_results now — WindowObservation has no dependency on
            # dispatch results; all inputs are available after pass 1.
            reason, reason_code = build_reason(new_state, comfort_assessment)
            window_results[window_id] = WindowObservation(
                state=new_state,
                reason=reason,
                reason_code=reason_code,
                next_action=build_next_action(new_state, current_state, self.shade_position_defaults),
                guard_blocked=guard_blocked,
                exposure=exposure,
                outdoor_temperature=weather_inputs.outdoor_temperature,
                solar_radiation=weather_inputs.solar_radiation,
                cloud_cover=weather_inputs.cloud_cover,
                wind_speed=weather_inputs.wind_speed,
                weather_condition=weather_inputs.weather_condition,
                cover_position=cover_position,
                lifecycle_state=self._lifecycle_state.value,
                previous_lifecycle_state=_prev_lifecycle_state.value,
                sun_elevation_deg=sun_position.elevation,
                night_active=self._lifecycle_state is LifecycleState.NIGHT,
                absence_active=absence_active,
                effective_solar_sector=_effective_in_solar_sector,
                solar_source=_solar_source,
                obstruction_blocked=_obstruction_blocked,
                manual_sun_sector_active=_manual_sun_sector_active,
                comfort_assessment=comfort_assessment,
                override_active=current_override is not None,
                override_expires_at=current_override.expires_at if current_override is not None else None,
                override_source=current_override.source if current_override is not None else None,
                # Convert internal → HA convention for sensor display.
                override_position=(
                    100 - current_override.override_position
                    if current_override is not None else None
                ),
                # Phase 9E: learning diagnostics (computed above, before the no-sun branch).
                **_learn_diag,
            )

        # --- ShadingGroup Harmonization (Step 9G10e) ---------------------------
        # Run after all per-window CommandFilter results are available.
        # Groups windows by (zone_id, shading_group_id) and aligns
        # target_position_ha to min(group) for all eligible members.
        _harmonization_candidates: dict[str, ShadingGroupCandidate] = {
            window_id: ShadingGroupCandidate(
                window_id=window_id,
                zone_id=s.window.zone_id,
                shading_group_id=s.window.shading_group_id,
                execution_mode_value=s.exec_mode.value,
                command_allowed=(
                    s.exec_filter_result.allowed if s.exec_filter_result is not None else None
                ),
                target_position_ha=(
                    s.exec_filter_result.target_position_ha
                    if s.exec_filter_result is not None else None
                ),
                is_safety=s.is_safety,
                is_override_active=s.is_override_active,
                cover_available=s.cover_available,
                min_position_floor_ha=s.min_position_floor_ha,
                in_solar_sector=s.in_solar_sector,
            )
            for window_id, s in _window_states.items()
        }
        _harmonization: dict[str, HarmonizationResult] = compute_harmonization(
            _harmonization_candidates
        )

        if self._debug_logging_enabled:
            _harm_summary = {
                wid: h.final_target_position_ha
                for wid, h in _harmonization.items()
                if h.harmonized
            }
            if _harm_summary:
                _LOGGER.debug("SmartShading: harmonization applied: %s", _harm_summary)

        # --- Execution Pipeline — Pass 2 (async): dispatch + build diagnostics ---
        # For harmonized windows, the filter result is replaced with a new one
        # carrying the group's harmonized target_position_ha before plan building.
        for window_id, s in _window_states.items():
            harm = _harmonization[window_id]
            _exec_filter_for_dispatch = s.exec_filter_result

            if harm.harmonized and s.exec_filter_result is not None and s.exec_cap is not None:
                # Build a modified CommandFilterResult with the harmonized HA target.
                # Convert back to internal so AssumedStateManager.update() receives
                # the correct position after dispatch.
                _harm_internal = to_internal_position(
                    harm.final_target_position_ha,  # type: ignore[arg-type]
                    invert=s.exec_cap.invert_position,
                )
                _exec_filter_for_dispatch = replace(
                    s.exec_filter_result,
                    target_position_ha=harm.final_target_position_ha,
                    target_position_internal=_harm_internal,
                )

            _exec_plan_result = None
            _dispatch_suppressed_reason: str | None = None
            _dispatch_throttled: bool = False
            _throttle_wait_ms: int | None = None

            if (
                s.exec_entity_id is not None
                and s.exec_cap is not None
                and s.exec_snapshot is not None
                and _exec_filter_for_dispatch is not None
            ):
                _exec_plan = build_execution_plan(
                    window_id=window_id,
                    cover_entity_ids=self.cover_groups[s.window.cover_group_id].cover_ids,
                    filter_result=_exec_filter_for_dispatch,
                    decided_by=s.tier_decided_by or "unknown",
                    now=now,
                )
                _exec_results = []
                for _intent in _exec_plan.intents:
                    if not _intent.allowed:
                        # CommandFilter blocked this intent — no dispatch.
                        _exec_results.append(build_blocked_result(
                            _intent,
                            reason=f"command blocked: {_intent.blocked_reason}",
                        ))
                    elif self._startup_cycles_remaining > 0 and not _intent.is_safety:
                        # Startup Grace: suppress non-safety dispatch until entity
                        # states have hydrated after HA restart.
                        _dispatch_suppressed_reason = "startup_grace_active"
                        _exec_results.append(build_not_attempted_result(
                            _intent,
                            reason="startup_grace_active: dispatch suppressed during startup hydration",
                        ))
                    else:
                        # Serial Dispatch (Step 10): acquire the integration-wide
                        # lock before every cover command.  The lock is shared
                        # across ALL zone coordinators so commands from different
                        # zones are fully serialised — no two zones can dispatch
                        # at the same time.
                        #
                        # While holding the lock:
                        #   - Non-safety: sleep until the throttle allows the next
                        #     dispatch (≥1 s since the previous SENT command).
                        #   - Safety: skip the sleep — prioritised, but still serial.
                        #
                        # POSITION INVARIANT: dispatch_cover_intent uses
                        # target_position_ha, never target_position_internal.
                        async with self._serial_dispatch.lock:
                            if not _intent.is_safety:
                                _now_pre = dt_util.utcnow()
                                _wait = self._serial_dispatch.time_until_next_allowed(
                                    _now_pre
                                )
                                if _wait.total_seconds() > 0:
                                    _dispatch_throttled = True
                                    _throttle_wait_ms = round(_wait.total_seconds() * 1000)
                                    if self._debug_logging_enabled:
                                        _LOGGER.debug(
                                            "SmartShading: dispatch throttle: sleeping %.0f ms "
                                            "before cover=%s ha_pos=%s",
                                            _wait.total_seconds() * 1000,
                                            _intent.cover_entity_id,
                                            _intent.target_position_ha,
                                        )
                                    await asyncio.sleep(_wait.total_seconds())
                            _intent_result = await dispatch_cover_intent(
                                self.hass, _intent, now_utc=dt_util.utcnow()
                            )
                            # Update throttle clock on confirmed send only.
                            # Safety SENT also updates so subsequent non-safety
                            # commands wait the full interval from safety dispatch.
                            # FAILED: no confirmed dispatch — do not update throttle.
                            if _intent_result.status is ExecutionStatus.SENT:
                                self._serial_dispatch.record_dispatch(dt_util.utcnow())
                                if self._debug_logging_enabled:
                                    _LOGGER.debug(
                                        "SmartShading: dispatched cover=%s "
                                        "ha_pos=%s safety=%s",
                                        _intent.cover_entity_id,
                                        _intent.target_position_ha,
                                        _intent.is_safety,
                                    )
                        _exec_results.append(_intent_result)
                _exec_plan_result = build_execution_plan_result(window_id, _exec_results)

                # Post-dispatch side effects — only on confirmed sends, never on failure.
                if _exec_plan_result.any_sent and not _exec_plan_result.any_failed:
                    # Record the action timestamp so StateGuard cooldown works correctly.
                    self.guard.record_action_sent(window_id, now)
                    # Update assumed position for covers sent a command this cycle.
                    # For feedback-rich covers, the next observe() cycle will overwrite
                    # with the actual position; update() provides immediate best-estimate.
                    for _sent_result in _exec_plan_result.results:
                        if (
                            _sent_result.status is ExecutionStatus.SENT
                            and _sent_result.target_position_internal is not None
                        ):
                            _sent_cap = self._get_or_detect_capability(_sent_result.entity_id)
                            self.assumed_state_manager.update(
                                _sent_result.entity_id,
                                _sent_result.target_position_internal,
                                now,
                                _sent_cap.has_reliable_position_feedback,
                            )

            _last_exec_result = (
                _exec_plan_result.results[0]
                if _exec_plan_result is not None and _exec_plan_result.results
                else None
            )
            _safety_result_failed = (
                _exec_plan_result is not None
                and _exec_plan_result.contains_safety_result
                and _exec_plan_result.any_failed
            )
            execution_diagnostics[window_id] = WindowExecutionDiagnostics(
                observation_enabled=s.obs_enabled,
                active_control_enabled=s.active_control_enabled,
                execution_mode=s.exec_mode.value,
                cover_entity_id=s.exec_entity_id,
                cover_available=(
                    s.exec_snapshot.available if s.exec_snapshot is not None else None
                ),
                actual_position_ha=(
                    s.exec_snapshot.current_position_ha if s.exec_snapshot is not None else None
                ),
                actual_position_internal=(
                    s.exec_snapshot.current_position_internal
                    if s.exec_snapshot is not None else None
                ),
                assumed_position_internal=(
                    s.exec_snapshot.assumed_position_internal
                    if s.exec_snapshot is not None else None
                ),
                has_position_feedback=(
                    s.exec_snapshot.current_position_ha is not None
                    if s.exec_snapshot is not None else None
                ),
                tier_decided_by=s.tier_decided_by,
                target_position_internal=s.exec_target_internal,
                target_position_ha=(
                    _exec_filter_for_dispatch.target_position_ha
                    if _exec_filter_for_dispatch is not None else None
                ),
                is_safety=s.is_safety,
                command_allowed=(
                    _exec_filter_for_dispatch.allowed
                    if _exec_filter_for_dispatch is not None else None
                ),
                command_blocked_reason=(
                    _exec_filter_for_dispatch.blocked_reason
                    if _exec_filter_for_dispatch is not None else None
                ),
                last_command_status=(
                    _last_exec_result.status.value if _last_exec_result is not None else None
                ),
                last_command_sent_at=(
                    _last_exec_result.sent_at_utc if _last_exec_result is not None else None
                ),
                service_call_sent=(
                    _exec_plan_result.any_sent if _exec_plan_result is not None else False
                ),
                service_call_failed=(
                    _exec_plan_result.any_failed if _exec_plan_result is not None else False
                ),
                execution_error=(
                    _last_exec_result.error if _last_exec_result is not None else None
                ),
                safety_result_failed=_safety_result_failed,
                dispatch_suppressed_reason=_dispatch_suppressed_reason,
                night_hard_hold_applied=s.night_hard_hold_applied,
                startup_grace_remaining=self._startup_cycles_remaining,
                dispatch_throttled=_dispatch_throttled,
                throttle_wait_ms=_throttle_wait_ms,
                # ShadingGroup harmonization context (Step 9G10e).
                shading_group_id=s.window.shading_group_id,
                shading_group_harmonized=harm.harmonized,
                pre_harmonization_target_position_ha=harm.pre_harmonization_target_position_ha,
                # Daytime Minimum Open Position context (Step 9G10f-b).
                daytime_min_open_applied=s.daytime_min_open_applied,
                pre_daytime_min_target_position_ha=s.pre_daytime_min_target_position_ha,
                # Anti-Heat-Buildup context (Step 9G10f-c).
                anti_heat_buildup_applied=s.anti_heat_buildup_applied,
                pre_anti_heat_buildup_target_position_ha=s.pre_anti_heat_buildup_target_position_ha,
                # Tilt execution context (Step 9G10f-d).
                target_tilt_ha=s.target_tilt_ha,
                current_tilt_ha=(
                    s.exec_snapshot.current_tilt if s.exec_snapshot is not None else None
                ),
                has_tilt_feedback=(
                    s.exec_snapshot.has_tilt_feedback if s.exec_snapshot is not None else False
                ),
                tilt_command_sent=(
                    _last_exec_result.tilt_sent if _last_exec_result is not None else False
                ),
                tilt_command_failed=(
                    _last_exec_result.tilt_error is not None
                    if _last_exec_result is not None else False
                ),
                tilt_error=(
                    _last_exec_result.tilt_error if _last_exec_result is not None else None
                ),
            )

        # Startup Grace Period (9G5): decrement at the end of the cycle so that
        # STARTUP_GRACE_CYCLES cycles are fully suppressed before dispatch is
        # allowed.  Decrementing here (after dispatch) rather than at the top of
        # the function ensures the count matches the number of suppressed cycles:
        # with STARTUP_GRACE_CYCLES=3, cycles 1-3 are suppressed and cycle 4 is
        # the first that can dispatch.
        if self._startup_cycles_remaining > 0:
            self._startup_cycles_remaining -= 1

        # Time-based periodic persistence save.
        # Saves when _learning_dirty (important event this cycle: override / outcome)
        # OR when at least PERSISTENCE_INTERVAL_MINUTES have elapsed since the last save.
        # async_save catches all exceptions internally; the coordinator is never blocked.
        _elapsed_since_save = (
            (now - self._persistence_last_save_at).total_seconds() / 60
            if self._persistence_last_save_at is not None
            else float("inf")
        )
        _should_persist = (
            self._learning_dirty
            or _elapsed_since_save >= PERSISTENCE_INTERVAL_MINUTES
        )
        if _should_persist:
            await self._learning_persistence.async_save(
                self._learning_store,
                set(self.windows.keys()),
                now,
                target_adapter=self._target_position_adapter,
            )
            self._persistence_last_save_at = now
            self._learning_dirty = False

        return SmartShadingData(
            window_results=window_results,
            adaptive_profiles=dict(self._adaptive_profiles),
            adaptation_traces=dict(self._adaptation_traces),
            execution_diagnostics=execution_diagnostics,
            updated_at=now,
        )

    def _run_learning_pipeline(
        self,
        *,
        window_id: str,
        current_situation: SituationRecord,
        decided_by: str | None,
    ) -> AdaptiveProfile:
        """Run the full Learning Pipeline for *window_id* and return an AdaptiveProfile.

        Steps:
          1. SimilarityPipeline  — nearest-neighbour search in historical situations
          2. ConfidenceEngine    — data-richness and neighbourhood-quality gate
          3. OverrideLearning    — override pattern signal (inactive: no outcome yet)
          4. SolarImpactLearning — thermal solar signal from completed situations
          5. LearningSignalAggregator — weighted combination of all signals
          6. AdaptationLayer     — translate aggregate into AdaptiveProfile factors

        Tier safety:
          No evaluator threshold is read or written.  No Tier 1–5 component is
          touched.  The returned AdaptiveProfile is purely observational.

        Never raises — any exception produces _NEUTRAL_ADAPTIVE_PROFILE.
        """
        try:
            transitions = self._learning_store.get_transitions(window_id)
            outcomes    = self._learning_store.get_outcomes(window_id)

            resolved       = [o for o in outcomes if o.outcome_score is not None]
            total_resolved = len(resolved)
            global_override_rate: float | None = (
                sum(1 for o in resolved if o.override_occurred) / total_resolved
                if total_resolved > 0 else None
            )

            similarity_result = compute_similarity_result(
                window_id=window_id,
                current=current_situation,
                transitions=transitions,
                outcomes=outcomes,
            )
            confidence_result = compute_confidence(ConfidenceInput(
                result=similarity_result,
                total_resolved_outcomes=total_resolved,
            ))
            # override_signal_strength is None because the current situation has
            # no resolved outcome yet — the signal-strength gate will not pass,
            # producing learning_level=NONE.  This is the correct honest state.
            override_result = compute_override_learning(OverrideLearningInput(
                similarity_result=similarity_result,
                confidence_result=confidence_result,
                override_signal_strength=None,
                global_override_rate=global_override_rate,
                decided_by=decided_by,
            ))
            situations   = build_situations(transitions, outcomes)
            solar_result = compute_solar_impact(SolarImpactInput(
                situations=situations,
                confidence_result=confidence_result,
            ))
            aggregate_result = aggregate_learning_signals(LearningAggregateInput(
                similarity_result=similarity_result,
                confidence_result=confidence_result,
                override_result=override_result,
                solar_result=solar_result,
            ))
            return compute_adaptive_profile(AdaptationInput(
                aggregate_result=aggregate_result,
                override_result=override_result,
                solar_result=solar_result,
            ))
        except Exception:
            _LOGGER.warning(
                "SmartShading: Learning Pipeline failed for %s — neutral profile used", window_id
            )
            return _NEUTRAL_ADAPTIVE_PROFILE
