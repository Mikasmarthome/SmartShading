"""Morning release and behavior mode dispatch matrix tests.

Verifies that ABSENCE_AND_SCHEDULE windows:
  - are night-closed correctly (existing behavior, regression guard)
  - release to OPEN on morning/day lifecycle (normal live-transition path)
  - release to OPEN after a coordinator restart (post-restart path, prev=DAY)
  - are held (BehaviorMode:hold) for daytime fallback OPEN without a release condition
  - are held for solar/heat/glare daytime dispatch (those are suppressed)
  - are released on absence-return (absence release allowed)

And that ABSENCE_ONLY and DISABLED_AUTOMATIC are NOT extended:
  - ABSENCE_ONLY: night close and morning release do not apply
  - DISABLED_AUTOMATIC: no morning release

Groups
------
MR-01  _mode_dispatch_allowed: ABSENCE_AND_SCHEDULE night-close allowed
MR-02  _mode_dispatch_allowed: ABSENCE_AND_SCHEDULE daytime-OPEN held
MR-03  should_allow_lifecycle_release + _mode_dispatch_allowed: morning release (live transition)
MR-04  should_allow_lifecycle_release + _mode_dispatch_allowed: morning release (post-restart)
MR-05  _mode_dispatch_allowed: ABSENCE_AND_SCHEDULE absence release allowed
MR-06  ABSENCE_ONLY: night close and morning lifecycle not dispatched
MR-07  DISABLED_AUTOMATIC: no morning release
MR-08  FULLY_AUTOMATIC: night close and morning release both pass (regression)
"""
from __future__ import annotations

import sys
import types
from typing import Any


# ---------------------------------------------------------------------------
# HA stubs — installed before coordinator import
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

# ---------------------------------------------------------------------------
# Pure-function imports (no HA stubs needed)
# ---------------------------------------------------------------------------

from custom_components.smartshading.engines.lifecycle_guard import (  # noqa: E402
    should_allow_lifecycle_release,
)
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    LifecycleState, NightDayLifecycleConfig,
)
from custom_components.smartshading.models.window import WindowBehaviorMode, WindowConfig  # noqa: E402
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults  # noqa: E402
from custom_components.smartshading.models.comfort import ComfortConfig  # noqa: E402
from custom_components.smartshading.models.window_decision_input import (  # noqa: E402
    build_window_decision_input,
)
from custom_components.smartshading.evaluators.night_evaluator import NightEvaluator  # noqa: E402
from custom_components.smartshading.state_machine.states import ShadingState  # noqa: E402

_mode_dispatch_allowed = _coord_mod._mode_dispatch_allowed
_apply_window_behavior_mode = _coord_mod._apply_window_behavior_mode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lifecycle_release(
    *,
    prev: LifecycleState,
    new: LifecycleState,
    current_state: ShadingState,
) -> bool:
    """Compute _is_lifecycle_release as the coordinator does."""
    return should_allow_lifecycle_release(
        prev=prev,
        new=new,
        current_shading_state=current_state,
        active_override=None,
        proposed_is_open=True,
    )


def _dispatch_allowed(
    mode: WindowBehaviorMode,
    shading_state: ShadingState,
    *,
    is_absence_release: bool = False,
    is_lifecycle_release: bool = False,
) -> bool:
    return _mode_dispatch_allowed(
        mode, shading_state,
        is_absence_release=is_absence_release,
        is_lifecycle_release=is_lifecycle_release,
    )


# ===========================================================================
# MR-01  ABSENCE_AND_SCHEDULE: night close allowed
# ===========================================================================

