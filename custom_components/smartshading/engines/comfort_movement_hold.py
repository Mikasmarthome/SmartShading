"""Comfort Movement Stability Hold — v1.1.1 field fix.

Real-world report: two west-facing living-room windows dispatched real cover
commands repeatedly every 5-15 minutes, alternating between SolarEvaluator's
normal_shade target and GlareEvaluator's light_shade target as the measured
exposure hovered near both evaluators' entry thresholds. Neither evaluator was
individually flapping — this is a NORMAL_SHADE<->LIGHT_SHADE alternation
driven by PositionResolver.resolve() picking whichever of {SolarEvaluator,
HeatEvaluator, GlareEvaluator} currently fires with the higher (more-shaded)
target_position, which can differ cycle to cycle as measured exposure crosses
different evaluators' thresholds. The v1.1.0-beta era GlareEvaluator
STRONG-exit hysteresis (const.py GLARE_INTENSITY_STRONG_EXIT_RATIO) does not
apply here — no STRONG_SHADE involved, and the flip is across evaluators, not
within GlareEvaluator's own intensity scaling.

The State Guard's escalation-always-bypasses-hold rule (see
state_machine/transitions.py bypasses_guard()) is by design and must not be
weakened — it is what makes STORM_SAFE/MANUAL_OVERRIDE detection instantly
responsive. But that same rule means a de-escalation (NORMAL_SHADE ->
LIGHT_SHADE, lower priority) is throttled by minimum_state_duration while the
reverse escalation (LIGHT_SHADE -> NORMAL_SHADE, higher priority) is NOT,
producing an asymmetric, repeated real-world dispatch cycle whenever the
underlying measurement hovers near the boundary between two evaluators.

This is a narrow, ADDITIONAL, independent hold — it does not modify
StateGuard, minimum_state_duration, or bypasses_guard() at all. It only
throttles repeated REAL non-priority position changes for the same window
(Solar/Heat/Glare and, since the v1.1.2 field-fix follow-up, the daytime
OPEN fallback — see NON_PRIORITY_DECIDERS below), leaving every genuinely
prioritized decision path (Safety, Night, Night Contact, Absence, Manual
Override, cover-availability recovery) completely untouched and immediate,
exactly as before.

v1.1.2 field-fix follow-up (two confirmed loopholes closed):
  1. STRONG_SHADE no longer bypasses the hold unconditionally. A prior bare
     `shading_state is STRONG_SHADE` check let any threshold-boundary flicker
     immediately escalate (e.g. a real "30 -> 10" dispatch within minutes).
     No robust numeric escalation margin is available at the call site
     without duplicating each evaluator's own threshold/ratio logic in the
     coordinator, so — per explicit product decision — STRONG_SHADE is now
     held exactly like any other non-priority movement. A late-but-stable
     strong escalation is preferred over frequent, unstable movement. The
     `is_strong_escalation` parameter is kept on should_hold() as a hook for
     a future evidence-based margin, but the coordinator currently always
     passes False.
  2. "TierOrchestrator:fallback" (daytime OPEN) is now itself a member of
     NON_PRIORITY_DECIDERS. Previously a brief fallback/open interlude reset
     the hold (recording a non-comfort dispatch let the next comfort move
     resume immediately); now it is held/blocked like any other non-priority
     transition, and does not reset the timer for the position it interrupted.

v1.1.2 second follow-up — confirmed-exit carve-out for Fallback/Open:
  Blocking every Fallback/Open unconditionally (point 2 above) risks the
  opposite failure: a window whose sun/glare exposure has genuinely and
  robustly ended would stay artificially shaded for up to 60 minutes. The
  coordinator already computes, once per window per cycle regardless of this
  hold, whether the window is currently inside its effective solar sector
  (automatic azimuth/elevation tolerance, minus any manual sector override or
  obstruction zone) — a hysteresis-free geometric fact, unlike measured
  solar/glare intensity which can hover near an evaluator's threshold.
  `should_hold()` accepts `is_confirmed_exit` for exactly this: when the
  coordinator proposes Fallback/Open AND the window is confirmed OUT of its
  solar sector this cycle, the hold is bypassed for that dispatch. This is
  deliberately NOT extended to Solar/Heat/Glare comfort proposals or to
  STRONG_SHADE — only Fallback/Open gets this carve-out, and only on the
  geometric out-of-sector fact, never on exposure/intensity thresholds (those
  remain exactly as noisy as before and are not duplicated here). Because the
  confirmed-exit open is still recorded via record_dispatch() like any other
  real dispatch, it becomes the new "last non-priority dispatch" — so a
  comfort proposal shortly afterwards (e.g. the sun re-entering the sector at
  a boundary) is itself held against the fresh OPEN position, preventing an
  immediate open -> shade -> open flap in the other direction.

F27 field fix — protective shade after fallback open must not be held:
  Real-world report: a window opened via the daytime OPEN fallback (nothing
  fired that cycle) and, at the next cycle, GlareEvaluator/HeatEvaluator/
  SolarEvaluator correctly detected exposure and proposed a lower (more
  shaded) target — but the hold blocked it for up to 60 minutes because the
  fallback open is itself a NON_PRIORITY_DECIDERS member (point 2 above), so
  it looked like an ordinary comfort-to-comfort switch. Unlike the removed
  STRONG_SHADE bypass, this carve-out is narrow: it only fires when the LAST
  dispatch was specifically the fallback open (never a genuine comfort tier),
  the PROPOSED decider is one of Solar/Heat/Glare, and the proposed target is
  strictly lower (more shade) than the fallback's target. It never applies
  between Solar/Heat/Glare proposals themselves, and never for an opening
  move — those keep being held exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: Deciders whose target changes are subject to the movement stability hold.
#:
#: v1.1.2 field-fix follow-up: "TierOrchestrator:fallback" (the daytime OPEN
#: fallback) was previously excluded on the reasoning that "opening up is
#: always safe/desired". In practice this let a brief, noise-driven
#: fallback/open interlude reset the hold entirely — the comfort dispatch
#: right before it stopped counting as "the last real movement" the moment
#: fallback/open was recorded, so a repeated comfort move right after was
#: treated as a fresh first entry and fired immediately. Fallback/open is
#: only ever reached when NO higher-priority tier (Safety, Manual Override,
#: Night, Absence, Heat, Glare, Solar) produced a decision (see
#: evaluators/tier_orchestrator.py) — it never has a "hard end reason" of its
#: own, so treating it as a non-priority movement like Solar/Heat/Glare is
#: safe and does not affect any genuinely prioritized transition.
#:
#: Safety/Night/NightContact/Absence/ManualOverride evaluators use their own
#: decided_by strings, none of which appear here, so they remain fully
#: exempt from this hold, exactly as before.
NON_PRIORITY_DECIDERS: frozenset[str] = frozenset({
    "SolarEvaluator", "HeatEvaluator", "GlareEvaluator",
    "TierOrchestrator:fallback",
})

#: Default minimum time between two DIFFERENT comfort-tier dispatches for the
#: same window (v1.1.1 field fix). Matches the explicitly requested "about
#: once per hour" comfort movement cadence — well above the ~5-15 minute
#: alternation period observed in the field report, while still allowing a
#: fresh comfort dispatch within a single afternoon if conditions genuinely
#: and durably change.
COMFORT_MOVEMENT_MIN_HOLD_MINUTES: float = 60.0


@dataclass
class ComfortMovementHold:
    """Per-window tracker for the last confirmed dispatch (of any kind).

    Records every confirmed SENT dispatch — comfort or not — so a non-comfort
    event (safety, night, absence, manual-override release) correctly clears
    the hold: the next comfort decision after such an event is always treated
    as a fresh entry into shading, never held.

    Usage (coordinator, once per window per cycle)::

        hold = _comfort_movement_holds.setdefault(window_id, ComfortMovementHold())
        held = hold.should_hold(
            proposed_decided_by=tier_decision.decided_by,
            proposed_target_ha=target_ha,
            is_strong_escalation=False,  # no unconditional STRONG_SHADE bypass (see module docstring)
            is_confirmed_exit=(
                tier_decision.decided_by == "TierOrchestrator:fallback"
                and not effective_in_solar_sector
            ),
            now=now,
        )
        # ... feed `not held` into CommandFilter.evaluate(comfort_hold_allowed=...)
        # ... after a confirmed SENT dispatch:
        hold.record_dispatch(decided_by=tier_decision.decided_by, target_ha=target_ha, now=now)
    """

    last_decided_by: str | None = None
    last_target_ha: int | None = None
    last_dispatch_at: datetime | None = None

    def should_hold(
        self,
        *,
        proposed_decided_by: str,
        proposed_target_ha: int | None,
        is_strong_escalation: bool,
        now: datetime,
        is_confirmed_exit: bool = False,
        hold_minutes: float = COMFORT_MOVEMENT_MIN_HOLD_MINUTES,
    ) -> bool:
        """True when this proposed non-priority dispatch should be held back.

        Held only when ALL of:
          - the proposed decision is itself a non-priority decider (Solar/
            Heat/Glare comfort tiers, or the daytime OPEN fallback),
          - the PREVIOUS confirmed dispatch was also a non-priority decider
            (so this is a non-priority-to-non-priority switch, not a first
            entry after a genuinely prioritized event — Safety, Night,
            Night Contact, Absence, Manual Override),
          - the proposed target differs from the last dispatched target
            (same-position is already a natural CommandFilter no-op and must
            not be counted as a held movement),
          - `is_strong_escalation` is not set — currently never passed True
            by the coordinator (see module docstring: STRONG_SHADE no longer
            bypasses this hold), kept as a hook for a future evidence-based
            margin,
          - `is_confirmed_exit` is not set — the coordinator passes True only
            for a Fallback/Open proposal on a window confirmed OUT of its
            solar sector this cycle (see module docstring), never for Solar/
            Heat/Glare comfort proposals or STRONG_SHADE,
          - the proposal is not a protective move directly after a fallback
            open (F27: last dispatch was "TierOrchestrator:fallback" and
            this proposal is Solar/Heat/Glare wanting a strictly lower, more
            shaded target — see module docstring),
          - less than `hold_minutes` have elapsed since the last dispatch.
        """
        if proposed_decided_by not in NON_PRIORITY_DECIDERS:
            return False
        if self.last_decided_by not in NON_PRIORITY_DECIDERS:
            return False
        if self.last_target_ha == proposed_target_ha:
            return False
        if is_strong_escalation:
            return False
        if is_confirmed_exit:
            return False
        if (
            self.last_decided_by == "TierOrchestrator:fallback"
            and proposed_decided_by in ("GlareEvaluator", "HeatEvaluator", "SolarEvaluator")
            and proposed_target_ha is not None
            and self.last_target_ha is not None
            and proposed_target_ha < self.last_target_ha
        ):
            return False
        if self.last_dispatch_at is None:
            return False
        elapsed = now - self.last_dispatch_at
        return elapsed < timedelta(minutes=hold_minutes)

    def age_minutes(self, now: datetime) -> float | None:
        """Minutes since the last recorded dispatch, or None if never dispatched."""
        if self.last_dispatch_at is None:
            return None
        return (now - self.last_dispatch_at).total_seconds() / 60.0

    def hold_remaining_minutes(
        self, now: datetime, hold_minutes: float = COMFORT_MOVEMENT_MIN_HOLD_MINUTES,
    ) -> float | None:
        """Minutes remaining in the current hold window, or None when not held
        (never dispatched, or the hold window has already elapsed)."""
        age = self.age_minutes(now)
        if age is None:
            return None
        remaining = hold_minutes - age
        return remaining if remaining > 0 else None

    def record_dispatch(self, *, decided_by: str, target_ha: int | None, now: datetime) -> None:
        """Record a confirmed SENT dispatch. Call for EVERY sent dispatch —
        comfort or not — so a non-comfort event correctly clears the hold."""
        self.last_decided_by = decided_by
        self.last_target_ha = target_ha
        self.last_dispatch_at = now
