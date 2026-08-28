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

B3-010 field fix — MorningEvaluator joins the non-priority pool:
  MorningEvaluator (evaluators/morning_evaluator.py) fires exactly once, on
  the NIGHT->MORNING transition cycle, and never again — the ordinary DAY
  cycle immediately following it has no Morning candidate at all and
  resolves via whichever tier genuinely applies, typically the plain
  Fallback/Open. Without this fix, that immediately-following Fallback/Open
  proposal would dispatch a real, undesired second movement the very next
  cycle (e.g. a configured HA 70% morning position followed one cycle later
  by a full HA 100% open), even though nothing in the outside world actually
  changed between the two cycles — the SAME class of problem this module
  already solves for Solar<->Glare<->Fallback alternation.
  "MorningEvaluator" is therefore added to NON_PRIORITY_DECIDERS below and to
  the F27 protective-carve-out condition in should_hold(): the immediately
  following Fallback/Open is held against Morning's last dispatched target
  for the same COMFORT_MOVEMENT_MIN_HOLD_MINUTES/FALLBACK_OPEN_RELEASE_CYCLES
  window ordinary daytime operation already uses, while a genuinely stronger
  Solar/Heat/Glare protective need (wanting a strictly more shaded target
  than Morning just set) still bypasses the hold immediately, exactly like it
  already bypasses a hold recorded against a prior Fallback/Open. No new
  state is introduced: this widens membership of the SAME runtime-only,
  non-persisted, per-window ComfortMovementHold instances already created
  once per window and reused every cycle (coordinator.py's
  _comfort_movement_holds dict) — on a real restart or config-entry reload
  the dict starts empty exactly like today, so the very first post-restart/
  reload dispatch (Morning or Fallback/Open) is never artificially held; the
  hold only ever throttles a SECOND dispatch against a FIRST one already
  observed live this run.

F29 field fix — exit-debounce / confirmed release for Fallback/Open:
  Real-world report: HeatEvaluator (and, by the same shape, Solar/Glare)
  reads its outdoor/indoor temperature and solar-exposure inputs fresh every
  cycle with no smoothing or hysteresis (see evaluators/heat_evaluator.py).
  A single free cycle where none of Tier 4/5 fires — a threshold-hovering
  reading, not a genuine, durable clearing of heat/glare/solar protection —
  let "TierOrchestrator:fallback" dispatch a full OPEN, only for the very
  next cycle to re-trigger HeatEvaluator and shade back down: a visible
  open-then-close flap five minutes apart. The F27 bypass above is correct
  and unrelated (it concerns a comfort re-target AFTER an open has already
  been dispatched); this fix addresses whether the fallback-open should have
  dispatched at all on a single outlier cycle.
  `should_delay_fallback_open()` tracks, per window, how many CONSECUTIVE
  cycles in a row have proposed "TierOrchestrator:fallback" (regardless of
  whether the resulting command actually dispatched). A fallback proposal is
  held back until it has been proposed for `FALLBACK_OPEN_RELEASE_CYCLES`
  (2) consecutive cycles in a row; any other decider — Safety, Night, Night
  Contact, Absence, Manual Override, or a genuine Solar/Heat/Glare
  protective decision — resets the counter to zero immediately, since only
  a fallback proposal itself is ever delayed here. A confirmed, geometric
  solar-sector exit (`is_confirmed_exit`, same hysteresis-free fact used by
  the F27/v1.1.2 carve-outs above) skips the debounce, exactly like it
  already skips the movement-stability hold itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..cover_control.position_semantics import DEFAULT_POSITION_TOLERANCE_INTERNAL

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
#: "MorningEvaluator" (B3-010 field fix, see module docstring above): the
#: one-cycle MORNING transition dispatch is itself non-priority, and its
#: recorded target is what the immediately following Fallback/Open proposal
#: is held against — this is what prevents the unwanted second movement.
NON_PRIORITY_DECIDERS: frozenset[str] = frozenset({
    "SolarEvaluator", "HeatEvaluator", "GlareEvaluator",
    "TierOrchestrator:fallback", "MorningEvaluator",
})

#: Default minimum time between two DIFFERENT comfort-tier dispatches for the
#: same window (v1.1.1 field fix). Matches the explicitly requested "about
#: once per hour" comfort movement cadence — well above the ~5-15 minute
#: alternation period observed in the field report, while still allowing a
#: fresh comfort dispatch within a single afternoon if conditions genuinely
#: and durably change.
COMFORT_MOVEMENT_MIN_HOLD_MINUTES: float = 60.0