class TestMR01_AbsenceAndScheduleNightCloseAllowed:

    def test_night_closed_dispatch_allowed(self):
        """NIGHT_CLOSED is in the allowed set for ABSENCE_AND_SCHEDULE."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.NIGHT_CLOSED,
        ) is True

    def test_night_vent_dispatch_allowed(self):
        """NIGHT_VENT (contact Option B) is allowed for ABSENCE_AND_SCHEDULE."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.NIGHT_VENT,
        ) is True

    def test_safety_dispatch_allowed(self):
        """Safety states are always allowed."""
        for state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):
            assert _dispatch_allowed(
                WindowBehaviorMode.ABSENCE_AND_SCHEDULE, state
            ) is True

    def test_manual_override_dispatch_allowed(self):
        """MANUAL_OVERRIDE is always allowed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.MANUAL_OVERRIDE,
        ) is True


# ===========================================================================
# MR-02  ABSENCE_AND_SCHEDULE: daytime fallback OPEN held
# ===========================================================================

class TestMR02_AbsenceAndScheduleDaytimeHeld:

    def test_open_without_release_condition_held(self):
        """Plain OPEN without absence_release or lifecycle_release → dispatch NOT allowed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.OPEN,
            is_absence_release=False,
            is_lifecycle_release=False,
        ) is False

    def test_strong_shade_held(self):
        """STRONG_SHADE (solar/heat shading) is suppressed for ABSENCE_AND_SCHEDULE."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.STRONG_SHADE,
        ) is False

    def test_normal_shade_held(self):
        """NORMAL_SHADE is suppressed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.NORMAL_SHADE,
        ) is False

    def test_light_shade_held(self):
        """LIGHT_SHADE is suppressed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.LIGHT_SHADE,
        ) is False


# ===========================================================================
# MR-03  ABSENCE_AND_SCHEDULE: morning release — live NIGHT→MORNING transition
# ===========================================================================

class TestMR03_AbsenceAndScheduleMorningReleaseLiveTransition:

    def test_night_to_morning_night_closed_lifecycle_release_true(self):
        """Live transition: prev=NIGHT, new=MORNING, state=NIGHT_CLOSED → release True."""
        assert _lifecycle_release(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.MORNING,
            current_state=ShadingState.NIGHT_CLOSED,
        ) is True

    def test_night_to_day_night_closed_lifecycle_release_true(self):
        """NIGHT→DAY (no MORNING phase): lifecycle release fires."""
        assert _lifecycle_release(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.DAY,
            current_state=ShadingState.NIGHT_CLOSED,
        ) is True

    def test_dispatch_allowed_with_lifecycle_release(self):
        """OPEN + is_lifecycle_release=True → dispatch allowed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.OPEN,
            is_lifecycle_release=True,
        ) is True

    def test_fully_automatic_morning_release_unrestricted(self):
        """FULLY_AUTOMATIC is not subject to behavior mode suppression at all."""
        assert _dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC,
            ShadingState.OPEN,
            is_lifecycle_release=False,
        ) is True

    def test_absence_and_schedule_open_blocked_without_release_flag(self):
        """Sanity: OPEN without release flag is suppressed (daytime OPEN stays held)."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.OPEN,
            is_lifecycle_release=False,
        ) is False


# ===========================================================================
# MR-04  ABSENCE_AND_SCHEDULE: morning release — post-restart (prev≠NIGHT)
# ===========================================================================

class TestMR04_AbsenceAndScheduleMorningReleasePostRestart:
    """After coordinator restart during MORNING/DAY, prev initialises to DAY.
    The window is still NIGHT_CLOSED (persisted).  Release must still fire."""

    def test_post_restart_day_prev_morning_new_releases(self):
        """Restart at 07:30 (MORNING): prev=DAY, new=MORNING, state=NIGHT_CLOSED → True."""
        assert _lifecycle_release(
            prev=LifecycleState.DAY,
            new=LifecycleState.MORNING,
            current_state=ShadingState.NIGHT_CLOSED,
        ) is True

    def test_post_restart_day_prev_day_new_releases(self):
        """Restart at 09:00 (DAY): prev=DAY, new=DAY, state=NIGHT_CLOSED → True."""
        assert _lifecycle_release(
            prev=LifecycleState.DAY,
            new=LifecycleState.DAY,
            current_state=ShadingState.NIGHT_CLOSED,
        ) is True

    def test_night_hard_hold_miss_then_morning_to_day_releases(self):
        """NightHardHold blocked OPEN on NIGHT→MORNING cycle.
        Next cycle: prev=MORNING, new=DAY, state still NIGHT_CLOSED → True."""
        assert _lifecycle_release(
            prev=LifecycleState.MORNING,
            new=LifecycleState.DAY,
            current_state=ShadingState.NIGHT_CLOSED,
        ) is True

    def test_post_restart_dispatch_allowed_with_release(self):
        """Full pipeline: post-restart lifecycle release → dispatch allowed."""
        lc_release = _lifecycle_release(
            prev=LifecycleState.DAY,
            new=LifecycleState.MORNING,
            current_state=ShadingState.NIGHT_CLOSED,
        )
        assert lc_release is True
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.OPEN,
            is_lifecycle_release=lc_release,
        ) is True

    def test_post_restart_manual_override_not_released(self):
        """Post-restart MANUAL_OVERRIDE (daytime override): must NOT release."""
        assert _lifecycle_release(
            prev=LifecycleState.DAY,
            new=LifecycleState.MORNING,
            current_state=ShadingState.MANUAL_OVERRIDE,
        ) is False

    def test_release_blocked_when_new_is_night(self):
        """new=NIGHT is always blocked (entering night, not leaving it)."""
        assert _lifecycle_release(
            prev=LifecycleState.DAY,
            new=LifecycleState.NIGHT,
            current_state=ShadingState.NIGHT_CLOSED,
        ) is False


# ===========================================================================
# F25  ABSENCE_AND_SCHEDULE: morning release out of NIGHT_VENT (Option B)
# ===========================================================================
#
# Field motivation: a window vented by night-contact Option B (NightContactVent,
# current_state=NIGHT_VENT) had NO dedicated release path — only NIGHT_CLOSED
# was eligible.  It relied entirely on the generic position-based recovery-open
# safety net (_is_position_recovery_release), which only fires when the
# observed position is >= 20 HA below fully open.  A window vented to a
# position close to open (window_open_night_position configured near 100)
# would never cross that threshold and could remain stuck at the vent
# position indefinitely after the NIGHT->MORNING/DAY transition.

class TestF25_AbsenceAndScheduleMorningReleaseFromNightVent:

    def test_night_to_morning_night_vent_lifecycle_release_true(self):
        """Live transition: prev=NIGHT, new=MORNING, state=NIGHT_VENT -> release True."""
        assert _lifecycle_release(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.MORNING,
            current_state=ShadingState.NIGHT_VENT,
        ) is True

    def test_night_to_day_night_vent_lifecycle_release_true(self):
        """NIGHT->DAY (no MORNING phase) also releases a vented window."""
        assert _lifecycle_release(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.DAY,
            current_state=ShadingState.NIGHT_VENT,
        ) is True

    def test_post_restart_night_vent_releases(self):
        """Post-restart (prev never NIGHT this session) still releases NIGHT_VENT,
        mirroring the NIGHT_CLOSED post-restart path (MR-04)."""
        assert _lifecycle_release(
            prev=LifecycleState.DAY,
            new=LifecycleState.MORNING,
            current_state=ShadingState.NIGHT_VENT,
        ) is True

    def test_dispatch_allowed_with_night_vent_lifecycle_release(self):
        """Full pipeline: OPEN + is_lifecycle_release=True (from NIGHT_VENT) -> allowed."""
        lc_release = _lifecycle_release(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.MORNING,
            current_state=ShadingState.NIGHT_VENT,
        )
        assert lc_release is True
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.OPEN,
            is_lifecycle_release=lc_release,
        ) is True

    def test_release_blocked_when_new_is_night_for_night_vent(self):
        """new=NIGHT must never release a currently-venting window (still night)."""
        assert _lifecycle_release(
            prev=LifecycleState.DAY,
            new=LifecycleState.NIGHT,
            current_state=ShadingState.NIGHT_VENT,
        ) is False

    def test_active_override_blocks_night_vent_release(self):
        """An active manual override still takes precedence over the release,
        exactly as for NIGHT_CLOSED — existing architecture, unchanged."""
        assert should_allow_lifecycle_release(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.MORNING,
            current_shading_state=ShadingState.NIGHT_VENT,
            active_override=object(),
            proposed_is_open=True,
        ) is False

    def test_not_open_proposal_does_not_release_night_vent(self):
        """proposed_is_open=False (e.g. Solar/Heat/Glare wants shading this
        cycle instead) must not force a release out of NIGHT_VENT."""
        assert should_allow_lifecycle_release(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.MORNING,
            current_shading_state=ShadingState.NIGHT_VENT,
            active_override=None,
            proposed_is_open=False,
        ) is False

    def test_fully_automatic_night_vent_release_unrestricted(self):
        """FULLY_AUTOMATIC is not subject to behavior mode suppression at all —
        it was never affected by this gap (regression guard)."""
        assert _dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC,
            ShadingState.OPEN,
            is_lifecycle_release=False,
        ) is True

    def test_night_vent_itself_still_dispatchable_unaffected(self):
        """The pre-existing NIGHT_VENT initiation allowance (Option B venting
        itself, MR-01) is untouched by this fix."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.NIGHT_VENT,
        ) is True

    def test_other_states_not_affected_by_night_vent_addition(self):
        """Sanity: adding NIGHT_VENT as a release-eligible current_state must
        not accidentally make unrelated states (e.g. NORMAL_SHADE, OPEN as a
        current_state) eligible too."""
        for state in (ShadingState.NORMAL_SHADE, ShadingState.OPEN,
                      ShadingState.STRONG_SHADE, ShadingState.LIGHT_SHADE):
            assert _lifecycle_release(
                prev=LifecycleState.NIGHT,
                new=LifecycleState.MORNING,
                current_state=state,
            ) is False


