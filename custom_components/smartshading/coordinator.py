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
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
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
from .models.multi_objective_outcome import (
    MOVE_CAUSE_ABSENCE,
    MOVE_CAUSE_COMFORT,
    MOVE_CAUSE_LIFECYCLE,
    MOVE_CAUSE_MANUAL,
    MOVE_CAUSE_NONE,
    MOVE_CAUSE_SAFETY,
)
from .engines.outcome_resolution import (
    MovementObservation,
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
import uuid

from .engines.adaptation_application import AdaptationTrace, apply_adaptive_profile
from .engines.config_fingerprint import ConfigGenerationTracker, compute_config_fingerprint
from .engines.decision_materiality import is_material_learning_decision
from .engines.zone_temperature import aggregate_zone_temperature, source_reliability_factor
from .engines.thermal_response_engine import (
    context_key as thermal_context_key,
    recompute_model,
    select_observation_window,
)
from .models.thermal_response import ThermalResponseModel, ThermalResponseObservation
from .engines.window_attribution import WindowEventFacts, classify_window_attribution
from .engines.window_contribution_engine import (
    WindowPriorFacts,
    compute_geometric_solar_prior,
    derive_eligibility,
    recompute_contribution_models,
)
from .models.window_contribution import (
    ATTR_WINDOW_ISOLATED,
    WindowContributionEvidence,
    WindowContributionModel,
    event_weight_for,
)
from .engines.shadow_eligibility import ShadowEligibilityInput, evaluate_shadow_eligibility
from .engines.shadow_engine import (
    CandidateReasonInput,
    compute_candidate_reason,
    compute_shadow_candidate,
    evaluate_supported_status,
)
from .models.shadow_proposal import ShadowEvaluation, ShadowProposal, STATUS_SUPPORTED
from .engines.experiment_eligibility import (
    ExperimentEligibilityInput,
    evaluate_experiment_eligibility,
)
from .engines.experiment_engine import (
    ExperimentEvaluationInput,
    P8AdoptionInput,
    derive_p8_adoption_eligible,
    evaluate_experiment,
    is_cooldown_active,
    reconcile_restored_experiments,
    revalidate_experiment_candidate,
)
from .models.bounded_experiment import (
    ACTIVE_STATUSES as _EXP_ACTIVE_STATUSES,
    EVAL_DEGRADED,
    EVAL_IMPROVED,
    EVAL_NO_DEGRADATION,
    EVAL_PREFERENCE_REJECTED,
    EXPERIMENT_HISTORY_PER_WINDOW,
    STATUS_ABORTED,
    STATUS_ACCEPTED_FOR_P8,
    STATUS_ACTIVATED,
    STATUS_ARMED,
    STATUS_COMPLETED,
    STATUS_INTERRUPTED_PARTIAL,
    STATUS_OBSERVING,
    STATUS_REJECTED,
    BoundedExperiment,
)
from .models.decision_provenance import (
    AdaptationDecision,
    AdaptationStep,
    BaselineDecision,
    DecisionCandidate,
    DecisionContext,
    DecisionProvenance,
    DispatchProvenance,
    LearningDecisionRecord,
    ModelEligibility,
    ResolvedDecision,
    SOURCE_ADAPTIVE_HEAT,
    SOURCE_ADAPTIVE_SOLAR,
    SOURCE_FORECAST_MODIFIER,
    SOURCE_MANUAL_PREFERENCE,
)
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
# P4: this remains the cold-start / low-confidence / no-temperature fallback;
# a confident per-zone ThermalResponseModel may select a different window
# (bounded by hard caps) at pending creation.
_OUTCOME_OBSERVATION_DELAY_MIN: int = 30

# P4 — per-zone thermal observation/store caps.
_THERMAL_OBS_CAP_PER_ZONE: int = 300
_THERMAL_SAMPLE_CAP: int = 6

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
STARTUP_GRACE_CYCLES: int = 1


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
    # post-tick override state — matches what CommandFilter uses this cycle.
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
    # Override-reference diagnostics (Step 9G5c-diag): captured in the
    # non-safety path alongside override tick(). Defaults apply for the
    # safety path (tick not called) and the no-sun fast-path.
    override_ref_source: str | None = None
    prev_observation_was_available: bool = False
    last_commanded_was_available: bool = False
    # True when observed_internal was non-None this cycle — meaning a valid
    # cover position was stored in _prev_observed_internal for the next cycle.
    current_observation_available: bool = False

    # --- P2 Decision Provenance inputs (captured in pass 1) ---
    # Deterministic baseline decision (no learning), in internal convention.
    baseline_state: ShadingState | None = None
    baseline_target_internal: int | None = None
    baseline_decided_by: str | None = None
    # Applied adaptation provenance inputs.
    adapt_trace: object | None = None
    forecast_modifier: object | None = None
    any_pos_adapted: bool = False
    normal_cfg_ha_for_prov: int | None = None
    normal_eff_ha_for_prov: int | None = None
    adapt_confidence_level: str | None = None
    adapt_strength: float = 0.0
    # Decision context inputs.
    config_fingerprint: str = ""
    config_generation: int = 0
    lifecycle_state_value: str = "day"
    absence_active_at_decision: bool = False
    manual_override_active_at_decision: bool = False
    # P2 — decision_id shared with the cycle's PendingOutcome (authoritative link).
    decision_id: str | None = None


def _apply_window_behavior_mode(
    wdi: WindowDecisionInput,
    behavior_mode: WindowBehaviorMode,
) -> WindowDecisionInput:
    """Apply per-window behavior-mode masking to a WindowDecisionInput.

    Pure function (P2 extraction).  Safety (Tier 1) is never suppressed here —
    masking only disables heat/solar/glare/absence/lifecycle per the mode.
    The deterministic baseline pass and the adapted pass both route through
    this helper so the baseline reflects the same mode restrictions.
    """
    if behavior_mode is WindowBehaviorMode.ABSENCE_ONLY:
        return replace(
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
    if behavior_mode is WindowBehaviorMode.ABSENCE_AND_SCHEDULE:
        return replace(
            wdi,
            effective_behavior=replace(
                wdi.effective_behavior,
                heat_outdoor_threshold_c=None,
                heat_indoor_threshold_c=None,
                solar_gain_suppresses_shading=True,
                glare_protection_enabled=False,
            ),
        )
    if behavior_mode is WindowBehaviorMode.DISABLED_AUTOMATIC:
        return replace(
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
    return wdi


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
        # Phase 9F4b-3: pending outcome queue.  P2: now persisted (restart-safe)
        # with an interruption gate applied on restore.
        self._pending_outcomes = PendingOutcomeQueue()
        # P2 Decision Provenance — per-window last persisted decision summary
        # (materiality dedup), config-fingerprint generation tracker, and a
        # monotonic per-cycle counter for grouping windows decided together.
        self._last_decision_summaries: dict[str, object] = {}
        self._config_generation_tracker = ConfigGenerationTracker()
        self._cycle_counter: int = 0
        # P2.6 — (window_id, decision_timestamp) keys of pending observations that
        # survived a restart with an interruption; their resolved outcome is
        # downgraded to interrupted_partial (never complete).
        self._interrupted_decision_keys: set[tuple[str, datetime]] = set()
        # P3 — deterministic movement counters per active observation window and a
        # rolling comfort-target history (HA) for oscillation detection.
        self._movement_acc: dict[str, dict] = {}
        self._recent_comfort_targets: dict[str, list[int]] = {}
        # P4 — per-zone thermal response model + bounded observations + open
        # observation accumulator + sparse sample buffer.  Keyed by zone_id
        # (== this entry's single zone).  Nothing is shared between config entries.
        self._thermal_models: dict[str, ThermalResponseModel] = {}
        self._thermal_observations: dict[str, list[ThermalResponseObservation]] = {}
        self._thermal_open: dict[str, dict] = {}          # zone_id → open observation context
        self._thermal_prev_zone_temp: dict[str, float] = {}
        self._thermal_sampled_cycle: dict[str, int] = {}  # sample once per zone per cycle
        self._thermal_last_obs_cycle: dict[str, int] = {} # multi-window dedupe per cycle
        # P5 — per-window relative contribution models + bounded evidence (per
        # window).  Keyed by window_id; each config entry (= zone) is independent.
        self._contribution_models: dict[str, WindowContributionModel] = {}
        self._contribution_evidence: dict[str, list[WindowContributionEvidence]] = {}
        # P6 — shadow proposals (analysis only; never applied).  Active proposals
        # keyed by (window_id, intensity, context_family); bounded terminal history.
        self._shadow_active: dict[tuple, object] = {}
        self._shadow_history: list[object] = []
        # P7 — bounded experiments.  At most ONE active experiment per zone.
        self._experiments_active: dict[str, BoundedExperiment] = {}   # zone_id → experiment
        self._experiment_history: list[BoundedExperiment] = []
        self._experiment_zone_last_activation: dict[str, datetime] = {}
        # Per-cycle injection context (window_id → dict), reset each cycle.
        self._cycle_experiment: dict[str, dict] = {}
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
        # Unsubscribe callbacks for presence state change listeners.
        # Populated by async_setup_presence_listeners(); cleared by teardown.
        self._unsub_presence_listeners: list[Callable[[], None]] = []
        # Presence dispatch generation (v1.0.4 stale-intent guard).
        # Incremented by each _on_presence_change callback; checked inside the
        # dispatch lock to cancel non-safety intents computed before a newer
        # presence event invalidated the batch.
        self._dispatch_generation: int = 0
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
        # Per-window previous-cycle observed position (internal convention).
        # Used by the override-tick own-command guard when _last_commanded=None:
        # comparing observed against the PREVIOUS cycle's observed position
        # detects real user movement (delta > 0) without falsely triggering
        # on a cover that is legitimately at a non-target position at restart.
        self._prev_observed_internal: dict[str, int | None] = {}
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
                    experiments_enabled=_ctrl.get("experiments_enabled", False),
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
    # Presence listener fan-out
    # ------------------------------------------------------------------

    def async_setup_presence_listeners(self, entry: ConfigEntry) -> None:
        """Register immediate-refresh listeners for all configured presence entities.

        When any presence entity changes state, an immediate coordinator refresh
        is requested rather than waiting for the next 5-minute polling cycle.
        This ensures that a global away→home transition causes all affected zone
        coordinators to recalculate within the same event-handling window.

        Per-zone isolation: each coordinator subscribes only to its own presence
        entities, so zones with different presence sensors react independently.

        unknown/unavailable are handled safely by _read_presence() which treats
        all-unavailable as "present" (safe default, never triggers absence).

        Idempotency: teardown cancels all listeners and clears the list, so
        reload/unload can never leave stale subscriptions.
        """
        if not self._presence_entity_ids:
            return

        @callback
        def _on_presence_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            # Entity removed or added with no prior state — not a meaningful change.
            if new_state is None or old_state is None:
                return
            # Deduplicate: skip if state value is identical (e.g. duplicate events).
            if new_state.state == old_state.state:
                return
            # Increment before scheduling so any intents currently waiting
            # for the dispatch lock see the updated generation and self-cancel.
            self._dispatch_generation += 1
            self.hass.async_create_task(self.async_request_refresh())

        for entity_id in self._presence_entity_ids:
            unsub = async_track_state_change_event(
                self.hass, entity_id, _on_presence_change
            )
            self._unsub_presence_listeners.append(unsub)

        # Register teardown so listeners are always cleaned up on entry unload,
        # even if async_unload_entry is not called in the normal sequence.
        entry.async_on_unload(self.async_teardown_presence_listeners)

    def async_teardown_presence_listeners(self) -> None:
        """Cancel all presence state change listeners.

        Called by entry.async_on_unload() registered in async_setup_presence_listeners()
        and explicitly in async_unload_entry() as a safety net.  Idempotent:
        safe to call multiple times.
        """
        for unsub in self._unsub_presence_listeners:
            unsub()
        self._unsub_presence_listeners.clear()

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
            experiments_enabled=current.experiments_enabled,
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
            experiments_enabled=current.experiments_enabled,
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

    async def async_set_zone_experiments_enabled(
        self, zone_id: str, enabled: bool
    ) -> None:
        """Toggle experiments_enabled ("Lernexperimente") for a zone.

        When turned OFF, any active experiment for the zone is logically aborted
        (authority removed immediately; no proactive inverse command).  Shadow
        proposals, learned models and experiment history are preserved.  The
        switch state persists regardless of observation/active-control state.
        """
        current = self.effective_zone_execution(zone_id)
        self._zone_execution_overrides[zone_id] = ZoneExecutionConfig(
            observation_enabled=current.observation_enabled,
            active_control_enabled=current.active_control_enabled,
            experiments_enabled=enabled,
        )
        if not enabled:
            try:
                self._abort_zone_experiment(zone_id, "experiments_disabled", dt_util.utcnow())
            except Exception:
                _LOGGER.warning("Learning: experiment abort on disable failed for %s", zone_id)
        self._persist_zone_controls()
        await self.async_request_refresh()

    def _persist_zone_controls(self) -> None:
        """Write current zone execution overrides into config_entry.options."""
        zone_controls = {
            zone_id: {
                "observation_enabled": cfg.observation_enabled,
                "active_control_enabled": cfg.active_control_enabled,
                "experiments_enabled": cfg.experiments_enabled,
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
            # P2: reconcile restore extras — config generations + restart-safe
            # pending outcomes (with the interruption gate applied).
            try:
                _extras = self._learning_persistence.last_restore_extras
                if _extras is not None:
                    self._config_generation_tracker = ConfigGenerationTracker.from_storage_dict(
                        _extras.config_generations
                    )
                    self._restore_pending_outcomes(_extras.pending_outcomes, _restore_now)
                    # P4: restore per-zone thermal models + observations.
                    self._thermal_models = dict(_extras.thermal_models)
                    self._thermal_observations = {
                        z: list(lst) for z, lst in _extras.thermal_observations.items()
                    }
                    # P5: restore per-window contribution models + evidence.
                    self._contribution_models = dict(_extras.window_contribution_models)
                    self._contribution_evidence = {
                        w: list(lst) for w, lst in _extras.window_contribution_evidence.items()
                    }
                    # P6: restore shadow proposals (active by key vs terminal history).
                    _terminal = {"rejected", "expired", "invalidated"}
                    self._shadow_active = {}
                    self._shadow_history = []
                    for _p in _extras.shadow_proposals:
                        if _p.status in _terminal:
                            self._shadow_history.append(_p)
                        else:
                            self._shadow_active[_p.proposal_key] = _p
                    # P7: restore bounded experiments with the restart safety rule
                    # (activated/observing can NEVER resume as complete; no target
                    # is re-injected by restore alone).
                    self._experiments_active, self._experiment_history = (
                        reconcile_restored_experiments(
                            _extras.bounded_experiments, _restore_now)
                    )
            except Exception:
                _LOGGER.warning("Learning: failed to reconcile restore extras (non-fatal)")
            # Write a schema-valid storage file immediately on first setup so
            # /config/.storage/smartshading_learning_<id> is visible right away,
            # even before any learning data has been collected.  Also performs the
            # one-shot controlled save after a v1→v2 migration (coordinator owns it).
            if self._learning_persistence.fresh_start or self._learning_persistence.migration_dirty:
                await self._learning_persistence.async_save(
                    self._learning_store,
                    set(self.windows.keys()),
                    _restore_now,
                    target_adapter=self._target_position_adapter,
                    pending_outcomes=self._pending_outcomes.all_pending(),
                    config_generations=self._config_generation_tracker.to_storage_dict(),
                    thermal_models=self._thermal_models_storage(),
                    thermal_observations=self._thermal_observations_storage(),
                    window_contribution_models=self._contribution_models_storage(),
                    window_contribution_evidence=self._contribution_evidence_storage(),
                    shadow_proposals=self._shadow_proposals_storage(),
                    bounded_experiments=self._experiments_storage(),
                )
                self._persistence_last_save_at = _restore_now
                self._learning_persistence.clear_migration_dirty()

        # P2: monotonic per-cycle id for grouping all windows decided this tick.
        self._cycle_counter += 1

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
                                solar_exposure_at_decision=_lc_pending.solar_exposure_at_decision,
                                cleared_by_lifecycle=True,
                                observation_interrupted=(window_id, _lc_pending.decision_timestamp)
                                in self._interrupted_decision_keys,
                                movement_observation=self._movement_take(window_id, MOVE_CAUSE_LIFECYCLE),
                            ),
                        )
                        self._store_outcome(_outcome)
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
                    and not sun_geometry.elevation_clipped
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
            # P2 provenance: snapshot the pre-adaptation (config) WDI so the
            # deterministic baseline can be evaluated from the same input.
            _wdi_preadapt = wdi
            _adapt_trace = None
            _any_pos_adapted = False
            _normal_cfg_ha = None
            _normal_eff_ha = None
            _adapt_conf_level = "very_low"
            _adapt_strength = 0.0

            # Adaptation Application (9F17): apply the last-cycle AdaptiveProfile
            # to the resolved BehaviorConfig.  Only when obs_enabled=True — when
            # observation is disabled, _NEUTRAL_ADAPTIVE_PROFILE is always used
            # and the WDI remains as resolved from config (no learning-based change).
            if obs_enabled:
                _adapt_profile = self._adaptive_profiles.get(window_id, _NEUTRAL_ADAPTIVE_PROFILE)
                _adapt_conf_level = _adapt_profile.confidence_level
                _adapt_strength = _adapt_profile.adaptation_strength
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

                # P7 — bounded experiment injection (single Tier-5 parameter).
                # Strictly gated; never bypasses any higher authority because it
                # only overrides one intensity position BEFORE the tier evaluation,
                # so every downstream resolver/clamp/harmonization/command-filter
                # still applies.  Returns the (possibly) modified wdi.
                try:
                    wdi = self._experiment_try_inject(
                        zone=zone, window=window, window_id=window_id, wdi=wdi,
                        eff_ha={"light": _light_eff_ha, "normal": _normal_eff_ha,
                                "strong": _strong_eff_ha},
                        cfg_ha={"light": _light_cfg_ha, "normal": _normal_cfg_ha,
                                "strong": _strong_cfg_ha},
                        exposure_wm2=exposure.effective_exposure,
                        outdoor_temp=weather_inputs.outdoor_temperature,
                        in_solar_sector=_effective_in_solar_sector,
                        manual_pref_active=_any_pos_adapted,
                        current_state=current_state,
                        now=now,
                    )
                except Exception:
                    _LOGGER.warning(
                        "Learning: experiment injection failed for %s (non-fatal)", window_id
                    )
            # else: wdi stays as resolved from config; neutral profile is implied.

            # Per-window behavior mode (v1.0): restrict which tiers are active.
            # Safety (Tier 1: Storm/Wind) is never suppressed regardless of mode.
            # Extracted to a pure helper (P2) so the deterministic baseline pass
            # applies the identical masking before its own tier evaluation.
            wdi = _apply_window_behavior_mode(wdi, window.behavior_mode)

            tier_decision = self._tier_orchestrator.evaluate_window(wdi)

            # P2 Decision Provenance: evaluate the deterministic baseline from the
            # un-adapted config WDI.  The orchestrator is pure (stateless), so this
            # extra evaluation has no side effects: it never touches StateGuard,
            # transitions, override detection, AssumedState, or dispatch.  Used for
            # provenance only and only when observation is enabled.
            _baseline_decision = None
            _prov_fingerprint = ""
            _prov_generation = 0
            # P2: one decision_id per window per observation cycle.  Shared by the
            # PendingOutcome (if a transition creates one) and the decision record,
            # so the outcome is later attached by decision_id (authoritative v2 path).
            _decision_id = uuid.uuid4().hex if obs_enabled else None
            if obs_enabled:
                try:
                    _wdi_baseline = _apply_window_behavior_mode(_wdi_preadapt, window.behavior_mode)
                    _baseline_decision = self._tier_orchestrator.evaluate_window(_wdi_baseline)
                    _prov_fingerprint, _prov_generation = self._config_fingerprint_for_window(window, zone)
                except Exception:
                    _baseline_decision = None

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
            # Override-reference diagnostics: defaults for the safety path (tick not
            # called there). Overwritten in the else-branch below.
            _override_ref_source: str | None = None
            _last_commanded_was_available: bool = False
            _prev_obs_was_available: bool = False
            _observed_internal_stored: bool = False
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
                                    solar_exposure_at_decision=_safety_pending.solar_exposure_at_decision,
                                    cleared_by_safety=True,
                                    observation_interrupted=(window_id, _safety_pending.decision_timestamp)
                                    in self._interrupted_decision_keys,
                                    movement_observation=self._movement_take(window_id, MOVE_CAUSE_SAFETY),
                                ),
                            )
                            self._store_outcome(_outcome)
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
                # Override-reference selection (Fix D + observed_internal fallback).
                # Priority:
                #   1. last_commanded: set only by dispatch — never by passive observation.
                #      The correct reference when SmartShading has dispatched at least once.
                #   2. prev_observed: previous cycle's observed position.
                #      Cover stable → delta=0 → guard fires → no false override.
                #      Cover moved  → delta>0 → guard skips → real override detected.
                #   3. observed_internal: used when NEITHER is available (first cycle,
                #      or cover was unavailable last cycle).  abs(observed-observed)=0
                #      → guard always fires → no false positive on first observation.
                _cover_group = self.cover_groups.get(window.cover_group_id)
                _cov_id = _cover_group.cover_ids[0] if _cover_group and _cover_group.cover_ids else None
                _assumed_st = self.assumed_state_manager.get_state(_cov_id, now) if _cov_id else None
                _last_commanded = _assumed_st.last_commanded_position if _assumed_st is not None else None
                _prev_obs = self._prev_observed_internal.get(window_id)
                _last_commanded_was_available = _last_commanded is not None
                _prev_obs_was_available = _prev_obs is not None
                if _last_commanded is not None:
                    _override_assumed = _last_commanded
                    _override_ref_source = "last_commanded"
                elif _prev_obs is not None:
                    _override_assumed = _prev_obs
                    _override_ref_source = "previous_observation"
                else:
                    # First observation or cover was unavailable last cycle.
                    # observed_internal may itself be None (cover still unavailable);
                    # the OverrideDetector fail-safe handles that case (returns early).
                    _override_assumed = observed_internal
                    _override_ref_source = "unavailable"
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
                self._prev_observed_internal[window_id] = observed_internal
                _observed_internal_stored = observed_internal is not None

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
                            is_in_solar_sector=_effective_in_solar_sector,
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
                            # P3 movement: record this transition into the OLD window
                            # before it resolves, classifying comfort vs excluded.
                            _new_target_ha = (
                                to_ha_position(tier_decision.target_position)
                                if tier_decision.target_position is not None else None
                            )
                            _mv_cause = self._classify_movement_cause(new_state)
                            if _mv_cause == MOVE_CAUSE_COMFORT:
                                self._movement_note_comfort_transition(window_id, _new_target_ha)
                            else:
                                self._movement_note_excluded_transition(window_id)

                            # P4 active authority: choose the observation window
                            # from the per-zone thermal model (cold-start/low-conf/
                            # no-temp → 30 min = unchanged behavior).
                            _zone_reading = self._read_zone_temperature()
                            _obs_window_min, _thermal_authority, _thermal_conf = (
                                self._thermal_select_window(
                                    window.zone_id,
                                    outdoor=weather_inputs.outdoor_temperature,
                                    exposure=exposure.effective_exposure,
                                    temperature_available=_zone_reading.available,
                                    now=now,
                                )
                            )
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
                                solar_exposure_at_decision=exposure.effective_exposure,
                                indoor_temp_outcome_delay_min=_obs_window_min,
                                # P2: authoritative link shared with the decision record.
                                decision_id=_decision_id,
                                config_fingerprint=_prov_fingerprint or None,
                                created_at_utc=now,
                                # P4: observation-window authority provenance.
                                thermal_authority_applied=_thermal_authority,
                                thermal_confidence_at_decision=_thermal_conf,
                                # P7: link the pending to an active experiment, only
                                # when this decision actually used the experiment's
                                # injected intensity (exact decision_id/experiment_id
                                # linkage — never a timestamp fallback).
                                experiment_id=self._experiment_pending_link(
                                    window_id, new_state, _decision_id, now,
                                ),
                            )
                            _old_pending = self._pending_outcomes.replace(_new_pending)
                            if _old_pending is not None:
                                _outcome = resolve_outcome(
                                    _old_pending,
                                    OutcomeResolutionInput(
                                        trigger=OutcomeResolutionTrigger.STATE_CHANGE,
                                        resolution_timestamp=now,
                                        indoor_temp_outcome_c=indoor_temperature,
                                        solar_exposure_at_decision=_old_pending.solar_exposure_at_decision,
                                        observation_interrupted=(window_id, _old_pending.decision_timestamp)
                                        in self._interrupted_decision_keys,
                                        movement_observation=self._movement_take(window_id, _mv_cause),
                                    ),
                                )
                                self._store_outcome(_outcome)
                            # Start a fresh movement window for the new pending.
                            self._movement_reset(window_id, _new_target_ha)
                            # P4: open/extend the per-zone thermal observation.
                            self._thermal_start_or_extend(
                                window.zone_id, _decision_id, now=now,
                                indoor=indoor_temperature,
                                outdoor=weather_inputs.outdoor_temperature,
                                exposure=exposure.effective_exposure,
                                target_ha=(
                                    to_ha_position(tier_decision.target_position)
                                    if tier_decision.target_position is not None else None
                                ),
                                shading_state=new_state.value,
                            )
                            # P5: this window materially changed in the zone event.
                            self._thermal_mark_material_window(window.zone_id, window_id)
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
                                    # P3: convert to logical HA (0=closed,100=open) BEFORE
                                    # resolve — no internal position enters the outcome.
                                    override_target_ha=to_ha_position(current_override.override_position),
                                    final_requested_target_ha=(
                                        to_ha_position(_ov_pending.target_position)
                                        if _ov_pending.target_position is not None else None
                                    ),
                                    solar_exposure_at_decision=_ov_pending.solar_exposure_at_decision,
                                    observation_interrupted=(window_id, _ov_pending.decision_timestamp)
                                    in self._interrupted_decision_keys,
                                    movement_observation=self._movement_take(window_id, MOVE_CAUSE_MANUAL),
                                ),
                            )
                            self._store_outcome(_outcome)
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
                                    override_target_ha=to_ha_position(current_override.override_position),
                                    final_requested_target_ha=(
                                        to_ha_position(_ren_pending.target_position)
                                        if _ren_pending.target_position is not None else None
                                    ),
                                    solar_exposure_at_decision=_ren_pending.solar_exposure_at_decision,
                                    observation_interrupted=(window_id, _ren_pending.decision_timestamp)
                                    in self._interrupted_decision_keys,
                                    movement_observation=self._movement_take(window_id, MOVE_CAUSE_MANUAL),
                                ),
                            )
                            self._store_outcome(_outcome)
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
                                        solar_exposure_at_decision=_to_pending.solar_exposure_at_decision,
                                        observation_interrupted=(window_id, _to_pending.decision_timestamp)
                                        in self._interrupted_decision_keys,
                                        movement_observation=self._movement_take(window_id, MOVE_CAUSE_NONE),
                                        thermal_maturity=self._thermal_maturity_for(
                                            window.zone_id, _to_pending, now
                                        ),
                                    ),
                                )
                                self._store_outcome(_outcome)
                except Exception:
                    _LOGGER.warning(
                        "Learning: outcome resolution (timeout) failed for %s", window_id
                    )

            # P4: sample the zone temperature into any open thermal observation
            # (once per zone per cycle, bounded).
            if obs_enabled:
                try:
                    self._thermal_sample(window.zone_id, now)
                except Exception:
                    pass

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
                    is_manual_override=current_override is not None,
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
                    and _effective_in_solar_sector
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
            # is_override_active uses current_override (post-tick), consistent with
            # CommandFilter which also uses the post-tick state so that a freshly
            # detected override is honoured in the same cycle it was found.
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
                is_override_active=current_override is not None,
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
                override_ref_source=_override_ref_source,
                prev_observation_was_available=_prev_obs_was_available,
                last_commanded_was_available=_last_commanded_was_available,
                current_observation_available=_observed_internal_stored,
                # --- P2 Decision Provenance inputs ---
                baseline_state=(_baseline_decision.shading_state if _baseline_decision is not None else None),
                baseline_target_internal=(_baseline_decision.target_position if _baseline_decision is not None else None),
                baseline_decided_by=(_baseline_decision.decided_by if _baseline_decision is not None else None),
                adapt_trace=_adapt_trace,
                forecast_modifier=_forecast_modifier,
                any_pos_adapted=_any_pos_adapted,
                normal_cfg_ha_for_prov=_normal_cfg_ha,
                normal_eff_ha_for_prov=_normal_eff_ha,
                adapt_confidence_level=_adapt_conf_level,
                adapt_strength=_adapt_strength,
                config_fingerprint=_prov_fingerprint,
                config_generation=_prov_generation,
                lifecycle_state_value=self._lifecycle_state.value,
                absence_active_at_decision=absence_active,
                manual_override_active_at_decision=(current_override is not None),
                decision_id=_decision_id,
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
        # Capture presence generation snapshot before any awaits in this pass.
        # If _on_presence_change fires during dispatch (between awaits), it
        # increments _dispatch_generation and stale intents self-cancel.
        _this_dispatch_gen = self._dispatch_generation
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
                        #     dispatch (≥1.5 s since the previous SENT command).
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
                                # Stale-intent guard: a presence event that fired
                                # while we waited for the lock or slept through the
                                # throttle already incremented _dispatch_generation.
                                # Cancel this non-safety intent; the refresh queued
                                # by that event will dispatch the correct state.
                                # Safety is exempt — always dispatches.
                                if self._dispatch_generation != _this_dispatch_gen:
                                    _exec_results.append(build_not_attempted_result(
                                        _intent,
                                        reason="stale_presence_superseded",
                                    ))
                                    continue
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
            # Post-dispatch diagnostic values: reflect the state AFTER this cycle
            # completes so the Support Export is not misleading for the first dispatch.
            # last_commanded_available: True if assumed_state_manager already had it
            # OR if a successful dispatch just set it this cycle.
            _diag_last_commanded_avail: bool = s.last_commanded_was_available or (
                _exec_plan_result is not None
                and _exec_plan_result.any_sent
                and not _exec_plan_result.any_failed
            )
            # previous_observation_available: True when a valid cover position was
            # captured this cycle and stored in _prev_observed_internal (post-cycle).
            # Reflects what will be available as the previous observation next cycle.
            _diag_prev_obs_avail: bool | None = (
                s.current_observation_available
                if s.override_ref_source is not None  # non-safety path ran
                else None  # safety / no-sun path: field stays None
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
                # Startup / override-reference diagnostics (Step 9G5c-diag).
                startup_grace_configured_cycles=STARTUP_GRACE_CYCLES,
                startup_initialization_complete=(self._startup_cycles_remaining == 0),
                previous_observation_available=_diag_prev_obs_avail,
                last_commanded_available=_diag_last_commanded_avail,
                override_reference_source=s.override_ref_source,
            )

            # --- P2 Decision Provenance: build dispatch provenance + record ---
            # Fully additive and guarded — never affects the cycle on failure.
            try:
                _dp_attempted = _exec_plan_result is not None and (
                    _exec_plan_result.any_sent or _exec_plan_result.any_failed
                )
                _dp_status = (
                    _last_exec_result.status.value if _last_exec_result is not None else None
                )
                # P3 movement: a CommandFilter block / not-attempted is NOT a command;
                # a successful service call counts as a successful command.
                self._movement_record_dispatch(
                    window_id,
                    attempted=bool(_dp_attempted),
                    success=bool(
                        _exec_plan_result is not None
                        and _exec_plan_result.any_sent
                        and not _exec_plan_result.any_failed
                    ),
                )
                _dispatch_prov = DispatchProvenance(
                    dispatch_allowed=(
                        _exec_filter_for_dispatch.allowed
                        if _exec_filter_for_dispatch is not None else None
                    ),
                    dispatch_filter_reason=(
                        _exec_filter_for_dispatch.blocked_reason
                        if _exec_filter_for_dispatch is not None else None
                    ) or _dispatch_suppressed_reason,
                    dispatch_attempted=bool(_dp_attempted),
                    dispatch_succeeded=(
                        bool(_exec_plan_result.any_sent and not _exec_plan_result.any_failed)
                        if _exec_plan_result is not None else None
                    ),
                    dispatch_status=_dp_status,
                    dispatch_error_category=(
                        _last_exec_result.error if _last_exec_result is not None else None
                    ),
                    requested_target_ha=(
                        _exec_filter_for_dispatch.target_position_ha
                        if _exec_filter_for_dispatch is not None else None
                    ),
                    transport_inversion_applied=(
                        s.exec_cap.invert_position if s.exec_cap is not None else False
                    ),
                )
                self._maybe_record_provenance(
                    window_id,
                    s,
                    harmonized_target_ha=harm.final_target_position_ha,
                    harmonized=harm.harmonized,
                    dispatch=_dispatch_prov,
                    now=now,
                )
                # P7: confirm or abort experiment activation from the real
                # dispatch result this cycle (command blocked/failed → not
                # activated).  Reliable feedback gates the confirmation class.
                try:
                    self._experiment_confirm_dispatch(
                        window_id,
                        dispatch_succeeded=bool(
                            _exec_plan_result is not None
                            and _exec_plan_result.any_sent
                            and not _exec_plan_result.any_failed
                        ),
                        has_reliable_feedback=(
                            s.exec_cap.has_reliable_position_feedback
                            if s.exec_cap is not None else False
                        ),
                        now=now,
                    )
                except Exception:
                    _LOGGER.warning(
                        "SmartShading: experiment dispatch-confirm failed for %s (non-fatal)",
                        window_id,
                    )
                # P5: record per-window event facts for the open zone observation.
                if s.obs_enabled and s.window.zone_id in self._thermal_open:
                    _obs_pos = (
                        s.exec_snapshot.assumed_position_internal
                        if s.exec_snapshot is not None and s.exec_snapshot.assumed_position_internal is not None
                        else (s.exec_snapshot.current_position_internal if s.exec_snapshot is not None else None)
                    )
                    self._thermal_record_window_facts(
                        s.window.zone_id, window_id,
                        command_status=(_dp_status.lower() if _dp_status else "none"),
                        harmonized=harm.harmonized,
                        has_reliable_feedback=(
                            s.exec_cap.has_reliable_position_feedback if s.exec_cap is not None else False
                        ),
                        active_control=s.active_control_enabled,
                        observed_position_internal=_obs_pos,
                        target_internal=s.exec_target_internal,
                    )
            except Exception:
                _LOGGER.warning(
                    "SmartShading: provenance recording failed for %s (non-fatal)", window_id
                )

        # Startup Grace Period (9G5): decrement at the end of the cycle so that
        # STARTUP_GRACE_CYCLES cycles are fully suppressed before dispatch is
        # allowed.  Decrementing here (after dispatch) rather than at the top of
        # the function ensures the count matches the number of suppressed cycles:
        # with STARTUP_GRACE_CYCLES=1, cycle 1 is suppressed and cycle 2 is
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
                pending_outcomes=self._pending_outcomes.all_pending(),
                config_generations=self._config_generation_tracker.to_storage_dict(),
                thermal_models=self._thermal_models_storage(),
                thermal_observations=self._thermal_observations_storage(),
                window_contribution_models=self._contribution_models_storage(),
                window_contribution_evidence=self._contribution_evidence_storage(),
                shadow_proposals=self._shadow_proposals_storage(),
                bounded_experiments=self._experiments_storage(),
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

    # ------------------------------------------------------------------
    # P2 Decision Provenance helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # P3 Movement counters (deterministic; no estimates)
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_movement_cause(state: ShadingState) -> str:
        if state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):
            return MOVE_CAUSE_SAFETY
        if state is ShadingState.NIGHT_CLOSED:
            return MOVE_CAUSE_LIFECYCLE
        if state is ShadingState.ABSENCE_CLOSED:
            return MOVE_CAUSE_ABSENCE
        if state is ShadingState.MANUAL_OVERRIDE:
            return MOVE_CAUSE_MANUAL
        return MOVE_CAUSE_COMFORT

    def _movement_reset(self, window_id: str, target_ha: int | None) -> None:
        """Start a fresh movement accumulator for a new observation window."""
        self._movement_acc[window_id] = {
            "decision_target_ha": target_ha,
            "command_attempt_count": 0,
            "successful_command_count": 0,
            "comfort_state_transition_count": 0,
            "excluded_transition_count": 0,
            "material_target_change_count": 0,
        }

    def _movement_record_dispatch(self, window_id: str, attempted: bool, success: bool) -> None:
        acc = self._movement_acc.get(window_id)
        if acc is None:
            return
        if attempted:
            acc["command_attempt_count"] += 1
        if success:
            acc["successful_command_count"] += 1

    def _movement_note_comfort_transition(self, window_id: str, target_ha: int | None) -> None:
        """Record a comfort-driven transition into the active accumulator and the
        rolling oscillation history."""
        acc = self._movement_acc.get(window_id)
        hist = self._recent_comfort_targets.setdefault(window_id, [])
        if acc is not None:
            acc["comfort_state_transition_count"] += 1
            prev = hist[-1] if hist else acc.get("decision_target_ha")
            if target_ha is not None and prev is not None and abs(target_ha - prev) >= 3:
                acc["material_target_change_count"] += 1
        if target_ha is not None:
            hist.append(target_ha)
            del hist[:-6]  # keep last 6

    def _movement_note_excluded_transition(self, window_id: str) -> None:
        acc = self._movement_acc.get(window_id)
        if acc is not None:
            acc["excluded_transition_count"] += 1

    def _movement_take(self, window_id: str, resolving_cause: str) -> MovementObservation:
        """Snapshot the accumulator for a resolving observation window."""
        acc = self._movement_acc.pop(window_id, None)
        hist = tuple(self._recent_comfort_targets.get(window_id, []))
        if acc is None:
            return MovementObservation(movement_cause=resolving_cause, target_history=hist)
        return MovementObservation(
            decision_target_ha=acc["decision_target_ha"],
            command_attempt_count=acc["command_attempt_count"],
            successful_command_count=acc["successful_command_count"],
            comfort_state_transition_count=acc["comfort_state_transition_count"],
            excluded_transition_count=acc["excluded_transition_count"],
            material_target_change_count=acc["material_target_change_count"],
            target_history=hist,
            movement_cause=resolving_cause,
        )

    # ------------------------------------------------------------------
    # P4 Thermal Response (per-zone == per-entry)
    # ------------------------------------------------------------------

    def _read_zone_temperature(self):
        """Robustly aggregate this zone's configured indoor temperature sensors.

        Returns a ZoneTemperatureReading.  Uses the existing (already
        zone-specific) indoor_temperature_sensor_ids — never a second source —
        and does NOT replace the Heat-Evaluator temperature path.
        """
        values: list[float | None] = []
        for sensor_id in self._indoor_temperature_sensor_ids:
            state = self.hass.states.get(sensor_id)
            if state is None or state.state in ("unknown", "unavailable"):
                values.append(None)
                continue
            values.append(WeatherEngine.parse_numeric_state(state.state))
        # previous value keyed by the (single) zone; first window's zone is fine
        prev = next(iter(self._thermal_prev_zone_temp.values()), None)
        reading = aggregate_zone_temperature(values, previous_value=prev)
        return reading

    def thermal_diagnostics(self, zone_id: str) -> dict:
        """Privacy-safe per-zone thermal diagnostics for the Support Export.

        No raw entity IDs.  Reflects current source classification, model state
        and the gate reason for the selected observation window.
        """
        reading = self._read_zone_temperature()
        model = self._thermal_models.get(zone_id)
        ctx = thermal_context_key(dt_util.utcnow(), None, None)
        window, reason = select_observation_window(
            model, ctx, temperature_available=reading.available
        )
        return {
            "configured_temperature_sensor_count": reading.configured_count,
            "valid_temperature_sensor_count": reading.valid_count,
            "temperature_source_available": reading.available,
            "temperature_source_kind": reading.source_kind,
            "aggregation_method": reading.aggregation_method,
            "temperature_value": reading.value,
            "thermal_model_active": bool(model and model.effective_observation_minutes is not None),
            "thermal_model_confidence": round(model.confidence, 3) if model else 0.0,
            "thermal_model_sample_count": model.sample_count if model else 0,
            "thermal_model_distinct_days": model.distinct_days if model else 0,
            "thermal_model_gate_reason": reason,
            "selected_observation_window_min": window,
        }

    # ------------------------------------------------------------------
    # P7 — Bounded experiment lifecycle (real cover movement, gated)
    # ------------------------------------------------------------------

    _EXP_STATE_FOR_INTENSITY = {
        "light": ShadingState.LIGHT_SHADE,
        "normal": ShadingState.NORMAL_SHADE,
        "strong": ShadingState.STRONG_SHADE,
    }

    def _experiment_context_family(self, now, outdoor, exposure) -> str:
        parts = thermal_context_key(now, outdoor, exposure).split("|")
        return f"{parts[0]}|{parts[-1]}"

    def _experiment_find_supported_proposal(self, window_id: str, ctx_family: str):
        """A supported shadow proposal for this window in the current context."""
        for (wid, _intensity, cfam), prop in self._shadow_active.items():
            if wid == window_id and cfam == ctx_family and prop.status == STATUS_SUPPORTED:
                return prop
        return None

    def _experiment_cooldown(self, zone_id: str, key: tuple, now: datetime):
        last_zone = self._experiment_zone_last_activation.get(zone_id)
        last_ctx = None
        last_rej = None
        win_count = 0
        cutoff = now - timedelta(days=30)
        for e in self._experiment_history:
            if e.experiment_key == key:
                if e.completed_at is not None:
                    if last_ctx is None or e.completed_at > last_ctx:
                        last_ctx = e.completed_at
                    if e.completed_at >= cutoff:
                        win_count += 1
                    if e.evaluation.decision in (EVAL_DEGRADED, EVAL_PREFERENCE_REJECTED):
                        if last_rej is None or e.completed_at > last_rej:
                            last_rej = e.completed_at
        return is_cooldown_active(
            now=now, last_zone_activation_at=last_zone, last_context_completion_at=last_ctx,
            last_rejection_at=last_rej, window_activations_last_30d=win_count,
        )

    def _experiment_try_inject(
        self, *, zone, window, window_id, wdi, eff_ha, cfg_ha, exposure_wm2,
        outdoor_temp, in_solar_sector, manual_pref_active, current_state, now,
    ):
        """Plan/arm + (single) inject a bounded experiment parameter.  Returns wdi
        (possibly with one intensity position overridden).  Fully gated; never
        bypasses a higher authority."""
        self._cycle_experiment.pop(window_id, None)
        zone_id = window.zone_id
        exec_cfg = self.effective_zone_execution(zone_id)
        # Three mandatory user levels.
        if not (exec_cfg.observation_enabled and exec_cfg.active_control_enabled
                and exec_cfg.experiments_enabled):
            return wdi

        exp = self._experiments_active.get(zone_id)
        if exp is not None and exp.window_id != window_id:
            return wdi  # zone's single slot is held by another window

        ctx_family = self._experiment_context_family(now, outdoor_temp, exposure_wm2)
        proposal = self._experiment_find_supported_proposal(window_id, ctx_family)
        if exp is not None and (proposal is None or proposal.intensity_level != exp.intensity_level):
            return wdi
        if proposal is None:
            return wdi
        intensity = proposal.intensity_level
        state = self._EXP_STATE_FOR_INTENSITY.get(intensity)
        if state is None or intensity not in eff_ha:
            return wdi

        gen = self._thermal_config_generation(zone_id)
        tmodel = self._thermal_models.get(zone_id)
        contrib = self._contribution_models.get(window_id)
        _shadow_elig, contrib_exp_elig = derive_eligibility(contrib, gen)
        cg = self.cover_groups.get(window.cover_group_id)
        cap = (
            self._get_or_detect_capability(cg.cover_ids[0])
            if cg is not None and cg.cover_ids else None
        )
        reliable_fb = bool(cap.has_reliable_position_feedback) if cap is not None else False
        reading = self._read_zone_temperature()

        cur_auth_ha = eff_ha[intensity]
        cfg_base_ha = cfg_ha[intensity]
        hw = default_hardware_settings(cg.hardware_type) if cg is not None else {}
        reval = revalidate_experiment_candidate(
            current_authoritative_target_ha=cur_auth_ha,
            real_regular_target_ha=cur_auth_ha,
            configured_base_target_ha=cfg_base_ha,
            new_state=state,
            daytime_min_ha=hw.get("daytime_min_open_position_ha"),
            ahb_position_ha=hw.get("anti_heat_buildup_position_ha"),
            ahb_enabled=False,
            hardware_type=(cg.hardware_type if cg is not None else CoverHardwareType.GENERIC),
            in_solar_sector=in_solar_sector,
            effective_exposure_wm2=exposure_wm2,
        )

        elig = evaluate_experiment_eligibility(ExperimentEligibilityInput(
            intensity_level=intensity,
            observation_enabled=exec_cfg.observation_enabled,
            active_control_enabled=exec_cfg.active_control_enabled,
            experiments_enabled=exec_cfg.experiments_enabled,
            shadow_status=proposal.status, proposal_present=True,
            p5_reference_valid=window_id in self._contribution_models,
            contribution_current=contrib_exp_elig,
            attribution_quality=proposal.attribution_quality,
            config_generation_matches=(proposal.config_generation == gen),
            thermal_available=tmodel is not None,
            thermal_mature=bool(tmodel and tmodel.active),
            thermal_reliability=(tmodel.confidence if tmodel else 0.0),
            temperature_source_available=reading.available,
            preference_veto=proposal.evaluation.preference_veto,
            manual_preference_active=manual_pref_active,
            fully_automatic=getattr(window.behavior_mode, "name", "") == "FULLY_AUTOMATIC",
            manual_override_active=self._override_detector.get(window_id, now) is not None,
            safety_active=current_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE),
            lifecycle_active=self._lifecycle_state.value != "day",
            presence_absence_transition=False,
            solar_context_ok=((exposure_wm2 or 0.0) >= 150.0
                              and ctx_family == proposal.context_family),
            reliable_position_feedback=reliable_fb,
            confounded=False,
            candidate_valid=reval.valid,
            other_active_zone_experiment=False,
            cooldown_active=self._experiment_cooldown(zone_id, proposal.proposal_key, now)[0],
        ))
        if not elig.eligible:
            # Lost eligibility while an experiment was armed for this window →
            # invalidate/abort logically (no command).
            if exp is not None:
                self._abort_zone_experiment(
                    zone_id, f"ineligible:{elig.block_reason}", now)
            return wdi

        # Arm a new experiment if none exists for the zone.
        if exp is None:
            exp = BoundedExperiment(
                experiment_id=uuid.uuid4().hex, source_shadow_id=proposal.shadow_id,
                window_id=window_id, zone_id=zone_id, intensity_level=intensity,
                context_family=ctx_family, created_at=now, updated_at=now,
                config_generation=gen,
                source_decision_ids=proposal.source_decision_ids[:10],
                status=STATUS_ARMED, planned_start_at=now,
            )
        exp = replace(
            exp, updated_at=now, status=STATUS_ARMED,
            baseline_parameter_target_ha=cur_auth_ha,
            experiment_parameter_target_ha=reval.experiment_parameter_target_ha,
            expected_final_candidate_target_ha=reval.expected_final_candidate_target_ha,
            cumulative_delta_from_config_ha=reval.cumulative_delta_from_config_ha,
            eligibility_snapshot=elig.to_dict(),
        )
        self._experiments_active[zone_id] = exp

        # Inject: override exactly this intensity position (Tier-5 parameter).
        param_internal = to_internal_position(reval.experiment_parameter_target_ha)
        eb = wdi.effective_behavior
        if intensity == "light":
            eb = replace(eb, light_shade_position=param_internal)
        elif intensity == "normal":
            eb = replace(eb, normal_shade_position=param_internal)
        else:
            eb = replace(eb, strong_shade_position=param_internal)
        wdi = replace(wdi, effective_behavior=eb)
        self._cycle_experiment[window_id] = {
            "experiment_id": exp.experiment_id, "zone_id": zone_id,
            "intensity": intensity, "state": state,
            "baseline_ha": cur_auth_ha,
            "param_ha": reval.experiment_parameter_target_ha,
            "expected_final_ha": reval.expected_final_candidate_target_ha,
            "configured_base_ha": cfg_base_ha,
        }
        return wdi

    def _experiment_pending_link(self, window_id, new_state, decision_id, now):
        """Mark the experiment activated when the committed decision actually used
        the injected intensity, and return its experiment_id for the pending."""
        ctx = self._cycle_experiment.get(window_id)
        if ctx is None:
            return None
        if self._SHADE_INTENSITY.get(new_state) != ctx["intensity"]:
            return None  # a higher authority chose a different state → not activated
        zone_id = ctx["zone_id"]
        exp = self._experiments_active.get(zone_id)
        if exp is None or exp.experiment_id != ctx["experiment_id"]:
            return None
        self._experiments_active[zone_id] = replace(
            exp, status=STATUS_ACTIVATED, activated_at=now, updated_at=now,
            experiment_decision_id=decision_id,
            outcome_reference=decision_id,
            confirmation="command_attempted",
        )
        self._experiment_zone_last_activation[zone_id] = now
        self._learning_dirty = True
        return ctx["experiment_id"]

    def _experiment_confirm_dispatch(
        self, window_id, *, dispatch_succeeded, has_reliable_feedback, now,
    ):
        """Confirm or abort activation based on the real dispatch result this
        cycle.  A command that was blocked/failed means NOT activated."""
        ctx = self._cycle_experiment.get(window_id)
        if ctx is None:
            return
        zone_id = ctx["zone_id"]
        exp = self._experiments_active.get(zone_id)
        if exp is None or exp.experiment_id != ctx["experiment_id"]:
            return
        if exp.status != STATUS_ACTIVATED:
            return  # decision did not land on the experiment intensity this cycle
        if not dispatch_succeeded:
            self._abort_zone_experiment(zone_id, "command_blocked", now)
            return
        # Activated + dispatched → move to observing (pending outcome open).
        delta = (
            (exp.expected_final_candidate_target_ha - exp.baseline_parameter_target_ha)
            if exp.expected_final_candidate_target_ha is not None
            and exp.baseline_parameter_target_ha is not None else None
        )
        self._experiments_active[zone_id] = replace(
            exp, status=STATUS_OBSERVING, updated_at=now, delta_ha=delta,
            confirmation=("position_change_confirmed" if has_reliable_feedback
                          else "command_sent"),
        )
        self._learning_dirty = True

    def _experiment_finalize_from_outcome(self, outcome) -> None:
        """Evaluate the experiment whose decision produced this outcome."""
        did = outcome.decision_id
        if did is None:
            return
        match = None
        for zone_id, exp in self._experiments_active.items():
            if exp.experiment_decision_id == did:
                match = (zone_id, exp)
                break
        if match is None:
            return
        zone_id, exp = match

        mo = outcome.multi_objective
        exp_thermal = mo.thermal.score if (mo and mo.thermal.available) else None
        exp_pref = mo.preference.score if mo else None
        exp_move = mo.movement.score if mo else None
        open_more = bool(mo and mo.preference.override_direction == "open_more")
        reliability = (mo.reliability.thermal if mo else 0.0)

        # Robust baseline: comparable non-experiment thermal scores for this window.
        baseline_scores: list[float] = []
        baseline_days: set = set()
        for o in self._learning_store.get_outcomes():
            if o.window_id != exp.window_id or o.decision_id == did:
                continue
            omo = o.multi_objective
            if omo is None or not omo.thermal.available or omo.thermal.score is None:
                continue
            baseline_scores.append(omo.thermal.score)
            baseline_days.add(o.decision_timestamp.date())

        evaluation = evaluate_experiment(ExperimentEvaluationInput(
            experiment_outcome_available=exp_thermal is not None,
            experiment_thermal_score=exp_thermal,
            experiment_preference_score=exp_pref,
            experiment_movement_score=exp_move,
            baseline_thermal_scores=tuple(baseline_scores[-30:]),
            baseline_distinct_days=len(baseline_days),
            user_open_more_rejection=open_more,
            reliability=reliability,
        ))

        # P8 adoption snapshot from this window/context history (+ this result).
        hist = [e for e in self._experiment_history
                if e.experiment_key == exp.experiment_key]
        valid_non_degraded = sum(
            1 for e in hist
            if e.evaluation.decision in (EVAL_IMPROVED, EVAL_NO_DEGRADATION)
        ) + (1 if evaluation.decision in (EVAL_IMPROVED, EVAL_NO_DEGRADATION) else 0)
        days = {e.completed_at.date() for e in hist if e.completed_at is not None}
        if outcome.decision_timestamp is not None:
            days.add(outcome.decision_timestamp.date())
        any_rej = any(e.evaluation.decision == EVAL_PREFERENCE_REJECTED for e in hist) or open_more
        any_deg = any(e.evaluation.decision == EVAL_DEGRADED for e in hist) \
            or evaluation.decision == EVAL_DEGRADED
        min_conf = min(
            [e.evaluation.confidence for e in hist] + [evaluation.confidence]
        ) if hist else evaluation.confidence
        p8_ok = derive_p8_adoption_eligible(P8AdoptionInput(
            valid_non_degraded_experiments=valid_non_degraded, distinct_days=len(days),
            any_preference_rejection=any_rej, any_degraded=any_deg,
            min_confidence_seen=min_conf,
        ))
        evaluation = replace(evaluation, p8_adoption_eligible=p8_ok)

        if evaluation.decision in (EVAL_DEGRADED, EVAL_PREFERENCE_REJECTED):
            final_status = STATUS_REJECTED
        elif p8_ok:
            final_status = STATUS_ACCEPTED_FOR_P8
        else:
            final_status = STATUS_COMPLETED
        completed = replace(
            exp, status=final_status, completed_at=outcome.decision_timestamp or exp.updated_at,
            updated_at=outcome.decision_timestamp or exp.updated_at,
            evaluation=evaluation,
        )
        self._experiments_active.pop(zone_id, None)
        self._experiment_to_history(completed)
        self._learning_dirty = True

    def _abort_zone_experiment(self, zone_id: str, reason: str, now: datetime) -> None:
        """Logical rollback: remove the experiment authority immediately (no
        proactive counter-command).  The next regular decision uses the regular
        target.  Shadow/learned data and history are preserved."""
        exp = self._experiments_active.pop(zone_id, None)
        if exp is None:
            return
        terminal = STATUS_INTERRUPTED_PARTIAL if reason.startswith("interrupted") else STATUS_ABORTED
        self._experiment_to_history(replace(
            exp, status=terminal, abort_reason=reason, updated_at=now,
            completed_at=now, rollback_state="logical",
        ))
        self._learning_dirty = True

    def _experiment_to_history(self, exp) -> None:
        self._experiment_history.append(exp)
        # Bounded terminal history per (window,intensity,context).
        per_key: dict = {}
        for e in self._experiment_history:
            per_key.setdefault(e.experiment_key, []).append(e)
        trimmed: list = []
        for _key, items in per_key.items():
            trimmed.extend(items[-EXPERIMENT_HISTORY_PER_WINDOW:])
        # Preserve chronological order.
        trimmed.sort(key=lambda e: e.updated_at)
        self._experiment_history = trimmed

    def experiment_diagnostics(self, window_id: str) -> dict:
        """Privacy-safe per-window experiment diagnostics.  p8_adoption_eligible
        is a snapshot only; P8 must re-derive from current data."""
        window = self.windows.get(window_id)
        zone_id = window.zone_id if window is not None else ""
        exec_cfg = self.effective_zone_execution(zone_id) if zone_id else ZoneExecutionConfig()
        gate = None
        if not exec_cfg.observation_enabled:
            gate = "observation_mode_required"
        elif not exec_cfg.active_control_enabled:
            gate = "active_control_required"
        elif not exec_cfg.experiments_enabled:
            gate = "experiments_not_enabled"
        exp = self._experiments_active.get(zone_id)
        if exp is None or exp.window_id != window_id:
            latest = next(
                (e for e in reversed(self._experiment_history) if e.window_id == window_id),
                None,
            )
            return {
                "experiment_status": (latest.status if latest else "none"),
                "experiment_id": (latest.experiment_id if latest else None),
                "source_shadow_id": (latest.source_shadow_id if latest else None),
                "evaluation_class": (latest.evaluation.decision if latest else None),
                "p8_adoption_eligible": (latest.evaluation.p8_adoption_eligible if latest else False),
                "active_experiment_zone_lock": exp is not None,
                "activation_gate": gate,
                "latest_abort_reason": (latest.abort_reason if latest else None),
                "rollback_status": (latest.rollback_state if latest else "none"),
            }
        return {
            "experiment_status": exp.status, "experiment_id": exp.experiment_id,
            "source_shadow_id": exp.source_shadow_id,
            "experiment_target_ha": exp.expected_final_candidate_target_ha,
            "effective_delta_ha": exp.delta_ha,
            "active_experiment_zone_lock": True, "activation_gate": gate,
            "latest_abort_reason": exp.abort_reason, "rollback_status": exp.rollback_state,
            "outcome_status": exp.confirmation,
            "evaluation_class": exp.evaluation.decision,
            "p8_adoption_eligible": exp.evaluation.p8_adoption_eligible,
            "cooldown_remaining": None,
        }

    def _experiments_storage(self) -> list:
        out = [e.to_dict() for e in self._experiments_active.values()]
        out.extend(e.to_dict() for e in self._experiment_history[-200:])
        return out

    def window_contribution_diagnostics(self, window_id: str) -> dict:
        """Privacy-safe per-window contribution diagnostics.  Eligibility is
        derived from the CURRENT model + config generation (not a stale bool)."""
        window = self.windows.get(window_id)
        model = self._contribution_models.get(window_id)
        zone_id = window.zone_id if window is not None else ""
        current_gen = self._thermal_config_generation(zone_id) if zone_id else 0
        shadow_elig, exp_elig = derive_eligibility(model, current_gen)
        ev = self._contribution_evidence.get(window_id, [])
        latest_disq = None
        return {
            "attribution_quality": (ev[-1].attribution_quality if ev else "unknown"),
            "contribution_index": (
                model.normalized_relative_contribution_index if model else None
            ),
            "contribution_confidence": round(model.confidence, 3) if model else 0.0,
            "isolated_sample_count": model.isolated_sample_count if model else 0,
            "candidate_sample_count": model.candidate_sample_count if model else 0,
            "shared_sample_count": model.shared_sample_count if model else 0,
            "distinct_days": model.distinct_days if model else 0,
            "prior_source": model.prior_source if model else "neutral",
            "latest_disqualifier": latest_disq,
            "shadow_contribution_eligible": shadow_elig,
            "experiment_contribution_eligible": exp_elig,
        }

    def shadow_diagnostics(self, window_id: str) -> dict:
        """Privacy-safe per-window shadow diagnostics (latest active proposal).

        experiment_candidate_ready is a diagnostic SNAPSHOT only — P7 must
        re-derive eligibility from current data.
        """
        latest = None
        for key, p in self._shadow_active.items():
            if key[0] == window_id:
                if latest is None or p.updated_at > latest.updated_at:
                    latest = p
        if latest is None:
            return {"shadow_status": "none"}
        ev = latest.evaluation
        return {
            "shadow_status": latest.status,
            "shadow_candidate_target_ha": latest.shadow_final_candidate_target_ha,
            "shadow_candidate_delta_ha": latest.net_shadow_delta_vs_real_ha,
            "shadow_reason": latest.proposal_reason,
            "shadow_confidence": round(ev.confidence, 3),
            "shadow_context": latest.context_family,
            "comparable_outcome_count": ev.comparable_baseline_outcomes,
            "distinct_days": ev.distinct_days,
            "preference_veto": ev.preference_veto,
            "attribution_quality": latest.attribution_quality,
            "experiment_candidate_ready": latest.experiment_candidate_ready,
            "latest_block_reason": latest.block_reason,
        }

    def _shadow_proposals_storage(self) -> list:
        out = [p.to_dict() for p in self._shadow_active.values()]
        out.extend(p.to_dict() for p in self._shadow_history[-200:])
        return out

    def _contribution_models_storage(self) -> dict:
        return {w: m.to_dict() for w, m in self._contribution_models.items()}

    def _contribution_evidence_storage(self) -> dict:
        return {w: [e.to_dict() for e in lst] for w, lst in self._contribution_evidence.items()}

    def _thermal_models_storage(self) -> dict:
        return {z: m.to_dict() for z, m in self._thermal_models.items()}

    def _thermal_observations_storage(self) -> dict:
        return {
            z: [o.to_dict() for o in lst]
            for z, lst in self._thermal_observations.items()
        }

    def _thermal_config_generation(self, zone_id: str) -> int:
        """Monotonic generation that changes when the zone's temperature SENSOR
        configuration changes (not on unrelated per-window config)."""
        fp = compute_config_fingerprint(
            {"thermal_sensors": sorted(self._indoor_temperature_sensor_ids)}
        )
        gen, _changed = self._config_generation_tracker.observe(f"thermal::{zone_id}", fp)
        return gen

    _THERMAL_FALLBACK_REASONS = frozenset(
        {"cold_start_fallback", "low_confidence_diagnostic", "no_temperature_fallback"}
    )

    def _thermal_select_window(
        self, zone_id: str, *, outdoor: float | None, exposure: float | None,
        temperature_available: bool, now: datetime,
    ) -> tuple[int, bool, float | None]:
        """P4 active authority: choose the outcome observation window (minutes).

        Returns (window_minutes, authority_applied, model_confidence).
        Cold-start / low-confidence / no-temperature → fixed 30-min fallback
        with authority_applied=False (unchanged behavior).
        """
        model = self._thermal_models.get(zone_id)
        ctx = thermal_context_key(now, outdoor, exposure)
        window, reason = select_observation_window(
            model, ctx, temperature_available=temperature_available
        )
        authority = reason not in self._THERMAL_FALLBACK_REASONS
        confidence = model.confidence if model is not None else None
        return window, authority, confidence

    def _thermal_maturity_for(self, zone_id: str, pending, now: datetime):
        """Build the P4 ThermalMaturityInput for a TIMEOUT resolution."""
        from .engines.outcome_resolution import (
            ThermalMaturityInput, MATURITY_MATURE, MATURITY_MAXIMUM_REACHED,
            MATURITY_IMMATURE,
        )
        from .engines.thermal_response_engine import (
            detect_response_onset, MAX_OBSERVATION_MIN,
        )

        authority = getattr(pending, "thermal_authority_applied", False)
        window = getattr(pending, "indoor_temp_outcome_delay_min", _OUTCOME_OBSERVATION_DELAY_MIN)
        conf = getattr(pending, "thermal_confidence_at_decision", None)
        duration = (now - pending.decision_timestamp).total_seconds() / 60.0

        acc = self._thermal_open.get(zone_id)
        samples = tuple(acc["samples"]) if acc else ()
        onset = detect_response_onset(samples, solar_exposure=pending.solar_exposure_at_decision)
        stable = onset is not None

        if window >= MAX_OBSERVATION_MIN:
            maturity, reason = MATURITY_MAXIMUM_REACHED, "maximum_window"
        elif not authority:
            maturity, reason = MATURITY_MATURE, "fallback_window"   # legacy 30-min
        elif window < _OUTCOME_OBSERVATION_DELAY_MIN:
            if stable:
                maturity, reason = MATURITY_MATURE, "learned_window_stable_early"
            else:
                maturity, reason = MATURITY_IMMATURE, "learned_window_unstable_early"
        else:
            maturity, reason = MATURITY_MATURE, "learned_window"

        return ThermalMaturityInput(
            authority_applied=authority, selected_window_minutes=window,
            model_confidence_at_decision=conf, response_onset_detected=stable,
            response_onset_minutes=onset, stable_trend_detected=stable,
            resolution_reason=reason, maturity=maturity,
        )

    def _thermal_start_or_extend(
        self, zone_id: str, decision_id: str | None, *, now: datetime,
        indoor: float | None, outdoor: float | None, exposure: float | None,
        target_ha: int | None, shading_state: str,
    ) -> None:
        """Open a per-zone thermal observation, or attach a concurrent window's
        decision_id to the open one (zone-shared event, single observation)."""
        if decision_id is None:
            return
        reading = self._read_zone_temperature()
        gen = self._thermal_config_generation(zone_id)
        acc = self._thermal_open.get(zone_id)
        if acc is not None and acc.get("config_generation") == gen:
            acc["decision_ids"].add(decision_id)   # zone-shared — no new observation
            return
        # Start a fresh observation window for the zone.
        if reading.value is not None:
            self._thermal_prev_zone_temp[zone_id] = reading.value
        self._thermal_open[zone_id] = {
            "started_at": now,
            "indoor_start": indoor,
            "outdoor_start": outdoor,
            "solar_start": exposure,
            "target_before": target_ha,
            "shading_state": shading_state,
            "decision_ids": {decision_id},
            "samples": [(0, indoor)] if indoor is not None else [],
            "source_kind": reading.source_kind,
            "valid_sensor_count": reading.valid_count,
            "configured_sensor_count": reading.configured_count,
            "aggregation_method": reading.aggregation_method,
            "reliability_factor": source_reliability_factor(reading),
            "config_generation": gen,
            "context_key": thermal_context_key(now, outdoor, exposure),
            "window_facts": {},        # P5: per-window event facts (sticky)
            "material_windows": set(),  # P5: windows with a material change this observation
        }

    def _thermal_sample(self, zone_id: str, now: datetime) -> None:
        """Append one sparse zone-temperature sample to the open observation
        (once per zone per cycle, bounded, decimated)."""
        if self._thermal_sampled_cycle.get(zone_id) == self._cycle_counter:
            return
        self._thermal_sampled_cycle[zone_id] = self._cycle_counter
        acc = self._thermal_open.get(zone_id)
        if acc is None:
            return
        # Invalidate on sensor-config change.
        if self._thermal_config_generation(zone_id) != acc.get("config_generation"):
            self._thermal_open.pop(zone_id, None)
            return
        reading = self._read_zone_temperature()
        if reading.value is None:
            return
        self._thermal_prev_zone_temp[zone_id] = reading.value
        offset = int(round((now - acc["started_at"]).total_seconds() / 60.0))
        samples: list = acc["samples"]
        samples.append((offset, reading.value))
        if len(samples) > _THERMAL_SAMPLE_CAP:
            # Keep first, last, and a decimated middle.
            acc["samples"] = [samples[0]] + samples[-(_THERMAL_SAMPLE_CAP - 1):]

    def _thermal_mark_material_window(self, zone_id: str, window_id: str) -> None:
        """Mark a window as having a material change during the open zone obs."""
        acc = self._thermal_open.get(zone_id)
        if acc is not None:
            acc.setdefault("material_windows", set()).add(window_id)

    def _thermal_record_window_facts(
        self, zone_id: str, window_id: str, *, command_status: str,
        harmonized: bool, has_reliable_feedback: bool, active_control: bool,
        observed_position_internal: int | None, target_internal: int | None,
    ) -> None:
        """Capture/merge per-window event facts into the open zone observation
        (sticky material/external flags; latest positions/status)."""
        acc = self._thermal_open.get(zone_id)
        if acc is None:
            return
        material = window_id in acc.get("material_windows", set())
        wf: dict = acc.setdefault("window_facts", {})
        f = wf.get(window_id)
        if f is None:
            f = {
                "material": False, "command_status": "none", "harmonized": False,
                "has_reliable_feedback": has_reliable_feedback, "active_control": active_control,
                "start_position": observed_position_internal, "end_position": observed_position_internal,
                "target": target_internal, "external_movement": False,
            }
            wf[window_id] = f
        # Sticky material/harmonized; latest status/positions.
        f["material"] = f["material"] or material
        f["harmonized"] = f["harmonized"] or harmonized
        if command_status == "sent":
            f["command_status"] = "sent"
        elif f["command_status"] != "sent":
            f["command_status"] = command_status
        f["has_reliable_feedback"] = has_reliable_feedback
        f["active_control"] = active_control
        if target_internal is not None:
            f["target"] = target_internal
        # External movement: position moved materially in a cycle with no command.
        if observed_position_internal is not None and f["end_position"] is not None:
            moved = abs(observed_position_internal - f["end_position"])
            if not material and command_status != "sent" and moved >= 3:
                f["external_movement"] = True
        if observed_position_internal is not None:
            f["end_position"] = observed_position_internal

    def _thermal_finalize(self, outcome: DecisionOutcome) -> None:
        """On a resolved outcome, build a thermal observation when usable and
        recompute the zone model; otherwise drop the (confounded) zone window."""
        window = self.windows.get(outcome.window_id)
        if window is None:
            return
        zone_id = window.zone_id
        acc = self._thermal_open.get(zone_id)
        if acc is None or outcome.decision_id not in acc["decision_ids"]:
            return

        mo = outcome.multi_objective
        gen = self._thermal_config_generation(zone_id)
        interrupted = (outcome.window_id, outcome.decision_timestamp) in self._interrupted_decision_keys
        usable = (
            mo is not None and mo.thermal.available
            and acc.get("config_generation") == gen
            and not interrupted
        )
        # Dedupe: at most one observation per zone per cycle (multi-window event).
        already = self._thermal_last_obs_cycle.get(zone_id) == self._cycle_counter

        if usable and not already:
            now = outcome.evaluation_timestamp or acc["started_at"]
            # Circularity guard: an early (<30 min) learned-window observation
            # trains the model with reduced weight vs a full-length observation.
            _obs_reliability = acc["reliability_factor"]
            _dur = mo.thermal.actual_observation_duration_minutes
            if (mo.thermal.thermal_model_authority_applied and _dur is not None
                    and _dur < _OUTCOME_OBSERVATION_DELAY_MIN):
                _obs_reliability *= 0.6
            # P5: conservative window attribution (may upgrade zone_shared →
            # window_candidate / window_isolated under the strict solo gate).
            mature = mo.thermal.thermal_maturity in ("mature", "maximum_reached")
            _attr = self._classify_zone_attribution(
                zone_id, acc, thermal_available=True, thermal_mature=mature,
                thermal_reliability=mo.reliability.thermal,
                confounded=mo.confounders.thermal_confounded,
            )
            obs = ThermalResponseObservation(
                zone_id=zone_id,
                decision_ids=tuple(sorted(acc["decision_ids"])),
                started_at=acc["started_at"], ended_at=now,
                observation_duration_min=(now - acc["started_at"]).total_seconds() / 60.0,
                indoor_start=acc["indoor_start"], indoor_end=outcome.indoor_temp_outcome_c,
                indoor_samples=tuple(acc["samples"]),
                outdoor_start=acc["outdoor_start"], outdoor_end=outcome.outdoor_temp_at_decision,
                solar_start=acc["solar_start"], solar_end=acc["solar_start"],
                shading_state=acc["shading_state"],
                target_before_ha=acc["target_before"], target_after_ha=acc["target_before"],
                thermal_available=True, thermal_score=mo.thermal.score,
                thermal_direction=mo.thermal.observed_direction,
                attribution_quality=_attr.attribution_quality,
                source_kind=acc["source_kind"],
                valid_sensor_count=acc["valid_sensor_count"],
                configured_sensor_count=acc["configured_sensor_count"],
                aggregation_method=acc["aggregation_method"],
                reliability=_obs_reliability,
                context_key=acc["context_key"], confounded=False,
                config_generation=gen,
            )
            lst = self._thermal_observations.setdefault(zone_id, [])
            lst.append(obs)
            if len(lst) > _THERMAL_OBS_CAP_PER_ZONE:
                del lst[0]
            self._thermal_models[zone_id] = recompute_model(
                zone_id, lst, now, config_generation=gen,
                previous=self._thermal_models.get(zone_id),
            )
            self._thermal_last_obs_cycle[zone_id] = self._cycle_counter
            # P5: record contribution evidence + recompute contribution models.
            try:
                self._update_contribution(zone_id, acc, _attr, mo, now, gen)
            except Exception:
                _LOGGER.warning("Learning: contribution update failed for %s (non-fatal)", zone_id)
            # P6: shadow proposal (analysis only; never applied).
            try:
                self._maybe_shadow(zone_id, acc, mo, _attr, now, gen)
            except Exception:
                _LOGGER.warning("Learning: shadow update failed for %s (non-fatal)", zone_id)
            self._learning_dirty = True
        # Whether usable or not, the zone window is now closed.
        self._thermal_open.pop(zone_id, None)

    _SHADE_INTENSITY = {
        ShadingState.LIGHT_SHADE: "light",
        ShadingState.NORMAL_SHADE: "normal",
        ShadingState.STRONG_SHADE: "strong",
    }

    def _maybe_shadow(self, zone_id: str, acc: dict, mo, attr, now: datetime, gen: int) -> None:
        """P6: compute/observe a close-more shadow candidate (analysis only).

        Read-only: never writes back to WDI/TierDecision/harmonization/dispatch.
        """
        if attr.attribution_quality not in (ATTR_WINDOW_ISOLATED, "window_candidate"):
            return
        wid = attr.candidate_window_id
        if wid is None or wid not in self.windows:
            return
        try:
            state = ShadingState(acc["shading_state"])
        except ValueError:
            return
        intensity = self._SHADE_INTENSITY.get(state)
        if intensity is None:
            return
        real_applied = acc.get("target_before")
        if real_applied is None:
            return
        window = self.windows[wid]
        parts = acc["context_key"].split("|")
        context_family = f"{parts[0]}|{parts[-1]}"
        key = (wid, intensity, context_family)

        # Manual preference detection (conservative: an active learned preference
        # blocks an extra shadow step → no double application).
        _conf_level = (
            self._adaptive_profiles.get(wid).confidence_level
            if self._adaptive_profiles.get(wid) is not None else "very_low"
        )
        try:
            _mp_diag = self._target_position_adapter.get_adaptation_diagnostics(wid, _conf_level)
            mp_active = bool(_mp_diag.get("target_adaptation_active", False))
        except Exception:
            mp_active = False

        shadow_eligible = derive_eligibility(self._contribution_models.get(wid), gen)[0]
        elig = evaluate_shadow_eligibility(ShadowEligibilityInput(
            intensity_level=intensity, observation_mode=True,
            fully_automatic=getattr(window.behavior_mode, "name", "") == "FULLY_AUTOMATIC",
            safety_active=mo.confounders.safety_event,
            manual_override_active=mo.confounders.manual_override,
            lifecycle_active=state in (ShadingState.NIGHT_CLOSED,),
            presence_absence_transition=mo.confounders.presence_absence_transition,
            thermal_available=mo.thermal.available,
            thermal_mature=mo.thermal.thermal_maturity in ("mature", "maximum_reached"),
            thermal_reliability=mo.reliability.thermal,
            attribution_quality=attr.attribution_quality,
            contribution_shadow_eligible=shadow_eligible,
            config_generation_matches=True,
            p5_reference_valid=wid in self._contribution_models,
            manual_preference_active=mp_active,
            manual_preference_open_more=False,
            confounded=mo.confounders.thermal_confounded,
        ))
        if not elig.eligible:
            # Pause an existing proposal; do not invalidate on transient gates.
            existing = self._shadow_active.get(key)
            if existing is not None:
                self._shadow_active[key] = replace(
                    existing, block_reason=elig.block_reason, updated_at=now)
            return

        # Candidate reason (defensible signals only).
        reason = compute_candidate_reason(CandidateReasonInput(
            thermal_available=mo.thermal.available,
            thermal_mature=mo.thermal.thermal_maturity in ("mature", "maximum_reached"),
            shade_state_active=True,
            insufficient_response=mo.thermal.insufficient_response,
            thermal_score=mo.thermal.score,
            sufficient_solar_load=(acc.get("solar_start") or 0.0) >= 150.0,
            shade_was_timely_active=True,
            contribution_present=True,
            close_more_preference=False,
        ))
        if reason is None:
            return

        # Pure dry-run through the REAL clamp functions.
        cg = self.cover_groups.get(window.cover_group_id)
        hw = default_hardware_settings(cg.hardware_type) if cg is not None else {}
        cand = compute_shadow_candidate(
            current_authoritative_target_ha=real_applied,
            real_applied_target_ha=real_applied,
            configured_base_target_ha=real_applied,
            new_state=state,
            daytime_min_ha=hw.get("daytime_min_open_position_ha"),
            ahb_position_ha=hw.get("anti_heat_buildup_position_ha"),
            ahb_enabled=False,
            hardware_type=(cg.hardware_type if cg is not None else CoverHardwareType.GENERIC),
            in_solar_sector=True,
            effective_exposure_wm2=acc.get("solar_start"),
        )
        if not cand.valid:
            return

        # Dedup: update existing proposal for the key, else create.
        existing = self._shadow_active.get(key)
        negative = mo.thermal.score is not None and mo.thermal.score < 0
        prev_eval = existing.evaluation if existing is not None else ShadowEvaluation()
        neg_count = prev_eval.negative_baseline_outcomes + (1 if negative else 0)
        comparable = prev_eval.comparable_baseline_outcomes + 1
        # distinct-day tracking (RAM side index; count persists on the proposal).
        day_key = (key, now.date())
        days = prev_eval.distinct_days
        if not hasattr(self, "_shadow_day_seen"):
            self._shadow_day_seen = set()
        if day_key not in self._shadow_day_seen:
            self._shadow_day_seen.add(day_key)
            days += 1
        confidence = min(1.0, neg_count / 8.0) * min(1.0, days / 3.0)
        evaluation = ShadowEvaluation(
            comparable_baseline_outcomes=comparable,
            negative_baseline_outcomes=neg_count,
            neutral_baseline_outcomes=prev_eval.neutral_baseline_outcomes + (0 if negative else 1),
            contradictory_outcomes=prev_eval.contradictory_outcomes,
            distinct_days=days, context_consistency=1.0,
            candidate_direction_consistency=1.0, preference_support=False,
            preference_veto=False, confidence=confidence,
        )
        status = evaluate_supported_status(
            evaluation, attribution_quality=attr.attribution_quality, preference_veto=False)
        evaluation = replace(evaluation, status=status)

        contrib = self._contribution_models.get(wid)
        proposal = ShadowProposal(
            shadow_id=(existing.shadow_id if existing is not None else uuid.uuid4().hex),
            window_id=wid, zone_id=zone_id, intensity_level=intensity,
            context_family=context_family,
            created_at=(existing.created_at if existing is not None else now), updated_at=now,
            configured_intensity_target_ha=real_applied,
            current_authoritative_intensity_target_ha=real_applied,
            shadow_parameter_target_ha=cand.shadow_parameter_target_ha,
            real_applied_target_ha=real_applied,
            shadow_final_candidate_target_ha=cand.shadow_final_candidate_target_ha,
            net_shadow_delta_vs_real_ha=cand.net_delta_vs_real_ha,
            proposal_reason=reason,
            evidence_sources=("thermal", "contribution"),
            source_decision_ids=tuple(sorted(acc["decision_ids"]))[:10],
            attribution_quality=attr.attribution_quality,
            contribution_index=(contrib.normalized_relative_contribution_index if contrib else None),
            contribution_confidence=(contrib.confidence if contrib else None),
            config_generation=gen, status=status, evaluation=evaluation,
        )
        self._shadow_active[key] = proposal

    def _classify_zone_attribution(
        self, zone_id: str, acc: dict, *, thermal_available: bool,
        thermal_mature: bool, thermal_reliability: float, confounded: bool,
    ):
        """Build WindowEventFacts from the accumulator and run the solo gate."""
        facts = []
        for wid, f in acc.get("window_facts", {}).items():
            facts.append(WindowEventFacts(
                window_id=wid, material_change=f["material"],
                command_status=f["command_status"],
                has_reliable_feedback=f["has_reliable_feedback"],
                active_control=f["active_control"],
                start_position_internal=f["start_position"],
                end_position_internal=f["end_position"],
                target_internal=f["target"], harmonized=f["harmonized"],
                external_movement_detected=f["external_movement"],
            ))
        return classify_window_attribution(
            facts, thermal_available=thermal_available, thermal_mature=thermal_mature,
            thermal_reliability=thermal_reliability, confounded=confounded,
        )

    def _update_contribution(self, zone_id, acc, attr, mo, now, gen) -> None:
        """Append per-window evidence and recompute the zone's contribution models."""
        signal = mo.thermal.score
        # Evidence: one record per contributing window (shared keeps all).
        for wid in attr.contributing_window_ids:
            f = acc.get("window_facts", {}).get(wid, {})
            ev = WindowContributionEvidence(
                window_id=wid, zone_id=zone_id, decision_id=None,
                observation_decision_ids=tuple(sorted(acc["decision_ids"])),
                timestamp=now, attribution_quality=attr.attribution_quality,
                event_weight=event_weight_for(attr.attribution_quality),
                observation_reliability=mo.reliability.thermal,
                observed_contribution_signal=signal,
                effective_exposure=acc.get("solar_start"),
                blocked_or_no_exposure=(acc.get("solar_start") or 0.0) <= 0.0,
                context_key=acc["context_key"], config_generation=gen,
            )
            lst = self._contribution_evidence.setdefault(wid, [])
            lst.append(ev)
            if len(lst) > 200:
                del lst[0]
        # Recompute models over all eligible windows of the zone.
        eligible = [
            w.id for w in self.windows.values()
            if w.zone_id == zone_id
            and getattr(w.behavior_mode, "name", "") == "FULLY_AUTOMATIC"
        ]
        if not eligible:
            return
        priors = compute_geometric_solar_prior([
            WindowPriorFacts(
                window_id=w.id,
                effective_exposure=(acc.get("solar_start") if w.id in acc.get("window_facts", {}) else None),
                sector_factor=1.0,
                area_m2=getattr(self.windows[w.id], "area_m2", None),
                blocked=False,
            )
            for w in self.windows.values() if w.id in eligible
        ])
        self._contribution_models.update(recompute_contribution_models(
            zone_id, eligible, self._contribution_evidence, priors, now,
            config_generation=gen, previous=self._contribution_models,
        ))

    def _config_fingerprint_for_window(
        self, window: WindowConfig, zone: ZoneConfig
    ) -> tuple[str, int]:
        """Compute (fingerprint, generation) for a window's learning-relevant config."""
        eb = self._comfort_config
        fields = {
            "behavior_mode": getattr(window.behavior_mode, "value", str(window.behavior_mode)),
            "azimuth": getattr(window, "azimuth", None),
            "cover_group_id": getattr(window, "cover_group_id", None),
            "zone_id": getattr(window, "zone_id", None),
            "shading_group_id": getattr(window, "shading_group_id", None),
            "indoor_sensor_ids": sorted(self._indoor_temperature_sensor_ids),
            "heat_outdoor_c": getattr(eb, "heat_outdoor_threshold_c", None),
            "heat_indoor_c": getattr(eb, "heat_indoor_threshold_c", None),
        }
        fp = compute_config_fingerprint(fields)
        gen, _changed = self._config_generation_tracker.observe(window.id, fp)
        return fp, gen

    def _store_outcome(self, outcome: DecisionOutcome) -> None:
        """Persist a resolved outcome (legacy ring) AND link it to its decision
        record (P2).

        v2 outcomes are linked EXCLUSIVELY by decision_id (authoritative).  An
        unknown v2 decision_id leaves the outcome unlinked — it NEVER silently
        falls back to timestamp matching, which could hit the wrong record.
        Timestamp matching is reserved for legacy v1 outcomes (decision_id is
        None) via an explicitly isolated path.  The merged get_outcomes() view
        deduplicates so the legacy stream is never duplicated.
        """
        self._learning_store.record_outcome(outcome)
        try:
            status = outcome.resolution_status
            key = (outcome.window_id, outcome.decision_timestamp)
            if key in self._interrupted_decision_keys:
                # Observation was interrupted by a restart — never claim complete.
                status = "interrupted_partial"
                self._interrupted_decision_keys.discard(key)

            if outcome.decision_id is not None:
                # Authoritative v2 path — by decision_id only.
                linked = self._learning_store.attach_outcome_by_decision_id(
                    outcome.window_id, outcome.decision_id, outcome, status
                )
                if not linked:
                    _LOGGER.warning(
                        "Learning: outcome with unknown v2 decision_id %s for %s — "
                        "left unlinked (no timestamp fallback)",
                        outcome.decision_id, outcome.window_id,
                    )
            else:
                # Legacy v1 outcome (no decision_id) — isolated timestamp fallback.
                self._learning_store.attach_outcome_by_timestamp_legacy(outcome, status)
        except Exception:
            pass  # provenance link is best-effort; never blocks learning
        # P4: feed the resolved outcome into the per-zone thermal response model.
        try:
            self._thermal_finalize(outcome)
        except Exception:
            pass  # thermal learning is best-effort; never blocks the cycle
        # P7: if this outcome belongs to an active experiment (exact decision_id
        # linkage), finalize and evaluate the experiment.
        try:
            self._experiment_finalize_from_outcome(outcome)
        except Exception:
            _LOGGER.warning("Learning: experiment finalize failed (non-fatal)")

    def _restore_pending_outcomes(
        self, pendings: list, now: datetime
    ) -> None:
        """Apply the P2.6 restart interruption gate to restored pending outcomes.

        A pending is INVALIDATED (dropped, no synthetic outcome) when the total
        elapsed time exceeds the observation window + grace, the config
        fingerprint changed, or it has already survived more than one restart.
        Surviving pendings are restored and flagged interrupted so their later
        outcome can never be scored as 'complete'.
        """
        grace = timedelta(minutes=5)
        delay = timedelta(minutes=_OUTCOME_OBSERVATION_DELAY_MIN)
        for po in pendings:
            if po.window_id not in self.windows:
                continue
            window = self.windows[po.window_id]
            zone = self.zones.get(window.zone_id, ZoneConfig(id=window.zone_id, name=window.zone_id))
            try:
                current_fp, _gen = self._config_fingerprint_for_window(window, zone)
            except Exception:
                current_fp = None
            elapsed = now - po.decision_timestamp
            restart_count = po.restart_count + 1
            fingerprint_changed = (
                po.config_fingerprint is not None
                and current_fp is not None
                and po.config_fingerprint != current_fp
            )
            if elapsed > (delay + grace) or fingerprint_changed or restart_count > 1:
                # Invalidate the associated record (if any); never fabricate an outcome.
                try:
                    rec = self._learning_store.get_decision_by_timestamp(
                        po.window_id, po.decision_timestamp
                    )
                    if rec is not None:
                        self._learning_store.mark_decision_invalidated(
                            po.window_id, rec.decision_id, "observation_interrupted_too_long"
                        )
                except Exception:
                    pass
                continue
            # Survive as an interrupted observation.
            from dataclasses import replace as _replace
            restored = _replace(
                po,
                restart_count=restart_count,
                created_at_utc=po.created_at_utc or po.decision_timestamp,
            )
            self._pending_outcomes.restore(restored)
            self._interrupted_decision_keys.add((po.window_id, po.decision_timestamp))

    def _maybe_record_provenance(
        self,
        window_id: str,
        s: "_WindowComputeState",
        harmonized_target_ha: int | None,
        harmonized: bool,
        dispatch: DispatchProvenance,
        now: datetime,
    ) -> None:
        """Build and (if material) persist a LearningDecisionRecord.

        Purely additive: never influences the decision, dispatch, or timing.
        All positions are logical HA convention (0=closed, 100=open).
        """
        if not s.obs_enabled or s.baseline_state is None:
            return

        def _ha(internal: int | None) -> int | None:
            return to_ha_position(internal) if internal is not None else None

        baseline_ha = _ha(s.baseline_target_internal)
        final_internal = s.exec_target_internal
        learning_ha = _ha(final_internal)
        cf_ha = s.exec_filter_result.target_position_ha if s.exec_filter_result is not None else None
        final_requested = harmonized_target_ha if harmonized else cf_ha

        # --- Adaptation steps (ordered) ---
        steps: list[AdaptationStep] = []
        sources: list[str] = []
        tr = s.adapt_trace
        if tr is not None and getattr(tr, "heat_outdoor_factor", None) is not None:
            steps.append(AdaptationStep(
                source=SOURCE_ADAPTIVE_HEAT, applied=True,
                input_thresholds={"outdoor": tr.heat_outdoor_original, "indoor": tr.heat_indoor_original},
                output_thresholds={"outdoor": tr.heat_outdoor_adapted, "indoor": tr.heat_indoor_adapted},
                strength=s.adapt_strength,
            ))
            sources.append(SOURCE_ADAPTIVE_HEAT)
        if tr is not None and getattr(tr, "solar_escalation_factor_applied", None) is not None:
            steps.append(AdaptationStep(
                source=SOURCE_ADAPTIVE_SOLAR, applied=True,
                input_thresholds={"light": tr.light_shade_threshold_original,
                                  "normal": tr.normal_shade_threshold_original,
                                  "strong": tr.strong_shade_threshold_original},
                output_thresholds={"light": tr.light_shade_threshold_adapted,
                                   "normal": tr.normal_shade_threshold_adapted,
                                   "strong": tr.strong_shade_threshold_adapted},
                strength=s.adapt_strength,
            ))
            sources.append(SOURCE_ADAPTIVE_SOLAR)
        fm = s.forecast_modifier
        fm_delta = None
        fm_trust = None
        if fm is not None and getattr(fm, "applied", False):
            fm_delta = getattr(fm, "threshold_delta_wm2", None)
            fm_trust = getattr(fm, "trust_score", None)
            steps.append(AdaptationStep(
                source=SOURCE_FORECAST_MODIFIER, applied=True,
                input_thresholds={"delta_wm2": fm_delta}, confidence=fm_trust,
            ))
            sources.append(SOURCE_FORECAST_MODIFIER)
        mp_applied = bool(s.any_pos_adapted)
        mp_target_ha = s.normal_eff_ha_for_prov if mp_applied else None
        mp_delta_ha = (
            (s.normal_eff_ha_for_prov - s.normal_cfg_ha_for_prov)
            if (mp_applied and s.normal_eff_ha_for_prov is not None and s.normal_cfg_ha_for_prov is not None)
            else None
        )
        if mp_applied:
            steps.append(AdaptationStep(
                source=SOURCE_MANUAL_PREFERENCE, applied=True,
                input_target_ha=s.normal_cfg_ha_for_prov, output_target_ha=s.normal_eff_ha_for_prov,
                confidence=s.adapt_confidence_level,  # type: ignore[arg-type]
            ))
            sources.append(SOURCE_MANUAL_PREFERENCE)

        net_delta = (
            (final_requested - baseline_ha)
            if (final_requested is not None and baseline_ha is not None) else None
        )

        # --- Materiality dedup ---
        candidate = DecisionCandidate(
            shading_state=s.new_state.value,
            baseline_target_ha=baseline_ha,
            final_target_ha=final_requested,
            adaptation_sources=frozenset(sources),
            dispatch_attempted=dispatch.dispatch_attempted,
            dispatch_status=dispatch.dispatch_status,
            filter_reason=dispatch.dispatch_filter_reason,
            suppression_reason=None,
        )
        prev_summary = self._last_decision_summaries.get(window_id)
        if not is_material_learning_decision(prev_summary, candidate):  # type: ignore[arg-type]
            return

        # --- Build provenance ---
        # Reuse the cycle's decision_id (shared with any PendingOutcome) so the
        # outcome is later attached by decision_id, not by timestamp.
        decision_id = s.decision_id or uuid.uuid4().hex
        safety_active = s.new_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE)
        is_night = s.new_state is ShadingState.NIGHT_CLOSED
        is_absence = s.new_state is ShadingState.ABSENCE_CLOSED
        shade_states = (ShadingState.LIGHT_SHADE, ShadingState.NORMAL_SHADE, ShadingState.STRONG_SHADE)
        shading_eligible = (
            window_id in self.windows
            and getattr(self.windows[window_id].behavior_mode, "name", "") == "FULLY_AUTOMATIC"
            and s.obs_enabled
        )
        indoor_available = bool(self._indoor_temperature_sensor_ids)
        thermal_ok = (
            shading_eligible and indoor_available and s.new_state in shade_states
            and not (safety_active or s.manual_override_active_at_decision or is_night or s.absence_active_at_decision)
        )
        eligibility = ModelEligibility(
            thermal=thermal_ok,
            preference=s.new_state in shade_states,
            movement=True,
            forecast=s.forecast_modifier is not None and getattr(s.forecast_modifier, "applied", False),
            shadow=thermal_ok,
            experiment=False,
        )
        clamps: list[str] = []
        if s.daytime_min_open_applied:
            clamps.append("daytime_min_open")
        if s.anti_heat_buildup_applied:
            clamps.append("anti_heat_buildup")
        if harmonized:
            clamps.append("harmonization")

        provenance = DecisionProvenance(
            decision_id=decision_id,
            context=DecisionContext(
                window_id=window_id,
                zone_id=s.window.zone_id,
                decision_timestamp=now,
                cycle_id=self._cycle_counter,
                config_fingerprint=s.config_fingerprint,
                config_generation=s.config_generation,
                behavior_mode_at_decision=getattr(s.window.behavior_mode, "value", str(s.window.behavior_mode)),
                observation_mode=s.obs_enabled,
                active_control=s.active_control_enabled,
                shading_learning_eligible=shading_eligible,
                model_eligibility=eligibility,
                lifecycle_state=s.lifecycle_state_value,
                presence_absence="absent" if s.absence_active_at_decision else "present",
                manual_override_active=s.manual_override_active_at_decision,
                safety_active=safety_active,
            ),
            baseline=BaselineDecision(
                baseline_state=s.baseline_state.value,
                baseline_requested_target_ha=baseline_ha,
                baseline_decided_by=s.baseline_decided_by or "unknown",
            ),
            adaptation=AdaptationDecision(
                steps=tuple(steps),
                adaptation_sources=tuple(sources),
                net_target_delta_ha=net_delta,
                adaptation_strength=s.adapt_strength,
                confidence_level_at_decision=s.adapt_confidence_level,
                manual_preference_available=mp_applied,
                manual_preference_applied=mp_applied,
                manual_preference_target_ha=mp_target_ha,
                manual_preference_delta_ha=mp_delta_ha,
                manual_preference_profile_key="normal" if mp_applied else None,
                manual_preference_confidence=s.adapt_confidence_level if mp_applied else None,
                forecast_modifier_delta_wm2=fm_delta,
                forecast_trust_score=fm_trust,
            ),
            resolved=ResolvedDecision(
                final_state=s.new_state.value,
                decided_by=s.tier_decided_by or "unknown",
                target_after_learning_ha=learning_ha,
                target_after_tier_resolution_ha=learning_ha,
                target_after_command_filter_ha=cf_ha,
                target_after_daytime_min_ha=cf_ha if s.daytime_min_open_applied else None,
                target_after_anti_heat_buildup_ha=cf_ha if s.anti_heat_buildup_applied else None,
                target_after_harmonization_ha=harmonized_target_ha if harmonized else None,
                final_requested_target_ha=final_requested,
                applied_clamps=tuple(clamps),
                suppression_reason=None,
            ),
            dispatch=dispatch,
        )
        record = LearningDecisionRecord(
            decision_id=decision_id,
            decision_timestamp=now,
            cycle_id=self._cycle_counter,
            window_id=window_id,
            provenance=provenance,
            outcome=None,
            outcome_status="none",
            provenance_available=True,
            legacy_record=False,
        )
        self._learning_store.record_decision(record)
        self._learning_store.set_pending_decision(window_id, decision_id)
        self._last_decision_summaries[window_id] = candidate.to_summary()
        self._learning_dirty = True
