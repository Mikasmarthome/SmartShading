"""Rain-safe target position and Tier-1 safety handling — regression tests.

Field report (GitHub issue #4): with rain protection active, an awning was
driven to HA position 100 — fully extended, the most exposed position —
instead of being retracted.

Conventions involved (see cover_control/position_semantics.py and
engines/safety_hold.py):

  Home Assistant cover position   0 = retracted / closed, 100 = extended / open
  SmartShading internal position  internal = 100 - HA   (converted at exactly
                                  one place, to_ha_position(), at the execution
                                  boundary)

``HARDWARE_RAIN_SAFE_POSITIONS`` is an INTERNAL-convention table.  The decision
path consumes its value as internal and converts only an explicit per-window
``rain_safe_position_ha`` (HA convention), once.

RAIN_SAFE is a Tier-1 safety state like STORM_SAFE and WIND_SAFE, so in the
dispatch layer it must get the same safety treatment: it is not delayed by the
startup grace, the StateGuard action interval or the same-position tolerance,
it is never cancelled as a stale intent, and it overrides (clears) a manual
override.  While Active Control is off no cover command is sent at all — the
recommendation is still computed and published.

Most tests drive the real SmartShadingCoordinator update cycle (real tier
orchestration, rain hold, CommandFilter, harmonization, dispatch) with a
stubbed Home Assistant and assert on the actual ``cover.set_cover_position``
service calls.

Plan invalidation vs. queue: the rain-sensor listener invalidates a not yet sent
normal plan of the SAME coordinator (generation bump).  Intents other zones
already queued in the global dispatch lock are plain FIFO and are served first,
exactly as for wind/storm — there is no global preemption.
"""
from __future__ import annotations

import asyncio
import datetime as _datetime
import re
import sys
import types
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# HA stubs — installed before the coordinator import, same pattern as the
# other coordinator-level tests in this directory.
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry

    def async_request_refresh(self) -> None:
        pass

    def async_update_listeners(self) -> None:
        pass


class _StoreStub:
    def __init__(self, hass, version, key) -> None:
        pass

    async def async_load(self):
        return None

    async def async_save(self, data) -> None:
        pass

    async def async_remove(self) -> None:
        pass


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
    "homeassistant.core": _stub(
        "homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub(
        "homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None,
    ),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}.items():
    sys.modules.setdefault(_name, _mod)

if "homeassistant.util.dt" not in sys.modules:
    sys.modules["homeassistant.util.dt"] = _stub("homeassistant.util.dt")

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

from custom_components.smartshading.config_entry_data import (  # noqa: E402
    SmartShadingConfigEntryData,
    from_storage_dict,
    to_storage_dict,
)
from custom_components.smartshading.cover_control.anti_heat_buildup import (  # noqa: E402
    apply_anti_heat_buildup,
)
from custom_components.smartshading.cover_control.cover_capabilities import (  # noqa: E402
    CoverCapability,
    CoverProfile,
)
from custom_components.smartshading.cover_control.daytime_min_open import (  # noqa: E402
    apply_daytime_min_open,
)
from custom_components.smartshading.cover_control.global_dispatch_throttle import (  # noqa: E402
    GlobalSerialDispatch,
)
from custom_components.smartshading.cover_control.position_semantics import (  # noqa: E402
    to_ha_position,
)
from custom_components.smartshading.engines.diagnostics_privacy import (  # noqa: E402
    NS_WINDOW,
    Pseudonymizer,
)
from custom_components.smartshading.engines.observability_evaluator import (  # noqa: E402
    build_next_action,
    build_reason,
)
from custom_components.smartshading.engines.rain_engine import RainStatus  # noqa: E402
from custom_components.smartshading.engines.safety_hold import (  # noqa: E402
    HARDWARE_RAIN_SAFE_POSITIONS,
    HARDWARE_SAFE_POSITIONS,
)
from custom_components.smartshading.engines.support_export import (  # noqa: E402
    _hardware_type_name,
    build_support_export_v3,
    privacy_scan,
)
from custom_components.smartshading.evaluators.rain_evaluator import RainEvaluator  # noqa: E402
from custom_components.smartshading.models.config import (  # noqa: E402
    GlobalDefaults,
    ShadePositionDefaults,
)
from custom_components.smartshading.models.cover_group import (  # noqa: E402
    CoverGroup,
    CoverHardwareType,
    cover_hardware_type_from_str,
)
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    LifecycleState,
    NightDayLifecycleConfig,
)
from custom_components.smartshading.models.window import WindowConfig  # noqa: E402
from custom_components.smartshading.models.window_decision_input import (  # noqa: E402
    build_window_decision_input,
)
from custom_components.smartshading.models.zone import ZoneConfig  # noqa: E402
from custom_components.smartshading.models.zone_execution_config import (  # noqa: E402
    ZoneExecutionConfig,
)
from custom_components.smartshading.state_machine.guards import StateGuard  # noqa: E402
from custom_components.smartshading.state_machine.states import (  # noqa: E402
    STATE_PRIORITY,
    SAFETY_SHADING_STATES,
    ShadingState,
)
from custom_components.smartshading.state_machine.transitions import bypasses_guard  # noqa: E402

_UTC = _datetime.timezone.utc
_NOW = _datetime.datetime(2026, 6, 30, 12, 0, 0, tzinfo=_UTC)
_NIGHT = _datetime.datetime(2026, 9, 8, 3, 24, 59, tzinfo=_UTC)

_COVER = "cover.test_cover"
_RAIN = "binary_sensor.rain"
_WIND = "sensor.wind_speed"

# The Tier-1 safety states, spelled out so parametrization does not depend on the
# production constant (which has its own test below).
_TIER1_STATES = sorted(
    (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE, ShadingState.RAIN_SAFE),
    key=lambda s: s.value,
)

AWNING = CoverHardwareType.AWNING
SCREEN = CoverHardwareType.EXTERIOR_SCREEN
ROLLER = CoverHardwareType.ROLLER_SHUTTER
VENETIAN = CoverHardwareType.VENETIAN_BLIND
GENERIC = CoverHardwareType.GENERIC


@pytest.fixture(autouse=True)
def _no_real_travel_waits(monkeypatch):
    """Normal (non-safety) dispatches wait for the cover to finish travelling and
    pause afterwards; the stubbed covers never move, so neither wait is real here."""
    from custom_components.smartshading.cover_control import dispatch_plan_executor
    from custom_components.smartshading.cover_control.dispatch_completion import (
        CompletionMethod,
        CompletionResult,
    )

    async def _instant_completion(*_args, **_kwargs):
        return CompletionResult(CompletionMethod.POSITION, 0.0, False)

    monkeypatch.setattr(_coord_mod, "wait_for_travel_completion", _instant_completion)
    monkeypatch.setattr(dispatch_plan_executor, "INTERMEDIATE_POST_COMPLETION_PAUSE_S", 0.0)


# ---------------------------------------------------------------------------
# Harness: a stubbed Home Assistant around the real coordinator
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}
        self.last_updated = _NOW


class _States:
    def __init__(self) -> None:
        self._d: dict[str, _State] = {}

    def set(self, entity_id: str, state: str, **attributes: Any) -> None:
        self._d[entity_id] = _State(state, attributes)

    def get(self, entity_id: str) -> _State | None:
        return self._d.get(entity_id)

    def touch(self, when: _datetime.datetime) -> None:
        """Every entity was just updated (sensors never look stale)."""
        for st in self._d.values():
            st.last_updated = when


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.states = _States()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)

    def _create_task(coro, *a, **k):
        if asyncio.iscoroutine(coro):
            coro.close()
        return None

    hass.async_create_task = MagicMock(side_effect=_create_task)
    # Daylight, sun in the window's solar sector (windows face south).  60 deg is
    # deliberately away from the exposure thresholds (45 deg sits exactly on the
    # 500 W/m2 strong-shade threshold and rounds differently per platform).
    hass.states.set("sun.sun", "above_horizon", azimuth=180.0, elevation=60.0)
    hass.states.set(_RAIN, "off")
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    return entry


def _capability(entity_id: str = _COVER, *, invert: bool = False) -> CoverCapability:
    return CoverCapability(
        entity_id=entity_id,
        supports_position=True,
        supports_tilt=False,
        supports_open_close_only=False,
        supports_stop=True,
        supports_open=True,
        supports_close=True,
        has_reliable_position_feedback=True,
        invert_position=invert,
        cover_profile=CoverProfile.GENERIC_POSITION,
    )


class _Win:
    """An additional window (with its own cover group) in the primary zone."""

    def __init__(self, window_id: str, hardware_type: CoverHardwareType,
                 covers: Sequence[str], *, rain_protection_enabled=None,
                 rain_safe_position_ha=None, cover_position: int = 40) -> None:
        self.window_id = window_id
        self.hardware_type = hardware_type
        self.covers = list(covers)
        self.rain_protection_enabled = rain_protection_enabled
        self.rain_safe_position_ha = rain_safe_position_ha
        self.cover_position = cover_position