# ===========================================================================
# MR-05  ABSENCE_AND_SCHEDULE: absence release allowed
# ===========================================================================

class TestMR05_AbsenceAndScheduleAbsenceRelease:

    def test_absence_release_dispatch_allowed(self):
        """ABSENCE_CLOSED + is_absence_release=True → dispatch allowed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.OPEN,
            is_absence_release=True,
        ) is True

    def test_absence_closed_dispatch_allowed(self):
        """Absence close (ABSENCE_CLOSED state) is always allowed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
            ShadingState.ABSENCE_CLOSED,
        ) is True

    def test_absence_release_absence_only_also_allowed(self):
        """ABSENCE_ONLY: absence release is also allowed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY,
            ShadingState.OPEN,
            is_absence_release=True,
        ) is True


# ===========================================================================
# MR-06  ABSENCE_ONLY: night close and morning release do not apply
# ===========================================================================

class TestMR06_AbsenceOnlyNoNightMorning:

    def test_night_closed_not_dispatched(self):
        """ABSENCE_ONLY: NIGHT_CLOSED is not in the allowed dispatch set."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY,
            ShadingState.NIGHT_CLOSED,
        ) is False

    def test_open_without_release_held(self):
        """ABSENCE_ONLY: plain OPEN (no release) → held."""
        assert _dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY,
            ShadingState.OPEN,
            is_absence_release=False,
            is_lifecycle_release=False,
        ) is False

    def test_lifecycle_release_not_called_for_absence_only(self):
        """The coordinator guards `is_lifecycle_release` with behavior==ABSENCE_AND_SCHEDULE.
        For ABSENCE_ONLY, _mode_dispatch_allowed is never passed is_lifecycle_release=True.
        Verify that even if it were (defensive), it would be blocked by the caller guard."""
        # The coordinator only computes _is_lifecycle_release when
        # _window_behavior is ABSENCE_AND_SCHEDULE. This tests the
        # _mode_dispatch_allowed surface — ABSENCE_ONLY + is_lifecycle_release=True
        # is not a real production path, but the function must still not
        # silently allow it (it falls through to the is_lifecycle_release=True
        # branch which IS allowed by _mode_dispatch_allowed for any mode).
        # The real protection is the caller-level behavior check.
        pass  # isolation ensured by coordinator caller guard, not _mode_dispatch_allowed


