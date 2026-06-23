"""Adoption eligibility — LE 2.0 / Phase P8 (pure).

A FRESH eligibility evaluation, run before every adoption creation/upgrade AND
before reactivation after a restart or Learning-Mode toggle.  A persisted P7
``accepted_for_p8`` / ``p8_adoption_eligible`` snapshot is NEVER sufficient.

No Home Assistant import.  No runtime mutation.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.window_contribution import ATTR_WINDOW_ISOLATED
from ..engines.experiment_eligibility import (
    ATTR_WINDOW_CANDIDATE,
    EXPERIMENT_MIN_THERMAL_RELIABILITY,
)


@dataclass(frozen=True)
class AdoptionEligibilityInput:
    intensity_level: str
    # Two user levels (active_control only required for REAL effect, not for the
    # recommendation-only path — see coordinator).
    learning_enabled: bool
    active_control_required_now: bool       # True when the caller demands real actuation
    active_control_enabled: bool
    # Authority / window state.
    fully_automatic: bool
    manual_preference_active: bool          # per-intensity manual preference
    manual_override_active: bool
    safety_active: bool
    lifecycle_active: bool
    presence_absence_transition: bool
    # References / models.
    config_generation_matches: bool
    contribution_current: bool
    attribution_quality: str
    thermal_available: bool
    thermal_reliability: float
    p6_p7_reference_present: bool
    reliable_position_feedback: bool
    # Context / candidate safety.
    context_compatible: bool
    confounded: bool
    candidate_material_and_safe: bool
    # Evidence / cooldown.
    evidence_sufficient: bool
    cooldown_active: bool


def evaluate_adoption_eligibility(inp: AdoptionEligibilityInput) -> "object":
    from ..models.persistent_adoption import AdoptionEligibilityResult

    blocked: list[str] = []
    passed: list[str] = []

    def gate(ok: bool, code: str) -> None:
        (passed if ok else blocked).append(code)

    gate(inp.learning_enabled, "learning_mode_required")
    if inp.active_control_required_now:
        gate(inp.active_control_enabled, "active_control_required")
    gate(inp.fully_automatic, "not_fully_automatic")
    gate(not inp.manual_preference_active, "manual_preference_active")
    gate(not inp.manual_override_active, "manual_override_active")
    gate(not inp.safety_active, "safety_active")
    gate(not inp.lifecycle_active, "lifecycle_active")
    gate(not inp.presence_absence_transition, "presence_absence_transition")
    gate(inp.config_generation_matches, "config_generation_changed")
    gate(inp.contribution_current, "contribution_model_stale")
    gate(
        inp.attribution_quality in (ATTR_WINDOW_ISOLATED, ATTR_WINDOW_CANDIDATE),
        "attribution_unsuitable",
    )
    gate(inp.thermal_available, "thermal_unavailable")
    gate(inp.thermal_reliability >= EXPERIMENT_MIN_THERMAL_RELIABILITY, "thermal_low_confidence")
    gate(inp.p6_p7_reference_present, "p6_p7_reference_missing")
    gate(inp.reliable_position_feedback, "no_reliable_feedback")
    gate(inp.context_compatible, "context_family_mismatch")
    gate(not inp.confounded, "confounded")
    gate(inp.candidate_material_and_safe, "candidate_not_material_or_safe")
    gate(inp.evidence_sufficient, "evidence_insufficient")
    gate(not inp.cooldown_active, "cooldown_active")

    eligible = not blocked
    return AdoptionEligibilityResult(
        eligible=eligible, intensity_level=inp.intensity_level,
        reasons=tuple(passed), blocked_by=tuple(blocked),
        block_reason=(blocked[0] if blocked else None),
    )
