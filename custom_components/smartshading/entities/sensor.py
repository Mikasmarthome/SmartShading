"""SmartShading sensors (ARCHITECTURE.md §8.1, simplified observability
scope, 2026-06-16).

Applies a sensor-UX design lesson: one main "state" sensor with
`reason`/`next_action` as attributes instead of separate entities, plus one
numeric "exposure" sensor carrying the diagnostic geometry/radiation
values (azimuth, sun_elevation, solar_geometry_factor) as attributes - two
entities per window instead of five flat ones.

Observability cleanup (2026-06-16): the Exposure sensor's attributes now
make the three-level exposure model explicit:
  Level 1 (geometry only)      -> `window_in_solar_sector` attribute
                                   (also the binary_sensor's own value)
  Level 2 (geometric factor)   -> `solar_geometry_factor` (0.0-1.0,
                                   renamed from `solar_factor` for clarity -
                                   purely angle/elevation-based, no weather)
  Level 3 (decision-relevant)  -> the sensor's own native_value
                                   (`effective_exposure`, W/m²)
`elevation_clipped` is now also exposed as a diagnostics-only attribute
(see SmartShadingExposureSensor docstring) - display only, not consumed by
any decision logic.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..coordinator import SmartShadingCoordinator
from ..cover_control.command_filter import BLOCKED_GUARD_ACTION_INTERVAL
from ..engines.observability_evaluator import WindowObservation
from ..models.execution_diagnostics import WindowExecutionDiagnostics
from ..models.runtime_mode import derive_runtime_mode
from ..state_machine.states import ShadingState
from .base import SmartShadingWindowEntity
from .zone_summary import (
    SmartShadingLearningProgressSensor,
    SmartShadingZoneShadingResultSensor,
    SmartShadingZoneSummarySensor,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SmartShadingCoordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = []

    # Determine which zones have multiple windows and assign stable 1-based indices.
    _zone_window_count: dict[str, int] = {}
    _window_index: dict[str, int] = {}
    _zone_counter: dict[str, int] = {}
    for window_id, window in coordinator.windows.items():
        _zone_window_count[window.zone_id] = _zone_window_count.get(window.zone_id, 0) + 1
        count = _zone_counter.get(window.zone_id, 0) + 1
        _zone_counter[window.zone_id] = count
        _window_index[window_id] = count

    # Per-window sensors.
    for window_id, window in coordinator.windows.items():
        is_multi = _zone_window_count.get(window.zone_id, 1) > 1
        idx = _window_index.get(window_id)
        entities.append(SmartShadingStateSensor(coordinator, window_id, window.name, window.zone_id, is_multi, idx))
        entities.append(SmartShadingExposureSensor(coordinator, window_id, window.name, window.zone_id, is_multi, idx))
        entities.append(SmartShadingCoverPositionSensor(coordinator, window_id, window.name, window.zone_id, is_multi, idx))
        entities.append(SmartShadingRecommendationSensor(coordinator, window_id, window.name, window.zone_id, is_multi, idx))

    # Per-zone sensors: one Zone Summary Sensor per configured zone.
    # Windows are grouped by zone_id to pass the relevant subset to each entity.
    _zone_windows: dict[str, list[str]] = {}
    for window_id, window in coordinator.windows.items():
        _zone_windows.setdefault(window.zone_id, []).append(window_id)

    for zone_id, zone in coordinator.zones.items():
        _wids = _zone_windows.get(zone_id, [])
        entities.append(
            SmartShadingZoneSummarySensor(
                coordinator=coordinator,
                zone_id=zone_id,
                zone_name=zone.name,
                window_ids=_wids,
            )
        )
        entities.append(
            SmartShadingLearningProgressSensor(
                coordinator=coordinator,
                zone_id=zone_id,
                zone_name=zone.name,
                window_ids=_wids,
            )
        )
        entities.append(
            SmartShadingZoneShadingResultSensor(
                coordinator=coordinator,
                zone_id=zone_id,
                zone_name=zone.name,
                window_ids=_wids,
            )
        )

    async_add_entities(entities)


def _recommendation_native_value(diag: WindowExecutionDiagnostics | None) -> int | None:
    """State for RecommendationSensor: target_position_ha (HA convention, 0=closed/100=open).

    Returns None when no recommendation is available this cycle (no diagnostics,
    no sun data, or no TierDecision produced a target position).
    Never returns target_position_internal.
    """
    if diag is None:
        return None
    return diag.target_position_ha


def _recommendation_attributes(
    diag: WindowExecutionDiagnostics | None,
    obs: WindowObservation | None = None,
) -> dict[str, Any] | None:
    """Attributes for RecommendationSensor: execution diagnostic + decision trace.

    Returns None (no attributes this cycle) when diagnostics are unavailable.
    Intentionally excludes internal positions (target_position_internal,
    actual_position_internal, assumed_position_internal) — those are debug-only
    and must not appear in user-facing entities.

    Decision-trace fields (from WindowObservation) explain WHY the current
    recommendation was produced — e.g. why 25% appears even when solar
    exposure is 0 W/m² (StateGuard hysteresis keeps the state locked).
    """
    if diag is None:
        return None

    # Decision-trace: what state was decided and what drove it.
    _exposure = obs.exposure if obs is not None else None
    attrs: dict[str, Any] = {
        # Decision trace (Teil C) — reason for the current recommendation.
        "shading_state": obs.state.value if obs is not None else None,
        "lifecycle_state": obs.lifecycle_state if obs is not None else None,
        "previous_lifecycle_state": obs.previous_lifecycle_state if obs is not None else None,
        "sun_elevation_deg": obs.sun_elevation_deg if obs is not None else None,
        "absence_active": obs.absence_active if obs is not None else None,
        "override_active": obs.override_active if obs is not None else None,
        # in_solar_sector: effective value — includes manual sector and obstruction
        # corrections. Falls back to raw geometry when effective_solar_sector is not
        # set (legacy or no-sun path). Raw geometry is also on the exposure sensor
        # as window_in_solar_sector.
        "in_solar_sector": (
            obs.effective_solar_sector
            if obs is not None and obs.effective_solar_sector is not None
            else (_exposure.is_in_tolerance_window if _exposure is not None else None)
        ),
        "solar_source": obs.solar_source if obs is not None else None,
        "obstruction_blocked": obs.obstruction_blocked if obs is not None else None,
        "manual_sun_sector_active": obs.manual_sun_sector_active if obs is not None else None,
        "solar_exposure_w_m2": (
            round(_exposure.effective_exposure, 1) if _exposure is not None else None
        ),
        # Authoritative measured (or fallback) source value and the vertical-window
        # low-angle direct-glare estimate.  Makes the morning/evening case explicit:
        # the standard effective exposure can be low while low-angle direct sun is
        # high (the glare floor then fires via the low-angle path).
        "measured_solar_w_m2": (
            round(_exposure.measured_solar_wm2, 1) if _exposure is not None else None
        ),
        "low_angle_direct_glare_w_m2": (
            round(_exposure.low_angle_direct_glare_wm2, 1)
            if _exposure is not None else None
        ),
        # Diagnostic-only (v1.1.1): the window is in its solar sector, the sun
        # is high enough for direct sun to be plausible, the measured sensor
        # (not a weather/cloud estimate) is authoritative and unusually low,
        # and it is not raining — suggests the sensor may be locally shaded /
        # unrepresentative for this window. Never drives any decision; purely
        # informational. See coordinator._measured_solar_may_be_locally_shaded.
        "measured_solar_may_be_locally_shaded": diag.measured_solar_may_be_locally_shaded,
        "heat_protection_active": (
            obs.comfort_assessment.heat_protection_needed
            if obs is not None and obs.comfort_assessment is not None
            else None
        ),
        "glare_protection_active": (
            obs.comfort_assessment.glare_protection_needed
            if obs is not None and obs.comfort_assessment is not None
            else None
        ),
        # StateGuard state-duration hold (minimum_state_duration).
        # True means the state machine wanted to transition but was held by the
        # minimum_state_duration lock. target_position_ha already shows what the
        # tier pipeline recommends; the cover has not moved yet because of this hold.
        # Distinct from guard_blocked (action-interval guard on cover commands).
        "state_guard_blocked": obs.guard_blocked if obs is not None else None,
    }
    attrs.update({
        "execution_mode": diag.execution_mode,
        "learning_enabled": diag.learning_enabled,
        "active_control_enabled": diag.active_control_enabled,
        # Resolved two-switch runtime mode (inactive / shadow_only / deterministic
        # / adaptive) so the mode is readable directly instead of recombining the
        # two switch attributes by hand.
        "runtime_mode": derive_runtime_mode(
            diag.learning_enabled, diag.active_control_enabled
        ).value,
        "tier_decided_by": diag.tier_decided_by,
        "command_allowed": diag.command_allowed,
        "command_blocked_reason": diag.command_blocked_reason,
        "dispatch_suppressed_reason": diag.dispatch_suppressed_reason,
        "night_hard_hold_applied": diag.night_hard_hold_applied,
        "startup_grace_remaining": diag.startup_grace_remaining,
        "last_command_status": diag.last_command_status,
        "last_command_sent_at": diag.last_command_sent_at,
        "service_call_sent": diag.service_call_sent,
        "service_call_failed": diag.service_call_failed,
        "execution_error": diag.execution_error,
        "is_safety": diag.is_safety,
        "safety_result_failed": diag.safety_result_failed,
        # Learning trace (beta.10): deterministic baseline vs final + adaptive state.
        # All None when there is no sun data / no learning history yet, so the
        # attributes are robust whether or not learning is enabled.
        "deterministic_baseline_target": diag.deterministic_baseline_target_ha,
        "deterministic_baseline_decided_by": diag.deterministic_baseline_decided_by,
        "baseline_to_final_delta": diag.baseline_to_final_delta_ha,
        "adaptive_strength": diag.adaptive_strength,
        "adaptive_applied": diag.adaptive_applied,
        # Which indoor-temperature basis fed this window's thermal reasoning:
        # "global" (house-wide average, current default), "unknown" (no sensor),
        # or "window"/"zone" if a more specific source is configured later.
        "thermal_attribution_source": diag.thermal_attribution_source,
        # True when a contact-driven Option B night move skipped the minimum action
        # interval this cycle (immediate reaction to a real window open/close).
        "min_interval_bypassed": diag.min_interval_bypassed,
        "dispatch_throttled": diag.dispatch_throttled,
        "throttle_wait_ms": diag.throttle_wait_ms,
        "cover_available": diag.cover_available,
        # Derived: True when StateGuard blocked the command this cycle.
        "guard_blocked": diag.command_blocked_reason == BLOCKED_GUARD_ACTION_INTERVAL,
        "cover_entity_id": diag.cover_entity_id,
        "has_position_feedback": diag.has_position_feedback,
        # ShadingGroup harmonization context (Step 9G10e / Step 7).
        # harmonization_active=True means target_position_ha was changed from
        # the window's own recommendation to the group's minimum.
        "shading_group_id": diag.shading_group_id,
        "harmonization_active": diag.shading_group_harmonized,
        "harmonized_target_position": (
            diag.target_position_ha if diag.shading_group_harmonized else None
        ),
        "pre_harmonization_target_position": diag.pre_harmonization_target_position_ha,
        # Daytime Minimum Open Position context (Step 9G10f-b).
        # daytime_min_open_applied=True means target_position_ha was raised from
        # the tier recommendation to the hardware-type minimum open position.
        "daytime_min_open_applied": diag.daytime_min_open_applied,
        "pre_daytime_min_target_position_ha": diag.pre_daytime_min_target_position_ha,
        # Anti-Heat-Buildup context (Step 9G10f-c).
        # anti_heat_buildup_applied=True means target_position_ha was raised to
        # prevent heat buildup between roller shutter and window glass under
        # strong direct solar radiation.
        "anti_heat_buildup_applied": diag.anti_heat_buildup_applied,
        "pre_anti_heat_buildup_target_position_ha": diag.pre_anti_heat_buildup_target_position_ha,
        # Tilt execution context (Step 9G10f-d).
        # Shows the planned tilt target, current tilt feedback, and whether
        # a tilt service call was sent.  All None / False until Step 9G10f-e
        # provides sun-angle-based tilt targets for VENETIAN_BLIND covers.
        "target_tilt_ha": diag.target_tilt_ha,
        "current_tilt_ha": diag.current_tilt_ha,
        "has_tilt_feedback": diag.has_tilt_feedback,
        "tilt_command_sent": diag.tilt_command_sent,
        "tilt_command_failed": diag.tilt_command_failed,
        "tilt_error": diag.tilt_error,
        # Comfort Movement Stability Hold (v1.1.1/v1.1.2). Whether the hold is
        # currently active for this window is derivable from
        # command_blocked_reason == "comfort_position_hold" above; these two
        # fields add the timing context (how long since the last real
        # non-priority dispatch, how many minutes remain) so a held state is
        # explainable without a research export.
        "comfort_hold_last_dispatch_age_min": diag.comfort_hold_last_dispatch_age_min,
        "comfort_hold_remaining_min": diag.comfort_hold_remaining_min,
        # Manual Override daytime/night duration scope (v1.1.3). scope is
        # "daytime" (fixed duration, default 120 min) or "night" (held until
        # the Morning lifecycle transition; expires_at is a safety-net cap,
        # not the real release point). release_reason explains why an
        # override that was active last cycle is not active this cycle:
        # "timeout", "lifecycle_transition", "safety", or None.
        "manual_override_active": diag.manual_override_active,
        "manual_override_scope": diag.manual_override_scope,
        "manual_override_expires_at": diag.manual_override_expires_at,
        "manual_override_remaining_min": diag.manual_override_remaining_min,
        "manual_override_release_reason": diag.manual_override_release_reason,
        # Position-based self-healing recovery open (v1.1.5). True when an
        # ABSENCE_ONLY / ABSENCE_AND_SCHEDULE window that was stuck physically
        # down after a current_state desync was released with a one-directional
        # OPEN this cycle (decided_by == "BehaviorMode:recovery_open").
        "behavior_mode_recovery_open": diag.behavior_mode_recovery_open,
    })
    return attrs


class SmartShadingStateSensor(SmartShadingWindowEntity, SensorEntity):
    """Main per-window sensor: current ShadingState, with reason and the
    display-only next-action preview as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: SmartShadingCoordinator, window_id: str, window_name: str, zone_id: str, is_multi_window_zone: bool = False, window_index: int | None = None) -> None:
        super().__init__(coordinator, window_id, window_name, "state", zone_id, is_multi_window_zone, window_index)
        self._attr_options = [state.value for state in ShadingState]

    @property
    def _adaptive_profile(self):
        """AdaptiveProfile for this window from coordinator data, or None."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.adaptive_profiles.get(self._window_id)

    @property
    def native_value(self) -> str | None:
        observation = self._observation
        return observation.state.value if observation else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        observation = self._observation
        if observation is None:
            return None
        attributes: dict[str, Any] = {
            "reason": observation.reason,
            "reason_code": observation.reason_code,
            "next_action": observation.next_action,
            # Lifecycle Engine round (2026-06-16)
            "lifecycle_state": observation.lifecycle_state,
            "previous_lifecycle_state": observation.previous_lifecycle_state,
            "sun_elevation_deg": observation.sun_elevation_deg,
            "night_active": observation.night_active,
            "absence_active": observation.absence_active,
        }
        # Comfort Engine round (2026-06-17) - comfort flags and the
        # temperatures that drove the decision, added as attributes rather
        # than separate entities (same "no entity flood" rule as lifecycle).
        comfort = observation.comfort_assessment
        if comfort is not None:
            attributes["heat_protection_needed"] = comfort.heat_protection_needed
            attributes["glare_protection_needed"] = comfort.glare_protection_needed
            attributes["solar_gain_beneficial"] = comfort.solar_gain_beneficial
            attributes["comfort_reason"] = comfort.reason
            attributes["indoor_temperature_available"] = comfort.indoor_temp_available
            if comfort.indoor_temperature is not None:
                attributes["indoor_temperature"] = round(comfort.indoor_temperature, 1)
        if observation.outdoor_temperature is not None:
            attributes["outdoor_temperature"] = round(observation.outdoor_temperature, 1)
        # Phase 9E: learning diagnostics — always present (defaults to empty/zero/False
        # for Minimal-Setup installations with no learning history yet).
        attributes["learning_data_available"] = observation.learning_data_available
        attributes["last_5_transitions"] = observation.last_5_transitions
        attributes["override_count_24h"] = observation.override_count_24h
        attributes["override_count_7d"] = observation.override_count_7d
        attributes["transition_count_24h"] = observation.transition_count_24h
        attributes["transition_count_7d"] = observation.transition_count_7d
        # Step 9G10b: manual override visibility.
        # override_position is in HA convention (0=closed, 100=open) —
        # already converted at the WindowObservation level.
        attributes["override_active"] = observation.override_active
        attributes["override_position"] = observation.override_position
        attributes["override_expires_at"] = observation.override_expires_at
        attributes["override_source"] = observation.override_source
        # Step 9G10c: learning and adaptation visibility.
        # None when no AdaptiveProfile exists yet (first cycle before learning pipeline ran).
        # Raw factors (heat_sensitivity_factor etc.) and adaptation_strength are
        # intentionally excluded — they are internal calibration values, not user-facing.
        profile = self._adaptive_profile
        attributes["learning_active"] = profile.learning_active if profile is not None else None
        attributes["confidence_level"] = profile.confidence_level if profile is not None else None
        attributes["adaptation_active"] = (profile.adaptation_strength > 0) if profile is not None else None
        return attributes


class SmartShadingExposureSensor(SmartShadingWindowEntity, SensorEntity):
    """Effective solar exposure (W/m², Level 3 of the three-level exposure
    model) with the underlying geometry/radiation breakdown (Levels 1-2)
    as attributes - avoids a separate sensor per diagnostic value.

    `elevation_clipped` is included purely so real installations generate
    data points for the still-unvalidated floor-level threshold placeholders
    (see TODO.md Concern #2) - it has no effect on `effective_exposure` or
    on any state/decision logic, today or in this round's changes.
    """

    _attr_device_class = SensorDeviceClass.IRRADIANCE
    _attr_native_unit_of_measurement = "W/m²"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SmartShadingCoordinator, window_id: str, window_name: str, zone_id: str, is_multi_window_zone: bool = False, window_index: int | None = None) -> None:
        super().__init__(coordinator, window_id, window_name, "exposure", zone_id, is_multi_window_zone, window_index)

    @property
    def native_value(self) -> float | None:
        observation = self._observation
        if observation is None or observation.exposure is None:
            return None
        return round(observation.exposure.effective_exposure, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        observation = self._observation
        if observation is None or observation.exposure is None:
            return None
        exposure = observation.exposure
        attributes: dict[str, Any] = {
            "azimuth": round(exposure.sun_azimuth, 1),
            "sun_elevation": round(exposure.sun_elevation, 1),
            # Level 1 (pure geometry) - raw azimuth-tolerance result before
            # manual sector override and obstruction zones are applied.
            "window_in_solar_sector": exposure.is_in_tolerance_window,
            # Level 1 (effective) - what SolarEvaluator actually sees.
            # False when manual sun sector or an obstruction zone suppresses
            # direct exposure even though raw geometry says "in sector".
            "effective_in_solar_sector": observation.effective_solar_sector,
            # Source trace: which input produced effective_solar_radiation_wm2.
            "solar_source": observation.solar_source,
            "obstruction_blocked": observation.obstruction_blocked,
            "manual_sun_sector_active": observation.manual_sun_sector_active,
            # Level 2 (geometric attenuation factor, 0.0-1.0) - renamed from
            # "solar_factor": purely angle/elevation-based, no weather input.
            "solar_geometry_factor": round(exposure.direct_radiation_factor, 3),
            "theoretical_exposure": round(exposure.theoretical_exposure, 1),
            # Diagnostics only (2026-06-16) - not used by any decision yet;
            # see class docstring and TODO.md Concern #2.
            "elevation_clipped": exposure.elevation_clipped,
        }
        # Weather/solar inputs (2026-06-16) - only included when a sensor or
        # weather entity actually provided a value, so the attribute list
        # doesn't fill up with Nones when nothing is configured.
        if observation.outdoor_temperature is not None:
            attributes["outdoor_temperature"] = round(observation.outdoor_temperature, 1)
        if observation.solar_radiation is not None:
            attributes["solar_radiation"] = round(observation.solar_radiation, 1)
        if observation.cloud_cover is not None:
            attributes["cloud_cover"] = round(observation.cloud_cover, 1)
        if observation.wind_speed is not None:
            attributes["wind_speed"] = round(observation.wind_speed, 1)
        if observation.weather_condition is not None:
            attributes["weather_condition"] = observation.weather_condition
        return attributes


class SmartShadingCoverPositionSensor(SmartShadingWindowEntity, SensorEntity):
    """Capability Detector / Position Awareness round (2026-06-16).

    SmartShading's view of the cover's best-known position — actual if HA
    reports one, otherwise AssumedStateManager estimate, otherwise unknown.
    Marked DIAGNOSTIC because HA's native cover entity already exposes the
    cover position; this sensor represents SmartShading's inferred/assumed
    view plus the capability/confidence breakdown, which is diagnostic detail.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SmartShadingCoordinator, window_id: str, window_name: str, zone_id: str, is_multi_window_zone: bool = False, window_index: int | None = None) -> None:
        super().__init__(coordinator, window_id, window_name, "cover_position", zone_id, is_multi_window_zone, window_index)

    @property
    def native_value(self) -> int | None:
        observation = self._observation
        if observation is None or observation.cover_position is None:
            return None
        return observation.cover_position.best_known_position

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        observation = self._observation
        if observation is None or observation.cover_position is None:
            return None
        cover_position = observation.cover_position
        return {
            "actual_position": cover_position.actual_position,
            "assumed_position": cover_position.assumed_position,
            "position_source": cover_position.position_source,
            "position_confidence": (
                round(cover_position.position_confidence, 3)
                if cover_position.position_confidence is not None
                else None
            ),
            "position_confidence_level": cover_position.position_confidence_level,
            "position_uncertainty_pct": cover_position.position_uncertainty_pct,
            "capability_type": cover_position.capability_type,
            "supports_position": cover_position.supports_position,
            "supports_stop": cover_position.supports_stop,
            "supports_open": cover_position.supports_open,
            "supports_close": cover_position.supports_close,
            "assumed_position_required": cover_position.assumed_position_required,
        }