class _Scenario:
    """Windows with covers, driven through the real update cycle.

    The primary window (``window_id``) carries the scenario's hardware type;
    ``extra`` adds more windows to the same zone/coordinator.  Several
    scenarios may share one ``hass`` and one ``serial`` dispatch to model
    several zones/coordinators.
    """

    def __init__(
        self,
        hardware_type: CoverHardwareType,
        *,
        rain_protection_enabled: bool | None = None,
        rain_safe_position_ha: int | None = None,
        active_control: bool = True,
        cover_position: int = 40,
        window_id: str = "w1",
        zone_id: str = "z1",
        covers: Sequence[str] = (_COVER,),
        extra: Sequence[_Win] = (),
        hass: MagicMock | None = None,
        serial: GlobalSerialDispatch | None = None,
        base_now: _datetime.datetime | None = None,
        rain_entity: str = _RAIN,
    ) -> None:
        self.hass = hass if hass is not None else _make_hass()
        self.rain_entity = rain_entity
        if rain_entity != _RAIN:
            self.hass.states.set(
                rain_entity, "off" if rain_entity.startswith("binary_sensor.") else "0.0")
        self.wid = window_id
        self.cover = covers[0]
        self.base_now = base_now if base_now is not None else _NOW
        self.serial = serial if serial is not None else GlobalSerialDispatch(
            min_interval=timedelta(milliseconds=1))
        specs = [_Win(window_id, hardware_type, covers,
                      rain_protection_enabled=rain_protection_enabled,
                      rain_safe_position_ha=rain_safe_position_ha,
                      cover_position=cover_position), *extra]
        windows, groups = {}, {}
        for i, sp in enumerate(specs):
            gid = f"cg_{sp.window_id}"
            windows[sp.window_id] = WindowConfig(
                id=sp.window_id, name=sp.window_id, zone_id=zone_id, azimuth=180.0,
                floor_level=0, cover_group_id=gid,
                rain_protection_enabled=sp.rain_protection_enabled,
                rain_safe_position_ha=sp.rain_safe_position_ha,
            )
            groups[gid] = CoverGroup(id=gid, window_id=sp.window_id,
                                     cover_ids=list(sp.covers), hardware_type=sp.hardware_type)
            for cid in sp.covers:
                self.set_cover_position(sp.cover_position, cover=cid)
        zone = ZoneConfig(
            id=zone_id, name=zone_id,
            execution=ZoneExecutionConfig(
                learning_enabled=False, active_control_enabled=active_control,
            ),
        )
        self.coord = _coord_mod.SmartShadingCoordinator(
            self.hass,
            _make_entry(),
            windows=windows,
            zones={zone_id: zone},
            cover_groups=groups,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            rain_sensor_id=rain_entity,
            wind_speed_sensor_id=_WIND,
            wind_protection_enabled=True,
            global_serial_dispatch=self.serial,
        )
        # Steady state: restore done, startup grace elapsed (production state
        # from the second cycle on).
        self.coord._learning_restored = True
        self.coord._startup_cycles_remaining = 0
        for sp in specs:
            for cid in sp.covers:
                self.coord._cover_capabilities[cid] = _capability(cid)

    # -- environment ------------------------------------------------------
    def set_rain(self, raining: bool) -> None:
        if self.rain_entity.startswith("binary_sensor."):
            self.hass.states.set(self.rain_entity, "on" if raining else "off")
        else:
            self.hass.states.set(self.rain_entity, "1.5" if raining else "0.0")

    def set_wind(self, ms: float) -> None:
        self.hass.states.set(_WIND, str(ms), unit_of_measurement="m/s")

    def set_cover_position(self, ha_position: int, *, cover: str | None = None,
                           state: str = "open") -> None:
        self.hass.states.set(
            cover or self.cover, state, current_position=ha_position, supported_features=1,
        )

    def set_invert(self, invert: bool, *, cover: str | None = None) -> None:
        cid = cover or self.cover
        self.coord._cover_capabilities[cid] = _capability(cid, invert=invert)

    def night(self) -> None:
        """Sun below the horizon (default lifecycle config => night)."""
        self.base_now = _NIGHT
        self.hass.states.set("sun.sun", "below_horizon", azimuth=40.0, elevation=-25.0)

    # -- one coordinator cycle ---------------------------------------------
    def _clock(self, minutes: float) -> ExitStack:
        when = self.base_now + timedelta(minutes=minutes)
        self.hass.states.touch(when)
        stack = ExitStack()
        d = _coord_mod.dt_util
        stack.enter_context(patch.object(d, "utcnow", create=True, side_effect=lambda: when))
        stack.enter_context(patch.object(d, "now", create=True, side_effect=lambda: when))
        stack.enter_context(patch.object(d, "as_local", create=True, side_effect=lambda dt: dt))
        stack.enter_context(patch.object(d, "as_utc", create=True, side_effect=lambda dt: dt))
        return stack

    def cycle(self, minutes: float = 0):
        """Run one real update cycle at base_now + minutes; return SmartShadingData."""
        with self._clock(minutes):
            return asyncio.run(self.coord._async_update_data())

    # -- observation ---------------------------------------------------------
    @property
    def calls(self) -> list[tuple[str, int]]:
        """(entity_id, HA position) of every cover.set_cover_position call so far."""
        out = []
        for c in self.hass.services.async_call.await_args_list:
            args = c.args
            if args[0] == "cover" and args[1] == "set_cover_position":
                out.append((args[2]["entity_id"], args[2]["position"]))
        return out

    def calls_for(self, cover: str) -> list[int]:
        return [pos for ent, pos in self.calls if ent == cover]

    @property
    def position_calls(self) -> list[int]:
        """HA positions sent to the primary cover."""
        return self.calls_for(self.cover)

    def diag(self, data, wid: str | None = None):
        return data.execution_diagnostics[wid or self.wid]

    def state(self, data, wid: str | None = None) -> ShadingState:
        return data.window_results[wid or self.wid].state


def _state_of(data) -> ShadingState:
    return data.window_results["w1"].state


def _establish_manual_override(s: _Scenario, *, user_position: int = 90) -> None:
    """Normal shading command, then the user moves the cover by hand."""
    s.cycle(0)
    s.set_cover_position(user_position)
    s.cycle(5)
    data = s.cycle(10)
    d = s.diag(data)
    assert d.manual_override_active, "precondition: manual override must be active"
    assert s.state(data) is ShadingState.MANUAL_OVERRIDE


# ---------------------------------------------------------------------------
# Convention table: HARDWARE_RAIN_SAFE_POSITIONS is INTERNAL convention
# ---------------------------------------------------------------------------


class TestRainSafeTableConvention:
    """The tables are internal convention; HA position = 100 - internal, once.

    Physical meaning of the HA position (Home Assistant: 0 = closed, 100 = open):
      awning           0 = retracted, 100 = extended   -> safe = HA 0
      exterior screen  0 = lowered,   100 = raised     -> safe = HA 100
      roller / venetian                100 = raised    -> safe = HA 100
    """

    def test_awning_retracts_to_ha_zero(self):
        assert to_ha_position(HARDWARE_RAIN_SAFE_POSITIONS[AWNING]) == 0

    @pytest.mark.parametrize("hw", [SCREEN, ROLLER, VENETIAN])
    def test_vertical_covers_are_raised_to_ha_open(self, hw):
        # Roller/venetian: documented table decision (rain protection is off by
        # default for these types and cannot be switched on from the stable UI).
        assert to_ha_position(HARDWARE_RAIN_SAFE_POSITIONS[hw]) == 100

    def test_awning_and_screen_do_not_share_a_target(self):
        assert HARDWARE_RAIN_SAFE_POSITIONS[AWNING] != HARDWARE_RAIN_SAFE_POSITIONS[SCREEN]
        assert HARDWARE_SAFE_POSITIONS[AWNING] != HARDWARE_SAFE_POSITIONS[SCREEN]

    def test_generic_never_extends_in_rain(self):
        # GENERIC is what a missing/None hardware type is normalized to, and it
        # may physically be an awning: it must not default to an extended HA 100.
        assert to_ha_position(HARDWARE_RAIN_SAFE_POSITIONS[GENERIC]) == 0

    def test_missing_hardware_type_is_generic(self):
        assert cover_hardware_type_from_str(None) is GENERIC
        assert cover_hardware_type_from_str("") is GENERIC
        assert cover_hardware_type_from_str("not-a-type") is GENERIC

    def test_every_hardware_type_has_a_target_in_both_tables(self):
        assert set(HARDWARE_RAIN_SAFE_POSITIONS) == set(CoverHardwareType)
        assert set(HARDWARE_SAFE_POSITIONS) == set(CoverHardwareType)

    @pytest.mark.parametrize("hw", [h for h in CoverHardwareType if h is not GENERIC])
    def test_rain_and_wind_storm_tables_agree(self, hw):
        assert HARDWARE_RAIN_SAFE_POSITIONS[hw] == HARDWARE_SAFE_POSITIONS[hw]

    def test_wind_storm_targets_in_ha_terms(self):
        assert {hw: to_ha_position(v) for hw, v in HARDWARE_SAFE_POSITIONS.items()} == {
            AWNING: 0, SCREEN: 100, ROLLER: 100, VENETIAN: 100, GENERIC: 100,
        }


# ---------------------------------------------------------------------------
# Real decision + execution path — position semantics (package A)
# ---------------------------------------------------------------------------


