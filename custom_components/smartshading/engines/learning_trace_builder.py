"""Input/source provenance + learning-trace builder — LE 2.0 / Phase P11.3+P11.5.

Read-only, HA-free (duck-typed coordinator).  Aggregates ALREADY-COMPUTED runtime
values + existing per-window diagnostics getters into the consolidated contract
sections: inputs / source_provenance / position_learning / strategy_learning /
learning_authority.  Never re-runs an evaluator, never recomputes a decision,
never mutates state.  Honest status model: missing data → not_recorded (never
fabricated, never silently 0/false).
"""
from __future__ import annotations

# common source-status model
S_MEASURED = "measured"
S_ESTIMATED = "estimated"
S_FALLBACK = "fallback"
S_DERIVED = "derived"
S_MISSING = "missing"
S_INVALID = "invalid"
S_STALE = "stale"
S_BLOCKED = "blocked"
S_NOT_CONFIGURED = "not_configured"
S_NOT_RECORDED = "not_recorded"

POSITION_INTENSITIES = ("light", "normal", "strong")
STRATEGY_FAMILIES = (
    "entry_threshold", "exit_threshold", "entry_timing", "exit_timing",
    "tier_choice", "minimum_hold", "hysteresis",
)
# Code-grounded family units (FAMILY_BOUNDS in models/strategy_learning.py).
STRATEGY_FAMILY_UNITS = {
    "entry_threshold": "w_m2", "exit_threshold": "w_m2",
    "entry_timing": "minutes", "exit_timing": "minutes",
    "minimum_hold": "minutes", "hysteresis": "w_m2",
    "tier_choice": "semantic_tier",
}


def _call(coord, name, *args):
    fn = getattr(coord, name, None)
    if fn is None:
        return None
    try:
        return fn(*args)
    except Exception:
        return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ledger_state(coord, namespace: str) -> dict:
    integ = getattr(coord, "_ledger_integrity", None)
    status = getattr(integ, namespace, "unknown") if integ is not None else "unknown"
    safe = status in ("valid", "missing")
    return {"namespace": namespace, "status": status, "safe_for_learning": safe,
            "experiments_allowed": safe, "adoptions_allowed": safe}


# ---------------------------------------------------------------------------
# input / source provenance
# ---------------------------------------------------------------------------

def build_input_provenance(coord, window_id: str) -> dict:
    """Current per-window input/source provenance from the real input path
    (_read_weather_inputs + this-cycle snapshot + forecast adapter)."""
    window = getattr(coord, "windows", {}).get(window_id)
    zone_id = getattr(window, "zone_id", None)
    out: dict = {
        "window_id": window_id,
        "config_generation": _call(coord, "_thermal_config_generation", zone_id) or 0,
    }
    wi = _call(coord, "_read_weather_inputs")
    indoor = _call(coord, "_read_indoor_temperature_for_zone", zone_id) if zone_id else None
    out["indoor_temperature"] = {
        "value_c": _num(indoor),
        "source_status": (S_MEASURED if indoor is not None
                          else (S_NOT_CONFIGURED
                                if not getattr(coord, "_indoor_temperature_sensor_ids", None)
                                else S_MISSING)),
        "is_estimated": False,
    }
    out["outdoor_temperature"] = {
        "value_c": _num(getattr(wi, "outdoor_temperature", None)),
        "source_status": (S_MEASURED if getattr(wi, "outdoor_temperature", None) is not None
                          else S_MISSING),
        "is_estimated": False,
    }
    out["weather"] = {
        "configured": getattr(coord, "_weather_entity_id", None) is not None,
        "available": getattr(wi, "weather_condition", None) is not None,
        "cloud_cover": _num(getattr(wi, "cloud_cover", None)),
        "cloud_cover_status": (S_MEASURED if getattr(wi, "cloud_cover", None) is not None
                               else (S_NOT_CONFIGURED
                                     if getattr(coord, "_cloud_cover_sensor_id", None) is None
                                     else S_MISSING)),
    }
    out["solar"] = build_solar_provenance(coord, window_id, wi)
    out["threshold_provenance"] = build_threshold_provenance(coord, window_id)
    out["forecast"] = build_forecast_provenance(coord)
    return out


