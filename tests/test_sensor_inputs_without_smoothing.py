"""Sensor inputs reach the evaluators exactly as read - no smoothing layer.

The optional sensor smoothing (a v1.2.0-beta.1 option, off by default) was
removed. These tests pin what is left: every weather/indoor value the
coordinator reads is handed on unchanged in every cycle, the solar reading is
still judged by the existing source classifier, a stored entry that still
carries the old keys loads and has no runtime effect, and neither the setup
nor the options flow offers the option any more.

  SIU-01  Outdoor, indoor, solar, cloud and wind values follow the sensor
          exactly from one cycle to the next (no damping, no memory).
  SIU-02  An invalid sample is reported as missing, not replaced by an
          earlier value.
  SIU-03  An implausible, stale or lux-unit solar reading still reaches the
          source classifier unchanged and is still rejected there.
  SIU-04  The coordinator has no smoothing parameters, state or methods.
  SIU-05  A stored entry with the old keys loads; the keys have no effect and
          are not written back.
  SIU-06  Neither flow offers the option, and any options save retires the
          old stored keys.
  SIU-07  No smoothing code, option or text remains in the integration.
"""
from __future__ import annotations

import re
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
# SIU-01 / SIU-02 - values follow the sensor
# ---------------------------------------------------------------------------

class TestValuesFollowTheSensor:
    def test_every_channel_is_handed_on_unchanged_across_cycles(self):
        coord = _make_coord()
        _set_states(coord, outdoor="20.0", solar="500", wind="2.0", indoor="21.0")
        first = coord._read_weather_inputs()
        first_indoor = coord._read_indoor_temperature()
        _set_states(coord, outdoor="30.0", solar="900", wind="9.0", indoor="26.0")
        second = coord._read_weather_inputs()
        second_indoor = coord._read_indoor_temperature()
        assert (first.outdoor_temperature, second.outdoor_temperature) == (20.0, 30.0)
        assert (first.solar_radiation, second.solar_radiation) == (500.0, 900.0)
        assert (first.wind_speed, second.wind_speed) == (2.0, 9.0)
        assert (first_indoor, second_indoor) == (21.0, 26.0)

    def test_a_long_series_never_lags_behind_the_sensor(self):
        coord = _make_coord()
        for sample in (10.0, 15.0, 12.0, 20.0, 18.0, 5.0):
            _set_states(coord, outdoor=str(sample), indoor=str(sample + 1.0))
            assert coord._read_weather_inputs().outdoor_temperature == sample
            assert coord._read_indoor_temperature() == sample + 1.0

    def test_cloud_cover_is_read_as_reported(self):
        coord = SmartShadingCoordinator(
            _make_hass(), _make_entry(), cloud_cover_sensor_id="sensor.cloud")
        for value in (10.0, 90.0, 40.0):
            coord.hass.states.get = MagicMock(
                side_effect=lambda eid, v=value: _make_state(str(v)) if eid == "sensor.cloud" else None)
            assert coord._read_weather_inputs().cloud_cover == value

    def test_an_invalid_sample_is_missing_not_replaced_by_an_earlier_value(self):
        coord = _make_coord()
        _set_states(coord, outdoor="10.0", indoor="21.0")
        assert coord._read_weather_inputs().outdoor_temperature == 10.0
        assert coord._read_indoor_temperature() == 21.0
        _set_states(coord, outdoor=None, indoor=None)
        assert coord._read_weather_inputs().outdoor_temperature is None
        assert coord._read_indoor_temperature() is None

    def test_without_indoor_sensors_there_is_no_indoor_value(self):
        coord = SmartShadingCoordinator(_make_hass(), _make_entry())
        assert coord._read_indoor_temperature() is None

    def test_a_restart_changes_nothing_because_there_is_no_state(self):
        for _ in range(2):
            coord = _make_coord()
            _set_states(coord, outdoor="50.0")
            assert coord._read_weather_inputs().outdoor_temperature == 50.0


# ---------------------------------------------------------------------------
# SIU-03 - the solar reading is still judged by the existing classifier
# ---------------------------------------------------------------------------

class TestSolarValidationIsUnchanged:
    def _classify(self, wi):
        from custom_components.smartshading.engines.solar_source import (
            SOURCE_MEASURED,
            classify_solar_source,
        )
        result = classify_solar_source(
            sensor_configured=True,
            measured_wm2=wi.solar_radiation,
            measured_age_s=wi.solar_radiation_age_s,
            estimated_wm2=300.0,
            cloud_cover_pct=None,
            measured_unit=wi.solar_radiation_unit,
        )
        return result, SOURCE_MEASURED

    def test_a_plausible_fresh_reading_is_the_measured_source(self):
        coord = _make_coord()
        _set_solar_reading(coord, "640", unit="W/m²")
        wi = coord._read_weather_inputs()
        result, measured = self._classify(wi)
        assert wi.solar_radiation == 640.0
        assert result.source == measured and result.effective_radiation_wm2 == 640.0

    def test_an_out_of_range_reading_reaches_the_classifier_raw_and_is_rejected(self):
        coord = _make_coord()
        _set_solar_reading(coord, "50000", unit="W/m²")
        wi = coord._read_weather_inputs()
        result, measured = self._classify(wi)
        assert wi.solar_radiation == 50000.0
        assert result.source != measured and result.effective_radiation_wm2 == 300.0

    def test_a_stale_reading_keeps_its_age_and_is_rejected(self):
        coord = _make_coord()
        _set_solar_reading(coord, "640", unit="W/m²", stale_seconds=24 * 3600)
        wi = coord._read_weather_inputs()
        result, measured = self._classify(wi)
        assert wi.solar_radiation == 640.0 and wi.solar_radiation_age_s > 3600
        assert result.source != measured

    def test_a_lux_reading_keeps_its_unit_and_is_rejected(self):
        coord = _make_coord()
        _set_solar_reading(coord, "640", unit="lx")
        wi = coord._read_weather_inputs()
        result, measured = self._classify(wi)
        assert wi.solar_radiation_unit == "lx"
        assert result.source != measured


