"""ManualOverridePolicy — T7 pure decision function for Manual Override gating.

Single central place where "is this action allowed while a Manual Override
is active?" is decided. No distributed `if override:` checks anywhere else —
every Tier 3/4/5 evaluator remains completely unaware that Manual Override
exists (unchanged from before T7; see evaluators/*.py module docstrings).

v1.2.0-beta.1, T10: also the single central place where "does this candidate
qualify to RELEASE the active override" is decided, via
engines/override_release.resolve_candidate_release() — set as
WindowDecision.release_override on the returned decision. Choosing
FIRST_COMFORT / FIRST_PROTECTION / FIRST_ANY_DECISION as the override's
release_strategy implicitly treats the *triggering* candidate as allowed
through this same cycle (so the user sees the shading move immediately when
the override ends, not a one-cycle-delayed catch-up) — independent of the
allow_comfort/allow_protection flags, which remain a separate, orthogonal
knob for passthrough-without-ending-the-override under any strategy.

Pure function: no HA state, no I/O, no override-state mutation, no dispatch.
Does not call OverrideDetector — the caller (TierOrchestrator) passes the
already-resolved `active_override` and the already-computed Tier 3/4/5
winning candidate.

Policy matrix (ARCHITECTURE.md-equivalent, T7):
    No active override        → candidate unchanged.
    SAFETY candidate          → always allowed (defensive; SAFETY candidates
                                 never actually reach this function in
                                 practice — Tier 1 early-exits in
                                 TierOrchestrator before this policy runs).
    LIFECYCLE candidate       → always blocked while an override is active.
                                 T7 introduces NO new allow-switch for
                                 Lifecycle — the existing
                                 override_break_on_lifecycle /
                                 lifecycle_should_break_override() mechanism
                                 (engines/lifecycle_guard.py, unchanged) is
                                 the only way a lifecycle transition ends an
                                 active override; by the time that happens,
                                 active_override is already None here.
    PROTECTION candidate      → allowed only if allow_protection=True.
    COMFORT candidate         → allowed only if allow_comfort=True.
    HOLD candidate            → the ONLY real-world HOLD-tagged candidate
                                 that ever reaches this function is
                                 "PresenceUncertain:hold" (constructed inside
                                 TierOrchestrator itself, before the Tier 2
                                 gate). Two other decisions are sometimes
                                 informally called "HOLD" but never reach
                                 this function as a *candidate*:
                                   - The pre-T7 ManualOverrideEvaluator's own
                                     MANUAL_OVERRIDE decision is no longer
                                     part of the Tier 1-5 candidate pipeline
                                     at all (that evaluator is not called
                                     from TierOrchestrator any more) — so
                                     "an existing MANUAL_OVERRIDE being
                                     replaced by a new MANUAL_OVERRIDE" is
                                     not a scenario this function can ever
                                     encounter; this function's own BLOCKED
                                     output is a fresh construction, not a
                                     replacement of an incoming candidate.
                                   - "BehaviorMode:hold" (coordinator.py's
                                     non-FULLY_AUTOMATIC dispatch
                                     suppression) is applied via
                                     dataclasses.replace() to whatever this
                                     function already returned — i.e. it
                                     runs AFTER this policy, downstream, and
                                     is never itself evaluated by it.
                                 ALSO blocked while an override is active
                                 (converted to the MANUAL_OVERRIDE hold
                                 below), NOT auto-passed. This is required
                                 for legacy parity: pre-T7, an active
                                 override always won via ManualOverrideEvaluator's
                                 Tier-2 early exit regardless of what Tier
                                 3-5 would otherwise have decided — including
                                 a candidate as inert as
                                 "PresenceUncertain:hold" (target_position=
                                 None). If a HOLD candidate were exempted
                                 here, that specific edge case would leak
                                 the underlying non-override decision through
                                 an active override, breaking the T7
                                 legacy-oracle guarantee. A HOLD-tagged
                                 candidate reaching this function while no
                                 override is active is unaffected (returned
                                 by the "no active override" branch above).

When a candidate is blocked, the returned WindowDecision holds the cover at
the override's own position — identical in shape to the pre-T7
ManualOverrideEvaluator result (same shading_state, decided_by, and target
position), so every downstream consumer that pattern-matches on
ShadingState.MANUAL_OVERRIDE or decided_by="ManualOverrideEvaluator"
(coordinator.py's NightHardHold exemption, OverrideDetector.tick()'s
smartshading_target reference, diagnostics/support export, Learning) keeps
working unchanged for the legacy-default configuration.

An ALLOWED Protection/Comfort candidate is returned exactly as produced by
its own evaluator (its own shading_state/decided_by/position) — the active
override itself is NOT cleared and its expires_at is NOT touched by this
function; only OverrideDetector (via its own tick()/clear() calls, driven
by the coordinator) ever changes override state.
"""
from __future__ import annotations

from dataclasses import replace

from .override_release import resolve_candidate_release
from ..models.manual_override import ManualOverride, OverrideReleaseStrategy
from ..models.window_decision import WindowDecision
from ..state_machine.states import DecisionCategory, ShadingState

_ALLOW_ALWAYS = frozenset({DecisionCategory.SAFETY})


def evaluate_manual_override_policy(
    *,
    active_override: ManualOverride | None,
    candidate: WindowDecision,
    allow_comfort: bool,
    allow_protection: bool,
    release_strategy: OverrideReleaseStrategy = OverrideReleaseStrategy.LIFECYCLE,
) -> WindowDecision:
    """Return the effective WindowDecision after applying Manual Override policy.

    Args:
        active_override: The currently active override for this window, or
            None if no override is active.
        candidate: The WindowDecision that Tier 3/4/5 (Lifecycle/Protection/
            Comfort) would produce this cycle, with its category already set.
        allow_comfort: Whether COMFORT-category candidates may proceed while
            an override is active (independent of release_strategy).
        allow_protection: Whether PROTECTION-category candidates may proceed
            while an override is active (independent of release_strategy).
        release_strategy: The active override's configured release strategy
            (v1.2.0-beta.1, T10) — only FIRST_COMFORT / FIRST_PROTECTION /
            FIRST_ANY_DECISION affect this function's outcome; every other
            value behaves exactly as if this parameter were absent.

    Returns:
        `candidate` unchanged if no override is active or its category is
        always-allowed. Otherwise, if this candidate qualifies to release
        the override (per release_strategy) or its category's allow-flag is
        set, `candidate` with `release_override` set accordingly. Otherwise
        a MANUAL_OVERRIDE hold decision at the override's position.
    """
    if active_override is None:
        return candidate

    category = candidate.category
    if category in _ALLOW_ALWAYS:
        return candidate

    release_now = resolve_candidate_release(strategy=release_strategy, category=category)

    if category is DecisionCategory.PROTECTION and (allow_protection or release_now):
        return replace(candidate, release_override=release_now) if release_now else candidate
    if category is DecisionCategory.COMFORT and (allow_comfort or release_now):
        return replace(candidate, release_override=release_now) if release_now else candidate

    return WindowDecision(
        window_id=candidate.window_id,
        shading_state=ShadingState.MANUAL_OVERRIDE,
        target_position=active_override.override_position,
        decided_by="ManualOverrideEvaluator",
        category=DecisionCategory.HOLD,
    )