def build_solar_provenance(coord, window_id: str, wi=None) -> dict:
    """Solar-transformation provenance from values ALREADY computed this cycle
    (WindowExposure + base/source captured at the real exposure path).  Chain:
    base_solar → ×incidence(direct_radiation_factor)=theoretical → ×learned
    ×seasonal = effective_exposure (the authoritative evaluator input).  There is
    no separate cloud factor in the exposure engine: cloud is reflected in the
    source value (measured) or folded once into the estimate (fallback) — never
    applied twice.  Missing intermediates are individually not_recorded."""
    wi = wi if wi is not None else _call(coord, "_read_weather_inputs")
    raw = _num(getattr(wi, "solar_radiation", None)) if wi is not None else None
    configured = getattr(coord, "_solar_radiation_sensor_id", None) is not None
    measured_valid = raw is not None
    snap = getattr(coord, "_cycle_solar_provenance", {}).get(window_id)
    out = {
        "configured_solar_source_category": "sensor" if configured else "none",
        "selected_solar_source_type": (S_MEASURED if measured_valid
                                       else (S_FALLBACK if configured else S_NOT_CONFIGURED)),
        "selected_solar_source_status": (S_MEASURED if measured_valid
                                         else (S_MISSING if configured else S_NOT_CONFIGURED)),
        "raw_measured_solar_w_m2": raw,
        "fallback_used": (configured and not measured_valid),
        "forecast_used_for_current_measurement": False,
    }
    if snap is None:
        # No exposure captured this cycle → intermediates honestly not_recorded.
        out.update({
            "base_solar_recording_status": S_NOT_RECORDED,
            "incidence_recording_status": S_NOT_RECORDED,
            "effective_exposure_recording_status": S_NOT_RECORDED,
            "cloud_adjustment_count": None,
        })
        return out
    exp = snap.get("exposure")
    source = snap.get("solar_source")
    cloud_applied = (source == "estimate")  # cloud folded into the estimate path only
    # Authoritative source selection (solar_source.py): surface measured vs the
    # estimate that was (or was not) used, the source quality, and the fallback /
    # cloud-not-applied reasons so a user/support can see exactly which value drove
    # the decision and why (answers "where did the weather value come from").
    sel = snap.get("solar_selection")
    if sel is not None:
        out.update({
            "selected_solar_source": getattr(sel, "source", None),
            "solar_source_quality": getattr(sel, "quality", None),
            "measured_solar_w_m2": _num(getattr(sel, "measured_wm2", None)),
            "measured_solar_valid": getattr(sel, "measured_valid", None),
            "estimated_solar_w_m2": _num(getattr(sel, "estimated_wm2", None)),
            "solar_fallback_reason": getattr(sel, "fallback_reason", None),
            "cloud_not_applied_reason": getattr(sel, "cloud_not_applied_reason", None),
        })
    out.update({
        "base_solar_value_w_m2": _num(snap.get("base_solar_wm2")),
        "base_solar_source_type": (S_ESTIMATED if source == "estimate" else S_MEASURED),
        "cloud_adjustment_applied": cloud_applied,
        # 1 in the estimate path; 0 for measured (source already reflects cloud).
        "cloud_adjustment_count": 1 if cloud_applied else 0,
        "incidence_factor": _num(getattr(exp, "direct_radiation_factor", None)),
        "incidence_status": (S_DERIVED if exp is not None else S_NOT_RECORDED),
        "geometry_adjusted_solar_w_m2": _num(getattr(exp, "theoretical_exposure", None)),
        "learned_solar_impact_factor": _num(getattr(exp, "learned_solar_impact_factor", None)),
        "seasonal_factor": _num(getattr(exp, "seasonal_factor", None)),
        "sun_azimuth_deg": _num(getattr(exp, "sun_azimuth", None)),
        "sun_elevation_deg": _num(getattr(exp, "sun_elevation", None)),
        "window_azimuth_delta_deg": _num(getattr(exp, "azimuth_delta_deg", None)),
        "manual_sector_result": ("inside" if getattr(exp, "is_in_tolerance_window", None)
                                 else ("outside" if exp is not None else S_NOT_RECORDED)),
        "above_horizon": getattr(exp, "is_above_horizon", None),
        "direct_exposure_blocked": (
            bool(exp is not None and (not getattr(exp, "is_above_horizon", True)
                                      or not getattr(exp, "is_in_tolerance_window", True)))),
        # obstruction factor is not modelled in WindowExposure → not_recorded.
        "obstruction_recording_status": S_NOT_RECORDED,
        # AUTHORITATIVE effective exposure = exact value passed to the evaluators.
        "effective_exposure_w_m2": _num(getattr(exp, "effective_exposure", None)),
        "effective_exposure_status": (S_DERIVED if exp is not None else S_NOT_RECORDED),
    })
    # Glare distinction (P-solar): separate "geometrically in sector" from
    # "meaningfully lit".  Glare fires only when in-sector AND effective exposure
    # >= glare_min_exposure_wm2 (authoritative source).  Surfaces why glare was
    # active or suppressed so the in-sector / low-exposure real case is visible.
    _glare_min = _num(snap.get("glare_min_exposure_wm2"))
    _glare_enabled = snap.get("glare_protection_enabled")
    _eff = _num(getattr(exp, "effective_exposure", None))
    _in_sector = bool(getattr(exp, "is_in_tolerance_window", False))
    _sufficient = (_eff is not None and _glare_min is not None and _eff >= _glare_min)
    if _glare_min is not None:
        out.update({
            "geometrically_in_solar_sector": _in_sector,
            "glare_protection_enabled": bool(_glare_enabled),
            "glare_min_exposure_w_m2": _glare_min,
            "glare_exposure_sufficient": _sufficient,
            "glare_active": bool(_glare_enabled and _in_sector and _sufficient),
            "glare_suppressed_reason": (
                None if (_glare_enabled and _in_sector and _sufficient)
                else ("glare_disabled" if not _glare_enabled
                      else ("not_in_sector" if not _in_sector
                            else "below_min_exposure"))),
        })
    return out


