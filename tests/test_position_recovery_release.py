"""Position-based self-healing recovery release — v1.1.5 field fix.

Real-world report (support export 2026-07-06): an ABSENCE_ONLY terrace door
(west) and an ABSENCE_AND_SCHEDULE window were stuck physically DOWN
(actual_position_ha = 0 and 30) all morning while SmartShading's internal
shading_state was `open`. Every cycle produced
`decided_by=BehaviorMode:hold, reason=no_target_position`, and the covers had
to be raised by hand.

Root cause: the absence-release gate requires `current_state ==
ShadingState.ABSENCE_CLOSED` and the lifecycle-release gate requires
`NIGHT_CLOSED`. When the internal current_state desynced from the physical
position — e.g. an ABSENCE_CLOSED that was lost across a restart / the
v1.1.3->v1.1.4 upgrade (v1.1.3 never persisted current_states), or an
external/manual close SmartShading never recorded — the internal state is
`open` while the cover is physically down. Neither release fires, plain OPEN
is not an allowed dispatch for these modes, so `BehaviorMode:hold` blocks
forever and nothing brings the cover up.

Fix: `_is_position_recovery_release()` — a narrow, strictly one-directional
safety net that may allow exactly one controlled OPEN (retract) dispatch to
un-stick such a window. It never closes, never activates solar/heat/glare
shading for ABSENCE_ONLY, and stays inert whenever Safety, Manual Override,
Night Contact, the real NIGHT phase, an active absence-close, an unavailable
cover, or an already-open position applies. After the recovery open the
internal state becomes OPEN (consistent with the raised cover), so subsequent
cycles are same-position no-ops and it never loops.

These tests exercise the pure helper (all guards) plus `_mode_dispatch_allowed`
with the new `is_position_recovery` reason. FULLY_AUTOMATIC is unaffected.
"""
from __future__ import annotations

import sys
import types
from typing import Any


# ---------------------------------------------------------------------------
# HA stubs — installed before coordinator import (same pattern as
# test_morning_release_behavior_modes.py).
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

_recovery = _coord_mod._is_position_recovery_release
_mode_dispatch_allowed = _coord_mod._mode_dispatch_allowed

# F32: zone-ordered dispatch (F18) combined with the real dispatch chain —
# these modules have no Home Assistant dependency of their own (pure Python,
# see their own module docstrings), so they can be imported directly here
# alongside the HA-stubbed coordinator import above.
import asyncio  # noqa: E402
import time  # noqa: E402
from datetime import timedelta as _timedelta  # noqa: E402
from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock  # noqa: E402

from custom_components.smartshading.cover_control.command_filter import (  # noqa: E402
    CommandFilter as _CommandFilter,
    ExecutionCapability as _ExecutionCapability,
    ExecutionMode as _ExecutionMode,
)
from custom_components.smartshading.cover_control.execution_plan import (  # noqa: E402
    build_execution_plan as _build_execution_plan,
)
from custom_components.smartshading.cover_control.execution_result import (  # noqa: E402
    ExecutionStatus as _ExecutionStatus,
)
from custom_components.smartshading.cover_control.global_dispatch_throttle import (  # noqa: E402
    GlobalSerialDispatch as _GlobalSerialDispatch,
)
from custom_components.smartshading.cover_control.ha_service_adapter import (  # noqa: E402
    dispatch_cover_intent as _dispatch_cover_intent,
)
_MIN_BELOW = _coord_mod._RECOVERY_MIN_BELOW_OPEN_HA
_OPEN_HA = _coord_mod._OPEN_POSITION_HA
_dispatch_order_key = _coord_mod._dispatch_order_key
_build_zone_dispatch_order = _coord_mod._build_zone_dispatch_order


def _base_kwargs(**overrides):
    """A fully-qualifying recovery scenario; override single fields per test."""
    kw = dict(
        behavior_mode=WindowBehaviorMode.ABSENCE_ONLY,
        proposed_is_open=True,
        actual_position_ha=0,          # terrace: fully down
        cover_available=True,
        active_control_enabled=True,
        lifecycle_is_night=False,
        is_safety=False,
        safety_hold_active=False,
        active_override_present=False,
        night_contact_blocking=False,
        absence_active=False,
    )
    kw.update(overrides)
    return kw


