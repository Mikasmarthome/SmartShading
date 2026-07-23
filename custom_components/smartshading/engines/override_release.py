"""Manual Override release-strategy resolution — v1.2.0-beta.1, T10.

Single central place where "how does an active override end" is decided,
replacing T7's two-value OverrideDurationMode with a full strategy
architecture (see models/manual_override.OverrideReleaseStrategy). Two
independent, pure, side-effect-free concerns live here:

  1. compute_expiry() — what expires_at to stamp on a NEW or RENEWED
     ManualOverride for a given strategy. Called by OverrideDetector.

  2. resolve_candidate_release() — whether an ACTIVE override should be
     released THIS cycle because Tier 3-5 produced a qualifying candidate
     (a real Comfort or Protection decision). Called by
     evaluate_manual_override_policy().

Deliberately NOT centralized here — these remain their own existing,
unrelated, already-well-tested mechanisms, triggered by fundamentally
different signals (a lifecycle/safety STATE transition, not a per-strategy
release check on a candidate):
  - LIFECYCLE release: engines/lifecycle_guard.lifecycle_should_break_override()
  - Safety release: Tier 1 early-exit + Coordinator's own SAFETY_SHADING_STATES clear
  - MANUAL release: an explicit user action (coordinator.async_clear_manual_override())

No hardcoded per-strategy branching lives in coordinator.py or
OverrideDetector — both call into these two functions and act on the
result, so adding a future strategy only ever touches this module (and the
OverrideReleaseStrategy enum + UI/i18n for it).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from .override_fixed_time import compute_fixed_time_expiry
from ..models.manual_override import OverrideReleaseStrategy
from ..state_machine.states import DecisionCategory

# Sentinel expires_at for a release strategy with no active timeout (MANUAL /
# LIFECYCLE / FIRST_* with safety_timeout_enabled=False). Far enough in the
# future that `now >= expires_at` never fires in practice, while staying a
# concrete, always-comparable datetime — avoids making ManualOverride.expires_at
# Optional, which every existing reader (diagnostics, Learning duration calc,
# support export) already assumes is a real value.
NO_SAFETY_TIMEOUT = datetime(9999, 12, 31, tzinfo=timezone.utc)

# Strategies whose renewal (a further manual movement while already active)
# extends expires_at, matching DURATION's historical, explicitly-tested
# behavior. Every other strategy keeps its original single boundary fixed
# across renewals — mirrors T7's fixed_time behavior, now generalized: a
# long/unbounded single-boundary release is not the kind of "keep touching
# it to keep extending" duration DURATION represents.
_EXTENDS_ON_RENEWAL = frozenset({OverrideReleaseStrategy.DURATION})

# release_strategy -> which DecisionCategory values ends it. Only strategies
# with a non-empty set here are resolved by resolve_candidate_release();
# DURATION/FIXED_TIME/LIFECYCLE/MANUAL are governed by their own mechanisms
# (expires_at, lifecycle_guard, or an explicit user action) and never match.
_CANDIDATE_RELEASE_CATEGORIES: dict[OverrideReleaseStrategy, frozenset[DecisionCategory]] = {
    OverrideReleaseStrategy.FIRST_COMFORT: frozenset({DecisionCategory.COMFORT}),
    OverrideReleaseStrategy.FIRST_PROTECTION: frozenset({DecisionCategory.PROTECTION}),
    OverrideReleaseStrategy.FIRST_ANY_DECISION: frozenset(
        {DecisionCategory.COMFORT, DecisionCategory.PROTECTION}
    ),
}


def compute_expiry(
    *,
    strategy: OverrideReleaseStrategy,
    now: datetime,
    now_local: datetime | None,
    duration_min: int,
    fixed_until: time | None,
    safety_timeout_enabled: bool,
) -> datetime:
    """expires_at for a brand-new (or renewal-extending) ManualOverride.

    `duration_min` is the already scope-resolved value (daytime vs. night —
    the caller, OverrideDetector.tick(), picks which one applies this cycle,
    unchanged from T7). `now_local` is required for FIXED_TIME (a local
    wall-clock time) — see engines/override_fixed_time.py.
    """
    if strategy is OverrideReleaseStrategy.FIXED_TIME:
        if fixed_until is not None and now_local is not None:
            local_expiry = compute_fixed_time_expiry(now=now_local, fixed_until=fixed_until)
            return local_expiry.astimezone(timezone.utc)
        # Defensive fallback (fixed_until not configured / now_local not
        # supplied) — never raises, matches T7's original behavior.
        return now + timedelta(minutes=duration_min)
    if strategy is OverrideReleaseStrategy.DURATION:
        return now + timedelta(minutes=duration_min)
    # LIFECYCLE / FIRST_COMFORT / FIRST_PROTECTION / FIRST_ANY_DECISION / MANUAL:
    # the real release is a different mechanism entirely; expires_at here is
    # only ever the optional defensive safety-net.
    if safety_timeout_enabled:
        return now + timedelta(minutes=duration_min)
    return NO_SAFETY_TIMEOUT


def extends_on_renewal(strategy: OverrideReleaseStrategy) -> bool:
    """True if a further manual movement while already active should push
    expires_at forward again, instead of keeping the original boundary."""
    return strategy in _EXTENDS_ON_RENEWAL


def uses_post_expiry_baseline(strategy: OverrideReleaseStrategy) -> bool:
    """True for every strategy except DURATION — see
    OverrideDetector._maybe_arm_post_expiry_baseline()'s docstring for the
    full rationale (DURATION's existing re-arm-after-stale-cycles behavior
    is historical, tested, and deliberately left unchanged; every other
    strategy's much longer/unbounded single-boundary expiry needs the
    baseline to avoid silently re-arming from a stale, unmoved position)."""
    return strategy is not OverrideReleaseStrategy.DURATION


def resolve_candidate_release(
    *, strategy: OverrideReleaseStrategy, category: DecisionCategory
) -> bool:
    """True if an active override should be released THIS cycle because
    `category` is the kind of decision `strategy` is waiting for.

    Pure function: does not know about allow_comfort/allow_protection (the
    caller, evaluate_manual_override_policy(), combines this with those
    flags to decide whether the *triggering* candidate itself also passes
    through immediately, vs. waiting one more cycle)."""
    return category in _CANDIDATE_RELEASE_CATEGORIES.get(strategy, frozenset())