class TestRainSafeTargetAtServiceCall:
    def test_default_target_retracts_an_awning(self):
        s = _Scenario(AWNING)  # rain protection on by hardware default
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.position_calls == [0]
        assert s.diag(data).target_position_ha == 0
        assert s.diag(data).rain_safe_active is True

    def test_default_target_raises_an_exterior_screen(self):
        # A vertical cover: HA 100 = raised/rolled up (HA 0 would lower it).
        s = _Scenario(SCREEN)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.position_calls == [100]
        assert s.diag(data).target_position_ha == 100
        assert s.diag(data).rain_safe_active is True

    @pytest.mark.parametrize("hw,expected", [(AWNING, 0), (SCREEN, 100)])
    def test_explicit_rain_protection_enabled_gives_the_same_target(self, hw, expected):
        # The configuration quoted in the field report: protection enabled
        # explicitly, no explicit rain-safe position.
        s = _Scenario(hw, rain_protection_enabled=True, rain_safe_position_ha=None)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.position_calls == [expected]

    def test_extended_awning_is_retracted(self):
        s = _Scenario(AWNING, cover_position=100)   # fully extended when rain starts
        s.set_rain(True)
        s.cycle()
        assert s.position_calls == [0]

    def test_retracted_awning_is_never_extended(self):
        s = _Scenario(AWNING, cover_position=0)     # already retracted
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        # Safety commands are not suppressed by the same-position filter (the
        # assumed position may be stale), so the retract may be repeated — but
        # a rain decision can never send anything other than "retracted".
        assert s.position_calls and set(s.position_calls) == {0}

    def test_lowered_exterior_screen_is_raised(self):
        s = _Scenario(SCREEN, cover_position=0)     # lowered (covering the window)
        s.set_rain(True)
        s.cycle()
        assert s.position_calls == [100]

    def test_raised_exterior_screen_is_never_lowered(self):
        s = _Scenario(SCREEN, cover_position=100)   # already raised
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.position_calls and set(s.position_calls) == {100}

    @pytest.mark.parametrize("hw", [AWNING, SCREEN])
    @pytest.mark.parametrize("explicit", [0, 30, 100])
    def test_explicit_position_is_used_in_ha_semantics(self, hw, explicit):
        s = _Scenario(hw, rain_safe_position_ha=explicit)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.position_calls == [explicit]

    def test_explicit_low_position_is_not_raised_by_the_daytime_minimum(self):
        # Exterior screens carry a daytime minimum-open clamp for normal shading;
        # a configured rain-safe position below it must be honoured, not raised.
        s = _Scenario(SCREEN, rain_safe_position_ha=5)
        s.set_rain(True)
        s.cycle()
        assert s.position_calls == [5]

    def test_explicit_low_position_is_not_raised_by_anti_heat_buildup(self):
        # Roller shutters raise a nearly-closed target under strong sun in the
        # solar sector; that protection must not alter a configured rain-safe
        # position either (sun-shower: rain and strong sun at the same time).
        s = _Scenario(ROLLER, rain_protection_enabled=True, rain_safe_position_ha=5)
        s.set_rain(True)
        s.cycle()
        assert s.position_calls == [5]

    @pytest.mark.parametrize("hw", [ROLLER, VENETIAN])
    def test_roller_and_venetian_default_to_open_when_rain_is_enabled(self, hw):
        s = _Scenario(hw, rain_protection_enabled=True)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.position_calls == [100]

    @pytest.mark.parametrize("hw", [ROLLER, VENETIAN, GENERIC])
    def test_rain_protection_is_off_by_default_outside_awning_and_screen(self, hw):
        s = _Scenario(hw)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is not ShadingState.RAIN_SAFE

    def test_missing_hardware_type_cannot_extend_a_cover(self):
        # A cover group stored without a hardware type resolves to GENERIC;
        # with rain protection explicitly enabled the fallback must retract.
        s = _Scenario(cover_hardware_type_from_str(None), rain_protection_enabled=True)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.position_calls == [0]

    def test_rain_protection_disabled_for_the_window_is_respected(self):
        s = _Scenario(AWNING, rain_protection_enabled=False)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is not ShadingState.RAIN_SAFE


class TestNightIncident:
    """The reported event happened at 03:24:59 local time, sun below horizon."""

    @pytest.mark.parametrize("start", [0, 40])
    def test_night_rain_never_extends_the_awning(self, start):
        s = _Scenario(AWNING, cover_position=start)
        s.night()
        s.set_rain(True)
        data = s.cycle()
        assert data.window_results["w1"].night_active is True
        assert data.window_results["w1"].lifecycle_state == "night"
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.diag(data).rain_safe_active is True
        assert s.diag(data).target_position_ha == 0
        assert s.position_calls and set(s.position_calls) == {0}


class TestNoRainUnchanged:
    def test_no_rain_enforces_no_rain_target(self):
        s = _Scenario(AWNING)
        s.set_rain(False)
        data = s.cycle()
        assert _state_of(data) is not ShadingState.RAIN_SAFE
        assert s.diag(data).tier_decided_by not in ("RainEvaluator", "RainSafeHold")
        assert s.diag(data).rain_safe_active is False
        assert s.diag(data).is_safety is False

    def test_normal_shading_decision_is_not_altered(self):
        # Same decision as before the change: strong shade from the solar
        # evaluator, dispatched as a normal (non-safety) command.
        s = _Scenario(AWNING)
        data = s.cycle()
        assert _state_of(data) is ShadingState.STRONG_SHADE
        assert s.diag(data).tier_decided_by == "SolarEvaluator"
        assert s.diag(data).is_safety is False
        assert s.position_calls == [10]


class TestRainSafeStaysConsistentAcrossEvaluations:
    def test_repeated_evaluation_keeps_the_same_target(self):
        s = _Scenario(AWNING)
        s.set_rain(True)
        first = s.cycle(0)
        s.set_cover_position(0)          # cover reached the retracted position
        second = s.cycle(5)
        assert _state_of(first) is _state_of(second) is ShadingState.RAIN_SAFE
        assert s.diag(first).target_position_ha == 0
        assert s.diag(second).target_position_ha == 0
        # Wind/storm parity: the safety command may be repeated every cycle,
        # but never with a different (opposite) target.
        assert s.position_calls and set(s.position_calls) == {0}

    def test_dry_cooldown_hold_keeps_the_same_target(self):
        s = _Scenario(AWNING)
        s.set_rain(True)
        s.cycle(0)
        s.set_cover_position(0)
        s.set_rain(False)                # rain stopped; dry cooldown (30 min) running
        held = s.cycle(10)
        assert _state_of(held) is ShadingState.RAIN_SAFE
        assert s.diag(held).tier_decided_by == "RainSafeHold"
        assert s.diag(held).target_position_ha == 0
        assert s.diag(held).rain_safe_active is True
        assert set(s.position_calls) == {0}

    def test_hold_is_released_after_the_dry_cooldown(self):
        s = _Scenario(AWNING)
        s.set_rain(True)
        s.cycle(0)
        s.set_cover_position(0)
        s.set_rain(False)
        assert _state_of(s.cycle(29)) is ShadingState.RAIN_SAFE
        released = s.cycle(31)           # > 30 min dry
        assert _state_of(released) is not ShadingState.RAIN_SAFE
        # (the release cycle itself still reports the state it is leaving)
        after = s.cycle(36)
        assert s.diag(after).rain_safe_active is False
        assert s.diag(after).is_safety is False

    def test_reload_with_rain_still_active_gives_the_same_target(self):
        first = _Scenario(AWNING)
        first.set_rain(True)
        first.cycle()
        reloaded = _Scenario(AWNING)     # fresh coordinator, as after an entry reload
        reloaded.set_rain(True)
        data = reloaded.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert reloaded.position_calls == first.position_calls == [0]


class TestRainSafeWithActiveControl:
    def test_recommendation_is_correct_but_no_command_when_active_control_is_off(self):
        # Active Control off is recommendation-only for every state, safety
        # included (CommandFilter: recommendation_only blocks everything).
        s = _Scenario(AWNING, active_control=False)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.diag(data).target_position_ha == 0
        assert s.diag(data).is_safety is True
        assert s.diag(data).command_blocked_reason == "recommendation_only"
        assert s.hass.services.async_call.await_count == 0

    def test_no_command_even_with_a_manual_override_when_active_control_is_off(self):
        s = _Scenario(AWNING, active_control=False)
        s.cycle(0)
        s.set_cover_position(90)
        s.cycle(5)
        s.set_rain(True)
        data = s.cycle(15)
        assert s.diag(data).target_position_ha == 0
        assert s.hass.services.async_call.await_count == 0


_WIND_MS = 16.0     # >= 14 m/s wind threshold, < 20 m/s storm threshold
_STORM_MS = 25.0    # >= 20 m/s storm threshold

_MATRIX = [
    (AWNING, "rain", 0), (AWNING, "wind", 0), (AWNING, "storm", 0),
    (SCREEN, "rain", 100), (SCREEN, "wind", 100), (SCREEN, "storm", 100),
    (ROLLER, "rain", 100), (ROLLER, "wind", 100), (ROLLER, "storm", 100),
    (VENETIAN, "rain", 100), (VENETIAN, "wind", 100), (VENETIAN, "storm", 100),
]


def _trigger(s: _Scenario, trigger: str) -> None:
    if trigger == "rain":
        s.set_rain(True)
    else:
        s.set_wind(_WIND_MS if trigger == "wind" else _STORM_MS)


def _safety_scenario(hw: CoverHardwareType, trigger: str, **kw) -> _Scenario:
    # Roller/venetian rain protection is off by default and not switchable from
    # the stable UI: enable it explicitly to exercise the documented target.
    if trigger == "rain" and hw in (ROLLER, VENETIAN):
        kw.setdefault("rain_protection_enabled", True)
    s = _Scenario(hw, **kw)
    _trigger(s, trigger)
    return s