def build_threshold_provenance(coord, window_id: str) -> dict:
    """Entry/exit exposure-threshold provenance connected to the REAL evaluator
    input.  Entry thresholds (light/normal/strong) come from the cycle's
    SolarThresholdResolution + captured configured/strategy values; the effective
    value equals the threshold the evaluator used at runtime.  Exit thresholds are
    surfaced from the strategy exit_threshold family; the exit-comparison value is
    consumed by the state guard, not this snapshot → not_recorded where absent."""
    snap = getattr(coord, "_cycle_solar_provenance", {}).get(window_id)
    res = getattr(coord, "_cycle_solar_resolution", {}).get(window_id)
    if snap is None or res is None:
        return {"recording_status": S_NOT_RECORDED}
    exp = snap.get("exposure")
    strat_delta = _num(snap.get("strategy_threshold_delta_wm2")) or 0.0
    entry = {}
    for tier in ("light", "normal", "strong"):
        configured = _num(snap.get(f"configured_{tier}_wm2"))
        learned = _num(getattr(res, f"applied_learned_delta_{tier}", None)) or 0.0
        forecast = _num(getattr(res, "applied_forecast_delta", None)) or 0.0
        effective = _num(getattr(res, f"effective_{tier}_wm2", None))
        entry[tier] = {
            "configured_entry_threshold_w_m2": configured,
            "entry_learned_delta_w_m2": learned,
            "entry_forecast_delta_w_m2": forecast,
            "entry_strategy_delta_w_m2": strat_delta,
            # equals the actual evaluator threshold input (the resolver output that
            # was written into the adapted BehaviorConfig used for tier evaluation).
            "effective_entry_threshold_w_m2": effective,
            "entry_threshold_used_by_evaluator_w_m2": effective,
        }
    return {
        "exposure_value_compared_w_m2": _num(getattr(exp, "effective_exposure", None)),
        "entry_thresholds": entry,
        "forecast_trust_level": getattr(res, "forecast_trust_level", None),
        # exit-threshold provenance: surfaced from the strategy family; the runtime
        # exit comparison lives in the state guard and is not captured here.
        "exit_threshold_recording_status": S_NOT_RECORDED,
    }


