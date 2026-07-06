"""Absence-release restart persistence — v1.1.3 field fix.

Real-world report: a west-facing living-room door (ABSENCE_ONLY) stayed
closed at its absence position for hours after presence returned home.
Support export evidence for the affected window (24h timeline):

    16:46 dispatch_sent    AbsenceEvaluator   shading_state=absence_closed  target_ha=30
    17:42 command_blocked  BehaviorMode:hold  shading_state=open  reason=no_target_position

and, further back in the same 24h window, a FIVE-HOUR continuous stretch
(08:42-13:23) of "shading_state=open, decided_by=BehaviorMode:hold,
reason=no_target_position" — i.e. the tier evaluation had correctly
determined OPEN (absence no longer active) for hours, yet the dispatch
was blocked every single cycle.

Root cause: `_is_absence_release` (coordinator.py, the "Behavior Mode
Dispatch Suppression" block) only allows the ABSENCE_CLOSED -> OPEN
release dispatch when ``current_state is ShadingState.ABSENCE_CLOSED``.
``self._current_states`` is an in-memory-only dict — unlike
OverrideDetector's active overrides, it was never restart-persisted. On
an HA restart/reload while a window is legitimately ABSENCE_CLOSED, the
next cycle reads ``self._current_states.get(window_id, ShadingState.OPEN)``
and gets the OPEN default instead. The window is then permanently
outside the ``_is_absence_release`` gate: it is not ABSENCE_CLOSED (so no
release is recognised) and plain OPEN is not itself an allowed dispatch
target for ABSENCE_ONLY/ABSENCE_AND_SCHEDULE (see _mode_dispatch_allowed)
-- so it is stuck on BehaviorMode:hold/no_target_position, with the cover
still physically sitting at the old absence position, until the next full
absence-away/absence-return cycle happens to pass through ABSENCE_CLOSED
again.

Fix: ``current_states`` is now persisted and restored exactly like
``active_overrides`` (engines/learning_persistence.py: serialize/
deserialize + RestoreExtras field; coordinator.py: `_build_save_kwargs()`
+ the restore block). This test file covers the persistence roundtrip and
documents the bug mechanism via the coordinator's existing pure functions
(``_mode_dispatch_allowed`` / ``_hold_state_for_no_dispatch``) -- no new
architecture, no behavior-mode masking changes, no evaluator changes.

BehaviorMode is otherwise untouched: normal daytime solar/heat/glare
suppression for ABSENCE_ONLY/ABSENCE_AND_SCHEDULE, Safety, Manual
Override, Night Contact, and Morning/lifecycle release are unaffected.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# HA stubs — installed before coordinator import (mirrors
# test_morning_release_behavior_modes.py's established pattern).
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    def __class_getitem__(cls, item): return cls
    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
    def async_request_refresh(self) -> None: pass


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


for _name, _mod in {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {
            "SET_POSITION": 1, "SET_TILT_POSITION": 2,
            "OPEN": 4, "CLOSE": 8, "STOP": 16,
        }),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub("homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub("homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}.items():
    sys.modules.setdefault(_name, _mod)

import datetime as _datetime
_dt_mod = sys.modules.get("homeassistant.util.dt")
if _dt_mod is None or not hasattr(_dt_mod, "utcnow"):
    sys.modules["homeassistant.util.dt"] = _stub(
        "homeassistant.util.dt",
        utcnow=lambda: _datetime.datetime.now(_datetime.timezone.utc),
        DEFAULT_TIME_ZONE=_datetime.timezone.utc,
    )

sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type(
        "CE", (),
        {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None},
    ),
)
sys.modules["homeassistant.helpers.storage"] = _stub("homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)
import custom_components.smartshading.coordinator as _coord_mod  # noqa: E402

from custom_components.smartshading.models.window import WindowBehaviorMode  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402
from custom_components.smartshading.engines.learning_persistence import (  # noqa: E402
    LearningPersistenceConfig,
    RestoreExtras,
    serialize_learning_store,
    deserialize_into_learning_store,
)
from custom_components.smartshading.engines.learning_store import LearningStore  # noqa: E402

_mode_dispatch_allowed = _coord_mod._mode_dispatch_allowed
_hold_state_for_no_dispatch = _coord_mod._hold_state_for_no_dispatch

_NOW = datetime(2026, 7, 5, 17, 42, 51, tzinfo=timezone.utc)


def _is_absence_release(
    *, behavior_mode: WindowBehaviorMode, current_state: ShadingState,
    proposed_state: ShadingState, active_override: object | None = None,
) -> bool:
    """Mirror the coordinator's inline `_is_absence_release` expression."""
    return (
        behavior_mode in (WindowBehaviorMode.ABSENCE_ONLY, WindowBehaviorMode.ABSENCE_AND_SCHEDULE)
        and current_state is ShadingState.ABSENCE_CLOSED
        and proposed_state is ShadingState.OPEN
        and active_override is None
    )