class TestWindStormTargets:
    @pytest.mark.parametrize("trigger,state", [
        ("wind", ShadingState.WIND_SAFE), ("storm", ShadingState.STORM_SAFE),
    ])
    @pytest.mark.parametrize("hw,expected", [
        (AWNING, 0), (SCREEN, 100), (ROLLER, 100), (VENETIAN, 100), (GENERIC, 100),
    ])
    def test_targets_follow_the_hardware_table(self, hw, expected, trigger, state):
        s = _Scenario(hw)
        _trigger(s, trigger)
        data = s.cycle()
        assert _state_of(data) is state
        assert s.position_calls == [expected]
        assert to_ha_position(HARDWARE_SAFE_POSITIONS[hw]) == expected
        assert s.diag(data).is_safety is True

    def test_storm_still_outranks_rain(self):
        s = _Scenario(AWNING)
        s.set_wind(_STORM_MS)
        s.set_rain(True)
        data = s.cycle()
        assert _state_of(data) is ShadingState.STORM_SAFE


class TestExteriorScreenWindStormFix:
    """Additional safety fix, independent of the rain conversion: an exterior
    screen is a vertical cover and is RAISED (HA 100) in wind/storm; it used to
    be lowered to HA 0, the most exposed position."""

    @pytest.mark.parametrize("trigger", ["wind", "storm"])
    def test_screen_is_raised_not_lowered(self, trigger):
        s = _Scenario(SCREEN, cover_position=40)
        _trigger(s, trigger)
        s.cycle()
        assert s.position_calls == [100]
        assert 0 not in s.position_calls

    @pytest.mark.parametrize("trigger", ["wind", "storm"])
    def test_lowered_screen_is_raised(self, trigger):
        s = _Scenario(SCREEN, cover_position=0)
        _trigger(s, trigger)
        s.cycle()
        assert s.position_calls == [100]

    @pytest.mark.parametrize("trigger", ["wind", "storm"])
    def test_already_raised_screen_gets_no_opposing_command(self, trigger):
        s = _Scenario(SCREEN, cover_position=100)
        _trigger(s, trigger)
        s.cycle()
        assert s.position_calls and set(s.position_calls) == {100}

    @pytest.mark.parametrize("trigger", ["wind", "storm"])
    def test_awning_wind_storm_stays_retracted(self, trigger):
        s = _Scenario(AWNING, cover_position=40)
        _trigger(s, trigger)
        s.cycle()
        assert s.position_calls == [0]

    @pytest.mark.parametrize("trigger", ["rain", "wind", "storm"])
    def test_no_safety_state_ever_lowers_a_screen(self, trigger):
        for start in (0, 40, 100):
            s = _Scenario(SCREEN, cover_position=start)
            _trigger(s, trigger)
            s.cycle(0)
            s.set_cover_position(100)
            s.cycle(5)
            assert s.position_calls and set(s.position_calls) == {100}, (trigger, start)


class TestSafetyTargetMatrix:
    """Rain / wind / storm x awning / screen / roller / venetian."""

    @pytest.mark.parametrize("hw,trigger,expected", _MATRIX)
    def test_documented_target(self, hw, trigger, expected):
        s = _safety_scenario(hw, trigger)
        data = s.cycle()
        assert s.diag(data).is_safety is True
        assert s.diag(data).target_position_ha == expected
        assert s.position_calls == [expected]

    @pytest.mark.parametrize("hw,trigger,expected", _MATRIX)
    def test_already_safe_position_gets_no_unsafe_counter_command(self, hw, trigger, expected):
        s = _safety_scenario(hw, trigger, cover_position=expected)
        s.cycle(0)
        s.cycle(5)
        assert s.position_calls and set(s.position_calls) == {expected}

    @pytest.mark.parametrize("hw,trigger,expected", _MATRIX)
    def test_stale_assumed_position_cannot_block_the_safety_command(self, hw, trigger, expected):
        # RF-style cover without position feedback: SmartShading assumes "already
        # safe" but the cover was moved with the remote control.
        s = _safety_scenario(hw, trigger)
        cap = _capability(s.cover)
        cap.has_reliable_position_feedback = False
        cap.assumed_state = True
        s.coord._cover_capabilities[s.cover] = cap
        s.hass.states.set(s.cover, "open", supported_features=15)     # no current_position
        s.coord.assumed_state_manager.update(
            s.cover, 100 - expected, commanded_at=_NOW - timedelta(hours=1),
            has_reliable_position_feedback=False)
        data = s.cycle()
        assert s.diag(data).command_blocked_reason is None
        assert s.position_calls == [expected]

    @pytest.mark.parametrize("hw,trigger,expected", _MATRIX)
    def test_position_clamps_do_not_change_the_safety_target(self, hw, trigger, expected):
        s = _safety_scenario(hw, trigger)
        data = s.cycle()
        d = s.diag(data)
        assert d.daytime_min_open_applied is False
        assert d.anti_heat_buildup_applied is False
        assert d.target_position_ha == expected

    @pytest.mark.parametrize("hw", [AWNING, SCREEN, ROLLER, VENETIAN])
    def test_rain_wind_and_storm_agree_per_hardware(self, hw):
        results = {}
        for trigger in ("rain", "wind", "storm"):
            s = _safety_scenario(hw, trigger)
            s.cycle()
            results[trigger] = s.position_calls
        assert results["rain"] == results["wind"] == results["storm"]


class TestInvertPosition:
    """With invert_position the cover's integration already inverts the numbering,
    so the HA value equals the internal value (no arithmetic conversion).  Rain,
    wind and storm must stay consistent and must not be converted twice."""

    @pytest.mark.parametrize("hw,trigger,expected", _MATRIX)
    def test_inverted_cover_gets_the_internal_value_without_double_conversion(
            self, hw, trigger, expected):
        s = _safety_scenario(hw, trigger)
        s.set_invert(True)
        s.cycle()
        assert s.position_calls == [100 - expected]

    @pytest.mark.parametrize("hw", [AWNING, SCREEN, ROLLER, VENETIAN])
    @pytest.mark.parametrize("invert", [False, True])
    def test_rain_and_wind_storm_agree(self, hw, invert):
        results = {}
        for trigger in ("rain", "wind", "storm"):
            s = _safety_scenario(hw, trigger)
            s.set_invert(invert)
            s.cycle()
            results[trigger] = s.position_calls
        assert results["rain"] == results["wind"] == results["storm"]


# ---------------------------------------------------------------------------
# Package B1 — RAIN_SAFE gets the Tier-1 dispatch treatment
# ---------------------------------------------------------------------------


class TestTier1DispatchParity:
    def test_rain_is_flagged_as_safety(self):
        s = _Scenario(AWNING)
        s.set_rain(True)
        data = s.cycle()
        assert s.diag(data).is_safety is True

    def test_startup_grace_does_not_delay_rain_safety(self):
        s = _Scenario(AWNING, cover_position=100)
        s.coord._startup_cycles_remaining = 1        # first cycle after a restart
        s.set_rain(True)
        data = s.cycle(0)
        assert s.diag(data).dispatch_suppressed_reason is None
        assert s.position_calls == [0]

    def test_startup_grace_still_delays_normal_shading(self):
        s = _Scenario(AWNING)
        s.coord._startup_cycles_remaining = 1
        data = s.cycle(0)
        assert s.diag(data).dispatch_suppressed_reason == "startup_grace_active"
        assert s.position_calls == []

    def test_state_guard_interval_does_not_block_rain_safety(self):
        s = _Scenario(AWNING, cover_position=40)
        s.cycle(0)                                    # normal command -> action interval starts
        shading_calls = list(s.position_calls)
        assert shading_calls
        s.set_cover_position(shading_calls[-1])
        s.set_rain(True)
        data = s.cycle(1)                             # < 3 min later
        assert s.diag(data).command_blocked_reason is None
        assert s.position_calls == [*shading_calls, 0]

    def test_state_guard_interval_still_blocks_normal_shading(self):
        # Control for the test above: a normal (non-safety) re-target within the
        # action interval is still held back by the StateGuard.
        s = _Scenario(AWNING, cover_position=40)
        s.cycle(0)
        shading_calls = list(s.position_calls)
        s.set_cover_position(shading_calls[-1])
        s.hass.states.set("sun.sun", "above_horizon", azimuth=180.0, elevation=12.0)
        data = s.cycle(1)
        assert s.diag(data).command_blocked_reason == "guard_action_interval"
        assert s.position_calls == shading_calls

    def test_same_position_filter_does_not_suppress_a_stale_assumed_position(self):
        # RF-style cover without position feedback: SmartShading assumes
        # "retracted" but the awning was extended with the remote control.
        s = _Scenario(AWNING, cover_position=0)
        cap = _capability()
        cap.has_reliable_position_feedback = False
        cap.assumed_state = True
        s.coord._cover_capabilities[_COVER] = cap
        s.hass.states.set(_COVER, "open", supported_features=15)   # no current_position
        s.coord.assumed_state_manager.update(
            _COVER, 100, commanded_at=_NOW - timedelta(hours=1),
            has_reliable_position_feedback=False)
        s.set_rain(True)
        data = s.cycle(0)
        assert s.diag(data).command_blocked_reason is None
        assert s.position_calls == [0]

    def test_rain_and_storm_are_treated_identically_by_the_command_filter(self):
        rain = _Scenario(AWNING, cover_position=0)
        rain.set_rain(True)
        rd = rain.diag(rain.cycle())
        storm = _Scenario(AWNING, cover_position=0)
        storm.set_wind(40.0)
        sd = storm.diag(storm.cycle())
        for attr in ("is_safety", "command_allowed", "command_blocked_reason",
                     "service_call_sent", "dispatch_suppressed_reason",
                     "target_position_ha"):
            assert getattr(rd, attr) == getattr(sd, attr), attr


# ---------------------------------------------------------------------------
# Package B2 — Rain-Safety overrules a manual override like wind/storm
# ---------------------------------------------------------------------------