def build_forecast_provenance(coord) -> dict:
    """Forecast provenance — availability is NOT usage; current control never uses
    forecast as a measurement.  Forecast may only shift precautionary solar entry
    thresholds (bounded), and only when trust is high and a current forecast
    snapshot exists; it never replaces a measured value or overrides safety."""
    from . import forecast_strategy_modifier as _fsm
    adapter = getattr(coord, "forecast_adapter", None)
    store_loaded = getattr(coord, "_forecast_learning_store", None) is not None
    diag = getattr(adapter, "restore_diagnostics", {}) if adapter is not None else {}
    out = {
        "forecast_configured": getattr(coord, "_weather_entity_id", None) is not None,
        "forecast_store_loaded": store_loaded,
        "forecast_provider_match": (diag or {}).get("provider_fingerprint_match"),
        "forecast_used_for_current_measurement": False,
        "forecast_used_for_current_control": False,
        "forecast_used_for_planning": store_loaded,
        # Bounded threshold-only usage; measured solar stays authoritative.
        "forecast_role": "threshold_bias_only",
        "forecast_max_threshold_delta_w_m2": _fsm.FORECAST_MAX_DELTA_WM2,
        "forecast_horizon_lookback_min": _fsm._LOOKBACK_MINUTES,
        "forecast_horizon_lookahead_h": _fsm._LOOKAHEAD_HOURS,
        # Availability is NOT usage: a forecast can be available yet not applied.
        "forecast_available": store_loaded,
    }
    # This cycle's forecast strategy modifier (read-only): trust, applied delta,
    # reason (why applied or not), and the forecast fields used.
    fm = getattr(coord, "_cycle_forecast_modifier", None)
    if fm is not None:
        out.update({
            "forecast_modifier_applied": bool(getattr(fm, "applied", False)),
            "forecast_trust_score": _num(getattr(fm, "trust_score", None)),
            "forecast_threshold_delta_w_m2": _num(getattr(fm, "threshold_delta_wm2", None)),
            "forecast_reason": getattr(fm, "reason", None),
            "forecast_solar_w_m2": _num(getattr(fm, "forecast_solar_wm2", None)),
            "forecast_cloud_pct": _num(getattr(fm, "forecast_cloud_pct", None)),
            # Fields actually consulted by the modifier this cycle.
            "forecast_fields_used": [
                f for f, v in (("solar_irradiance", getattr(fm, "forecast_solar_wm2", None)),
                               ("cloud_coverage", getattr(fm, "forecast_cloud_pct", None)))
                if v is not None],
        })
    else:
        # Keep the section shape stable so a support case always shows the
        # trust / threshold-delta fields (here: not recorded this cycle).
        out["forecast_modifier_applied"] = False
        out["forecast_trust_score"] = None
        out["forecast_threshold_delta_w_m2"] = None
        out["forecast_reason"] = "no_forecast_modifier"
    return out


# ---------------------------------------------------------------------------
# position learning trace
# ---------------------------------------------------------------------------

def _base_resolution(window, intensity: str):
    attr = f"{intensity}_shade_position"
    override = getattr(window, attr, None) if window is not None else None
    if override is not None:
        return override, "window_override"
    return None, "zone_config_or_default"


def build_position_learning_trace(coord, window_id: str) -> dict:
    window = getattr(coord, "windows", {}).get(window_id)
    adopt = _call(coord, "adoption_diagnostics", window_id) or {}
    adopt_int = adopt.get("intensities", {})
    cycle_applied = getattr(coord, "_cycle_adoption_applied", {}).get(window_id, {})
    out: dict = {"intensities": {}, "ledger_integrity_state": _ledger_state(coord, "position")}
    for intensity in POSITION_INTENSITIES:
        base, base_src = _base_resolution(window, intensity)
        a = adopt_int.get(intensity, {})
        applied_entry = cycle_applied.get(intensity)
        out["intensities"][intensity] = {
            "intensity": intensity,
            "resolved_base_position_ha": base,
            "base_resolution_source": base_src,
            "active_adoption": {
                "present": bool(a),
                "adoption_id_internal": a.get("adoption_id"),
                "status": a.get("adoption_status"),
                "adopted_delta_ha": a.get("adopted_delta_ha"),
                "effective_delta_ha": a.get("adopted_delta_ha"),
                "suspended": a.get("suspended"),
                "gate_reason": a.get("current_gate_reason"),
                "rollback_reason": a.get("rollback_reason"),
                "monitoring_outcome_count": a.get("monitoring_outcome_count"),
                "monitoring_degraded_count": a.get("monitoring_degraded_count"),
                "cooldown_remaining_days": a.get("cooldown_remaining_days"),
                "source_experiment_count": a.get("source_experiment_count"),
                "confidence": a.get("adoption_confidence"),
                "reliability": a.get("adoption_reliability"),
            },
            # experiment delta is kept DISTINCT from adopted delta; only one
            # authority influences the effective position (never summed).
            "experiment_delta_ha": _position_experiment_delta(coord, window_id, intensity),
            "applied_this_cycle": applied_entry is not None,
            "applied_source": ("adoption" if applied_entry is not None else None),
            "blocked_reason": (a.get("current_gate_reason") if a.get("suspended") else None),
        }
    return out


