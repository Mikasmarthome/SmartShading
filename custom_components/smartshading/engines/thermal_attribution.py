"""Thermal attribution source — which indoor-temperature basis a decision uses.

Pure, Home-Assistant-free.  This reports *transparently* what indoor-temperature
basis fed a window's thermal reasoning this cycle, so a beta tester (and the
outcome layer) can tell a window-specific reading apart from the house-wide
average fallback.

Each SmartShading config entry is exactly one zone, and indoor temperature sensors
are configured per entry (the per-zone comfort step), averaged across the valid
sensors of that zone.  A configured, readable indoor reading is therefore
ZONE-scoped, so the live source is ``zone`` (this zone has at least one readable
indoor sensor) or ``unknown`` (none configured/readable).  ``window`` is reserved
for a future per-window sensor and ``global`` for a possible cross-zone shared
source; neither is produced today, but both stay in the vocabulary so the resolver
can return the more specific/less specific value later without renaming the
attribute.

This module intentionally does NOT change any control or learning behaviour — it
only labels the basis.  The outcome layer keeps its own, separate
``attribution_quality`` (zone_shared / window_candidate / window_isolated) for how
strongly a *resolved* thermal outcome is credited to a window; that is a distinct
concept from this decision-time input basis.
"""
from __future__ import annotations

# Input-basis labels (decision-time indoor-temperature source).
THERMAL_SOURCE_WINDOW: str = "window"
THERMAL_SOURCE_ZONE: str = "zone"
THERMAL_SOURCE_GLOBAL: str = "global"
THERMAL_SOURCE_UNKNOWN: str = "unknown"


def resolve_thermal_attribution_source(
    *,
    has_indoor_temperature: bool,
    window_indoor_temp_available: bool = False,
    zone_indoor_temp_available: bool = False,
) -> str:
    """Return the indoor-temperature basis label for a window this cycle.

    Preference order (most specific first): a per-window reading, then a per-zone
    reading, then a cross-zone shared reading, then nothing.  Because a config
    entry is one zone and indoor sensors are configured per entry, the coordinator
    passes ``zone_indoor_temp_available`` for a readable indoor value, so the
    normal result is ``zone`` or ``unknown``.  ``window`` and ``global`` are
    reserved for future per-window / cross-zone sources.
    """
    if window_indoor_temp_available:
        return THERMAL_SOURCE_WINDOW
    if zone_indoor_temp_available:
        return THERMAL_SOURCE_ZONE
    if has_indoor_temperature:
        return THERMAL_SOURCE_GLOBAL
    return THERMAL_SOURCE_UNKNOWN
