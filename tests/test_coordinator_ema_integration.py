"""EMA wiring at the Coordinator level — v1.2.0-beta.1, Beta.1-T4.

Covers what tests/test_ema_engine.py (pure engine tests) cannot: the actual
insertion point inside SmartShadingCoordinator._read_weather_inputs() /
_read_indoor_temperature(), disabled-by-default backward compatibility,
per-channel independence at the coordinator level, no double-smoothing
across repeated cycles, restart-without-persistence, and confirmation that
the exact fields WindowDecisionInput consumes (weather_inputs.
outdoor_temperature / indoor_temperature, both smoothed; weather_inputs.
wind_speed / .wind_gust, deliberately raw/unsmoothed) are the ones this
module's tests exercise — see coordinator.py build_window_decision_input()
call site (outdoor_temp_c=weather_inputs.outdoor_temperature, indoor_temp_c=
indoor_temperature, wind_speed_ms=weather_inputs.wind_speed, wind_gust_ms=
weather_inputs.wind_gust), so no separate full _async_update_data() run is
needed to prove propagation.

Post pre-push-review correction (see engines/ema_engine.py module docstring
"Wind is excluded" / "Solar radiation is gated..."): wind_speed/wind_gust
are excluded from EMA entirely (Tier-1 safety path), and solar_radiation is
gated by _solar_reading_ema_eligible() (range/staleness/unit-family, reusing
solar_source.py's own public thresholds) BEFORE it may update the EMA state,
so an implausible or stale raw reading can never corrupt the running solar
average — it is passed through unsmoothed instead, exactly like pre-T4.

Coverage:
  CEI-01  EMA disabled (default) -> raw values pass through unchanged across
          multiple cycles (full backward compatibility).
  CEI-02  EMA enabled -> outdoor_temperature blends via alpha exactly like
          ema_update(), cross-checked against the pure function.
  CEI-03  Multiple channels (outdoor_temperature, solar_radiation) are
          independent at the coordinator level; wind is never smoothed
          regardless of what other channels do.
  CEI-04  No double-smoothing: N repeated cycles match a single ema_update()
          chain applied once per cycle, not twice.
  CEI-05  Restart without persistence: a fresh Coordinator instance's first
          reading is the raw value, not carried over from a prior instance.
  CEI-06  indoor_temperature (the cross-sensor average) is smoothed too.
  CEI-07  solar_radiation_age_s / solar_radiation_unit / weather_condition /
          weather_condition_enum are NEVER smoothed (metadata/categorical).
  CEI-08  Invalid samples (unavailable/unknown/None) never destroy the
          running EMA at the coordinator level either.
  CEI-09  WindowDecisionInput field values match what _read_weather_inputs()/
          _read_indoor_temperature() return — the exact fields flow through
          (smoothed for outdoor/indoor, raw for wind).
  CEI-10  Regression: EMA-disabled coordinator behavior is identical to a
          coordinator built with no ema_enabled/ema_alpha args at all
          (defaults match), proving zero behavior change for every existing
          config that predates T4.
  CEI-11  Out-of-range solar reading never corrupts the EMA state; the
          RAW (not stale-EMA) value is what classify_solar_source() would
          see, so it still correctly rejects/falls back this cycle.
  CEI-12  Stale (numerically valid) solar reading never updates the EMA.
  CEI-13  A later valid solar reading resumes blending from the last GOOD
          EMA value, not from an outlier or a stale-gap value.
  CEI-14  Solar unit-family mismatch (lux) is gated the same as range/staleness.
  CEI-15  NaN/Infinity from any channel never corrupts that channel's state.
  CEI-16  Wind protection is never delayed by EMA: a genuine gust spike is
          visible to WindowDecisionInput on the very next cycle, unsmoothed.
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# HA stubs — must precede any coordinator import in this module (mirrors the
# proven-working pattern in tests/test_v104_presence_fanout.py).
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _CoordBase:
    """Minimal DataUpdateCoordinator stub."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


