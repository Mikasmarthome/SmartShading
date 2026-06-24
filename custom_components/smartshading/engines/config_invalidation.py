"""Differentiated config-change invalidation matrix — LE 2.0 / Phase P10 (pure).

The generic config_generation gate is the last safety net; this matrix is the
precise, per-change classification of which learned authority must be
invalidated, suspended, revalidated or retained.  Pure mapping — the caller
applies the actions.  No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- change types ---
CHANGE_ORIENTATION = "window_orientation"
CHANGE_SUN_SECTOR = "manual_sun_sector"
CHANGE_OBSTRUCTION = "obstruction_config"
CHANGE_COVER_REPLACEMENT = "cover_entity_replacement"
CHANGE_INDOOR_TEMP_SENSOR = "indoor_temperature_sensor"
CHANGE_SOLAR_SENSOR = "solar_sensor"
CHANGE_FORECAST_PROVIDER = "forecast_provider"
CHANGE_BEHAVIOR_MODE_AWAY = "behavior_mode_left_fully_automatic"
CHANGE_BEHAVIOR_MODE_BACK = "behavior_mode_back_to_fully_automatic"
CHANGE_CONFIGURED_TARGETS = "configured_targets_or_thresholds"
CHANGE_FEEDBACK_CAPABILITY_LOSS = "feedback_capability_loss"

# --- actions ---
ACTION_INVALIDATE = "invalidate"
ACTION_SUSPEND = "suspend"
ACTION_REVALIDATE = "revalidate"
ACTION_RETAIN = "retain"
ACTION_RESET_RELIABILITY = "reset_reliability"

# --- learned-authority scopes ---
SCOPE_GEOMETRY_SHADOWS = "geometry_dependent_shadows"
SCOPE_CONTRIBUTION = "contribution_evidence"
SCOPE_POSITION = "position_experiments_and_adoptions"
SCOPE_MOVEMENT = "movement_feedback_evidence"
SCOPE_THERMAL_MODEL = "thermal_response_model"
SCOPE_SOLAR_THRESHOLD = "solar_threshold_timing"
SCOPE_FORECAST_TRUST = "forecast_trust_context"
SCOPE_STRATEGY = "strategy_adoptions"


@dataclass(frozen=True)
class InvalidationDirective:
    scope: str
    action: str
    reason: str


@dataclass(frozen=True)
class InvalidationPlan:
    change_type: str
    directives: tuple[InvalidationDirective, ...] = field(default_factory=tuple)

    def actions_for(self, scope: str) -> tuple[str, ...]:
        return tuple(d.action for d in self.directives if d.scope == scope)


def _d(scope: str, action: str, reason: str) -> InvalidationDirective:
    return InvalidationDirective(scope, action, reason)


_MATRIX: dict[str, tuple[InvalidationDirective, ...]] = {
    CHANGE_ORIENTATION: (
        _d(SCOPE_GEOMETRY_SHADOWS, ACTION_INVALIDATE, "geometry_changed"),
        _d(SCOPE_CONTRIBUTION, ACTION_INVALIDATE, "geometry_changed"),
        _d(SCOPE_POSITION, ACTION_INVALIDATE, "geometry_changed"),
        _d(SCOPE_STRATEGY, ACTION_INVALIDATE, "geometry_changed"),
        # General thermal zone model retained unless window-specifically tainted.
        _d(SCOPE_THERMAL_MODEL, ACTION_RETAIN, "zone_model_not_window_specific"),
    ),
    CHANGE_SUN_SECTOR: (
        _d(SCOPE_GEOMETRY_SHADOWS, ACTION_INVALIDATE, "solar_context_changed"),
        _d(SCOPE_SOLAR_THRESHOLD, ACTION_INVALIDATE, "solar_context_changed"),
        _d(SCOPE_STRATEGY, ACTION_INVALIDATE, "solar_context_changed"),
    ),
    CHANGE_OBSTRUCTION: (
        _d(SCOPE_GEOMETRY_SHADOWS, ACTION_INVALIDATE, "obstruction_changed"),
        _d(SCOPE_CONTRIBUTION, ACTION_INVALIDATE, "obstruction_changed"),
        _d(SCOPE_SOLAR_THRESHOLD, ACTION_INVALIDATE, "obstruction_changed"),
    ),
    CHANGE_COVER_REPLACEMENT: (
        _d(SCOPE_POSITION, ACTION_INVALIDATE, "cover_replaced"),
        _d(SCOPE_MOVEMENT, ACTION_INVALIDATE, "cover_replaced"),
        _d(SCOPE_STRATEGY, ACTION_SUSPEND, "position_dependent_cover_replaced"),
        _d(SCOPE_THERMAL_MODEL, ACTION_RETAIN, "geometry_and_identity_unchanged"),
    ),
    CHANGE_INDOOR_TEMP_SENSOR: (
        _d(SCOPE_THERMAL_MODEL, ACTION_RESET_RELIABILITY, "indoor_sensor_changed"),
        _d(SCOPE_THERMAL_MODEL, ACTION_REVALIDATE, "indoor_sensor_changed"),
        _d(SCOPE_POSITION, ACTION_RETAIN, "position_not_temperature_dependent"),
    ),
    CHANGE_SOLAR_SENSOR: (
        _d(SCOPE_SOLAR_THRESHOLD, ACTION_SUSPEND, "solar_source_changed"),
        _d(SCOPE_FORECAST_TRUST, ACTION_REVALIDATE, "solar_source_changed"),
    ),
    CHANGE_FORECAST_PROVIDER: (
        _d(SCOPE_FORECAST_TRUST, ACTION_INVALIDATE, "forecast_provider_changed"),
        _d(SCOPE_STRATEGY, ACTION_SUSPEND, "forecast_strategy_evidence_stale"),
        _d(SCOPE_POSITION, ACTION_RETAIN, "position_not_forecast_dependent"),
    ),
    CHANGE_BEHAVIOR_MODE_AWAY: (
        _d(SCOPE_STRATEGY, ACTION_SUSPEND, "not_fully_automatic"),
        _d(SCOPE_POSITION, ACTION_SUSPEND, "not_fully_automatic"),
    ),
    CHANGE_BEHAVIOR_MODE_BACK: (
        _d(SCOPE_STRATEGY, ACTION_REVALIDATE, "back_to_fully_automatic"),
        _d(SCOPE_POSITION, ACTION_REVALIDATE, "back_to_fully_automatic"),
    ),
    CHANGE_CONFIGURED_TARGETS: (
        _d(SCOPE_POSITION, ACTION_INVALIDATE, "configured_targets_changed"),
        _d(SCOPE_SOLAR_THRESHOLD, ACTION_INVALIDATE, "configured_thresholds_changed"),
        _d(SCOPE_STRATEGY, ACTION_INVALIDATE, "configured_targets_changed"),
        # Consumed evidence stays consumed; other independent models retained.
        _d(SCOPE_THERMAL_MODEL, ACTION_RETAIN, "independent_of_targets"),
    ),
    CHANGE_FEEDBACK_CAPABILITY_LOSS: (
        _d(SCOPE_POSITION, ACTION_SUSPEND, "feedback_capability_lost"),
        _d(SCOPE_STRATEGY, ACTION_SUSPEND, "feedback_capability_lost"),
    ),
}


def classify_config_change(change_type: str) -> InvalidationPlan:
    """Return the InvalidationPlan for a config change (empty plan if unknown)."""
    return InvalidationPlan(change_type, _MATRIX.get(change_type, ()))
