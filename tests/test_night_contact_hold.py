"""NightContactHold state machine unit tests.

Tests:
  1. Initial state — all flags False, state_label = "idle"
  2. Feature disabled (night_block_enabled=False) → PASS_THROUGH always
  3. Not in night phase → PASS_THROUGH, vent reset if active
  4. Option A: BLOCK when contact OPEN on night decision
  5. Option A: PASS_THROUGH (+ mark caught_up) when contact CLOSED on night decision
  6. Option A: subsequent cycles remain blocked while contact OPEN
  7. Catch-up: CATCH_UP when blocked + contact closes
  8. Catch-up: no duplicate catch-up after first
  9. Lifecycle reset — state cleared when night ends
  10. Option B: HOLD_NIGHT_VENT when contact opens post catch-up
  11. Option B: maintain HOLD_NIGHT_VENT on consecutive open cycles
  12. Option B: RETURN_TO_NIGHT when contact closes while venting
  13. Option B: not triggered without Option A (night_lift_enabled=True but night_block_enabled=False)
  14. catch_up_pending property — True when blocked but not caught up
  15. state_label reflects correct state
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.engines.night_contact_hold import (
    NightContactAction,
    NightContactHold,
)


def _hold() -> NightContactHold:
    return NightContactHold()


def _eval(hold: NightContactHold, **kwargs) -> str:
    defaults = dict(
        contact_open=False,
        night_active=True,
        night_block_enabled=True,
        night_lift_enabled=False,
        night_decision_pending=False,
    )
    defaults.update(kwargs)
    return hold.evaluate(**defaults)


# ---------------------------------------------------------------------------
# 1. Initial state
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_all_flags_false(self):
        h = _hold()
        assert h.blocked_this_night is False
        assert h.caught_up_this_night is False
        assert h.night_vent_active is False

    def test_state_label_idle(self):
        assert _hold().state_label == "idle"

    def test_catch_up_pending_false(self):
        assert _hold().catch_up_pending is False


# ---------------------------------------------------------------------------
# 2. Feature disabled
# ---------------------------------------------------------------------------

class TestFeatureDisabled:
    def test_pass_through_when_block_disabled(self):
        h = _hold()
        result = _eval(h, night_block_enabled=False, night_decision_pending=True, contact_open=True)
        assert result == NightContactAction.PASS_THROUGH

    def test_no_state_change_when_disabled(self):
        h = _hold()
        _eval(h, night_block_enabled=False, contact_open=True, night_decision_pending=True)
        assert h.blocked_this_night is False


# ---------------------------------------------------------------------------
# 3. Not in night phase
# ---------------------------------------------------------------------------

class TestNotNight:
    def test_pass_through_when_not_night(self):
        h = _hold()
        result = _eval(h, night_active=False, night_decision_pending=True, contact_open=True)
        assert result == NightContactAction.PASS_THROUGH

    def test_vent_reset_when_night_ends(self):
        h = _hold()
        h.night_vent_active = True
        _eval(h, night_active=False)
        assert h.night_vent_active is False


# ---------------------------------------------------------------------------
# 4. Option A: BLOCK
# ---------------------------------------------------------------------------

class TestOptionABlock:
    def test_block_when_contact_open_on_night_decision(self):
        h = _hold()
        result = _eval(h, contact_open=True, night_decision_pending=True)
        assert result == NightContactAction.BLOCK
        assert h.blocked_this_night is True

    def test_no_block_when_contact_closed_on_night_decision(self):
        h = _hold()
        result = _eval(h, contact_open=False, night_decision_pending=True)
        assert result != NightContactAction.BLOCK

    def test_no_block_when_not_night_decision(self):
        h = _hold()
        result = _eval(h, contact_open=True, night_decision_pending=False)
        assert result == NightContactAction.PASS_THROUGH


# ---------------------------------------------------------------------------
# 5. Option A: normal night move (contact CLOSED)
# ---------------------------------------------------------------------------

class TestOptionANormalMove:
    def test_pass_through_and_mark_caught_up_when_closed(self):
        h = _hold()
        result = _eval(h, contact_open=False, night_decision_pending=True)
        assert result == NightContactAction.PASS_THROUGH
        assert h.caught_up_this_night is True
        assert h.blocked_this_night is False

    def test_no_duplicate_caught_up_from_second_night_decision(self):
        h = _hold()
        _eval(h, contact_open=False, night_decision_pending=True)
        result = _eval(h, contact_open=False, night_decision_pending=True)
        # Second cycle: caught_up already True, so the night_decision_pending branch
        # is skipped (condition: not caught_up_this_night). Expect PASS_THROUGH.
        assert result == NightContactAction.PASS_THROUGH


# ---------------------------------------------------------------------------
# 6. Option A: stays blocked while contact OPEN across cycles
# ---------------------------------------------------------------------------

class TestOptionABlockPersists:
    def test_repeated_block_action_each_cycle_while_open(self):
        h = _hold()
        for _ in range(3):
            result = _eval(h, contact_open=True, night_decision_pending=True)
            assert result == NightContactAction.BLOCK


# ---------------------------------------------------------------------------
# 7. Catch-up: CATCH_UP when blocked + contact closes
# ---------------------------------------------------------------------------

class TestCatchUp:
    def test_catch_up_when_blocked_and_contact_closes(self):
        h = _hold()
        _eval(h, contact_open=True, night_decision_pending=True)   # → BLOCK
        result = _eval(h, contact_open=False, night_decision_pending=False)
        assert result == NightContactAction.CATCH_UP
        assert h.caught_up_this_night is True
        assert h.blocked_this_night is False

    def test_return_from_vent_clears_vent(self):
        # Realistic Phase 2 state: night position reached (caught_up) and the
        # cover is venting.  When the window closes it returns to the night
        # position and the vent state is cleared.
        h = _hold()
        h.caught_up_this_night = True
        h.night_vent_active = True
        result = _eval(h, contact_open=False, night_lift_enabled=True,
                       night_decision_pending=False)
        assert result == NightContactAction.RETURN_TO_NIGHT
        assert h.night_vent_active is False


# ---------------------------------------------------------------------------
# 8. No duplicate catch-up
# ---------------------------------------------------------------------------

class TestNoDuplicateCatchUp:
    def test_no_second_catch_up_after_first(self):
        h = _hold()
        _eval(h, contact_open=True, night_decision_pending=True)   # BLOCK
        _eval(h, contact_open=False, night_decision_pending=False)  # CATCH_UP
        result = _eval(h, contact_open=False, night_decision_pending=False)
        assert result != NightContactAction.CATCH_UP


# ---------------------------------------------------------------------------
# 9. Lifecycle reset
# ---------------------------------------------------------------------------

class TestLifecycleReset:
    def test_state_cleared_when_night_ends(self):
        h = _hold()
        h.on_lifecycle_transition(night_active=True)
        _eval(h, contact_open=True, night_decision_pending=True)   # block
        assert h.blocked_this_night is True
        h.on_lifecycle_transition(night_active=False)              # night ends
        assert h.blocked_this_night is False
        assert h.caught_up_this_night is False
        assert h.night_vent_active is False

    def test_no_reset_while_night_continues(self):
        h = _hold()
        h.on_lifecycle_transition(night_active=True)
        _eval(h, contact_open=True, night_decision_pending=True)
        h.on_lifecycle_transition(night_active=True)               # still night
        assert h.blocked_this_night is True

    def test_reset_only_on_night_to_day_transition(self):
        h = _hold()
        h.on_lifecycle_transition(night_active=False)
        h.on_lifecycle_transition(night_active=False)              # day → day: no reset
        assert h.blocked_this_night is False   # nothing to reset, but no error


# ---------------------------------------------------------------------------
# 10. Option B: HOLD_NIGHT_VENT
# ---------------------------------------------------------------------------

class TestOptionB:
    def _setup_post_catchup(self) -> NightContactHold:
        h = _hold()
        _eval(h, contact_open=False, night_decision_pending=True, night_lift_enabled=True)
        assert h.caught_up_this_night is True
        return h

    def test_hold_night_vent_when_contact_opens_post_catchup(self):
        h = self._setup_post_catchup()
        result = _eval(h, contact_open=True, night_decision_pending=False, night_lift_enabled=True)
        assert result == NightContactAction.HOLD_NIGHT_VENT
        assert h.night_vent_active is True

    def test_hold_night_vent_maintained_across_cycles(self):
        h = self._setup_post_catchup()
        _eval(h, contact_open=True, night_decision_pending=False, night_lift_enabled=True)
        for _ in range(3):
            result = _eval(h, contact_open=True, night_decision_pending=False, night_lift_enabled=True)
            assert result == NightContactAction.HOLD_NIGHT_VENT

    def test_return_to_night_when_contact_closes_while_venting(self):
        h = self._setup_post_catchup()
        _eval(h, contact_open=True, night_decision_pending=False, night_lift_enabled=True)
        result = _eval(h, contact_open=False, night_decision_pending=False, night_lift_enabled=True)
        assert result == NightContactAction.RETURN_TO_NIGHT
        assert h.night_vent_active is False


# ---------------------------------------------------------------------------
# 13. Option B not triggered without Option A
# ---------------------------------------------------------------------------

class TestOptionBIndependentOfOptionA:
    def test_vent_without_block_enabled(self):
        # beta.6: Option B (ventilation) is independent of Option A (block).
        # With only Option B enabled and the night move already done, opening the
        # window must lift the cover to the ventilation position.
        h = _hold()
        h.caught_up_this_night = True
        result = _eval(h, contact_open=True, night_block_enabled=False,
                       night_lift_enabled=True, night_decision_pending=False)
        assert result == NightContactAction.HOLD_NIGHT_VENT
        assert h.night_vent_active is True

    def test_open_at_initial_night_without_block_does_not_vent(self):
        # Variant 2: Option B never replaces a pending night move on an already
        # open window.  Option B only, window open as the night move is due →
        # the normal night move proceeds; no vent, and Option B stays disarmed.
        h = _hold()
        result = _eval(h, contact_open=True, night_block_enabled=False,
                       night_lift_enabled=True, night_decision_pending=True)
        assert result == NightContactAction.PASS_THROUGH
        assert h.night_vent_active is False
        assert h.caught_up_this_night is False


# ---------------------------------------------------------------------------
# 14. catch_up_pending
# ---------------------------------------------------------------------------

class TestCatchUpPending:
    def test_pending_when_blocked_not_caught_up(self):
        h = _hold()
        h.blocked_this_night = True
        h.caught_up_this_night = False
        assert h.catch_up_pending is True

    def test_not_pending_when_caught_up(self):
        h = _hold()
        h.blocked_this_night = False
        h.caught_up_this_night = True
        assert h.catch_up_pending is False

    def test_not_pending_in_idle(self):
        assert _hold().catch_up_pending is False


# ---------------------------------------------------------------------------
# 15. state_label
# ---------------------------------------------------------------------------

class TestStateLabel:
    def test_idle(self):
        assert _hold().state_label == "idle"

    def test_blocked(self):
        h = _hold()
        h.blocked_this_night = True
        assert h.state_label == "blocked"

    def test_caught_up(self):
        h = _hold()
        h.caught_up_this_night = True
        assert h.state_label == "caught_up"

    def test_night_vent_active(self):
        h = _hold()
        h.night_vent_active = True
        assert h.state_label == "night_vent_active"

    def test_night_vent_takes_priority_over_caught_up(self):
        h = _hold()
        h.caught_up_this_night = True
        h.night_vent_active = True
        assert h.state_label == "night_vent_active"


# ---------------------------------------------------------------------------
# 16. UNKNOWN contact semantics (contact_unknown=True)
# ---------------------------------------------------------------------------

class TestContactUnknownSemantics:
    """Sensor-fault safety: UNKNOWN must never trigger movement."""

    def test_no_catch_up_when_unknown_while_blocked(self):
        # contact was OPEN → blocked; sensor goes unavailable → UNKNOWN
        h = _hold()
        _eval(h, contact_open=True, night_decision_pending=True)  # BLOCK
        assert h.blocked_this_night is True
        result = _eval(h, contact_open=False, contact_unknown=True, night_decision_pending=False)
        # UNKNOWN while already blocked keeps the cover held (BLOCK), never a
        # catch-up to the night position on an unconfirmed state.
        assert result == NightContactAction.BLOCK
        assert h.blocked_this_night is True    # still blocked
        assert h.caught_up_this_night is False  # no false catch-up

    def test_catch_up_fires_on_definitive_closed_after_unknown(self):
        # After UNKNOWN, a real CLOSED reading should still trigger catch-up.
        h = _hold()
        _eval(h, contact_open=True, night_decision_pending=True)  # BLOCK
        _eval(h, contact_open=False, contact_unknown=True)         # UNKNOWN — no action
        result = _eval(h, contact_open=False, contact_unknown=False)  # definitive CLOSED
        assert result == NightContactAction.CATCH_UP

    def test_no_return_to_night_when_unknown_while_venting(self):
        # contact was OPEN → NIGHT_VENT; sensor goes unavailable → UNKNOWN
        h = _hold()
        _eval(h, contact_open=False, night_decision_pending=True, night_lift_enabled=True)
        assert h.caught_up_this_night is True
        _eval(h, contact_open=True, night_decision_pending=False, night_lift_enabled=True)
        assert h.night_vent_active is True
        # Now sensor goes UNKNOWN (contact_open=False but unknown=True)
        result = _eval(h, contact_open=False, contact_unknown=True,
                       night_decision_pending=False, night_lift_enabled=True)
        assert result == NightContactAction.HOLD_NIGHT_VENT   # maintain vent
        assert h.night_vent_active is True                    # not reset

    def test_return_to_night_fires_on_definitive_closed_after_unknown_vent(self):
        h = _hold()
        _eval(h, contact_open=False, night_decision_pending=True, night_lift_enabled=True)
        _eval(h, contact_open=True, night_decision_pending=False, night_lift_enabled=True)
        _eval(h, contact_open=False, contact_unknown=True,
              night_decision_pending=False, night_lift_enabled=True)  # UNKNOWN — maintain
        result = _eval(h, contact_open=False, contact_unknown=False,
                       night_decision_pending=False, night_lift_enabled=True)
        assert result == NightContactAction.RETURN_TO_NIGHT

    def test_unknown_at_night_decision_blocks_night_move(self):
        # beta.6 safety fix: UNKNOWN at the first night transition cannot confirm
        # the window is closed (e.g. sensor not yet hydrated after an HA restart)
        # → BLOCK the full night move rather than driving the cover down.
        h = _hold()
        result = _eval(h, contact_open=False, contact_unknown=True, night_decision_pending=True)
        assert result == NightContactAction.BLOCK

    def test_unknown_does_not_mark_caught_up(self):
        # UNKNOWN blocks but must NOT mark caught_up, so a later definitive CLOSED
        # still drives the deferred night move (catch-up) and a later OPEN keeps
        # the cover held instead of bypassing the block.
        h = _hold()
        _eval(h, contact_open=False, contact_unknown=True,
              night_decision_pending=True, night_block_enabled=True)
        assert h.caught_up_this_night is False
        assert h.blocked_this_night is True
        # Sensor resolves to CLOSED → no longer blocked; the still-pending night
        # decision is allowed through now that the window is confirmed closed.
        result = _eval(h, contact_open=False, contact_unknown=False,
                       night_decision_pending=True, night_block_enabled=True)
        assert result != NightContactAction.BLOCK
        assert h.caught_up_this_night is True