class TestManualOverridePreemption:
    def test_rain_overrules_a_daytime_manual_override(self):
        s = _Scenario(AWNING, cover_position=40)
        _establish_manual_override(s, user_position=90)   # user extended the awning
        calls_before = len(s.position_calls)
        s.set_rain(True)
        data = s.cycle(15)
        d = s.diag(data)
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert d.manual_override_active is False
        assert d.manual_override_release_reason == "safety"
        assert d.command_blocked_reason is None
        assert s.position_calls[calls_before:] == [0]

    def test_rain_overrules_a_night_manual_override(self):
        s = _Scenario(AWNING, cover_position=40)
        s.night()
        s.cycle(0)                          # night command
        s.set_cover_position(60)            # user moves the cover during the night
        s.cycle(5)
        data = s.cycle(10)
        assert s.diag(data).manual_override_active
        assert s.diag(data).manual_override_scope == "night"
        calls_before = len(s.position_calls)
        s.set_rain(True)
        data = s.cycle(15)
        d = s.diag(data)
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert d.manual_override_active is False
        assert d.manual_override_release_reason == "safety"
        assert s.position_calls[calls_before:] == [0]

    def test_override_does_not_come_back_after_the_rain(self):
        s = _Scenario(AWNING, cover_position=40)
        _establish_manual_override(s)
        s.set_rain(True)
        s.cycle(15)
        s.set_cover_position(0)             # cover followed the rain command
        s.set_rain(False)
        for minutes in (20, 25, 40, 45):
            data = s.cycle(minutes)
            assert s.diag(data).manual_override_active is False, minutes

    def test_manual_move_during_rain_safety_is_not_registered_as_override(self):
        # Same as wind/storm: while the safety state is active no override
        # detection runs, so the next cycle simply retracts again.
        s = _Scenario(AWNING, cover_position=40)
        s.set_rain(True)
        s.cycle(0)
        s.set_cover_position(90)            # user extends it during the rain
        data = s.cycle(5)
        assert s.diag(data).manual_override_active is False
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.position_calls[-1] == 0

    def test_override_handling_is_identical_for_rain_and_storm(self):
        rain = _Scenario(AWNING, cover_position=40)
        _establish_manual_override(rain)
        rain.set_rain(True)
        rd = rain.diag(rain.cycle(15))
        storm = _Scenario(AWNING, cover_position=40)
        _establish_manual_override(storm)
        storm.set_wind(40.0)
        sd = storm.diag(storm.cycle(15))
        for attr in ("manual_override_active", "manual_override_release_reason",
                     "command_blocked_reason", "service_call_sent", "is_safety"):
            assert getattr(rd, attr) == getattr(sd, attr), attr


# ---------------------------------------------------------------------------
# Preemption: running / waiting dispatch, stale generation, covers and zones
# ---------------------------------------------------------------------------


