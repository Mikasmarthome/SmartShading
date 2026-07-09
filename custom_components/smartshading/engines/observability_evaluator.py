"""Observation helpers for the SmartShading coordinator.

Contains:
  HYSTERESIS_THRESHOLDS — W/m² entry/exit thresholds (ARCHITECTURE.md §4.3).
  build_reason()        — human/machine-readable reason for the active state.
  build_next_action()   — display-only action preview for sensor attributes.
  CoverPositionObservation — cover position data snapshot.
  WindowObservation     — per-window result for one coordinator cycle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..cover_control.cover_capabilities import CoverProfile
from ..models.comfort import ComfortAssessment
from ..models.config import ShadePositionDefaults
from ..models.state import ReasonCode
from ..state_machine.guards import HysteresisConfig
from ..state_machine.states import ShadingState
from .exposure_engine import WindowExposure
from .learning_store import LearningStore

_LOGGER = logging.getLogger(__name__)

# ARCHITECTURE.md §4.3 "Hysterese-Beispiele" - entry/exit thresholds in W/m².
HYSTERESIS_THRESHOLDS: dict[ShadingState, HysteresisConfig] = {
    ShadingState.LIGHT_SHADE: HysteresisConfig(entry_threshold_wm2=150.0, exit_threshold_wm2=100.0),
    ShadingState.NORMAL_SHADE: HysteresisConfig(entry_threshold_wm2=300.0, exit_threshold_wm2=220.0),
    ShadingState.STRONG_SHADE: HysteresisConfig(entry_threshold_wm2=500.0, exit_threshold_wm2=380.0),
}


def build_reason(
    state: ShadingState,
    comfort: ComfortAssessment | None = None,
) -> tuple[str, str]:
    """Human-readable + machine-readable reason (ARCHITECTURE.md §3.5).

    When a ComfortAssessment is supplied the reason reflects the active
    comfort goal that raised or held the state (Comfort Engine phase,
    2026-06-17). Falls back to the exposure-based reason when comfort is
    absent or neutral.
    """
    if state in (ShadingState.LIGHT_SHADE, ShadingState.NORMAL_SHADE, ShadingState.STRONG_SHADE):
        if comfort is not None:
            if comfort.heat_protection_needed:
                return "HEAT_PROTECTION", ReasonCode.HEAT_PROTECTION.value
            if comfort.glare_protection_needed:
                return "GLARE_PROTECTION", ReasonCode.GLARE_PROTECTION.value
        return "SUN", ReasonCode.SUN_EXPOSURE_THRESHOLD.value
    if state is ShadingState.OPEN:
        if comfort is not None and comfort.solar_gain_beneficial:
            return "SOLAR_GAIN", ReasonCode.SOLAR_GAIN.value
        return "NO_SUN", ReasonCode.HYSTERESIS_EXIT.value
    if state is ShadingState.STORM_SAFE:
        return "STORM", ReasonCode.STORM_DETECTED.value
    if state is ShadingState.WIND_SAFE:
        return "WIND", ReasonCode.WIND_DETECTED.value
    if state is ShadingState.NIGHT_CLOSED:
        return "NIGHT", ReasonCode.NIGHT_LIFECYCLE.value
    if state is ShadingState.ABSENCE_CLOSED:
        return "ABSENCE", ReasonCode.ABSENCE_DETECTED.value
    if state is ShadingState.MANUAL_OVERRIDE:
        return "MANUAL", ReasonCode.MANUAL_INTERVENTION_DETECTED.value
    return "NO_SUN", ReasonCode.HYSTERESIS_EXIT.value


def build_next_action(
    new_state: ShadingState, current_state: ShadingState, defaults: ShadePositionDefaults
) -> str:
    """Display-only preview of what a future PositionCalculator
    (ARCHITECTURE.md §5.7 step 11) would do - no command is ever sent in
    this phase.

    v1.1.2 field fix: ShadingState.NIGHT_VENT and ShadingState.RAIN_SAFE were
    missing from the mapping below, so a real Night Contact Option B vent
    (ShadingState.NIGHT_VENT) raised a KeyError here and crashed the
    coordinator refresh (all SmartShading entities for the zone went
    unavailable). This function is display-only diagnostics — it must never
    be able to crash the coordinator, so an unmapped/future ShadingState now
    falls back to a safe, self-describing label instead of raising.

    NIGHT_VENT and RAIN_SAFE (like NIGHT_CLOSED/ABSENCE_CLOSED before them)
    do not have a real target position available here — `defaults` only
    carries light/normal/strong shade positions (ShadePositionDefaults), not
    the per-window night/vent/absence/rain-safe position. NIGHT_VENT is
    labelled as a named action (its real position is per-window config, not
    a fixed default) rather than a fabricated percentage; RAIN_SAFE mirrors
    STORM_SAFE/WIND_SAFE (all three Tier-1 safety states are already
    simplified to "MOVE_TO_0" here for the same reason).
    """
    if new_state == current_state:
        return "NO_ACTION"
    if new_state is ShadingState.MANUAL_OVERRIDE:
        return "NO_ACTION"  # never override the user
    action = {
        ShadingState.OPEN: "OPEN",
        ShadingState.LIGHT_SHADE: f"MOVE_TO_{defaults.light_shade_position}",
        ShadingState.NORMAL_SHADE: f"MOVE_TO_{defaults.normal_shade_position}",
        ShadingState.STRONG_SHADE: f"MOVE_TO_{defaults.strong_shade_position}",
        ShadingState.NIGHT_CLOSED: "MOVE_TO_0",
        ShadingState.NIGHT_VENT: "MOVE_TO_NIGHT_VENT",
        ShadingState.ABSENCE_CLOSED: "MOVE_TO_30",
        ShadingState.STORM_SAFE: "MOVE_TO_0",
        ShadingState.WIND_SAFE: "MOVE_TO_0",
        ShadingState.RAIN_SAFE: "MOVE_TO_0",
    }.get(new_state)
    if action is not None:
        return action
    # Defensive fallback: a future ShadingState added without updating this
    # display-only mapping must never crash the coordinator refresh.
    return f"UNKNOWN_STATE:{getattr(new_state, 'value', new_state)}"


@dataclass(frozen=True)
class CoverPositionObservation:
    """What the cover_position sensor displays (Capability Detector round,
    2026-06-16). Read-only observation - never produced by sending a
    command, only by reading Home Assistant state and the
    AssumedStateManager (ARCHITECTURE.md §6.2).
    """

    actual_position: int | None           # HA convention: 0=closed, 100=open
    assumed_position: int | None          # HA convention: 0=closed, 100=open (converted from internal)
    best_known_position: int | None       # HA convention: actual_position if available, else assumed_position
    position_source: str  # "actual" | "assumed" | "unknown"
    position_confidence: float | None
    position_confidence_level: str | None  # "High" | "Good" | "Low" | "Unreliable"
    position_uncertainty_pct: float | None
    capability_type: str  # CoverProfile.value
    supports_position: bool
    supports_stop: bool
    supports_open: bool
    supports_close: bool
    assumed_position_required: bool  # = not has_reliable_position_feedback

    @classmethod
    def unknown(cls) -> "CoverPositionObservation":
        """No cover assigned yet, or nothing has been observed at all."""
        return cls(
            actual_position=None,
            assumed_position=None,
            best_known_position=None,
            position_source="unknown",
            position_confidence=None,
            position_confidence_level=None,
            position_uncertainty_pct=None,
            capability_type=CoverProfile.UNKNOWN.value,
            supports_position=False,
            supports_stop=False,
            supports_open=False,
            supports_close=False,
            assumed_position_required=True,
        )


@dataclass(frozen=True)
class WindowObservation:
    """Per-window result for one update cycle - what the sensors display.

    outdoor_temperature/solar_radiation/cloud_cover/wind_speed/
    weather_condition are read once per cycle from the optional weather/
    solar sensors (2026-06-16 weather-input round) and copied into every
    window's observation - there is one shared weather source for the
    whole house, not per-window weather data.

    `cover_position` (2026-06-16 Capability Detector round) is per-window,
    derived from that window's CoverGroup - unlike the weather fields above.
    """

    state: ShadingState
    reason: str
    reason_code: str
    next_action: str
    guard_blocked: bool
    exposure: WindowExposure | None
    outdoor_temperature: float | None = None
    solar_radiation: float | None = None
    cloud_cover: float | None = None
    wind_speed: float | None = None
    weather_condition: str | None = None
    cover_position: CoverPositionObservation | None = None
    # Lifecycle Engine round (2026-06-16) - house-wide, copied into every
    # window's observation, same pattern as the weather fields above.
    lifecycle_state: str | None = None
    previous_lifecycle_state: str | None = None
    sun_elevation_deg: float | None = None
    night_active: bool = False
    absence_active: bool = False
    # Solar trace fields (2026-06-21): source selection and sector evaluation.
    # effective_solar_sector reflects manual-sector-override and obstruction
    # corrections; it is what SolarEvaluator actually used (not raw geometry).
    effective_solar_sector: bool | None = None
    solar_source: str | None = None            # "sensor" | "estimate"
    obstruction_blocked: bool | None = None    # True when an obstruction zone suppressed direct exposure
    manual_sun_sector_active: bool | None = None  # True when manual sun sector override replaced raw geometry
    # Comfort Engine round (2026-06-17) - per-window (is_in_solar_sector
    # differs per window, outdoor/indoor temperature are house-wide).
    comfort_assessment: ComfortAssessment | None = None
    # Manual Override round (Step 8, 2026-06-17) - set when an active
    # override is holding the cover at a user-chosen position.
    # override_position is in HA convention (0=closed, 100=open) for
    # direct use as a sensor attribute.
    override_active: bool = False
    override_expires_at: datetime | None = None
    override_source: str | None = None
    override_position: int | None = None
    # Phase 9E: learning diagnostics — always populated from LearningStore;
    # default to empty/zero/False so Minimal-Setup (cover-only) works without
    # any learning data being present.
    last_5_transitions: list[dict] = field(default_factory=list)
    override_count_24h: int = 0
    override_count_7d: int = 0
    transition_count_24h: int = 0
    transition_count_7d: int = 0
    learning_data_available: bool = False

    @classmethod
    def unavailable(
        cls,
        state: ShadingState = ShadingState.OPEN,
        cover_position: CoverPositionObservation | None = None,
        lifecycle_state: str | None = None,
        night_active: bool = False,
        absence_active: bool = False,
        override_active: bool = False,
        override_expires_at: datetime | None = None,
        override_source: str | None = None,
        override_position: int | None = None,
        learning_diagnostics: dict | None = None,
    ) -> "WindowObservation":
        return cls(
            state=state, reason="UNAVAILABLE", reason_code="unavailable",
            next_action="NO_ACTION", guard_blocked=False, exposure=None,
            cover_position=cover_position,
            lifecycle_state=lifecycle_state, night_active=night_active, absence_active=absence_active,
            override_active=override_active,
            override_expires_at=override_expires_at,
            override_source=override_source,
            override_position=override_position,
            **(learning_diagnostics or {}),
        )


def compute_learning_diagnostics(
    store: LearningStore,
    window_id: str,
    now: datetime,
) -> dict:
    """Return the 6 Phase-9E learning diagnostic fields for one window.

    All store reads are wrapped in try/except — a LearningStore failure
    must never propagate to core shading logic.

    The returned dict can be spread directly into the WindowObservation
    constructor: ``WindowObservation(..., **compute_learning_diagnostics(...))``,
    or passed as ``learning_diagnostics=`` to ``WindowObservation.unavailable()``.
    """
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    last_5_transitions: list[dict] = []
    transition_count_24h = 0
    transition_count_7d = 0
    override_count_24h = 0
    override_count_7d = 0
    learning_data_available = False

    transitions: list = []
    overrides: list = []

    try:
        transitions = store.get_transitions(window_id)
        last_5_transitions = [
            {
                "timestamp": r.timestamp.isoformat(),
                "from": r.from_state.value,
                "to": r.to_state.value,
                "decided_by": r.decided_by,
            }
            for r in transitions[:5]
        ]
        transition_count_24h = sum(1 for r in transitions if r.timestamp >= cutoff_24h)
        transition_count_7d = sum(1 for r in transitions if r.timestamp >= cutoff_7d)
    except Exception as exc:
        # F7: debug-level (this runs every cycle per window) — a persistent
        # failure here previously left transition counts at 0, indistinguishable
        # from "genuinely no transitions yet."
        _LOGGER.debug(
            "compute_learning_diagnostics: get_transitions failed for window=%s "
            "(%s: %s)", window_id, type(exc).__name__, exc,
        )

    try:
        overrides = store.get_overrides(window_id)
        override_count_24h = sum(1 for r in overrides if r.timestamp >= cutoff_24h)
        override_count_7d = sum(1 for r in overrides if r.timestamp >= cutoff_7d)
    except Exception as exc:
        _LOGGER.debug(
            "compute_learning_diagnostics: get_overrides failed for window=%s "
            "(%s: %s)", window_id, type(exc).__name__, exc,
        )

    try:
        learning_data_available = bool(
            transitions
            or overrides
            or store.get_snapshots(window_id, limit=1)
        )
    except Exception as exc:
        _LOGGER.debug(
            "compute_learning_diagnostics: get_snapshots failed for window=%s "
            "(%s: %s)", window_id, type(exc).__name__, exc,
        )

    return {
        "last_5_transitions": last_5_transitions,
        "override_count_24h": override_count_24h,
        "override_count_7d": override_count_7d,
        "transition_count_24h": transition_count_24h,
        "transition_count_7d": transition_count_7d,
        "learning_data_available": learning_data_available,
    }