#: Number of consecutive "TierOrchestrator:fallback" cycles required before
#: a fallback OPEN proposal is actually allowed to dispatch (F29 field fix).
#: A single free cycle is treated as a possible threshold-hovering outlier,
#: not a confirmed, durable clearing of heat/glare/solar protection.
FALLBACK_OPEN_RELEASE_CYCLES: int = 2


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
    #: Consecutive "TierOrchestrator:fallback" cycles observed so far this
    #: run (F29 exit-debounce). Runtime-only, resets to 0 on HA restart —
    #: same non-persistence as every other field on this class.
    pending_fallback_open_release_count: int = 0

    def should_hold(
        self,
        *,
        proposed_decided_by: str,
        proposed_target_ha: int | None,
        is_strong_escalation: bool,
        now: datetime,
        is_confirmed_exit: bool = False,
        hold_minutes: float = COMFORT_MOVEMENT_MIN_HOLD_MINUTES,
        actual_position_ha: int | None = None,
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
          - `actual_position_ha` (B3-010 R4/R5, fact-based release) is either
            unknown or still within DEFAULT_POSITION_TOLERANCE_INTERNAL of
            `last_target_ha` — i.e. the cover is still physically where it
            was last (successfully) dispatched to, allowing for sensor
            rounding/reporting noise (R5: a strict `!=` treated a 1-point
            rounding difference the same as a genuine divergence — replaced
            with the same canonical tolerance CommandFilter itself uses for
            "close enough to be the same position"). The moment the REAL
            observed position diverges from that by MORE than the tolerance
            (a manual move, an external actor, a restart landing after the
            cover was already moved elsewhere) the "unchanged facts" premise
            this hold exists to protect is no longer true, so the hold
            releases immediately regardless of `hold_minutes` — a genuine
            fact, not a clock, ends it.
            Known limitation (R5, not yet resolved): this check cannot by
            itself distinguish "the cover is still travelling toward
            last_target_ha" from "the cover was stopped/moved away and will
            never reach it" — both look identical here (actual position
            outside tolerance, target unchanged). The coordinator call site
            is responsible for not calling should_hold() with a stale
            actual_position_ha snapshot taken mid-travel; this module has no
            travel-duration or completion-state input to make that
            distinction itself without either duplicating
            cover_control.dispatch_completion's completion detection here or
            being passed an explicit "dispatch still in flight" flag — a
            larger, separate change deliberately not made in this pass.
          - less than `hold_minutes` have elapsed since the last dispatch
            (the remaining, bounded safety-net cap for the case neither a
            stronger tier nor a position-drift fact ever arrives — see
            module docstring "B3-010 field fix" for why an unbounded hold
            would regress to the rejected permanent all-day baseline).
        """
        if proposed_decided_by not in NON_PRIORITY_DECIDERS:
            return False
        if self.last_decided_by not in NON_PRIORITY_DECIDERS:
            return False
        if self.last_target_ha == proposed_target_ha:
            return False
        if is_strong_escalation:
            return False
        if (
            actual_position_ha is not None
            and self.last_target_ha is not None
            and abs(actual_position_ha - self.last_target_ha) > DEFAULT_POSITION_TOLERANCE_INTERNAL
        ):
            return False
        # is_confirmed_exit is a geometric "sun genuinely left this window's
        # solar sector" fact — its trust rationale (module docstring, F27/
        # v1.1.2 second follow-up) is about Solar/Heat/Glare/Fallback
        # alternation, where the exit fact is directly relevant to whatever
        # was previously held. It has NO logical connection to a
        # MorningEvaluator-dispatched target, which is never solar-driven —
        # a north-facing/out-of-sector window's Morning position must not
        # lose its hold protection merely because that window happens to be
        # geometrically out of sector every cycle (B3-010 fix: exclude ONLY
        # the Morning-held case from this carve-out; every other
        # last_decided_by keeps the original, broader bypass).
        if is_confirmed_exit and self.last_decided_by != "MorningEvaluator":
            return False
        if (
            # B3-010: "MorningEvaluator" joins this carve-out alongside
            # "TierOrchestrator:fallback" — neither is ever a protective
            # decision, so a genuinely stronger Solar/Heat/Glare need must
            # bypass the hold immediately after either one, exactly the
            # same way, rather than being throttled for up to
            # COMFORT_MOVEMENT_MIN_HOLD_MINUTES.
            self.last_decided_by in ("TierOrchestrator:fallback", "MorningEvaluator")
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

    def should_delay_fallback_open(
        self,
        *,
        proposed_decided_by: str,
        is_confirmed_exit: bool = False,
        required_consecutive_cycles: int = FALLBACK_OPEN_RELEASE_CYCLES,
    ) -> bool:
        """True when this cycle's "TierOrchestrator:fallback" OPEN proposal
        must be held back (F29 exit-debounce / confirmed release).

        Updates the internal consecutive-fallback-cycle counter as a side
        effect — call exactly once per window per cycle, passing the SAME
        `proposed_decided_by` used for `should_hold()` this cycle:

          - Any decider OTHER than "TierOrchestrator:fallback" — Safety,
            Night, Night Contact, Absence, Manual Override, or a genuine
            Solar/Heat/Glare protective decision — resets the counter to
            zero and returns False immediately. This gate only ever delays
            the fallback-open proposal itself; every other decision path is
            untouched.
          - `is_confirmed_exit=True` (the same hysteresis-free geometric
            solar-sector-exit fact used by should_hold()) also resets the
            counter and returns False: a confirmed sector exit is trusted
            immediately, exactly like it already skips the movement
            stability hold.
          - Otherwise the counter increments, and this cycle's proposal is
            held back (returns True) until it has been observed for
            `required_consecutive_cycles` consecutive cycles in a row —
            i.e. a single free cycle is treated as a possible
            threshold-hovering outlier, never as a confirmed release.
        """
        if proposed_decided_by != "TierOrchestrator:fallback" or is_confirmed_exit:
            self.pending_fallback_open_release_count = 0
            return False
        self.pending_fallback_open_release_count += 1
        return self.pending_fallback_open_release_count < required_consecutive_cycles

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
