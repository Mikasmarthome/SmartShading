"""Adaptation Application — Phase 9F17.

Applies AdaptiveProfile factors to a BehaviorConfig, producing a new
BehaviorConfig that evaluators use for the current evaluation cycle.

Design invariants
-----------------
  Pure function: apply_adaptive_profile() never mutates either input.
  A new BehaviorConfig is always created via dataclasses.replace().

  No HA dependency: importable and testable without a Home Assistant
  installation.

  Deterministic: same inputs always produce the same outputs.

  Evaluators remain unmodified: they read from BehaviorConfig fields;
  the adaptation is fully transparent to all Tier 1–5 evaluators.

  ComfortConfig is never touched: heat thresholds originate from
  ComfortConfig → BehaviorConfig via build_window_decision_input().
  Only the already-resolved BehaviorConfig field is adapted here.

  Hard clamps are always enforced — no adapted value ever falls outside
  the defined safety bounds, regardless of the AdaptiveProfile factor.

  Dead-band suppresses micro-drift: changes smaller than the deadband
  thresholds are discarded so short-lived fluctuations do not produce
  a measurable BehaviorConfig change.

Confidence gating (from Architecture Audit 9F16; updated 9F17+)
-----------------------------------------------------------------
  learning_active=False  → BehaviorConfig returned unchanged
  very_low / low / medium → BehaviorConfig returned unchanged (trace produced)
  high                   → BehaviorConfig returned unchanged (position handled by TargetPositionAdapter)
  very_high              → heat thresholds AND solar W/m² thresholds adapted

  Position adaptation per shade intensity (light / normal / strong) is handled
  by TargetPositionAdapter (engines/target_position_adapter.py) which applies
  per-window learned deltas downstream in the coordinator.

  signal_count gate is enforced by the upstream ConfidenceEngine;
  apply_adaptive_profile() trusts the confidence_level it receives.

Solar threshold adaptation (9F17+)
-----------------------------------
  AdaptiveProfile.solar_escalation_factor carries a BIDIRECTIONAL factor
  computed by AdaptationLayer from the signed solar impact signal:

    factor < 1.0 → thresholds RISE → window stays open longer / escalates later
                   (repeated uncritical outcomes: indoor temp rise < 2°C neutral)
    factor > 1.0 → thresholds FALL → window shades earlier
                   (strong solar impact: indoor temp rise > 2°C neutral)
    factor = 1.0 → no change (neutral)

  Applied at the same VERY_HIGH confidence gate as heat thresholds.
  Hard W/m² clamps and a 10 W/m² deadband prevent oscillation and
  guarantee absolute safety bounds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..engines.adaptation_layer import AdaptiveProfile
from ..models.behavior_config import BehaviorConfig

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence-level ordering (must stay in sync with ConfidenceEngine)
# ---------------------------------------------------------------------------

_LEVEL_ORDER: tuple[str, ...] = ("very_low", "low", "medium", "high", "very_high")


def _gte(level: str, threshold: str) -> bool:
    """True when *level* is at or above *threshold* in the confidence ordering."""
    try:
        return _LEVEL_ORDER.index(level) >= _LEVEL_ORDER.index(threshold)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Gate thresholds (Architecture Audit 9F16)
# ---------------------------------------------------------------------------

_GATE_POSITION:          str = "high"       # retained for backward compat; position now handled by TargetPositionAdapter
_GATE_HEAT:              str = "very_high"  # heat threshold adaptation at VERY_HIGH only
_GATE_SOLAR_THRESHOLD:   str = "very_high"  # W/m² solar threshold adaptation at VERY_HIGH only

# ---------------------------------------------------------------------------
# Hard clamps — absolute safety bounds; never exceeded regardless of factor
# ---------------------------------------------------------------------------

_HEAT_OUTDOOR_MIN: float = 20.0   # °C
_HEAT_OUTDOOR_MAX: float = 35.0   # °C
_HEAT_INDOOR_MIN:  float = 19.0   # °C
_HEAT_INDOOR_MAX:  float = 30.0   # °C
_POSITION_MIN:     int   = 60     # internal convention (0=open, 100=shaded)
_POSITION_MAX:     int   = 95

# W/m² clamps for solar entry thresholds (hard bounds; defaults: 150 / 300 / 500)
_SOLAR_LIGHT_MIN_WM2:  float = 100.0
_SOLAR_LIGHT_MAX_WM2:  float = 200.0
_SOLAR_NORMAL_MIN_WM2: float = 200.0
_SOLAR_NORMAL_MAX_WM2: float = 450.0
_SOLAR_STRONG_MIN_WM2: float = 350.0
_SOLAR_STRONG_MAX_WM2: float = 700.0

# Dead-band: changes smaller than these values are silently discarded
_HEAT_DEADBAND_C:       float = 0.5   # °C
_POSITION_DEADBAND:     int   = 2     # internal position units
_SOLAR_THRESHOLD_DEADBAND_WM2: float = 10.0  # W/m²


# ---------------------------------------------------------------------------
# AdaptationTrace — per-window, per-cycle explainability record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdaptationTrace:
    """Records what adaptation was applied this cycle and why.

    Produced alongside every call to apply_adaptive_profile(), including
    cycles where no adaptation was applied (learning_active=False, low
    confidence, dead-band suppression).  Provides a full audit trail
    regardless of whether adaptation took effect.

    All position values use internal convention (0=open, 100=shaded).
    A None on a *_factor field means that gate did not pass this cycle.
    """

    window_id: str
    computed_at_utc: datetime

    # Gate state
    learning_active:     bool
    confidence_level:    str    # "very_low" .. "very_high"
    adaptation_strength: float

    # Heat threshold adaptation (gate: VERY_HIGH)
    heat_outdoor_original: float | None   # value from BehaviorConfig before adaptation
    heat_outdoor_adapted:  float | None   # value passed to evaluators this cycle
    heat_outdoor_factor:   float | None   # None = gate did not pass or dead-band

    heat_indoor_original: float | None
    heat_indoor_adapted:  float | None
    heat_indoor_factor:   float | None

    # Position adaptation (gate: HIGH) — handled by TargetPositionAdapter
    shade_position_original: int          # normal_shade_position before adaptation
    shade_position_adapted:  int          # value passed to evaluators this cycle
    shade_position_factor:   float | None # None = gate did not pass or dead-band

    # Solar W/m² threshold adaptation (gate: VERY_HIGH)
    light_shade_threshold_original:  float
    light_shade_threshold_adapted:   float
    normal_shade_threshold_original: float
    normal_shade_threshold_adapted:  float
    strong_shade_threshold_original: float
    strong_shade_threshold_adapted:  float
    solar_escalation_factor_applied:  float | None  # None = gate did not pass or dead-band

    # Exposure factor — recorded for diagnostics; used to compute solar_escalation_factor
    exposure_factor_recorded:    float
    exposure_adaptation_applied: bool

    # Human-readable summary (for logs / diagnostics)
    reason: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _adapt_threshold(
    original: float | None,
    factor: float,
    clamp_min: float,
    clamp_max: float,
    deadband: float,
) -> float | None:
    """Apply *factor* to a heat threshold value.

    Inverse relationship: factor > 1.0 → lower threshold (more aggressive
    heat protection).  Returns original when within dead-band or when
    original is None.  Result is rounded to one decimal place.
    """
    if original is None:
        return None
    adapted = max(clamp_min, min(clamp_max, original / factor))
    if abs(adapted - original) < deadband:
        return original
    return round(adapted, 1)


def _adapt_wm2_threshold(
    original: float,
    factor: float,
    clamp_min: float,
    clamp_max: float,
    deadband: float,
) -> float:
    """Apply bidirectional solar threshold adaptation to a W/m² value.

    Inverse relationship:
      factor > 1.0 → lower threshold (shade earlier when solar impact is HIGH)
      factor < 1.0 → higher threshold (shade later when solar impact is LOW/uncritical)

    Returns original when within dead-band.  Result rounded to 1 decimal place.
    """
    adapted = max(clamp_min, min(clamp_max, original / factor))
    if abs(adapted - original) < deadband:
        return original
    return round(adapted, 1)


def _adapt_position(
    original: int,
    factor: float,
    clamp_min: int,
    clamp_max: int,
    deadband: int,
) -> int:
    """Apply *factor* to a shade position value.

    Direct relationship: factor > 1.0 → higher position (more shading).
    Returns original when within dead-band.
    """
    adapted = max(clamp_min, min(clamp_max, round(original * factor)))
    if abs(adapted - original) < deadband:
        return original
    return adapted


def _build_reason(
    *,
    learning_active: bool,
    confidence_level: str,
    adaptation_strength: float,
    heat_gate: bool,
    heat_outdoor_factor: float | None,
    solar_gate: bool,
    solar_escalation_factor: float | None,
    exposure_factor: float,
) -> str:
    if not learning_active:
        return "learning_active=False"
    parts = [f"confidence={confidence_level}", f"strength={adaptation_strength:.2f}"]
    if heat_outdoor_factor is not None:
        parts.append(f"heat_factor={heat_outdoor_factor:.3f}")
    if not heat_gate:
        parts.append("no_gate_passed")
    if heat_gate and heat_outdoor_factor is None:
        parts.append("heat_in_deadband")
    if solar_gate and solar_escalation_factor is not None:
        parts.append(f"solar_wm2_factor={solar_escalation_factor:.3f}")
    if solar_gate and solar_escalation_factor is None:
        parts.append("solar_wm2_in_deadband")
    parts.append(f"exposure={exposure_factor:.3f}(diagnostic)")
    parts.append("position=target_position_adapter")
    return ", ".join(parts)


def _trace_inactive(
    bc: BehaviorConfig,
    profile: AdaptiveProfile,
    window_id: str,
    ts: datetime,
) -> AdaptationTrace:
    """Build a no-op AdaptationTrace when learning_active=False."""
    return AdaptationTrace(
        window_id=window_id,
        computed_at_utc=ts,
        learning_active=False,
        confidence_level=profile.confidence_level,
        adaptation_strength=profile.adaptation_strength,
        heat_outdoor_original=bc.heat_outdoor_threshold_c,
        heat_outdoor_adapted=bc.heat_outdoor_threshold_c,
        heat_outdoor_factor=None,
        heat_indoor_original=bc.heat_indoor_threshold_c,
        heat_indoor_adapted=bc.heat_indoor_threshold_c,
        heat_indoor_factor=None,
        shade_position_original=bc.normal_shade_position,
        shade_position_adapted=bc.normal_shade_position,
        shade_position_factor=None,
        light_shade_threshold_original=bc.light_shade_threshold_wm2,
        light_shade_threshold_adapted=bc.light_shade_threshold_wm2,
        normal_shade_threshold_original=bc.normal_shade_threshold_wm2,
        normal_shade_threshold_adapted=bc.normal_shade_threshold_wm2,
        strong_shade_threshold_original=bc.strong_shade_threshold_wm2,
        strong_shade_threshold_adapted=bc.strong_shade_threshold_wm2,
        solar_escalation_factor_applied=None,
        exposure_factor_recorded=profile.exposure_sensitivity_factor,
        exposure_adaptation_applied=False,
        reason="learning_active=False",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_adaptive_profile(
    behavior_config: BehaviorConfig,
    adaptive_profile: AdaptiveProfile,
    *,
    window_id: str = "",
    now: datetime | None = None,
) -> tuple[BehaviorConfig, AdaptationTrace]:
    """Apply AdaptiveProfile factors to *behavior_config*.

    Returns a (BehaviorConfig, AdaptationTrace) tuple.  The returned
    BehaviorConfig is a new object — the original is never modified.
    The AdaptationTrace documents every gate decision and which values
    were actually passed to evaluators.

    Parameters
    ----------
    behavior_config:
        Pre-resolved BehaviorConfig for one evaluation cycle.  Produced by
        build_window_decision_input() and consumed by Tier 1–5 evaluators.
    adaptive_profile:
        AdaptiveProfile from the PREVIOUS evaluation cycle's Learning Pipeline
        (1-cycle lag is intentional and correct for learning-based adaptation).
    window_id:
        Used only for AdaptationTrace identification — not used for logic.
    now:
        Timestamp for the AdaptationTrace.  Defaults to UTC now when None.

    Returns
    -------
    tuple[BehaviorConfig, AdaptationTrace]
        The first element is the BehaviorConfig the evaluators should use.
        The second element is the full explainability trace.
    """
    ts = now if now is not None else datetime.now(timezone.utc)

    if not adaptive_profile.learning_active:
        return behavior_config, _trace_inactive(behavior_config, adaptive_profile, window_id, ts)

    confidence = adaptive_profile.confidence_level
    strength   = adaptive_profile.adaptation_strength
    heat_f     = adaptive_profile.heat_sensitivity_factor
    exp_f      = adaptive_profile.exposure_sensitivity_factor
    solar_tf   = adaptive_profile.solar_escalation_factor

    heat_gate  = _gte(confidence, _GATE_HEAT)
    solar_gate = _gte(confidence, _GATE_SOLAR_THRESHOLD)

    # ------------------------------------------------------------------
    # Heat threshold adaptation (VERY_HIGH gate only)
    # ------------------------------------------------------------------
    new_heat_outdoor = behavior_config.heat_outdoor_threshold_c
    new_heat_indoor  = behavior_config.heat_indoor_threshold_c
    heat_outdoor_factor_applied: float | None = None
    heat_indoor_factor_applied:  float | None = None

    if heat_gate:
        adapted_outdoor = _adapt_threshold(
            behavior_config.heat_outdoor_threshold_c,
            heat_f,
            _HEAT_OUTDOOR_MIN, _HEAT_OUTDOOR_MAX, _HEAT_DEADBAND_C,
        )
        if adapted_outdoor != behavior_config.heat_outdoor_threshold_c:
            new_heat_outdoor = adapted_outdoor
            heat_outdoor_factor_applied = heat_f

        adapted_indoor = _adapt_threshold(
            behavior_config.heat_indoor_threshold_c,
            heat_f,
            _HEAT_INDOOR_MIN, _HEAT_INDOOR_MAX, _HEAT_DEADBAND_C,
        )
        if adapted_indoor != behavior_config.heat_indoor_threshold_c:
            new_heat_indoor = adapted_indoor
            heat_indoor_factor_applied = heat_f

    # ------------------------------------------------------------------
    # Solar W/m² threshold adaptation (VERY_HIGH gate only)
    # ------------------------------------------------------------------
    new_light  = behavior_config.light_shade_threshold_wm2
    new_normal = behavior_config.normal_shade_threshold_wm2
    new_strong = behavior_config.strong_shade_threshold_wm2
    solar_escalation_factor_applied: float | None = None
    exposure_adaptation_applied = False

    if solar_gate:
        adapted_light  = _adapt_wm2_threshold(behavior_config.light_shade_threshold_wm2,  solar_tf, _SOLAR_LIGHT_MIN_WM2,  _SOLAR_LIGHT_MAX_WM2,  _SOLAR_THRESHOLD_DEADBAND_WM2)
        adapted_normal = _adapt_wm2_threshold(behavior_config.normal_shade_threshold_wm2, solar_tf, _SOLAR_NORMAL_MIN_WM2, _SOLAR_NORMAL_MAX_WM2, _SOLAR_THRESHOLD_DEADBAND_WM2)
        adapted_strong = _adapt_wm2_threshold(behavior_config.strong_shade_threshold_wm2, solar_tf, _SOLAR_STRONG_MIN_WM2, _SOLAR_STRONG_MAX_WM2, _SOLAR_THRESHOLD_DEADBAND_WM2)

        any_changed = (
            adapted_light  != behavior_config.light_shade_threshold_wm2
            or adapted_normal != behavior_config.normal_shade_threshold_wm2
            or adapted_strong != behavior_config.strong_shade_threshold_wm2
        )
        if any_changed:
            new_light  = adapted_light
            new_normal = adapted_normal
            new_strong = adapted_strong
            solar_escalation_factor_applied = solar_tf
            exposure_adaptation_applied = True

    # Position adaptation is handled by TargetPositionAdapter in the coordinator.

    # ------------------------------------------------------------------
    # Build adapted BehaviorConfig
    # ------------------------------------------------------------------
    adapted_bc = replace(
        behavior_config,
        heat_outdoor_threshold_c=new_heat_outdoor,
        heat_indoor_threshold_c=new_heat_indoor,
        light_shade_threshold_wm2=new_light,
        normal_shade_threshold_wm2=new_normal,
        strong_shade_threshold_wm2=new_strong,
    )

    # ------------------------------------------------------------------
    # Build AdaptationTrace
    # ------------------------------------------------------------------
    reason = _build_reason(
        learning_active=True,
        confidence_level=confidence,
        adaptation_strength=strength,
        heat_gate=heat_gate,
        heat_outdoor_factor=heat_outdoor_factor_applied,
        solar_gate=solar_gate,
        solar_escalation_factor=solar_escalation_factor_applied,
        exposure_factor=exp_f,
    )

    trace = AdaptationTrace(
        window_id=window_id,
        computed_at_utc=ts,
        learning_active=True,
        confidence_level=confidence,
        adaptation_strength=strength,
        heat_outdoor_original=behavior_config.heat_outdoor_threshold_c,
        heat_outdoor_adapted=new_heat_outdoor,
        heat_outdoor_factor=heat_outdoor_factor_applied,
        heat_indoor_original=behavior_config.heat_indoor_threshold_c,
        heat_indoor_adapted=new_heat_indoor,
        heat_indoor_factor=heat_indoor_factor_applied,
        shade_position_original=behavior_config.normal_shade_position,
        shade_position_adapted=behavior_config.normal_shade_position,
        shade_position_factor=None,
        light_shade_threshold_original=behavior_config.light_shade_threshold_wm2,
        light_shade_threshold_adapted=new_light,
        normal_shade_threshold_original=behavior_config.normal_shade_threshold_wm2,
        normal_shade_threshold_adapted=new_normal,
        strong_shade_threshold_original=behavior_config.strong_shade_threshold_wm2,
        strong_shade_threshold_adapted=new_strong,
        solar_escalation_factor_applied=solar_escalation_factor_applied,
        exposure_factor_recorded=exp_f,
        exposure_adaptation_applied=exposure_adaptation_applied,
        reason=reason,
    )

    return adapted_bc, trace