class SmartShadingRecommendationSensor(SmartShadingWindowEntity, SensorEntity):
    """Per-window recommendation sensor: what SmartShading would do, and why.

    State: target_position_ha in HA convention (0=closed, 100=open).
    None/unknown when no recommendation is available this cycle (no sun data,
    no TierDecision, or window not yet in execution_diagnostics).

    Attributes expose the full WindowExecutionDiagnostics snapshot so the user
    can inspect: execution mode, command filter decision, blocking reason,
    startup grace, StateGuard, GlobalDispatchThrottle, safety state, and whether
    a real service call was sent.  Internal positions are intentionally omitted —
    use _recommendation_attributes() for the canonical attribute set.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: SmartShadingCoordinator,
        window_id: str,
        window_name: str,
        zone_id: str,
        is_multi_window_zone: bool = False,
        window_index: int | None = None,
    ) -> None:
        super().__init__(coordinator, window_id, window_name, "recommendation", zone_id, is_multi_window_zone, window_index)

    @property
    def _execution_diagnostic(self) -> WindowExecutionDiagnostics | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.execution_diagnostics.get(self._window_id)

    @property
    def native_value(self) -> int | None:
        return _recommendation_native_value(self._execution_diagnostic)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        diag = self._execution_diagnostic
        obs: WindowObservation | None = None
        if self.coordinator.data is not None:
            obs = self.coordinator.data.window_results.get(self._window_id)
        attrs = _recommendation_attributes(diag, obs)

        # Step 6: target adaptation diagnostics (privacy-safe, aggregate only).
        try:
            _profile = (
                self.coordinator.data.adaptive_profiles.get(self._window_id)
                if self.coordinator.data is not None
                else None
            )
            _confidence = _profile.confidence_level if _profile is not None else "very_low"
            _adapter = self.coordinator.target_position_adapter
            _ta_diag = _adapter.get_adaptation_diagnostics(
                self._window_id, _confidence
            )
            if attrs is None:
                attrs = {}
            attrs.update(_ta_diag)
        except Exception:
            pass

        # Solar threshold trace: expose adapted thresholds so the user can see
        # whether learning has lowered normal_threshold below the raw exposure —
        # which would explain NORMAL_SHADE at unexpectedly low W/m² values.
        try:
            if self.coordinator.data is not None:
                _trace = self.coordinator.data.adaptation_traces.get(self._window_id)
                if _trace is not None:
                    if attrs is None:
                        attrs = {}
                    attrs["light_threshold_wm2"] = round(_trace.light_shade_threshold_adapted, 1)
                    attrs["normal_threshold_wm2"] = round(_trace.normal_shade_threshold_adapted, 1)
                    attrs["strong_threshold_wm2"] = round(_trace.strong_shade_threshold_adapted, 1)
        except Exception:
            pass

        return attrs
