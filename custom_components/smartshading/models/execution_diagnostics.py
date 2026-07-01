"""WindowExecutionDiagnostics — per-window execution runtime snapshot.

Produced each coordinator cycle alongside WindowObservation.
Captures the full execution-layer state for diagnostics, debugging,
and future UI/sensor entities.

No Home Assistant dependency. Pure frozen dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WindowExecutionDiagnostics:
    """Execution state snapshot for one window, one coordinator cycle.

    Stored in SmartShadingData.execution_diagnostics[window_id].
    Not yet exposed as HA entities — RuntimeData only in Step 9G5a.
    All position fields clearly document their convention so no implicit
    mixing is possible at the reader side.

    In Step 9G5a (dry-run), service_call_sent, service_call_failed,
    last_command_sent_at, and safety_result_failed are always False/None.
    They are reserved for Step 9G5b/9G6 when actual dispatch is enabled.
    """

    # --- Zone configuration (source of truth for this cycle) ---------------

    learning_enabled: bool
    """True when the zone's Learning Mode (learning pipeline) is active."""

    active_control_enabled: bool
    """True when the zone allows cover commands to be dispatched."""

    execution_mode: str
    """ExecutionMode.value: "automatic" when active_control_enabled=True,
    "recommendation_only" when active_control_enabled=False."""

    # --- Cover entity state (from CoverEntitySnapshot, first cover) ---------

    cover_entity_id: str | None
    """HA entity_id of the representative cover for this window.
    None when the window has no cover group or no covers configured."""

    cover_available: bool | None
    """True when the cover entity is reachable and not in unknown/unavailable.
    None when no cover entity_id is configured."""

    actual_position_ha: int | None
    """Raw position from the cover's current_position attribute,
    in HA convention: 0=closed, 100=open. None if unavailable or absent."""

    actual_position_internal: int | None
    """Same position converted to SmartShading internal convention:
    0=open/retracted, 100=shaded/closed. None if actual_position_ha is None."""

    assumed_position_internal: int | None
    """Best-estimate position from AssumedStateManager,
    in SmartShading internal convention. None if no assumed state yet."""

    has_position_feedback: bool | None
    """True when the cover provides reliable position feedback (not Somfy RTS).
    None when no cover entity_id is configured."""

    # --- TierDecision output ------------------------------------------------

    tier_decided_by: str | None
    """Evaluator name that produced the TierDecision (e.g. "SolarEvaluator").
    None when sun data is unavailable this cycle."""

    target_position_internal: int | None
    """Target position from TierDecision, SmartShading internal convention
    (0=open, 100=shaded). None if TierOrchestrator produced no position."""

    target_position_ha: int | None
    """target_position_internal converted to HA convention (0=closed, 100=open)
    by CommandFilter / CoverIntent. None when target_position_internal is None."""

    is_safety: bool
    """True when TierDecision is STORM_SAFE or WIND_SAFE (Tier 1 Safety)."""

    # --- CommandFilter result -----------------------------------------------

    command_allowed: bool | None
    """True when CommandFilter permitted the command.
    None when no cover entity_id or no TierDecision is available."""

    command_blocked_reason: str | None
    """BLOCKED_* constant explaining why execution was prevented.
    None when command_allowed=True or no filter result available."""

    # --- ExecutionResult (most recent result per window) --------------------

    last_command_status: str | None
    """ExecutionStatus.value for the dry-run result:
    "blocked" when CommandFilter blocked, "not_attempted" for all others
    in 9G5a (dispatch not yet enabled). None when no intents were built."""

    last_command_sent_at: datetime | None
    """UTC timestamp when the HA service call was dispatched.
    Always None in Step 9G5a (no dispatch). Reserved for 9G5b/9G6."""

    service_call_sent: bool
    """True when a cover.set_cover_position call was dispatched.
    Always False in Step 9G5a."""

    service_call_failed: bool
    """True when dispatch raised an exception.
    Always False in Step 9G5a."""

    execution_error: str | None
    """Exception text when service_call_failed=True.
    None when dispatch succeeded or was never attempted."""

    safety_result_failed: bool
    """True when is_safety=True AND the service call failed (FAILED status).
    Always False when no dispatch has occurred."""

    dispatch_suppressed_reason: str | None
    """Human-readable reason dispatch was suppressed for an otherwise-allowed intent.
    None when dispatch was not suppressed (either allowed+sent, blocked by CommandFilter,
    or no intent at all).

    Current values:
      "startup_grace_active"  — dispatch suppressed during the post-restart hydration
                                period (_startup_cycles_remaining > 0) for non-safety intents.
    Reserved for future suppression reasons (TravelTracker, maintenance mode, etc.)."""

    night_hard_hold_applied: bool = False
    """True when Night Hard Hold suppressed a non-safety OPEN/raise command that
    would have moved the cover past night_position during the active night interval.
    The decision is replaced with NIGHT_CLOSED at the configured night_position."""

    startup_grace_remaining: int | None = None
    """Number of coordinator startup grace cycles remaining at the time this
    diagnostic was produced. 0 when the grace period has expired and dispatch
    is fully allowed; None in the no-sun path (diagnostics not fully built)."""

    dispatch_throttled: bool = False
    """True when the global dispatch throttle (GlobalDispatchThrottle) delayed this
    dispatch by sleeping before the HA service call.

    False when no throttle wait was applied:
      - First dispatch in this coordinator session (no previous SENT recorded).
      - Minimum inter-dispatch interval had already elapsed.
      - Safety command (STORM_SAFE / WIND_SAFE): bypasses the throttle wait.
      - Command was BLOCKED, NOT_ATTEMPTED, or startup-grace suppressed (no dispatch).

    See throttle_wait_ms for the actual delay duration."""

    throttle_wait_ms: int | None = None
    """Duration in milliseconds the global dispatch throttle slept before dispatching.
    None when dispatch_throttled=False.

    Populated only when dispatch_throttled=True — the integer value is the actual
    wait computed by GlobalDispatchThrottle.time_until_next_allowed(), rounded to
    the nearest millisecond."""

    # --- ShadingGroup harmonization fields (Step 9G10e) ----------------------

    shading_group_id: str | None = None
    """The shading_group_id from WindowConfig, or None when no group is assigned.
    Zone-scoped string key (e.g. 'south', 'west')."""

    shading_group_harmonized: bool = False
    """True when ShadingGroup harmonization changed this window's target_position_ha
    from its own window-level recommendation to the group's minimum value.
    False when: not in a group, group had < 2 eligible members, or this window
    already had the minimum target in the group (target unchanged)."""

    pre_harmonization_target_position_ha: int | None = None
    """This window's own target_position_ha (HA convention, 0=closed/100=open)
    before ShadingGroup harmonization was applied.
    Only populated when shading_group_harmonized=True; None otherwise."""

    # --- Daytime Minimum Open Position fields (Step 9G10f-b) ------------------

    daytime_min_open_applied: bool = False
    """True when the daytime minimum open position clamp raised target_position_ha
    above the TierOrchestrator's original recommendation.
    False when: hardware type has no minimum (GENERIC, VENETIAN_BLIND, AWNING),
    the original target was already at or above the minimum, or the current
    ShadingState is exempt (STORM_SAFE, WIND_SAFE, MANUAL_OVERRIDE,
    NIGHT_CLOSED, ABSENCE_CLOSED)."""

    pre_daytime_min_target_position_ha: int | None = None
    """The target_position_ha value before the daytime minimum open clamp was
    applied (HA convention, 0=closed/100=open).
    Only populated when daytime_min_open_applied=True; None otherwise."""

    # --- Anti-Heat-Buildup fields (Step 9G10f-c) ----------------------------------

    anti_heat_buildup_applied: bool = False
    """True when the anti-heat-buildup clamp raised target_position_ha above
    the TierOrchestrator's recommendation.
    Active only for CoverHardwareType.ROLLER_SHUTTER with anti_heat_buildup_enabled=True,
    when the window is in the solar sector, effective solar exposure meets the
    threshold, and the current ShadingState is not exempt
    (STORM_SAFE, WIND_SAFE, MANUAL_OVERRIDE, NIGHT_CLOSED, or
    ABSENCE_CLOSED unless allow_anti_heat_buildup_during_absence=True)."""

    pre_anti_heat_buildup_target_position_ha: int | None = None
    """The target_position_ha value before the anti-heat-buildup clamp was
    applied (HA convention, 0=closed/100=open).
    Only populated when anti_heat_buildup_applied=True; None otherwise."""

    # --- Tilt execution fields (Step 9G10f-d) ------------------------------------

    target_tilt_ha: int | None = None
    """Target tilt position in HA tilt convention [0, 100].
    None when no tilt target was computed this cycle (position-only covers or
    before Step 9G10f-e implements tilt calculation)."""

    current_tilt_ha: int | None = None
    """Current tilt position as reported by the cover entity [0, 100] HA convention.
    None when the cover does not report tilt or tilt feedback is unavailable."""

    has_tilt_feedback: bool = False
    """True when the cover entity provided a valid numeric tilt value this cycle.
    Mirrors CoverEntitySnapshot.has_tilt_feedback for the representative cover."""

    tilt_command_sent: bool = False
    """True when cover.set_cover_tilt_position was successfully dispatched.
    False for position-only commands, blocked intents, or failed tilt dispatches."""

    tilt_command_failed: bool = False
    """True when the tilt service call raised an exception or HA returned an error.
    When True, tilt_error contains the exception text."""

    tilt_error: str | None = None
    """Exception text when tilt_command_failed=True.
    None when tilt was not attempted, succeeded, or was not part of this command."""

    # --- Startup / override-reference diagnostics (Step 9G5c-diag) -----------

    startup_grace_configured_cycles: int | None = None
    """The STARTUP_GRACE_CYCLES constant used for this coordinator instance.
    Allows support exports to report the configured warmup length alongside
    startup_grace_remaining so the user can see both values."""

    startup_initialization_complete: bool | None = None
    """True when the coordinator startup grace period has fully elapsed
    (_startup_cycles_remaining == 0) at the time this diagnostic was produced.
    False during the grace period (cycles 1..STARTUP_GRACE_CYCLES).
    None in the no-sun path (minimal diagnostics)."""

    previous_observation_available: bool | None = None
    """True when a valid cover position (observed_internal ≠ None) was captured
    this cycle and stored in _prev_observed_internal — i.e. it will be available
    as the previous-observation reference for the NEXT cycle's override check.
    False when the cover was unavailable this cycle (no position to store).
    None in the safety or no-sun path (override branch did not run).

    Note: this reflects the POST-cycle stored state, not the value that was used
    as input to this cycle's override tick (which may have been None on first
    dispatch if the cover was unavailable during the grace cycle)."""

    last_commanded_available: bool | None = None
    """True when, after this cycle, AssumedStateManager has a last_commanded_position
    for this cover — either because SmartShading dispatched in a prior cycle, or
    because a successful dispatch happened in this cycle.
    False only when no dispatch has ever succeeded for this cover since HA restart.
    None in the safety or no-sun path."""

    override_reference_source: str | None = None
    """Source used as the own-command guard reference this cycle.
    One of:
      "last_commanded"        — AssumedStateManager.last_commanded_position used.
      "previous_observation"  — Previous cycle's observed position used.
      "unavailable"           — Neither available; observed_internal used as
                                 fallback so the guard fires (delta=0→no false
                                 override on first observation).
    None in the safety or no-sun path (tick not called)."""

    # --- Clock / bootstrap / lifecycle diagnostics ---------------------------

    cycle_timestamp_utc: datetime | None = None
    """UTC timestamp of the coordinator cycle that produced this diagnostic
    (the `now` captured at the start of _async_update_data). Allows log
    correlation between diagnostics snapshots and coordinator log lines."""

    restore_completed: bool | None = None
    """True when the learning-store restore has finished (_learning_restored=True).
    False on the very first cycle if restore is still pending (rare).
    None when not populated (pre-upgrade snapshots)."""

    required_inputs_ready: bool | None = None
    """True when all inputs required for a full evaluation cycle are available
    this cycle (sun_position is not None). False when operating degraded
    (sun.sun entity unavailable). None when not populated."""

    degraded_input_codes: tuple[str, ...] | None = None
    """Codes for inputs that are missing or degraded this cycle.
    Empty tuple when all inputs are present. None when not populated.
    Current codes: "sun_unavailable"."""

    lifecycle_state_at_cycle: str | None = None
    """LifecycleState.value after lifecycle engine evaluation this cycle
    ("day", "night", "morning"). None when not populated."""

    previous_lifecycle_state: str | None = None
    """LifecycleState.value before lifecycle engine evaluation this cycle —
    the state the coordinator held entering this cycle. Paired with
    lifecycle_state_at_cycle to verify lifecycle_trigger independently.
    None when not populated."""

    lifecycle_trigger: str | None = None
    """Reason code for any lifecycle state transition this cycle.
    "no_change" when state is unchanged. "night_start", "morning_start",
    "day_start" when state transitioned. None when not populated."""

    startup_grace_active: bool | None = None
    """True when startup_grace_remaining > 0 (grace period not yet expired).
    False when grace has fully elapsed and dispatch is allowed.
    None when not populated."""

    rain_status: str | None = None
    """Normalized rain status for this cycle: "raining", "dry", "unknown", or None
    when no rain sensor is configured."""

    rain_safe_active: bool | None = None
    """True when this window is currently in RAIN_SAFE state or holding via
    RainSafeHold. False when rain protection is idle. None when not populated."""

    rain_release_remaining_s: float | None = None
    """Seconds remaining in the rain dry-cooldown hold, or None when no hold
    is active. Computed from SafetyHold._last_triggered and rain_release_delay_min."""

    # --- Night contact diagnostics (v1.1.0) ----------------------------------

    contact_sensor_configured: bool = False
    """True when at least one contact sensor is set for this window."""

    contact_sensor_count: int = 0
    """Number of contact sensors configured for this window (multi-contact)."""

    contact_open_count: int = 0
    """How many of the configured contacts currently read open (aggregated)."""

    contact_status: str | None = None
    """ContactStatus.value ("open", "closed", "unknown") or None when not configured."""

    contact_is_stale: bool = False
    """True when the contact sensor reading is older than the staleness threshold (10 min default).
    A stale reading keeps its last normalized status; the flag surfaces the age in diagnostics."""

    night_contact_blocked: bool = False
    """True when the night move was blocked this night (Option A active, contact OPEN)."""

    catch_up_pending: bool = False
    """True when the night move was blocked and the contact has not yet closed (catch-up waiting)."""

    catch_up_done: bool = False
    """True when a catch-up move was executed this night (contact closed after block)."""

    night_vent_active: bool = False
    """True when the cover is currently commanded to NIGHT_VENT position (Option B)."""

    night_contact_state_label: str | None = None
    """Human-readable NightContactHold.state_label: "idle", "blocked", "caught_up", "night_vent_active"."""

    # --- Learning trace (beta.10) — read-only diagnostics, HA convention ---
    # These surface the deterministic baseline vs the final dispatched target plus
    # the adaptive-layer state at decision time, so beta testers can see what
    # learning changed (if anything) without a research export.  All None on the
    # no-sun / unavailable path and when no learning data exists yet.
    deterministic_baseline_target_ha: int | None = None
    """The deterministic (no-learning) baseline target for this cycle, HA convention."""

    deterministic_baseline_decided_by: str | None = None
    """Which evaluator produced the deterministic baseline (e.g. SolarEvaluator)."""

    baseline_to_final_delta_ha: int | None = None
    """final target_position_ha − deterministic baseline (HA convention).  Includes the
    adaptive layer and any post-baseline clamps; 0 means learning/clamps changed nothing."""

    adaptive_strength: float | None = None
    """AdaptiveProfile.adaptation_strength at decision time [0.0, 1.0]; 0.0 = no adaptation."""

    adaptive_applied: bool | None = None
    """True when a learned position adaptation was actually applied this cycle."""

    thermal_attribution_source: str | None = None
    """Indoor-temperature basis for this window's thermal reasoning: "zone" (this
    zone's configured indoor sensor(s) — a config entry is one zone), "window" or
    "global" (reserved), or "unknown" (no indoor sensor).  Transparency only; it
    does not change control or learning."""

    min_interval_bypassed: bool = False
    """True when a contact-driven night-contact move (Option B vent / return /
    catch-up) bypassed the minimum action interval this cycle so it could react to
    a real window open/close at once.  False for all other decisions, which keep
    the normal interval."""