class _GenerationBumpingSerial(GlobalSerialDispatch):
    """Simulates a presence/contact/lifecycle event arriving while the first
    queued intent waits for its dispatch slot (bumps the coordinator's
    dispatch generation once)."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.coordinator = None
        self._armed = True

    def time_until_next_allowed(self, *args, **kwargs):
        if self._armed and self.coordinator is not None:
            self._armed = False
            self.coordinator._dispatch_generation += 1
        return super().time_until_next_allowed(*args, **kwargs)


class TestPreemptionAndStaleGenerations:
    def test_cover_that_is_still_moving_receives_the_rain_command(self):
        s = _Scenario(AWNING, cover_position=55)
        s.set_cover_position(55, state="opening")        # moving out right now
        s.set_rain(True)
        s.cycle()
        assert s.position_calls == [0]

    def test_rain_command_waits_for_its_dispatch_slot_and_is_then_sent(self):
        serial = GlobalSerialDispatch(min_interval=timedelta(milliseconds=150))
        s = _Scenario(AWNING, serial=serial)
        serial.record_dispatch(_NOW)                      # another zone just dispatched
        s.set_rain(True)
        data = s.cycle()
        assert s.diag(data).dispatch_throttled is True
        assert s.position_calls == [0]

    def test_stale_generation_cancels_normal_intents_but_not_rain_safety(self):
        serial = _GenerationBumpingSerial(min_interval=timedelta(milliseconds=1))
        s = _Scenario(AWNING, serial=serial,
                      extra=[_Win("w2", ROLLER, ["cover.roller"])],
                      covers=["cover.awning"])
        serial.coordinator = s.coord
        s.set_rain(True)
        data = s.cycle(0)
        # Rain intent (safety) is dispatched first and is exempt from the stale
        # check; the roller's normal shading intent is cancelled (old plan).
        assert s.calls_for("cover.awning") == [0]
        assert s.calls_for("cover.roller") == []
        assert s.diag(data, "w2").service_call_sent is False
        # The cancelled plan is not executed later.
        s.set_cover_position(0, cover="cover.awning")
        s.cycle(5)
        assert set(s.calls_for("cover.awning")) == {0}

    def test_every_cover_of_a_group_is_retracted(self):
        covers = ["cover.awning_left", "cover.awning_right"]
        s = _Scenario(AWNING, covers=covers)
        s.set_rain(True)
        s.cycle()
        assert s.calls_for("cover.awning_left") == [0]
        assert s.calls_for("cover.awning_right") == [0]



# ---------------------------------------------------------------------------
# Rain-sensor state listener
# ---------------------------------------------------------------------------


class _FakeBus:
    """Stand-in for async_track_state_change_event with real subscribe /
    unsubscribe semantics, so tests can count active listeners and prove that
    nothing is called after unload."""

    def __init__(self) -> None:
        self.subs: dict[str, list] = {}
        self.registered = 0

    def track(self, hass, entity_id, action):
        self.subs.setdefault(entity_id, []).append(action)
        self.registered += 1

        def _unsub():
            lst = self.subs.get(entity_id, [])
            if action in lst:
                lst.remove(action)

        return _unsub

    def active(self, entity_id: str) -> int:
        return len(self.subs.get(entity_id, []))

    def fire(self, entity_id: str, old, new) -> None:
        event = SimpleNamespace(data={"old_state": old, "new_state": new})
        for callback in list(self.subs.get(entity_id, [])):
            callback(event)


def _attach_rain_listener(s: _Scenario, bus: _FakeBus) -> _Scenario:
    s.coord.async_request_refresh = MagicMock(return_value=None)
    s.entry = _make_entry()
    with patch.object(_coord_mod, "async_track_state_change_event", side_effect=bus.track):
        s.coord.async_setup_rain_listener(s.entry)
    return s


def _change_rain(s: _Scenario, bus: _FakeBus, new_raw: str | None) -> None:
    """The rain entity changes to ``new_raw`` (None = entity removed)."""
    old = s.hass.states.get(s.rain_entity)
    if new_raw is None:
        s.hass.states._d.pop(s.rain_entity, None)
    else:
        s.hass.states.set(s.rain_entity, new_raw)
    bus.fire(s.rain_entity, old, s.hass.states.get(s.rain_entity))


def _listener_scenario(hw=AWNING, *, rain_entity=_RAIN, **kw):
    bus = _FakeBus()
    s = _Scenario(hw, rain_entity=rain_entity, **kw)
    _attach_rain_listener(s, bus)
    return s, bus


class TestRainSensorListener:
    def test_dry_to_raining_bumps_the_generation_and_requests_a_refresh(self):
        s, bus = _listener_scenario()
        gen = s.coord._dispatch_generation
        _change_rain(s, bus, "on")
        assert s.coord._dispatch_generation == gen + 1
        assert s.coord.async_request_refresh.call_count == 1

    def test_raining_to_dry_requests_a_refresh_without_a_generation_bump(self):
        s, bus = _listener_scenario()
        s.set_rain(True)
        gen = s.coord._dispatch_generation
        _change_rain(s, bus, "off")
        assert s.coord.async_request_refresh.call_count == 1
        assert s.coord._dispatch_generation == gen       # old plan stays valid

    def test_raining_to_dry_starts_the_hold_and_dry_cooldown(self):
        s, bus = _listener_scenario()
        s.set_rain(True)
        s.cycle(0)
        s.set_cover_position(0)
        _change_rain(s, bus, "off")                       # event -> prompt re-evaluation
        data = s.cycle(1)                                 # what the debounced refresh runs
        assert _state_of(data) is ShadingState.RAIN_SAFE
        assert s.diag(data).tier_decided_by == "RainSafeHold"
        assert _state_of(s.cycle(25)) is ShadingState.RAIN_SAFE      # still inside the cooldown
        assert _state_of(s.cycle(32)) is not ShadingState.RAIN_SAFE  # released afterwards

    def test_numeric_changes_inside_the_same_status_do_nothing(self):
        s, bus = _listener_scenario(rain_entity="sensor.rain_rate")
        s.set_rain(True)                                  # 1.5 mm/h
        gen = s.coord._dispatch_generation
        for raw in ("0.2", "0.5", "3.4", "12.0", "0.01"):
            _change_rain(s, bus, raw)
        assert s.coord.async_request_refresh.call_count == 0
        assert s.coord._dispatch_generation == gen

    def test_numeric_status_changes_are_detected(self):
        s, bus = _listener_scenario(rain_entity="sensor.rain_rate")
        gen = s.coord._dispatch_generation
        _change_rain(s, bus, "0.3")                       # 0.0 -> raining
        assert (s.coord.async_request_refresh.call_count, s.coord._dispatch_generation) == (1, gen + 1)
        _change_rain(s, bus, "0.0")                       # raining -> dry
        assert (s.coord.async_request_refresh.call_count, s.coord._dispatch_generation) == (2, gen + 1)

    @pytest.mark.parametrize("raw", ["unavailable", "unknown"])
    def test_unavailable_or_unknown_to_raining_reacts(self, raw):
        s, bus = _listener_scenario()
        s.hass.states.set(_RAIN, raw)
        gen = s.coord._dispatch_generation
        _change_rain(s, bus, "on")
        assert (s.coord.async_request_refresh.call_count, s.coord._dispatch_generation) == (1, gen + 1)

    def test_raining_to_unavailable_reevaluates_without_a_bump(self):
        s, bus = _listener_scenario()
        s.set_rain(True)
        gen = s.coord._dispatch_generation
        _change_rain(s, bus, "unavailable")
        assert s.coord.async_request_refresh.call_count == 1
        assert s.coord._dispatch_generation == gen

    def test_dry_and_unavailable_flapping_is_ignored(self):
        # Neither status triggers the evaluator: nothing to re-evaluate.
        s, bus = _listener_scenario()
        for raw in ("unavailable", "off", "unknown", "off", "unavailable"):
            _change_rain(s, bus, raw)
        assert s.coord.async_request_refresh.call_count == 0

    def test_invalid_values_are_treated_conservatively(self):
        s, bus = _listener_scenario()
        _change_rain(s, bus, "garbage")                   # dry -> unknown: ignored
        assert s.coord.async_request_refresh.call_count == 0
        _change_rain(s, bus, "on")                        # unknown -> raining: reacts
        assert s.coord.async_request_refresh.call_count == 1

    def test_entity_added_or_removed(self):
        s, bus = _listener_scenario()
        s.hass.states._d.pop(_RAIN, None)
        _change_rain(s, bus, "on")                        # appears already raining
        assert s.coord.async_request_refresh.call_count == 1
        _change_rain(s, bus, None)                        # removed while raining
        assert s.coord.async_request_refresh.call_count == 2

    def test_no_listener_without_a_configured_rain_entity(self):
        s = _Scenario(AWNING)
        s.coord._rain_sensor_id = None
        bus = _FakeBus()
        _attach_rain_listener(s, bus)
        assert bus.registered == 0

    def test_listener_subscribes_only_to_the_configured_rain_entity(self):
        s, bus = _listener_scenario()
        assert list(bus.subs) == [_RAIN]
        assert bus.registered == 1

    # -- setup / reload / unload --------------------------------------------------
    def test_setup_with_rain_already_active_is_covered_by_the_first_cycle(self):
        s = _Scenario(AWNING)
        s.set_rain(True)
        data = s.cycle()                                  # the first refresh of the setup
        assert s.position_calls == [0] and _state_of(data) is ShadingState.RAIN_SAFE
        bus = _FakeBus()
        gen = s.coord._dispatch_generation
        _attach_rain_listener(s, bus)
        assert s.coord.async_request_refresh.call_count == 0     # nothing missed
        assert s.coord._dispatch_generation == gen

    def test_rain_that_starts_between_the_first_refresh_and_the_listener_is_caught(self):
        s = _Scenario(AWNING)
        s.cycle()                                         # first refresh: dry
        s.set_rain(True)                                  # rain starts before registration
        bus = _FakeBus()
        gen = s.coord._dispatch_generation
        _attach_rain_listener(s, bus)
        assert s.coord.async_request_refresh.call_count == 1
        assert s.coord._dispatch_generation == gen + 1

    def test_repeated_setup_keeps_exactly_one_subscription(self):
        s, bus = _listener_scenario()
        with patch.object(_coord_mod, "async_track_state_change_event", side_effect=bus.track):
            s.coord.async_setup_rain_listener(s.entry)
        assert bus.registered == 2 and bus.active(_RAIN) == 1
        gen = s.coord._dispatch_generation
        _change_rain(s, bus, "on")
        assert s.coord._dispatch_generation == gen + 1      # one reaction, not two

    def test_reload_with_a_new_coordinator_leaves_exactly_one_listener(self):
        bus = _FakeBus()
        hass = _make_hass()
        old = _attach_rain_listener(_Scenario(AWNING, hass=hass), bus)
        old.coord.async_teardown_rain_listener()            # entry unload
        new = _attach_rain_listener(_Scenario(AWNING, hass=hass), bus)   # entry setup
        assert bus.active(_RAIN) == 1
        old_gen, new_gen = old.coord._dispatch_generation, new.coord._dispatch_generation
        _change_rain(new, bus, "on")
        assert old.coord._dispatch_generation == old_gen
        assert new.coord._dispatch_generation == new_gen + 1

    def test_unload_removes_the_listener_and_is_idempotent(self):
        s, bus = _listener_scenario()
        s.entry.async_on_unload.assert_called_once()
        assert bus.active(_RAIN) == 1
        s.coord.async_teardown_rain_listener()
        s.coord.async_teardown_rain_listener()
        assert bus.active(_RAIN) == 0

    def test_no_callback_and_no_service_call_after_unload(self):
        s, bus = _listener_scenario()
        s.coord.async_teardown_rain_listener()
        gen = s.coord._dispatch_generation
        _change_rain(s, bus, "on")
        assert s.coord._dispatch_generation == gen
        assert s.coord.async_request_refresh.call_count == 0
        assert s.hass.services.async_call.await_count == 0

    # -- production-faithful plan invalidation ------------------------------------
    def _hazard_scenario(self, *, with_event: bool):
        """Zone with a normal plan that would EXTEND the awning (shade = HA 100)
        and queued behind another zone's command; rain starts while it waits."""
        hass = _make_hass()
        serial = GlobalSerialDispatch(min_interval=timedelta(milliseconds=600))
        s = _Scenario(AWNING, hass=hass, serial=serial, cover_position=0,
                      window_id="x1", zone_id="zx", covers=["cover.x1"])
        s.coord.shade_position_defaults = ShadePositionDefaults(
            light_shade_position=100, normal_shade_position=100, strong_shade_position=100)
        bus = _FakeBus()
        _attach_rain_listener(s, bus)

        async def main():
            with s._clock(0):
                await serial.lock.acquire()                # another zone is dispatching
                old_plan = asyncio.ensure_future(s.coord._async_update_data())
                await asyncio.sleep(0.05)                  # old plan waits for its slot
                if with_event:
                    _change_rain(s, bus, "on")             # rain event -> listener
                else:
                    s.set_rain(True)                       # rain only visible on the next poll
                serial.lock.release()
                await old_plan
                # HA runs refreshes of one coordinator serially: the next cycle
                # (event-requested or periodic) starts after the old one.
                await s.coord._async_update_data()

        asyncio.run(main())
        return s

    def test_old_unsent_normal_plan_of_the_zone_is_discarded_after_the_rain_event(self):
        s = self._hazard_scenario(with_event=True)
        assert s.position_calls == [0]                      # never extended, then retracted

    def test_without_the_event_the_old_plan_still_extends_the_cover(self):
        # Documents the limitation the listener removes: only an event can
        # invalidate a plan that was computed before the rain was visible.
        s = self._hazard_scenario(with_event=False)
        assert s.position_calls == [100, 0]

    def test_all_windows_of_the_zone_drop_their_old_plan(self):
        hass = _make_hass()
        serial = GlobalSerialDispatch(min_interval=timedelta(milliseconds=500))
        s = _Scenario(AWNING, hass=hass, serial=serial, cover_position=40,
                      window_id="x1", zone_id="zx", covers=["cover.x1"],
                      extra=[_Win("x2", SCREEN, ["cover.x2"], cover_position=40),
                             _Win("x3", ROLLER, ["cover.x3"], cover_position=40)])
        bus = _FakeBus()
        _attach_rain_listener(s, bus)

        async def main():
            with s._clock(0):
                await serial.lock.acquire()                # another zone is dispatching
                old_plan = asyncio.ensure_future(s.coord._async_update_data())
                await asyncio.sleep(0.05)
                _change_rain(s, bus, "on")
                serial.lock.release()
                await old_plan
                await s.coord._async_update_data()

        asyncio.run(main())
        assert s.calls_for("cover.x1") == [0]               # awning retracted, no old command
        assert s.calls_for("cover.x2") == [100]             # screen raised, never lowered to 10
        # Not rain-protected: its normal plan was discarded, and the cycle that
        # carries a safety command defers comfort dispatch to a later cycle.
        assert s.calls_for("cover.x3") == []

    @pytest.mark.parametrize("zones", [2, 3])
    def test_every_zone_invalidates_its_own_plan(self, zones):
        hass = _make_hass()
        serial = GlobalSerialDispatch(min_interval=timedelta(milliseconds=400))
        bus = _FakeBus()
        scenarios = []
        for i in range(zones):
            tag = "abc"[i]
            z = _Scenario(AWNING, hass=hass, serial=serial, cover_position=0,
                          window_id=f"{tag}1", zone_id=f"z{tag}", covers=[f"cover.{tag}1"])
            z.coord.shade_position_defaults = ShadePositionDefaults(
                light_shade_position=100, normal_shade_position=100, strong_shade_position=100)
            _attach_rain_listener(z, bus)
            scenarios.append(z)
        gens: list[int] = []
        after_event: list[int] = []

        async def main():
            with scenarios[0]._clock(0):
                await serial.lock.acquire()                  # another zone is dispatching
                old_plans = [asyncio.ensure_future(z.coord._async_update_data())
                             for z in scenarios]
                await asyncio.sleep(0.05)
                gens.extend(z.coord._dispatch_generation for z in scenarios)
                _change_rain(scenarios[0], bus, "on")        # one shared rain entity
                after_event.extend(z.coord._dispatch_generation for z in scenarios)
                serial.lock.release()
                await asyncio.gather(*old_plans)
                for z in scenarios:                          # each zone's next cycle
                    await z.coord._async_update_data()

        asyncio.run(main())
        for z, gen, after in zip(scenarios, gens, after_event):
            assert after == gen + 1                          # exactly one bump per zone
            assert z.coord.async_request_refresh.call_count == 1
        for tag in "abc"[:zones]:
            assert hass_calls(scenarios[0], f"cover.{tag}1") == [0], tag

    def test_rain_start_signals_a_running_comfort_dispatch(self):
        s, bus = _listener_scenario()
        cancellation = asyncio.Event()
        s.coord._active_dispatch_cancellation = cancellation
        _change_rain(s, bus, "on")
        assert cancellation.is_set()

    def test_rain_end_does_not_signal_a_running_comfort_dispatch(self):
        s, bus = _listener_scenario()
        s.set_rain(True)
        cancellation = asyncio.Event()
        s.coord._active_dispatch_cancellation = cancellation
        _change_rain(s, bus, "off")
        assert not cancellation.is_set()

    def test_refresh_is_a_tracked_background_task_of_the_entry(self):
        s, bus = _listener_scenario()
        _change_rain(s, bus, "on")
        assert s.entry.async_create_background_task.call_count == 1
        assert s.entry.async_create_background_task.call_args.args[2] == "smartshading_rain_refresh"

    def test_no_refresh_is_scheduled_once_the_coordinator_is_unloading(self):
        s, bus = _listener_scenario()
        s.coord.async_request_refresh = MagicMock(return_value=MagicMock())
        s.coord._unloading = True
        _change_rain(s, bus, "on")
        assert s.entry.async_create_background_task.call_count == 0

    # -- interplay with the other gates ---------------------------------------------
    def test_active_control_off_reacts_but_sends_nothing(self):
        s, bus = _listener_scenario(active_control=False)
        _change_rain(s, bus, "on")
        assert s.coord.async_request_refresh.call_count == 1
        data = s.cycle()
        assert s.diag(data).target_position_ha == 0
        assert s.hass.services.async_call.await_count == 0

    def test_event_during_a_manual_override(self):
        s, bus = _listener_scenario(cover_position=40)
        _establish_manual_override(s)
        n = len(s.position_calls)
        _change_rain(s, bus, "on")
        data = s.cycle(15)
        assert s.diag(data).manual_override_active is False
        assert s.position_calls[n:] == [0]

    def test_event_during_the_startup_grace(self):
        s, bus = _listener_scenario(cover_position=100)
        s.coord._startup_cycles_remaining = 1
        _change_rain(s, bus, "on")
        data = s.cycle()
        assert s.diag(data).dispatch_suppressed_reason is None
        assert s.position_calls == [0]

    def test_event_inside_the_state_guard_interval(self):
        s, bus = _listener_scenario(cover_position=40)
        s.cycle(0)                                          # normal command -> interval starts
        s.set_cover_position(s.position_calls[-1])
        n = len(s.position_calls)
        _change_rain(s, bus, "on")
        data = s.cycle(1)
        assert s.diag(data).command_blocked_reason is None
        assert s.position_calls[n:] == [0]


