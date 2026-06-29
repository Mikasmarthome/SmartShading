"""Per-window state machine for night-contact behavior.

Option A (night_block_on_window_open) and Option B (night_lift_on_window_open)
are INDEPENDENT contact-night features.  Both require a configured window
contact, but neither requires the other:

  - Option A — block: while the window is open (or its state cannot be confirmed
    closed) at night, the full night move is blocked.  Option A never lifts or
    vents; it only holds the cover until the window is safely closed.
  - Option B — ventilation: while the window is open at night, the cover is
    driven at most to the configured ventilation position instead of the full
    night position; when the window closes it returns to the night position.

When BOTH are enabled and the window is open, Option B wins (the ventilation
position is the more specific, deliberately configured action).

The state machine is in-memory and resets when the night phase ends.
After a restart during night, the machine starts fresh — the coordinator
re-derives the correct state from the current contact sensor reading.

State transitions (decided by contact state each cycle):
  Any state  →  reset()         on morning/day transition
  any        →  blocked         contact OPEN + Option A only (no Option B)
  any        →  night_vent      contact OPEN + Option B enabled
  any        →  blocked         contact UNKNOWN (and not already venting)
  blocked    →  caught_up       contact CLOSED during night (catch-up move)
  night_vent →  caught_up       contact CLOSED while venting (return to night)

UNKNOWN contact semantics (conservative — UNKNOWN is never treated as CLOSED):
  - The full night move is BLOCKED and caught_up is NOT marked; a definitive
    CLOSED reading is required before the cover moves to the night position.
  - No NEW active vent move is started on an unconfirmed state.  If the cover
    was already venting (window was known open before the fault) the vent
    position is maintained rather than dropping to night.
  - Catch-up and RETURN_TO_NIGHT never fire on UNKNOWN.

Conservative restart semantics:
  After restart with contact OPEN during night → block the night move
    (behaves as if freshly blocked; catch_up_done=False after restart).
  After restart with contact UNKNOWN during night → also block the night move
    (cannot confirm the window is closed) until a definitive reading arrives;
    a later CLOSED then drives the deferred move, a later OPEN keeps it held.
  After restart with contact CLOSED during night → normal night move.
  The "catch-up already done" guard works only within a single HA session.
  If HA restarts and contact is still open, a new night move will be blocked
  and a new catch-up will fire when the contact closes. This is correct and
  safe (one extra catch-up per restart-during-blocked-night is acceptable).
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

        # The contact only modifies an actual full-night intent: a pending
        # NIGHT_CLOSED decision, an already-established block, or an active vent.
        _night_close_relevant = (
            (night_decision_pending and not self.caught_up_this_night)
            or self.blocked_this_night
            or self.night_vent_active
            or self.caught_up_this_night
        )

        # --- Contact OPEN ----------------------------------------------------
        if contact_open:
            if night_lift_enabled:
                # Option B is the more specific action and is preferred over a
                # plain block when both options are active: move to (or hold) the
                # ventilation position instead of the full night position.  Only
                # acts when there is a night-close intent to downgrade, an active
                # vent to maintain, or a completed night move to lift.
                if _night_close_relevant:
                    self.night_vent_active = True
                    self.blocked_this_night = False
                    return NightContactAction.HOLD_NIGHT_VENT
                return NightContactAction.PASS_THROUGH
            # Option A only: block the full night move; A never lifts/vents.
            if (night_decision_pending and not self.caught_up_this_night) or (
                    self.blocked_this_night and not self.caught_up_this_night):
                self.night_vent_active = False
                self.blocked_this_night = True
                return NightContactAction.BLOCK
            return NightContactAction.PASS_THROUGH

        # --- Contact UNKNOWN / UNAVAILABLE / stale ---------------------------
        if contact_unknown:
            # Conservative for BOTH options: the window cannot be confirmed
            # closed (e.g. the sensor has not hydrated after an HA restart).
            # Never drive to the full night position and never start a NEW active
            # vent move on an unconfirmed state.  If we were already venting (the
            # window was known open before the fault) keep the vent position;
            # otherwise block the night move until a definitive reading arrives.
            # Never mark caught_up.
            if self.night_vent_active:
                return NightContactAction.HOLD_NIGHT_VENT
            if (night_decision_pending and not self.caught_up_this_night) or (
                    self.blocked_this_night and not self.caught_up_this_night):
                self.blocked_this_night = True
                return NightContactAction.BLOCK
            return NightContactAction.PASS_THROUGH

        # --- Contact CLOSED (definitive) -------------------------------------
        if self.night_vent_active:
            # Was venting; the window is now closed → return to night position.
            self.night_vent_active = False
            self.caught_up_this_night = True
            self.blocked_this_night = False
            return NightContactAction.RETURN_TO_NIGHT
        if self.blocked_this_night and not self.caught_up_this_night:
            # The full night move was deferred while the window was open/unknown
            # → catch up now that it is confirmed closed.
            self.blocked_this_night = False
            self.caught_up_this_night = True
            return NightContactAction.CATCH_UP
        # Normal night move (or the cover is already at night): let the tier
        # decision through and record that the night move has happened.
        if night_decision_pending and not self.caught_up_this_night:
            self.caught_up_this_night = True
            self.blocked_this_night = False
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
