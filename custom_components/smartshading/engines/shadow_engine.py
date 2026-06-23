"""Shadow engine — LE 2.0 / Phase P6 (pure).

Computes a close-more shadow candidate via a real-clamp DRY-RUN (reusing the
existing apply_daytime_min_open / apply_anti_heat_buildup functions — never an
`applied-5` shortcut), derives candidate reasons from defensible evidence, and
evaluates the supported maturity gate.  No runtime state is mutated.  No Home
Assistant import beyond the (pure) clamp helpers.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..cover_control.anti_heat_buildup import apply_anti_heat_buildup
from ..cover_control.daytime_min_open import apply_daytime_min_open
from ..cover_control.position_semantics import clamp_position
from ..models.cover_group import CoverHardwareType
from ..models.shadow_proposal import (
    DIRECTION_CLOSE_MORE,
    SHADOW_CUMULATIVE_CAP_HA,
    SHADOW_MATERIALITY_HA,
    SHADOW_STEP_HA,
    STATUS_INCONCLUSIVE,
    STATUS_OBSERVING,
    STATUS_REJECTED,
    STATUS_SUPPORTED,
    SUPPORTED_MIN_CONFIDENCE,
    SUPPORTED_MIN_DAYS_CANDIDATE,
    SUPPORTED_MIN_DAYS_ISOLATED,
    SUPPORTED_MIN_OUTCOMES_CANDIDATE,
    SUPPORTED_MIN_OUTCOMES_ISOLATED,
    ShadowEvaluation,
)
from ..models.window_contribution import ATTR_WINDOW_ISOLATED
from ..state_machine.states import ShadingState


@dataclass(frozen=True)
class ShadowCandidateResult:
    """Result of the close-more candidate dry-run."""

    valid: bool
    shadow_parameter_target_ha: int | None      # current authoritative − step (pre-clamp)
    shadow_final_candidate_target_ha: int | None  # after real clamps
    net_delta_vs_real_ha: int | None             # final − real applied (negative when closing)
    block_reason: str | None = None


def compute_shadow_candidate(
    *,
    current_authoritative_target_ha: int,
    real_applied_target_ha: int,
    configured_base_target_ha: int,
    new_state: ShadingState,
    daytime_min_ha: int | None,
    ahb_position_ha: int | None,
    ahb_enabled: bool,
    hardware_type: CoverHardwareType,
    in_solar_sector: bool,
    effective_exposure_wm2: float | None,
    allow_ahb_during_absence: bool = False,
) -> ShadowCandidateResult:
    """Dry-run a close-more candidate through the REAL clamp functions.

    Steps: authoritative target − 5 pp (parameter) → daytime-min clamp →
    anti-heat-buildup clamp → cumulative cap vs configured base.  A candidate is
    valid only when, after all real guardrails, it is still materially MORE
    closed than the real applied target.
    """
    param = clamp_position(current_authoritative_target_ha - SHADOW_STEP_HA)

    # Real daytime-minimum-open clamp.
    after_daytime, _, _ = apply_daytime_min_open(param, daytime_min_ha, new_state)
    # Real anti-heat-buildup clamp.
    after_ahb, _, _ = apply_anti_heat_buildup(
        after_daytime, ahb_position_ha, ahb_enabled, hardware_type, new_state,
        in_solar_sector, effective_exposure_wm2, allow_ahb_during_absence,
    )
    final = after_ahb if after_ahb is not None else after_daytime
    final = clamp_position(final)

    # Cumulative cap: never more than SHADOW_CUMULATIVE_CAP_HA closed vs config base.
    floor = clamp_position(configured_base_target_ha - SHADOW_CUMULATIVE_CAP_HA)
    if final < floor:
        final = floor

    net = final - real_applied_target_ha

    if final >= real_applied_target_ha:
        return ShadowCandidateResult(
            valid=False, shadow_parameter_target_ha=param,
            shadow_final_candidate_target_ha=final, net_delta_vs_real_ha=net,
            block_reason="candidate_neutralized_by_guardrail",
        )
    if abs(net) < SHADOW_MATERIALITY_HA:
        return ShadowCandidateResult(
            valid=False, shadow_parameter_target_ha=param,
            shadow_final_candidate_target_ha=final, net_delta_vs_real_ha=net,
            block_reason="candidate_below_materiality",
        )
    return ShadowCandidateResult(
        valid=True, shadow_parameter_target_ha=param,
        shadow_final_candidate_target_ha=final, net_delta_vs_real_ha=net,
    )


# ---------------------------------------------------------------------------
# Candidate reason engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateReasonInput:
    """Defensible signals for proposing a close-more candidate."""

    thermal_available: bool
    thermal_mature: bool
    shade_state_active: bool
    insufficient_response: bool
    thermal_score: float | None
    sufficient_solar_load: bool             # exposure high enough to expect a response
    shade_was_timely_active: bool           # real shading active in time (P4)
    contribution_present: bool              # isolated/candidate attribution
    close_more_preference: bool


def compute_candidate_reason(inp: CandidateReasonInput) -> str | None:
    """Return a reason code, or None when no defensible position signal exists.

    Never from a single high solar value, legacy score, prior or forecast alone,
    or stable temperature without load.
    """
    if not (inp.thermal_available and inp.thermal_mature and inp.shade_state_active):
        return None
    negative_thermal = inp.insufficient_response or (
        inp.thermal_score is not None and inp.thermal_score < 0
    )
    if not negative_thermal or not inp.sufficient_solar_load:
        return None

    # late_protection_response only when shading was timely active AND there is a
    # genuine negative thermal signal under sustained load (not mere inertia).
    if inp.shade_was_timely_active and inp.insufficient_response and inp.contribution_present:
        base = "late_protection_response"
    elif inp.contribution_present:
        base = "contribution_weighted_underprotection"
    else:
        base = "insufficient_thermal_response"

    if inp.close_more_preference:
        return "preference_supported_closing"
    return base


# ---------------------------------------------------------------------------
# Supported maturity gate (trigger ≠ evidence ≠ supported)
# ---------------------------------------------------------------------------

def evaluate_supported_status(
    evaluation: ShadowEvaluation,
    *,
    attribution_quality: str,
    preference_veto: bool,
) -> str:
    """Decide the proposal status from accumulated evidence.

    A single outcome can never reach 'supported'.  window_candidate needs
    stricter sample/day gates than window_isolated.  preference_veto and
    contradictory evidence force inconclusive/rejected.
    """
    if preference_veto:
        return STATUS_INCONCLUSIVE
    if evaluation.contradictory_outcomes > evaluation.negative_baseline_outcomes:
        return STATUS_REJECTED

    if attribution_quality == ATTR_WINDOW_ISOLATED:
        min_out, min_days = SUPPORTED_MIN_OUTCOMES_ISOLATED, SUPPORTED_MIN_DAYS_ISOLATED
    else:  # window_candidate — stricter
        min_out, min_days = SUPPORTED_MIN_OUTCOMES_CANDIDATE, SUPPORTED_MIN_DAYS_CANDIDATE

    mature = (
        evaluation.negative_baseline_outcomes >= min_out
        and evaluation.distinct_days >= min_days
        and evaluation.confidence >= SUPPORTED_MIN_CONFIDENCE
        and evaluation.candidate_direction_consistency >= 0.8
    )
    return STATUS_SUPPORTED if mature else STATUS_OBSERVING
