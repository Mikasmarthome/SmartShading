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
SUPPORT_EXPORT_SCHEMA_VERSION: int = 1


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


def _build_window_section(
    window_ref: str,
    window_id: str,
    learning_store: Any | None,
) -> dict:
    """Build the per-window export section.  Never raises.

    forecast_learning is zone-scoped and lives at the zone level, not here.
    """
    store_section = _build_learning_store_section(learning_store, window_id)
    return {
        "window_ref": window_ref,
        "learning_store": store_section,
    }


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
          "entry_id"      — raw config entry ID (used only for ref generation)
          "window_ids"    — ordered list of window IDs
          "learning_store" — LearningStore instance or None
          "forecast_store" — ForecastLearningStore instance or None
    """
    windows_out: list[dict] = []
    window_ids: list[str] = zone_entry.get("window_ids", [])
    learning_store = zone_entry.get("learning_store")
    forecast_store = zone_entry.get("forecast_store")
    target_adapter = zone_entry.get("target_position_adapter")

    for w_idx, window_id in enumerate(sorted(window_ids), start=1):
        window_ref = f"window_{w_idx}"
        window_section = _build_window_section(
            window_ref=window_ref,
            window_id=window_id,
            learning_store=learning_store,
        )
        windows_out.append(window_section)

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

    return {
        "zone_ref": zone_ref,
        "entry_ref": entry_ref,
        "windows_count": len(windows_out),
        "forecast_learning": forecast_out,
        "target_adaptation_summary": target_adaptation_summary,
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
