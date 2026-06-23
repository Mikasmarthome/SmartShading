"""Experiment eligibility — LE 2.0 / Phase P7 (pure).

A FRESH eligibility evaluation that must be run both at planning time and again
immediately before activation.  A persisted P6 'supported' flag is never enough.

Three mandatory user levels (all required, plus every other gate):
    observation_enabled AND active_control_enabled AND experiments_enabled

No Home Assistant import.  No runtime mutation.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.window_contribution import ATTR_WINDOW_ISOLATED
from ..models.shadow_proposal import STATUS_SUPPORTED

# Stricter than the shadow gate (0.4): a real movement requires a more confident
# thermal response model.
EXPERIMENT_MIN_THERMAL_RELIABILITY: float = 0.5

ATTR_WINDOW_CANDIDATE: str = "window_candidate"


@dataclass(frozen=True)
class ExperimentEligibilityInput:
    intensity_level: str
    # Three user levels (verbatim names from ZoneExecutionConfig).
    observation_enabled: bool
    active_control_enabled: bool
    experiments_enabled: bool
    # Shadow / references.
    shadow_status: str
    proposal_present: bool
    p5_reference_valid: bool
    contribution_current: bool
    attribution_quality: str
    config_generation_matches: bool
    # Thermal model.
    thermal_available: bool
    thermal_mature: bool
    thermal_reliability: float
    temperature_source_available: bool
    # Preference / authority.
    preference_veto: bool
    manual_preference_active: bool
    fully_automatic: bool
    manual_override_active: bool
    safety_active: bool
    lifecycle_active: bool
    presence_absence_transition: bool
    # Context / feedback / safety of candidate.
    solar_context_ok: bool
    reliable_position_feedback: bool
    confounded: bool
    candidate_valid: bool
    # Zone lock / cooldown.
    other_active_zone_experiment: bool
    cooldown_active: bool


def evaluate_experiment_eligibility(
    inp: ExperimentEligibilityInput,
) -> "object":
    """Return an ExperimentEligibilityResult (imported lazily to avoid cycles)."""
    from ..models.bounded_experiment import ExperimentEligibilityResult

    blocked: list[str] = []
    passed: list[str] = []

    def gate(ok: bool, code: str) -> None:
        (passed if ok else blocked).append(code)

    # --- three mandatory user levels (ordered for stable primary reason) ---
    gate(inp.observation_enabled, "observation_mode_required")
    gate(inp.active_control_enabled, "active_control_required")
    gate(inp.experiments_enabled, "experiments_not_enabled")
    gate(inp.reliable_position_feedback, "no_reliable_feedback")

    # --- shadow + references ---
    gate(inp.shadow_status == STATUS_SUPPORTED, "shadow_not_supported")
    gate(inp.proposal_present, "shadow_proposal_missing")
    gate(inp.p5_reference_valid, "p5_reference_invalid")
    gate(inp.contribution_current, "contribution_model_stale")
    gate(inp.config_generation_matches, "config_generation_changed")
    gate(
        inp.attribution_quality in (ATTR_WINDOW_ISOLATED, ATTR_WINDOW_CANDIDATE),
        "insufficient_actuation_evidence",
    )

    # --- thermal model ---
    gate(inp.thermal_available, "thermal_unavailable")
    gate(inp.thermal_mature, "thermal_immature")
    gate(inp.thermal_reliability >= EXPERIMENT_MIN_THERMAL_RELIABILITY, "thermal_low_reliability")
    gate(inp.temperature_source_available, "temperature_source_unavailable")

    # --- authority / preference (experiment is the lowest learning authority) ---
    gate(inp.fully_automatic, "not_fully_automatic")
    gate(not inp.manual_override_active, "manual_override_active")
    gate(not inp.safety_active, "safety_active")
    gate(not inp.lifecycle_active, "lifecycle_active")
    gate(not inp.presence_absence_transition, "presence_absence_transition")
    gate(not inp.manual_preference_active, "manual_preference_active")
    gate(not inp.preference_veto, "preference_open_more_veto")

    # --- context / confounders / candidate safety ---
    gate(inp.solar_context_ok, "solar_context_unsuitable")
    gate(not inp.confounded, "confounded")
    gate(inp.candidate_valid, "candidate_not_material_or_safe")

    # --- zone lock / cooldown ---
    gate(not inp.other_active_zone_experiment, "zone_experiment_active")
    gate(not inp.cooldown_active, "cooldown_active")

    eligible = not blocked
    return ExperimentEligibilityResult(
        eligible=eligible,
        intensity_level=inp.intensity_level,
        reasons=tuple(passed),
        blocked_by=tuple(blocked),
        block_reason=(blocked[0] if blocked else None),
    )
