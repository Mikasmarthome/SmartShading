"""Per-window state machine for night-contact behavior.

Tracks whether the automatic night move was blocked (Option A), whether a
catch-up move has already been executed this night, and whether the cover
is currently in NIGHT_VENT position (Option B).

The state machine is in-memory and resets when the night phase ends.
After a restart during night, the machine starts fresh — the coordinator
re-derives the correct state from the current contact sensor reading.

State transitions:
  Any state  →  reset()         on morning/day transition
  idle       →  blocked         Option A active + night transition + contact OPEN
  idle       →  caught_up       night transition + contact CLOSED (normal night move done)
  blocked    →  caught_up       contact closes during night (triggers catch-up move)
  caught_up  →  night_vent      Option B active + contact opens while caught_up
  night_vent →  caught_up       contact closes while in NIGHT_VENT

Conservative restart semantics:
  After restart with contact OPEN during night → block the night move
    (behaves as if freshly blocked; catch_up_done=False after restart).
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
            False for CLOSED or UNKNOWN (conservative: UNKNOWN → treat as CLOSED
            for the purpose of not blocking a night move).
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
        if not night_active or not night_block_enabled:
            # Feature disabled or not in night phase — no modification.
            if self.night_vent_active:
                # Safety: if we somehow lost night context while in vent, reset.
                self.night_vent_active = False
            return NightContactAction.PASS_THROUGH

        # --- Option A: block the night move if contact is OPEN ---------------

        if night_decision_pending and not self.caught_up_this_night:
            if contact_open:
                # Block the night move this cycle.
                self.blocked_this_night = True
                return NightContactAction.BLOCK
            else:
                # Contact is closed, night move is being executed normally.
                # Mark as caught_up so we don't re-block after window opens.
                if not self.caught_up_this_night:
                    self.caught_up_this_night = True
                    self.blocked_this_night = False
                return NightContactAction.PASS_THROUGH

        # --- Catch-up: contact closed after a blocked night ------------------

        if self.blocked_this_night and not contact_open and not self.caught_up_this_night:
            self.blocked_this_night = False
            self.caught_up_this_night = True
            self.night_vent_active = False
            return NightContactAction.CATCH_UP

        # --- Option B: lift/return while in night context --------------------

        if night_lift_enabled and self.caught_up_this_night:
            if contact_open and not self.night_vent_active:
                # Window opened after night move was done → go to NIGHT_VENT.
                self.night_vent_active = True
                return NightContactAction.HOLD_NIGHT_VENT

            if self.night_vent_active and not contact_open:
                # Window closed while in NIGHT_VENT → return to night position.
                self.night_vent_active = False
                return NightContactAction.RETURN_TO_NIGHT

            if self.night_vent_active and contact_open:
                # Maintain NIGHT_VENT position each cycle while open.
                return NightContactAction.HOLD_NIGHT_VENT

        # No modification needed.
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