def hass_calls(s: _Scenario, cover: str) -> list[int]:
    return s.calls_for(cover)


# ---------------------------------------------------------------------------
# Global dispatch queue: plain FIFO, no global preemption
# ---------------------------------------------------------------------------


class TestGlobalDispatchQueue:
    def test_rain_command_waits_fifo_behind_intents_other_zones_already_queued(self):
        # The rain listener invalidates the old plan of ITS OWN coordinator only.
        # Intents other zones already queued in the global dispatch lock are
        # served first (plain FIFO, same as wind/storm; the zones' items take their
        # dispatch slots alternately); a global safety priority is a separate,
        # later change.  If it is added, "x1" moves ahead of the still-queued
        # intents.
        hass = _make_hass()
        serial = GlobalSerialDispatch(min_interval=timedelta(milliseconds=500))
        zone_a = _Scenario(ROLLER, hass=hass, serial=serial, window_id="a1", zone_id="za",
                           covers=["cover.a1"], extra=[_Win("a2", ROLLER, ["cover.a2"])])
        zone_b = _Scenario(ROLLER, hass=hass, serial=serial, window_id="b1", zone_id="zb",
                           covers=["cover.b1"], extra=[_Win("b2", ROLLER, ["cover.b2"])])
        zone_x = _Scenario(AWNING, hass=hass, serial=serial, window_id="x1", zone_id="zx",
                           covers=["cover.x1"])
        order: list[str] = []

        async def record(domain, service, data, *a, **k):
            order.append(data["entity_id"].replace("cover.", ""))

        hass.services.async_call = AsyncMock(side_effect=record)

        async def main():
            with zone_a._clock(0):
                task_a = asyncio.ensure_future(zone_a.coord._async_update_data())
                task_b = asyncio.ensure_future(zone_b.coord._async_update_data())
                await asyncio.sleep(0.05)
                zone_a.set_rain(True)                         # rain reported while A/B are queued
                task_x = asyncio.ensure_future(zone_x.coord._async_update_data())
                await asyncio.gather(task_a, task_b, task_x)

        asyncio.run(main())
        assert order == ["a1", "b1", "a2", "b2", "x1"]
        assert order.index("x1") > order.index("b1")           # waited behind queued intents
        assert order.count("x1") == 1                          # and was sent exactly once

    def test_wind_storm_have_the_same_queue_behaviour(self):
        results = {}
        for trigger in ("rain", "storm"):
            hass = _make_hass()
            serial = GlobalSerialDispatch(min_interval=timedelta(milliseconds=400))
            zone_a = _Scenario(ROLLER, hass=hass, serial=serial, window_id="a1", zone_id="za",
                               covers=["cover.a1"], extra=[_Win("a2", ROLLER, ["cover.a2"])])
            zone_x = _Scenario(AWNING, hass=hass, serial=serial, window_id="x1", zone_id="zx",
                               covers=["cover.x1"])
            order: list[str] = []

            async def record(domain, service, data, *a, _o=order, **k):
                _o.append(data["entity_id"].replace("cover.", ""))

            hass.services.async_call = AsyncMock(side_effect=record)

            async def main():
                with zone_a._clock(0):
                    task_a = asyncio.ensure_future(zone_a.coord._async_update_data())
                    await asyncio.sleep(0.05)
                    _trigger(zone_a, trigger)
                    task_x = asyncio.ensure_future(zone_x.coord._async_update_data())
                    await asyncio.gather(task_a, task_x)

            asyncio.run(main())
            results[trigger] = order
        assert results["rain"] == results["storm"]


# ---------------------------------------------------------------------------
# Audit guards: no stray storm/wind-only safety checks
# ---------------------------------------------------------------------------


class TestSafetyGateAudit:
    def test_tier1_set_matches_the_priority_table(self):
        tier1 = {s for s, rank in STATE_PRIORITY.items() if rank <= 3}
        assert tier1 == set(SAFETY_SHADING_STATES) == {
            ShadingState.STORM_SAFE, ShadingState.WIND_SAFE, ShadingState.RAIN_SAFE,
        }

    @pytest.mark.parametrize("state", _TIER1_STATES)
    def test_state_guard_never_throttles_a_tier1_command(self, state):
        guard = StateGuard()
        now = _NOW
        guard.record_action_sent("w1", now)
        assert guard.can_send_action("w1", state, now + timedelta(seconds=5)) is True

    def test_state_guard_still_throttles_normal_commands(self):
        guard = StateGuard()
        guard.record_action_sent("w1", _NOW)
        assert guard.can_send_action(
            "w1", ShadingState.STRONG_SHADE, _NOW + timedelta(seconds=5)) is False

    @pytest.mark.parametrize("state", _TIER1_STATES)
    def test_leaving_a_tier1_state_is_not_delayed(self, state):
        assert bypasses_guard(state, ShadingState.OPEN) is True

    def test_rain_moves_are_classified_as_safety_movements(self):
        classify = _coord_mod.SmartShadingCoordinator._classify_movement_cause
        for state in _TIER1_STATES:
            assert classify(state) == _coord_mod.MOVE_CAUSE_SAFETY

    def test_only_the_intended_storm_wind_only_checks_remain(self):
        # Any tuple/set/list literal that names STORM_SAFE and WIND_SAFE but not
        # RAIN_SAFE is a place that would treat rain differently from the other
        # Tier-1 states.  Only two such checks are specific to storm/wind on
        # purpose (comments and docstrings are ignored by the AST walk).
        import ast

        root = Path(_coord_mod.__file__).parent
        found = []
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, (ast.Tuple, ast.Set, ast.List)):
                    continue
                names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                names |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                if {"STORM_SAFE", "WIND_SAFE"} <= names and "RAIN_SAFE" not in names:
                    found.append((path.relative_to(root).as_posix(),
                                  lines[node.lineno - 1].strip()))
        allowed = [
            # hardware-aware wind/storm position correction (own block for rain)
            ("coordinator.py",
             "if tier_decision.shading_state in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE):"),
            # rain hold must not override a storm/wind decision (lower priority)
            ("coordinator.py",
             "and tier_decision.shading_state not in (ShadingState.STORM_SAFE, ShadingState.WIND_SAFE)"),
        ]
        assert sorted(found) == sorted(allowed), found


# ---------------------------------------------------------------------------
# Position clamps must not alter a rain-safe (Tier-1 safety) target
# ---------------------------------------------------------------------------


