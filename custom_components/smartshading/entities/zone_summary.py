"""Zone-level sensor entities: Zone Summary, Learning Progress, and Shading Result.

Zone Summary Sensor (Step 9G10g):
  One entity per configured zone.  Aggregates window-level data from
  SmartShadingData to give a compact, human- and machine-readable view of the
  whole zone without duplicating the per-window Recommendation Sensor detail.

  State priority (highest wins):
    1. "safety"              — any window is in a Tier-1 safety state
    2. "override"            — any window has an active manual override
    3. "automatic"           — zone has active_control_enabled=True
    4. "recommendation_only" — zone has learning_enabled=True, active_control off
    5. "disabled"            — both observation and active_control are off

Learning Progress Sensor:
  One entity per configured zone.  Derives a 0–100 % progress value from the
  average confidence-dampened adaptation_strength across all windows in the zone.
  Gives users a simple, privacy-safe view of whether SmartShading is still
  collecting data or has accumulated enough to actively adapt recommendations.

  State: integer 0–100 (unit=%, MEASUREMENT)
  Attributes: status, windows_tracked, windows_learning_active, adaptation_active

  Progress formula:
    per window: effective_strength = min(adaptation_strength, confidence_cap)
    zone:       progress = round(avg(effective_strength) × 100)

  Confidence caps (_CONFIDENCE_LEVEL_CAPS):
    "very_low" / "low" → 0.00  (learning_ready gate already blocks these)
    "medium"           → 0.50  (G×S ∈ [0.40, 0.60) — growing data basis)
    "high"             → 0.75  (G×S ∈ [0.60, 0.80) — solid data basis)
    "very_high"        → 1.00  (G×S ≥ 0.80 — extensive history)

  This prevents inflated override/solar signal scores from overstating progress
  when global data volume (G = resolved_outcomes/50) is still low.
  "confident" (80–100 %) is only reachable once confidence is "very_high".

Shading Result Sensor (v1.0.0):
  One entity per zone.  Aggregates resolved DecisionOutcome records from the
  learning store to give a quality rating for the zone's shading decisions.

  States: "excellent" / "good" / "acceptable" / "poor" / "unknown"

  Classification uses the most recent resolved outcomes for each window in the
  zone.  A minimum of _MIN_RESOLVED_OUTCOMES across all windows is required
  before any state other than "unknown" is reported.

  Privacy-safe: attributes expose only aggregate counts and rates, never
  raw positions, entity IDs, sensor readings, or individual outcome records.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from ..coordinator import SmartShadingCoordinator
from ..engines.learning_store import LearningStore
from ..models.window import WindowBehaviorMode
from ..state_machine.states import ShadingState

# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def is_shading_learning_eligible(behavior_mode: WindowBehaviorMode) -> bool:
    """True only for windows that participate in adaptive solar/heat/glare shading.

    Only FULLY_AUTOMATIC windows produce Solar-, Heat-, and Glare-evaluator
    decisions that make sense as adaptive-shading quality metrics.
    ABSENCE_AND_SCHEDULE, ABSENCE_ONLY, and DISABLED_AUTOMATIC windows must
    never contribute to Shading Outcome or Learning Progress aggregation.
    """
    return behavior_mode == WindowBehaviorMode.FULLY_AUTOMATIC


# ---------------------------------------------------------------------------
# State string constants
# ---------------------------------------------------------------------------

ZONE_STATE_SAFETY: str = "safety"
ZONE_STATE_OVERRIDE: str = "override"
ZONE_STATE_AUTOMATIC: str = "automatic"
ZONE_STATE_RECOMMENDATION_ONLY: str = "recommendation_only"
ZONE_STATE_DISABLED: str = "disabled"

# ---------------------------------------------------------------------------
# Confidence level ordering for highest_confidence_level computation
# ---------------------------------------------------------------------------

_CONFIDENCE_RANK: dict[str, int] = {
    "very_low": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "very_high": 4,
}

_SAFETY_STATES: frozenset[ShadingState] = frozenset({
    ShadingState.STORM_SAFE,
    ShadingState.WIND_SAFE,
})


# ---------------------------------------------------------------------------
# Pure aggregation helper
# ---------------------------------------------------------------------------

def compute_zone_summary_attributes(
    zone_id: str,
    *,
    learning_enabled: bool,
    active_control_enabled: bool,
    window_ids: list[str],
    coordinator_data: Any,  # SmartShadingData | None
) -> dict[str, Any]:
    """Aggregate per-window coordinator data into zone-level attribute values.

    Returns a dict that maps directly to the sensor's extra_state_attributes.
    Designed to be callable from both the live entity and from pure-Python tests.

    When coordinator_data is None (coordinator not yet fetched), all count
    fields default to 0 / False / None.
    """
    totals = len(window_ids)

    if coordinator_data is None or totals == 0:
        return {
            "learning_enabled": learning_enabled,
            "active_control_enabled": active_control_enabled,
            "windows_total": totals,
            "windows_with_recommendation": 0,
            "windows_in_solar_sector": 0,
            "windows_with_override": 0,
            "windows_with_safety": 0,
            "startup_grace_active": False,
            "dispatch_throttled_last_cycle": False,
            "learning_active_count": 0,
            "highest_confidence_level": None,
            "windows_with_adaptation": 0,
        }

    window_results = coordinator_data.window_results       # dict[str, WindowObservation]
    diagnostics = coordinator_data.execution_diagnostics   # dict[str, WindowExecutionDiagnostics]
    profiles = coordinator_data.adaptive_profiles          # dict[str, AdaptiveProfile]

    n_recommendation = 0
    n_solar_sector = 0
    n_override = 0
    n_safety = 0
    startup_grace_any = False
    throttled_any = False
    n_learning = 0
    n_adaptation = 0
    best_confidence_rank: int | None = None

    for wid in window_ids:
        obs = window_results.get(wid)
        diag = diagnostics.get(wid)
        profile = profiles.get(wid)

        # windows_with_recommendation: any target_position_ha present this cycle
        if diag is not None and diag.target_position_ha is not None:
            n_recommendation += 1

        # windows_in_solar_sector: exposure geometry says window faces the sun
        if obs is not None and obs.exposure is not None and obs.exposure.is_in_tolerance_window:
            n_solar_sector += 1

        # windows_with_override: manual override is active for this window
        if obs is not None and obs.override_active:
            n_override += 1

        # windows_with_safety: diagnostics flag OR window state is safety tier
        if diag is not None and diag.is_safety:
            n_safety += 1
        elif obs is not None and obs.state in _SAFETY_STATES:
            n_safety += 1

        # startup_grace_active: at least one window suppressed due to startup grace
        if (
            diag is not None
            and diag.dispatch_suppressed_reason == "startup_grace_active"
        ):
            startup_grace_any = True

        # dispatch_throttled_last_cycle: any window's dispatch was throttled
        if diag is not None and diag.dispatch_throttled:
            throttled_any = True

        # learning / adaptation
        if profile is not None:
            if profile.learning_active:
                n_learning += 1
            if profile.adaptation_strength > 0:
                n_adaptation += 1
            rank = _CONFIDENCE_RANK.get(profile.confidence_level)
            if rank is not None:
                if best_confidence_rank is None or rank > best_confidence_rank:
                    best_confidence_rank = rank

    # Resolve best confidence rank back to string
    if best_confidence_rank is not None:
        highest_confidence = next(
            (k for k, v in _CONFIDENCE_RANK.items() if v == best_confidence_rank),
            None,
        )
    else:
        highest_confidence = None

    return {
        "learning_enabled": learning_enabled,
        "active_control_enabled": active_control_enabled,
        "windows_total": totals,
        "windows_with_recommendation": n_recommendation,
        "windows_in_solar_sector": n_solar_sector,
        "windows_with_override": n_override,
        "windows_with_safety": n_safety,
        "startup_grace_active": startup_grace_any,
        "dispatch_throttled_last_cycle": throttled_any,
        "learning_active_count": n_learning,
        "highest_confidence_level": highest_confidence,
        "windows_with_adaptation": n_adaptation,
    }


def compute_zone_summary_state(
    *,
    learning_enabled: bool,
    active_control_enabled: bool,
    window_ids: list[str],
    coordinator_data: Any,  # SmartShadingData | None
) -> str:
    """Derive the zone's summary state string.

    Priority (highest wins):
      1. safety            — any window in Tier-1 safety state
      2. override          — any window has active manual override
      3. automatic         — active_control_enabled=True (observation on/off)
      4. recommendation_only — learning_enabled=True, active_control off
      5. disabled           — both flags False
    """
    if coordinator_data is not None:
        window_results = coordinator_data.window_results
        diagnostics = coordinator_data.execution_diagnostics

        for wid in window_ids:
            diag = diagnostics.get(wid)
            obs = window_results.get(wid)

            if (diag is not None and diag.is_safety) or (
                obs is not None and obs.state in _SAFETY_STATES
            ):
                return ZONE_STATE_SAFETY

        for wid in window_ids:
            obs = window_results.get(wid)
            if obs is not None and obs.override_active:
                return ZONE_STATE_OVERRIDE

    # Active-control flag overrides the observation flag for state derivation.
    if active_control_enabled:
        return ZONE_STATE_AUTOMATIC

    if learning_enabled:
        return ZONE_STATE_RECOMMENDATION_ONLY

    return ZONE_STATE_DISABLED


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------

class SmartShadingZoneSummarySensor(CoordinatorEntity[SmartShadingCoordinator], SensorEntity):
    """Zone Summary Sensor — one entity per SmartShading zone.

    State: one of "safety" / "override" / "automatic" /
           "recommendation_only" / "disabled" (priority hierarchy).

    Attributes: aggregated counts and zone-level flags derived from the
    current coordinator cycle data.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: SmartShadingCoordinator,
        zone_id: str,
        zone_name: str,
        window_ids: list[str],
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._window_ids = window_ids

        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{zone_id}_zone_summary"
        )
        self._attr_translation_key = "zone_summary"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_{zone_id}")},
            name=zone_name,
        )

    @property
    def native_value(self) -> str:
        exec_cfg = self.coordinator.effective_zone_execution(self._zone_id)
        return compute_zone_summary_state(
            learning_enabled=exec_cfg.learning_enabled,
            active_control_enabled=exec_cfg.active_control_enabled,
            window_ids=self._window_ids,
            coordinator_data=self.coordinator.data,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        exec_cfg = self.coordinator.effective_zone_execution(self._zone_id)
        return compute_zone_summary_attributes(
            self._zone_id,
            learning_enabled=exec_cfg.learning_enabled,
            active_control_enabled=exec_cfg.active_control_enabled,
            window_ids=self._window_ids,
            coordinator_data=self.coordinator.data,
        )


# ---------------------------------------------------------------------------
# Learning Progress — pure aggregation helper
# ---------------------------------------------------------------------------

_LEARNING_STATUS_COLLECTING = "collecting"
_LEARNING_STATUS_LEARNING   = "learning"
_LEARNING_STATUS_ADAPTING   = "adapting"
_LEARNING_STATUS_CONFIDENT  = "confident"

# Per-confidence-level ceiling applied to adaptation_strength before averaging.
# Prevents high override/solar signal scores from overstating progress when the
# global data volume (G = resolved_outcomes/50) is still low.
# "very_low" and "low" are capped at 0.0 as a defensive backstop; in practice
# the learning_ready gate in adaptation_layer.py already sets adaptation_strength
# to 0.0 for those levels.
_CONFIDENCE_LEVEL_CAPS: dict[str, float] = {
    "very_low": 0.0,
    "low":      0.0,
    "medium":   0.50,
    "high":     0.75,
    "very_high": 1.00,
}

# ---------------------------------------------------------------------------
# Learning progress calibration — conservative caps based on dispatch telemetry
# ---------------------------------------------------------------------------

# Days of confirmed coordinator activity required before progress can reach its
# maximum.  Progress scales linearly: min(1.0, days_observed / _MIN_DAYS_FULL_PROGRESS).
_MIN_DAYS_FULL_PROGRESS: int = 60

# Physical-dispatch fraction threshold below which progress is capped at
# _SHADOW_ONLY_CAP.  Installations in recommendation-only mode accumulate
# observational data but cannot verify actual cover movements, so progress
# should not overstate learning quality.
_MIN_DISPATCH_FRACTION: float = 0.10

# Maximum progress when the physical dispatch fraction is below _MIN_DISPATCH_FRACTION.
_SHADOW_ONLY_CAP: float = 0.20


@dataclass
class _LearningProgressContext:
    """Calibration context derived from coordinator dispatch telemetry."""

    days_observed: int = 0
    dispatched_count: int = 0
    recommendation_only_count: int = 0


def _build_learning_context(coordinator: Any) -> _LearningProgressContext | None:
    """Build calibration context from a coordinator's research_daily_buckets.

    Returns None when the coordinator has no dispatch telemetry attribute
    (e.g. in isolated unit tests), leaving compute_learning_progress behaviour
    identical to before this calibration layer was added.
    """
    buckets = getattr(coordinator, "_research_daily_buckets", None)
    if not isinstance(buckets, dict):
        return None
    days_observed = len(buckets)
    dispatched = sum(b.get("dispatched", 0) for b in buckets.values())
    rec_only = sum(b.get("recommendation_only", 0) for b in buckets.values())
    return _LearningProgressContext(
        days_observed=days_observed,
        dispatched_count=dispatched,
        recommendation_only_count=rec_only,
    )


def compute_learning_progress(
    window_ids: list[str],
    coordinator_data: Any,  # SmartShadingData | None
    window_configs: dict[str, Any] | None = None,  # dict[str, WindowConfig] | None
    learning_context: _LearningProgressContext | None = None,
) -> tuple[int | None, dict[str, Any]]:
    """Derive zone-level learning progress from per-window AdaptiveProfiles.

    Returns (progress_percent, attributes) where:
      progress_percent  int|None  0-100 or None when no eligible windows
      attributes        dict      status, eligible_windows, excluded_windows, …

    Only FULLY_AUTOMATIC windows enter the average.  Non-eligible windows
    contribute neither to the numerator nor to the denominator.
    Returns (None, attrs) when no eligible windows exist — the sensor entity
    should then report unavailable.

    When learning_context is provided, two additional conservative caps are
    applied on top of the per-confidence-level caps:
      1. Days-based cap: progress scales linearly over _MIN_DAYS_FULL_PROGRESS days.
      2. Dispatch fraction cap: capped at _SHADOW_ONLY_CAP when physical dispatch
         is below _MIN_DISPATCH_FRACTION of all observed decisions.
    When learning_context is None the function behaves identically to before.

    Uses only privacy-safe aggregate values.  Safe with coordinator_data=None.
    """
    n_total = len(window_ids)

    # Determine eligible window subset.
    if window_configs is not None:
        eligible_ids = [
            wid for wid in window_ids
            if is_shading_learning_eligible(
                getattr(window_configs.get(wid), "behavior_mode", WindowBehaviorMode.FULLY_AUTOMATIC)
            )
        ]
    else:
        eligible_ids = list(window_ids)

    n_eligible = len(eligible_ids)
    n_excluded = n_total - n_eligible

    if n_eligible == 0:
        return None, {
            "status": _LEARNING_STATUS_COLLECTING,
            "eligible_windows": 0,
            "excluded_windows": n_excluded,
            "windows_learning_active": 0,
            "adaptation_active": False,
            "reason": "no_fully_automatic_windows",
        }

    if coordinator_data is None:
        return 0, {
            "status": _LEARNING_STATUS_COLLECTING,
            "eligible_windows": n_eligible,
            "excluded_windows": n_excluded,
            "windows_learning_active": 0,
            "adaptation_active": False,
        }

    profiles = coordinator_data.adaptive_profiles  # dict[str, AdaptiveProfile]

    total_strength = 0.0
    n_learning = 0
    n_adapting = 0

    for wid in eligible_ids:
        profile = profiles.get(wid)
        if profile is not None:
            cap = _CONFIDENCE_LEVEL_CAPS.get(profile.confidence_level, 0.0)
            effective_strength = min(profile.adaptation_strength, cap)
            total_strength += effective_strength
            if profile.learning_active:
                n_learning += 1
            if effective_strength > 0:
                n_adapting += 1

    avg_strength = total_strength / n_eligible

    # Apply calibration caps when dispatch telemetry is available.
    calibrated_strength = avg_strength
    limiters: list[str] = []

    if learning_context is not None:
        days = learning_context.days_observed
        dispatched = learning_context.dispatched_count
        rec_only = learning_context.recommendation_only_count
        total_decisions = dispatched + rec_only
        dispatch_fraction = dispatched / total_decisions if total_decisions > 0 else 0.0

        days_cap = min(1.0, days / _MIN_DAYS_FULL_PROGRESS) if days > 0 else 0.0
        if days_cap < 0.85:
            limiters.append("insufficient_days")

        if total_decisions > 0 and dispatch_fraction < _MIN_DISPATCH_FRACTION:
            calibrated_strength = min(calibrated_strength, _SHADOW_ONLY_CAP)
            limiters.append("low_dispatch_ratio")

        calibrated_strength = min(calibrated_strength, days_cap)

    progress_pct = round(calibrated_strength * 100)

    if progress_pct == 0:
        status = _LEARNING_STATUS_COLLECTING
    elif progress_pct < 40:
        status = _LEARNING_STATUS_LEARNING
    elif progress_pct < 80:
        status = _LEARNING_STATUS_ADAPTING
    else:
        status = _LEARNING_STATUS_CONFIDENT

    attrs: dict[str, Any] = {
        "status": status,
        "eligible_windows": n_eligible,
        "excluded_windows": n_excluded,
        "windows_learning_active": n_learning,
        "adaptation_active": n_adapting > 0,
    }

    if learning_context is not None:
        if not limiters:
            progress_limited_by = "none"
        elif len(limiters) == 2:
            progress_limited_by = "both"
        else:
            progress_limited_by = limiters[0]

        if days < 7:
            situation_diversity = "low"
        elif days < 21 or dispatch_fraction < 0.20:
            situation_diversity = "moderate"
        else:
            situation_diversity = "high"

        if dispatched < 10:
            outcome_coverage = "low"
        elif dispatched < 50:
            outcome_coverage = "moderate"
        else:
            outcome_coverage = "high"

        if avg_strength == 0.0:
            confidence_quality = "none"
        elif avg_strength < 0.10:
            confidence_quality = "very_low"
        elif avg_strength < 0.30:
            confidence_quality = "low"
        elif avg_strength < 0.50:
            confidence_quality = "medium"
        elif avg_strength < 0.75:
            confidence_quality = "high"
        else:
            confidence_quality = "very_high"

        if days < 3:
            learning_stage = "recent_start"
        elif progress_pct == 0:
            learning_stage = "collecting_data"
        elif progress_pct < 15:
            learning_stage = "early_patterns"
        elif progress_pct < 40:
            learning_stage = "partial_understanding"
        elif progress_pct < 70:
            learning_stage = "reliable_learning"
        else:
            learning_stage = "mature"

        attrs.update({
            "learning_stage": learning_stage,
            "progress_limited_by": progress_limited_by,
            "days_observed": days,
            "dispatched_count": dispatched,
            "recommendation_only_count": rec_only,
            "situation_diversity": situation_diversity,
            "outcome_coverage": outcome_coverage,
            "confidence_quality": confidence_quality,
        })

    return progress_pct, attrs


# ---------------------------------------------------------------------------
# Learning Progress Sensor entity
# ---------------------------------------------------------------------------

class SmartShadingLearningProgressSensor(CoordinatorEntity[SmartShadingCoordinator], SensorEntity):
    """Learning Progress Sensor — one entity per SmartShading zone.

    State: integer 0–100 representing the zone's average adaptation_strength
    as a percentage.  0 % means SmartShading is still collecting data.
    100 % means maximum confidence and full adaptation strength.

    Note: 100 % does NOT mean perfect decisions — it means enough observed
    data and high confidence for adaptation to be active at full strength.

    Attributes: status (collecting/learning/adapting/confident),
    windows_tracked, windows_learning_active, adaptation_active.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: SmartShadingCoordinator,
        zone_id: str,
        zone_name: str,
        window_ids: list[str],
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._window_ids = window_ids

        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{zone_id}_learning_progress"
        )
        self._attr_translation_key = "learning_progress"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_{zone_id}")},
            name=zone_name,
        )

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        # Unavailable (not just unknown) when no eligible windows exist in this zone.
        return any(
            is_shading_learning_eligible(
                getattr(self.coordinator.windows.get(wid), "behavior_mode",
                        WindowBehaviorMode.FULLY_AUTOMATIC)
            )
            for wid in self._window_ids
        )

    @property
    def native_value(self) -> int | None:
        ctx = _build_learning_context(self.coordinator)
        pct, _ = compute_learning_progress(
            self._window_ids, self.coordinator.data, self.coordinator.windows, ctx
        )
        return pct

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ctx = _build_learning_context(self.coordinator)
        _, attrs = compute_learning_progress(
            self._window_ids, self.coordinator.data, self.coordinator.windows, ctx
        )
        return attrs


# ---------------------------------------------------------------------------
# Shading Result — pure aggregation helper
# ---------------------------------------------------------------------------

_SHADING_RESULT_EXCELLENT:       str = "excellent"
_SHADING_RESULT_GOOD:            str = "good"
_SHADING_RESULT_ACCEPTABLE:      str = "acceptable"
_SHADING_RESULT_POOR:            str = "poor"
_SHADING_RESULT_UNKNOWN:         str = "unknown"
_SHADING_RESULT_NOT_APPLICABLE:  str = "not_applicable"

# Minimum resolved outcomes across ALL windows in the zone required before
# any state other than "unknown" is reported.  Protects against noisy early data.
_MIN_RESOLVED_OUTCOMES: int = 10

# Most recent outcomes per window to consider.  Older outcomes become less
# representative as the installation matures and the environment changes.
_RECENT_OUTCOMES_COUNT: int = 30

# Classification thresholds
_EXCELLENT_SCORE: float = 0.60
_GOOD_SCORE:      float = 0.30
_ACCEPTABLE_SCORE: float = 0.0

_EXCELLENT_MAX_OVERRIDE_RATE: float = 0.10
_GOOD_MAX_OVERRIDE_RATE:      float = 0.25
_ACCEPTABLE_MAX_OVERRIDE_RATE: float = 0.50


def compute_zone_shading_result(
    window_ids: list[str],
    learning_store: LearningStore,
    window_configs: dict[str, Any] | None = None,  # dict[str, WindowConfig] | None
) -> tuple[str, dict[str, Any]]:
    """Derive zone-level shading result from resolved DecisionOutcome records.

    Returns (state_str, attributes) where state_str is one of the
    _SHADING_RESULT_* constants.

    Only FULLY_AUTOMATIC windows are included in the aggregation.
    Zones with no eligible windows return "not_applicable" (never "poor" etc.)
    Zones with eligible windows but too few outcomes return "unknown".

    Privacy-safe: attributes expose only aggregate counts and rates.
    No raw positions, entity IDs, sensor readings, or outcome records.
    """
    n_total = len(window_ids)

    # Determine eligible window subset.
    if window_configs is not None:
        eligible_ids = [
            wid for wid in window_ids
            if is_shading_learning_eligible(
                getattr(window_configs.get(wid), "behavior_mode", WindowBehaviorMode.FULLY_AUTOMATIC)
            )
        ]
    else:
        eligible_ids = list(window_ids)

    n_eligible = len(eligible_ids)
    n_excluded = n_total - n_eligible

    base_attrs: dict[str, Any] = {
        "eligible_windows": n_eligible,
        "excluded_windows": n_excluded,
    }

    if n_eligible == 0:
        return _SHADING_RESULT_NOT_APPLICABLE, {
            **base_attrs,
            "resolved_outcomes": 0,
            "reason": "no_fully_automatic_windows",
        }

    all_scores: list[float] = []
    override_count = 0
    escalation_count = 0

    for wid in eligible_ids:
        outcomes = learning_store.get_outcomes(wid)
        resolved = [o for o in outcomes if o.outcome_score is not None]
        recent = resolved[-_RECENT_OUTCOMES_COUNT:]
        for o in recent:
            if o.outcome_score is not None:
                all_scores.append(o.outcome_score)
            if o.override_occurred:
                override_count += 1
            if o.escalation_occurred:
                escalation_count += 1

    total = len(all_scores)
    if total < _MIN_RESOLVED_OUTCOMES:
        return _SHADING_RESULT_UNKNOWN, {
            **base_attrs,
            "resolved_outcomes": total,
            "reason": "insufficient_outcomes",
        }

    avg_score = sum(all_scores) / total
    override_rate = override_count / total
    escalation_rate = escalation_count / total

    if avg_score >= _EXCELLENT_SCORE and override_rate <= _EXCELLENT_MAX_OVERRIDE_RATE:
        state = _SHADING_RESULT_EXCELLENT
    elif avg_score >= _GOOD_SCORE and override_rate <= _GOOD_MAX_OVERRIDE_RATE:
        state = _SHADING_RESULT_GOOD
    elif avg_score >= _ACCEPTABLE_SCORE and override_rate <= _ACCEPTABLE_MAX_OVERRIDE_RATE:
        state = _SHADING_RESULT_ACCEPTABLE
    else:
        state = _SHADING_RESULT_POOR

    return state, {
        **base_attrs,
        "resolved_outcomes": total,
        "avg_outcome_score": round(avg_score, 2),
        "override_rate": round(override_rate, 2),
        "escalation_rate": round(escalation_rate, 2),
    }


# ---------------------------------------------------------------------------
# Shading Result Sensor entity
# ---------------------------------------------------------------------------

class SmartShadingZoneShadingResultSensor(CoordinatorEntity[SmartShadingCoordinator], SensorEntity):
    """Shading Result Sensor — zone-level quality rating for shading decisions.

    State: "excellent" / "good" / "acceptable" / "poor" / "unknown"

    Derived from resolved DecisionOutcome records in the learning store.
    Reports "unknown" until at least 10 resolved outcomes have been collected
    across all windows in the zone.

    Attributes: resolved_outcomes, avg_outcome_score, override_rate, escalation_rate.
    All values are privacy-safe aggregates — no raw positions, entity IDs,
    sensor readings, or individual outcome records are exposed.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: SmartShadingCoordinator,
        zone_id: str,
        zone_name: str,
        window_ids: list[str],
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._window_ids = window_ids

        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{zone_id}_shading_result"
        )
        self._attr_translation_key = "shading_result"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_{zone_id}")},
            name=zone_name,
        )

    @property
    def native_value(self) -> str:
        state, _ = compute_zone_shading_result(
            self._window_ids, self.coordinator.learning_store, self.coordinator.windows
        )
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _, attrs = compute_zone_shading_result(
            self._window_ids, self.coordinator.learning_store, self.coordinator.windows
        )
        return attrs