# ===========================================================================
# Bug-mechanism documentation (pure functions, no restart-persistence yet)
# ===========================================================================

class TestBugMechanismWithoutRestoredState:
    """Reproduces the reported stuck-window mechanism via the coordinator's
    own pure functions, given the field-observed timeline: absence ends,
    but current_state is not (or no longer) ABSENCE_CLOSED."""

    def test_absence_release_recognised_when_current_state_is_absence_closed(self):
        allowed = _is_absence_release(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
            current_state=ShadingState.ABSENCE_CLOSED,
            proposed_state=ShadingState.OPEN,
        )
        assert allowed is True

    def test_absence_release_not_recognised_when_current_state_reset_to_open(self):
        # This is the exact stuck scenario: current_state defaulted back to
        # OPEN (e.g. after a restart that lost in-memory current_states),
        # even though the window is still physically at its absence
        # position and the tier now proposes OPEN.
        allowed = _is_absence_release(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
            current_state=ShadingState.OPEN,
            proposed_state=ShadingState.OPEN,
        )
        assert allowed is False

    def test_plain_open_is_not_independently_allowed_for_absence_only(self):
        # Confirms the window has no other way out: OPEN alone (without the
        # is_absence_release carve-out) is not in the ABSENCE_ONLY allowlist.
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.OPEN,
            is_absence_release=False, is_lifecycle_release=False,
        ) is False

    def test_stuck_window_repeats_behavior_mode_hold_every_cycle(self):
        # Once current_state has desynced (is_absence_release=False forever
        # until a fresh absence cycle), every cycle's tier_decision keeps
        # getting overwritten to BehaviorMode:hold, exactly as the support
        # export's repeated 5h timeline of command_blocked/no_target_position
        # events showed.
        for _ in range(5):
            allowed = _mode_dispatch_allowed(
                WindowBehaviorMode.ABSENCE_ONLY, ShadingState.OPEN,
                is_absence_release=False, is_lifecycle_release=False,
            )
            assert allowed is False

    def test_hold_state_for_no_dispatch_preserves_absence_closed_while_absence_active(self):
        # This part is NOT the bug -- while absence is genuinely still
        # active, repeated BehaviorMode:hold cycles correctly keep
        # current_state at ABSENCE_CLOSED (this is by design, see
        # _NO_DISPATCH_HOLD_DECIDERS).
        held_state = _hold_state_for_no_dispatch(
            "BehaviorMode:hold", ShadingState.OPEN, ShadingState.ABSENCE_CLOSED,
        )
        assert held_state is ShadingState.ABSENCE_CLOSED


# ===========================================================================
# Fix: current_states restart persistence (engines/learning_persistence.py)
# ===========================================================================