class TestClampsExemptTier1States:
    @pytest.mark.parametrize("state", _TIER1_STATES)
    def test_daytime_minimum_open_does_not_raise_a_safety_target(self, state):
        target, applied, original = apply_daytime_min_open(
            target_position_ha=0, daytime_min_ha=10, new_state=state,
        )
        assert (target, applied, original) == (0, False, None)

    def test_daytime_minimum_open_still_applies_to_normal_shading(self):
        target, applied, _ = apply_daytime_min_open(
            target_position_ha=0, daytime_min_ha=10, new_state=ShadingState.STRONG_SHADE,
        )
        assert (target, applied) == (10, True)

    @pytest.mark.parametrize("state", _TIER1_STATES)
    def test_anti_heat_buildup_does_not_raise_a_safety_target(self, state):
        target, applied, original = apply_anti_heat_buildup(
            target_position_ha=5, ahb_position_ha=10, enabled=True,
            hardware_type=ROLLER, new_state=state,
            in_solar_sector=True, effective_exposure_wm2=600.0,
            allow_during_absence=False,
        )
        assert (target, applied, original) == (5, False, None)

    def test_anti_heat_buildup_still_applies_to_normal_shading(self):
        target, applied, _ = apply_anti_heat_buildup(
            target_position_ha=5, ahb_position_ha=10, enabled=True,
            hardware_type=ROLLER, new_state=ShadingState.STRONG_SHADE,
            in_solar_sector=True, effective_exposure_wm2=600.0,
            allow_during_absence=False,
        )
        assert (target, applied) == (10, True)


# ---------------------------------------------------------------------------
# RainEvaluator fallback when no safe position was resolved
# ---------------------------------------------------------------------------


class TestRainEvaluatorFallback:
    def test_unresolved_safe_position_falls_back_to_retracted(self):
        wdi = build_window_decision_input(
            window=WindowConfig(
                id="w1", name="w1", zone_id="z1", azimuth=180.0, floor_level=0,
                cover_group_id="cg1",
            ),
            zone=ZoneConfig(id="z1", name="z1"),
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None,
            indoor_temp_c=None,
            exposure=None,
            is_in_solar_sector=False,
            rain_status=RainStatus.RAINING,
            rain_protection_enabled=True,
            rain_safe_position=None,
            rain_release_delay_min=30,
        )
        decision = RainEvaluator().evaluate(wdi)
        assert decision is not None
        assert to_ha_position(decision.target_position) == 0


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _coordinator_like(groups: dict[str, CoverHardwareType | None], windows: dict[str, str]):
    return SimpleNamespace(
        cover_groups={gid: SimpleNamespace(hardware_type=hw) for gid, hw in groups.items()},
        windows={wid: SimpleNamespace(cover_group_id=gid) for wid, gid in windows.items()},
    )


class TestSupportExportHardwareType:
    @pytest.mark.parametrize("hw", list(CoverHardwareType))
    def test_hardware_type_comes_from_the_cover_group(self, hw):
        c = _coordinator_like({"cg": hw}, {"w": "cg"})
        assert _hardware_type_name(c, c.windows["w"]) == hw.name

    def test_each_window_maps_to_its_own_group(self):
        c = _coordinator_like(
            {"cg1": AWNING, "cg2": SCREEN, "cg3": GENERIC},
            {"w1": "cg1", "w2": "cg2", "w3": "cg3", "w4": "cg1"},
        )
        got = {wid: _hardware_type_name(c, w) for wid, w in c.windows.items()}
        assert got == {"w1": "AWNING", "w2": "EXTERIOR_SCREEN",
                       "w3": "GENERIC", "w4": "AWNING"}

    def test_missing_assignment_is_reported_as_unknown_not_invented(self):
        c = _coordinator_like({"cg": AWNING}, {"w": "other_group"})
        assert _hardware_type_name(c, c.windows["w"]) is None
        assert _hardware_type_name(c, None) is None
        no_type = _coordinator_like({"cg": None}, {"w": "cg"})
        assert _hardware_type_name(no_type, no_type.windows["w"]) is None

    def test_full_export_reports_the_real_hardware_type_and_stays_private(self):
        s = _Scenario(AWNING, extra=[_Win("w2", SCREEN, ["cover.screen"])])
        s.set_rain(True)
        s.coord.data = s.cycle()          # what DataUpdateCoordinator stores after a refresh
        with s._clock(0):
            export = build_support_export_v3(s.coord, now=_NOW)
        pz = Pseudonymizer(s.coord.config_entry.entry_id)
        snap = export["current_snapshot"]
        assert snap[pz.ref(NS_WINDOW, "w1")]["hardware_type"] == "AWNING"
        assert snap[pz.ref(NS_WINDOW, "w2")]["hardware_type"] == "EXTERIOR_SCREEN"
        assert snap[pz.ref(NS_WINDOW, "w1")]["is_safety"] is True
        assert privacy_scan(export) == []
        assert export["section_errors"] == {}


class TestNextActionAndReason:
    DEFAULTS = ShadePositionDefaults()

    @pytest.mark.parametrize("state", _TIER1_STATES)
    def test_safety_label_reports_the_real_target(self, state):
        assert build_next_action(state, ShadingState.OPEN, self.DEFAULTS,
                                 target_position_ha=100) == "MOVE_TO_100"
        assert build_next_action(state, ShadingState.OPEN, self.DEFAULTS,
                                 target_position_ha=30) == "MOVE_TO_30"

    def test_without_a_target_the_old_constant_label_is_kept(self):
        assert build_next_action(
            ShadingState.RAIN_SAFE, ShadingState.OPEN, self.DEFAULTS) == "MOVE_TO_0"

    def test_other_states_ignore_the_supplied_target(self):
        assert build_next_action(
            ShadingState.STRONG_SHADE, ShadingState.OPEN, self.DEFAULTS,
            target_position_ha=99) == f"MOVE_TO_{self.DEFAULTS.strong_shade_position}"

    def test_no_action_when_the_state_does_not_change(self):
        assert build_next_action(
            ShadingState.RAIN_SAFE, ShadingState.RAIN_SAFE, self.DEFAULTS,
            target_position_ha=0) == "NO_ACTION"

    @pytest.mark.parametrize("kwargs,expected", [
        (dict(hw=AWNING), "MOVE_TO_0"),
        (dict(hw=SCREEN), "MOVE_TO_100"),
        (dict(hw=AWNING, rain_safe_position_ha=30), "MOVE_TO_30"),
        (dict(hw=ROLLER, rain_protection_enabled=True), "MOVE_TO_100"),
    ])
    def test_coordinator_label_matches_the_dispatched_target(self, kwargs, expected):
        hw = kwargs.pop("hw")
        s = _Scenario(hw, **kwargs)
        s.set_rain(True)
        data = s.cycle()
        assert data.window_results["w1"].next_action == expected
        assert f"MOVE_TO_{s.diag(data).target_position_ha}" == expected
        assert s.position_calls == [int(expected.rsplit("_", 1)[1])]

    def test_rain_reason_is_reported_as_rain(self):
        assert build_reason(ShadingState.RAIN_SAFE) == ("RAIN", "rain_detected")
        s = _Scenario(AWNING)
        s.set_rain(True)
        data = s.cycle()
        assert data.window_results["w1"].reason == "RAIN"
        assert data.window_results["w1"].reason_code == "rain_detected"


# ---------------------------------------------------------------------------
# Stored configuration needs no migration
# ---------------------------------------------------------------------------


class TestStoredConfigurationUnchanged:
    def test_rain_fields_and_hardware_type_survive_a_storage_roundtrip(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=False,
            zones=[ZoneConfig(id="z1", name="z1")],
            windows=[
                WindowConfig(id="w1", name="w1", zone_id="z1", azimuth=180.0,
                             floor_level=0, cover_group_id="cg1"),
                WindowConfig(id="w2", name="w2", zone_id="z1", azimuth=90.0,
                             floor_level=0, cover_group_id="cg2",
                             rain_protection_enabled=True, rain_safe_position_ha=30),
            ],
            cover_groups=[
                CoverGroup(id="cg1", window_id="w1", cover_ids=["cover.a"], hardware_type=AWNING),
                CoverGroup(id="cg2", window_id="w2", cover_ids=["cover.b"], hardware_type=SCREEN),
            ],
        )
        stored = to_storage_dict(data)
        restored = from_storage_dict(stored)
        assert {w.id: (w.rain_protection_enabled, w.rain_safe_position_ha)
                for w in restored.windows} == {"w1": (None, None), "w2": (True, 30)}
        assert {g.id: g.hardware_type for g in restored.cover_groups} == {
            "cg1": AWNING, "cg2": SCREEN}

    def test_entries_stored_without_rain_fields_still_load(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=False,
            zones=[ZoneConfig(id="z1", name="z1")],
            windows=[WindowConfig(id="w1", name="w1", zone_id="z1", azimuth=180.0,
                                  floor_level=0, cover_group_id="cg1")],
            cover_groups=[CoverGroup(id="cg1", window_id="w1", cover_ids=["cover.a"])],
        )
        stored = to_storage_dict(data)
        for window in stored["windows"]:
            for key in ("rain_protection_enabled", "rain_safe_position_ha",
                        "rain_release_delay_min"):
                window.pop(key, None)                     # as written by older versions
        for group in stored["cover_groups"]:
            group.pop("hardware_type", None)
        restored = from_storage_dict(stored)
        assert restored.windows[0].rain_safe_position_ha is None
        assert restored.cover_groups[0].hardware_type is GENERIC