_HA_STUBS = {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {"SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16}),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub(
        "homeassistant.core",
        HomeAssistant=object,
        Event=object,
        callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub("homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_track_point_in_time=lambda *a, **k: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}
for _name, _mod in _HA_STUBS.items():
    sys.modules.setdefault(_name, _mod)

import datetime as _datetime
# Unconditional (not merely setdefault): conftest.py registers a baseline
# "homeassistant.util.dt" stub with utcnow=_noop (returns None) for test
# files that don't need real timestamps. THIS module's solar-staleness gate
# tests (CEI-12/13) need genuine wall-clock arithmetic — dt_util.utcnow() is
# looked up as an attribute at CALL time inside coordinator.py, so mutating
# the already-imported module object here still takes effect for every
# subsequent call, regardless of import order across the test session.
sys.modules["homeassistant.util.dt"] = _stub(
    "homeassistant.util.dt",
    utcnow=lambda: _datetime.datetime.now(_datetime.timezone.utc),
    now=lambda: _datetime.datetime.now(_datetime.timezone.utc),
    as_utc=lambda dt: dt.astimezone(_datetime.timezone.utc),
    as_local=lambda dt: dt,
    DEFAULT_TIME_ZONE=_datetime.timezone.utc,
)

sys.modules["homeassistant.helpers.update_coordinator"] = _stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_CoordBase,
    CoordinatorEntity=type("CE", (), {"__class_getitem__": classmethod(lambda cls, x: cls), "__init__": lambda self, c: None}),
)
sys.modules["homeassistant.helpers.storage"] = _stub("homeassistant.helpers.storage", Store=_StoreStub)

sys.modules.pop("custom_components.smartshading.coordinator", None)
from custom_components.smartshading.coordinator import SmartShadingCoordinator  # noqa: E402
from custom_components.smartshading.engines.ema_engine import ema_update  # noqa: E402


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    entry.data = {}
    entry.async_on_unload = MagicMock()
    return entry


def _make_state(value: Any, unit: str | None = None, last_updated=None) -> MagicMock:
    state = MagicMock()
    state.state = value
    state.attributes = {"unit_of_measurement": unit} if unit else {}
    state.last_updated = last_updated
    return state


def _set_solar_reading(
    coord: SmartShadingCoordinator,
    value: Any,
    *,
    unit: str | None = None,
    stale_seconds: float | None = None,
) -> None:
    """Dedicated helper for the solar-gate tests: lets a test simulate a
    stale reading via an actual `last_updated` timestamp far enough in the
    past that solar_age_s (computed in _read_weather_inputs() from
    dt_util.utcnow() - state.last_updated) exceeds SOLAR_SENSOR_MAX_AGE_S."""
    from datetime import datetime, timedelta, timezone

    last_updated = (
        datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
        if stale_seconds is not None
        else None
    )
    state = _make_state(value, unit=unit, last_updated=last_updated)
    coord.hass.states.get = MagicMock(side_effect=lambda eid: state if eid == "sensor.solar" else None)


def _make_coord(**kwargs) -> SmartShadingCoordinator:
    hass = _make_hass()
    entry = _make_entry()
    return SmartShadingCoordinator(
        hass, entry,
        outdoor_temperature_sensor_id="sensor.outdoor_temp",
        solar_radiation_sensor_id="sensor.solar",
        wind_speed_sensor_id="sensor.wind",
        indoor_temperature_sensor_ids=["sensor.indoor"],
        **kwargs,
    )


def _set_states(coord: SmartShadingCoordinator, *, outdoor=None, solar=None, wind=None, indoor=None) -> None:
    def _get(entity_id: str):
        mapping = {
            "sensor.outdoor_temp": outdoor,
            "sensor.solar": solar,
            "sensor.wind": wind,
            "sensor.indoor": indoor,
        }
        raw = mapping.get(entity_id)
        return _make_state(raw) if raw is not None else None

    coord.hass.states.get = MagicMock(side_effect=_get)


# ---------------------------------------------------------------------------
# CEI-01 — EMA disabled (default): raw values pass through unchanged.
# ---------------------------------------------------------------------------

class TestDisabledByDefault:
    def test_raw_values_pass_through_unchanged_across_cycles(self):
        coord = _make_coord()  # ema_enabled defaults to False
        _set_states(coord, outdoor="20.0", solar="500")
        first = coord._read_weather_inputs()
        _set_states(coord, outdoor="30.0", solar="900")
        second = coord._read_weather_inputs()
        assert first.outdoor_temperature == 20.0
        assert second.outdoor_temperature == 30.0  # no damping — exact pass-through
        assert first.solar_radiation == 500.0
        assert second.solar_radiation == 900.0


# ---------------------------------------------------------------------------
# CEI-02 — EMA enabled: blends via alpha, cross-checked against ema_update().
# ---------------------------------------------------------------------------

class TestEnabledBlending:
    def test_outdoor_temperature_blends_like_pure_ema_update(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.4)
        _set_states(coord, outdoor="20.0")
        first = coord._read_weather_inputs().outdoor_temperature
        _set_states(coord, outdoor="30.0")
        second = coord._read_weather_inputs().outdoor_temperature
        expected_first = ema_update(None, 20.0, 0.4)
        expected_second = ema_update(expected_first, 30.0, 0.4)
        assert first == expected_first == 20.0
        assert second == expected_second


# ---------------------------------------------------------------------------
# CEI-03 — independent channels at the coordinator level.
# ---------------------------------------------------------------------------

class TestIndependentChannels:
    def test_outdoor_and_solar_are_independent(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.5)
        _set_states(coord, outdoor="10.0", solar="100")
        coord._read_weather_inputs()
        # Only outdoor goes invalid on cycle 2 — solar must be unaffected.
        _set_states(coord, outdoor=None, solar="200")
        wi = coord._read_weather_inputs()
        assert wi.outdoor_temperature == 10.0  # unchanged — invalid sample ignored
        assert wi.solar_radiation == ema_update(100.0, 200.0, 0.5)

    def test_wind_is_never_smoothed_regardless_of_other_channels(self):
        """wind_speed/wind_gust are excluded from EMA entirely (T4 pre-push
        review correction — see engines/ema_engine.py "Wind is excluded")."""
        coord = _make_coord(ema_enabled=True, ema_alpha=0.5)
        _set_states(coord, outdoor="10.0", solar="100", wind="2.0")
        coord._read_weather_inputs()
        _set_states(coord, outdoor="20.0", solar="200", wind="9.0")
        wi = coord._read_weather_inputs()
        assert wi.wind_speed == 9.0  # raw pass-through, not blended toward 2.0


# ---------------------------------------------------------------------------
# CEI-04 — no double smoothing across repeated cycles.
# ---------------------------------------------------------------------------

class TestNoDoubleSmoothing:
    def test_five_cycles_match_single_application_reference(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.3)
        samples = [10.0, 15.0, 12.0, 20.0, 18.0]
        reference: float | None = None
        actual: float | None = None
        for sample in samples:
            _set_states(coord, outdoor=str(sample))
            actual = coord._read_weather_inputs().outdoor_temperature
            reference = ema_update(reference, sample, 0.3)
        assert actual == reference  # would diverge if smoothed twice per cycle


# ---------------------------------------------------------------------------
# CEI-05 — restart without persistence.
# ---------------------------------------------------------------------------

class TestRestartWithoutPersistence:
    def test_fresh_instance_does_not_inherit_prior_ema_state(self):
        coord1 = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_states(coord1, outdoor="10.0")
        coord1._read_weather_inputs()
        _set_states(coord1, outdoor="50.0")
        coord1._read_weather_inputs()  # coord1's EMA is now damped, well below 50

        # Simulate an HA restart: a brand-new Coordinator instance.
        coord2 = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_states(coord2, outdoor="50.0")
        first_reading_after_restart = coord2._read_weather_inputs().outdoor_temperature
        assert first_reading_after_restart == 50.0  # seeded fresh, not resumed


# ---------------------------------------------------------------------------
# CEI-06 — indoor_temperature is smoothed too.
# ---------------------------------------------------------------------------

class TestIndoorTemperatureSmoothed:
    def test_indoor_temperature_blends_via_alpha(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.5)
        _set_states(coord, indoor="21.0")
        first = coord._read_indoor_temperature()
        _set_states(coord, indoor="25.0")
        second = coord._read_indoor_temperature()
        assert first == 21.0
        assert second == ema_update(21.0, 25.0, 0.5)

    def test_indoor_temperature_disabled_passes_through(self):
        coord = _make_coord()  # disabled
        _set_states(coord, indoor="21.0")
        first = coord._read_indoor_temperature()
        _set_states(coord, indoor="25.0")
        second = coord._read_indoor_temperature()
        assert first == 21.0
        assert second == 25.0


# ---------------------------------------------------------------------------
# CEI-07 — metadata/categorical fields are never smoothed.
# ---------------------------------------------------------------------------

class TestMetadataNeverSmoothed:
    def test_solar_metadata_and_weather_condition_untouched(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.5)
        _set_states(coord, solar="500")
        wi = coord._read_weather_inputs()
        # solar_radiation_age_s is timestamp-derived (None here, no last_updated
        # set on the mock) and solar_radiation_unit/weather_condition are
        # string/enum fields — none of these run through EMA at all.
        assert wi.solar_radiation_age_s is None
        assert wi.solar_radiation_unit is None
        assert wi.weather_condition is None
        assert wi.weather_condition_enum is None


# ---------------------------------------------------------------------------
# CEI-08 — invalid samples never destroy state at the coordinator level.
# ---------------------------------------------------------------------------

class TestInvalidSamplesAtCoordinatorLevel:
    def test_unavailable_then_valid_resumes_correctly(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.4)
        _set_states(coord, outdoor="20.0")
        first = coord._read_weather_inputs().outdoor_temperature
        # Sensor goes unavailable: state text "unavailable" -> parse_numeric_state -> None.
        coord.hass.states.get = MagicMock(
            side_effect=lambda eid: _make_state("unavailable") if eid == "sensor.outdoor_temp" else None
        )
        during_outage = coord._read_weather_inputs().outdoor_temperature
        assert during_outage == first  # unchanged, not destroyed
        _set_states(coord, outdoor="22.0")
        after_outage = coord._read_weather_inputs().outdoor_temperature
        assert after_outage == ema_update(first, 22.0, 0.4)


# ---------------------------------------------------------------------------
# CEI-09 — WindowDecisionInput consumes exactly these smoothed fields.
# ---------------------------------------------------------------------------

class TestWindowDecisionInputFieldsMatch:
    def test_smoothed_fields_are_the_ones_wdi_consumes(self):
        """coordinator.py's build_window_decision_input() call site passes
        outdoor_temp_c=weather_inputs.outdoor_temperature, indoor_temp_c=
        indoor_temperature, wind_speed_ms=weather_inputs.wind_speed,
        wind_gust_ms=weather_inputs.wind_gust verbatim — this test proves
        those exact attributes carry the EMA'd value, which is sufficient to
        prove propagation without exercising the full multi-thousand-line
        _async_update_data() cycle."""
        coord = _make_coord(ema_enabled=True, ema_alpha=0.5)
        _set_states(coord, outdoor="18.0", wind="3.0", indoor="21.0")
        wi = coord._read_weather_inputs()
        indoor = coord._read_indoor_temperature()
        _set_states(coord, outdoor="24.0", wind="7.0", indoor="23.0")
        wi2 = coord._read_weather_inputs()
        indoor2 = coord._read_indoor_temperature()
        assert wi2.outdoor_temperature == ema_update(18.0, 24.0, 0.5)
        assert wi2.wind_speed == 7.0  # wind is excluded from EMA — raw pass-through
        assert indoor2 == ema_update(21.0, 23.0, 0.5)


# ---------------------------------------------------------------------------
# CEI-10 — regression: defaults produce identical behavior to no EMA args.
# ---------------------------------------------------------------------------

class TestDefaultArgsRegression:
    def test_no_ema_kwargs_matches_explicit_disabled_defaults(self):
        coord_implicit = _make_coord()
        coord_explicit = _make_coord(ema_enabled=False, ema_alpha=0.3)
        _set_states(coord_implicit, outdoor="19.5")
        _set_states(coord_explicit, outdoor="19.5")
        assert (
            coord_implicit._read_weather_inputs().outdoor_temperature
            == coord_explicit._read_weather_inputs().outdoor_temperature
            == 19.5
        )


# ---------------------------------------------------------------------------
# CEI-11 — out-of-range solar reading never corrupts the EMA state.
# ---------------------------------------------------------------------------

class TestSolarOutOfRangeGate:
    def test_extreme_spike_does_not_corrupt_ema_and_raw_flows_through(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_solar_reading(coord, "300")
        first = coord._read_weather_inputs().solar_radiation
        assert first == 300.0

        # 50000 W/m² is far above MAX_PLAUSIBLE_SOLAR_WM2 (1500.0).
        _set_solar_reading(coord, "50000")
        during_spike = coord._read_weather_inputs().solar_radiation
        # The RAW (rejected) value flows through unsmoothed — NOT a damped
        # ~15000 that would still fool classify_solar_source() into thinking
        # the EMA state is a plausible live reading, and NOT the stale-good
        # 300 either (that would hide from classify_solar_source() that
        # THIS cycle's real sensor value was implausible).
        assert during_spike == 50000.0

        # A subsequent normal reading must resume blending from the last
        # GOOD value (300), proving the EMA's internal state was untouched
        # by the spike, not from something derived off 50000.
        _set_solar_reading(coord, "320")
        after_spike = coord._read_weather_inputs().solar_radiation
        assert after_spike == ema_update(300.0, 320.0, 0.3)

    def test_negative_reading_is_also_rejected(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_solar_reading(coord, "300")
        coord._read_weather_inputs()
        _set_solar_reading(coord, "-50")
        during_negative = coord._read_weather_inputs().solar_radiation
        assert during_negative == -50.0  # raw pass-through, EMA state untouched
        _set_solar_reading(coord, "310")
        after = coord._read_weather_inputs().solar_radiation
        assert after == ema_update(300.0, 310.0, 0.3)


# ---------------------------------------------------------------------------
# CEI-12 — stale (numerically valid) solar reading never updates the EMA.
# ---------------------------------------------------------------------------

class TestSolarStaleGate:
    def test_stale_reading_does_not_update_ema(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_solar_reading(coord, "300")
        coord._read_weather_inputs()
        # A numerically plausible value (400), but stale (last_updated 2000s
        # ago > SOLAR_SENSOR_MAX_AGE_S=1800s).
        _set_solar_reading(coord, "400", stale_seconds=2000.0)
        during_stale = coord._read_weather_inputs().solar_radiation
        assert during_stale == 400.0  # raw pass-through for classify_solar_source()
        # A fresh valid reading afterward resumes from the last GOOD (300),
        # not from the stale 400.
        _set_solar_reading(coord, "310")
        after = coord._read_weather_inputs().solar_radiation
        assert after == ema_update(300.0, 310.0, 0.3)

    def test_fresh_reading_within_threshold_still_updates_ema(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_solar_reading(coord, "300")
        coord._read_weather_inputs()
        _set_solar_reading(coord, "310", stale_seconds=60.0)  # well under 1800s
        after = coord._read_weather_inputs().solar_radiation
        assert after == ema_update(300.0, 310.0, 0.3)


# ---------------------------------------------------------------------------
# CEI-13 — resumes blending from the last GOOD value after an invalid gap.
# ---------------------------------------------------------------------------

class TestSolarResumesFromLastGoodValue:
    def test_resumes_from_last_good_after_mixed_invalid_gap(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.4)
        _set_solar_reading(coord, "250")
        good = coord._read_weather_inputs().solar_radiation
        assert good == 250.0
        _set_solar_reading(coord, "99999")  # out of range
        coord._read_weather_inputs()
        _set_solar_reading(coord, "260", stale_seconds=5000.0)  # stale
        coord._read_weather_inputs()
        _set_solar_reading(coord, "270")  # valid again
        resumed = coord._read_weather_inputs().solar_radiation
        assert resumed == ema_update(250.0, 270.0, 0.4)


# ---------------------------------------------------------------------------
# CEI-14 — unit-family mismatch (lux) is gated like range/staleness.
# ---------------------------------------------------------------------------

class TestSolarUnitMismatchGate:
    def test_lux_unit_does_not_update_ema(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_solar_reading(coord, "300")
        coord._read_weather_inputs()
        _set_solar_reading(coord, "45000", unit="lx")  # plausible lux, not W/m²
        during_mismatch = coord._read_weather_inputs().solar_radiation
        assert during_mismatch == 45000.0  # raw pass-through, unit rejected for EMA
        _set_solar_reading(coord, "310")
        after = coord._read_weather_inputs().solar_radiation
        assert after == ema_update(300.0, 310.0, 0.3)


# ---------------------------------------------------------------------------
# CEI-15 — NaN/Infinity from any channel never corrupts that channel's state.
# ---------------------------------------------------------------------------

class TestNaNAcrossChannels:
    def test_nan_solar_reading_does_not_corrupt_state(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_solar_reading(coord, "300")
        coord._read_weather_inputs()
        _set_solar_reading(coord, "nan")
        during_nan = coord._read_weather_inputs().solar_radiation
        assert during_nan is None or during_nan != during_nan  # NaN itself, or None — never a poisoned float
        _set_solar_reading(coord, "310")
        after = coord._read_weather_inputs().solar_radiation
        assert after == ema_update(300.0, 310.0, 0.3)

    def test_infinity_outdoor_reading_does_not_corrupt_state(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.3)
        _set_states(coord, outdoor="20.0")
        coord._read_weather_inputs()
        _set_states(coord, outdoor="inf")
        coord._read_weather_inputs()
        _set_states(coord, outdoor="22.0")
        after = coord._read_weather_inputs().outdoor_temperature
        assert after == ema_update(20.0, 22.0, 0.3)


# ---------------------------------------------------------------------------
# CEI-16 — wind protection is never delayed by EMA.
# ---------------------------------------------------------------------------

class TestWindProtectionNeverDelayed:
    def test_gust_spike_is_visible_immediately_unsmoothed(self):
        coord = _make_coord(ema_enabled=True, ema_alpha=0.1)  # heavy smoothing elsewhere
        _set_states(coord, wind="2.0")
        coord._read_weather_inputs()
        _set_states(coord, wind="25.0")  # sudden storm gust
        wi = coord._read_weather_inputs()
        # If wind were smoothed at alpha=0.1 it would show ~4.3, dangerously
        # underrepresenting the gust for a full-strength safety reaction.
        assert wi.wind_speed == 25.0
