"""Typed config-change diff — LE 2.0 / Phase P10 final closure (pure).

Compares a PREVIOUS normalised config snapshot (persisted with the learning
store) against the CURRENT normalised snapshot and emits a typed list of
ConfigChange that the coordinator routes through classify_config_change.

Stable internal window/zone ids only (never display names).  First setup (no
previous snapshot) and an unchanged restart both emit zero changes, so neither
fabricates an invalidation.  No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config_invalidation import (
    CHANGE_BEHAVIOR_MODE_AWAY,
    CHANGE_BEHAVIOR_MODE_BACK,
    CHANGE_CONFIGURED_TARGETS,
    CHANGE_COVER_REPLACEMENT,
    CHANGE_FEEDBACK_CAPABILITY_LOSS,
    CHANGE_FORECAST_PROVIDER,
    CHANGE_INDOOR_TEMP_SENSOR,
    CHANGE_OBSTRUCTION,
    CHANGE_ORIENTATION,
    CHANGE_SOLAR_SENSOR,
    CHANGE_SUN_SECTOR,
)

# Window removal is handled directly by the coordinator (not in the suspend matrix).
CHANGE_WINDOW_REMOVAL = "window_removal"

_FULLY_AUTOMATIC = "WindowBehaviorMode.FULLY_AUTOMATIC"


@dataclass(frozen=True)
class ConfigChange:
    zone_id: str
    change_type: str
    window_id: str | None = None


def diff_config_snapshots(prev: dict | None, current: dict) -> list[ConfigChange]:
    """Return the typed config changes between *prev* and *current* snapshots.

    Empty when *prev* is falsy (first setup) or nothing relevant changed."""
    if not prev:
        return []
    changes: list[ConfigChange] = []

    # --- zone-level (sensors / forecast source) ---
    cur_z = current.get("zones", {})
    prev_z = prev.get("zones", {})
    for zone, cz in cur_z.items():
        pz = prev_z.get(zone)
        if pz is None:
            continue  # newly added zone → no invalidation
        if cz.get("indoor") != pz.get("indoor"):
            changes.append(ConfigChange(zone, CHANGE_INDOOR_TEMP_SENSOR))
        if cz.get("solar") != pz.get("solar"):
            changes.append(ConfigChange(zone, CHANGE_SOLAR_SENSOR))
        if cz.get("forecast") != pz.get("forecast"):
            changes.append(ConfigChange(zone, CHANGE_FORECAST_PROVIDER))

    # --- window-level ---
    cur_w = current.get("windows", {})
    prev_w = prev.get("windows", {})
    for wid, cw in cur_w.items():
        pw = prev_w.get(wid)
        if pw is None:
            continue  # newly added window → no invalidation
        zone = cw.get("zone_id", "")
        if cw.get("azimuth") != pw.get("azimuth"):
            changes.append(ConfigChange(zone, CHANGE_ORIENTATION, wid))
        if cw.get("sun_sector") != pw.get("sun_sector"):
            changes.append(ConfigChange(zone, CHANGE_SUN_SECTOR, wid))
        if cw.get("obstruction") != pw.get("obstruction"):
            changes.append(ConfigChange(zone, CHANGE_OBSTRUCTION, wid))
        if cw.get("cover_group") != pw.get("cover_group"):
            changes.append(ConfigChange(zone, CHANGE_COVER_REPLACEMENT, wid))
        if cw.get("positions") != pw.get("positions"):
            changes.append(ConfigChange(zone, CHANGE_CONFIGURED_TARGETS, wid))
        if cw.get("feedback_capable") and not pw.get("feedback_capable"):
            pass  # capability gained → handled by fresh eligibility, no invalidation
        elif pw.get("feedback_capable") and not cw.get("feedback_capable"):
            changes.append(ConfigChange(zone, CHANGE_FEEDBACK_CAPABILITY_LOSS, wid))
        if cw.get("behavior_mode") != pw.get("behavior_mode"):
            left_auto = pw.get("behavior_mode") == _FULLY_AUTOMATIC
            back_auto = cw.get("behavior_mode") == _FULLY_AUTOMATIC
            if left_auto and not back_auto:
                changes.append(ConfigChange(zone, CHANGE_BEHAVIOR_MODE_AWAY, wid))
            elif back_auto and not left_auto:
                changes.append(ConfigChange(zone, CHANGE_BEHAVIOR_MODE_BACK, wid))

    # --- window removal ---
    for wid, pw in prev_w.items():
        if wid not in cur_w:
            changes.append(ConfigChange(pw.get("zone_id", ""), CHANGE_WINDOW_REMOVAL, wid))

    return changes
