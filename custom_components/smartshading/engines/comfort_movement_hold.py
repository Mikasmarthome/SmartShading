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
throttles repeated REAL comfort-tier position changes (Solar/Heat/Glare),
leaving every other decision path (Safety, Night, Night Contact, Absence,
Manual Override, cover-availability recovery, fallback/open) completely
untouched and immediate, exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: Deciders whose target changes are subject to the comfort movement hold.
#: TierOrchestrator:fallback (open) is intentionally excluded — opening up is
#: always safe/desired, and the FIRST comfort dispatch after a non-comfort
#: decision (fallback/open, night, absence, safety, manual override) is a
#: genuine new entry into shading, not a repeated flip, so it must never be
#: held either. Safety/Night/NightContact/Absence/ManualOverride evaluators
#: use their own decided_by strings, none of which appear here, so they are
#: never subject to this hold.
COMFORT_DECIDERS: frozenset[str] = frozenset({
    "SolarEvaluator", "HeatEvaluator", "GlareEvaluator",
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
            is_strong_escalation=tier_decision.shading_state is ShadingState.STRONG_SHADE,
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
        hold_minutes: float = COMFORT_MOVEMENT_MIN_HOLD_MINUTES,
    ) -> bool:
        """True when this proposed comfort dispatch should be held back.

        Held only when ALL of:
          - the proposed decision is itself a comfort decider,
          - the PREVIOUS confirmed dispatch was also a comfort decider (so
            this is a comfort-to-comfort switch, not a first entry into
            shading after a non-comfort event),
          - the proposed target differs from the last dispatched target
            (same-position is already a natural CommandFilter no-op and must
            not be counted as a held movement),
          - it is not a strong escalation (STRONG_SHADE) — a genuinely
            stronger protective decision may still act immediately,
          - less than `hold_minutes` have elapsed since the last dispatch.
        """
        if proposed_decided_by not in COMFORT_DECIDERS:
            return False
        if self.last_decided_by not in COMFORT_DECIDERS:
            return False
        if self.last_target_ha == proposed_target_ha:
            return False
        if is_strong_escalation:
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
