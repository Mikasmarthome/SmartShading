"""Shadow eligibility gate — LE 2.0 / Phase P6 (pure).

Decides whether a per-window shadow candidate may be COMPUTED/observed.  This is
analysis only: active_control being off does NOT block shadow computation
(Observation Mode ≠ Active Control).  active_control is a P7 execution gate.

window_isolated / window_candidate attribution (which require real actuation
evidence in P5) is what gates per-window eligibility; without such evidence the
result is blocked with 'insufficient_actuation_evidence'.  No Home Assistant
import.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.shadow_proposal import ShadowEligibilityResult
from ..models.window_contribution import ATTR_WINDOW_CANDIDATE, ATTR_WINDOW_ISOLATED

# Minimum dimension-specific thermal reliability to consider a shadow candidate.
SHADOW_MIN_THERMAL_RELIABILITY: float = 0.4


@dataclass(frozen=True)
class ShadowEligibilityInput:
    """Explicit, HA-free facts for the shadow eligibility gate."""

    intensity_level: str | None
    observation_mode: bool
    fully_automatic: bool
    safety_active: bool
    manual_override_active: bool
    lifecycle_active: bool                 # night/morning
    presence_absence_transition: bool
    thermal_available: bool
    thermal_mature: bool
    thermal_reliability: float
    attribution_quality: str               # final P5 attribution
    contribution_shadow_eligible: bool     # derive_eligibility(...).shadow at CURRENT generation
    config_generation_matches: bool
    p5_reference_valid: bool
    manual_preference_active: bool          # a reliable learned preference is already applied
    manual_preference_open_more: bool       # repeated open_more feedback (hard veto)
    confounded: bool


def evaluate_shadow_eligibility(inp: ShadowEligibilityInput) -> ShadowEligibilityResult:
    """Return ShadowEligibilityResult.  Every failing gate is reported."""
    blocked: list[str] = []
    reasons: list[str] = []

    def gate(ok: bool, name: str) -> None:
        (reasons if ok else blocked).append(name)

    gate(inp.observation_mode, "observation_mode")
    gate(inp.fully_automatic, "fully_automatic")
    gate(not inp.safety_active, "no_safety")
    gate(not inp.manual_override_active, "no_manual_override")
    gate(not inp.lifecycle_active, "no_lifecycle")
    gate(not inp.presence_absence_transition, "no_presence_change")
    gate(not inp.confounded, "no_confounder")
    gate(inp.config_generation_matches, "config_generation_current")
    gate(inp.p5_reference_valid, "p5_reference_valid")
    gate(inp.thermal_available, "thermal_available")
    gate(inp.thermal_mature, "thermal_mature")
    gate(inp.thermal_reliability >= SHADOW_MIN_THERMAL_RELIABILITY, "thermal_reliability")

    # Per-window attribution must be candidate or isolated (real actuation evidence).
    if inp.attribution_quality in (ATTR_WINDOW_ISOLATED, ATTR_WINDOW_CANDIDATE):
        reasons.append(f"attribution_{inp.attribution_quality}")
    else:
        blocked.append("insufficient_actuation_evidence")

    gate(inp.contribution_shadow_eligible, "contribution_shadow_eligible")

    # Manual preference authority (close-more shadow must not double-apply, and
    # open_more is a hard veto).  An active reliable preference blocks an extra
    # thermal shadow step (conservative P6 rule).
    if inp.manual_preference_open_more:
        blocked.append("preference_open_more_veto")
    if inp.manual_preference_active:
        blocked.append("manual_preference_active_no_extra_step")

    if inp.intensity_level not in ("light", "normal", "strong"):
        blocked.append("invalid_intensity_level")

    eligible = not blocked
    primary = blocked[0] if blocked else None
    return ShadowEligibilityResult(
        eligible=eligible, intensity_level=inp.intensity_level,
        reasons=tuple(reasons), blocked_by=tuple(blocked), block_reason=primary,
    )
