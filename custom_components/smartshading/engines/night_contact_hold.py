"""Per-window state machine for night-contact behavior.

Option A (night_block_on_window_open) and Option B (night_lift_on_window_open)
are INDEPENDENT contact-night features.  Both require a configured window
contact, but neither requires the other:

The two options act in two distinct phases of the night:

  - Phase 1 — before the night position is reached.  Option A governs the
    initial full night move: while the window is open (Option A enabled) or its
    state cannot be confirmed closed, the move is blocked and the cover is held
    until the window is safely closed.  Option A never lifts or vents.  Option B
    plays NO role in this phase — it never replaces a pending night move on an
    already-open window.  When Option A is disabled the night move proceeds
    regardless of the contact (only a definitive CLOSED arms Option B).
  - Phase 2 — after the night position has been reached (caught_up, which is set
    only on a confirmed CLOSED).  Option B governs ventilation: if the window is
    opened later in the night the cover moves to the configured ventilation
    position, and returns to the night position when the window closes again.

So when BOTH are enabled and the window is already open as the night move is
due, Option A blocks completely (no ventilation); Option B only ever acts on a
*later* opening once the night position has been reached.

The state machine is in-memory and resets when the night phase ends.
After a restart during night, the machine starts fresh — the coordinator
re-derives the correct state from the current contact sensor reading.

State transitions:
  Any state  →  reset()         on morning/day transition
  phase 1    →  blocked         Option A + contact OPEN/UNKNOWN (night move held)
  phase 1    →  caught_up       contact CLOSED (night move done; arms Option B)
  blocked    →  caught_up       contact CLOSED during night (catch-up move)
  caught_up  →  night_vent      Option B + contact OPEN (later opening)
  night_vent →  caught_up       contact CLOSED while venting (return to night)

UNKNOWN contact semantics (conservative — UNKNOWN is never treated as CLOSED):
  - Phase 1 with Option A: the full night move is BLOCKED and caught_up is NOT
    marked; a definitive CLOSED reading is required first.
  - Phase 2: no NEW active vent move is started on an unconfirmed state.  An
    already-active vent is maintained rather than dropping to night.
  - Catch-up and RETURN_TO_NIGHT never fire on UNKNOWN.

Conservative restart semantics:
  After restart with contact OPEN/UNKNOWN during night (Option A enabled) →
    block the night move until a definitive CLOSED reading arrives; a later
    CLOSED then drives the deferred move.
  After restart with contact CLOSED during night → normal night move.
  The "catch-up already done" guard works only within a single HA session.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NightContactHold:
    """Per-window state tracker for night-contact behavior.

    All state is reset when the night phase ends (reset() call in coordinator).
    The coordinator updates this object once per cycle for each window that has
    a contact sensor configured.

    Usage pattern (coordinator)::

        hold = _night_contact_holds[window_id]
        hold.on_lifecycle_transition(new_lifecycle_state)
        action = hold.evaluate(
            contact_open=contact_reading.status is ContactStatus.OPEN,
            night_active=lifecycle_state is LifecycleState.NIGHT,
            night_block_enabled=wdi.effective_behavior.night_block_on_window_open,
            night_lift_enabled=wdi.effective_behavior.night_lift_on_window_open,
            night_decision_pending=tier_decision.shading_state is ShadingState.NIGHT_CLOSED,
        )
        # Apply action to modify tier_decision accordingly.
    """

    # True during the night in which the night move was blocked (Option A).
    blocked_this_night: bool = False

    # True once the catch-up move was dispatched this night.
    # Prevents duplicate catch-up moves across coordinator cycles.
    caught_up_this_night: bool = False

    # True while the cover is commanded to NIGHT_VENT position (Option B).
    night_vent_active: bool = False

    # Internal: track night start to allow reset on transition.
    _last_known_night: bool = field(default=False, compare=False, repr=False)

    def on_lifecycle_transition(self, night_active: bool) -> None:
        """Call once per cycle before evaluate().

        Resets the night-specific state when the night phase ends.
        """
        if self._last_known_night and not night_active:
            self._reset()
        self._last_known_night = night_active

    def _reset(self) -> None:
        self.blocked_this_night = False
        self.caught_up_this_night = False
        self.night_vent_active = False

    def evaluate(
        self,
        *,
        contact_open: bool,
        contact_unknown: bool = False,
        night_active: bool,
        night_block_enabled: bool,
        night_lift_enabled: bool,
        night_decision_pending: bool,
    ) -> "NightContactAction":
        """Determine what the coordinator should do this cycle.

        Parameters
        ----------
        contact_open:
            True if the contact sensor reports OPEN (window physically open).
            False for CLOSED or UNKNOWN.
        contact_unknown:
            True when the sensor is unavailable, stale-unknown, or absent.
            When True: the initial night move is BLOCKED (cannot confirm the
            window is closed) and catch-up / RETURN_TO_NIGHT are suppressed, so
            a sensor fault never drives the cover to night position nor triggers
            an unexpected movement.
        night_active:
            True when the current lifecycle phase is NIGHT.
        night_block_enabled:
            Option A flag from effective_behavior (night_block_on_window_open).
        night_lift_enabled:
            Option B flag from effective_behavior (night_lift_on_window_open).
        night_decision_pending:
            True when TierOrchestrator returned NIGHT_CLOSED this cycle
            (i.e. NightEvaluator fired).

        Returns
        -------
        NightContactAction
            One of: PASS_THROUGH, BLOCK, CATCH_UP, HOLD_NIGHT_VENT, RETURN_TO_NIGHT.
        """
        # Gate: contact-based night logic acts only during the night phase and
        # only when at least one option is enabled.  Option A and Option B are
        # INDEPENDENT — either may be enabled on its own (both require a contact,
        # enforced by the config flow).  When neither is enabled the contact has
        # no effect on the night decision.
        if not night_active or (not night_block_enabled and not night_lift_enabled):
            if self.night_vent_active:
                # Lost night context while venting — reset.
                self.night_vent_active = False
            return NightContactAction.PASS_THROUGH

        # === Phase 1: the night position has NOT yet been reached ============
        # Option A governs whether the initial (or deferred) full night move
        # happens.  Option B plays NO role here: it never replaces a pending
        # night move on an already-open window.  caught_up is set only when the
        # night position is reached with a CONFIRMED CLOSED contact, so it also
        # arms Option B (which reacts to a *later* opening).
        if not self.caught_up_this_night:
            if not (night_decision_pending or self.blocked_this_night):
                # No full night move to make yet — nothing to do.
                return NightContactAction.PASS_THROUGH
            if night_block_enabled and (contact_open or contact_unknown):
                # Option A: the window is open or its state cannot be confirmed
                # closed → block the full night move.  Never vent, never mark
                # caught_up; a definitive CLOSED reading is required first.
                self.night_vent_active = False
                self.blocked_this_night = True
                return NightContactAction.BLOCK
            if not contact_open and not contact_unknown:
                # Definitive CLOSED → perform the night move and arm Option B.
                was_blocked = self.blocked_this_night
                self.blocked_this_night = False
                self.caught_up_this_night = True
                return (NightContactAction.CATCH_UP if was_blocked
                        else NightContactAction.PASS_THROUGH)
            # Option A disabled and contact OPEN/UNKNOWN: the normal night move
            # proceeds (Option B does not block or downgrade it).  caught_up is
            # NOT set, so Option B stays disarmed until the window is confirmed
            # closed at night — it only vents on a *later* opening.
            return NightContactAction.PASS_THROUGH

        # === Phase 2: the night position has been reached ====================
        # Option B governs ventilation when the window is opened later in the
        # night.  Option A has no further effect once the night move is done.
        if night_lift_enabled:
            if contact_open:
                # Window opened after the night position was reached → vent.
                self.night_vent_active = True
                return NightContactAction.HOLD_NIGHT_VENT
            if contact_unknown:
                # No NEW active move on an unconfirmed state; keep an active vent.
                if self.night_vent_active:
                    return NightContactAction.HOLD_NIGHT_VENT
                return NightContactAction.PASS_THROUGH
            # Definitive CLOSED.
            if self.night_vent_active:
                self.night_vent_active = False
                return NightContactAction.RETURN_TO_NIGHT
            return NightContactAction.PASS_THROUGH
        return NightContactAction.PASS_THROUGH

    # --- Diagnostic helpers --------------------------------------------------

    @property
    def catch_up_pending(self) -> bool:
        """True when a catch-up move is still waiting for the contact to close."""
        return self.blocked_this_night and not self.caught_up_this_night

    @property
    def state_label(self) -> str:
        """Human-readable state for diagnostics."""
        if self.night_vent_active:
            return "night_vent_active"
        if self.blocked_this_night:
            return "blocked"
        if self.caught_up_this_night:
            return "caught_up"
        return "idle"


class NightContactAction:
    """Action codes returned by NightContactHold.evaluate()."""

    PASS_THROUGH    = "pass_through"    # no modification; use tier_decision as-is
    BLOCK           = "block"           # suppress the NIGHT_CLOSED move; stay put
    CATCH_UP        = "catch_up"        # override to NIGHT_CLOSED (deferred move)
    HOLD_NIGHT_VENT = "hold_night_vent" # set/maintain NIGHT_VENT position
    RETURN_TO_NIGHT = "return_to_night" # return from NIGHT_VENT to NIGHT_CLOSED