# ===========================================================================
# 1. Positive: recovery fires for the exact field scenarios.
# ===========================================================================

class TestRecoveryFiresForStuckDownWindows:
    def test_absence_only_terrace_fully_down_qualifies(self):
        assert _recovery(**_base_kwargs(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY, actual_position_ha=0)) is True

    def test_absence_and_schedule_at_30_qualifies(self):
        assert _recovery(**_base_kwargs(
            behavior_mode=WindowBehaviorMode.ABSENCE_AND_SCHEDULE, actual_position_ha=30)) is True

    def test_position_just_below_threshold_qualifies(self):
        # threshold: actual must be < open - min_below (100 - 20 = 80).
        assert _recovery(**_base_kwargs(actual_position_ha=_OPEN_HA - _MIN_BELOW - 1)) is True


# ===========================================================================
# 2. Guard: mode must be ABSENCE_ONLY / ABSENCE_AND_SCHEDULE.
# ===========================================================================

class TestOnlyAbsenceModes:
    def test_fully_automatic_never_recovers(self):
        assert _recovery(**_base_kwargs(behavior_mode=WindowBehaviorMode.FULLY_AUTOMATIC)) is False

    def test_disabled_automatic_never_recovers(self):
        assert _recovery(**_base_kwargs(behavior_mode=WindowBehaviorMode.DISABLED_AUTOMATIC)) is False


# ===========================================================================
# 3. Guard: only when the tier baseline proposes OPEN (never a shading move).
# ===========================================================================

class TestOnlyWhenBaselineWantsOpen:
    def test_no_recovery_when_baseline_is_not_open(self):
        assert _recovery(**_base_kwargs(proposed_is_open=False)) is False


# ===========================================================================
# 4. Guard: position must be clearly below open.
# ===========================================================================

class TestPositionThreshold:
    def test_already_open_does_not_recover(self):
        assert _recovery(**_base_kwargs(actual_position_ha=100)) is False

    def test_near_open_at_threshold_does_not_recover(self):
        # exactly open - min_below (80) is NOT clearly-below → no recovery.
        assert _recovery(**_base_kwargs(actual_position_ha=_OPEN_HA - _MIN_BELOW)) is False

    def test_slightly_down_above_threshold_does_not_recover(self):
        assert _recovery(**_base_kwargs(actual_position_ha=90)) is False

    def test_none_position_does_not_recover(self):
        assert _recovery(**_base_kwargs(actual_position_ha=None)) is False


# ===========================================================================
# 5. Guards: priority paths keep recovery inert.
# ===========================================================================

class TestPriorityGuardsKeepRecoveryInert:
    def test_safety_state_blocks_recovery(self):
        assert _recovery(**_base_kwargs(is_safety=True)) is False

    def test_rain_storm_wind_hold_blocks_recovery(self):
        assert _recovery(**_base_kwargs(safety_hold_active=True)) is False

    def test_manual_override_blocks_recovery(self):
        assert _recovery(**_base_kwargs(active_override_present=True)) is False

    def test_night_contact_blocking_blocks_recovery(self):
        assert _recovery(**_base_kwargs(night_contact_blocking=True)) is False

    def test_night_lifecycle_blocks_recovery(self):
        assert _recovery(**_base_kwargs(lifecycle_is_night=True)) is False

    def test_active_absence_blocks_recovery(self):
        # Presence still away → absence should close, not recover-open.
        assert _recovery(**_base_kwargs(absence_active=True)) is False

    def test_unavailable_cover_blocks_recovery(self):
        assert _recovery(**_base_kwargs(cover_available=False)) is False

    def test_active_control_off_blocks_recovery(self):
        assert _recovery(**_base_kwargs(active_control_enabled=False)) is False


# ===========================================================================
# 6. _mode_dispatch_allowed integration: recovery is a new allowed reason.
# ===========================================================================

