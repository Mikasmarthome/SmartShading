"""Schedule (lifecycle) configuration flows: what is shown and what is saved.

These tests pin the stored result of editing the day/night schedule - in the
options flow and in the setup flow - through one set of documented rules
(``_expected_lifecycle_config`` / ``_expected_setup``) and one flat set of
user answers. Only the small driver functions know how the answers are
spread over pages, so the same assertions hold for any page layout.

  CHAR-01  The pages after the trigger selection offer exactly the fields the
           selected triggers call for (and nothing of the advanced schedule).
  CHAR-02  The options flow saves exactly the documented result for every
           combination of night trigger, morning trigger and schedule mode,
           with named and custom sun elevations, with an empty and with a
           fully populated stored schedule; unrelated keys survive.
  CHAR-03  The setup flow collects the same values, and never offers the
           advanced schedule fields.
  PAGE-01  Page sequence: Night / Morning, or each split into Weekdays and
           Weekend; a disabled phase has no page.
  PAGE-02  Conditional fields: time, sun position, position per trigger; a
           custom elevation only on its own follow-up page.
  PAGE-03  One final save after the last page.
  PAGE-04  The Advanced schedule step: reachable from the Advanced menu, its
           own persistence, no stale or lost values.
  PAGE-05  Translations of every page, in strings.json and all languages.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import time
from typing import Any
from unittest.mock import MagicMock

import pytest
import voluptuous as vol

# ---------------------------------------------------------------------------
# HA stubs — identical technique to test_config_flow_manual_override.py.
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _SelectorConfigBase:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class EntitySelectorConfig(_SelectorConfigBase):
    pass


class NumberSelectorConfig(_SelectorConfigBase):
    pass


class SelectSelectorConfig(_SelectorConfigBase):
    pass


class _SelectorBase:
    def __init__(self, config: Any = None) -> None:
        self.config = config

    def __call__(self, value: Any) -> Any:
        return value


class EntitySelector(_SelectorBase):
    pass


class NumberSelector(_SelectorBase):
    pass


class SelectSelector(_SelectorBase):
    pass


class TimeSelector(_SelectorBase):
    pass


class BooleanSelector(_SelectorBase):
    pass


class NumberSelectorMode:
    BOX = "box"
    SLIDER = "slider"


class SelectSelectorMode:
    DROPDOWN = "dropdown"
    LIST = "list"


sys.modules["homeassistant.helpers.selector"] = _stub(
    "homeassistant.helpers.selector",
    EntitySelector=EntitySelector,
    EntitySelectorConfig=EntitySelectorConfig,
    NumberSelector=NumberSelector,
    NumberSelectorConfig=NumberSelectorConfig,
    NumberSelectorMode=NumberSelectorMode,
    SelectSelector=SelectSelector,
    SelectSelectorConfig=SelectSelectorConfig,
    SelectSelectorMode=SelectSelectorMode,
    TimeSelector=TimeSelector,
    BooleanSelector=BooleanSelector,
)


sys.modules.pop("custom_components.smartshading.config_flow", None)
from custom_components.smartshading.config_flow import (  # noqa: E402
    SmartShadingConfigFlow,
    SmartShadingOptionsFlow,
)
from custom_components.smartshading.const import (  # noqa: E402
    DEFAULT_MORNING_FIXED_TIME,
    DEFAULT_MORNING_POSITION,
    DEFAULT_MORNING_SUN_ELEVATION,
    DEFAULT_NIGHT_FIXED_TIME,
    DEFAULT_NIGHT_POSITION,
    DEFAULT_NIGHT_SUN_ELEVATION,
    DEFAULT_WEEKDAY_MORNING_FIXED_TIME,
    DEFAULT_WEEKDAY_NIGHT_FIXED_TIME,
    DEFAULT_WEEKEND_MORNING_FIXED_TIME,
    DEFAULT_WEEKEND_NIGHT_FIXED_TIME,
    MORNING_ELEVATION_PRESETS,
    NIGHT_ELEVATION_PRESETS,
)
from custom_components.smartshading.models.lifecycle import (  # noqa: E402
    LifecycleScheduleMode,
    MorningTrigger,
    NightTrigger,
)

NIGHT_TRIGGERS = list(NightTrigger)
MORNING_TRIGGERS = list(MorningTrigger)
MODES = list(LifecycleScheduleMode)
_TIME_TRIGGERS = ("fixed_time", "both")
_SUN_TRIGGERS = ("sun_elevation", "both")
_OFF = "disabled"


# ---------------------------------------------------------------------------
# What a user can enter - one flat answer set, whatever the page layout is.
# ---------------------------------------------------------------------------

def _visible_basic_fields(night: str, morning: str, mode: str) -> set[str]:
    """Basic schedule fields a user can fill in for these selections."""
    ww = mode == "weekday_weekend"
    fields: set[str] = set()
    for phase, trigger in (("night", night), ("morning", morning)):
        if trigger == _OFF:
            continue
        if trigger in _SUN_TRIGGERS:
            fields |= {f"{phase}_elevation_preset", f"{phase}_sun_elevation"}
        if ww:
            for day in ("weekday", "weekend"):
                fields.add(f"{day}_{phase}_position")
                if trigger in _TIME_TRIGGERS:
                    fields.add(f"{day}_{phase}_fixed_time")
        else:
            fields.add(f"{phase}_position")
            if trigger in _TIME_TRIGGERS:
                fields.add(f"{phase}_fixed_time")
    return fields


def _visible_advanced_fields(night: str, morning: str) -> set[str]:
    fields = {"active_months"}
    for phase, trigger in (("night", night), ("morning", morning)):
        if trigger in _TIME_TRIGGERS:
            fields |= {f"{phase}_sun_event", f"{phase}_not_before", f"{phase}_not_after"}
    return fields


_VALUES_NAMED = {
    "night_fixed_time": "21:15:00", "morning_fixed_time": "05:45:00",
    "weekday_night_fixed_time": "21:45:00", "weekday_morning_fixed_time": "06:10:00",
    "weekend_night_fixed_time": "23:30:00", "weekend_morning_fixed_time": "09:00:00",
    "night_position": 0, "morning_position": 55,
    "weekday_night_position": 0, "weekday_morning_position": 55,
    "weekend_night_position": 12, "weekend_morning_position": 100,
    "night_elevation_preset": "dusk", "morning_elevation_preset": "dawn",
    "night_sun_event": "dusk", "morning_sun_event": "sunrise",
    "night_not_before": "00:00:00", "night_not_after": "23:30:00",
    "morning_not_before": "05:00:00", "morning_not_after": "09:00:00",
    "active_months": ["11", "12", "1"],
}
# custom elevations: a negative half degree and an exact zero
_VALUES_CUSTOM = {
    **_VALUES_NAMED,
    "night_elevation_preset": "custom", "night_sun_elevation": -3.5,
    "morning_elevation_preset": "custom", "morning_sun_elevation": 0.0,
}


def _answers(values: dict[str, Any], night: str, morning: str, mode: str) -> dict[str, Any]:
    visible = _visible_basic_fields(night, morning, mode) | _visible_advanced_fields(night, morning)
    out = {k: v for k, v in values.items() if k in visible}
    if out.get("night_elevation_preset") != "custom":
        out.pop("night_sun_elevation", None)
    if out.get("morning_elevation_preset") != "custom":
        out.pop("morning_sun_elevation", None)
    return out


_STORED_EMPTY: dict[str, Any] = {}
_STORED_RICH: dict[str, Any] = {
    "id": "default", "unrelated_key": {"keep": [1, 2]},
    "night_trigger": "fixed_time", "morning_trigger": "fixed_time", "schedule_mode": "same_every_day",
    "night_fixed_time": "22:30:00", "morning_fixed_time": "07:00:00",
    "night_sun_elevation_deg": -9.5, "morning_sun_elevation_deg": 0.0,
    "night_position": 5, "morning_position": 80,
    "weekday_night_fixed_time": "22:10:00", "weekday_morning_fixed_time": "06:20:00",
    "weekend_night_fixed_time": "23:40:00", "weekend_morning_fixed_time": "08:50:00",
    "weekday_night_position": 7, "weekday_morning_position": 70,
    "weekend_night_position": 9, "weekend_morning_position": 90,
    "active_months": [3, 4, 5], "night_sun_event": "sunset", "morning_sun_event": "dawn",
    "night_not_before": "20:00:00", "night_not_after": "23:59:00",
    "morning_not_before": "05:30:00", "morning_not_after": "08:30:00",
}

_BASIC_CASES = [(n.value, m.value, s.value) for n in NIGHT_TRIGGERS for m in MORNING_TRIGGERS for s in MODES]


# ---------------------------------------------------------------------------
# The documented rules of the saved schedule (reference model)
# ---------------------------------------------------------------------------

def _time_out(answer: Any, fallback: str) -> str:
    return time.fromisoformat(answer if isinstance(answer, str) else fallback).isoformat()


def _optional_time_out(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _expected_lifecycle_config(
    stored: dict[str, Any], night: str, morning: str, mode: str, a: dict[str, Any]
) -> dict[str, Any]:
    out = dict(stored)
    out.update(night_trigger=night, morning_trigger=morning, schedule_mode=mode)
    if night == _OFF and morning == _OFF:
        return out                       # nothing to detail: only the selection is saved
    ww = mode == "weekday_weekend"

    def elevation(phase: str, presets: dict[str, float], default: float) -> float:
        key = f"{phase}_sun_elevation_deg"
        preset = a.get(f"{phase}_elevation_preset")
        if preset is None:
            return stored.get(key, default)
        if preset != "custom":
            return presets[preset]
        return float(a.get(f"{phase}_sun_elevation", stored.get(key, default)))

    out["night_sun_elevation_deg"] = elevation("night", NIGHT_ELEVATION_PRESETS, DEFAULT_NIGHT_SUN_ELEVATION)
    out["morning_sun_elevation_deg"] = elevation("morning", MORNING_ELEVATION_PRESETS, DEFAULT_MORNING_SUN_ELEVATION)
    if ww:
        out["weekday_night_fixed_time"] = _time_out(
            a.get("weekday_night_fixed_time"),
            stored.get("weekday_night_fixed_time") or stored.get("night_fixed_time", DEFAULT_WEEKDAY_NIGHT_FIXED_TIME))
        out["weekday_morning_fixed_time"] = _time_out(
            a.get("weekday_morning_fixed_time"),
            stored.get("weekday_morning_fixed_time") or stored.get("morning_fixed_time", DEFAULT_WEEKDAY_MORNING_FIXED_TIME))
        out["weekend_night_fixed_time"] = _time_out(
            a.get("weekend_night_fixed_time"), stored.get("weekend_night_fixed_time", DEFAULT_WEEKEND_NIGHT_FIXED_TIME))
        out["weekend_morning_fixed_time"] = _time_out(
            a.get("weekend_morning_fixed_time"), stored.get("weekend_morning_fixed_time", DEFAULT_WEEKEND_MORNING_FIXED_TIME))
        for day in ("weekday", "weekend"):
            for phase, default in (("night", DEFAULT_NIGHT_POSITION), ("morning", DEFAULT_MORNING_POSITION)):
                key = f"{day}_{phase}_position"
                out[key] = int(a.get(key, stored.get(key, default)))
    else:
        out["night_fixed_time"] = _time_out(a.get("night_fixed_time"), stored.get("night_fixed_time", DEFAULT_NIGHT_FIXED_TIME))
        out["morning_fixed_time"] = _time_out(
            a.get("morning_fixed_time"), stored.get("morning_fixed_time", DEFAULT_MORNING_FIXED_TIME))
        out["night_position"] = int(a.get("night_position", stored.get("night_position", DEFAULT_NIGHT_POSITION)))
        out["morning_position"] = int(a.get("morning_position", stored.get("morning_position", DEFAULT_MORNING_POSITION)))
    months = a.get("active_months", stored.get("active_months") or [])
    out["active_months"] = sorted(int(m) for m in months) if months else None
    for phase, trigger in (("night", night), ("morning", morning)):
        # a sun event or a time constraint only exists next to a time: it is
        # cleared whenever the phase has no time any more
        applicable = trigger in _TIME_TRIGGERS
        out[f"{phase}_sun_event"] = (a.get(f"{phase}_sun_event", stored.get(f"{phase}_sun_event")) or None) if applicable else None
        for edge in ("not_before", "not_after"):
            key = f"{phase}_{edge}"
            out[key] = _optional_time_out(a.get(key, stored.get(key))) if applicable else None
    return out


# ---------------------------------------------------------------------------
# Driving the real flows (the only part that depends on the page layout)
# ---------------------------------------------------------------------------

def _entry(data: dict[str, Any]) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = data
    entry.options = {}
    return entry


def _options_flow(stored: dict[str, Any]) -> SmartShadingOptionsFlow:
    entry = _entry({"name": "Zone", "lifecycle_config": dict(stored), "unrelated": "kept"})
    flow = SmartShadingOptionsFlow(entry)
    flow.hass = MagicMock()
    flow.async_show_form = lambda **kw: {"type": "form", **kw}  # type: ignore[method-assign]
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}  # type: ignore[method-assign]

    def _update(config_entry, *, data=None, **_kw):
        config_entry.data = data

    flow.hass.config_entries.async_update_entry.side_effect = _update
    return flow


def _saves(flow: SmartShadingOptionsFlow) -> int:
    return flow.hass.config_entries.async_update_entry.call_count


def _stored_after(flow: SmartShadingOptionsFlow) -> dict[str, Any]:
    return flow._config_entry.data["lifecycle_config"]


def _selection(night: str, morning: str, mode: str) -> dict[str, Any]:
    return {"night_trigger": night, "morning_trigger": morning, "schedule_mode": mode}


def _form_fields(result: dict[str, Any]) -> set[str]:
    return {str(getattr(k, "schema", k)) for k in result["data_schema"].schema}


# ---------------------------------------------------------------------------
# CHAR-01  what the schedule detail form shows
# ---------------------------------------------------------------------------

def _walk_pages(flow, first_result, values, *, setup=False):
    """Answer every page the flow shows, in order. Returns the visited
    (step_id, field names) list and the final result."""
    visited = []
    result = first_result
    guard = 0
    while result["type"] == "form" and result["step_id"] not in ("lifecycle_advanced", "presence"):
        guard += 1
        assert guard < 12, "schedule pages do not end"
        step_id = result["step_id"]
        fields = _form_fields(result)
        visited.append((step_id, fields))
        data = {k: v for k, v in values.items() if k in fields}
        result = asyncio.run(getattr(flow, f"async_step_{step_id}")(user_input=data))
    return visited, result


class TestScheduleFormContents:
    @pytest.mark.parametrize("night, morning, mode", _BASIC_CASES)
    def test_options_pages_offer_exactly_the_fields_of_the_selected_triggers(self, night, morning, mode):
        if night == _OFF and morning == _OFF:
            pytest.skip("both phases disabled: no pages")
        flow = _options_flow(_STORED_RICH)
        first = asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, mode)))
        visited, _ = _walk_pages(flow, first, _VALUES_CUSTOM)
        offered = set().union(*(fields for _, fields in visited))
        assert offered == _visible_basic_fields(night, morning, mode)

    @pytest.mark.parametrize("night, morning, mode", _BASIC_CASES)
    def test_setup_pages_offer_the_same_basic_fields_plus_the_sun_events(self, night, morning, mode):
        if night == _OFF and morning == _OFF:
            pytest.skip("both phases disabled: no pages")
        flow = _setup_flow()
        first = asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, mode)))
        visited, _ = _walk_pages(flow, first, _VALUES_CUSTOM, setup=True)
        offered = set().union(*(fields for _, fields in visited))
        events = {f"{p}_sun_event" for p, trig in (("night", night), ("morning", morning)) if trig in _TIME_TRIGGERS}
        assert offered == _visible_basic_fields(night, morning, mode) | events


# ---------------------------------------------------------------------------
# CHAR-02  what the options flow saves
# ---------------------------------------------------------------------------

class TestOptionsSaveSemantics:
    @pytest.mark.parametrize("stored_name, stored", [("empty", _STORED_EMPTY), ("rich", _STORED_RICH)])
    @pytest.mark.parametrize("values_name, values", [("named", _VALUES_NAMED), ("custom", _VALUES_CUSTOM)])
    @pytest.mark.parametrize("night, morning, mode", _BASIC_CASES)
    def test_saved_schedule_matches_the_rules(self, night, morning, mode, values_name, values, stored_name, stored):
        flow = _options_flow(stored)
        answers = _answers(values, night, morning, mode)
        out = _drive_options_all(flow, night, morning, mode, answers)
        assert out["errors"] == {}
        assert _stored_after(flow) == _expected_lifecycle_config(stored, night, morning, mode, answers)
        assert flow._config_entry.data["unrelated"] == "kept"
        assert flow._config_entry.data["name"] == "Zone"

    @pytest.mark.parametrize("night, morning, mode", _BASIC_CASES)
    def test_submitting_the_prefilled_pages_keeps_the_stored_schedule(self, night, morning, mode):
        """Accepting every prefilled value changes nothing but the selection (and
        what the rules clear or normalise)."""
        flow = _options_flow(_STORED_RICH)
        out = _drive_options_all(flow, night, morning, mode, {}, accept_defaults=True)
        assert out["errors"] == {}
        assert _stored_after(flow) == _expected_lifecycle_config(_STORED_RICH, night, morning, mode, out["submitted"])
        assert _stored_after(flow)["unrelated_key"] == {"keep": [1, 2]}

    def test_both_phases_disabled_saves_only_the_selection(self):
        flow = _options_flow(_STORED_RICH)
        result = asyncio.run(flow.async_step_lifecycle(user_input=_selection(_OFF, _OFF, "weekday_weekend")))
        assert result["type"] == "create_entry"
        assert _saves(flow) == 1
        assert _stored_after(flow) == {
            **_STORED_RICH, "night_trigger": _OFF, "morning_trigger": _OFF, "schedule_mode": "weekday_weekend"}

    @pytest.mark.parametrize("mode", ["same_every_day", "weekday_weekend"])
    def test_trigger_change_clears_sun_events_and_constraints_of_a_phase_without_a_time(self, mode):
        flow = _options_flow(_STORED_RICH)
        answers = _answers(_VALUES_NAMED, "sun_elevation", "fixed_time", mode)
        _drive_options_all(flow, "sun_elevation", "fixed_time", mode, answers)
        saved = _stored_after(flow)
        assert saved["night_sun_event"] is None
        assert saved["night_not_before"] is None and saved["night_not_after"] is None
        assert saved["morning_sun_event"] == "sunrise"
        assert saved["morning_not_before"] == "05:00:00" and saved["morning_not_after"] == "09:00:00"

    @pytest.mark.parametrize("mode", ["same_every_day", "weekday_weekend"])
    def test_a_disabled_phase_also_loses_its_sun_event_and_constraints(self, mode):
        flow = _options_flow(_STORED_RICH)
        answers = _answers(_VALUES_NAMED, "disabled", "fixed_time", mode)
        _drive_options_all(flow, "disabled", "fixed_time", mode, answers)
        saved = _stored_after(flow)
        assert saved["night_sun_event"] is None and saved["night_not_before"] is None
        assert saved["night_trigger"] == "disabled"
        # its position and times stay as they were stored
        assert saved["night_position"] == _STORED_RICH["night_position"] or mode == "weekday_weekend"

    @pytest.mark.parametrize("phase, edges", [
        ("night", {"night_not_before": "23:00:00", "night_not_after": "20:00:00"}),
        ("morning", {"morning_not_before": "09:00:00", "morning_not_after": "05:00:00"}),
    ])
    def test_an_inverted_constraint_window_is_rejected_and_not_saved(self, phase, edges):
        flow = _options_flow(_STORED_RICH)
        answers = _answers(_VALUES_NAMED, "fixed_time", "fixed_time", "same_every_day")
        answers.update(edges)
        out = _drive_options_all(flow, "fixed_time", "fixed_time", "same_every_day", answers)
        assert out["errors"] == {"base": f"{phase}_clamp_window_invalid"}
        saved = _stored_after(flow)
        for key in ("active_months", "night_sun_event", "morning_sun_event", "night_not_before", "night_not_after",
                    "morning_not_before", "morning_not_after"):
            assert saved[key] == _STORED_RICH[key], key

    def test_equal_constraint_edges_are_accepted(self):
        flow = _options_flow(_STORED_RICH)
        answers = _answers(_VALUES_NAMED, "fixed_time", "fixed_time", "same_every_day")
        answers.update(night_not_before="21:00:00", night_not_after="21:00:00")
        out = _drive_options_all(flow, "fixed_time", "fixed_time", "same_every_day", answers)
        assert out["errors"] == {}
        assert _stored_after(flow)["night_not_before"] == "21:00:00"

    def test_clearing_the_constraints_and_months_stores_none(self):
        flow = _options_flow(_STORED_RICH)
        answers = _answers(_VALUES_NAMED, "fixed_time", "fixed_time", "same_every_day")
        for key in ("night_not_before", "night_not_after", "morning_not_before", "morning_not_after", "night_sun_event"):
            answers.pop(key)
        answers["active_months"] = []
        _drive_options_all(flow, "fixed_time", "fixed_time", "same_every_day", answers, cleared=True)
        saved = _stored_after(flow)
        assert saved["active_months"] is None
        assert saved["night_not_before"] is None and saved["morning_not_after"] is None
        assert saved["night_sun_event"] is None
        assert saved["morning_sun_event"] == "sunrise"

    def test_midnight_and_zero_values_survive(self):
        flow = _options_flow(_STORED_EMPTY)
        answers = _answers(_VALUES_CUSTOM, "both", "both", "same_every_day")
        answers.update(night_fixed_time="00:00:00", night_position=0, morning_position=0)
        _drive_options_all(flow, "both", "both", "same_every_day", answers)
        saved = _stored_after(flow)
        assert saved["night_fixed_time"] == "00:00:00"
        assert saved["night_position"] == 0 and saved["morning_position"] == 0
        assert saved["night_sun_elevation_deg"] == -3.5
        assert saved["morning_sun_elevation_deg"] == 0.0
        assert saved["night_not_before"] == "00:00:00"

    def test_custom_elevation_round_trips_through_a_second_edit(self):
        flow = _options_flow(_STORED_EMPTY)
        _drive_options_all(flow, "sun_elevation", "sun_elevation", "same_every_day",
                           _answers(_VALUES_CUSTOM, "sun_elevation", "sun_elevation", "same_every_day"))
        first = _stored_after(flow)
        assert (first["night_sun_elevation_deg"], first["morning_sun_elevation_deg"]) == (-3.5, 0.0)
        again = _options_flow(first)
        out = _drive_options_all(again, "sun_elevation", "sun_elevation", "same_every_day", {}, accept_defaults=True)
        assert out["errors"] == {}
        saved = _stored_after(again)
        assert (saved["night_sun_elevation_deg"], saved["morning_sun_elevation_deg"]) == (-3.5, 0.0)

    def test_a_stored_elevation_that_equals_a_preset_is_offered_as_that_preset(self):
        flow = _options_flow({"night_sun_elevation_deg": -6.0, "morning_sun_elevation_deg": -3.0})
        night_page = asyncio.run(flow.async_step_lifecycle(
            user_input=_selection("sun_elevation", "sun_elevation", "same_every_day")))
        assert _defaults_of(night_page)["night_elevation_preset"] == "dusk"
        morning_page = asyncio.run(flow.async_step_lifecycle_night(
            user_input={"night_elevation_preset": "dusk", "night_position": 0}))
        assert _defaults_of(morning_page)["morning_elevation_preset"] == "custom"

    def test_switching_mode_keeps_the_other_modes_values(self):
        flow = _options_flow(_STORED_RICH)
        answers = _answers(_VALUES_NAMED, "fixed_time", "fixed_time", "same_every_day")
        _drive_options_all(flow, "fixed_time", "fixed_time", "same_every_day", answers)
        saved = _stored_after(flow)
        for key in ("weekday_night_fixed_time", "weekend_night_fixed_time", "weekday_night_position",
                    "weekend_morning_position"):
            assert saved[key] == _STORED_RICH[key]


def _drive_options_all(flow, night, morning, mode, answers, accept_defaults=False, cleared=False):
    """Run the whole schedule edit with the given flat answers; see _SCHEDULE_DRIVER."""
    return _SCHEDULE_DRIVER(flow, night, morning, mode, answers, accept_defaults, cleared)


def _defaults_of(form: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for marker in form["data_schema"].schema:
        name = str(getattr(marker, "schema", marker))
        default = getattr(marker, "default", None)
        if callable(default):
            try:
                value = default()
            except TypeError:
                continue
            if value is not None and type(value).__name__ != "UndefinedType":
                out[name] = value
        suggested = (getattr(marker, "description", None) or {}).get("suggested_value")
        if suggested is not None:
            out[name] = suggested
    return out


def _wizard_driver(flow, night, morning, mode, answers, accept_defaults, cleared):  # noqa: ARG001
    """Answer the schedule pages one by one, then the Advanced schedule page."""
    result = asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, mode)))
    if result["type"] == "create_entry":
        return {"errors": {}, "submitted": {}}
    submitted: dict[str, Any] = {}
    guard = 0
    while result["type"] == "form":
        guard += 1
        assert guard < 12
        step_id = result["step_id"]
        data = _defaults_of(result) if accept_defaults else {k: v for k, v in answers.items() if k in _form_fields(result)}
        submitted.update(data)
        result = asyncio.run(getattr(flow, f"async_step_{step_id}")(user_input=data))
    assert result["type"] == "create_entry"          # one final save after the last page
    advanced = asyncio.run(flow.async_step_lifecycle_advanced())
    data = _defaults_of(advanced) if accept_defaults else {k: v for k, v in answers.items() if k in _form_fields(advanced)}
    submitted.update(data)
    result = asyncio.run(flow.async_step_lifecycle_advanced(user_input=data))
    return {"errors": result.get("errors") or {}, "submitted": submitted}


_SCHEDULE_DRIVER = _wizard_driver


# ---------------------------------------------------------------------------
# CHAR-03  what the setup flow collects
# ---------------------------------------------------------------------------

def _setup_flow() -> SmartShadingConfigFlow:
    flow = SmartShadingConfigFlow()
    flow.hass = MagicMock()
    flow.async_show_form = lambda **kw: {"type": "form", **kw}  # type: ignore[method-assign]

    async def _presence(user_input=None):
        return {"type": "form", "step_id": "presence"}

    flow.async_step_presence = _presence  # type: ignore[method-assign]
    return flow


def _setup_snapshot(flow: SmartShadingConfigFlow) -> dict[str, Any]:
    names = [
        "night_trigger", "night_fixed_time", "night_sun_elevation", "night_position", "night_sun_event",
        "morning_trigger", "morning_fixed_time", "morning_sun_elevation", "morning_position", "morning_sun_event",
        "schedule_mode", "weekday_night_fixed_time", "weekday_night_position", "weekday_morning_fixed_time",
        "weekday_morning_position", "weekend_night_fixed_time", "weekend_night_position",
        "weekend_morning_fixed_time", "weekend_morning_position",
    ]
    snap: dict[str, Any] = {}
    for name in names:
        value = getattr(flow, f"_{name}")
        snap[name] = getattr(value, "value", value) if not isinstance(value, time) else value.isoformat()
    return snap


def _expected_setup(night: str, morning: str, mode: str, a: dict[str, Any]) -> dict[str, Any]:
    ww = mode == "weekday_weekend"

    def t(key: str, fallback: str) -> str:
        return _time_out(a.get(key), fallback)

    def elevation(phase: str, presets: dict[str, float], default: float) -> float:
        preset = a.get(f"{phase}_elevation_preset")
        if preset is None:
            return default
        if preset != "custom":
            return presets[preset]
        return float(a.get(f"{phase}_sun_elevation", default))

    snap = {
        "night_trigger": night, "morning_trigger": morning, "schedule_mode": mode,
        "night_fixed_time": DEFAULT_NIGHT_FIXED_TIME, "morning_fixed_time": DEFAULT_MORNING_FIXED_TIME,
        "night_position": DEFAULT_NIGHT_POSITION, "morning_position": DEFAULT_MORNING_POSITION,
        "night_sun_event": None, "morning_sun_event": None,
        "weekday_night_fixed_time": DEFAULT_WEEKDAY_NIGHT_FIXED_TIME,
        "weekday_morning_fixed_time": DEFAULT_WEEKDAY_MORNING_FIXED_TIME,
        "weekend_night_fixed_time": DEFAULT_WEEKEND_NIGHT_FIXED_TIME,
        "weekend_morning_fixed_time": DEFAULT_WEEKEND_MORNING_FIXED_TIME,
        "weekday_night_position": DEFAULT_NIGHT_POSITION, "weekday_morning_position": DEFAULT_MORNING_POSITION,
        "weekend_night_position": DEFAULT_NIGHT_POSITION, "weekend_morning_position": DEFAULT_MORNING_POSITION,
        "night_sun_elevation": DEFAULT_NIGHT_SUN_ELEVATION,
        "morning_sun_elevation": DEFAULT_MORNING_SUN_ELEVATION,
    }
    if night == _OFF and morning == _OFF:
        return snap
    snap["night_sun_elevation"] = elevation("night", NIGHT_ELEVATION_PRESETS, DEFAULT_NIGHT_SUN_ELEVATION) \
        if night in _SUN_TRIGGERS else DEFAULT_NIGHT_SUN_ELEVATION
    snap["morning_sun_elevation"] = elevation("morning", MORNING_ELEVATION_PRESETS, DEFAULT_MORNING_SUN_ELEVATION) \
        if morning in _SUN_TRIGGERS else DEFAULT_MORNING_SUN_ELEVATION
    snap["night_sun_event"] = a.get("night_sun_event") if night in _TIME_TRIGGERS else None
    snap["morning_sun_event"] = a.get("morning_sun_event") if morning in _TIME_TRIGGERS else None
    if ww:
        for day, d_night, d_morning in (
            ("weekday", DEFAULT_WEEKDAY_NIGHT_FIXED_TIME, DEFAULT_WEEKDAY_MORNING_FIXED_TIME),
            ("weekend", DEFAULT_WEEKEND_NIGHT_FIXED_TIME, DEFAULT_WEEKEND_MORNING_FIXED_TIME),
        ):
            snap[f"{day}_night_fixed_time"] = t(f"{day}_night_fixed_time", d_night)
            snap[f"{day}_morning_fixed_time"] = t(f"{day}_morning_fixed_time", d_morning)
            snap[f"{day}_night_position"] = int(a.get(f"{day}_night_position", DEFAULT_NIGHT_POSITION))
            snap[f"{day}_morning_position"] = int(a.get(f"{day}_morning_position", DEFAULT_MORNING_POSITION))
    else:
        snap["night_fixed_time"] = t("night_fixed_time", DEFAULT_NIGHT_FIXED_TIME)
        snap["morning_fixed_time"] = t("morning_fixed_time", DEFAULT_MORNING_FIXED_TIME)
        snap["night_position"] = int(a.get("night_position", DEFAULT_NIGHT_POSITION))
        snap["morning_position"] = int(a.get("morning_position", DEFAULT_MORNING_POSITION))
    return snap


def _setup_answers(values: dict[str, Any], night: str, morning: str, mode: str) -> dict[str, Any]:
    """Setup offers the basic fields plus the optional sun events, nothing else."""
    visible = _visible_basic_fields(night, morning, mode)
    for phase, trigger in (("night", night), ("morning", morning)):
        if trigger in _TIME_TRIGGERS:
            visible.add(f"{phase}_sun_event")
    out = {k: v for k, v in values.items() if k in visible}
    if out.get("night_elevation_preset") != "custom":
        out.pop("night_sun_elevation", None)
    if out.get("morning_elevation_preset") != "custom":
        out.pop("morning_sun_elevation", None)
    return out


def _setup_wizard(flow, night, morning, mode, answers):
    result = asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, mode)))
    _, result = _walk_pages(flow, result, answers, setup=True)
    assert result["step_id"] == "presence"


_SETUP_DRIVER = _setup_wizard


class TestSetupFlowCollectsTheSchedule:
    @pytest.mark.parametrize("values_name, values", [("named", _VALUES_NAMED), ("custom", _VALUES_CUSTOM)])
    @pytest.mark.parametrize("night, morning, mode", _BASIC_CASES)
    def test_setup_collects_what_the_user_entered(self, night, morning, mode, values_name, values):
        flow = _setup_flow()
        answers = _setup_answers(values, night, morning, mode)
        _SETUP_DRIVER(flow, night, morning, mode, answers)
        assert _setup_snapshot(flow) == _expected_setup(night, morning, mode, answers)

    @pytest.mark.parametrize("night, morning, mode", _BASIC_CASES)
    def test_setup_never_offers_the_advanced_schedule_fields(self, night, morning, mode):
        if night == _OFF and morning == _OFF:
            pytest.skip("both phases disabled: no detail form")
        flow = _setup_flow()
        result = asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, mode)))
        shown = _form_fields(result)
        assert not shown & {"active_months", "night_not_before", "night_not_after",
                            "morning_not_before", "morning_not_after"}


# ---------------------------------------------------------------------------
# PAGE-01  page sequence
# ---------------------------------------------------------------------------

def _visit(flow, first, values):
    visited, last = _walk_pages(flow, first, values)
    return [step for step, _ in visited], last


class TestPageSequence:
    @pytest.mark.parametrize("night, morning, expected", [
        ("fixed_time", "fixed_time", ["lifecycle_night", "lifecycle_morning"]),
        ("sun_elevation", "both", ["lifecycle_night", "lifecycle_morning"]),
        ("fixed_time", "disabled", ["lifecycle_night"]),
        ("disabled", "both", ["lifecycle_morning"]),
    ])
    def test_same_every_day_has_one_page_per_enabled_phase(self, night, morning, expected):
        flow = _options_flow(_STORED_EMPTY)
        first = asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, "same_every_day")))
        steps, _ = _visit(flow, first, _VALUES_NAMED)
        assert steps == expected

    @pytest.mark.parametrize("night, morning, expected", [
        ("fixed_time", "fixed_time", ["lifecycle_night_weekday", "lifecycle_night_weekend",
                                      "lifecycle_morning_weekday", "lifecycle_morning_weekend"]),
        ("both", "disabled", ["lifecycle_night_weekday", "lifecycle_night_weekend"]),
        ("disabled", "sun_elevation", ["lifecycle_morning_weekday", "lifecycle_morning_weekend"]),
    ])
    def test_weekday_weekend_splits_each_enabled_phase(self, night, morning, expected):
        flow = _options_flow(_STORED_EMPTY)
        first = asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, "weekday_weekend")))
        steps, _ = _visit(flow, first, _VALUES_NAMED)
        assert steps == expected

    def test_the_setup_flow_walks_the_same_pages(self):
        flow = _setup_flow()
        first = asyncio.run(flow.async_step_lifecycle(user_input=_selection("fixed_time", "both", "weekday_weekend")))
        steps, last = _visit(flow, first, _VALUES_NAMED)
        assert steps == ["lifecycle_night_weekday", "lifecycle_night_weekend",
                         "lifecycle_morning_weekday", "lifecycle_morning_weekend"]
        assert last["step_id"] == "presence"

    def test_both_disabled_shows_no_page_in_either_flow(self):
        options = _options_flow(_STORED_RICH)
        result = asyncio.run(options.async_step_lifecycle(user_input=_selection(_OFF, _OFF, "same_every_day")))
        assert result["type"] == "create_entry"
        setup = _setup_flow()
        result = asyncio.run(setup.async_step_lifecycle(user_input=_selection(_OFF, _OFF, "same_every_day")))
        assert result["step_id"] == "presence"

    def test_the_trigger_selection_page_is_unchanged(self):
        for flow in (_options_flow(_STORED_RICH), _setup_flow()):
            form = asyncio.run(flow.async_step_lifecycle())
            assert form["step_id"] == "lifecycle"
            assert _form_fields(form) == {"night_trigger", "morning_trigger", "schedule_mode"}


# ---------------------------------------------------------------------------
# PAGE-02  conditional fields and the custom elevation follow-up
# ---------------------------------------------------------------------------

class TestConditionalFields:
    def _page(self, night, morning, mode, step):
        flow = _options_flow(_STORED_RICH)
        form = asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, mode)))
        while form["step_id"] != step:
            form = asyncio.run(getattr(flow, f"async_step_{form['step_id']}")(
                user_input={k: v for k, v in _VALUES_NAMED.items() if k in _form_fields(form)}))
        return form

    def test_a_time_trigger_shows_the_time_and_no_sun_position(self):
        form = self._page("fixed_time", "fixed_time", "same_every_day", "lifecycle_night")
        assert _form_fields(form) == {"night_fixed_time", "night_position"}

    def test_a_sun_trigger_shows_the_sun_position_and_no_time(self):
        form = self._page("sun_elevation", "fixed_time", "same_every_day", "lifecycle_night")
        assert _form_fields(form) == {"night_elevation_preset", "night_position"}

    def test_both_shows_time_and_sun_position(self):
        form = self._page("both", "fixed_time", "same_every_day", "lifecycle_night")
        assert _form_fields(form) == {"night_fixed_time", "night_elevation_preset", "night_position"}

    def test_the_weekend_page_has_only_time_and_position(self):
        form = self._page("both", "both", "weekday_weekend", "lifecycle_night_weekend")
        assert _form_fields(form) == {"weekend_night_fixed_time", "weekend_night_position"}

    def test_the_weekday_page_carries_the_shared_sun_position(self):
        form = self._page("both", "both", "weekday_weekend", "lifecycle_night_weekday")
        assert _form_fields(form) == {"weekday_night_fixed_time", "night_elevation_preset", "weekday_night_position"}

    def test_the_custom_value_is_never_on_the_page_with_the_preset(self):
        for step in ("lifecycle_night", "lifecycle_morning"):
            shown = _form_fields(self._page("both", "both", "same_every_day", step))
            assert not shown & {"night_sun_elevation", "morning_sun_elevation"}

    def test_positions_of_a_disabled_phase_are_never_shown(self):
        flow = _options_flow(_STORED_RICH)
        first = asyncio.run(flow.async_step_lifecycle(user_input=_selection("disabled", "fixed_time", "same_every_day")))
        visited, _ = _walk_pages(flow, first, _VALUES_NAMED)
        shown = set().union(*(fields for _, fields in visited))
        assert not {f for f in shown if f.startswith("night") or "_night_" in f}


class TestCustomElevationStep:
    def _start(self, flow, mode="same_every_day", night="sun_elevation", morning="sun_elevation"):
        return asyncio.run(flow.async_step_lifecycle(user_input=_selection(night, morning, mode)))

    def test_a_named_preset_goes_straight_to_the_next_page(self):
        flow = _options_flow(_STORED_EMPTY)
        self._start(flow)
        nxt = asyncio.run(flow.async_step_lifecycle_night(
            user_input={"night_elevation_preset": "dusk", "night_position": 0}))
        assert nxt["step_id"] == "lifecycle_morning"

    def test_custom_opens_one_follow_up_page_with_only_the_value(self):
        flow = _options_flow(_STORED_EMPTY)
        self._start(flow)
        form = asyncio.run(flow.async_step_lifecycle_night(
            user_input={"night_elevation_preset": "custom", "night_position": 0}))
        assert form["step_id"] == "lifecycle_night_elevation"
        assert _form_fields(form) == {"night_sun_elevation"}
        nxt = asyncio.run(flow.async_step_lifecycle_night_elevation(user_input={"night_sun_elevation": -3.5}))
        assert nxt["step_id"] == "lifecycle_morning"
        assert flow._sched_pending["night_sun_elevation_deg"] == -3.5

    def test_the_follow_up_value_is_a_float_and_keeps_zero_and_negative(self):
        for value in (0, 0.0, -0.5, -90, 12.5):
            flow = _options_flow(_STORED_EMPTY)
            self._start(flow)
            asyncio.run(flow.async_step_lifecycle_night(
                user_input={"night_elevation_preset": "custom", "night_position": 0}))
            asyncio.run(flow.async_step_lifecycle_night_elevation(user_input={"night_sun_elevation": value}))
            stored = flow._sched_pending["night_sun_elevation_deg"]
            assert stored == float(value) and isinstance(stored, float)

    def test_a_weekday_weekend_schedule_asks_before_the_weekend_page(self):
        flow = _options_flow(_STORED_EMPTY)
        self._start(flow, mode="weekday_weekend")
        form = asyncio.run(flow.async_step_lifecycle_night_weekday(
            user_input={"night_elevation_preset": "custom", "weekday_night_position": 0}))
        assert form["step_id"] == "lifecycle_night_elevation"
        nxt = asyncio.run(flow.async_step_lifecycle_night_elevation(user_input={"night_sun_elevation": -4.0}))
        assert nxt["step_id"] == "lifecycle_night_weekend"

    def test_switching_from_custom_back_to_a_preset_drops_the_custom_value(self):
        flow = _options_flow({"night_sun_elevation_deg": -3.5})
        self._start(flow, morning="disabled")
        asyncio.run(flow.async_step_lifecycle_night(
            user_input={"night_elevation_preset": "dark", "night_position": 0}))
        assert _stored_after(flow)["night_sun_elevation_deg"] == -12.0

    def test_the_setup_flow_uses_the_same_follow_up(self):
        flow = _setup_flow()
        asyncio.run(flow.async_step_lifecycle(user_input=_selection("sun_elevation", "disabled", "same_every_day")))
        form = asyncio.run(flow.async_step_lifecycle_night(
            user_input={"night_elevation_preset": "custom", "night_position": 0}))
        assert form["step_id"] == "lifecycle_night_elevation"
        nxt = asyncio.run(flow.async_step_lifecycle_night_elevation(user_input={"night_sun_elevation": 0.0}))
        assert nxt["step_id"] == "presence"
        assert flow._night_sun_elevation == 0.0


# ---------------------------------------------------------------------------
# PAGE-03  one save after the last page
# ---------------------------------------------------------------------------

class TestSingleFinalSave:
    @pytest.mark.parametrize("mode", ["same_every_day", "weekday_weekend"])
    def test_nothing_is_saved_until_the_last_page_is_answered(self, mode):
        flow = _options_flow(_STORED_RICH)
        form = asyncio.run(flow.async_step_lifecycle(user_input=_selection("both", "both", mode)))
        pages = 0
        while form["type"] == "form":
            assert _saves(flow) == 0
            pages += 1
            form = asyncio.run(getattr(flow, f"async_step_{form['step_id']}")(
                user_input={k: v for k, v in _VALUES_NAMED.items() if k in _form_fields(form)}))
        assert pages == (4 if mode == "weekday_weekend" else 2)
        assert _saves(flow) == 1

    def test_abandoning_the_pages_saves_nothing(self):
        flow = _options_flow(_STORED_RICH)
        asyncio.run(flow.async_step_lifecycle(user_input=_selection("both", "both", "same_every_day")))
        assert _saves(flow) == 0
        assert _stored_after(flow) == _STORED_RICH

    def test_a_second_edit_starts_from_the_stored_schedule_not_the_previous_answers(self):
        flow = _options_flow(_STORED_EMPTY)
        _drive_options_all(flow, "fixed_time", "fixed_time", "same_every_day",
                           _answers(_VALUES_NAMED, "fixed_time", "fixed_time", "same_every_day"))
        assert _stored_after(flow)["night_position"] == 0
        _drive_options_all(flow, "fixed_time", "fixed_time", "same_every_day", {"morning_position": 40})
        saved = _stored_after(flow)
        assert saved["morning_position"] == 40
        assert saved["night_position"] == 0                       # the first edit's value was stored
        assert saved["night_fixed_time"] == _VALUES_NAMED["night_fixed_time"]


# ---------------------------------------------------------------------------
# PAGE-04  the Advanced schedule step
# ---------------------------------------------------------------------------

class TestAdvancedSchedule:
    def test_it_is_an_item_of_the_advanced_menu(self):
        flow = _options_flow(_STORED_RICH)

        def _show_menu(*, step_id, menu_options, **_kw):
            return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

        flow.async_show_menu = _show_menu  # type: ignore[attr-defined]
        menu = asyncio.run(flow.async_step_advanced())
        assert "lifecycle_advanced" in menu["menu_options"] and "manual_override" in menu["menu_options"]

    def test_it_offers_only_what_the_stored_triggers_allow(self):
        flow = _options_flow({**_STORED_RICH, "night_trigger": "sun_elevation", "morning_trigger": "both"})
        fields = _form_fields(asyncio.run(flow.async_step_lifecycle_advanced()))
        assert fields == {"active_months", "morning_sun_event", "morning_not_before", "morning_not_after"}

    def test_with_both_phases_disabled_only_the_months_remain(self):
        flow = _options_flow({**_STORED_RICH, "night_trigger": "disabled", "morning_trigger": "disabled"})
        assert _form_fields(asyncio.run(flow.async_step_lifecycle_advanced())) == {"active_months"}

    def test_it_is_prefilled_from_the_stored_schedule(self):
        flow = _options_flow(_STORED_RICH)
        defaults = _defaults_of(asyncio.run(flow.async_step_lifecycle_advanced()))
        assert defaults["night_sun_event"] == "sunset" and defaults["morning_not_after"] == "08:30:00"
        assert defaults["active_months"] == ["3", "4", "5"]

    def test_saving_it_changes_nothing_but_the_advanced_values(self):
        flow = _options_flow(_STORED_RICH)
        asyncio.run(flow.async_step_lifecycle_advanced(user_input={
            "active_months": ["6"], "night_sun_event": "dusk", "night_not_before": "21:00:00",
            "morning_sun_event": "", "morning_not_after": "10:00:00",
        }))
        saved = _stored_after(flow)
        assert _saves(flow) == 1
        changed = {k for k in set(saved) | set(_STORED_RICH) if saved.get(k) != _STORED_RICH.get(k)}
        assert changed == {"active_months", "night_sun_event", "night_not_before", "night_not_after",
                           "morning_sun_event", "morning_not_before", "morning_not_after"}
        assert saved["active_months"] == [6] and saved["night_sun_event"] == "dusk"
        assert saved["night_not_after"] is None and saved["morning_sun_event"] is None
        assert saved["morning_not_after"] == "10:00:00"
        assert flow._config_entry.data["unrelated"] == "kept"

    def test_it_never_touches_the_basic_schedule(self):
        flow = _options_flow(_STORED_RICH)
        asyncio.run(flow.async_step_lifecycle_advanced(user_input={}))
        saved = _stored_after(flow)
        for key in ("night_fixed_time", "morning_fixed_time", "night_position", "morning_position",
                    "weekday_night_fixed_time", "weekend_morning_position", "night_sun_elevation_deg",
                    "night_trigger", "morning_trigger", "schedule_mode", "unrelated_key"):
            assert saved[key] == _STORED_RICH[key], key

    def test_values_of_a_phase_without_a_time_are_neither_shown_nor_lost_nor_reactivated(self):
        stored = {**_STORED_RICH, "night_trigger": "sun_elevation"}
        flow = _options_flow(stored)
        form = asyncio.run(flow.async_step_lifecycle_advanced())
        assert not _form_fields(form) & {"night_sun_event", "night_not_before", "night_not_after"}
        asyncio.run(flow.async_step_lifecycle_advanced(user_input={
            "night_sun_event": "dusk", "night_not_before": "01:00:00", "active_months": ["1"]}))
        saved = _stored_after(flow)
        assert saved["night_sun_event"] == stored["night_sun_event"]
        assert saved["night_not_before"] == stored["night_not_before"]
        assert saved["night_not_after"] == stored["night_not_after"]

    def test_an_inverted_window_is_refused_with_the_known_error_and_the_input_stays(self):
        flow = _options_flow(_STORED_RICH)
        result = asyncio.run(flow.async_step_lifecycle_advanced(user_input={
            "night_not_before": "23:00:00", "night_not_after": "20:00:00"}))
        assert result["errors"] == {"base": "night_clamp_window_invalid"}
        assert _saves(flow) == 0
        assert _defaults_of(result)["night_not_before"] == "23:00:00"

    def test_a_later_wizard_run_still_clears_what_no_longer_applies(self):
        flow = _options_flow(_STORED_RICH)
        asyncio.run(flow.async_step_lifecycle_advanced(user_input={"night_sun_event": "dusk"}))
        _drive_options_all(flow, "sun_elevation", "fixed_time", "same_every_day",
                           _answers(_VALUES_NAMED, "sun_elevation", "fixed_time", "same_every_day"))
        assert _stored_after(flow)["night_sun_event"] is None

    def test_the_setup_flow_never_gets_the_advanced_page(self):
        assert not hasattr(SmartShadingConfigFlow, "async_step_lifecycle_advanced")


# ---------------------------------------------------------------------------
# PAGE-05  translations
# ---------------------------------------------------------------------------

def _translation_files():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"
    return [root / "strings.json", *sorted((root / "translations").glob("*.json"))]


_PAGE_STEPS = [
    "lifecycle_night", "lifecycle_night_weekday", "lifecycle_night_weekend",
    "lifecycle_morning", "lifecycle_morning_weekday", "lifecycle_morning_weekend",
    "lifecycle_night_elevation", "lifecycle_morning_elevation",
]


def _load_json(path):
    import json
    return json.loads(path.read_text(encoding="utf-8"))


class TestPageTranslations:
    def test_there_are_strings_and_many_languages(self):
        assert len(_translation_files()) >= 21

    @pytest.mark.parametrize("section", ["config", "options"])
    def test_every_page_has_a_title_and_labelled_fields_in_every_language(self, section):
        from custom_components.smartshading.config_flow import _PHASE_FIELDS, _SCHEDULE_PAGES
        for path in _translation_files():
            steps = _load_json(path)[section]["step"]
            assert "lifecycle_detail" not in steps, path.name
            for step_id in _PAGE_STEPS:
                assert steps[step_id]["title"].strip(), (path.name, step_id)
            for step_id, (phase, day) in _SCHEDULE_PAGES.items():
                labelled = set(steps[step_id]["data"])
                fields = _PHASE_FIELDS[phase]
                needed = {fields["position_field"][day], fields["time_field"][day]}
                if day != "weekend":
                    needed.add(fields["preset_field"])
                    if section == "config":
                        needed.add(fields["event_field"])
                assert needed <= labelled, (path.name, step_id, needed - labelled)
            for phase in ("night", "morning"):
                custom = _PHASE_FIELDS[phase]["custom_field"]
                assert custom in steps[f"lifecycle_{phase}_elevation"]["data"], path.name

    def test_the_advanced_schedule_step_and_menu_item_are_translated_everywhere(self):
        for path in _translation_files():
            options = _load_json(path)["options"]["step"]
            assert options["lifecycle_advanced"]["title"].strip(), path.name
            assert options["advanced"]["menu_options"]["lifecycle_advanced"].strip(), path.name
            labelled = set(options["lifecycle_advanced"]["data"])
            assert {"active_months", "night_sun_event", "night_not_before", "night_not_after",
                    "morning_sun_event", "morning_not_before", "morning_not_after"} <= labelled, path.name

    def test_the_setup_flow_has_no_advanced_schedule_step_text(self):
        for path in _translation_files():
            assert "lifecycle_advanced" not in _load_json(path)["config"]["step"], path.name

    def test_every_lifecycle_step_the_flows_define_is_translated(self):
        data = _load_json(_translation_files()[0])
        for name in dir(SmartShadingOptionsFlow):
            if name.startswith("async_step_lifecycle"):
                assert name.removeprefix("async_step_") in data["options"]["step"], name
        for name in dir(SmartShadingConfigFlow):
            if name.startswith("async_step_lifecycle"):
                assert name.removeprefix("async_step_") in data["config"]["step"], name