def _position_experiment_delta(coord, window_id: str, intensity: str):
    exp_map = getattr(coord, "_experiments_active", {})
    for _zid, exp in exp_map.items():
        if (getattr(exp, "window_id", None) == window_id
                and getattr(exp, "intensity_level", None) == intensity):
            return _num(getattr(exp, "experiment_parameter_target_ha", None))
    return None


# ---------------------------------------------------------------------------
# strategy learning trace
# ---------------------------------------------------------------------------

def build_strategy_learning_trace(coord, window_id: str) -> dict:
    sad = _call(coord, "strategy_adoption_diagnostics", window_id) or {}
    fam_map = sad.get("families", {})
    cycle_applied = getattr(coord, "_cycle_strategy_applied", {}).get(window_id, {})
    out: dict = {"families": {}, "ledger_integrity_state": _ledger_state(coord, "strategy")}
    for family in STRATEGY_FAMILIES:
        a = fam_map.get(family, {})
        applied = family in cycle_applied
        out["families"][family] = {
            "family": family,
            "unit": STRATEGY_FAMILY_UNITS[family],
            "context_family": a.get("context_family"),
            "adopted_delta": a.get("adopted_delta"),
            "effective_learning_delta": a.get("adopted_delta"),
            "effective_value": a.get("effective_value"),
            "active_adoption": {
                "present": bool(a),
                "adoption_id_internal": a.get("adoption_id"),
                "status": a.get("adoption_status"),
                "suspended": a.get("suspended"),
                "gate_reason": a.get("current_gate_reason"),
                "rollback_reason": a.get("rollback_reason"),
                "monitoring_count": a.get("monitoring_count"),
                "degraded_count": a.get("degraded_count"),
                "cooldown_remaining_days": a.get("cooldown_remaining_days"),
                "confidence": a.get("confidence"),
                "reliability": a.get("reliability"),
            },
            "applied_this_decision": applied,
            "applied_source": ("adoption" if applied else None),
            "blocked_reason": (a.get("current_gate_reason") if a.get("suspended") else None),
        }
    return out


# ---------------------------------------------------------------------------
# learning authority matrix
# ---------------------------------------------------------------------------

def build_learning_authority(coord, window_id: str) -> dict:
    window = getattr(coord, "windows", {}).get(window_id)
    zone_id = getattr(window, "zone_id", None)
    eff = _call(coord, "effective_zone_execution", zone_id) if zone_id else None
    learning = bool(getattr(eff, "learning_enabled", False))
    active = bool(getattr(eff, "active_control_enabled", False))
    pos_safe = _ledger_state(coord, "position")["safe_for_learning"]
    strat_safe = _ledger_state(coord, "strategy")["safe_for_learning"]
    pos_applied = window_id in getattr(coord, "_cycle_adoption_applied", {})
    strat_applied = window_id in getattr(coord, "_cycle_strategy_applied", {})
    blocking: list = []
    if not learning:
        blocking.append("learning_mode_off")
    if not active:
        blocking.append("active_control_off")
    if not pos_safe:
        blocking.append("position_ledger_unsafe")
    if not strat_safe:
        blocking.append("strategy_ledger_unsafe")
    return {
        "learning_enabled": learning,
        "active_control_enabled": active,
        "position_experiments_allowed": learning and active and pos_safe,
        "position_adoptions_allowed": learning and pos_safe,
        "strategy_experiments_allowed": learning and active and strat_safe,
        "strategy_adoptions_allowed": learning and strat_safe,
        "position_adoption_applied": pos_applied,
        "strategy_adoption_applied": strat_applied,
        "blocking_reasons": blocking,
    }