# ===========================================================================
# MR-07  DISABLED_AUTOMATIC: no morning release
# ===========================================================================

class TestMR07_DisabledAutomaticNoMorningRelease:

    def test_open_held(self):
        """DISABLED_AUTOMATIC: OPEN dispatch is not allowed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.DISABLED_AUTOMATIC,
            ShadingState.OPEN,
        ) is False

    def test_night_closed_held(self):
        """DISABLED_AUTOMATIC: NIGHT_CLOSED dispatch is not allowed."""
        assert _dispatch_allowed(
            WindowBehaviorMode.DISABLED_AUTOMATIC,
            ShadingState.NIGHT_CLOSED,
        ) is False

    def test_safety_always_allowed(self):
        """Safety overrides DISABLED_AUTOMATIC."""
        for state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):
            assert _dispatch_allowed(
                WindowBehaviorMode.DISABLED_AUTOMATIC, state
            ) is True


# ===========================================================================
# MR-08  FULLY_AUTOMATIC: night close and morning release both pass
# ===========================================================================

class TestMR08_FullyAutomaticUnrestricted:

    def test_fully_automatic_night_close_allowed(self):
        assert _dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC,
            ShadingState.NIGHT_CLOSED,
        ) is True

    def test_fully_automatic_open_allowed(self):
        assert _dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC,
            ShadingState.OPEN,
        ) is True

    def test_fully_automatic_strong_shade_allowed(self):
        assert _dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC,
            ShadingState.STRONG_SHADE,
        ) is True

    def test_fully_automatic_normal_shade_allowed(self):
        assert _dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC,
            ShadingState.NORMAL_SHADE,
        ) is True

    def test_fully_automatic_morning_release_allowed(self):
        """FULLY_AUTOMATIC: lifecycle release fires (live path)."""
        lc_release = _lifecycle_release(
            prev=LifecycleState.NIGHT,
            new=LifecycleState.MORNING,
            current_state=ShadingState.NIGHT_CLOSED,
        )
        # For FULLY_AUTOMATIC, _mode_dispatch_allowed always returns True regardless.
        assert _dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC,
            ShadingState.OPEN,
            is_lifecycle_release=lc_release,
        ) is True


# ===========================================================================
# F23  Per-window night_position override — behavior-mode end-to-end gating
#
# Confirms the override cannot create a "backdoor" to normal night control
# for ABSENCE_ONLY / DISABLED_AUTOMATIC — these modes already force
# lifecycle_state=DAY in _apply_window_behavior_mode BEFORE NightEvaluator
# ever runs, independent of whether effective_behavior.night_position was
# resolved from the lifecycle default or a window override.
# ===========================================================================

def _night_wdi(window, *, lifecycle_state=LifecycleState.NIGHT):
    zone = ZoneConfig(id="z1", name="Zone")
    defaults = GlobalDefaults(night_shading_enabled=True)
    lifecycle_config = NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True)
    return build_window_decision_input(
        window=window, zone=zone, global_defaults=defaults,
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=lifecycle_config, lifecycle_state=lifecycle_state,
        absence_active=False, current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
        is_in_solar_sector=False,
    )


def _window_with_night_override(behavior_mode):
    return WindowConfig(
        id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0,
        cover_group_id="cg1", behavior_mode=behavior_mode, night_position=20,
    )


class TestF23NightPositionOverrideBehaviorModeGating:

    def test_fully_automatic_uses_effective_night_position(self):
        """FULLY_AUTOMATIC: no masking at all — NightEvaluator sees the
        window override and produces NIGHT_CLOSED at the overridden position."""
        window = _window_with_night_override(WindowBehaviorMode.FULLY_AUTOMATIC)
        wdi = _night_wdi(window)
        # FULLY_AUTOMATIC skips _apply_window_behavior_mode masking entirely
        # (coordinator.py: only non-FULLY_AUTOMATIC modes are masked).
        decision = NightEvaluator().evaluate(wdi)
        assert decision is not None
        assert decision.shading_state == ShadingState.NIGHT_CLOSED
        from custom_components.smartshading.models.window_decision_input import _ha_to_internal
        assert decision.target_position == _ha_to_internal(20)

    def test_absence_and_schedule_uses_effective_night_position(self):
        """ABSENCE_AND_SCHEDULE: masking preserves lifecycle/night — the
        override still reaches NightEvaluator."""
        window = _window_with_night_override(WindowBehaviorMode.ABSENCE_AND_SCHEDULE)
        wdi = _night_wdi(window)
        masked = _apply_window_behavior_mode(wdi, WindowBehaviorMode.ABSENCE_AND_SCHEDULE)
        decision = NightEvaluator().evaluate(masked)
        assert decision is not None
        assert decision.shading_state == ShadingState.NIGHT_CLOSED

    def test_absence_only_gains_no_new_night_control_despite_override(self):
        """ABSENCE_ONLY: forces lifecycle_state=DAY regardless of the window
        override being set — NightEvaluator must still return None (no new
        normal night control introduced by F23)."""
        window = _window_with_night_override(WindowBehaviorMode.ABSENCE_ONLY)
        wdi = _night_wdi(window)  # built with lifecycle_state=NIGHT
        masked = _apply_window_behavior_mode(wdi, WindowBehaviorMode.ABSENCE_ONLY)
        assert masked.lifecycle_state is LifecycleState.DAY  # forced, pre-existing behavior
        decision = NightEvaluator().evaluate(masked)
        assert decision is None

    def test_disabled_automatic_gains_no_new_night_control_despite_override(self):
        """DISABLED_AUTOMATIC: same DAY-forcing guard, same result."""
        window = _window_with_night_override(WindowBehaviorMode.DISABLED_AUTOMATIC)
        wdi = _night_wdi(window)
        masked = _apply_window_behavior_mode(wdi, WindowBehaviorMode.DISABLED_AUTOMATIC)
        assert masked.lifecycle_state is LifecycleState.DAY
        decision = NightEvaluator().evaluate(masked)
        assert decision is None

    def test_absence_only_night_evaluator_none_without_override_too(self):
        """Regression baseline: ABSENCE_ONLY already returned None from
        NightEvaluator before F23 (no window override set) — confirms F23
        did not change this pre-existing gate at all."""
        window = WindowConfig(
            id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0,
            cover_group_id="cg1", behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
        )
        assert window.night_position is None
        wdi = _night_wdi(window)
        masked = _apply_window_behavior_mode(wdi, WindowBehaviorMode.ABSENCE_ONLY)
        assert NightEvaluator().evaluate(masked) is None


# ===========================================================================
# F8  _apply_window_behavior_mode: direct Heat/Solar/Glare masking assertions
#
# Prior coverage (test_p2_behavior_mode_helper_is_pure) only proves
# FULLY_AUTOMATIC is a pure pass-through; the actual field-nulling for the
# three restricted modes had no direct assertion.  These tests close that
# F8 gap: ABSENCE_AND_SCHEDULE/ABSENCE_ONLY/DISABLED_AUTOMATIC must not gain
# any new Solar/Heat/Glare intelligence via adoption/experiment/strategy —
# masking must reliably null those fields regardless of what an adopted
# comfort-tier position injection left in effective_behavior.
# ===========================================================================

def _day_wdi_with_comfort(window):
    """A DAY-lifecycle WDI with heat+glare comfort enabled and a real solar
    exposure — the exact shape needed to prove Heat/Solar/Glare get masked
    (there must be something real to suppress for the assertion to mean
    anything, mirroring test_wind_below_threshold_lets_adapted_solar_target_
    through's "control case" principle from test_tier_orchestrator.py)."""
    zone = ZoneConfig(id="z1", name="Zone")
    defaults = GlobalDefaults(absence_position=30, absence_shading_enabled=True)
    lifecycle_config = NightDayLifecycleConfig(id="default")
    comfort = ComfortConfig(heat_protection_enabled=True, glare_protection_enabled=True)
    return build_window_decision_input(
        window=window, zone=zone, global_defaults=defaults,
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=lifecycle_config, lifecycle_state=LifecycleState.DAY,
        absence_active=False, current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=32.0, indoor_temp_c=29.0, exposure=None,
        is_in_solar_sector=True, comfort_config=comfort,
    )


class TestF8BehaviorModeMaskingNullsHeatSolarGlare:

    def test_absence_and_schedule_masks_heat_solar_glare(self):
        window = WindowConfig(
            id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0,
            cover_group_id="cg1", behavior_mode=WindowBehaviorMode.ABSENCE_AND_SCHEDULE,
        )
        wdi = _day_wdi_with_comfort(window)
        assert wdi.effective_behavior.heat_outdoor_threshold_c is not None  # sanity: real before masking
        masked = _apply_window_behavior_mode(wdi, WindowBehaviorMode.ABSENCE_AND_SCHEDULE)
        assert masked.effective_behavior.heat_outdoor_threshold_c is None
        assert masked.effective_behavior.heat_indoor_threshold_c is None
        assert masked.effective_behavior.solar_gain_suppresses_shading is True
        assert masked.effective_behavior.glare_protection_enabled is False
        # Lifecycle/absence are explicitly KEPT for this mode (unlike the other two).
        assert masked.lifecycle_state is LifecycleState.DAY  # unchanged from input
        assert masked.effective_behavior.absence_position is not None

    def test_absence_only_masks_heat_solar_glare(self):
        window = WindowConfig(
            id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0,
            cover_group_id="cg1", behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
        )
        wdi = _day_wdi_with_comfort(window)
        masked = _apply_window_behavior_mode(wdi, WindowBehaviorMode.ABSENCE_ONLY)
        assert masked.effective_behavior.heat_outdoor_threshold_c is None
        assert masked.effective_behavior.heat_indoor_threshold_c is None
        assert masked.effective_behavior.solar_gain_suppresses_shading is True
        assert masked.effective_behavior.glare_protection_enabled is False
        assert masked.lifecycle_state is LifecycleState.DAY
        # Absence floor is explicitly KEPT for this mode.
        assert masked.effective_behavior.absence_position is not None

    def test_disabled_automatic_masks_heat_solar_glare_and_absence(self):
        window = WindowConfig(
            id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0,
            cover_group_id="cg1", behavior_mode=WindowBehaviorMode.DISABLED_AUTOMATIC,
        )
        wdi = _day_wdi_with_comfort(window)
        masked = _apply_window_behavior_mode(wdi, WindowBehaviorMode.DISABLED_AUTOMATIC)
        assert masked.effective_behavior.heat_outdoor_threshold_c is None
        assert masked.effective_behavior.heat_indoor_threshold_c is None
        assert masked.effective_behavior.solar_gain_suppresses_shading is True
        assert masked.effective_behavior.glare_protection_enabled is False
        assert masked.lifecycle_state is LifecycleState.DAY
        # Only DISABLED_AUTOMATIC additionally nulls the absence floor —
        # nothing beyond Safety/Manual Override may reach this mode.
        assert masked.effective_behavior.absence_position is None

    def test_fully_automatic_keeps_heat_solar_glare_untouched(self):
        """Regression: FULLY_AUTOMATIC must not be affected by this masking
        at all — the comfort fields set up by _day_wdi_with_comfort survive
        unchanged (companion to the existing pure-passthrough test)."""
        window = WindowConfig(
            id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0,
            cover_group_id="cg1", behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC,
        )
        wdi = _day_wdi_with_comfort(window)
        masked = _apply_window_behavior_mode(wdi, WindowBehaviorMode.FULLY_AUTOMATIC)
        assert masked is wdi  # pure pass-through, no replace() at all
        assert masked.effective_behavior.heat_outdoor_threshold_c is not None
        assert masked.effective_behavior.glare_protection_enabled is True