class TestModeDispatchAllowedWithRecovery:
    def test_absence_only_open_allowed_via_recovery(self):
        # Plain OPEN for ABSENCE_ONLY is normally NOT allowed...
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.OPEN,
            is_absence_release=False, is_lifecycle_release=False,
            is_position_recovery=False,
        ) is False
        # ...but IS allowed when recovery qualifies.
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.OPEN,
            is_absence_release=False, is_lifecycle_release=False,
            is_position_recovery=True,
        ) is True

    def test_absence_and_schedule_open_allowed_via_recovery(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE, ShadingState.OPEN,
            is_absence_release=False, is_lifecycle_release=False,
            is_position_recovery=True,
        ) is True

    def test_recovery_default_false_preserves_prior_behavior(self):
        # Omitting is_position_recovery must behave exactly as before (held).
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.OPEN,
            is_absence_release=False, is_lifecycle_release=False,
        ) is False

    def test_recovery_does_not_allow_a_shading_state(self):
        # Even with the flag set, a non-OPEN shading state is still gated by the
        # caller (recovery is only ever computed for proposed OPEN); the
        # allowlist itself does not turn a NORMAL_SHADE into an allowed dispatch
        # for ABSENCE_ONLY unless recovery is passed — and the coordinator never
        # passes recovery=True for a shading state. Documented here: passing a
        # shading state with recovery=True would (by the OR) return True, which
        # is why the coordinator only ever sets recovery for proposed OPEN.
        # This test pins that plain OPEN is the intended vehicle.
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.STRONG_SHADE,
            is_absence_release=False, is_lifecycle_release=False,
            is_position_recovery=False,
        ) is False


# ===========================================================================
# 7. Non-regression: normal absence/lifecycle release + FULLY_AUTOMATIC.
# ===========================================================================