# ---------------------------------------------------------------------------
# SIU-04 - no smoothing parameters, state or methods on the coordinator
# ---------------------------------------------------------------------------

class TestNoSmoothingWiringRemains:
    def test_constructor_has_no_smoothing_parameter(self):
        import inspect
        names = set(inspect.signature(SmartShadingCoordinator.__init__).parameters)
        assert not {n for n in names if "ema" in n.split("_")}

    def test_coordinator_has_no_smoothing_state_or_method(self):
        coord = _make_coord()
        leftovers = [n for n in dir(coord) if "ema" in n.lower().replace("schema", "").split("_")]
        assert leftovers == []

    def test_old_constructor_arguments_are_rejected_not_silently_accepted(self):
        with pytest.raises(TypeError):
            _make_coord(ema_enabled=True, ema_alpha=0.4)


# ---------------------------------------------------------------------------
# SIU-05 - existing entries that still carry the old keys
# ---------------------------------------------------------------------------

class TestStoredEntriesWithOldKeys:
    def _raw(self, **extra):
        from custom_components.smartshading.config_entry_data import (
            SmartShadingConfigEntryData,
            to_storage_dict,
        )
        base = to_storage_dict(SmartShadingConfigEntryData(name="Zone A", use_home_location=True))
        base.update(extra)
        return base

    def test_an_entry_with_old_keys_loads(self):
        from custom_components.smartshading.config_entry_data import from_storage_dict
        data = from_storage_dict(self._raw(ema_enabled=True, ema_alpha=0.4))
        assert data.name == "Zone A"
        assert not hasattr(data, "ema_enabled") and not hasattr(data, "ema_alpha")

    @pytest.mark.parametrize("enabled, alpha", [(True, 0.05), (True, 1.0), (False, 0.3), ("yes", "x"), (None, None)])
    def test_whatever_the_old_values_were_they_are_ignored(self, enabled, alpha):
        from custom_components.smartshading.config_entry_data import from_storage_dict
        with_keys = from_storage_dict(self._raw(ema_enabled=enabled, ema_alpha=alpha))
        without = from_storage_dict(self._raw())
        assert with_keys == without

    def test_the_old_keys_are_not_written_back(self):
        from custom_components.smartshading.config_entry_data import (
            from_storage_dict,
            to_storage_dict,
        )
        stored = to_storage_dict(from_storage_dict(self._raw(ema_enabled=True, ema_alpha=0.4)))
        assert "ema_enabled" not in stored and "ema_alpha" not in stored

    def test_old_stored_values_do_not_smooth_anything(self):
        # the only way left to reach the coordinator is the entry data, and it
        # carries nothing: values stay raw whatever the stored entry said
        from custom_components.smartshading.config_entry_data import from_storage_dict
        from_storage_dict(self._raw(ema_enabled=True, ema_alpha=0.05))
        coord = _make_coord()
        _set_states(coord, outdoor="10.0")
        coord._read_weather_inputs()
        _set_states(coord, outdoor="40.0")
        assert coord._read_weather_inputs().outdoor_temperature == 40.0


# ---------------------------------------------------------------------------
# SIU-07 - nothing of the feature is left
# ---------------------------------------------------------------------------

class TestNothingOfTheFeatureRemains:
    _PATTERN = re.compile(
        r"\bEMA\b|\bema_(enabled|alpha|update|engine)|EmaSmoother|ema_engine|_apply_ema|"
        r"CONF_EMA|DEFAULT_EMA|EMA_ALPHA|ema_eligible")
    # config_flow.py names the old keys only to retire them from a stored entry;
    # the thermal engine's docstring says in passing that it uses medians, not a moving average
    _ALLOWED = {"config_flow.py", "thermal_response_engine.py"}

    def _root(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"

    def test_the_engine_module_is_gone(self):
        assert not (self._root() / "engines" / "ema_engine.py").exists()

    def test_no_python_module_mentions_the_feature(self):
        offenders = []
        for path in sorted(self._root().rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self._PATTERN.search(line) and path.name not in self._ALLOWED:
                    offenders.append(f"{path.name}:{number}")
        assert offenders == []

    def test_no_translation_or_string_file_offers_the_option(self):
        offenders = [
            p.name for p in sorted((self._root() / "translations").glob("*.json")) + [self._root() / "strings.json"]
            if "ema_enabled" in p.read_text(encoding="utf-8") or "ema_alpha" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []
