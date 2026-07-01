"""Thermal attribution source — which indoor-temperature basis a decision uses.

Pure, Home-Assistant-free.  This reports *transparently* what indoor-temperature
basis fed a window's thermal reasoning this cycle, so a beta tester (and the
outcome layer) can tell a window-specific reading apart from the house-wide
average fallback.

Today SmartShading reads a single house-wide indoor temperature (the average of
all configured indoor sensors) and shares it across every window, so the live
source is either ``global`` (at least one indoor sensor configured and readable)
or ``unknown`` (none).  The ``window`` and ``zone`` sources are already part of
the vocabulary so that, if a per-zone or per-window indoor temperature is wired
up later behind explicit configuration, this resolver returns the more specific
value without any attribute rename.

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
    reading, then the house-wide average, then nothing.  The two specific inputs
    default to False because SmartShading has no per-zone/per-window indoor
    temperature configuration today; they are here so the resolver stays correct
    if that is added later without renaming the attribute.
    """
    if window_indoor_temp_available:
        return THERMAL_SOURCE_WINDOW
    if zone_indoor_temp_available:
        return THERMAL_SOURCE_ZONE
    if has_indoor_temperature:
        return THERMAL_SOURCE_GLOBAL
    return THERMAL_SOURCE_UNKNOWN
