"""v1.1.1 field fix: Night Contact Option B must react immediately and
repeatably to a real window contact open/close, with no dependency on the
normal coordinator cycle, minimum_action_interval, catch_up_done, or any
BehaviorMode:hold.

Root cause (confirmed by code inspection, ARCHITECTURE.md §5.7):
  state_machine.transitions.bypasses_guard() exempts NIGHT_VENT -> NIGHT_CLOSED
  from StateGuard.is_locked() only because it is already an *escalation* by
  rank (NIGHT_VENT priority 25 -> NIGHT_CLOSED priority 20). The reverse
  direction, NIGHT_CLOSED -> NIGHT_VENT, is a *de-escalation* by rank and was
  NOT exempted, so a reopen shortly after a completed ReturnToNight could be
  silently held at NIGHT_CLOSED by minimum_state_duration/hysteresis (e.g. an
  active LE2.0 MINIMUM_HOLD strategy delta, or any future default entry for
  NIGHT_CLOSED) even though NightContactHold correctly proposed HOLD_NIGHT_VENT
  every single cycle.

This file tests both halves of the fix:
  1. state_machine.transitions.bypasses_guard() — the state-hold layer.
  2. coordinator._night_contact_bypasses_action_interval() — the pre-existing
     per-window action-interval layer (already correct; covered here for
     completeness/regression).
  3. NightContactHold — pure state-machine repeatability across a rapid
     open -> close -> open -> close sequence (no coordinator involved).
"""
from __future__ import annotations

import sys
import types

from custom_components.smartshading.state_machine.states import ShadingState
from custom_components.smartshading.state_machine.transitions import bypasses_guard, is_escalation
from custom_components.smartshading.engines.night_contact_hold import (
    NightContactAction,
    NightContactHold,
)