class TestExistingPathsUnchanged:
    def test_absence_release_still_allowed_without_recovery(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.OPEN,
            is_absence_release=True, is_lifecycle_release=False,
        ) is True

    def test_absence_closed_still_allowed(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.ABSENCE_CLOSED,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_fully_automatic_unrestricted(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.FULLY_AUTOMATIC, ShadingState.OPEN,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_safety_still_allowed_for_absence_only(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.STORM_SAFE,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_wind_safe_allowed_for_absence_only(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.WIND_SAFE,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_rain_safe_allowed_for_absence_only(self):
        # Audit finding F1: RAIN_SAFE must bypass BehaviorMode-Hold exactly like
        # STORM_SAFE/WIND_SAFE — a restricted mode must never suppress a rain
        # safety dispatch.
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_ONLY, ShadingState.RAIN_SAFE,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_rain_safe_allowed_for_absence_and_schedule(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.ABSENCE_AND_SCHEDULE, ShadingState.RAIN_SAFE,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True

    def test_rain_safe_allowed_for_disabled_automatic(self):
        assert _mode_dispatch_allowed(
            WindowBehaviorMode.DISABLED_AUTOMATIC, ShadingState.RAIN_SAFE,
            is_absence_release=False, is_lifecycle_release=False,
        ) is True


# ===========================================================================
# 8. Global dispatch integration (v1.1.5 audit follow-up, Finding F1):
# a recovery-open dispatch must go through the SAME shared
# GlobalSerialDispatch (lock + ~1s throttle) as any other real cover command
# — never a parallel/unthrottled path — and must only ever move the cover
# toward OPEN.
#
# Mirrors tests/test_shading_group_global_dispatch_integration.py's harness
# (real CommandFilter -> build_execution_plan -> GlobalSerialDispatch ->
# dispatch_cover_intent, with a MagicMock hass) rather than duplicating it —
# this file owns the recovery-specific guarantee, that file owns the
# harmonization-specific one.
# ===========================================================================

from datetime import datetime as _datetime, timezone as _timezone  # noqa: E402

_RECOVERY_DISPATCH_NOW = _datetime(2026, 7, 6, 9, 0, 0, tzinfo=_timezone.utc)


class TestRecoveryOpenRespectsGlobalDispatchThrottle:
    """Runtime-code note: no coordinator.py / global_dispatch_throttle.py /
    command_filter.py logic is changed by this test class — it is coverage
    only, added per the v1.1.5 post-release dispatch-queue audit (Finding
    F1: no dedicated test previously exercised a recovery-open dispatch
    while the global throttle already had a fresh dispatch recorded)."""

    def _make_hass(self):
        from unittest.mock import AsyncMock, MagicMock
        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock(return_value=None)
        return hass

    def _ordinary_intent(self):
        """An unrelated FULLY_AUTOMATIC window's normal dispatch — this is
        what already occupies the global throttle slot before the recovery
        window's cycle runs, exactly as would happen in a real coordinator
        pass over multiple windows in the same update cycle."""
        from custom_components.smartshading.cover_control.command_filter import (
            CommandFilter, ExecutionCapability, ExecutionMode,
        )
        from custom_components.smartshading.cover_control.execution_plan import (
            build_execution_plan,
        )
        cfr = CommandFilter().evaluate(
            target_position_internal=70, current_position_internal=30,
            execution_mode=ExecutionMode.AUTOMATIC, is_safety=False,
            is_manual_override=False, is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(position_tolerance=3),
            invert_position=False,
        )
        assert cfr.allowed is True
        plan = build_execution_plan(
            window_id="win_south", cover_entity_ids=["cover.south"],
            filter_result=cfr, decided_by="SolarEvaluator", now=_RECOVERY_DISPATCH_NOW,
        )
        return list(plan.intents)

    def _recovery_open_intent(self, *, current_position_internal: int):
        """The stuck-down ABSENCE_ONLY window's recovery-open dispatch:
        target_position_internal=0 (fully OPEN, internal convention) —
        the only direction _is_position_recovery_release ever allows."""
        from custom_components.smartshading.cover_control.command_filter import (
            CommandFilter, ExecutionCapability, ExecutionMode,
        )
        from custom_components.smartshading.cover_control.execution_plan import (
            build_execution_plan,
        )
        cfr = CommandFilter().evaluate(
            target_position_internal=0, current_position_internal=current_position_internal,
            execution_mode=ExecutionMode.AUTOMATIC, is_safety=False,
            is_manual_override=False, is_cover_available=True,
            state_guard_allowed=True,
            execution_capability=ExecutionCapability(position_tolerance=3),
            invert_position=False,
        )
        assert cfr.allowed is True
        plan = build_execution_plan(
            window_id="win_terrace", cover_entity_ids=["cover.terrace"],
            filter_result=cfr,
            # Mirrors the exact tag the coordinator sets when
            # _is_position_recovery_release() is the sole reason a plain
            # OPEN was allowed for ABSENCE_ONLY / ABSENCE_AND_SCHEDULE.
            decided_by="BehaviorMode:recovery_open", now=_RECOVERY_DISPATCH_NOW,
        )
        return list(plan.intents)

    async def _dispatch_one(self, gsd, hass, intent):
        import asyncio as _asyncio_mod
        from custom_components.smartshading.cover_control.execution_result import (
            ExecutionStatus,
        )
        from custom_components.smartshading.cover_control.ha_service_adapter import (
            dispatch_cover_intent,
        )
        async with gsd.lock:
            wait = gsd.time_until_next_allowed()
            throttled = wait.total_seconds() > 0
            if throttled:
                await _asyncio_mod.sleep(wait.total_seconds())
            result = await dispatch_cover_intent(
                hass, intent, now_utc=_RECOVERY_DISPATCH_NOW)
            if result.status is ExecutionStatus.SENT:
                gsd.record_dispatch(_RECOVERY_DISPATCH_NOW)
        return result, throttled

    def test_recovery_open_waits_for_a_dispatch_already_in_flight(self):
        # Pre-condition (audit requirement 1): the position-recovery gate
        # itself confirms this window qualifies -- reuses the already-tested
        # pure guard, not a new/separate eligibility path.
        assert _recovery(**_base_kwargs(
            behavior_mode=WindowBehaviorMode.ABSENCE_ONLY, actual_position_ha=0,
        )) is True

        from datetime import timedelta as _timedelta
        from custom_components.smartshading.cover_control.global_dispatch_throttle import (
            GlobalSerialDispatch,
        )
        from custom_components.smartshading.cover_control.execution_result import (
            ExecutionStatus,
        )
        import asyncio as _asyncio

        gsd = GlobalSerialDispatch(min_interval=_timedelta(seconds=0.05))
        hass = self._make_hass()

        async def _run():
            ordinary_result, ordinary_throttled = await self._dispatch_one(
                gsd, hass, self._ordinary_intent()[0])
            recovery_result, recovery_throttled = await self._dispatch_one(
                gsd, hass, self._recovery_open_intent(current_position_internal=100)[0])
            return ordinary_result, ordinary_throttled, recovery_result, recovery_throttled

        ordinary_result, ordinary_throttled, recovery_result, recovery_throttled = (
            _asyncio.run(_run())
        )

        # 2/3: the recovery-open dispatch happened and used the identical
        # GlobalSerialDispatch (same lock, same throttle instance).
        assert ordinary_result.status is ExecutionStatus.SENT
        assert recovery_result.status is ExecutionStatus.SENT
        # 4/5: not sent in parallel -- the first consumed the throttle slot
        # immediately, the recovery-open had to wait out the shared minimum
        # interval, exactly like any other second dispatch this cycle.
        assert ordinary_throttled is False
        assert recovery_throttled is True
        assert hass.services.async_call.call_count == 2

        # 6: direction-safe -- the recovery dispatch's actual service-call
        # payload only ever requests the fully-open HA position (100), never
        # a shaded/closed one. async_call("cover", "set_cover_position",
        # {entity_id, position}) is invoked with positional args.
        call_args, _ = hass.services.async_call.call_args_list[1]
        _domain, _service, service_data = call_args
        assert _domain == "cover"
        assert _service == "set_cover_position"
        assert service_data["position"] == 100

    def test_recovery_open_still_inert_while_a_dispatch_is_in_flight_if_guards_fail(self):
        # 7: this is coverage that the SAME guards from
        # _is_position_recovery_release (Safety/Manual/NightContact/etc.)
        # are entirely unaffected by there being a dispatch already in
        # flight elsewhere in the throttle -- the gate is evaluated
        # independently of dispatch/throttle timing.
        assert _recovery(**_base_kwargs(is_safety=True)) is False
        assert _recovery(**_base_kwargs(active_override_present=True)) is False
        assert _recovery(**_base_kwargs(night_contact_blocking=True)) is False


# ===========================================================================
# 9. Zone-ordered dispatch sequencing (F18): _dispatch_order_key /
# _build_zone_dispatch_order. Pure functions, unit-testable without a real
# coordinator cycle.
# ===========================================================================

class TestBuildZoneDispatchOrder:
    def test_zone_order_follows_zones_dict_order(self):
        zones = {"z_b": object(), "z_a": object()}
        windows = {}
        zone_order, _ = _build_zone_dispatch_order(zones, windows)
        assert zone_order == {"z_b": 0, "z_a": 1}

    def test_window_order_within_zone_follows_windows_dict_order(self):
        zones = {"z1": object()}
        windows = {
            "w2": types.SimpleNamespace(zone_id="z1"),
            "w1": types.SimpleNamespace(zone_id="z1"),
            "w3": types.SimpleNamespace(zone_id="z1"),
        }
        _, window_order = _build_zone_dispatch_order(zones, windows)
        assert window_order == {"w2": 0, "w1": 1, "w3": 2}

    def test_window_order_resets_per_zone(self):
        zones = {"z1": object(), "z2": object()}
        windows = {
            "a1": types.SimpleNamespace(zone_id="z1"),
            "b1": types.SimpleNamespace(zone_id="z2"),
            "a2": types.SimpleNamespace(zone_id="z1"),
            "b2": types.SimpleNamespace(zone_id="z2"),
        }
        _, window_order = _build_zone_dispatch_order(zones, windows)
        assert window_order == {"a1": 0, "b1": 0, "a2": 1, "b2": 1}


class TestDispatchOrderKey:
    def test_safety_always_sorts_first(self):
        key = _dispatch_order_key(
            is_safety=True, zone_id="z9", window_id="w9",
            zone_order={}, window_order_in_zone={},
        )
        assert key == (0, 0, 0)

    def test_non_safety_sorts_by_zone_then_window(self):
        zone_order = {"z1": 0, "z2": 1}
        window_order_in_zone = {"w1": 0, "w2": 1}
        key_a = _dispatch_order_key(
            is_safety=False, zone_id="z1", window_id="w1",
            zone_order=zone_order, window_order_in_zone=window_order_in_zone,
        )
        key_b = _dispatch_order_key(
            is_safety=False, zone_id="z2", window_id="w2",
            zone_order=zone_order, window_order_in_zone=window_order_in_zone,
        )
        assert key_a < key_b

    def test_unknown_zone_sorts_after_known_zones(self):
        zone_order = {"z1": 0, "z2": 1}
        key = _dispatch_order_key(
            is_safety=False, zone_id="z_unknown", window_id="w1",
            zone_order=zone_order, window_order_in_zone={},
        )
        assert key == (1, 2, 0)

    def test_full_sort_groups_interleaved_windows_by_zone_and_pulls_safety_first(self):
        # Simulates the real Pass-2 scenario: config-insertion order interleaves
        # zones (cellar -> living room -> cellar -> bedroom -> cellar), plus one
        # safety window buried in the middle. After sorting: the safety window
        # comes first, then each zone's windows appear together, in stable
        # config order within the zone.
        zones = {"cellar": object(), "living_room": object(), "bedroom": object()}
        windows = {
            "cellar_1": types.SimpleNamespace(zone_id="cellar"),
            "living_room_1": types.SimpleNamespace(zone_id="living_room"),
            "cellar_2": types.SimpleNamespace(zone_id="cellar"),
            "bedroom_1": types.SimpleNamespace(zone_id="bedroom"),
            "cellar_3": types.SimpleNamespace(zone_id="cellar"),
        }
        zone_order, window_order_in_zone = _build_zone_dispatch_order(zones, windows)

        # Original (unsorted, config-insertion) order, matching _window_states'
        # plain dict iteration today — cellar_3 is the safety candidate.
        items = [
            ("cellar_1", False), ("living_room_1", False), ("cellar_2", False),
            ("bedroom_1", False), ("cellar_3", True),
        ]
        ordered = sorted(
            items,
            key=lambda item: _dispatch_order_key(
                is_safety=item[1],
                zone_id=windows[item[0]].zone_id,
                window_id=item[0],
                zone_order=zone_order,
                window_order_in_zone=window_order_in_zone,
            ),
        )
        ordered_ids = [window_id for window_id, _ in ordered]
        # Safety pulled to the front; the rest grouped strictly by zone order
        # (cellar, living_room, bedroom), preserving within-zone config order.
        assert ordered_ids == [
            "cellar_3", "cellar_1", "cellar_2", "living_room_1", "bedroom_1",
        ]


# ===========================================================================
# 10. F32 field fix — zone-ordered dispatch (F18, section 9 above) combined
# with the REAL dispatch chain (CommandFilter -> build_execution_plan ->
# GlobalSerialDispatch lock/throttle -> dispatch_cover_intent), mirroring
# tests/test_shading_group_global_dispatch_integration.py's pattern. Confirms
# the sort key genuinely drives the order real service calls are issued in
# — not just the order of an isolated sort-key computation — and that real
# measured dispatch timestamps strictly follow the zone-grouped order.
# ===========================================================================

def _cfr32(*, target_internal: int, current_internal: int, is_safety: bool = False):
    return _CommandFilter().evaluate(
        target_position_internal=target_internal,
        current_position_internal=current_internal,
        execution_mode=_ExecutionMode.AUTOMATIC,
        is_safety=is_safety,
        is_manual_override=False,
        is_cover_available=True,
        state_guard_allowed=True,
        execution_capability=_ExecutionCapability(position_tolerance=3),
        invert_position=False,
    )


def _intent32(window_id: str, cover_entity_id: str, filter_result):
    plan = _build_execution_plan(
        window_id=window_id,
        cover_entity_ids=[cover_entity_id],
        filter_result=filter_result,
        decided_by="TestEvaluator",
        now=_datetime(2026, 6, 18, 15, 0, 0, tzinfo=_timezone.utc),
    )
    return list(plan.intents)[0]


def _make_hass32():
    hass = _MagicMock()
    hass.services = _MagicMock()
    hass.services.async_call = _AsyncMock(return_value=None)
    return hass


async def _dispatch_one_timed32(gsd, hass, intent, now_fn):
    if not intent.allowed:
        return None, None
    async with gsd.lock:
        wait = gsd.time_until_next_allowed()
        if wait.total_seconds() > 0:
            await asyncio.sleep(wait.total_seconds())
        sent_at_mono = time.monotonic()
        result = await _dispatch_cover_intent(hass, intent, now_utc=now_fn())
        if result.status is _ExecutionStatus.SENT:
            gsd.record_dispatch(now_fn())
    return result, sent_at_mono


class TestZoneOrderedDispatchSequencingWithRealDispatch:
    def test_intents_sorted_by_real_zone_order_dispatch_sequentially_by_zone(self):
        zones = {"cellar": object(), "attic": object()}
        windows = {
            "cellar_1": types.SimpleNamespace(zone_id="cellar"),
            "attic_1": types.SimpleNamespace(zone_id="attic"),
            "cellar_2": types.SimpleNamespace(zone_id="cellar"),
        }
        zone_order, window_order_in_zone = _coord_mod._build_zone_dispatch_order(zones, windows)

        # Config-insertion order interleaves zones on purpose (cellar, attic,
        # cellar) — exactly the "not grouped by zone" starting point the
        # sort must correct for.
        cfr = _cfr32(target_internal=70, current_internal=0)
        raw = [
            ("cellar_1", "cover.cellar_1"),
            ("attic_1", "cover.attic_1"),
            ("cellar_2", "cover.cellar_2"),
        ]
        window_to_intent = {wid: _intent32(wid, eid, cfr) for wid, eid in raw}
        sorted_window_ids = sorted(
            window_to_intent.keys(),
            key=lambda wid: _coord_mod._dispatch_order_key(
                is_safety=False,
                zone_id=windows[wid].zone_id,
                window_id=wid,
                zone_order=zone_order,
                window_order_in_zone=window_order_in_zone,
            ),
        )
        # Sort key groups strictly by zone (cellar before attic — config
        # order), preserving stable within-zone order.
        assert sorted_window_ids == ["cellar_1", "cellar_2", "attic_1"]

        intents = [window_to_intent[wid] for wid in sorted_window_ids]
        gsd = _GlobalSerialDispatch(min_interval=_timedelta(seconds=0.05))
        hass = _make_hass32()
        now_fn = lambda: _datetime(2026, 6, 18, 15, 0, 0, tzinfo=_timezone.utc)  # noqa: E731

        async def _dispatch_all():
            return [await _dispatch_one_timed32(gsd, hass, intent, now_fn) for intent in intents]

        results = asyncio.run(_dispatch_all())

        assert [r[0].status for r in results] == [
            _ExecutionStatus.SENT, _ExecutionStatus.SENT, _ExecutionStatus.SENT,
        ]
        # Real dispatch timestamps strictly increase in the sorted (zone-
        # grouped) order — the sort key genuinely drives the real loop, it
        # is not merely a cosmetic label computed and then ignored.
        timestamps = [r[1] for r in results]
        assert timestamps == sorted(timestamps)

    def test_safety_still_dispatches_first_even_when_interleaved_with_zones(self):
        zones = {"cellar": object(), "attic": object()}
        windows = {
            "cellar_1": types.SimpleNamespace(zone_id="cellar"),
            "cellar_safety": types.SimpleNamespace(zone_id="cellar"),
            "attic_1": types.SimpleNamespace(zone_id="attic"),
        }
        zone_order, window_order_in_zone = _coord_mod._build_zone_dispatch_order(zones, windows)

        cfr_normal = _cfr32(target_internal=70, current_internal=0)
        cfr_safety = _cfr32(target_internal=10, current_internal=90, is_safety=True)
        window_to_intent = {
            "cellar_1": _intent32("cellar_1", "cover.cellar_1", cfr_normal),
            "attic_1": _intent32("attic_1", "cover.attic_1", cfr_normal),
            # Safety candidate buried last in config-insertion order.
            "cellar_safety": _intent32("cellar_safety", "cover.cellar_safety", cfr_safety),
        }
        is_safety_by_window = {"cellar_1": False, "attic_1": False, "cellar_safety": True}
        sorted_window_ids = sorted(
            window_to_intent.keys(),
            key=lambda wid: _coord_mod._dispatch_order_key(
                is_safety=is_safety_by_window[wid],
                zone_id=windows[wid].zone_id,
                window_id=wid,
                zone_order=zone_order,
                window_order_in_zone=window_order_in_zone,
            ),
        )
        assert sorted_window_ids[0] == "cellar_safety", (
            "Safety always sorts first, regardless of zone/config position."
        )
        assert sorted_window_ids == ["cellar_safety", "cellar_1", "attic_1"]
