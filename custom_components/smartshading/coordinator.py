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
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engines.forecast_persistence import ForecastPersistenceAdapter
    from .models.forecast_store import ForecastLearningStore

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .capability_detector import CapabilityDetector
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN
from .cover_control.assumed_state_manager import AssumedStateManager, confidence_level
from .cover_control.cover_capabilities import CoverCapability
from .cover_control.cover_controller import CoverController
from .engines.comfort_engine import ComfortEngine
from .engines.exposure_engine import ExposureEngine
from .engines.solar_source import SOURCE_MEASURED, classify_solar_source
from .engines.learning_persistence import (
    LearningPersistenceAdapter,
    LearningPersistenceConfig,
    PAYLOAD_SCHEMA_V2,
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
from .engines.contact_engine import (
    ContactStatus as _ContactStatus,
    build_contact_reading as _build_contact_reading,
)
from .engines.night_contact_hold import (
    NightContactAction as _NightContactAction,
    NightContactHold as _NightContactHold,
)
from .engines.pending_outcome_queue import PendingOutcomeQueue
from .models.pending_outcome import PendingOutcome
from .engines.lifecycle_engine import LifecycleEngine, PresenceDebouncer, check_night_interval_active
from .engines.lifecycle_guard import lifecycle_should_break_override, should_allow_lifecycle_release
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
    CausalSameCycleInput,
    P8AdoptionInput,
    derive_p8_adoption_eligible,
    evaluate_experiment_causal,
    is_cooldown_active,
    reconcile_restored_experiments,
    revalidate_experiment_candidate,
)
from .engines.staged_experiment import (
    enforce_monotonic_spacing,
    evaluate_stage_escalation,
)
from .models.bounded_experiment import (
    ACTIVE_STATUSES as _EXP_ACTIVE_STATUSES,
    EVAL_DEGRADED,
    EVAL_IMPROVED,
    EVAL_NO_DEGRADATION,
    EVAL_PREFERENCE_REJECTED,
    EXPERIMENT_HISTORY_PER_WINDOW,
    EXPERIMENT_MATERIALITY_HA,
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
from .engines.adoption_eligibility import (
    AdoptionEligibilityInput,
    evaluate_adoption_eligibility,
)
from .engines.adoption_engine import (
    ExperimentEvidence,
    ExperimentNeedInput,
    MonitoringActionInput,
    classify_monitoring_outcome,
    evaluate_adoption_evidence,
    evaluate_confirmation,
    evaluate_experiment_need,
    evaluate_monitoring_action,
    is_cooldown_active as _adoption_cooldown_active,
    reconcile_restored_adoptions,
    rollback_cooldown_until,
    update_monitoring,
)
from .models.persistent_adoption import (
    ACTION_FULL_ROLLBACK,
    ACTION_INVALIDATE,
    ACTION_REDUCE_ONE_STEP,
    ACTION_TEMPORARY_SUSPEND,
    ADOPTION_HISTORY_PER_WINDOW,
    ADOPTION_MATERIALITY_HA,
    ADOPTION_STEP_HA,
    S2_STABILITY_DAYS,
    STATUS_ADOPTED as ADOPT_STATUS_ADOPTED,
    STATUS_CONFIRMED as ADOPT_STATUS_CONFIRMED,
    STATUS_INVALIDATED as ADOPT_STATUS_INVALIDATED,
    STATUS_MONITORING as ADOPT_STATUS_MONITORING,
    STATUS_REDUCED as ADOPT_STATUS_REDUCED,
    STATUS_ROLLED_BACK as ADOPT_STATUS_ROLLED_BACK,
    AdoptionMonitoringState,
    PersistentTargetAdoption,
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
    compute_forecast_strategy_modifier,
)
from .engines.solar_threshold_resolver import resolve_solar_thresholds
from .engines.tier_order_resolver import project_tier_order
from .engines.strategy_resolver import StrategyResolverInput, resolve_strategy
from .engines.thermal_insufficiency import (
    InsufficiencyInput,
    classify_thermal_insufficiency,
)
from .models.shading_strategy import ForecastLoadFeatures
from .engines.strategy_experiment_engine import (
    StrategyEvidence,
    StrategyMonitoringActionInput,
    classify_strategy_outcome,
    evaluate_strategy_confirmation,
    evaluate_strategy_evidence,
    evaluate_strategy_monitoring_action,
    is_cooldown_active as _strategy_cooldown_active,
    reconcile_restored_strategy_adoptions,
    reconcile_restored_strategy_experiments,
    rollback_cooldown_until as _strategy_rollback_cooldown_until,
    update_strategy_monitoring,
)
from .models.strategy_learning import (
    ACTION_FULL_ROLLBACK as STRAT_ACTION_FULL_ROLLBACK,
    ACTION_INVALIDATE as STRAT_ACTION_INVALIDATE,
    ACTION_REDUCE_ONE_STEP as STRAT_ACTION_REDUCE_ONE_STEP,
    ACTION_TEMPORARY_SUSPEND as STRAT_ACTION_TEMPORARY_SUSPEND,
    AD_CONFIRMED as STRAT_AD_CONFIRMED,
    AD_MONITORING as STRAT_AD_MONITORING,
    AD_REDUCED as STRAT_AD_REDUCED,
    AD_ROLLED_BACK as STRAT_AD_ROLLED_BACK,
    AD_INVALIDATED as STRAT_AD_INVALIDATED,
    EXP_ABORTED as STRAT_EXP_ABORTED,
    ADOPTION_HISTORY_PER_KEY as STRAT_ADOPTION_HISTORY_PER_KEY,
    FAMILY_BOUNDS as STRAT_FAMILY_BOUNDS,
    FAMILY_ENTRY_THRESHOLD,
    FAMILY_EXIT_THRESHOLD,
    FAMILY_ENTRY_TIMING,
    FAMILY_EXIT_TIMING,
    FAMILY_TIER_CHOICE,
    FAMILY_MINIMUM_HOLD,
    FAMILY_HYSTERESIS,
    StrategyMonitoringState,
    PersistentStrategyAdoption,
    BoundedStrategyExperiment,
)
from .models.consumed_ledger import (
    TYPE_POSITION as _LEDGER_POSITION,
    TYPE_STRATEGY as _LEDGER_STRATEGY,
    ConsumedExperimentLedger,
    LedgerIntegrity,
)
from .engines.reference_validator import validate_adoptions as _validate_adoptions
from .models.shadow_tombstone import (
    KIND_POSITION as _TOMB_POSITION,
    KIND_STRATEGY as _TOMB_STRATEGY,
    TOMBSTONE_AGE_CAP_DAYS as _TOMB_AGE_DAYS,
    ShadowTombstone,
)
from .engines.config_invalidation import (
    classify_config_change as _classify_config_change,
    CHANGE_BEHAVIOR_MODE_AWAY as _CI_CHANGE_MODE_AWAY,
    CHANGE_COVER_REPLACEMENT as _CI_CHANGE_COVER,
    CHANGE_ORIENTATION as _CI_CHANGE_ORIENTATION,
    CHANGE_FEEDBACK_CAPABILITY_LOSS as _CI_CHANGE_FEEDBACK_LOSS,
    ACTION_SUSPEND as _CI_SUSPEND,
    ACTION_INVALIDATE as _CI_INVALIDATE,
    SCOPE_POSITION as _CI_SCOPE_POSITION,
    SCOPE_STRATEGY as _CI_SCOPE_STRATEGY,
)
from .engines.config_diff import (
    diff_config_snapshots as _diff_config_snapshots,
    CHANGE_WINDOW_REMOVAL as _CI_CHANGE_WINDOW_REMOVAL,
)
from .engines.strategy_runtime import (
    TimingState,
    apply_deescalation_hysteresis,
    apply_entry_timing,
    apply_exit_timing,
    apply_tier_choice,
    effective_exit_threshold,
    effective_min_hold_minutes,
)
from .engines.safety_hold import (
    HARDWARE_RAIN_SAFE_POSITIONS as _HARDWARE_RAIN_SAFE_POSITIONS,
    HARDWARE_SAFE_POSITIONS as _HARDWARE_SAFE_POSITIONS,
    RAIN_HOLD_S as _RAIN_HOLD_S,
    SafetyHold as _SafetyHold,
    WIND_HOLD_S as _WIND_HOLD_S,
    STORM_HOLD_S as _STORM_HOLD_S,
)
from .engines.rain_engine import (
    RainSourceType as _RainSourceType,
    RainStatus as _RainStatus,
    build_rain_sensor_reading as _build_rain_sensor_reading,
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
from .cover_control.position_semantics import (
    clamp_position,
    to_ha_position,
    to_internal_position,
)
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
from .const import DATA_DEBUG_LOGGING, DEFAULT_WINDOW_OPEN_NIGHT_POSITION_HA as _DEFAULT_VENT_POS_HA, DOMAIN

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

# P10 completion: important learning events trigger a coalesced near-immediate
# save after this delay (multiple events within the window collapse into one).
IMPORTANT_SAVE_DELAY_SECONDS: int = 3

# P11: bounded, EPHEMERAL (never persisted) dispatch-trace ring per zone, used only
# for read-only diagnostics/support export.  Not part of any control authority.
DISPATCH_TRACE_MAX_RECORDS_PER_ZONE: int = 500
# P11 Increment 2: bounded ephemeral decision-trace ring per zone + retarget window.
DECISION_TRACE_MAX_RECORDS_PER_ZONE: int = 200
RETARGET_TRACE_WINDOW_SECONDS: int = 300


@dataclass(frozen=True)
class _WeatherInputs:
    """One cycle's worth of optional weather/solar readings (2026-06-16
    weather-input round) - shared across all windows, since there is one
    weather source for the whole house."""

    outdoor_temperature: float | None
    solar_radiation: float | None
    solar_radiation_age_s: float | None             # seconds since the solar sensor last updated
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
    # Night contact hold diagnostics (v1.1.0).
    contact_sensor_configured: bool = False
    contact_status_value: str | None = None
    contact_is_stale: bool = False
    night_contact_blocked: bool = False
    night_contact_catch_up_pending: bool = False
    night_contact_catch_up_done: bool = False
    night_vent_active: bool = False
    night_contact_state_label: str | None = None
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
        rain_sensor_id: str | None = None,
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
        self._rain_sensor_id = rain_sensor_id
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
        # P10: dirty-generation counter (not a single bool) so a save can only
        # clear exactly the generation it snapshotted — mutations during an active
        # save remain dirty for the next save.  _learning_dirty is a read-only
        # property derived from these.
        self._dirty_generation: int = 0
        self._saved_generation: int = 0
        self._save_failures: int = 0
        self._restore_failures: int = 0
        # Optional thermal-finalization failures (best-effort learning step).
        # Counter + privacy-safe reason (exception class name only — never the
        # message/traceback) so a silent finalize failure is observable in
        # diagnostics without leaking internals or crashing the cycle.
        self._thermal_finalize_failures: int = 0
        self._thermal_finalize_last_reason: str | None = None
        # P10: structured, privacy-safe per-section restore reason counters.
        self._restore_diagnostics: dict = {}
        self._save_lock = asyncio.Lock()
        # P10 completion: coalescing near-immediate save scheduler handle.
        self._pending_save_unsub = None
        # P10: once unloading, no NEW important-save callbacks are scheduled.
        self._unloading: bool = False
        # P11: ephemeral (RAM-only, never persisted) dispatch-trace ring per zone +
        # small incremental per-cover state for retarget diagnostics.  Read-only.
        self._dispatch_trace: dict[str, deque] = {}
        self._cover_dispatch_state: dict[str, dict] = {}
        # P11 Increment 2: ephemeral (RAM-only) decision-trace ring per zone.
        self._decision_trace: dict[str, deque] = {}
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
        # P10: compact, restart-safe shadow provenance tombstones (no full time
        # series) keyed by (kind, shadow_id); bounded by age + count, referenced
        # ones protected from pruning.  Restore never creates runtime authority.
        self._shadow_tombstones: dict[tuple, ShadowTombstone] = {}
        # P7 — bounded experiments.  At most ONE active experiment per zone.
        self._experiments_active: dict[str, BoundedExperiment] = {}   # zone_id → experiment
        self._experiment_history: list[BoundedExperiment] = []
        self._experiment_zone_last_activation: dict[str, datetime] = {}
        # 3H — last staged-experiment block per window (diagnostics/export only;
        # not adaptive authority).  Set when a candidate is blocked, e.g. the
        # monotonic spacing is insufficient and the slot is handed to strategy.
        self._experiment_stage_block: dict[str, dict] = {}
        # Per-cycle injection context (window_id → dict), reset each cycle.
        self._cycle_experiment: dict[str, dict] = {}
        # P8 — persistent target adoptions.  At most ONE active adoption per
        # (window_id, intensity_level); bounded terminal history.
        self._adoptions_active: dict[tuple, PersistentTargetAdoption] = {}
        self._adoption_history: list[PersistentTargetAdoption] = []
        # Per-cycle record of which adoptions were applied (window → {intensity:
        # (adoption_id, control_applied)}), used by monitoring + provenance.
        self._cycle_adoption_applied: dict[str, dict] = {}
        # P9A — per-cycle solar-threshold resolution, tier-order projection and
        # strategy candidate, for provenance + diagnostics (observe/recommend).
        self._cycle_solar_resolution: dict[str, object] = {}
        # P11.3 closure: read-only per-window solar-transformation + entry-threshold
        # provenance snapshot (already-computed values; one entry per window).
        self._cycle_solar_provenance: dict[str, dict] = {}
        # This cycle's house-wide forecast strategy modifier (read-only diagnostics).
        self._cycle_forecast_modifier: object | None = None
        self._cycle_tier_order: dict[str, object] = {}
        self._strategy_candidates: dict[str, object] = {}
        # P9A — latest thermal-insufficiency cause per window (diagnostics only).
        self._last_thermal_cause: dict[str, tuple] = {}
        # P9B — bounded strategy experiments + persistent strategy adoptions.
        # ONE experiment per zone is shared with P7 position experiments (unified
        # zone-experiment authority).  Adoptions keyed by (window, parameter_family).
        self._strategy_experiments_active: dict[str, BoundedStrategyExperiment] = {}
        self._strategy_experiment_history: list[BoundedStrategyExperiment] = []
        self._strategy_adoptions_active: dict[tuple, PersistentStrategyAdoption] = {}
        self._strategy_adoption_history: list[PersistentStrategyAdoption] = []
        # P10 Variant A: strategy learning is evidence-based (evaluate_strategy_evidence);
        # it never materialises strategy shadows, so there is no _strategy_shadows map.
        # source_shadow_ids on strategy experiments/adoptions stays OPTIONAL provenance
        # and is never required for validity (source_experiment_ids + decision/outcome
        # linkage are the hard evidence).
        # P9B live authority: per-window timing trackers + per-cycle applied set +
        # per-decision applied families (for honest monitoring credit).
        self._strategy_timing_state: dict[str, TimingState] = {}
        self._cycle_strategy_applied: dict[str, dict] = {}
        self._strategy_applied_by_decision: dict[str, set] = {}
        # P10 — permanent bounded consumed-experiment ledger (position + strategy).
        self._consumed_ledger = ConsumedExperimentLedger()
        # P10 acceptance fix: per-namespace ledger integrity (fail-closed).  An
        # unsafe namespace blocks new + suspends restored adaptive authority for
        # that namespace; consumed evidence is never released by corruption.
        self._ledger_integrity = LedgerIntegrity()
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
        self._rain_holds: dict[str, _SafetyHold] = {}
        self._night_contact_holds: dict[str, _NightContactHold] = {}

        _zone_controls_raw = config_entry.options.get("zone_controls", {})
        # Defensive: a corrupted/old options blob may store None or a non-dict
        # here (or per-zone non-dict entries).  Never crash setup on stored
        # data — fall back to the safe defaults (observation on, control off).
        if isinstance(_zone_controls_raw, dict):
            for _zone_id, _ctrl in _zone_controls_raw.items():
                if not isinstance(_ctrl, dict):
                    continue
                self._zone_execution_overrides[_zone_id] = ZoneExecutionConfig(
                    # Two-control UX: learning_enabled is the merged learning
                    # master.  Tolerate a legacy observation_enabled key.
                    learning_enabled=_ctrl.get(
                        "learning_enabled", _ctrl.get("observation_enabled", True)),
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

    @property
    def _learning_dirty(self) -> bool:
        """True when in-memory learning data has unsaved changes (P10 dirty-gen)."""
        return self._dirty_generation != self._saved_generation

    def _mark_learning_dirty(self) -> None:
        """Record an unsaved learning change (P10: monotonic dirty generation)."""
        self._dirty_generation += 1

    def _build_save_kwargs(self) -> dict:
        """THE single authoritative complete learning snapshot used by EVERY save
        path (periodic, important-event, reload/unload/shutdown flush).  No save
        path may omit a section — this prevents the P0 flush data-loss where empty
        defaults overwrote populated P3–P9B sections.

        Built synchronously (no await) so the snapshot is a consistent reference
        graph; the actual I/O happens afterwards under the save lock."""
        return {
            "target_adapter": self._target_position_adapter,
            "pending_outcomes": self._pending_outcomes.all_pending(),
            "config_generations": self._config_generation_tracker.to_storage_dict(),
            "thermal_models": self._thermal_models_storage(),
            "thermal_observations": self._thermal_observations_storage(),
            "window_contribution_models": self._contribution_models_storage(),
            "window_contribution_evidence": self._contribution_evidence_storage(),
            "shadow_proposals": self._shadow_proposals_storage(),
            "bounded_experiments": self._experiments_storage(),
            "persistent_adoptions": self._adoptions_storage(),
            "strategy_experiments": self._strategy_experiments_storage(),
            "persistent_strategy_adoptions": self._strategy_adoptions_storage(),
            "consumed_experiment_ledger": self._consumed_ledger.to_dict(),
            "shadow_tombstones": [t.to_dict() for t in self._shadow_tombstones.values()],
            # Restart-safe active manual overrides (so a manual movement is not
            # re-asserted after restart/reload).  Bounded by per-override expiry.
            "active_overrides": self._override_detector.active_overrides_snapshot(
                dt_util.utcnow()),
            "config_snapshot": self._build_config_snapshot(),
            "owner_zone_id": next(iter(self.zones.keys()), None),
        }

    def _build_config_snapshot(self) -> dict:
        """P10: normalised config snapshot (stable internal window/zone ids only,
        never display names).  Persisted with the learning store so the next
        restore can diff previous vs current config and route real changes through
        the typed invalidation matrix — not a bare config_generation bump."""
        zone_indoor = sorted(self._indoor_temperature_sensor_ids)
        zones = {
            zid: {
                "indoor": list(zone_indoor),
                "solar": self._solar_radiation_sensor_id,
                "forecast": self._weather_entity_id,
            }
            for zid in self.zones
        }
        windows = {}
        for wid, w in self.windows.items():
            windows[wid] = {
                "zone_id": getattr(w, "zone_id", ""),
                "azimuth": getattr(w, "azimuth", None),
                "sun_sector": [
                    getattr(w, "manual_sun_sector_start_deg", None),
                    getattr(w, "manual_sun_sector_end_deg", None),
                ],
                "obstruction": repr(getattr(w, "obstruction_zones", []) or []),
                "cover_group": getattr(w, "cover_group_id", None),
                "positions": [
                    getattr(w, "light_shade_position", None),
                    getattr(w, "normal_shade_position", None),
                    getattr(w, "strong_shade_position", None),
                ],
                "behavior_mode": str(getattr(w, "behavior_mode", None)),
                "feedback_capable": getattr(w, "reliable_feedback_capable", None),
            }
        return {"zones": zones, "windows": windows}

    async def _save_learning_snapshot(self, now: datetime) -> bool:
        """Serialize+persist the COMPLETE snapshot under the save lock.

        Captures the dirty generation BEFORE building the snapshot; on success
        advances saved_generation to exactly that captured value so any mutation
        that happened during the save stays dirty.  A failed save never advances
        saved_generation (dirty is preserved).  Returns True on success."""
        async with self._save_lock:
            captured_gen = self._dirty_generation
            windows = set(self.windows.keys())
            # P10: bound the tombstone collection (referenced ones protected) just
            # before snapshotting so the serialized set is always within caps.
            self._prune_shadow_tombstones(now)
            kwargs = self._build_save_kwargs()
            ok = await self._learning_persistence.async_save(
                self._learning_store, windows, now, **kwargs)
            if not ok:
                self._save_failures += 1
                _LOGGER.warning("Learning: snapshot save failed (dirty preserved)")
                return False
            self._persistence_last_save_at = now
            # Only the captured generation is confirmed saved; mutations during the
            # save (higher dirty_generation) remain dirty for the next save.
            if self._saved_generation < captured_gen:
                self._saved_generation = captured_gen
            return True

    def _retain_terminal_history(self, history: list, *, max_count: int = 200,
                                 age_days: int = 365) -> list:
        """P10 retention for terminal history lists: drop records older than
        age_days (by updated_at/created_at), keep the newest max_count.  Active
        records are NOT stored here (they live in the *_active maps) so this never
        prunes active authority."""
        now = dt_util.utcnow()
        cutoff = now - timedelta(days=age_days)
        kept = [
            h for h in history
            if (getattr(h, "updated_at", None) or getattr(h, "created_at", None) or now) >= cutoff
        ]
        return kept[-max_count:]

    def _request_important_save(self) -> None:
        """Mark dirty and schedule ONE coalesced near-immediate save.

        Multiple important events within the delay window collapse into a single
        save (a pending handle suppresses re-scheduling).  Safe no-op when the HA
        event loop helper is unavailable (e.g. headless tests)."""
        self._mark_learning_dirty()
        if self._unloading:
            return  # unloading: never schedule a new callback (final flush handles it)
        if self._pending_save_unsub is not None:
            return  # already scheduled within this window → coalesce
        try:
            self._pending_save_unsub = async_call_later(
                self.hass, IMPORTANT_SAVE_DELAY_SECONDS, self._on_important_save_due)
        except Exception:
            self._pending_save_unsub = None

    async def _on_important_save_due(self, _now=None) -> None:
        self._pending_save_unsub = None
        try:
            await self._save_learning_snapshot(dt_util.utcnow())
        except Exception:
            _LOGGER.warning("Learning: scheduled important save failed (non-fatal)")

    def _cancel_pending_save(self) -> None:
        if self._pending_save_unsub is not None:
            try:
                self._pending_save_unsub()
            except Exception:
                pass
            self._pending_save_unsub = None

    async def async_flush_learning(self) -> None:
        """Flush pending learning data immediately using the COMPLETE snapshot.

        Called by async_unload_entry / shutdown.  Cancels any pending coalesced
        save first, then writes the complete snapshot, so reload/unload/shutdown
        can never lose data or orphan a save task.  Re-flushes (bounded) when a
        mutation bumps dirty_generation mid-save.  Swallows errors so unload is
        never blocked."""
        self._unloading = True
        self._cancel_pending_save()
        for _attempt in range(3):
            try:
                await self._save_learning_snapshot(dt_util.utcnow())
            except Exception:
                _LOGGER.warning("Learning: flush failed (non-fatal)")
                break
            if not self._learning_dirty:
                break  # saved_generation == dirty_generation → nothing left

    def storage_diagnostics(self) -> dict:
        """Privacy-safe storage status from CACHED metadata only (no re-serialize,
        no raw IDs/payloads)."""
        counts = {
            "thermal_models": len(self._thermal_models),
            "contribution_models": len(self._contribution_models),
            "shadow_active": len(self._shadow_active),
            "shadow_history": len(self._shadow_history),
            "position_experiments_active": len(self._experiments_active),
            "position_experiment_history": len(self._experiment_history),
            "position_adoptions_active": len(self._adoptions_active),
            "position_adoption_history": len(self._adoption_history),
            "strategy_experiments_active": len(self._strategy_experiments_active),
            "strategy_experiment_history": len(self._strategy_experiment_history),
            "strategy_adoptions_active": len(self._strategy_adoptions_active),
            "strategy_adoption_history": len(self._strategy_adoption_history),
            "consumed_ledger_position": len(self._consumed_ledger.consumed_ids(_LEDGER_POSITION)),
            "consumed_ledger_strategy": len(self._consumed_ledger.consumed_ids(_LEDGER_STRATEGY)),
            "shadow_tombstones_position": sum(
                1 for k in self._shadow_tombstones if k[0] == _TOMB_POSITION),
            "shadow_tombstones_strategy": sum(
                1 for k in self._shadow_tombstones if k[0] == _TOMB_STRATEGY),
        }
        return {
            "learning_store_schema_version": PAYLOAD_SCHEMA_V2,
            "learning_store_record_counts": counts,
            "learning_store_dirty": self._learning_dirty,
            "learning_store_dirty_generation": self._dirty_generation,
            "learning_store_saved_generation": self._saved_generation,
            "learning_store_last_save_at": (
                self._persistence_last_save_at.isoformat()
                if self._persistence_last_save_at is not None else None),
            "learning_store_save_failures": self._save_failures,
            "learning_store_restore_failures": self._restore_failures,
            "learning_thermal_finalize_failures": self._thermal_finalize_failures,
            "learning_thermal_finalize_last_reason": self._thermal_finalize_last_reason,
            "learning_restore": self._restore_diagnostics,
        }

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

    async def async_set_zone_learning_enabled(
        self, zone_id: str, enabled: bool
    ) -> None:
        """Toggle Learning Mode (learning_enabled) for a zone; persist + refresh.

        When turned OFF, any active experiment for the zone is logically aborted
        (authority removed immediately; no proactive inverse command).  Stored
        learned models, shadow proposals and experiment history are preserved.
        Deterministic control may continue if active control stays enabled.
        """
        current = self.effective_zone_execution(zone_id)
        self._zone_execution_overrides[zone_id] = ZoneExecutionConfig(
            learning_enabled=enabled,
            active_control_enabled=current.active_control_enabled,
        )
        if not enabled:
            _disable_now = dt_util.utcnow()
            try:
                self._abort_zone_experiment(zone_id, "learning_mode_disabled", _disable_now)
            except Exception:
                _LOGGER.warning("Learning: experiment abort on learning-disable failed for %s", zone_id)
            try:
                # Adoptions are preserved but suspended (authority removed at
                # runtime; no proactive inverse command).
                self._suspend_zone_adoptions(zone_id, "learning_mode_off", _disable_now)
            except Exception:
                _LOGGER.warning("Learning: adoption suspend on learning-disable failed for %s", zone_id)
            try:
                self._suspend_zone_strategy(zone_id, "learning_mode_off", _disable_now)
            except Exception:
                _LOGGER.warning("Learning: strategy suspend on learning-disable failed for %s", zone_id)
            try:
                # P10: route through the differentiated invalidation matrix so the
                # behaviour-mode-away semantics (suspend strategy + position
                # authority, preserve history) are applied with stable reason codes.
                self.apply_config_change_invalidation(
                    zone_id, _CI_CHANGE_MODE_AWAY, _disable_now)
            except Exception:
                _LOGGER.warning("Learning: config-change invalidation failed for %s", zone_id)
        self._persist_zone_controls()
        await self.async_request_refresh()

    async def async_set_zone_active_control_enabled(
        self, zone_id: str, enabled: bool
    ) -> None:
        """Toggle active_control_enabled for a zone; persist and refresh."""
        current = self.effective_zone_execution(zone_id)
        self._zone_execution_overrides[zone_id] = ZoneExecutionConfig(
            learning_enabled=current.learning_enabled,
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
                "learning_enabled": cfg.learning_enabled,
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

        # Staleness of the measured solar sensor: seconds since its last state write.
        # Defensive — any missing/unreadable timestamp yields None (treated as fresh),
        # so a stubbed/real state without last_updated never forces a false fallback.
        solar_age_s: float | None = None
        if self._solar_radiation_sensor_id is not None:
            _solar_state = self.hass.states.get(self._solar_radiation_sensor_id)
            _last_updated = getattr(_solar_state, "last_updated", None) if _solar_state is not None else None
            if _last_updated is not None:
                try:
                    solar_age_s = (dt_util.utcnow() - _last_updated).total_seconds()
                except Exception:
                    solar_age_s = None

        return _WeatherInputs(
            outdoor_temperature=self._read_value(self._outdoor_temperature_sensor_id, "temperature"),
            solar_radiation=self._read_value(self._solar_radiation_sensor_id, None),
            solar_radiation_age_s=solar_age_s,
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
                    # P10: restore compact shadow tombstones (provenance only —
                    # never creates runtime authority); bounded on next save.
                    self._shadow_tombstones = {
                        t.tombstone_key: t
                        for t in getattr(_extras, "shadow_tombstones", []) or []
                    }
                    # P7: restore bounded experiments with the restart safety rule
                    # (activated/observing can NEVER resume as complete; no target
                    # is re-injected by restore alone).
                    self._experiments_active, self._experiment_history = (
                        reconcile_restored_experiments(
                            _extras.bounded_experiments, _restore_now)
                    )
                    # P8: restore adoptions suspended pending fresh revalidation;
                    # no target is re-applied by restore alone.
                    self._adoptions_active, self._adoption_history = (
                        reconcile_restored_adoptions(
                            _extras.persistent_adoptions, _restore_now)
                    )
                    # P9B: restore strategy experiments + adoptions (never blindly
                    # reactivated; suspended pending fresh revalidation).
                    self._strategy_experiments_active, self._strategy_experiment_history = (
                        reconcile_restored_strategy_experiments(
                            _extras.strategy_experiments, _restore_now)
                    )
                    self._strategy_adoptions_active, self._strategy_adoption_history = (
                        reconcile_restored_strategy_adoptions(
                            _extras.persistent_strategy_adoptions, _restore_now)
                    )
                    # P10: ownership validation — a payload whose owner_entry_id is
                    # present but does NOT match this entry is foreign (copied file);
                    # drop ALL adaptive authority to prevent cross-zone authority.
                    _owner = getattr(_extras, "owner_entry_id", None)
                    if _owner is not None and _owner != self.config_entry.entry_id:
                        # P10: a foreign owner ⇒ reject the WHOLE learning payload of
                        # this file (not just adaptive sections) — no cross-zone data.
                        _LOGGER.warning(
                            "Learning: stored payload owner mismatch — rejecting whole payload")
                        self._learning_store = LearningStore()
                        self._thermal_models = {}
                        self._thermal_observations = {}
                        self._contribution_models = {}
                        self._contribution_evidence = {}
                        self._shadow_active = {}
                        self._shadow_history = []
                        self._shadow_tombstones = {}
                        self._experiments_active = {}
                        self._experiment_history = []
                        self._adoptions_active = {}
                        self._adoption_history = []
                        self._strategy_experiments_active = {}
                        self._strategy_experiment_history = []
                        self._strategy_adoptions_active = {}
                        self._strategy_adoption_history = []
                        self._consumed_ledger = ConsumedExperimentLedger()
                        self._restore_failures += 1
                    else:
                        # P10 acceptance fix: FAIL-CLOSED ledger restore.  Corruption
                        # / unsupported / owner-mismatch marks a namespace unsafe so
                        # we block new + suspend restored adaptive authority for it —
                        # consumed evidence is never released by corruption.
                        self._consumed_ledger, self._ledger_integrity = (
                            ConsumedExperimentLedger.restore_with_integrity(
                                getattr(_extras, "consumed_experiment_ledger", None),
                                owner_entry_id=getattr(_extras, "owner_entry_id", None),
                                current_entry_id=self.config_entry.entry_id,
                                now=_restore_now))
                        self._enforce_ledger_integrity(_restore_now)
                        # P10: structured per-section restore reason counters.
                        self._restore_diagnostics = (
                            getattr(_extras, "restore_diagnostics", {}) or {})
                        self._restore_diagnostics = dict(self._restore_diagnostics)
                        self._restore_diagnostics["consumed_ledger"] = {
                            "status_by_namespace": {
                                _LEDGER_POSITION: self._ledger_integrity.position,
                                _LEDGER_STRATEGY: self._ledger_integrity.strategy,
                            },
                            "invalid_by_reason": dict(self._ledger_integrity.invalid_by_reason),
                        }
                        # P10: reference-integrity — an adoption whose source/consumed
                        # experiment evidence is unresolvable is invalidated (hard
                        # reference), never blindly applied.
                        self._invalidate_unreferenced_adoptions(_restore_now)
                        # P10: typed config-change invalidation — diff the previous
                        # persisted config snapshot vs current and apply precise
                        # per-change directives (geometry/cover/sensor/forecast/mode/
                        # targets/window-removal) above the config_generation gate.
                        try:
                            self._apply_config_diff_on_restore(
                                getattr(_extras, "config_snapshot", {}) or {}, _restore_now)
                        except Exception:
                            _LOGGER.warning("Learning: config-diff invalidation failed (non-fatal)")
                        # Restart-safe: restore active manual overrides BEFORE the
                        # first dispatch decision so a pre-restart manual movement is
                        # honoured (not re-asserted).  Expired entries are dropped.
                        # Also seed the assumed-state last-commanded reference with the
                        # pre-override SmartShading target so the override is re-detected
                        # after its expiry exactly as in the no-restart case (otherwise
                        # the post-restart fallback to the observed user position would
                        # mask the override and re-assert the night position).
                        try:
                            _restored_ov = self._override_detector.restore_active_overrides(
                                getattr(_extras, "active_overrides", []) or [], _restore_now)
                            for _ov in _restored_ov:
                                if _ov.overridden_position is None:
                                    continue
                                _w = self.windows.get(_ov.window_id)
                                _cg = self.cover_groups.get(_w.cover_group_id) if _w else None
                                if _cg is None or not _cg.cover_ids:
                                    continue
                                _cid = _cg.cover_ids[0]
                                _cap = self._get_or_detect_capability(_cid)
                                self.assumed_state_manager.update(
                                    _cid, _ov.overridden_position,
                                    commanded_at=_ov.started_at,
                                    has_reliable_position_feedback=bool(
                                        getattr(_cap, "has_reliable_position_feedback", False)),
                                )
                        except Exception:
                            _LOGGER.warning("Learning: active-override restore failed (non-fatal)")
            except Exception:
                _LOGGER.warning("Learning: failed to reconcile restore extras (non-fatal)")
            # Write a schema-valid storage file immediately on first setup so
            # /config/.storage/smartshading_learning_<id> is visible right away,
            # even before any learning data has been collected.  Also performs the
            # one-shot controlled save after a v1→v2 migration (coordinator owns it).
            # Uses the SINGLE complete-snapshot authority (P10) — never partial.
            if self._learning_persistence.fresh_start or self._learning_persistence.migration_dirty:
                await self._save_learning_snapshot(_restore_now)
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

        # Rain sensor reading — once per cycle, shared across all windows.
        # Source type auto-detected from entity domain: binary_sensor.* → BINARY_SENSOR,
        # sensor.* → NUMERIC_RATE (mm/h), unconfigured → NONE.
        # build_rain_sensor_reading() handles absent/unavailable/stale → UNKNOWN.
        # Per-window enable/disable is resolved inside the window loop below.
        _rain_hs = (
            self.hass.states.get(self._rain_sensor_id)
            if self._rain_sensor_id else None
        )
        _rain_source_type: _RainSourceType = (
            _RainSourceType.BINARY_SENSOR
            if (self._rain_sensor_id or "").startswith("binary_sensor.")
            else _RainSourceType.NUMERIC_RATE
            if self._rain_sensor_id is not None
            else _RainSourceType.NONE
        )
        _rain_reading = _build_rain_sensor_reading(
            entity_id=self._rain_sensor_id,
            hass_state=_rain_hs.state if _rain_hs is not None else None,
            source_type=_rain_source_type,
            read_at_utc=getattr(_rain_hs, "last_updated", None) if _rain_hs is not None else None,
            now_utc=now,
        )
        _rain_status_global: _RainStatus = _rain_reading.status
        local_now = dt_util.as_local(now)  # same instant as now, converted to local timezone

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
        # Expose this cycle's forecast modifier for read-only diagnostics (trust,
        # applied delta, reason, forecast fields used) — never used as a measurement.
        self._cycle_forecast_modifier = _forecast_modifier

        # Lifecycle Engine: evaluated every cycle with absent-evidence semantics.
        # When sun.sun is unavailable, None is passed; the engine applies per-trigger
        # logic: FIXED_TIME fires on time alone, SUN_ELEVATION never triggers a new
        # state from absent data (elevation_met=False), BOTH uses only the time part.
        # This prevents false night triggers when a positive SUN_ELEVATION threshold
        # is configured and sun data is temporarily unavailable.
        _prev_lifecycle_state = self._lifecycle_state
        _sun_elevation = sun_position.elevation if sun_position is not None else None
        self._lifecycle_state = self.lifecycle_engine.get_lifecycle_state(
            local_now, _sun_elevation, self._lifecycle_config, self._lifecycle_state
        )

        # Night Hard Hold: pre-computed once per cycle for O(1) per-window check.
        # Dual condition: cached lifecycle state OR independent fresh evaluation.
        # The independent check catches windows using ABSENCE_ONLY behavior mode
        # (their WDI has lifecycle_state forced to DAY, defeating NightEvaluator).
        # check_night_interval_active accepts None elevation (substitutes 0.0).
        # Safety and Manual Override are exempt — they are checked per-window.
        _night_interval_active: bool = (
            self._lifecycle_state is LifecycleState.NIGHT
            or (
                self._lifecycle_config.night_enabled
                and check_night_interval_active(
                    local_now,
                    sun_position.elevation if sun_position is not None else None,
                    self._lifecycle_config,
                )
            )
        )

        # Diagnostics: lifecycle trigger reason and degraded-input codes.
        _lc_trigger_map = {
            LifecycleState.NIGHT: "night_start",
            LifecycleState.MORNING: "morning_start",
            LifecycleState.DAY: "day_start",
        }
        _lifecycle_trigger: str = (
            _lc_trigger_map.get(self._lifecycle_state, "no_change")
            if _prev_lifecycle_state != self._lifecycle_state
            else "no_change"
        )
        _degraded_input_codes: tuple[str, ...] = (
            ("sun_unavailable",) if sun_position is None else ()
        )
        _required_inputs_ready: bool = sun_position is not None

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
            obs_enabled = _exec.learning_enabled

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
                    self._mark_learning_dirty()
                except Exception:
                    pass  # never block the update cycle

            # Step 8c: lifecycle transition clears active override so the new
            # phase takes effect immediately without waiting for expiry.
            # Restart-safe: during startup grace the lifecycle state is freshly
            # recomputed (RAM), so the very first post-restart cycle would report
            # a spurious previous→current "transition" that must NOT break a
            # restored override.  A genuine later transition (e.g. morning) still
            # breaks it (grace is over by then).
            if (self._startup_cycles_remaining == 0
                    and lifecycle_should_break_override(
                        prev=_prev_lifecycle_state,
                        new=self._lifecycle_state,
                        break_enabled=self._override_break_on_lifecycle,
                    ) and active_override is not None):
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
                        self._mark_learning_dirty()
                    except Exception:
                        pass  # never block the update cycle

                self._override_detector.clear(window_id)
                # Suppress the next tick so that tick() in this same cycle does
                # not immediately re-create the override from the user's manual
                # position before the new morning/day target has been dispatched.
                # Without this suppress, abs(manual_pos - morning_target) > tolerance
                # would fire on every transition cycle, permanently undoing the clear.
                self._override_detector.suppress_next_override_tick(window_id)
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
                    learning_enabled=obs_enabled,
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
                    cycle_timestamp_utc=now,
                    restore_completed=self._learning_restored,
                    required_inputs_ready=_required_inputs_ready,
                    degraded_input_codes=_degraded_input_codes,
                    lifecycle_state_at_cycle=self._lifecycle_state.value,
                    previous_lifecycle_state=_prev_lifecycle_state.value,
                    lifecycle_trigger=_lifecycle_trigger,
                    startup_grace_active=(self._startup_cycles_remaining > 0),
                    rain_status=_rain_status_global.value,
                    rain_safe_active=None,
                    rain_release_remaining_s=None,
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
            # Authoritative solar source (solar_source.py): a configured, valid,
            # fresh and plausible measured sensor value is authoritative; a weather/
            # cloud estimate is a diagnosed fallback only and never overrides or
            # double-damps the measured value.  The estimate is always computed so
            # the diagnostics can show the value that was (or was not) used.
            _solar_estimate = self.weather_engine.calculate_effective_radiation(
                sun_elevation_deg=sun_position.elevation,
                cloud_cover_pct=weather_inputs.cloud_cover or 0.0,
            )
            _solar_sel = classify_solar_source(
                sensor_configured=self._solar_radiation_sensor_id is not None,
                measured_wm2=weather_inputs.solar_radiation,
                measured_age_s=weather_inputs.solar_radiation_age_s,
                estimated_wm2=_solar_estimate,
                cloud_cover_pct=weather_inputs.cloud_cover,
            )
            effective_radiation = _solar_sel.effective_radiation_wm2
            _solar_source = "sensor" if _solar_sel.source == SOURCE_MEASURED else "estimate"
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
            # --- Per-window rain config resolution --------------------------------
            # rain_protection_enabled: window override → hardware-type default.
            # rain_safe_position_ha: window override → HARDWARE_RAIN_SAFE_POSITIONS
            #   (HA convention: AWNING/EXTERIOR_SCREEN → 0 = retracted; others → 100 = raised).
            # rain_release_delay_min: window override → global default (30 min).
            # _early_hw_type is also used below for wind/storm position correction
            # and Night Hard Hold; resolved once here to avoid duplication.
            _early_cg = self.cover_groups.get(window.cover_group_id)
            _early_hw_type = (
                _early_cg.hardware_type
                if _early_cg is not None
                else CoverHardwareType.GENERIC
            )
            _hw_settings = default_hardware_settings(_early_hw_type)
            _rain_prot_enabled: bool = (
                window.rain_protection_enabled
                if window.rain_protection_enabled is not None
                else _hw_settings.get("rain_protection_enabled", False)
            )
            _rain_safe_ha: int = (
                window.rain_safe_position_ha
                if window.rain_safe_position_ha is not None
                else _HARDWARE_RAIN_SAFE_POSITIONS.get(_early_hw_type, 0)
            )
            # Convert HA convention to internal (0=open, 100=shaded).
            _rain_safe_internal: int = 100 - _rain_safe_ha
            _rain_delay_min: int = (
                window.rain_release_delay_min
                if window.rain_release_delay_min is not None
                else self.global_defaults.rain_release_delay_min
            )

            # --- Per-window contact sensor reading --------------------------------
            _cs_entity_id = window.contact_sensor_entity_id
            _cs_state_obj = self.hass.states.get(_cs_entity_id) if _cs_entity_id else None
            _cs_reading = _build_contact_reading(
                entity_id=_cs_entity_id,
                hass_state=_cs_state_obj.state if _cs_state_obj is not None else None,
                read_at_utc=_cs_state_obj.last_updated if _cs_state_obj is not None else None,
                now_utc=now,
            )
            _vent_pos_ha: int = (
                window.window_open_night_position_ha
                if window.window_open_night_position_ha is not None
                else _DEFAULT_VENT_POS_HA
            )
            _vent_pos_internal: int = to_internal_position(_vent_pos_ha)

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
                rain_status=_rain_status_global,
                rain_protection_enabled=_rain_prot_enabled,
                rain_safe_position=_rain_safe_internal,
                rain_release_delay_min=_rain_delay_min,
                active_override=active_override,
                override_duration_min=self._override_duration_min,
                override_detection_tolerance=self._override_detection_tolerance,
                override_break_on_lifecycle=self._override_break_on_lifecycle,
                night_block_on_window_open=window.night_block_on_window_open,
                night_lift_on_window_open=window.night_lift_on_window_open,
                window_open_night_position=_vent_pos_internal,
                contact_status=_cs_reading.status,
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
                self._adaptation_traces[window_id] = _adapt_trace

                # P9A Unified Solar Threshold Resolver — composes the learned solar
                # delta (adapted − configured) and the forecast delta EXACTLY ONCE
                # with a single final clamp.  Replaces the former sequential
                # adaptive-then-forecast threshold mutation (which clamped twice).
                # Measured solar irradiance stays authoritative; forecast only
                # shifts the precautionary entry thresholds.
                _cfg_bc = _wdi_preadapt.effective_behavior
                _fc_applied = _forecast_modifier is not None and _forecast_modifier.applied
                _solar_res = resolve_solar_thresholds(
                    configured_light_wm2=_cfg_bc.light_shade_threshold_wm2,
                    configured_normal_wm2=_cfg_bc.normal_shade_threshold_wm2,
                    configured_strong_wm2=_cfg_bc.strong_shade_threshold_wm2,
                    learned_delta_light=(_adapted_bc.light_shade_threshold_wm2
                                         - _cfg_bc.light_shade_threshold_wm2),
                    learned_delta_normal=(_adapted_bc.normal_shade_threshold_wm2
                                          - _cfg_bc.normal_shade_threshold_wm2),
                    learned_delta_strong=(_adapted_bc.strong_shade_threshold_wm2
                                          - _cfg_bc.strong_shade_threshold_wm2),
                    forecast_delta_wm2=(_forecast_modifier.threshold_delta_wm2
                                        if _fc_applied else 0.0),
                    forecast_available=_fc_applied,
                    forecast_trust_score=(_forecast_modifier.trust_score
                                          if _forecast_modifier is not None else None),
                    strategy_threshold_delta_wm2=(_strat_thr_delta := self._strategy_threshold_delta(
                        window_id, exposure.effective_exposure,
                        weather_inputs.outdoor_temperature, now)),
                )
                _adapted_bc = replace(
                    _adapted_bc,
                    light_shade_threshold_wm2=_solar_res.effective_light_wm2,
                    normal_shade_threshold_wm2=_solar_res.effective_normal_wm2,
                    strong_shade_threshold_wm2=_solar_res.effective_strong_wm2,
                )
                self._cycle_solar_resolution[window_id] = _solar_res
                # P11.3 closure: read-only solar-transformation + entry-threshold
                # provenance from values ALREADY computed this cycle (no recompute).
                # Cloud is reflected in the source value (measured) or applied once in
                # the estimate path — never a second time here.
                self._cycle_solar_provenance[window_id] = {
                    "exposure": exposure,
                    "base_solar_wm2": effective_radiation,
                    "solar_source": _solar_source,
                    "solar_selection": _solar_sel,
                    "glare_protection_enabled": wdi.effective_behavior.glare_protection_enabled,
                    "glare_min_exposure_wm2": wdi.effective_behavior.glare_min_exposure_wm2,
                    "configured_light_wm2": _cfg_bc.light_shade_threshold_wm2,
                    "configured_normal_wm2": _cfg_bc.normal_shade_threshold_wm2,
                    "configured_strong_wm2": _cfg_bc.strong_shade_threshold_wm2,
                    "strategy_threshold_delta_wm2": _strat_thr_delta,
                }
                wdi = replace(wdi, effective_behavior=_adapted_bc)

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

                _eff_ha = {"light": _light_eff_ha, "normal": _normal_eff_ha,
                           "strong": _strong_eff_ha}
                _cfg_ha = {"light": _light_cfg_ha, "normal": _normal_cfg_ha,
                           "strong": _strong_cfg_ha}

                # P8 — persistent adoption injection (single Tier-5 parameter,
                # BELOW manual preference, ABOVE the P7 experiment).  Runs before
                # the experiment so a P7 experiment tests -5 pp against the adopted
                # base; the shared cumulative cap keeps total ≤ -10 pp vs config.
                try:
                    wdi = self._adoption_apply(
                        zone=zone, window=window, window_id=window_id, wdi=wdi,
                        eff_ha=_eff_ha, cfg_ha=_cfg_ha,
                        exposure_wm2=exposure.effective_exposure,
                        outdoor_temp=weather_inputs.outdoor_temperature, now=now,
                    )
                except Exception:
                    _LOGGER.warning(
                        "Learning: adoption injection failed for %s (non-fatal)", window_id
                    )

                # P7 — bounded experiment injection (single Tier-5 parameter).
                # Strictly gated; never bypasses any higher authority because it
                # only overrides one intensity position BEFORE the tier evaluation,
                # so every downstream resolver/clamp/harmonization/command-filter
                # still applies.  Returns the (possibly) modified wdi.
                try:
                    _nc_hold_pre = self._night_contact_holds.get(window_id)
                    _nc_blocked_pre = (
                        _nc_hold_pre is not None
                        and (_nc_hold_pre.blocked_this_night or _nc_hold_pre.night_vent_active)
                    )
                    wdi = self._experiment_try_inject(
                        zone=zone, window=window, window_id=window_id, wdi=wdi,
                        eff_ha=_eff_ha, cfg_ha=_cfg_ha,
                        exposure_wm2=exposure.effective_exposure,
                        outdoor_temp=weather_inputs.outdoor_temperature,
                        in_solar_sector=_effective_in_solar_sector,
                        manual_pref_active=_any_pos_adapted,
                        current_state=current_state,
                        night_contact_blocked=_nc_blocked_pre,
                        now=now,
                    )
                except Exception:
                    _LOGGER.warning(
                        "Learning: experiment injection failed for %s (non-fatal)", window_id
                    )

                # P9A Tier-Order Resolver — project the FINAL effective per-intensity
                # set onto Strong ≤ Normal ≤ Light so no adaptive change (manual
                # preference, adoption, experiment, learned position) can invert the
                # semantic stages.  Pure projection of the effective set; never
                # rewrites stored configuration.
                try:
                    _eb = wdi.effective_behavior
                    _proj = project_tier_order(
                        light_ha=to_ha_position(_eb.light_shade_position),
                        normal_ha=to_ha_position(_eb.normal_shade_position),
                        strong_ha=to_ha_position(_eb.strong_shade_position),
                    )
                    if _proj.projected:
                        wdi = replace(wdi, effective_behavior=replace(
                            _eb,
                            light_shade_position=to_internal_position(_proj.light_ha),
                            normal_shade_position=to_internal_position(_proj.normal_ha),
                            strong_shade_position=to_internal_position(_proj.strong_ha),
                        ))
                    self._cycle_tier_order[window_id] = _proj
                except Exception:
                    _LOGGER.warning(
                        "Learning: tier-order projection failed for %s (non-fatal)", window_id
                    )

                # P9A Strategy Resolver — observe/recommend only (no control
                # authority).  Builds a ShadingStrategyCandidate for diagnostics.
                try:
                    self._strategy_observe(
                        window=window, window_id=window_id, wdi=wdi,
                        exposure_wm2=exposure.effective_exposure,
                        in_solar_sector=_effective_in_solar_sector,
                        current_state=current_state, outdoor_temp=weather_inputs.outdoor_temperature,
                        solar_resolution=_solar_res, now=now)
                except Exception:
                    _LOGGER.warning(
                        "Learning: strategy observe failed for %s (non-fatal)", window_id
                    )
            # else: wdi stays as resolved from config; neutral profile is implied.

            # Per-window behavior mode (v1.0): restrict which tiers are active.
            # Safety (Tier 1: Storm/Wind) is never suppressed regardless of mode.
            # Extracted to a pure helper (P2) so the deterministic baseline pass
            # applies the identical masking before its own tier evaluation.
            wdi = _apply_window_behavior_mode(wdi, window.behavior_mode)

            tier_decision = self._tier_orchestrator.evaluate_window(wdi)

            # P9B Live Authority: apply bounded strategy families to a comfort-tier
            # decision (exit/hysteresis, tier-choice, entry/exit timing).  No-op
            # for safety/lifecycle/override states; all higher authorities below
            # (night hold, behavior-mode suppression, StateGuard, CommandFilter)
            # still apply and win.
            self._cycle_strategy_applied.pop(window_id, None)
            if obs_enabled:
                try:
                    tier_decision = self._strategy_runtime_apply(
                        window=window, window_id=window_id, wdi=wdi,
                        tier_decision=tier_decision, current_state=current_state,
                        exposure=exposure.effective_exposure,
                        outdoor=weather_inputs.outdoor_temperature, now=now)
                except Exception:
                    _LOGGER.warning(
                        "Learning: strategy runtime apply failed for %s (non-fatal)", window_id)

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

            # --- Rain Safe Position Correction + Release Hold ----------------------
            # Mirror of the storm/wind position-correction block above.
            # AWNING/EXTERIOR_SCREEN: "safe" is HA 0 = retracted (internal 100).
            # Other types: already correct via HARDWARE_RAIN_SAFE_POSITIONS.
            # Position correction is only needed if the evaluator fired directly.
            if tier_decision.shading_state is ShadingState.RAIN_SAFE:
                if tier_decision.target_position != _rain_safe_internal:
                    tier_decision = replace(tier_decision, target_position=_rain_safe_internal)

            # Rain hysteresis hold — dry cooldown via per-call hold_s override.
            # RAIN_SAFE does not override STORM_SAFE or WIND_SAFE (lower priority).
            _eval_is_rain = tier_decision.shading_state is ShadingState.RAIN_SAFE
            _rain_sensor_unavailable = _rain_status_global is _RainStatus.UNKNOWN
            _rain_h = self._rain_holds.setdefault(window_id, _SafetyHold(_hold_s=_RAIN_HOLD_S))
            _rain_held = _rain_h.update(
                evaluator_triggered=_eval_is_rain,
                now=now,
                sensor_unavailable=_rain_sensor_unavailable,
                hold_s=_rain_delay_min * 60,
            )
            if (
                _rain_held
                and not _eval_is_rain
                and tier_decision.shading_state not in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE)
            ):
                tier_decision = WindowDecision(
                    window_id=window_id,
                    shading_state=ShadingState.RAIN_SAFE,
                    target_position=_rain_safe_internal,
                    decided_by="RainSafeHold",
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

            # --- Night Contact Hold ------------------------------------------------
            # Post-safety modifier: Option A (block night move while contact OPEN),
            # Option B (lift to NIGHT_VENT when contact opens after night move done).
            # Runs BEFORE Night Hard Hold so NIGHT_VENT is exempt from NightHardHold.
            # Safety states (STORM_SAFE / WIND_SAFE / RAIN_SAFE) are never modified.
            _nc_hold = self._night_contact_holds.setdefault(window_id, _NightContactHold())
            _nc_hold.on_lifecycle_transition(night_active=_night_interval_active)
            _nc_action = _nc_hold.evaluate(
                contact_open=_cs_reading.status is _ContactStatus.OPEN,
                contact_unknown=_cs_reading.status is _ContactStatus.UNKNOWN,
                night_active=_night_interval_active,
                night_block_enabled=wdi.effective_behavior.night_block_on_window_open,
                night_lift_enabled=wdi.effective_behavior.night_lift_on_window_open,
                night_decision_pending=tier_decision.shading_state is ShadingState.NIGHT_CLOSED,
            )
            if _nc_action == _NightContactAction.BLOCK:
                tier_decision = WindowDecision(
                    window_id=window_id,
                    shading_state=ShadingState.OPEN,
                    target_position=None,
                    decided_by="NightContactBlock",
                )
            elif _nc_action == _NightContactAction.CATCH_UP:
                tier_decision = WindowDecision(
                    window_id=window_id,
                    shading_state=ShadingState.NIGHT_CLOSED,
                    target_position=wdi.effective_behavior.night_position,
                    decided_by="NightContactCatchUp",
                )
            elif _nc_action == _NightContactAction.HOLD_NIGHT_VENT:
                tier_decision = WindowDecision(
                    window_id=window_id,
                    shading_state=ShadingState.NIGHT_VENT,
                    target_position=wdi.effective_behavior.window_open_night_position,
                    decided_by="NightContactVent",
                )
            elif _nc_action == _NightContactAction.RETURN_TO_NIGHT:
                tier_decision = WindowDecision(
                    window_id=window_id,
                    shading_state=ShadingState.NIGHT_CLOSED,
                    target_position=wdi.effective_behavior.night_position,
                    decided_by="NightContactReturnToNight",
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
            # Priority: Safety > Night Contact Hold > Night Hard Hold.
            # NIGHT_VENT is exempt — it is an intentional above-night-position state.
            _window_behavior = window.behavior_mode
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
                    ShadingState.RAIN_SAFE,
                    ShadingState.MANUAL_OVERRIDE,
                    ShadingState.NIGHT_VENT,
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
                    and should_allow_lifecycle_release(
                        prev=_prev_lifecycle_state,
                        new=self._lifecycle_state,
                        current_shading_state=current_state,
                        active_override=active_override,
                        proposed_is_open=tier_decision.shading_state is ShadingState.OPEN,
                    )
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
            # P9B MINIMUM_HOLD: bounded extra hold (floored at a safe minimum);
            # default 0 → identical to the deterministic StateGuard baseline.
            _mh_extra = timedelta(0)
            if obs_enabled:
                try:
                    _mh_td, _mh_applied = self._strategy_min_hold_extra(
                        window_id, exposure.effective_exposure,
                        weather_inputs.outdoor_temperature, now)
                    if _mh_applied:
                        _base = _DEFAULT_MINIMUM_STATE_DURATION.get(current_state, timedelta(0))
                        _eff_min = effective_min_hold_minutes(
                            _base.total_seconds() / 60.0,
                            delta_min=_mh_td.total_seconds() / 60.0, safe_floor_minutes=2.0)
                        _mh_extra = timedelta(minutes=_eff_min) - _base
                except Exception:
                    _mh_extra = timedelta(0)
            if bypasses_guard(current_state, proposed_state):
                new_state, guard_blocked = proposed_state, False
            elif self.guard.is_locked(window_id, current_state, now, extra_hold=_mh_extra):
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
                                solar_source_quality=_solar_sel.quality,
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
                                rain_status_at_decision=_rain_status_global.value,
                                rain_safe_active_at_decision=(
                                    new_state is ShadingState.RAIN_SAFE
                                    or _rain_status_global is _RainStatus.RAINING
                                ),
                                night_contact_blocked_at_decision=_nc_hold.blocked_this_night,
                                night_vent_active_at_decision=_nc_hold.night_vent_active,
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
                contact_sensor_configured=_cs_entity_id is not None,
                contact_status_value=_cs_reading.status.value,
                contact_is_stale=_cs_reading.is_stale,
                night_contact_blocked=_nc_hold.blocked_this_night,
                night_contact_catch_up_pending=_nc_hold.catch_up_pending,
                night_contact_catch_up_done=_nc_hold.caught_up_this_night,
                night_vent_active=_nc_hold.night_vent_active,
                night_contact_state_label=_nc_hold.state_label,
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
            # P11 accuracy closure: planned (pre-sleep) vs ACTUAL elapsed global wait.
            _planned_global_wait_ms: int | None = None
            _actual_global_wait_ms: float | None = None
            _global_wait_started_mono: float | None = None
            _global_slot_granted_mono: float | None = None
            _global_timing_recording_status: str = "recorded"

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
                        # Safety gate: safety intents bypass grace but have already
                        # passed CommandFilter — active_control_enabled=False blocks
                        # all commands (including safety) via BLOCKED_RECOMMENDATION_ONLY
                        # before reaching here. Sensor unavailability prevents safety
                        # ShadingState from being set. No restored safety state exists.
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
                        #   1. Throttle: ALL intents (including safety) sleep until
                        #      ≥1.0 s have elapsed since the previous SENT command.
                        #      Safety has queue priority but not a timing exemption.
                        #   2. Stale-intent guard: non-safety only.  Safety always
                        #      dispatches even if the generation changed.
                        #
                        # POSITION INVARIANT: dispatch_cover_intent uses
                        # target_position_ha, never target_position_internal.
                        async with self._serial_dispatch.lock:
                            _wait = self._serial_dispatch.time_until_next_allowed()
                            if _wait.total_seconds() > 0:
                                _dispatch_throttled = True
                                # PLANNED wait = value returned BEFORE the sleep.
                                _throttle_wait_ms = round(_wait.total_seconds() * 1000)
                                _planned_global_wait_ms = _throttle_wait_ms
                                if self._debug_logging_enabled:
                                    _LOGGER.debug(
                                        "SmartShading: dispatch throttle: sleeping %.0f ms "
                                        "before cover=%s ha_pos=%s",
                                        _wait.total_seconds() * 1000,
                                        _intent.cover_entity_id,
                                        _intent.target_position_ha,
                                    )
                                # ACTUAL wait = monotonic elapsed across the real await.
                                try:
                                    _global_wait_started_mono = time.monotonic()
                                    await asyncio.sleep(_wait.total_seconds())
                                    _global_slot_granted_mono = time.monotonic()
                                    _actual_global_wait_ms = max(
                                        0.0,
                                        (_global_slot_granted_mono - _global_wait_started_mono)
                                        * 1000.0)
                                except Exception:
                                    # Diagnostics clock failure must not affect dispatch:
                                    # the sleep already completed; mark timing not_recorded.
                                    _global_timing_recording_status = "not_recorded"
                                    _actual_global_wait_ms = None
                            else:
                                _planned_global_wait_ms = 0
                                _actual_global_wait_ms = 0.0
                            # Stale-intent guard: a presence event that fired
                            # while we waited for the lock or slept through the
                            # throttle already incremented _dispatch_generation.
                            # Cancel this non-safety intent; the refresh queued
                            # by that event will dispatch the correct state.
                            # Safety is exempt — always dispatches.
                            if not _intent.is_safety:
                                if self._dispatch_generation != _this_dispatch_gen:
                                    _exec_results.append(build_not_attempted_result(
                                        _intent,
                                        reason="stale_presence_superseded",
                                    ))
                                    continue
                            _dispatch_now = dt_util.utcnow()
                            _intent_result = await dispatch_cover_intent(
                                self.hass, _intent, now_utc=_dispatch_now
                            )
                            # Update throttle clock whenever async_call was started.
                            # SENT:   confirmed dispatch — always update.
                            # FAILED: async_call was invoked (but raised) — the call
                            #   starts the global 1.0 s interval regardless of outcome.
                            # NOT_ATTEMPTED / BLOCKED: no async_call — do not update.
                            if _intent_result.status in (
                                ExecutionStatus.SENT, ExecutionStatus.FAILED
                            ):
                                self._serial_dispatch.record_dispatch(_dispatch_now)
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
                learning_enabled=s.obs_enabled,
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
                # Clock / bootstrap / lifecycle diagnostics.
                cycle_timestamp_utc=now,
                restore_completed=self._learning_restored,
                required_inputs_ready=_required_inputs_ready,
                degraded_input_codes=_degraded_input_codes,
                lifecycle_state_at_cycle=self._lifecycle_state.value,
                previous_lifecycle_state=_prev_lifecycle_state.value,
                lifecycle_trigger=_lifecycle_trigger,
                startup_grace_active=(self._startup_cycles_remaining > 0),
                rain_status=_rain_status_global.value,
                rain_safe_active=(
                    _rain_held
                    or current_state is ShadingState.RAIN_SAFE
                    or new_state is ShadingState.RAIN_SAFE
                ),
                rain_release_remaining_s=(
                    max(0.0, _rain_delay_min * 60 - (_rain_h.seconds_held(now) or 0.0))
                    if _rain_held and not _eval_is_rain
                    else None
                ),
                contact_sensor_configured=s.contact_sensor_configured,
                contact_status=s.contact_status_value,
                contact_is_stale=s.contact_is_stale,
                night_contact_blocked=s.night_contact_blocked,
                catch_up_pending=s.night_contact_catch_up_pending,
                catch_up_done=s.night_contact_catch_up_done,
                night_vent_active=s.night_vent_active,
                night_contact_state_label=s.night_contact_state_label,
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
                # P11: read-only ephemeral decision + dispatch trace (never
                # persisted, bounded).  Pure observation — control is unaffected.
                _disp_ctx = self._dispatch_context(
                    s, throttled=_dispatch_throttled,
                    planned_ms=_planned_global_wait_ms, actual_ms=_actual_global_wait_ms,
                    started_mono=_global_wait_started_mono,
                    slot_granted_mono=_global_slot_granted_mono,
                    timing_status=_global_timing_recording_status)
                try:
                    self._record_decision_trace(
                        window_id, s, harm, _dispatch_prov, _last_exec_result, now,
                        disp_ctx=_disp_ctx)
                except Exception:
                    _LOGGER.debug("Diagnostics: decision-trace capture skipped (non-fatal)")
                try:
                    self._record_dispatch_trace(
                        window_id, s, harm, _dispatch_prov, _last_exec_result, now,
                        disp_ctx=_disp_ctx)
                except Exception:
                    _LOGGER.debug("Diagnostics: dispatch-trace capture skipped (non-fatal)")
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
            await self._save_learning_snapshot(now)

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

    def _record_decision_trace(self, window_id, s, harm, dispatch_prov, exec_result,
                               now, *, disp_ctx=None) -> None:
        """P11: bounded, ephemeral, read-only decision trace at the real decision
        path.  Captures the resolved decision, an HONEST candidate trace (winner +
        baseline recorded; everything else not_recorded — never reconstructed), the
        authority map (actual runtime influence), the full target chain and the
        no-dispatch reason.  Never re-runs an evaluator; never affects control."""
        decision_id = getattr(s, "decision_id", None)
        if decision_id is None:
            return  # only material decisions (P2 materiality gates decision_id)
        window = getattr(s, "window", None)
        zone_id = getattr(window, "zone_id", None) or "unknown"
        filt = getattr(s, "exec_filter_result", None)

        def _sv(state):
            return getattr(state, "value", None) if state is not None else None

        decided_by = getattr(s, "tier_decided_by", None) or getattr(s, "baseline_decided_by", None)
        resolved_target = filt.target_position_ha if filt is not None else None
        # --- honest candidate trace: winner + baseline recorded; rest not_recorded ---
        winner = {
            "candidate_type": "winner", "recording_status": "recorded",
            "was_selected": True, "decided_by": decided_by,
            "proposed_state": _sv(getattr(s, "new_state", None)),
            "proposed_position_ha": resolved_target,
        }
        baseline = {
            "candidate_type": "baseline", "recording_status": "recorded",
            "was_selected": False,
            "decided_by": getattr(s, "baseline_decided_by", None),
            "proposed_state": _sv(getattr(s, "baseline_state", None)),
            "proposed_position_ha": getattr(s, "normal_cfg_ha_for_prov", None),
        }
        not_recorded = [
            {"candidate_type": t, "recording_status": "not_recorded"}
            for t in ("safety", "manual_override", "lifecycle", "absence",
                      "heat", "glare", "solar", "position_learning", "strategy_learning")
        ]
        # --- authority map: actual runtime influence only ---
        pos_applied = window_id in self._cycle_adoption_applied
        strat_applied = window_id in self._cycle_strategy_applied
        cf_blocked = bool(filt is not None and not filt.allowed)
        authorities = {
            "safety_authority": {"active": bool(getattr(s, "is_safety", False)),
                                 "applied": bool(getattr(s, "is_safety", False)),
                                 "source_type": "safety"},
            "manual_override_authority": {
                "active": bool(getattr(s, "manual_override_active_at_decision", False)
                               or getattr(s, "is_override_active", False)),
                "source_type": "manual_override"},
            "lifecycle_authority": {
                "active": getattr(s, "lifecycle_state_value", "day") != "day",
                "source_type": "lifecycle"},
            "behavior_mode_authority": {
                "source_type": str(getattr(window, "behavior_mode", None))},
            "absence_authority": {
                "active": bool(getattr(s, "absence_active_at_decision", False)),
                "source_type": "absence"},
            "solar_authority": {"active": bool(getattr(s, "in_solar_sector", True)),
                                "source_type": "solar"},
            "position_learning_authority": {"applied": pos_applied,
                                            "source_type": "position_learning"},
            "strategy_learning_authority": {"applied": strat_applied,
                                            "source_type": "strategy_learning"},
            "harmonization_authority": {
                "applied": bool(getattr(harm, "harmonized", False)),
                "source_type": "harmonization"},
            "state_guard_authority": {"recording_status": "not_recorded"},
            "command_filter_authority": {
                "blocked": cf_blocked,
                "reason_code": (filt.blocked_reason if filt is not None else None),
                "source_type": "command_filter"},
            "dispatch_authority": {
                "applied": bool(dispatch_prov.dispatch_succeeded),
                "blocked": dispatch_prov.dispatch_allowed is False,
                "reason_code": dispatch_prov.dispatch_filter_reason,
                "source_type": "dispatch"},
        }
        # --- no-dispatch reason (follows the real gate/filter order) ---
        command_sent = bool(dispatch_prov.dispatch_succeeded)
        contributing: list = []
        primary = None
        if not command_sent:
            if not getattr(s, "active_control_enabled", False):
                primary = "active_control_off"
            elif cf_blocked:
                primary = filt.blocked_reason or "command_filter_suppressed"
            elif not dispatch_prov.dispatch_attempted:
                primary = "dispatch_not_required"
            else:
                primary = dispatch_prov.dispatch_filter_reason or "not_recorded"
            if dispatch_prov.dispatch_filter_reason and dispatch_prov.dispatch_filter_reason != primary:
                contributing.append(dispatch_prov.dispatch_filter_reason)
        rec = {
            "decision_id": decision_id,
            "window_id": window_id,
            "decision_timestamp_utc": now.isoformat() if now is not None else None,
            "baseline_state": _sv(getattr(s, "baseline_state", None)),
            "resolved_state": _sv(getattr(s, "new_state", None)),
            "decided_by": decided_by,
            "config_generation": getattr(s, "config_generation", 0),
            "adapt_confidence_level": getattr(s, "adapt_confidence_level", None),
            "candidates": [winner, baseline] + not_recorded,
            "authorities": authorities,
            "target_chain": {
                "recommendation_position_ha": getattr(s, "normal_cfg_ha_for_prov", None),
                "resolved_target_position_ha": resolved_target,
                "pre_command_filter_target_ha": resolved_target,
                "post_harmonization_target_ha": getattr(harm, "final_target_position_ha", None),
                "intended_payload_position_ha": (
                    dispatch_prov.requested_target_ha if command_sent else None),
                # ACTUAL payload from the real ExecutionResult (== the async_call
                # value); None when nothing was actually sent.
                "actual_payload_position_ha": (
                    getattr(exec_result, "target_position_ha", None) if command_sent else None),
                "final_dispatched_target_ha": (
                    getattr(exec_result, "target_position_ha", None) if command_sent else None),
                "target_tilt_ha": getattr(s, "target_tilt_ha", None),
            },
            "no_dispatch": {
                "recommendation_exists": getattr(s, "normal_cfg_ha_for_prov", None) is not None,
                "command_sent": command_sent,
                "primary_reason": primary,
                "contributing_reasons": contributing,
            },
            "dispatch_context": disp_ctx if isinstance(disp_ctx, dict) else {},
        }
        ring = self._decision_trace.setdefault(
            zone_id, deque(maxlen=DECISION_TRACE_MAX_RECORDS_PER_ZONE))
        ring.append(rec)

    def decision_trace_snapshot(self) -> dict:
        """Read-only snapshot of the ephemeral decision trace (RAM only, bounded).
        Raw ids present here; the export layer pseudonymizes them."""
        return {zid: {"records": list(ring), "count": len(ring)}
                for zid, ring in self._decision_trace.items()}

    def _dispatch_context(self, s, *, throttled=False, planned_ms=None, actual_ms=None,
                          started_mono=None, slot_granted_mono=None,
                          timing_status="recorded") -> dict:
        """P11: read-only global-interval + movement + position-before context from
        ALREADY-COMPUTED runtime data (this-cycle snapshot + throttle observation).
        No new polling/state read; honest not_recorded where data is absent.

        Architecture note: there is NO real dispatch queue — only a global asyncio
        lock + monotonic min-interval throttle.  Queue fields are therefore
        not_recorded.  PLANNED wait is the pre-sleep value from
        time_until_next_allowed(); ACTUAL wait is the monotonic elapsed across the
        real await — they are kept distinct and never silently equated."""
        # actual: honest null when the diagnostic clock failed (never fall back to
        # planned).  overrun = actual − planned when both are present.
        overrun = None
        if (isinstance(actual_ms, (int, float)) and isinstance(planned_ms, (int, float))):
            overrun = max(0.0, float(actual_ms) - float(planned_ms))
        ctx: dict = {
            # No real queue exists → honest not_recorded (never mislabel throttle).
            "queue_entered_at_monotonic": None,
            "queue_slot_granted_at_monotonic": None,
            "queue_wait_ms": None,
            "queue_recording_status": "not_recorded",
            # Real global serial min-interval wait (planned vs measured).
            "global_wait_required": bool(throttled),
            "planned_global_interval_wait_ms": (planned_ms if throttled else 0),
            "actual_global_interval_wait_ms": (
                actual_ms if (timing_status == "recorded") else None),
            "global_wait_started_at_monotonic": started_mono,
            "global_slot_granted_at_monotonic": slot_granted_mono,
            "global_wait_overrun_ms": overrun if timing_status == "recorded" else None,
            "timing_recording_status": timing_status,
            "required_global_interval_ms": None,
        }
        try:
            mi = getattr(getattr(self._serial_dispatch, "_throttle", None), "min_interval", None)
            ctx["required_global_interval_ms"] = (
                round(mi.total_seconds() * 1000) if mi is not None else None)
        except Exception:
            ctx["required_global_interval_ms"] = None
        # Movement + position-before from THIS cycle's snapshot (entity_state source).
        # TravelTracker is NOT consulted: it is owned by CoverController and is not
        # updated by the coordinator's live dispatch path, so it cannot reliably know
        # the coordinator's active travel.  unknown ≠ false: movement is only
        # recorded when the entity is available with a definite (non-unknown) state.
        snap = getattr(s, "exec_snapshot", None)
        _unknown_states = (None, "unknown", "unavailable")
        if snap is not None and getattr(snap, "available", False) and \
                getattr(snap, "state", None) not in _unknown_states:
            moving = bool(getattr(snap, "is_opening", False) or getattr(snap, "is_closing", False))
            has_fb = bool(getattr(snap, "has_position_feedback", False))
            ctx.update({
                "cover_was_moving": moving,
                "moving_state_source": "entity_state",
                "moving_state_confidence": "reported",
                "movement_recording_status": "recorded",
                "reported_position_before_ha": (
                    getattr(snap, "current_position_ha", None) if has_fb else None),
                "position_feedback_type": "reliable" if has_fb else "estimated_or_none",
                "position_is_estimated": not has_fb,
                "position_before_recording_status": "recorded" if has_fb else "not_recorded",
            })
        else:
            # No reliable source → honest unknown (never false), no position claim.
            ctx.update({
                "cover_was_moving": None, "moving_state_source": "not_recorded",
                "moving_state_confidence": None, "movement_recording_status": "not_recorded",
                "reported_position_before_ha": None,
                "position_feedback_type": "not_recorded", "position_is_estimated": None,
                "position_before_recording_status": "not_recorded",
            })
        # reported_position_after is honestly not_recorded (no exact later correlation
        # and no diagnostic polling is performed).
        ctx["reported_position_after_ha"] = None
        ctx["post_position_recording_status"] = "not_recorded"
        return ctx

    def _record_dispatch_trace(self, window_id, s, harm, dispatch_prov, exec_result,
                               now, *, disp_ctx=None) -> None:
        """P11: append a read-only, ephemeral, bounded dispatch trace record + update
        per-cover retarget state.  The ACTUAL payload + service name + monotonic
        service duration + safe failure type come from the real ExecutionResult
        captured AT the service boundary (ha_service_adapter); the intended payload
        is the coordinator/filter value, kept distinct.  Never affects control."""
        window = getattr(s, "window", None)
        zone_id = getattr(window, "zone_id", None) or "unknown"
        cover_id = getattr(s, "exec_entity_id", None)
        if not dispatch_prov.dispatch_attempted:
            return  # only real service-boundary attempts enter the trace
        intended = dispatch_prov.requested_target_ha
        filt = getattr(s, "exec_filter_result", None)
        er = exec_result
        status = getattr(getattr(er, "status", None), "value", None) or dispatch_prov.dispatch_status
        actually_sent = bool(er is not None and status in ("SENT", "sent"))
        # ExecutionResult.target_position_ha is exactly the value the adapter passed
        # to hass.services.async_call (code-proven: no adapter conversion).
        actual_pos = getattr(er, "target_position_ha", None) if actually_sent else None
        actual_tilt = getattr(er, "target_tilt", None) if (
            actually_sent and getattr(er, "tilt_sent", False)) else None
        rec = {
            "decision_id": getattr(s, "decision_id", None),
            "window_id": window_id,
            "cover_id": cover_id,
            "actual_service_domain": "cover" if actually_sent else None,
            "actual_service_name": ("set_cover_position" if actual_pos is not None
                                    else ("set_cover_tilt_position" if actual_tilt is not None
                                          else None)),
            "intended_payload_position_ha": intended,
            "actual_payload_position_ha": actual_pos,
            "actual_payload_tilt_ha": actual_tilt,
            "actual_payload_has_position": actual_pos is not None,
            "actual_payload_has_tilt": actual_tilt is not None,
            "service_duration_ms": getattr(er, "service_duration_ms", None),
            "service_started_monotonic": getattr(er, "service_started_monotonic", None),
            "service_completed_monotonic": getattr(er, "service_completed_monotonic", None),
            "dispatch_status": status,
            "failure_exception_type_safe": getattr(er, "failure_exception_type", None),
            "dispatch_filter_reason": dispatch_prov.dispatch_filter_reason,
            "recommendation_position_ha": getattr(s, "normal_cfg_ha_for_prov", None),
            "pre_command_filter_target_ha": (
                filt.target_position_ha if filt is not None else None),
            "post_harmonization_target_ha": getattr(harm, "final_target_position_ha", None),
            "harmonized": bool(getattr(harm, "harmonized", False)),
            "at": now.isoformat() if now is not None else None,
        }
        # P11 final: global-interval + movement + position-before context (read-only).
        if isinstance(disp_ctx, dict):
            rec.update(disp_ctx)
        # Retarget detection on ACTUALLY sent commands only (suppressed / failed-
        # before-start / intended-only do not count).  Per-cover state advances only
        # on an actual send.
        cstate = self._cover_dispatch_state.setdefault(cover_id or "unknown", {
            "last_actual_position_ha": None, "last_dispatch_at": None,
            "last_dispatch_status": None, "recent_at": deque(maxlen=64),
            "same_cover_retarget_count": 0,
        })
        is_retarget = False
        if actually_sent and actual_pos is not None:
            prev = cstate["last_actual_position_ha"]
            prev_at = cstate["last_dispatch_at"]
            is_retarget = (
                prev is not None and prev != actual_pos and prev_at is not None
                and (now - prev_at) <= timedelta(seconds=RETARGET_TRACE_WINDOW_SECONDS))
            rec["previous_actual_target_position_ha"] = prev
            rec["previous_dispatch_status"] = cstate["last_dispatch_status"]
            rec["previous_dispatch_age_ms"] = (
                round((now - prev_at).total_seconds() * 1000.0, 1)
                if prev_at is not None else None)
            rec["target_delta_ha"] = (
                (actual_pos - prev) if is_retarget and prev is not None else None)
            if is_retarget:
                cstate["same_cover_retarget_count"] += 1
            cstate["last_actual_position_ha"] = actual_pos
            cstate["last_dispatch_at"] = now
            cstate["last_dispatch_status"] = status
            cstate["recent_at"].append(now)
        rec["is_retarget"] = bool(is_retarget)
        # Source provenance only when production proves it — else not_recorded.
        rec["retarget_source"] = None
        rec["source_recording_status"] = "not_recorded" if is_retarget else "n/a"
        ring = self._dispatch_trace.setdefault(
            zone_id, deque(maxlen=DISPATCH_TRACE_MAX_RECORDS_PER_ZONE))
        ring.append(rec)

    def dispatch_trace_snapshot(self) -> dict:
        """Read-only snapshot of the ephemeral dispatch trace (RAM only, bounded).
        Raw ids are returned here; the export layer pseudonymizes them.  Counts are
        per zone; per-cover retarget state is summarised."""
        zones = {
            zid: {"records": list(ring), "count": len(ring)}
            for zid, ring in self._dispatch_trace.items()
        }
        covers = {
            cid: {
                "last_actual_position_ha": st.get("last_actual_position_ha"),
                "same_cover_retarget_count": st.get("same_cover_retarget_count", 0),
                "commands_last_5m": sum(
                    1 for t in st.get("recent_at", ()) if (self._dispatch_now() - t)
                    <= timedelta(minutes=5)),
                "commands_last_30m": sum(
                    1 for t in st.get("recent_at", ()) if (self._dispatch_now() - t)
                    <= timedelta(minutes=30)),
            }
            for cid, st in self._cover_dispatch_state.items()
        }
        return {"zones": zones, "covers": covers}

    def _dispatch_now(self) -> datetime:
        return dt_util.utcnow()

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

    def _experiment_stage_handoff(
        self, *, zone_id, window_id, intensity, context_family, reason, now,
    ):
        """Record a staged-experiment block and hand the zone slot to Strategy
        Learning (privacy-safe diagnostics only — no adaptive authority).

        Called when a position experiment cannot proceed safely (e.g. the
        next-stronger shade level leaves too little room for a material,
        monotonicity-preserving close-more step).  The zone slot is simply left
        free: with no position experiment armed, the existing strategy-experiment
        path can use it on a later cycle.
        """
        self._experiment_stage_block[window_id] = {
            "reason": reason, "intensity": intensity,
            "context_family": context_family, "at": now.isoformat(),
            "handed_to_strategy": True,
        }
        self._experiment_stage_handoff_count = (
            getattr(self, "_experiment_stage_handoff_count", 0) + 1)

    def _experiment_try_inject(
        self, *, zone, window, window_id, wdi, eff_ha, cfg_ha, exposure_wm2,
        outdoor_temp, in_solar_sector, manual_pref_active, current_state,
        night_contact_blocked: bool = False, now,
    ):
        """Plan/arm + (single) inject a bounded experiment parameter.  Returns wdi
        (possibly with one intensity position overridden).  Fully gated; never
        bypasses a higher authority."""
        self._cycle_experiment.pop(window_id, None)
        zone_id = window.zone_id
        exec_cfg = self.effective_zone_execution(zone_id)
        # Central authority: real experiments require ADAPTIVE mode (Learning
        # Mode + Active Control).  There is no separate experiments control.
        if not exec_cfg.authority.experiments_allowed:
            return wdi
        # P10 acceptance fix: no new position experiment while the position
        # consumed-ledger namespace is unsafe.
        if not self._ledger_namespace_safe(_LEDGER_POSITION):
            return wdi

        # Unified zone-experiment authority: a P9B strategy experiment also holds
        # the single per-zone slot — position and strategy never experiment together.
        if zone_id in self._strategy_experiments_active:
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

        # P8 experiment-need gate: a stable adoption suppresses needless repeat
        # experiments; a maxed (-10) adoption blocks all further close_more tests.
        if not self._experiment_need_allows(window_id, intensity, proposal, now):
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

        # 3H staged step: a NEW experiment escalates per the bounded staged
        # contract (Stage 1 = 5pp; Stage 2 = 10pp TOTAL vs the authoritative base
        # only after a complete, attributable, non-confounded, non-degraded Stage 1
        # on a later distinct day; never a Stage 3).  A running experiment keeps
        # its stamped step.  The config-base cumulative cap (10pp) is enforced in
        # revalidate, so a larger step can never exceed the total deviation bound.
        if exp is None:
            _terminal_for_key = [
                e for e in self._experiment_history
                if e.experiment_key == (window_id, intensity, ctx_family)
            ]
            stage_dec = evaluate_stage_escalation(
                terminal_experiments_for_key=_terminal_for_key, now=now)
            step_ha = stage_dec.target_step_ha
        else:
            stage_dec = None
            step_ha = exp.target_step_ha

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
            step_ha=step_ha,
        )

        # 3H monotonicity + min spacing: a close-more candidate must never cross
        # (or come within the minimum spacing of) the next-stronger configured
        # shade level — strong < normal < light must be preserved and neighbour
        # intensities are never silently shifted.  ``strong`` has no stronger
        # neighbour.  If the remaining room is too small for a material step, the
        # position experiment is blocked, diagnosed, and the zone slot is left for
        # Strategy Learning.
        if reval.valid and reval.experiment_parameter_target_ha is not None:
            _stronger = {"light": eff_ha.get("normal"),
                         "normal": eff_ha.get("strong")}.get(intensity)
            _floor, _was = enforce_monotonic_spacing(
                intensity_level=intensity,
                candidate_ha=reval.experiment_parameter_target_ha,
                stronger_neighbor_ha=_stronger)
            if _was:
                if abs(cur_auth_ha - _floor) < EXPERIMENT_MATERIALITY_HA:
                    self._experiment_stage_handoff(
                        zone_id=zone_id, window_id=window_id, intensity=intensity,
                        context_family=ctx_family,
                        reason="monotonic_spacing_insufficient", now=now)
                    if exp is not None:
                        self._abort_zone_experiment(
                            zone_id, "monotonic_spacing_insufficient", now)
                    return wdi
                _final = reval.expected_final_candidate_target_ha
                _final = _floor if _final is None else max(_final, _floor)
                reval = replace(
                    reval, experiment_parameter_target_ha=_floor,
                    expected_final_candidate_target_ha=_final,
                    cumulative_delta_from_config_ha=(cfg_base_ha - _final))

        elig = evaluate_experiment_eligibility(ExperimentEligibilityInput(
            intensity_level=intensity,
            learning_enabled=exec_cfg.learning_enabled,
            active_control_enabled=exec_cfg.active_control_enabled,
            shadow_status=proposal.status, proposal_present=True,
            p5_reference_valid=window_id in self._contribution_models,
            contribution_current=contrib_exp_elig,
            attribution_quality=proposal.attribution_quality,
            config_generation_matches=(proposal.config_generation == gen),
            thermal_available=tmodel is not None,
            # The zone-level ThermalResponseModel has no `.active` attribute
            # (only the per-context sub-model does).  A zone model is mature/usable
            # exactly when it has an effective observation window — the same
            # definition the diagnostics use for thermal_model_active.
            thermal_mature=bool(tmodel and tmodel.effective_observation_minutes is not None),
            thermal_reliability=(tmodel.confidence if tmodel else 0.0),
            temperature_source_available=reading.available,
            preference_veto=proposal.evaluation.preference_veto,
            manual_preference_active=manual_pref_active,
            fully_automatic=getattr(window.behavior_mode, "name", "") == "FULLY_AUTOMATIC",
            manual_override_active=self._override_detector.get(window_id, now) is not None,
            safety_active=current_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE, ShadingState.RAIN_SAFE),
            lifecycle_active=self._lifecycle_state.value != "day",
            presence_absence_transition=False,
            solar_context_ok=((exposure_wm2 or 0.0) >= 150.0
                              and ctx_family == proposal.context_family),
            reliable_position_feedback=reliable_fb,
            confounded=False,
            candidate_valid=reval.valid,
            other_active_zone_experiment=False,
            # Cooldown is a CREATION gate only: it must prevent arming a NEW
            # experiment, but must never retroactively abort an already-running
            # one (which set its own activation cooldown — a self-abort that
            # otherwise makes every experiment incapable of completing).  For an
            # existing experiment, continuation validity is governed by the other
            # gates (safety / override / feedback / candidate / config).
            cooldown_active=(
                self._experiment_cooldown(zone_id, proposal.proposal_key, now)[0]
                if exp is None else False),
            night_contact_blocked=night_contact_blocked,
        ))
        if not elig.eligible:
            # Lost eligibility while an experiment was armed for this window →
            # invalidate/abort logically (no command).  Genuine continuation
            # blockers (safety/override/feedback/config) still abort here.
            if exp is not None:
                self._abort_zone_experiment(
                    zone_id, f"ineligible:{elig.block_reason}", now)
            return wdi

        # Continuation: an experiment that is activated (just dispatched) or
        # observing (running with an open pending outcome) must run to its
        # outcome.  Do NOT re-arm or re-inject — that would revert it to ARMED,
        # cause a second dispatch and orphan the pending outcome so it never
        # finalizes.  Only an ARMED experiment (still waiting for the decision to
        # land on its intensity) is (re-)armed below.
        if exp is not None and exp.status in (STATUS_ACTIVATED, STATUS_OBSERVING):
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
                # 3H staged lineage (stamped from the escalation decision).
                stage=stage_dec.stage, target_step_ha=stage_dec.target_step_ha,
                previous_experiment_id=stage_dec.previous_experiment_id,
                previous_stage_evaluation=stage_dec.previous_stage_evaluation,
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
        # P10: experiment activation is an important event (not tied to a later
        # outcome) → schedule a coalesced near-immediate save.
        self._request_important_save()

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
        self._mark_learning_dirty()
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
        self._mark_learning_dirty()

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
        open_more = bool(mo and mo.preference.override_direction == "open_more")
        reliability = (mo.reliability.thermal if mo else 0.0)

        # Causal same-cycle baseline (3H): collect ONLY comparable, regular
        # (non-experiment) outcomes from the SAME context family.  The current
        # experiment is excluded (by decision_id) and so are all other experiment
        # outcomes (by their experiment_decision_ids) — an experiment must never
        # be part of its own / any experiment's counterfactual baseline.  A thin
        # context yields inconclusive in evaluate_experiment_causal (no fallback
        # to a favourable window-wide median).
        exp_ctx = exp.context_family
        _exp_decision_ids = {
            e.experiment_decision_id
            for e in (list(self._experiment_history)
                      + list(self._experiments_active.values()))
            if e.experiment_decision_id is not None
        }
        baseline_abs_deltas: list[float] = []
        baseline_solars: list[float] = []
        baseline_days: set = set()
        for o in self._learning_store.get_outcomes(exp.window_id):
            if o.window_id != exp.window_id or o.decision_id == did:
                continue
            if o.decision_id in _exp_decision_ids:
                continue  # never use an experiment outcome as a baseline
            omo = o.multi_objective
            if omo is None or not omo.thermal.available or omo.thermal.score is None:
                continue
            if (omo.thermal.temperature_delta is None
                    or omo.thermal.solar_exposure_at_decision is None):
                continue
            o_ctx = self._experiment_context_family(
                o.decision_timestamp, omo.thermal.outdoor_temp_at_decision,
                omo.thermal.solar_exposure_at_decision)
            if o_ctx != exp_ctx:
                continue
            baseline_abs_deltas.append(abs(omo.thermal.temperature_delta))
            baseline_solars.append(omo.thermal.solar_exposure_at_decision)
            baseline_days.add(o.decision_timestamp.date())

        evaluation = evaluate_experiment_causal(CausalSameCycleInput(
            experiment_outcome_available=exp_thermal is not None,
            observed_experiment_delta_c=(mo.thermal.temperature_delta if mo else None),
            observed_solar_wm2=(mo.thermal.solar_exposure_at_decision if mo else None),
            outdoor_temp_c=(mo.thermal.outdoor_temp_at_decision if mo else None),
            baseline_open_fraction=(
                exp.baseline_parameter_target_ha / 100.0
                if exp.baseline_parameter_target_ha is not None else None),
            experiment_open_fraction=(
                exp.expected_final_candidate_target_ha / 100.0
                if exp.expected_final_candidate_target_ha is not None else None),
            baseline_abs_deltas=tuple(baseline_abs_deltas[-30:]),
            baseline_solars=tuple(baseline_solars[-30:]),
            baseline_distinct_days=len(baseline_days),
            reliability=reliability,
            user_open_more_rejection=open_more,
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
        self._mark_learning_dirty()
        # P8: a freshly completed experiment is new evidence — re-evaluate whether
        # a persistent adoption can be created or upgraded for this (window,intensity).
        try:
            self._maybe_adopt(
                completed.window_id, completed.intensity_level, completed.zone_id,
                outcome.decision_timestamp or completed.updated_at)
        except Exception:
            _LOGGER.warning("Learning: adoption evaluation failed (non-fatal)")

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
        # P10: experiment abort/interruption is an important event.
        self._request_important_save()

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

    def _experiment_staged_diag(self, obj, window_id: str) -> dict:
        """Privacy-safe 3H staged + causal fields for one experiment record.

        Surfaces the bounded-staged lineage and the causal same-cycle scores so
        the observed/counterfactual scores, gap, context quality, confidence,
        stage, adoption stage, lineage, tested step and any block reason are all
        traceable in diagnostics and exports."""
        if obj is None:
            blk = self._experiment_stage_block.get(window_id)
            return {
                "experiment_stage": None, "tested_step_ha": None,
                "lineage_previous_experiment_id": None,
                "previous_stage_evaluation": None,
                "observed_thermal_score": None, "counterfactual_baseline_score": None,
                "score_gap": None, "baseline_scope": None,
                "context_baseline_sample_count": None,
                "context_baseline_distinct_days": None,
                "evaluation_confidence": None,
                "stage_block_reason": (blk or {}).get("reason"),
                "stage_handoff_to_strategy": bool((blk or {}).get("handed_to_strategy")),
                "adoption_stage": self._adoption_stage_for(window_id, None),
            }
        ev = obj.evaluation
        dist = (ev.baseline_thermal_distribution or {}) if ev else {}
        obs = ev.experiment_thermal_score if ev else None
        cf = dist.get("counterfactual_baseline_score")
        gap = (round(obs - cf, 4) if obs is not None and cf is not None else None)
        blk = self._experiment_stage_block.get(window_id)
        return {
            "experiment_stage": obj.stage, "tested_step_ha": obj.target_step_ha,
            "lineage_previous_experiment_id": obj.previous_experiment_id,
            "previous_stage_evaluation": obj.previous_stage_evaluation,
            "observed_thermal_score": obs,
            "counterfactual_baseline_score": cf,
            "score_gap": gap,
            "baseline_scope": dist.get("scope"),
            "context_baseline_sample_count": (ev.baseline_sample_count if ev else None),
            "context_baseline_distinct_days": (ev.baseline_distinct_days if ev else None),
            "evaluation_confidence": (ev.confidence if ev else None),
            "stage_block_reason": (blk or {}).get("reason"),
            "stage_handoff_to_strategy": bool((blk or {}).get("handed_to_strategy")),
            "adoption_stage": self._adoption_stage_for(window_id, obj.intensity_level),
        }

    def _adoption_stage_for(self, window_id: str, intensity: str | None) -> int | None:
        """Current active-adoption stage for (window, intensity), if any."""
        if intensity is None:
            for (wid, _int), a in self._adoptions_active.items():
                if wid == window_id:
                    return getattr(a, "stage", None)
            return None
        a = self._adoptions_active.get((window_id, intensity))
        return getattr(a, "stage", None) if a is not None else None

    def experiment_diagnostics(self, window_id: str) -> dict:
        """Privacy-safe per-window experiment diagnostics.  p8_adoption_eligible
        is a snapshot only; P8 must re-derive from current data."""
        window = self.windows.get(window_id)
        zone_id = window.zone_id if window is not None else ""
        exec_cfg = self.effective_zone_execution(zone_id) if zone_id else ZoneExecutionConfig()
        gate = None
        if not exec_cfg.authority.experiments_allowed:
            gate = (
                "learning_mode_required"
                if not exec_cfg.learning_enabled
                else "active_control_required"
            )
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
                **self._experiment_staged_diag(latest, window_id),
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
            **self._experiment_staged_diag(exp, window_id),
        }

    def _experiments_storage(self) -> list:
        out = [e.to_dict() for e in self._experiments_active.values()]
        out.extend(e.to_dict() for e in self._retain_terminal_history(self._experiment_history))
        return out

    # ------------------------------------------------------------------
    # P8 — Persistent adoption lifecycle (real, bounded, monitored)
    # ------------------------------------------------------------------

    def _adoption_consumed_ids(self, key: tuple) -> set:
        """Permanent consumed-experiment ledger for one (window,intensity) — never
        released, including after rollback/reduction or history pruning."""
        ids: set = set()
        a = self._adoptions_active.get(key)
        if a is not None:
            ids.update(a.consumed_experiment_ids)
        for h in self._adoption_history:
            if h.adoption_key == key:
                ids.update(h.consumed_experiment_ids)
        ids.update(self._consumed_ledger.consumed_ids(_LEDGER_POSITION))
        return ids

    def _adoption_experiment_evidence(self, window_id: str, intensity: str) -> list:
        """Fresh ExperimentEvidence list from terminal valid P7 experiments."""
        out: list = []
        for e in self._experiment_history:
            if e.window_id != window_id or e.intensity_level != intensity:
                continue
            if not e.evaluation.experiment_outcome_available or e.completed_at is None:
                continue
            out.append(ExperimentEvidence(
                experiment_id=e.experiment_id, decision_class=e.evaluation.decision,
                day=e.completed_at.date(), reliability=e.evaluation.reliability,
                confidence=e.evaluation.confidence, context_family=e.context_family,
                config_generation=e.config_generation,
                decision_id=e.experiment_decision_id, shadow_id=e.source_shadow_id,
                target_step_ha=e.target_step_ha,
            ))
        return out

    @staticmethod
    def _intensity_manual_pref(eff_ha: dict, cfg_ha: dict, intensity: str) -> bool:
        """Per-intensity manual preference = TargetPositionAdapter changed exactly
        this intensity's position (eff differs from the post-config base)."""
        return eff_ha.get(intensity) != cfg_ha.get(intensity)

    def _adoption_context_compatible(self, adoption, ctx_family: str) -> bool:
        fams = set(adoption.validated_context_families) | {adoption.context_family}
        return ctx_family in fams

    def _adoption_cooldown_for(self, key: tuple):
        latest = None
        for h in self._adoption_history:
            if h.adoption_key == key and h.cooldown_until is not None:
                if latest is None or h.cooldown_until > latest:
                    latest = h.cooldown_until
        return latest

    def _adoption_apply(self, *, zone, window, window_id, wdi, eff_ha, cfg_ha,
                        exposure_wm2, outdoor_temp, now):
        """Inject persistent adoptions (per intensity) as a Tier-5 parameter below
        manual preference and above the P7 experiment.  Mutates eff_ha in place so
        the experiment tests against the adopted base.  Applied only when context
        is compatible and no per-intensity manual preference is present."""
        self._cycle_adoption_applied.pop(window_id, None)
        zone_id = window.zone_id
        exec_cfg = self.effective_zone_execution(zone_id)
        ctx_family = self._experiment_context_family(now, outdoor_temp, exposure_wm2)
        gen = self._thermal_config_generation(zone_id)
        applied: dict = {}
        for intensity in ("light", "normal", "strong"):
            key = (window_id, intensity)
            a = self._adoptions_active.get(key)
            if a is None or a.adopted_delta_ha == 0:
                continue
            # P10 acceptance closure: while the position ledger namespace is unsafe,
            # a restored adoption stays PERMANENTLY suspended — never re-activated,
            # no learned delta reaches effective behaviour.  Rechecked every cycle;
            # consumed evidence + history are preserved.
            if not self._ledger_namespace_safe(_LEDGER_POSITION):
                self._adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason="ledger_integrity_unsafe",
                    updated_at=now)
                continue
            # Per-intensity manual preference always wins → do not apply.
            if self._intensity_manual_pref(eff_ha, cfg_ha, intensity):
                self._adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason="manual_preference_active",
                    updated_at=now)
                continue
            # Applicability: current context compatible + current config generation.
            if a.config_generation != gen:
                self._adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason="config_generation_changed",
                    updated_at=now)
                continue
            if not self._adoption_context_compatible(a, ctx_family):
                self._adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason="context_incompatible",
                    updated_at=now)
                continue
            base = a.configured_target_ha
            if base is None:
                continue
            effective = clamp_position(base - abs(a.adopted_delta_ha))
            # Inject into exactly this intensity position.
            eb = wdi.effective_behavior
            internal = to_internal_position(effective)
            if intensity == "light":
                eb = replace(eb, light_shade_position=internal)
            elif intensity == "normal":
                eb = replace(eb, normal_shade_position=internal)
            else:
                eb = replace(eb, strong_shade_position=internal)
            wdi = replace(wdi, effective_behavior=eb)
            eff_ha[intensity] = effective  # experiment will test -5 from here
            control_applied = exec_cfg.active_control_enabled
            applied[intensity] = (a.adoption_id, control_applied, base, effective)
            self._adoptions_active[key] = replace(
                a, suspended=False, current_gate_reason=None,
                effective_target_ha=effective, last_validated_at=now,
                status=(ADOPT_STATUS_MONITORING if a.status == ADOPT_STATUS_ADOPTED else a.status),
                updated_at=now)
        if applied:
            self._cycle_adoption_applied[window_id] = applied
        return wdi

    def _experiment_need_allows(self, window_id, intensity, proposal, now) -> bool:
        """P8 experiment-need gate for P7 planning/activation."""
        key = (window_id, intensity)
        a = self._adoptions_active.get(key)
        if a is None:
            allowed, _ = evaluate_experiment_need(ExperimentNeedInput(
                has_adoption=False, stage=0, status="none", confirmed_stable=False,
                activated_at=None, repeated_underprotection=False,
                new_independent_supported_evidence=True, revalidation_required=False, now=now))
            return allowed
        confirmed_stable = a.status == ADOPT_STATUS_CONFIRMED
        repeated_under = a.monitoring.degraded_count >= 1
        # "New independent evidence" = a supported proposal exists AND at least one
        # terminal experiment for this (window,intensity) is not yet consumed.
        consumed = self._adoption_consumed_ids(key)
        ev = self._adoption_experiment_evidence(window_id, intensity)
        has_unconsumed = any(e.experiment_id not in consumed for e in ev)
        new_evidence = proposal is not None and has_unconsumed
        allowed, _ = evaluate_experiment_need(ExperimentNeedInput(
            has_adoption=True, stage=a.stage, status=a.status,
            confirmed_stable=confirmed_stable, activated_at=a.activated_at,
            repeated_underprotection=repeated_under,
            new_independent_supported_evidence=new_evidence,
            revalidation_required=False, now=now))
        return allowed

    def _maybe_adopt(self, window_id: str, intensity: str, zone_id: str, now: datetime) -> None:
        """Create a first -5 pp adoption, or upgrade a confirmed/stable one to
        -10 pp, strictly from multiple fresh, exact, non-consumed P7 experiments."""
        # P10 acceptance fix: never activate adaptive authority while the position
        # consumed-ledger namespace is unsafe (consumed evidence integrity unknown).
        if not self._ledger_namespace_safe(_LEDGER_POSITION):
            return
        key = (window_id, intensity)
        existing = self._adoptions_active.get(key)
        # Cooldown after a prior rollback/rejection for this identity.
        cd = self._adoption_cooldown_for(key)
        if existing is None and cd is not None and _adoption_cooldown_active(cd, now):
            return

        if existing is None:
            stage = 1
        elif (existing.stage == 1 and existing.status == ADOPT_STATUS_CONFIRMED
              and existing.activated_at is not None
              and (now - existing.activated_at) >= timedelta(days=S2_STABILITY_DAYS)):
            stage = 2
        else:
            return  # stage-1 not yet confirmed/stable, or already stage-2

        gen = self._thermal_config_generation(zone_id)
        consumed = self._adoption_consumed_ids(key)
        evidence = self._adoption_experiment_evidence(window_id, intensity)
        # 3H: a deeper adoption must rest only on experiments that tested at least
        # the corresponding close-more magnitude (-5 → step≥5; -10 → step≥10), so
        # a -10 adoption can never be satisfied by -5-only evidence.
        _min_step = ADOPTION_STEP_HA if stage == 1 else 2 * ADOPTION_STEP_HA
        res = evaluate_adoption_evidence(
            evidence, stage=stage, consumed_ids=frozenset(consumed),
            config_generation=gen, min_experiment_step_ha=_min_step)
        if not res.sufficient:
            return

        # Configured base: for stage 1 use the latest experiment baseline (= config
        # when no adoption existed); for stage 2 keep the existing configured base.
        if existing is not None:
            configured = existing.configured_target_ha
        else:
            base_exp = None
            for e in self._experiment_history:
                if (e.window_id == window_id and e.intensity_level == intensity
                        and e.baseline_parameter_target_ha is not None):
                    if base_exp is None or (e.completed_at or e.updated_at) > (base_exp.completed_at or base_exp.updated_at):
                        base_exp = e
            configured = base_exp.baseline_parameter_target_ha if base_exp is not None else None
        if configured is None:
            return
        delta = -(ADOPTION_STEP_HA if stage == 1 else 2 * ADOPTION_STEP_HA)
        effective = clamp_position(configured + delta)
        if configured - effective < ADOPTION_MATERIALITY_HA:
            return

        window = self.windows.get(window_id)
        cg = self.cover_groups.get(window.cover_group_id) if window is not None else None
        cap = (self._get_or_detect_capability(cg.cover_ids[0])
               if cg is not None and cg.cover_ids else None)
        reliable_fb = bool(cap.has_reliable_position_feedback) if cap is not None else False
        tmodel = self._thermal_models.get(zone_id)
        contrib = self._contribution_models.get(window_id)
        _se, contrib_exp_elig = derive_eligibility(contrib, gen)
        exec_cfg = self.effective_zone_execution(zone_id)
        # Conservative window-level manual-preference check at creation (precise
        # per-intensity gating happens at apply time).
        try:
            _mp = self._target_position_adapter.get_adaptation_diagnostics(
                window_id, (self._adaptive_profiles.get(window_id).confidence_level
                            if self._adaptive_profiles.get(window_id) else "very_low"))
            mp_active = bool(_mp.get("target_adaptation_active", False))
        except Exception:
            mp_active = False

        elig = evaluate_adoption_eligibility(AdoptionEligibilityInput(
            intensity_level=intensity, learning_enabled=exec_cfg.learning_enabled,
            active_control_required_now=False,
            active_control_enabled=exec_cfg.active_control_enabled,
            fully_automatic=getattr(window.behavior_mode, "name", "") == "FULLY_AUTOMATIC"
            if window is not None else False,
            manual_preference_active=mp_active, manual_override_active=False,
            safety_active=False, lifecycle_active=False, presence_absence_transition=False,
            config_generation_matches=True, contribution_current=contrib_exp_elig,
            attribution_quality=(ATTR_WINDOW_ISOLATED if contrib is not None else "unknown"),
            thermal_available=tmodel is not None,
            thermal_reliability=(tmodel.confidence if tmodel else 0.0),
            p6_p7_reference_present=True, reliable_position_feedback=reliable_fb,
            context_compatible=True, confounded=False,
            candidate_material_and_safe=True, evidence_sufficient=True, cooldown_active=False,
        ))
        if not elig.eligible:
            return

        new_consumed = tuple(sorted(set(consumed) | set(res.selected_experiment_ids)))
        fams = tuple(sorted(set(res.validated_context_families)
                            | (set(existing.validated_context_families) if existing else set())))
        primary_ctx = (existing.context_family if existing is not None
                       else (res.validated_context_families[0] if res.validated_context_families else "global"))
        src_exp = tuple(sorted(set(res.selected_experiment_ids)
                               | (set(existing.source_experiment_ids) if existing else set())))
        adoption = PersistentTargetAdoption(
            adoption_id=(existing.adoption_id if existing is not None else uuid.uuid4().hex),
            window_id=window_id, zone_id=zone_id, intensity_level=intensity,
            context_family=primary_ctx, validated_context_families=fams,
            configured_target_ha=configured, adopted_delta_ha=delta,
            effective_target_ha=effective, source_experiment_ids=src_exp,
            consumed_experiment_ids=new_consumed,
            created_at=(existing.created_at if existing is not None else now), updated_at=now,
            activated_at=(existing.activated_at if existing is not None else now),
            stage2_activated_at=(now if stage == 2 else None),
            last_validated_at=now, config_generation=gen, status=ADOPT_STATUS_ADOPTED,
            confidence=res.confidence, reliability=res.reliability,
            distinct_experiment_days=res.distinct_days,
            successful_experiment_count=res.improved_count + res.no_degradation_count,
            no_degradation_count=res.no_degradation_count,
            # Fresh monitoring phase begins for each newly activated stage.
            monitoring=AdoptionMonitoringState(monitoring_started_at=now),
        )
        self._adoptions_active[key] = adoption
        for _eid in res.selected_experiment_ids:
            _ce = next((e for e in self._experiment_history if e.experiment_id == _eid), None)
            self._consumed_ledger.record(
                _LEDGER_POSITION, _eid, _ce.created_at if _ce is not None else now)
        # P10: adoption activation + consumed-ledger mutation are important events.
        self._request_important_save()

    def _monitor_adoption(self, outcome) -> None:
        """Update monitoring for an active adoption from a production outcome and
        apply confirm/suspend/reduce/rollback/invalidate (robust evidence only)."""
        intensity = self._SHADE_INTENSITY.get(outcome.decided_state)
        if intensity is None:
            return
        key = (outcome.window_id, intensity)
        a = self._adoptions_active.get(key)
        if a is None or a.adopted_delta_ha == 0:
            return
        now = outcome.decision_timestamp or dt_util.utcnow()
        mo = outcome.multi_objective
        thermal_available = bool(mo and mo.thermal.available)
        thermal_score = mo.thermal.score if mo else None
        open_more = bool(mo and mo.preference.override_direction == "open_more")
        confounded = bool(mo and getattr(mo.reliability, "thermal_confounded", False))
        cls = classify_monitoring_outcome(
            thermal_available=thermal_available, thermal_score=thermal_score,
            confounded=confounded, open_more_rejection=open_more)
        new_mon = update_monitoring(
            a.monitoring, outcome_class=cls, open_more_rejection=open_more,
            day=now.date(), now=now)
        a = replace(a, monitoring=new_mon, updated_at=now,
                    degraded_count=new_mon.degraded_count,
                    preference_rejection_count=new_mon.preference_rejection_count)
        self._adoptions_active[key] = a

        exec_cfg = self.effective_zone_execution(a.zone_id)
        gen = self._thermal_config_generation(a.zone_id)
        action, reason = evaluate_monitoring_action(MonitoringActionInput(
            stage=a.stage, learning_enabled=exec_cfg.learning_enabled,
            config_generation_matches=(a.config_generation == gen), reference_valid=True,
            context_compatible=True, sensor_available=thermal_available,
            open_more_rejection_now=open_more, monitoring=new_mon))

        if action == ACTION_FULL_ROLLBACK:
            self._adoptions_active.pop(key, None)
            self._adoption_to_history(replace(
                a, status=ADOPT_STATUS_ROLLED_BACK, rollback_reason=reason,
                suspended=False, cooldown_until=rollback_cooldown_until(now), updated_at=now))
        elif action == ACTION_INVALIDATE:
            self._adoptions_active.pop(key, None)
            self._adoption_to_history(replace(
                a, status=ADOPT_STATUS_INVALIDATED, rollback_reason=reason, updated_at=now))
        elif action == ACTION_REDUCE_ONE_STEP and a.stage == 2:
            self._adoptions_active[key] = replace(
                a, adopted_delta_ha=-ADOPTION_STEP_HA,
                effective_target_ha=clamp_position(a.configured_target_ha - ADOPTION_STEP_HA),
                status=ADOPT_STATUS_REDUCED, rollback_reason=reason,
                monitoring=AdoptionMonitoringState(monitoring_started_at=now), updated_at=now)
        elif action == ACTION_TEMPORARY_SUSPEND:
            self._adoptions_active[key] = replace(
                a, suspended=True, current_gate_reason=reason, updated_at=now)
        else:  # retain → maybe confirm
            activated = a.stage2_activated_at if a.stage == 2 else a.activated_at
            if evaluate_confirmation(stage=a.stage, activated_at=activated,
                                     monitoring=new_mon, now=now):
                self._adoptions_active[key] = replace(a, status=ADOPT_STATUS_CONFIRMED, updated_at=now)
        self._mark_learning_dirty()

    def _suspend_zone_adoptions(self, zone_id: str, reason: str, now: datetime) -> None:
        for key, a in list(self._adoptions_active.items()):
            if a.zone_id == zone_id and not a.suspended:
                self._adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason=reason, updated_at=now)

    def _invalidate_unreferenced_adoptions(self, now: datetime) -> None:
        """P10 reference integrity: drop any restored adoption whose source/consumed
        experiment evidence is unresolvable (hard reference) → move to history as
        invalidated, never apply.  Provenance-only gaps (e.g. a missing shadow
        tombstone) are NOT a reason to invalidate.  Reason code from the validator."""
        entry_id = self.config_entry.entry_id
        # Hard source-experiment resolution: EVERY required source_experiment_id must
        # resolve to a restored experiment (active or terminal history).  The consumed
        # ledger only proves prior consumption — it is NOT accepted as resolution.
        pos_resolvable = {
            e.experiment_id for e in self._experiment_history
        } | {e.experiment_id for e in self._experiments_active.values()}
        strat_resolvable = {
            e.experiment_id for e in self._strategy_experiment_history
        } | {e.experiment_id for e in self._strategy_experiments_active.values()}
        pos = _validate_adoptions(
            list(self._adoptions_active.values()),
            owner_entry_id=entry_id, current_entry_id=entry_id,
            resolvable_experiment_ids=pos_resolvable)
        for key, a in list(self._adoptions_active.items()):
            if a.adoption_id in pos.invalid_ids:
                self._adoptions_active.pop(key, None)
                self._adoption_to_history(replace(
                    a, status=ADOPT_STATUS_INVALIDATED,
                    rollback_reason=f"reference:{pos.reason_codes.get(a.adoption_id, 'invalid')}",
                    updated_at=now))
        strat = _validate_adoptions(
            list(self._strategy_adoptions_active.values()),
            owner_entry_id=entry_id, current_entry_id=entry_id,
            resolvable_experiment_ids=strat_resolvable)
        for key, a in list(self._strategy_adoptions_active.items()):
            if a.adoption_id in strat.invalid_ids:
                self._strategy_adoptions_active.pop(key, None)
                self._strategy_adoption_to_history(replace(
                    a, status=STRAT_AD_INVALIDATED,
                    rollback_reason=f"reference:{strat.reason_codes.get(a.adoption_id, 'invalid')}",
                    updated_at=now))

    def _adoption_to_history(self, adoption) -> None:
        self._adoption_history.append(adoption)
        per_key: dict = {}
        for h in self._adoption_history:
            per_key.setdefault(h.adoption_key, []).append(h)
        trimmed: list = []
        for _k, items in per_key.items():
            trimmed.extend(items[-ADOPTION_HISTORY_PER_WINDOW:])
        trimmed.sort(key=lambda h: (h.updated_at or h.created_at))
        self._adoption_history = trimmed

    def adoption_diagnostics(self, window_id: str) -> dict:
        """Privacy-safe per-window adoption diagnostics (one entry per intensity
        with an active adoption; plus the latest terminal otherwise)."""
        window = self.windows.get(window_id)
        zone_id = window.zone_id if window is not None else ""
        exec_cfg = self.effective_zone_execution(zone_id) if zone_id else ZoneExecutionConfig()
        gate = None
        if not exec_cfg.learning_enabled:
            gate = "learning_mode_required"
        out: dict = {"learning_mode_gate": gate, "intensities": {}}
        for intensity in ("light", "normal", "strong"):
            a = self._adoptions_active.get((window_id, intensity))
            if a is None:
                continue
            out["intensities"][intensity] = {
                "adoption_status": a.status, "adoption_id": a.adoption_id,
                "adopted_delta_ha": a.adopted_delta_ha,
                "effective_adopted_target_ha": a.effective_target_ha,
                "source_experiment_count": len(a.source_experiment_ids),
                "source_experiment_days": a.distinct_experiment_days,
                "adoption_confidence": round(a.confidence, 3),
                "adoption_reliability": round(a.reliability, 3),
                "monitoring_outcome_count": a.monitoring.outcome_count,
                "monitoring_degraded_count": a.monitoring.degraded_count,
                "preference_rejection_count": a.preference_rejection_count,
                "current_gate_reason": a.current_gate_reason, "suspended": a.suspended,
                "rollback_reason": a.rollback_reason,
                "cooldown_remaining_days": (
                    round((a.cooldown_until - dt_util.utcnow()).total_seconds() / 86400.0, 1)
                    if a.cooldown_until is not None else None),
            }
        if not out["intensities"]:
            out["adoption_status"] = "none"
        return out

    def _adoptions_storage(self) -> list:
        out = [a.to_dict() for a in self._adoptions_active.values()]
        out.extend(a.to_dict() for a in self._retain_terminal_history(self._adoption_history))
        return out

    # ------------------------------------------------------------------
    # P9A — Strategy foundation (observe / recommend / diagnostics)
    # ------------------------------------------------------------------

    def _strategy_observe(self, *, window, window_id, wdi, exposure_wm2, in_solar_sector,
                          current_state, outdoor_temp, solar_resolution, now) -> None:
        """Compute a non-authoritative ShadingStrategyCandidate for diagnostics."""
        eb = wdi.effective_behavior
        ctx = self._experiment_context_family(now, outdoor_temp, exposure_wm2)
        tmodel = self._thermal_models.get(window.zone_id)
        trust_level = getattr(solar_resolution, "forecast_trust_level", "forecast_unavailable")
        fc = ForecastLoadFeatures(
            available=(trust_level != "forecast_unavailable"), trust_level=trust_level)
        state_name = current_state.value if hasattr(current_state, "value") else str(current_state)
        cand = resolve_strategy(StrategyResolverInput(
            window_id=window_id, zone_id=window.zone_id, context_family=ctx,
            current_state=state_name, in_solar_sector=in_solar_sector,
            measured_exposure_wm2=exposure_wm2,
            light_threshold_wm2=eb.light_shade_threshold_wm2,
            normal_threshold_wm2=eb.normal_shade_threshold_wm2,
            strong_threshold_wm2=eb.strong_shade_threshold_wm2,
            forecast=fc,
            confidence=(tmodel.confidence if tmodel else 0.0),
            reliability=(tmodel.confidence if tmodel else 0.0)))
        self._strategy_candidates[window_id] = cand

    def _classify_outcome_insufficiency(self, outcome) -> None:
        """Best-effort thermal-insufficiency cause for diagnostics (observe only)."""
        mo = outcome.multi_objective
        if mo is None or not mo.thermal.available or not mo.thermal.insufficient_response:
            return
        contrib = self._contribution_models.get(outcome.window_id)
        attribution = (ATTR_WINDOW_ISOLATED if contrib is not None else "unknown")
        cause, follow_up = classify_thermal_insufficiency(InsufficiencyInput(
            thermal_available=True, confounded=bool(getattr(mo.reliability, "thermal_confounded", False)),
            shade_was_active=True, insufficient_response=True, attribution_quality=attribution,
            onset_reached=bool(mo.thermal.response_onset_detected),
            shade_was_timely=True, at_max_intensity=(outcome.decided_state == ShadingState.STRONG_SHADE),
            load_duration_long=False, outdoor_or_internal_dominant=False))
        self._last_thermal_cause[outcome.window_id] = (cause, follow_up)

    def strategy_diagnostics(self, window_id: str) -> dict:
        """Privacy-safe per-window strategy candidate snapshot (observe/recommend)."""
        cand = self._strategy_candidates.get(window_id)
        if cand is None:
            return {"strategy_available": False}
        d = cand.to_dict()
        d["strategy_available"] = True
        cause = self._last_thermal_cause.get(window_id)
        if cause is not None:
            d["thermal_insufficiency_cause"] = cause[0]
            d["thermal_insufficiency_follow_up"] = cause[1]
        return d

    def solar_threshold_diagnostics(self, window_id: str) -> dict:
        res = self._cycle_solar_resolution.get(window_id)
        return res.to_dict() if res is not None else {"available": False}

    def tier_order_diagnostics(self, window_id: str) -> dict:
        proj = self._cycle_tier_order.get(window_id)
        return proj.to_dict() if proj is not None else {"projected": False}

    # ------------------------------------------------------------------
    # P9B — Bounded strategy learning (threshold family live; all modeled)
    # ------------------------------------------------------------------

    def _zone_experiment_locked(self, zone_id: str) -> bool:
        """Unified zone-experiment authority: at most one active experiment per
        zone across P7 position experiments AND P9B strategy experiments."""
        return zone_id in self._experiments_active or zone_id in self._strategy_experiments_active

    def _strategy_consumed_ids(self, key: tuple) -> set:
        ids: set = set()
        a = self._strategy_adoptions_active.get(key)
        if a is not None:
            ids.update(a.consumed_experiment_ids)
        for h in self._strategy_adoption_history:
            if h.adoption_key == key:
                ids.update(h.consumed_experiment_ids)
        ids.update(self._consumed_ledger.consumed_ids(_LEDGER_STRATEGY))
        return ids

    def _strategy_adoption_to_history(self, adoption) -> None:
        self._strategy_adoption_history.append(adoption)
        per_key: dict = {}
        for h in self._strategy_adoption_history:
            per_key.setdefault(h.adoption_key, []).append(h)
        trimmed: list = []
        for _k, items in per_key.items():
            trimmed.extend(items[-STRAT_ADOPTION_HISTORY_PER_KEY:])
        trimmed.sort(key=lambda h: (h.updated_at or h.created_at))
        self._strategy_adoption_history = trimmed

    def _strategy_context_compatible(self, adoption, ctx_family: str) -> bool:
        fams = set(adoption.validated_context_families) | {adoption.context_family}
        return ctx_family in fams

    def _strategy_threshold_delta(self, window_id, exposure, outdoor, now) -> float:
        """Live runtime effect of an active ENTRY_THRESHOLD strategy adoption
        (bounded, single-clamp via the Unified Solar Threshold Resolver).  Returns
        0.0 unless applicable (learning on, current generation, compatible
        context, not suspended)."""
        key = (window_id, FAMILY_ENTRY_THRESHOLD)
        a = self._strategy_adoptions_active.get(key)
        if a is None or a.adopted_delta == 0:
            return 0.0
        window = self.windows.get(window_id)
        if window is None:
            return 0.0
        zone_id = window.zone_id
        exec_cfg = self.effective_zone_execution(zone_id)
        if not exec_cfg.learning_enabled:
            self._strategy_adoptions_active[key] = replace(
                a, suspended=True, current_gate_reason="learning_mode_off", updated_at=now)
            return 0.0
        gen = self._thermal_config_generation(zone_id)
        if a.config_generation != gen:
            self._strategy_adoptions_active[key] = replace(
                a, suspended=True, current_gate_reason="config_generation_changed", updated_at=now)
            return 0.0
        ctx = self._experiment_context_family(now, outdoor, exposure)
        if not self._strategy_context_compatible(a, ctx):
            self._strategy_adoptions_active[key] = replace(
                a, suspended=True, current_gate_reason="context_incompatible", updated_at=now)
            return 0.0
        if a.suspended or a.status == STRAT_AD_MONITORING:
            self._strategy_adoptions_active[key] = replace(
                a, suspended=False, current_gate_reason=None,
                status=(STRAT_AD_MONITORING if a.status in ("adopted", STRAT_AD_MONITORING) else a.status),
                last_validated_at=now, updated_at=now)
        return float(a.adopted_delta)

    def _strategy_experiment_evidence(self, window_id: str, family: str) -> list:
        out: list = []
        for e in self._strategy_experiment_history:
            if e.window_id != window_id or e.parameter_family != family:
                continue
            if e.completed_at is None or e.evaluation_class == "inconclusive":
                continue
            sign = 1 if e.delta > 0 else (-1 if e.delta < 0 else 0)
            out.append(StrategyEvidence(
                experiment_id=e.experiment_id, decision_class=e.evaluation_class,
                day=e.completed_at.date(), reliability=e.reliability, confidence=e.confidence,
                context_family=e.context_family, config_generation=e.config_generation,
                direction_sign=sign))
        return out

    def _maybe_adopt_strategy(self, window_id: str, family: str, zone_id: str, now: datetime) -> None:
        """Create/upgrade a persistent strategy adoption from multiple fresh, exact,
        non-consumed terminal strategy experiments (mirror of P8, generalized)."""
        # P10 acceptance fix: never activate adaptive authority while the strategy
        # consumed-ledger namespace is unsafe (consumed evidence integrity unknown).
        if not self._ledger_namespace_safe(_LEDGER_STRATEGY):
            return
        key = (window_id, family)
        existing = self._strategy_adoptions_active.get(key)
        # cooldown after rollback for this identity
        for h in self._strategy_adoption_history:
            if (h.adoption_key == key and h.cooldown_until is not None
                    and existing is None and _strategy_cooldown_active(h.cooldown_until, now)):
                return
        bounds = STRAT_FAMILY_BOUNDS.get(family)
        if bounds is None:
            return
        if existing is None:
            stage = 1
        elif (existing.status == STRAT_AD_CONFIRMED and existing.activated_at is not None
              and (now - existing.activated_at) >= timedelta(days=14)
              and abs(existing.adopted_delta) < bounds.cap - 1e-9):
            stage = 2
        else:
            return
        gen = self._thermal_config_generation(zone_id)
        consumed = self._strategy_consumed_ids(key)
        evidence = self._strategy_experiment_evidence(window_id, family)
        res = evaluate_strategy_evidence(
            evidence, stage=stage, consumed_ids=frozenset(consumed), config_generation=gen)
        if not res.sufficient:
            return
        exec_cfg = self.effective_zone_execution(zone_id)
        if not exec_cfg.learning_enabled:
            return
        # Bounded new cumulative delta in the evidence direction.
        prev_delta = existing.adopted_delta if existing is not None else 0.0
        new_delta = prev_delta + res.direction_sign * bounds.step
        if abs(new_delta) - bounds.cap > 1e-9:
            return
        # Representative baseline (configured value); 0.0 keeps the delta as the
        # authoritative bounded value for threshold families.
        baseline = existing.baseline_value if existing is not None else 0.0
        new_consumed = tuple(sorted(set(consumed) | set(res.selected_experiment_ids)))
        fams = tuple(sorted(set(res.validated_context_families)
                            | (set(existing.validated_context_families) if existing else set())))
        adoption = PersistentStrategyAdoption(
            adoption_id=(existing.adoption_id if existing is not None else uuid.uuid4().hex),
            zone_id=zone_id, window_id=window_id, parameter_family=family,
            context_family=(existing.context_family if existing is not None
                            else (res.validated_context_families[0] if res.validated_context_families else "global")),
            validated_context_families=fams, baseline_value=baseline, adopted_delta=new_delta,
            effective_value=baseline + new_delta,
            source_experiment_ids=tuple(sorted(set(res.selected_experiment_ids)
                                               | (set(existing.source_experiment_ids) if existing else set()))),
            consumed_experiment_ids=new_consumed,
            created_at=(existing.created_at if existing is not None else now), updated_at=now,
            activated_at=(existing.activated_at if existing is not None else now),
            stage2_activated_at=(now if stage == 2 else None), last_validated_at=now,
            config_generation=gen, status="adopted", confidence=res.confidence,
            reliability=res.reliability, distinct_experiment_days=res.distinct_days,
            monitoring=StrategyMonitoringState(monitoring_started_at=now))
        self._strategy_adoptions_active[key] = adoption
        # P10: permanently record consumed strategy-experiment ids (never reusable).
        for _eid in res.selected_experiment_ids:
            _ce = next((e for e in self._strategy_experiment_history
                        if e.experiment_id == _eid), None)
            self._consumed_ledger.record(
                _LEDGER_STRATEGY, _eid, _ce.created_at if _ce is not None else now)
        # P10: strategy adoption activation + consumed-ledger mutation are important.
        self._request_important_save()

    def _monitor_strategy_adoption(self, outcome) -> None:
        """Continuous monitoring of active strategy adoptions from production
        outcomes (mirror of P8; robust negative evidence only)."""
        mo = outcome.multi_objective
        if mo is None:
            return
        now = outcome.decision_timestamp or dt_util.utcnow()
        # Honest credit: only adoptions whose family actually influenced this
        # decision (recorded at decision time) may receive a monitoring outcome.
        applied_fams = self._strategy_applied_by_decision.pop(outcome.decision_id, set()) \
            if outcome.decision_id is not None else set()
        for key, a in list(self._strategy_adoptions_active.items()):
            if key[0] != outcome.window_id or a.adopted_delta == 0:
                continue
            if a.parameter_family not in applied_fams:
                continue  # adoption had no real effect this cycle → no credit
            open_more = bool(mo.preference.override_direction == "open_more")
            confounded = bool(getattr(mo.reliability, "thermal_confounded", False))
            cls = classify_strategy_outcome(
                thermal_available=bool(mo.thermal.available), thermal_score=mo.thermal.score,
                confounded=confounded, open_more_rejection=open_more)
            new_mon = update_strategy_monitoring(
                a.monitoring, outcome_class=cls, open_more_rejection=open_more,
                moved=False, day=now.date(), now=now)
            a = replace(a, monitoring=new_mon, updated_at=now)
            self._strategy_adoptions_active[key] = a
            exec_cfg = self.effective_zone_execution(a.zone_id)
            gen = self._thermal_config_generation(a.zone_id)
            action, reason = evaluate_strategy_monitoring_action(StrategyMonitoringActionInput(
                stage=a.stage, learning_enabled=exec_cfg.learning_enabled,
                config_generation_matches=(a.config_generation == gen), reference_valid=True,
                context_compatible=True, sensor_available=bool(mo.thermal.available),
                forecast_trust_ok=True, open_more_rejection_now=open_more, monitoring=new_mon))
            if action == STRAT_ACTION_FULL_ROLLBACK:
                self._strategy_adoptions_active.pop(key, None)
                self._strategy_adoption_to_history(replace(
                    a, status=STRAT_AD_ROLLED_BACK, rollback_reason=reason, suspended=False,
                    cooldown_until=_strategy_rollback_cooldown_until(now), updated_at=now))
            elif action == STRAT_ACTION_INVALIDATE:
                self._strategy_adoptions_active.pop(key, None)
                self._strategy_adoption_to_history(replace(
                    a, status=STRAT_AD_INVALIDATED, rollback_reason=reason, updated_at=now))
            elif action == STRAT_ACTION_REDUCE_ONE_STEP and a.stage == 2:
                bounds = STRAT_FAMILY_BOUNDS.get(a.parameter_family)
                step = bounds.step if bounds else 0
                reduced = a.adopted_delta - (step if a.adopted_delta > 0 else -step)
                self._strategy_adoptions_active[key] = replace(
                    a, adopted_delta=reduced, effective_value=a.baseline_value + reduced,
                    status=STRAT_AD_REDUCED, rollback_reason=reason,
                    monitoring=StrategyMonitoringState(monitoring_started_at=now), updated_at=now)
            elif action == STRAT_ACTION_TEMPORARY_SUSPEND:
                self._strategy_adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason=reason, updated_at=now)
            else:
                activated = a.stage2_activated_at if a.stage == 2 else a.activated_at
                if evaluate_strategy_confirmation(stage=a.stage, activated_at=activated,
                                                  monitoring=new_mon, now=now):
                    self._strategy_adoptions_active[key] = replace(
                        a, status=STRAT_AD_CONFIRMED, updated_at=now)
            self._mark_learning_dirty()

    def _suspend_zone_strategy(self, zone_id: str, reason: str, now: datetime) -> None:
        for key, a in list(self._strategy_adoptions_active.items()):
            if a.zone_id == zone_id and not a.suspended:
                self._strategy_adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason=reason, updated_at=now)

    def apply_config_change_invalidation(
        self, zone_id: str, change_type: str, now: datetime
    ) -> tuple:
        """P10: differentiated config-change invalidation with REAL runtime effect.

        Uses the typed invalidation matrix (engines/config_invalidation) to decide
        per scope whether learned position/strategy authority for *zone_id* is
        suspended or invalidated.  The generic config_generation gate remains the
        final safety net; this is the precise, reason-coded layer above it.  Marks
        learning dirty and requests a near-immediate save.  Returns the applied
        (scope, action, reason) directives for diagnostics/tests."""
        plan = _classify_config_change(change_type)
        applied: list = []
        for d in plan.directives:
            if d.scope == _CI_SCOPE_POSITION and d.action in (_CI_SUSPEND, _CI_INVALIDATE):
                if d.action == _CI_INVALIDATE:
                    self._abort_zone_experiment(zone_id, f"config:{d.reason}", now)
                self._suspend_zone_adoptions(zone_id, d.reason, now)
                applied.append((d.scope, d.action, d.reason))
            elif d.scope == _CI_SCOPE_STRATEGY and d.action in (_CI_SUSPEND, _CI_INVALIDATE):
                self._suspend_zone_strategy(zone_id, d.reason, now)
                applied.append((d.scope, d.action, d.reason))
        if applied:
            self._request_important_save()
        return tuple(applied)

    def _ledger_namespace_safe(self, namespace: str) -> bool:
        """P10 acceptance fix: adaptive authority for a namespace is permitted only
        while its consumed-ledger integrity is safe (valid or legitimately missing).
        Corruption/unsupported/owner-mismatch ⇒ unsafe ⇒ no evidence-consuming
        adaptive authority (deterministic baseline control is unaffected)."""
        return self._ledger_integrity.is_safe(namespace)

    def _enforce_ledger_integrity(self, now: datetime) -> int:
        """Suspend restored adaptive authority for any UNSAFE ledger namespace.

        Position-namespace unsafe ⇒ suspend all position adoptions + abort active
        position experiments.  Strategy-namespace unsafe ⇒ suspend all strategy
        adoptions + abort active strategy experiments.  Consumed evidence is never
        released; baseline control stays."""
        blocked = 0
        if not self._ledger_namespace_safe(_LEDGER_POSITION):
            for key, a in list(self._adoptions_active.items()):
                if not getattr(a, "suspended", False):
                    self._adoptions_active[key] = replace(
                        a, suspended=True,
                        current_gate_reason="ledger_integrity_unsafe", updated_at=now)
                    blocked += 1
            for zid, exp in list(self._experiments_active.items()):
                self._abort_zone_experiment(zid, "ledger_integrity_unsafe", now)
                blocked += 1
        if not self._ledger_namespace_safe(_LEDGER_STRATEGY):
            for key, a in list(self._strategy_adoptions_active.items()):
                if not getattr(a, "suspended", False):
                    self._strategy_adoptions_active[key] = replace(
                        a, suspended=True,
                        current_gate_reason="ledger_integrity_unsafe", updated_at=now)
                    blocked += 1
            # P10 acceptance recheck: a restored/active strategy experiment must be
            # durably aborted (terminal in history) so it cannot apply a delta next
            # cycle.  Consumed evidence stays consumed; no outcome credit.
            for zid, exp in list(self._strategy_experiments_active.items()):
                self._strategy_experiments_active.pop(zid, None)
                self._strategy_experiment_history.append(replace(
                    exp, status=STRAT_EXP_ABORTED, abort_reason="ledger_integrity_unsafe",
                    rollback_state="logical", updated_at=now, completed_at=now))
                blocked += 1
        if blocked:
            self._mark_learning_dirty()
            self._request_important_save()
        return blocked

    def _apply_config_diff_on_restore(self, prev_snapshot: dict, now: datetime) -> list:
        """P10: typed config-change invalidation across a restart/reload.

        Diffs the persisted PREVIOUS normalised config snapshot against the current
        one and routes each real change through the matrix (geometry/cover/sensor/
        forecast/behaviour/targets) or direct cleanup (window removal).  First setup
        (no previous snapshot) and an unchanged restart produce zero changes, so
        neither fabricates an invalidation.  The config_generation gate remains the
        final safety net.  Returns the applied ConfigChange list (for diagnostics)."""
        if not prev_snapshot:
            return []
        # P10: never compute typed changes from a corrupt snapshot — that could
        # fake an orientation/cover/sensor change.  The config_generation gate
        # remains the safety net; record structured diagnostics.
        from .engines.restore_validation import validate_config_snapshot
        ok, reasons = validate_config_snapshot(prev_snapshot)
        if not ok:
            self._restore_diagnostics = dict(self._restore_diagnostics or {})
            self._restore_diagnostics.setdefault("invalid_records_by_section", {})
            self._restore_diagnostics["invalid_records_by_section"]["config_snapshot"] = (
                sum(reasons.values()))
            return []
        current = self._build_config_snapshot()
        changes = _diff_config_snapshots(prev_snapshot, current)
        for ch in changes:
            if ch.change_type == _CI_CHANGE_WINDOW_REMOVAL:
                self._cleanup_removed_window(ch.window_id, now)
                continue
            self.apply_config_change_invalidation(ch.zone_id, ch.change_type, now)
            # Geometry / cover replacement / feedback loss also interrupt the single
            # active experiment of that window's zone (inconclusive, logical rollback).
            if ch.change_type in (
                _CI_CHANGE_ORIENTATION, _CI_CHANGE_COVER, _CI_CHANGE_FEEDBACK_LOSS,
            ) and ch.zone_id:
                self._abort_zone_experiment(
                    ch.zone_id, f"config:{ch.change_type}", now)
        if changes:
            self._request_important_save()
        return changes

    def _cleanup_removed_window(self, window_id: str, now: datetime) -> None:
        """P10: a removed window leaves no orphan runtime/persisted authority.

        Active adoptions/shadows for the window go to terminal history (invalidated);
        the zone's active experiment for this window is aborted; the pending outcome
        is dropped.  Consumed-ledger evidence stays protected (never reusable)."""
        if not window_id:
            return
        for key, a in list(self._adoptions_active.items()):
            if getattr(a, "window_id", None) == window_id:
                self._adoptions_active.pop(key, None)
                self._adoption_to_history(replace(
                    a, status=ADOPT_STATUS_INVALIDATED,
                    rollback_reason="window_removed", updated_at=now))
        for key, a in list(self._strategy_adoptions_active.items()):
            if getattr(a, "window_id", None) == window_id:
                self._strategy_adoptions_active.pop(key, None)
                self._strategy_adoption_to_history(replace(
                    a, status=STRAT_AD_INVALIDATED,
                    rollback_reason="window_removed", updated_at=now))
        for key in [k for k, p in self._shadow_active.items()
                    if getattr(p, "window_id", None) == window_id]:
            self._shadow_active.pop(key, None)
        for zid, exp in list(self._experiments_active.items()):
            if getattr(exp, "window_id", None) == window_id:
                self._abort_zone_experiment(zid, "window_removed", now)
        try:
            self._pending_outcomes.remove(window_id)
        except Exception:
            pass
        self._request_important_save()

    def strategy_adoption_diagnostics(self, window_id: str) -> dict:
        """Privacy-safe per-window strategy adoption snapshot."""
        window = self.windows.get(window_id)
        zone_id = window.zone_id if window is not None else ""
        exec_cfg = self.effective_zone_execution(zone_id) if zone_id else ZoneExecutionConfig()
        out: dict = {
            "learning_mode_gate": (None if exec_cfg.learning_enabled else "learning_mode_required"),
            "families": {},
        }
        for key, a in self._strategy_adoptions_active.items():
            if key[0] != window_id:
                continue
            out["families"][a.parameter_family] = {
                "adoption_status": a.status, "adoption_id": a.adoption_id,
                "parameter_family": a.parameter_family, "adopted_delta": a.adopted_delta,
                "effective_value": a.effective_value, "confidence": round(a.confidence, 3),
                "reliability": round(a.reliability, 3),
                "monitoring_count": a.monitoring.outcome_count,
                "degraded_count": a.monitoring.degraded_count,
                "preference_rejection_count": a.monitoring.preference_rejection_count,
                "suspended": a.suspended, "current_gate_reason": a.current_gate_reason,
                "rollback_reason": a.rollback_reason,
                "cooldown_remaining_days": (
                    round((a.cooldown_until - dt_util.utcnow()).total_seconds() / 86400.0, 1)
                    if a.cooldown_until is not None else None),
            }
        if not out["families"]:
            out["strategy_status"] = "none"
        return out

    def _strategy_active_delta(self, window_id, family, exposure, outdoor, now) -> tuple[float, bool]:
        """Effective bounded delta for one family from the active adoption (+ any
        active strategy experiment for this window/family), with fresh runtime
        gating (learning / generation / context / suspend).  Returns (delta, applied)."""
        delta = 0.0
        applied = False
        window = self.windows.get(window_id)
        if window is None:
            return (0.0, False)
        zone_id = window.zone_id
        exec_cfg = self.effective_zone_execution(zone_id)
        gen = self._thermal_config_generation(zone_id)
        key = (window_id, family)
        a = self._strategy_adoptions_active.get(key)
        # P10 acceptance recheck: the strategy-ledger gate guards the ENTIRE strategy
        # delta authority — BEFORE both the adoption AND the experiment path.  While
        # the strategy namespace is unsafe NO strategy delta is produced (adoption or
        # experiment), the total delta is exactly 0 and applied stays False; any
        # restored adoption is kept suspended.  Rechecked every cycle.
        if not self._ledger_namespace_safe(_LEDGER_STRATEGY):
            if a is not None and not a.suspended:
                self._strategy_adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason="ledger_integrity_unsafe",
                    updated_at=now)
            return (0.0, False)
        if a is not None and a.adopted_delta != 0:
            ctx = self._experiment_context_family(now, outdoor, exposure)
            if not exec_cfg.learning_enabled:
                self._strategy_adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason="learning_mode_off", updated_at=now)
            elif a.config_generation != gen:
                self._strategy_adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason="config_generation_changed", updated_at=now)
            elif not self._strategy_context_compatible(a, ctx):
                self._strategy_adoptions_active[key] = replace(
                    a, suspended=True, current_gate_reason="context_incompatible", updated_at=now)
            else:
                if a.suspended:
                    self._strategy_adoptions_active[key] = replace(
                        a, suspended=False, current_gate_reason=None,
                        status=STRAT_AD_MONITORING if a.status == "adopted" else a.status,
                        last_validated_at=now, updated_at=now)
                delta += float(a.adopted_delta)
                applied = True
        # Active strategy experiment for this window/family (single bounded step).
        exp = self._strategy_experiments_active.get(zone_id)
        if (exp is not None and exp.window_id == window_id and exp.parameter_family == family
                and exec_cfg.learning_enabled and exp.config_generation == gen):
            delta += float(exp.delta)
            applied = True
        return (delta, applied)

    def _strategy_runtime_apply(self, *, window, window_id, wdi, tier_decision,
                                current_state, exposure, outdoor, now):
        """Apply bounded strategy families (exit/hysteresis → tier-choice →
        entry-timing → exit-timing) to a COMFORT-tier decision.  No-op for
        safety/lifecycle/override/absence states (higher authority).  Returns the
        (possibly) modified tier_decision; records applied families for honest
        monitoring credit + provenance."""
        comfort = (ShadingState.OPEN, ShadingState.LIGHT_SHADE,
                   ShadingState.NORMAL_SHADE, ShadingState.STRONG_SHADE)
        if tier_decision.shading_state not in comfort:
            return tier_decision
        eb = wdi.effective_behavior
        applied: dict = {}
        state = tier_decision.shading_state
        ts = self._strategy_timing_state.setdefault(window_id, TimingState())

        # 1. EXIT_THRESHOLD + HYSTERESIS — value-based de-escalation hold.
        exit_delta, exit_thr_applied = self._strategy_active_delta(
            window_id, FAMILY_EXIT_THRESHOLD, exposure, outdoor, now)
        hyst_delta, hyst_applied = self._strategy_active_delta(
            window_id, FAMILY_HYSTERESIS, exposure, outdoor, now)
        if (exit_thr_applied or hyst_applied) and current_state in (
                ShadingState.LIGHT_SHADE, ShadingState.NORMAL_SHADE, ShadingState.STRONG_SHADE):
            _entry_for_cur = {
                ShadingState.LIGHT_SHADE: eb.light_shade_threshold_wm2,
                ShadingState.NORMAL_SHADE: eb.normal_shade_threshold_wm2,
                ShadingState.STRONG_SHADE: eb.strong_shade_threshold_wm2,
            }[current_state]
            cur_exit = effective_exit_threshold(
                _entry_for_cur, hysteresis_steps=hyst_delta,
                exit_threshold_delta_wm2=exit_delta)
            new_state, held = apply_deescalation_hysteresis(
                current_state=current_state, proposed_state=state,
                exposure_wm2=exposure, current_tier_exit_threshold_wm2=cur_exit)
            if held:
                state = new_state
                if exit_thr_applied:
                    applied[FAMILY_EXIT_THRESHOLD] = True
                if hyst_applied:
                    applied[FAMILY_HYSTERESIS] = True

        # 2. TIER_CHOICE — bounded ±1 tier shift among valid tiers.
        tc_delta, tc_applied = self._strategy_active_delta(
            window_id, FAMILY_TIER_CHOICE, exposure, outdoor, now)
        if tc_applied and int(tc_delta) != 0:
            shifted, changed = apply_tier_choice(state, tier_delta=int(tc_delta))
            if changed:
                state = shifted
                applied[FAMILY_TIER_CHOICE] = True

        # 3. ENTRY_TIMING — bounded transition-time gate.
        et_delta, et_applied = self._strategy_active_delta(
            window_id, FAMILY_ENTRY_TIMING, exposure, outdoor, now)
        if et_applied and et_delta != 0:
            shifted, changed = apply_entry_timing(
                current_state=current_state, proposed_state=state, now=now, state=ts,
                delta_min=et_delta, forecast_lead_minutes=None)
            if changed:
                state = shifted
                applied[FAMILY_ENTRY_TIMING] = True

        # 4. EXIT_TIMING — bounded release-time gate.
        xt_delta, xt_applied = self._strategy_active_delta(
            window_id, FAMILY_EXIT_TIMING, exposure, outdoor, now)
        if xt_applied and xt_delta != 0:
            shifted, changed = apply_exit_timing(
                current_state=current_state, proposed_state=state, now=now, state=ts,
                delta_min=xt_delta)
            if changed:
                state = shifted
                applied[FAMILY_EXIT_TIMING] = True

        if applied and state != tier_decision.shading_state:
            _pos = {
                ShadingState.LIGHT_SHADE: eb.light_shade_position,
                ShadingState.NORMAL_SHADE: eb.normal_shade_position,
                ShadingState.STRONG_SHADE: eb.strong_shade_position,
                ShadingState.OPEN: 0,  # internal 0 = fully open
            }.get(state, tier_decision.target_position)
            tier_decision = replace(
                tier_decision, shading_state=state, target_position=_pos,
                decided_by="StrategyRuntime")
        if applied:
            self._cycle_strategy_applied[window_id] = applied
        return tier_decision

    def _strategy_min_hold_extra(self, window_id, exposure, outdoor, now):
        """Bounded MINIMUM_HOLD delta as a timedelta for StateGuard (floored)."""
        delta, applied = self._strategy_active_delta(
            window_id, FAMILY_MINIMUM_HOLD, exposure, outdoor, now)
        if not applied or delta == 0:
            return timedelta(0), False
        return timedelta(minutes=delta), True

    def _strategy_experiments_storage(self) -> list:
        out = [e.to_dict() for e in self._strategy_experiments_active.values()]
        out.extend(e.to_dict() for e in self._retain_terminal_history(self._strategy_experiment_history))
        return out

    def _strategy_adoptions_storage(self) -> list:
        out = [a.to_dict() for a in self._strategy_adoptions_active.values()]
        out.extend(a.to_dict() for a in self._retain_terminal_history(self._strategy_adoption_history))
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
        out.extend(p.to_dict() for p in self._retain_terminal_history(self._shadow_history))
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
            # Real current outdoor temperature at finalization (descriptive
            # metadata; recompute_model does not use the start→end delta).
            _outdoor_now = self._read_value(self._outdoor_temperature_sensor_id, "temperature")
            obs = ThermalResponseObservation(
                zone_id=zone_id,
                decision_ids=tuple(sorted(acc["decision_ids"])),
                started_at=acc["started_at"], ended_at=now,
                observation_duration_min=(now - acc["started_at"]).total_seconds() / 60.0,
                indoor_start=acc["indoor_start"], indoor_end=outcome.indoor_temp_outcome_c,
                indoor_samples=tuple(acc["samples"]),
                outdoor_start=acc["outdoor_start"],
                outdoor_end=(_outdoor_now if _outdoor_now is not None
                             else acc["outdoor_start"]),
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
            self._mark_learning_dirty()
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
        # P10: upsert a compact provenance tombstone for this shadow (durable even
        # if the full proposal is later pruned).  Never creates runtime authority.
        self._upsert_shadow_tombstone(
            _TOMB_POSITION, shadow_id=proposal.shadow_id, window_id=wid,
            parameter_family=intensity, context_family=context_family,
            config_generation=gen,
            confidence=getattr(evaluation, "confidence", 0.0) or 0.0,
            reliability=attr.attribution_quality if isinstance(attr.attribution_quality, (int, float)) else 0.0,
            terminal_status=status, now=now)

    def _upsert_shadow_tombstone(
        self, kind: str, *, shadow_id: str, window_id: str, parameter_family: str,
        context_family: str, config_generation: int, confidence: float,
        reliability: float, terminal_status: str, now: datetime,
    ) -> None:
        """Create/refresh a compact tombstone (separate position/strategy namespace
        via *kind*).  No full shadow payload; bounded by _prune_shadow_tombstones."""
        if not shadow_id:
            return
        tkey = (kind, shadow_id)
        existing = self._shadow_tombstones.get(tkey)
        created = existing.created_at if existing is not None else now
        self._shadow_tombstones[tkey] = ShadowTombstone(
            shadow_id=shadow_id, kind=kind, window_id=window_id,
            parameter_family=parameter_family, context_family=context_family,
            config_generation=config_generation, created_at=created,
            expires_at=created + timedelta(days=_TOMB_AGE_DAYS),
            confidence=float(confidence), reliability=float(reliability),
            terminal_status=terminal_status)

    def _referenced_shadow_ids(self) -> set:
        """Shadow ids that an active experiment/adoption still references → their
        tombstones must never be pruned (provenance must stay resolvable)."""
        refs: set = set()
        for a in self._adoptions_active.values():
            refs.update(getattr(a, "source_shadow_ids", ()) or ())
        for a in self._strategy_adoptions_active.values():
            refs.update(getattr(a, "source_shadow_ids", ()) or ())
        for p in self._shadow_active.values():
            sid = getattr(p, "shadow_id", None)
            if sid:
                refs.add(sid)
        return refs

    def _prune_shadow_tombstones(self, now: datetime, max_count: int = 1000) -> None:
        """Age + count bound the tombstone collection; referenced tombstones are
        always retained even past the age/count limits."""
        refs = self._referenced_shadow_ids()
        kept: dict = {}
        droppable: list = []
        for tkey, t in self._shadow_tombstones.items():
            if t.shadow_id in refs:
                kept[tkey] = t  # referenced → protected
            elif t.expires_at is not None and t.expires_at <= now:
                continue        # unreferenced + expired → prune
            else:
                droppable.append((tkey, t))
        # Count cap on the surviving unreferenced set (newest kept).
        droppable.sort(key=lambda kt: kt[1].created_at or now)
        budget = max(0, max_count - len(kept))
        for tkey, t in droppable[-budget:] if budget else []:
            kept[tkey] = t
        self._shadow_tombstones = kept

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
        # Best-effort: a model error must never crash the cycle, but it must NOT
        # be silently invisible either (this is exactly how the 3A defect hid).
        # We record a privacy-safe health signal (exception class name only) and
        # skip this outcome's thermal step — no partial observation is written
        # because _thermal_finalize only mutates state after a successful build,
        # and the same outcome is not re-fed (resolution happens once).
        try:
            self._thermal_finalize(outcome)
        except Exception as _exc:
            self._thermal_finalize_failures += 1
            self._thermal_finalize_last_reason = type(_exc).__name__
            _LOGGER.warning(
                "Learning: thermal finalize failed (non-fatal) for %s — %s",
                outcome.window_id, type(_exc).__name__,
            )
        # P7: if this outcome belongs to an active experiment (exact decision_id
        # linkage), finalize and evaluate the experiment.
        try:
            self._experiment_finalize_from_outcome(outcome)
        except Exception:
            _LOGGER.warning("Learning: experiment finalize failed (non-fatal)")
        # P8: feed the production outcome into continuous adoption monitoring.
        try:
            self._monitor_adoption(outcome)
        except Exception:
            _LOGGER.warning("Learning: adoption monitoring failed (non-fatal)")
        # P9A: classify "still heating despite shading" cause (diagnostics only).
        try:
            self._classify_outcome_insufficiency(outcome)
        except Exception:
            _LOGGER.warning("Learning: thermal insufficiency classify failed (non-fatal)")
        # P9B: continuous monitoring of active strategy adoptions.
        try:
            self._monitor_strategy_adoption(outcome)
        except Exception:
            _LOGGER.warning("Learning: strategy adoption monitoring failed (non-fatal)")
        # P10 completion: a resolved outcome (and its experiment/adoption/monitoring
        # cascade) is an important event → schedule a coalesced near-immediate save.
        self._request_important_save()

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
                solar_threshold_resolution=(
                    _sr.to_dict() if (_sr := self._cycle_solar_resolution.get(window_id)) is not None
                    else None),
                tier_order_projected=bool(
                    (_to := self._cycle_tier_order.get(window_id)) is not None and _to.projected),
                tier_order_notes=(
                    tuple(_to.notes) if (_to := self._cycle_tier_order.get(window_id)) is not None
                    else ()),
                strategy_applied=bool(
                    (_sa := self._strategy_adoptions_active.get((window_id, FAMILY_ENTRY_THRESHOLD)))
                    is not None and not _sa.suspended and _sa.adopted_delta != 0),
                strategy_adoption_id=(
                    _sa.adoption_id if (_sa := self._strategy_adoptions_active.get(
                        (window_id, FAMILY_ENTRY_THRESHOLD))) is not None else None),
                strategy_parameter_family=(
                    _sa.parameter_family if (_sa := self._strategy_adoptions_active.get(
                        (window_id, FAMILY_ENTRY_THRESHOLD))) is not None else None),
                strategy_adopted_delta=(
                    _sa.adopted_delta if (_sa := self._strategy_adoptions_active.get(
                        (window_id, FAMILY_ENTRY_THRESHOLD))) is not None else None),
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
        # P9B: record which strategy families actually influenced THIS decision so
        # monitoring only credits an adoption that really had an effect.
        _applied_fams = set(self._cycle_strategy_applied.get(window_id, {}).keys())
        _et = self._strategy_adoptions_active.get((window_id, FAMILY_ENTRY_THRESHOLD))
        if _et is not None and not _et.suspended and _et.adopted_delta != 0:
            _applied_fams.add(FAMILY_ENTRY_THRESHOLD)
        if _applied_fams:
            self._strategy_applied_by_decision[decision_id] = _applied_fams
            if len(self._strategy_applied_by_decision) > 500:
                # bounded: drop oldest insertion
                _oldest = next(iter(self._strategy_applied_by_decision))
                self._strategy_applied_by_decision.pop(_oldest, None)
        self._last_decision_summaries[window_id] = candidate.to_summary()
        self._mark_learning_dirty()
