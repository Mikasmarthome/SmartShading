"""Privacy-safe Support Export for SmartShading — v1.0 System Area.

Aggregates learning data from ALL zone entries into a single privacy-safe JSON
document.  Extends the per-forecast export in learning_export.py to cover the
full LearningStore (transitions, overrides, snapshots, outcomes) across zones.

Design invariants
-----------------
  No HA import — pure Python.  All HA interactions happen in the button entity
  (entities/button.py) which calls build_global_learning_export().

  Privacy-first:
    - Zone names: OMITTED (pseudonymized as zone_ref)
    - Window names: OMITTED (pseudonymized as window_ref)
    - Entry IDs: hashed to stable short refs (e.g. "entry_a3b2")
    - Zone IDs / Window IDs: stable within one export, never raw
    - Cover entity IDs: NEVER included
    - Weather entity IDs: NEVER included
    - Person / presence entity IDs: NEVER included
    - Individual record timestamps: NEVER included
    - Raw learning records: NEVER included
    - Local file paths: NEVER included
    - IP addresses: NEVER included
    - Position values: HA convention (0=closed, 100=open), aggregates only

  Allowed:
    - Counts of records per type (aggregated)
    - Forecast trust scores / MAE / MBE (already privacy-safe per learning_export.py)
    - Timestamps: only export metadata timestamp (generated_at_utc)
    - Zone count, window count (structural metadata)

Format (format_version 2)
--------------------------
{
  "format_version": 2,
  "support_export_schema_version": 1,
  "export_type": "smartshading_support_export",
  "domain": "smartshading",
  "generated_at_utc": "...",
  "zones_count": N,
  "zones": [
    {
      "zone_ref": "zone_1",
      "entry_ref": "entry_a3b2",
      "windows_count": M,
      "forecast_learning": { ... },
      "target_adaptation_summary": { ... },
      "windows": [
        {
          "window_ref": "window_1",
          "learning_store": {
            "transitions_count": ...,
            "overrides_count": ...,
            "snapshots_count": ...,
            "outcomes_count": ...,
            "has_data": true|false
          }
        }
      ]
    }
  ]
}

Breaking change from format_version 1: forecast_learning moved from per-window
to per-zone level (it is zone-scoped, not per-window).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from .learning_export import build_learning_export

_LOGGER = logging.getLogger(__name__)

EXPORT_FORMAT_VERSION: int = 2
EXPORT_TYPE: str = "smartshading_support_export"
SUPPORT_EXPORT_SCHEMA_VERSION: int = 2


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _short_ref(raw_id: str) -> str:
    """Stable 6-char hex ref for a raw ID (entry_id, zone_id, etc.).

    The same raw_id always produces the same ref within one Python process
    session.  The hash is NOT reversible — it does not expose the original ID.
    """
    return hashlib.blake2b(raw_id.encode(), digest_size=3).hexdigest()


def _build_learning_store_section(learning_store: Any | None, window_id: str) -> dict:
    """Aggregate counts from a LearningStore for one window.  Never raises."""
    if learning_store is None:
        return {
            "transitions_count": 0,
            "overrides_count": 0,
            "snapshots_count": 0,
            "outcomes_count": 0,
            "has_data": False,
        }
    try:
        t_count = len(learning_store.get_transitions(window_id))
        o_count = len(learning_store.get_overrides(window_id))
        s_count = len(learning_store.get_snapshots(window_id))
        oc_count = len(learning_store.get_outcomes(window_id))
    except Exception:
        _LOGGER.warning(
            "SmartShading: zone_learning_export: error reading LearningStore for window"
        )
        return {
            "transitions_count": 0,
            "overrides_count": 0,
            "snapshots_count": 0,
            "outcomes_count": 0,
            "has_data": False,
        }

    has_data = (t_count + o_count + s_count + oc_count) > 0
    return {
        "transitions_count": t_count,
        "overrides_count": o_count,
        "snapshots_count": s_count,
        "outcomes_count": oc_count,
        "has_data": has_data,
    }


_ERROR_CATEGORY_MAP: dict[str, str] = {
    "not_found": "service_not_found",
    "unavailable": "entity_unavailable",
    "invalid": "invalid_target",
    "timeout": "timeout",
}


def _error_category(raw: str | None) -> str | None:
    """Convert a raw exception string to a privacy-safe error category."""
    if raw is None:
        return None
    lower = raw.lower()
    for key, cat in _ERROR_CATEGORY_MAP.items():
        if key in lower:
            return cat
    return "unknown_error"


def _build_window_runtime_section(
    window_diag: Any | None,
) -> dict:
    """Build the per-window runtime execution section from WindowExecutionDiagnostics.

    Privacy: no entity IDs, raw errors sanitized to categories.
    """
    if window_diag is None:
        return {"available": False}
    try:
        assumed_ha: int | None = None
        assumed_int = getattr(window_diag, "assumed_position_internal", None)
        if assumed_int is not None:
            assumed_ha = 100 - assumed_int

        return {
            "available": True,
            "active_control": getattr(window_diag, "active_control_enabled", None),
            "learning_mode": getattr(window_diag, "learning_enabled", None),
            "behavior_mode": getattr(window_diag, "execution_mode", None),
            "cover_available": getattr(window_diag, "cover_available", None),
            "current_position_ha": getattr(window_diag, "actual_position_ha", None),
            "assumed_position_ha": assumed_ha,
            "target_position_ha": getattr(window_diag, "target_position_ha", None),
            "shading_state_decided_by": getattr(window_diag, "tier_decided_by", None),
            "is_safety": getattr(window_diag, "is_safety", None),
            "manual_override_active": (
                getattr(window_diag, "command_blocked_reason", None) == "manual_override"
            ),
            "night_hard_hold_applied": getattr(window_diag, "night_hard_hold_applied", False),
            "command_filter_allowed": getattr(window_diag, "command_allowed", None),
            "command_filter_reason": getattr(window_diag, "command_blocked_reason", None),
            "startup_grace_configured_cycles": getattr(window_diag, "startup_grace_configured_cycles", None),
            "startup_grace_remaining": getattr(window_diag, "startup_grace_remaining", None),
            "startup_initialization_complete": getattr(window_diag, "startup_initialization_complete", None),
            "previous_observation_available": getattr(window_diag, "previous_observation_available", None),
            "last_commanded_available": getattr(window_diag, "last_commanded_available", None),
            "override_reference_source": getattr(window_diag, "override_reference_source", None),
            "dispatch_suppressed": getattr(window_diag, "dispatch_suppressed_reason", None) is not None,
            "dispatch_suppression_reason": getattr(window_diag, "dispatch_suppressed_reason", None),
            "dispatch_throttled": getattr(window_diag, "dispatch_throttled", False),
            "service_call_attempted": getattr(window_diag, "service_call_sent", False),
            "service_call_succeeded": (
                getattr(window_diag, "service_call_sent", False)
                and not getattr(window_diag, "service_call_failed", False)
            ) if getattr(window_diag, "service_call_sent", False) else None,
            "service_call_error_category": _error_category(
                getattr(window_diag, "execution_error", None)
            ),
        }
    except Exception:
        _LOGGER.warning("SmartShading: zone_learning_export: runtime section failed for window")
        return {"available": False}


def _build_zone_runtime_section(zone_runtime: dict) -> dict:
    """Build the per-zone runtime state section.  Never raises."""
    if not zone_runtime:
        return {"available": False}
    try:
        return {
            "available": True,
            "startup_grace_remaining": zone_runtime.get("startup_grace_remaining"),
            "dispatch_generation": zone_runtime.get("dispatch_generation"),
            "last_update_success": zone_runtime.get("last_update_success"),
            "last_global_dispatch_at": zone_runtime.get("last_global_dispatch_at"),
        }
    except Exception:
        return {"available": False}


def _build_window_section(
    window_ref: str,
    window_id: str,
    learning_store: Any | None,
    window_diag: Any | None = None,
    window_config: Any | None = None,  # WindowConfig | None
) -> dict:
    """Build the per-window export section.  Never raises.

    forecast_learning is zone-scoped and lives at the zone level, not here.
    """
    store_section = _build_learning_store_section(learning_store, window_id)
    runtime_section = _build_window_runtime_section(window_diag)

    behavior_mode = None
    shading_learning_eligible = None
    exclusion_reason = None
    if window_config is not None:
        try:
            bm = getattr(window_config, "behavior_mode", None)
            if bm is not None:
                behavior_mode = bm.value if hasattr(bm, "value") else str(bm)
                from ..entities.zone_summary import is_shading_learning_eligible
                from ..models.window import WindowBehaviorMode
                shading_learning_eligible = is_shading_learning_eligible(bm)
                if not shading_learning_eligible:
                    exclusion_reason = "behavior_mode_not_fully_automatic"
        except Exception:
            _LOGGER.warning(
                "SmartShading: zone_learning_export: behavior-mode section "
                "failed for window")

    result = {
        "window_ref": window_ref,
        "learning_store": store_section,
        "runtime_state": runtime_section,
    }
    if behavior_mode is not None:
        result["behavior_mode"] = behavior_mode
    if shading_learning_eligible is not None:
        result["shading_learning_eligible"] = shading_learning_eligible
    if exclusion_reason is not None:
        result["exclusion_reason"] = exclusion_reason
    return result


def _build_zone_section(
    zone_ref: str,
    entry_ref: str,
    zone_entry: dict,
    generated_at_utc: datetime,
    zone_index: int,
) -> dict:
    """Build the per-zone export section.  Never raises.

    Parameters
    ----------
    zone_entry:
        Dict with keys:
          "entry_id"            — raw config entry ID (used only for ref generation)
          "window_ids"          — ordered list of window IDs
          "window_configs"      — dict[window_id, WindowConfig] or {} (optional)
          "learning_store"      — LearningStore instance or None
          "forecast_store"      — ForecastLearningStore instance or None
          "execution_diagnostics" — dict[window_id, WindowExecutionDiagnostics] or {}
          "zone_runtime"        — dict of zone-level runtime state or {}
    """
    windows_out: list[dict] = []
    window_ids: list[str] = zone_entry.get("window_ids", [])
    window_configs: dict = zone_entry.get("window_configs", {})
    coordinator_data = zone_entry.get("coordinator_data")
    learning_store = zone_entry.get("learning_store")
    forecast_store = zone_entry.get("forecast_store")
    target_adapter = zone_entry.get("target_position_adapter")
    exec_diag: dict = zone_entry.get("execution_diagnostics", {})
    zone_runtime: dict = zone_entry.get("zone_runtime", {})

    n_eligible = 0
    n_excluded = 0

    for w_idx, window_id in enumerate(sorted(window_ids), start=1):
        window_ref = f"window_{w_idx}"
        wc = window_configs.get(window_id) if window_configs else None
        window_section = _build_window_section(
            window_ref=window_ref,
            window_id=window_id,
            learning_store=learning_store,
            window_diag=exec_diag.get(window_id),
            window_config=wc,
        )
        windows_out.append(window_section)
        if window_section.get("shading_learning_eligible") is True:
            n_eligible += 1
        elif window_section.get("shading_learning_eligible") is False:
            n_excluded += 1

    # Compute zone-level learning progress and shading outcome for shading_metrics.
    # These mirror the live sensor values so offline tooling sees the same semantics.
    lp_available: bool | None = None
    lp_percent: int | None = None
    lp_reason: str | None = None
    outcome_state: str | None = None
    outcome_reason: str | None = None

    if window_configs is not None:
        try:
            from ..entities.zone_summary import (
                compute_learning_progress,
                compute_zone_shading_result,
            )
            pct, lp_attrs = compute_learning_progress(
                list(window_ids), coordinator_data, window_configs
            )
            lp_available = pct is not None
            lp_percent = pct
            lp_reason = lp_attrs.get("reason")

            if learning_store is not None:
                o_state, o_attrs = compute_zone_shading_result(
                    list(window_ids), learning_store, window_configs
                )
                outcome_state = o_state
                outcome_reason = o_attrs.get("reason")
        except Exception:
            _LOGGER.warning(
                "SmartShading: zone_learning_export: shading_metrics computation failed"
            )

    shading_metrics: dict = {
        "total_windows": len(window_ids),
        "eligible_windows": n_eligible if window_configs else None,
        "excluded_windows": n_excluded if window_configs else None,
        "learning_progress_available": lp_available,
        "learning_progress_percent": lp_percent,
        "learning_progress_reason": lp_reason,
        "outcome_state": outcome_state,
        "outcome_reason": outcome_reason,
    }

    # Forecast learning is zone-scoped — build once at zone level.
    try:
        forecast_section = build_learning_export(
            forecast_store=forecast_store,
            generated_at_utc=generated_at_utc,
        )
        forecast_out = forecast_section.get("forecast_learning", {})
    except Exception:
        _LOGGER.warning("SmartShading: zone_learning_export: forecast section failed for zone")
        forecast_out = {"available": False}

    # Aggregate target adaptation summary (privacy-safe).
    target_adaptation_summary: dict = {"available": False}
    if target_adapter is not None:
        try:
            from .target_position_adapter import build_target_adaptation_export_summary
            target_adaptation_summary = build_target_adaptation_export_summary(target_adapter)
        except Exception:
            _LOGGER.warning("SmartShading: zone_learning_export: target adaptation summary failed")

    zone_runtime_section = _build_zone_runtime_section(zone_runtime)

    return {
        "zone_ref": zone_ref,
        "entry_ref": entry_ref,
        "windows_count": len(windows_out),
        "shading_metrics": shading_metrics,
        "forecast_learning": forecast_out,
        "target_adaptation_summary": target_adaptation_summary,
        "zone_runtime_state": zone_runtime_section,
        "windows": windows_out,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_global_learning_export(
    *,
    zone_entries: list[dict],
    generated_at_utc: datetime,
) -> dict:
    """Build a privacy-safe global learning export covering all zones.

    Parameters
    ----------
    zone_entries:
        List of per-zone dicts, each with:
          "entry_id"       str               — raw config entry ID (for ref generation only)
          "window_ids"     list[str]         — window IDs belonging to this zone
          "learning_store" LearningStore|None
          "forecast_store" ForecastLearningStore|None

        Entries are sorted internally by entry_id for stable ordering across calls.
        The caller must pass all active zone entries — the export is always global.

    generated_at_utc:
        UTC-aware datetime.  Raises ValueError for naive datetimes (UTC policy).

    Returns
    -------
    dict
        JSON-serializable export ready for HA Store.async_save().

    Raises
    ------
    ValueError
        If generated_at_utc has no timezone info (UTC policy guard).
    """
    if generated_at_utc.tzinfo is None:
        raise ValueError(
            "build_global_learning_export: generated_at_utc must be timezone-aware"
        )

    # Sort entries for stable ordering; assign sequential refs.
    sorted_entries = sorted(zone_entries, key=lambda e: e.get("entry_id", ""))
    zones_out: list[dict] = []

    for z_idx, zone_entry in enumerate(sorted_entries, start=1):
        entry_id = zone_entry.get("entry_id", f"unknown_{z_idx}")
        zone_ref = f"zone_{z_idx}"
        entry_ref = f"entry_{_short_ref(entry_id)}"
        zone_section = _build_zone_section(
            zone_ref=zone_ref,
            entry_ref=entry_ref,
            zone_entry=zone_entry,
            generated_at_utc=generated_at_utc,
            zone_index=z_idx,
        )
        zones_out.append(zone_section)

    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "support_export_schema_version": SUPPORT_EXPORT_SCHEMA_VERSION,
        "export_type": EXPORT_TYPE,
        "domain": "smartshading",
        "generated_at_utc": generated_at_utc.isoformat(),
        "zones_count": len(zones_out),
        "zones": zones_out,
    }