class TestCurrentStatesPersistenceRoundtrip:
    def _config(self) -> LearningPersistenceConfig:
        return LearningPersistenceConfig()

    def test_serialize_includes_current_states(self):
        store = LearningStore()
        data = serialize_learning_store(
            store, self._config(), _NOW,
            current_states={"w1": ShadingState.ABSENCE_CLOSED, "w2": ShadingState.OPEN},
        )
        assert data["current_states"] == {"w1": "absence_closed", "w2": "open"}

    def test_serialize_defaults_to_empty_dict_when_not_provided(self):
        store = LearningStore()
        data = serialize_learning_store(store, self._config(), _NOW)
        assert data["current_states"] == {}

    def test_deserialize_restores_current_states(self):
        store = LearningStore()
        data = serialize_learning_store(
            store, self._config(), _NOW,
            current_states={"w1": ShadingState.ABSENCE_CLOSED},
        )
        extras = deserialize_into_learning_store(data, LearningStore(), self._config(), _NOW)
        assert extras.current_states == {"w1": "absence_closed"}

    def test_deserialize_skips_unknown_state_value_without_raising(self):
        store = LearningStore()
        data = serialize_learning_store(
            store, self._config(), _NOW,
            current_states={"w1": ShadingState.ABSENCE_CLOSED},
        )
        data["current_states"]["w2"] = "not_a_real_state"
        extras = deserialize_into_learning_store(data, LearningStore(), self._config(), _NOW)
        assert extras.current_states == {"w1": "absence_closed"}

    def test_deserialize_tolerates_missing_current_states_key(self):
        store = LearningStore()
        data = serialize_learning_store(store, self._config(), _NOW)
        del data["current_states"]
        extras = deserialize_into_learning_store(data, LearningStore(), self._config(), _NOW)
        assert extras.current_states == {}

    def test_restore_extras_current_states_defaults_to_empty_dict(self):
        # Constructing RestoreExtras without current_states must not raise
        # (field has a default_factory, like active_overrides).
        extras = RestoreExtras(
            pending_outcomes=[], config_generations={}, thermal_models={},
            thermal_observations={}, window_contribution_models={},
            window_contribution_evidence={}, shadow_proposals=[],
            bounded_experiments=[], persistent_adoptions=[],
            strategy_experiments=[], persistent_strategy_adoptions=[],
            consumed_experiment_ledger={}, shadow_tombstones=[],
            owner_entry_id=None, owner_zone_id=None, restore_diagnostics={},
            config_snapshot={},
        )
        assert extras.current_states == {}


# ===========================================================================
# Fix verified end-to-end via the pure release-gate expression: a restored
# ABSENCE_CLOSED current_state (as the coordinator would apply from
# extras.current_states after restart) unlocks the release exactly like the
# never-restarted case.
# ===========================================================================

class TestFixUnlocksReleaseAfterSimulatedRestartRestore:
    def test_restored_absence_closed_state_allows_release(self):
        data = serialize_learning_store(
            LearningStore(), LearningPersistenceConfig(), _NOW,
            current_states={"living_room_door": ShadingState.ABSENCE_CLOSED},
        )
        extras = deserialize_into_learning_store(
            data, LearningStore(), LearningPersistenceConfig(), _NOW)

        # Coordinator restore step: current_states[wid] = ShadingState(value).
        restored_current_state = ShadingState(extras.current_states["living_room_door"])
        assert restored_current_state is ShadingState.ABSENCE_CLOSED

        allowed = _is_absence_release(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
            current_state=restored_current_state,
            proposed_state=ShadingState.OPEN,
        )
        assert allowed is True

    def test_absence_and_schedule_also_benefits_from_restored_state(self):
        data = serialize_learning_store(
            LearningStore(), LearningPersistenceConfig(), _NOW,
            current_states={"w": ShadingState.ABSENCE_CLOSED},
        )
        extras = deserialize_into_learning_store(
            data, LearningStore(), LearningPersistenceConfig(), _NOW)
        restored = ShadingState(extras.current_states["w"])
        assert _is_absence_release(
            behavior_mode=WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            current_state=restored,
            proposed_state=ShadingState.OPEN,
        ) is True


# ===========================================================================
# Non-regression: bypasses and daytime suppression untouched by this fix.
# ===========================================================================

class TestUnaffectedPathsStillCorrect:
    def test_safety_still_allowed_for_absence_only(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.STORM_SAFE,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_manual_override_still_allowed_for_absence_only(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.MANUAL_OVERRIDE,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_absence_closed_itself_still_allowed(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.ABSENCE_CLOSED,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_night_closed_still_not_allowed_for_absence_only(self):
        # ABSENCE_ONLY forces lifecycle_state=DAY -- night tiers never fire,
        # and this fix does not change that.
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.NIGHT_CLOSED,
            is_absence_release=False, is_lifecycle_release=False,
        ) is False

    def test_fully_automatic_is_never_gated_by_this_fix(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC, ShadingState.OPEN,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True
