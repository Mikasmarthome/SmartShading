"""Thermal Insufficiency Cause Classifier — LE 2.0 / Phase P9A (pure).

When indoor temperature keeps rising despite active shading, this classifier
attributes the most likely CAUSE so the right learning path is chosen — never a
blanket "shade harder".  It builds on P3 ThermalOutcome, P4 ThermalResponseModel
and P5 attribution; it makes no change itself.

No Home Assistant import.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.window_contribution import ATTR_WINDOW_ISOLATED

# Cause codes.
CAUSE_INSUFFICIENT_POSITION: str = "insufficient_position"
CAUSE_LATE_ENTRY: str = "late_entry"
CAUSE_INSUFFICIENT_INTENSITY: str = "insufficient_intensity_choice"
CAUSE_EXCESSIVE_LOAD_DURATION: str = "excessive_load_duration"
CAUSE_WRONG_WINDOW: str = "wrong_window_attribution"
CAUSE_OUTDOOR_OR_INTERNAL: str = "outdoor_or_internal_heat_dominant"
CAUSE_THERMAL_INERTIA: str = "thermal_inertia_expected"
CAUSE_CONFOUNDED: str = "confounded"
CAUSE_UNAVAILABLE: str = "unavailable"

# Follow-up routing (which learning path may act on the cause).
FOLLOWUP_POSITION_LEARNING: str = "position_learning"          # P7/P8
FOLLOWUP_TIMING_EXPERIMENT: str = "timing_experiment"          # P9B
FOLLOWUP_TIER_CHOICE_EXPERIMENT: str = "tier_choice_experiment"  # P9B
FOLLOWUP_FORECAST_STRATEGY: str = "forecast_strategy"          # earlier moderate start
FOLLOWUP_NONE: str = "no_change"

ATTR_WINDOW_CANDIDATE: str = "window_candidate"


@dataclass(frozen=True)
class InsufficiencyInput:
    thermal_available: bool
    confounded: bool
    shade_was_active: bool
    insufficient_response: bool          # P3: shading active but indoor still rose
    attribution_quality: str             # P5
    onset_reached: bool                  # P4: expected response onset already passed
    shade_was_timely: bool               # shading active in time vs load start
    at_max_intensity: bool               # already Strong (no stronger tier available)
    load_duration_long: bool             # load far exceeds typical observation window
    outdoor_or_internal_dominant: bool   # outdoor delta / internal source dominates


def classify_thermal_insufficiency(inp: InsufficiencyInput) -> tuple[str, str]:
    """Return (cause_code, follow_up).  Deterministic, conservative ordering:
    non-actionable causes are ruled out first."""
    if not inp.thermal_available:
        return (CAUSE_UNAVAILABLE, FOLLOWUP_NONE)
    if inp.confounded:
        return (CAUSE_CONFOUNDED, FOLLOWUP_NONE)
    # Not this window's fault.
    if inp.attribution_quality not in (ATTR_WINDOW_ISOLATED, ATTR_WINDOW_CANDIDATE):
        return (CAUSE_WRONG_WINDOW, FOLLOWUP_NONE)
    if inp.outdoor_or_internal_dominant:
        return (CAUSE_OUTDOOR_OR_INTERNAL, FOLLOWUP_NONE)
    # Response simply not observed yet → do not over-react.
    if not inp.onset_reached:
        return (CAUSE_THERMAL_INERTIA, FOLLOWUP_NONE)
    # From here: shading active, onset passed, this window, genuine signal.
    if not inp.shade_was_active or not inp.insufficient_response:
        return (CAUSE_THERMAL_INERTIA, FOLLOWUP_NONE)
    if not inp.shade_was_timely:
        return (CAUSE_LATE_ENTRY, FOLLOWUP_TIMING_EXPERIMENT)
    if inp.load_duration_long:
        return (CAUSE_EXCESSIVE_LOAD_DURATION, FOLLOWUP_FORECAST_STRATEGY)
    if not inp.at_max_intensity:
        return (CAUSE_INSUFFICIENT_INTENSITY, FOLLOWUP_TIER_CHOICE_EXPERIMENT)
    return (CAUSE_INSUFFICIENT_POSITION, FOLLOWUP_POSITION_LEARNING)