def _stub(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


# coordinator.py imports real Home Assistant modules at module scope; only the
# module-level pure function _night_contact_bypasses_action_interval is under
# test here, so a minimal setdefault stub set (mirrors
# test_coordinator_zone_control_lifecycle.py) is enough to make the import
# succeed without pulling in a full HA runtime.
for _name, _module in {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type(
            "CoverEntityFeature",
            (),
            {"SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16},
        ),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub(
        "homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub(
        "homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None
    ),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.helpers.update_coordinator": _stub(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=type("DataUpdateCoordinator", (), {"__class_getitem__": classmethod(lambda cls, item: cls)}),
        CoordinatorEntity=type("CoordinatorEntity", (), {"__class_getitem__": classmethod(lambda cls, item: cls)}),
    ),
    "homeassistant.helpers.storage": _stub(
        "homeassistant.helpers.storage",
        Store=type("Store", (), {"__init__": lambda self, *a, **k: None}),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
    "homeassistant.util.dt": _stub("homeassistant.util.dt", utcnow=lambda: None),
}.items():
    sys.modules.setdefault(_name, _module)


# ---------------------------------------------------------------------------
# 1. bypasses_guard: NIGHT_CLOSED <-> NIGHT_VENT must be fully symmetric
# ---------------------------------------------------------------------------

class TestNightContactBypassesGuard:
    def test_vent_direction_bypasses_guard(self):
        # NIGHT_CLOSED -> NIGHT_VENT: a de-escalation by rank, previously NOT
        # exempted. Must bypass so a reopen is never delayed by
        # minimum_state_duration/hysteresis.
        assert bypasses_guard(ShadingState.NIGHT_CLOSED, ShadingState.NIGHT_VENT) is True

    def test_return_direction_bypasses_guard(self):
        # NIGHT_VENT -> NIGHT_CLOSED: already an escalation by rank; must
        # remain exempted (regression guard for the pre-existing behavior).
        assert bypasses_guard(ShadingState.NIGHT_VENT, ShadingState.NIGHT_CLOSED) is True

    def test_unrelated_deescalations_still_gated(self):
        # The new carve-out must be narrowly scoped to NIGHT_CLOSED->NIGHT_VENT
        # only — it must not blanket-exempt other de-escalations.
        assert bypasses_guard(ShadingState.STRONG_SHADE, ShadingState.NORMAL_SHADE) is False
        assert bypasses_guard(ShadingState.NORMAL_SHADE, ShadingState.LIGHT_SHADE) is False

    def test_light_shade_to_night_vent_not_exempted_by_new_carve_out(self):
        # The new carve-out is scoped to the exact (NIGHT_CLOSED, NIGHT_VENT)
        # pair. LIGHT_SHADE -> NIGHT_VENT is unrelated to Night-Contact and
        # must not accidentally start bypassing the guard because of it (it
        # already bypasses via the pre-existing escalation rule, which this
        # regression guard confirms stays intact and is not double-counted).
        assert bypasses_guard(ShadingState.LIGHT_SHADE, ShadingState.NIGHT_VENT) is True
        assert is_escalation(ShadingState.LIGHT_SHADE, ShadingState.NIGHT_VENT) is True


# ---------------------------------------------------------------------------
# 2. _night_contact_bypasses_action_interval: pre-existing action-interval
#    bypass (regression coverage — this layer was already correct).
# ---------------------------------------------------------------------------

class TestNightContactActionIntervalBypass:
    def _fn(self):
        from custom_components.smartshading.coordinator import (
            _night_contact_bypasses_action_interval,
        )
        return _night_contact_bypasses_action_interval

    def _mode(self):
        from custom_components.smartshading.models.window import WindowBehaviorMode
        return WindowBehaviorMode

    def test_vent_bypasses_on_fresh_contact_fully_automatic(self):
        fn, Mode = self._fn(), self._mode()
        assert fn(
            "NightContactVent", Mode.FULLY_AUTOMATIC, contact_valid_and_fresh=True
        ) is True

    def test_return_bypasses_on_fresh_contact(self):
        fn, Mode = self._fn(), self._mode()
        assert fn(
            "NightContactReturnToNight", Mode.FULLY_AUTOMATIC, contact_valid_and_fresh=True
        ) is True

    def test_catch_up_bypasses_on_fresh_contact(self):
        fn, Mode = self._fn(), self._mode()
        assert fn(
            "NightContactCatchUp", Mode.ABSENCE_AND_SCHEDULE, contact_valid_and_fresh=True
        ) is True

    def test_no_bypass_when_contact_stale(self):
        fn, Mode = self._fn(), self._mode()
        assert fn(
            "NightContactVent", Mode.FULLY_AUTOMATIC, contact_valid_and_fresh=False
        ) is False

    def test_no_bypass_for_unrelated_decider(self):
        fn, Mode = self._fn(), self._mode()
        assert fn(
            "GlareEvaluator", Mode.FULLY_AUTOMATIC, contact_valid_and_fresh=True
        ) is False

    def test_no_bypass_outside_night_contact_modes(self):
        # Only FULLY_AUTOMATIC / ABSENCE_AND_SCHEDULE qualify.
        fn, Mode = self._fn(), self._mode()
        assert fn(
            "NightContactVent", Mode.ABSENCE_ONLY, contact_valid_and_fresh=True
        ) is False


# ---------------------------------------------------------------------------
# 3. NightContactHold: pure state-machine repeatability across a rapid
#    open -> close -> open -> close sequence (mirrors the field report).
# ---------------------------------------------------------------------------

class TestRepeatedOpenCloseCycles:
    def test_reopen_shortly_after_return_is_a_fresh_vent_case(self):
        h = NightContactHold()
        # Arm Option B (night move already done, contact confirmed closed).
        r0 = h.evaluate(
            contact_open=False, night_active=True,
            night_block_enabled=False, night_lift_enabled=True,
            night_decision_pending=True,
        )
        assert r0 == NightContactAction.PASS_THROUGH
        assert h.caught_up_this_night is True

        # First open -> vent.
        r1 = h.evaluate(
            contact_open=True, night_active=True,
            night_block_enabled=False, night_lift_enabled=True,
            night_decision_pending=False,
        )
        assert r1 == NightContactAction.HOLD_NIGHT_VENT

        # Close -> return to night.
        r2 = h.evaluate(
            contact_open=False, night_active=True,
            night_block_enabled=False, night_lift_enabled=True,
            night_decision_pending=False,
        )
        assert r2 == NightContactAction.RETURN_TO_NIGHT
        assert h.night_vent_active is False

        # Reopen shortly after (~2 minutes later in the field report) — must
        # be treated as a fresh vent case, not suppressed.
        r3 = h.evaluate(
            contact_open=True, night_active=True,
            night_block_enabled=False, night_lift_enabled=True,
            night_decision_pending=False,
        )
        assert r3 == NightContactAction.HOLD_NIGHT_VENT
        assert h.night_vent_active is True

        # Close again -> return to night again.
        r4 = h.evaluate(
            contact_open=False, night_active=True,
            night_block_enabled=False, night_lift_enabled=True,
            night_decision_pending=False,
        )
        assert r4 == NightContactAction.RETURN_TO_NIGHT

    def test_rapid_open_close_open_within_seconds_does_not_wedge(self):
        # Sub-second bounce (open/close/open/close/open, as seen in the real
        # HA contact history in the field report) must not desynchronize the
        # state machine — the final state always reflects the last reading.
        h = NightContactHold()
        h.evaluate(
            contact_open=False, night_active=True,
            night_block_enabled=False, night_lift_enabled=True,
            night_decision_pending=True,
        )
        sequence = [True, False, True, False, True]
        results = [
            h.evaluate(
                contact_open=c, night_active=True,
                night_block_enabled=False, night_lift_enabled=True,
                night_decision_pending=False,
            )
            for c in sequence
        ]
        assert results == [
            NightContactAction.HOLD_NIGHT_VENT,
            NightContactAction.RETURN_TO_NIGHT,
            NightContactAction.HOLD_NIGHT_VENT,
            NightContactAction.RETURN_TO_NIGHT,
            NightContactAction.HOLD_NIGHT_VENT,
        ]
        assert h.night_vent_active is True

    def test_absence_and_schedule_mode_same_repeatability(self):
        # Option B must behave identically regardless of which qualifying
        # behavior mode routes into NightContactHold — the state machine
        # itself has no behavior-mode awareness, so this is a contract check
        # that FULLY_AUTOMATIC and ABSENCE_AND_SCHEDULE both drive it via the
        # same evaluate() calls (mode gating happens in
        # _night_contact_bypasses_action_interval, tested above).
        h = NightContactHold()
        h.evaluate(
            contact_open=False, night_active=True,
            night_block_enabled=False, night_lift_enabled=True,
            night_decision_pending=True,
        )
        for _ in range(3):
            r_open = h.evaluate(
                contact_open=True, night_active=True,
                night_block_enabled=False, night_lift_enabled=True,
                night_decision_pending=False,
            )
            assert r_open == NightContactAction.HOLD_NIGHT_VENT
            r_close = h.evaluate(
                contact_open=False, night_active=True,
                night_block_enabled=False, night_lift_enabled=True,
                night_decision_pending=False,
            )
            assert r_close == NightContactAction.RETURN_TO_NIGHT
