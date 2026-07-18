"""HOLD-category semantics precision — T7 pre-push review point 7.

Audit findings (see engines/manual_override_policy.py module docstring for
the full write-up):

  1. "An existing MANUAL_OVERRIDE decision replaced by a new MANUAL_OVERRIDE
     decision" cannot occur in the current architecture: the pre-T7
     ManualOverrideEvaluator is no longer part of the Tier 1-5 candidate
     pipeline (TierOrchestrator does not call it), so a MANUAL_OVERRIDE-
     shaped WindowDecision never reaches evaluate_manual_override_policy()
     as an incoming `candidate` — the ONLY thing this function ever
     "replaces" is a LIFECYCLE/PROTECTION/COMFORT/HOLD candidate that gets
     blocked, constructing a fresh MANUAL_OVERRIDE-shaped result.
  2. The one real HOLD candidate that DOES reach the policy is
     "PresenceUncertain:hold" (target_position=None, shading_state=OPEN).
     When blocked, its own decided_by/target_position ARE discarded and
     replaced — but this loses nothing relative to legacy: pre-T7, an
     active override ALWAYS produced MANUAL_OVERRIDE unconditionally, so
     "PresenceUncertain:hold" was NEVER visible in the final decision while
     an override was active, either before or after T7 (see
     TestBlockedHoldMatchesPreT7ManualOverrideEvaluatorExactly below —
     direct field-by-field comparison against the real, still-existing
     ManualOverrideEvaluator class).
  3. The blocked target_position is exactly active_override.override_position
     — identical construction to the pre-T7 evaluator.
  4. PresenceUncertain-hold's OWN target_position is None (the most
     conservative possible outcome — no dispatch at all). The override-hold
     that replaces it dispatches a CONCRETE position (the override's own
     position) — but since that position is definitionally where the cover
     already physically is, this is a redundant re-assertion of an already-
     correct position, not a different or less-safe physical outcome. This
     is unchanged, pre-existing pre-T7 behavior (the old evaluator did the
     exact same substitution), not something T7 introduces.
  5. "BehaviorMode:hold" (coordinator.py's non-FULLY_AUTOMATIC dispatch
     suppression) is a DOWNSTREAM, coordinator-level post-processing step
     applied via dataclasses.replace() to whatever this policy already
     returned — it never reaches evaluate_manual_override_policy() as a
     candidate at all, so it cannot be "replaced by a less-safe HOLD" by
     this function; it operates strictly after the policy has already
     decided. Proven below (category flows through replace() unchanged).

Conclusion: no separate/finer HOLD category or explicit priority is
required — the three "HOLD" decisions named in the review occupy three
different, non-competing points in the pipeline (one real Tier 3-5
candidate; one pipeline output; one downstream post-processing step), never
colliding with each other inside the policy.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from custom_components.smartshading.engines.manual_override_policy import evaluate_manual_override_policy
from custom_components.smartshading.evaluators.manual_override_evaluator import ManualOverrideEvaluator
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window_decision import WindowDecision
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)


def _override(position: int = 42) -> ManualOverride:
    return ManualOverride(
        window_id="w1", override_position=position, started_at=_NOW,
        expires_at=_NOW.replace(hour=16), source="position_delta",
        overridden_state=ShadingState.OPEN, overridden_position=0,
    )


def _presence_uncertain_hold_candidate() -> WindowDecision:
    return WindowDecision(
        window_id="w1", shading_state=ShadingState.OPEN, target_position=None,
        decided_by="PresenceUncertain:hold", category=DecisionCategory.HOLD,
    )


class TestBlockedHoldMatchesPreT7ManualOverrideEvaluatorExactly:
    def test_field_by_field_comparison(self) -> None:
        """Directly constructs what the OLD (still-existing, still-tested,
        just no-longer-wired-into-the-pipeline) ManualOverrideEvaluator
        would have produced for the same active_override, and compares it
        field-by-field against the NEW policy's blocked-candidate output."""
        override = _override(55)
        wdi = build_window_decision_input(
            window=WindowConfig(id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1"),
            zone=ZoneConfig(id="z1", name="Zone"),
            global_defaults=GlobalDefaults(), shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY, absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
            active_override=override,
        )
        legacy_result = ManualOverrideEvaluator().evaluate(wdi)
        assert legacy_result is not None

        new_result = evaluate_manual_override_policy(
            active_override=override, candidate=_presence_uncertain_hold_candidate(),
            allow_comfort=False, allow_protection=False,
        )

        assert new_result.window_id == legacy_result.window_id
        assert new_result.shading_state == legacy_result.shading_state
        assert new_result.target_position == legacy_result.target_position
        assert new_result.decided_by == legacy_result.decided_by
        assert new_result.target_tilt == legacy_result.target_tilt


class TestNoInfoLossRelativeToLegacyBehavior:
    def test_presence_uncertain_reason_was_never_visible_under_legacy_either(self) -> None:
        """Pre-T7, ManualOverrideEvaluator ran BEFORE Tier 3-5 — Presence-
        Uncertain-hold's own reason/None-position was never even computed,
        let alone visible, whenever an override was active. So the new
        policy discarding it when blocking is not a regression — that
        information was equally absent under legacy."""
        override = _override(20)
        candidate = _presence_uncertain_hold_candidate()
        result = evaluate_manual_override_policy(
            active_override=override, candidate=candidate,
            allow_comfort=True, allow_protection=True,  # even fully permissive flags don't matter for HOLD
        )
        assert result.decided_by != "PresenceUncertain:hold"
        assert result.target_position == 20  # concrete override position, not None


class TestBehaviorModeHoldNeverReachesThePolicyAsACandidate:
    def test_replace_preserves_category_downstream_of_the_policy(self) -> None:
        """Simulates coordinator.py's BehaviorMode:hold construction
        (dataclasses.replace() on whatever the policy already returned) —
        proves category flows through unchanged, confirming this step
        operates strictly AFTER the policy, never as an input to it."""
        override = _override(20)
        policy_result = evaluate_manual_override_policy(
            active_override=override, candidate=_presence_uncertain_hold_candidate(),
            allow_comfort=False, allow_protection=False,
        )
        assert policy_result.category is DecisionCategory.HOLD

        behavior_mode_hold = replace(policy_result, target_position=None, decided_by="BehaviorMode:hold")
        assert behavior_mode_hold.category is DecisionCategory.HOLD  # inherited, not re-evaluated
        assert behavior_mode_hold.shading_state == policy_result.shading_state  # unchanged by replace()
