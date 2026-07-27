"""T21 Phase C — zone OptionsFlow menu grouping (11 flat items -> 7 top-level
items via two grouping submenus, "windows" and "advanced").

Presentation-layer only: every original leaf step (add_window, edit_window,
remove_window, lifecycle_profiles, manual_override) is unchanged and still
directly reachable by calling its async_step_* method; only the navigation
path through async_step_init changed. No config data, storage format, or
entry behavior is affected.

T21 Phase C2 update: Cover Dispatch moved out of the zone Advanced menu
entirely, onto the System entry (async_step_system_dispatch) — it is a
hardware/RF-pacing property of the shared dispatch pipeline, not a per-zone
concern (see the Phase C2 ownership analysis). The System entry's OptionsFlow
no longer aborts — it now owns real global settings (Manual Override
defaults, Cover Dispatch).
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
from custom_components.smartshading.config_flow import SmartShadingOptionsFlow  # noqa: E402
from custom_components.smartshading.const import CONF_ENTRY_TYPE, ENTRY_TYPE_SYSTEM  # noqa: E402

_INTEGRATION_ROOT = Path(__file__).parent.parent / "custom_components" / "smartshading"

_WINDOWS_LEAF_STEPS = {"add_window", "edit_window", "remove_window"}
_ADVANCED_LEAF_STEPS = {"lifecycle_profiles", "manual_override"}
_DIRECT_LEAF_STEPS = {"weather", "lifecycle", "presence", "comfort", "behavior"}
_SYSTEM_INIT_LEAF_STEPS = {"system_manual_override", "system_dispatch"}


def _make_entry(data: dict | None = None):
    entry = MagicMock()
    entry.data = data if data is not None else {}
    entry.options = {}
    return entry


def _make_options_flow(data: dict | None = None) -> SmartShadingOptionsFlow:
    flow = SmartShadingOptionsFlow(_make_entry(data))
    flow.hass = MagicMock()
    return flow


class TestInitMenuHasSevenItems:
    def test_init_menu_is_exactly_the_expected_seven_top_level_items(self) -> None:
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_init(user_input=None))
        assert result["type"] == "menu"
        assert set(result["menu_options"]) == _DIRECT_LEAF_STEPS | {"windows", "advanced"}
        assert len(result["menu_options"]) == 7


class TestWindowsSubmenu:
    def test_windows_menu_lists_all_three_window_crud_steps(self) -> None:
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_windows(user_input=None))
        assert result["type"] == "menu"
        assert set(result["menu_options"]) == _WINDOWS_LEAF_STEPS

    def test_every_windows_leaf_step_is_still_directly_callable(self) -> None:
        flow = _make_options_flow(data={"windows": [], "cover_groups": []})
        for step in _WINDOWS_LEAF_STEPS:
            assert hasattr(flow, f"async_step_{step}"), step


class TestAdvancedSubmenu:
    def test_advanced_menu_lists_all_three_grouped_steps(self) -> None:
        flow = _make_options_flow(data={})
        result = asyncio.run(flow.async_step_advanced(user_input=None))
        assert result["type"] == "menu"
        assert set(result["menu_options"]) == _ADVANCED_LEAF_STEPS

    def test_every_advanced_leaf_step_is_still_directly_callable(self) -> None:
        flow = _make_options_flow(data={})
        for step in _ADVANCED_LEAF_STEPS:
            assert hasattr(flow, f"async_step_{step}"), step


class TestSystemEntryHasRealOptions:
    """T21 Phase C2: the System entry no longer aborts — it now owns real
    global settings (Manual Override defaults, Cover Dispatch)."""

    def test_system_entry_options_flow_shows_a_real_menu(self) -> None:
        flow = _make_options_flow(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM})
        result = asyncio.run(flow.async_step_init(user_input=None))
        assert result["type"] == "menu"
        assert set(result["menu_options"]) == _SYSTEM_INIT_LEAF_STEPS

    def test_every_system_init_leaf_step_is_directly_callable(self) -> None:
        flow = _make_options_flow(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SYSTEM})
        for step in _SYSTEM_INIT_LEAF_STEPS:
            assert hasattr(flow, f"async_step_{step}"), step


class TestNoStepWasDroppedDuringRegrouping:
    """Every step ID that appeared in the pre-Phase-C flat 11-item zone menu
    must still exist as a real options.step entry in strings.json somewhere
    — directly, under "windows"/"advanced" (T21 Phase C), or on the System
    entry (T21 Phase C2, "dispatch" -> "system_dispatch") — the regrouping/
    relocation must not have silently orphaned a step."""

    _ORIGINAL_ELEVEN_TO_CURRENT_STEP_ID = {
        "weather": "weather", "lifecycle": "lifecycle", "presence": "presence",
        "comfort": "comfort", "behavior": "behavior",
        "add_window": "add_window", "edit_window": "edit_window", "remove_window": "remove_window",
        "lifecycle_profiles": "lifecycle_profiles", "manual_override": "manual_override",
        "dispatch": "system_dispatch",
    }

    def test_all_eleven_original_steps_present_in_strings_json(self) -> None:
        data = json.loads((_INTEGRATION_ROOT / "strings.json").read_text(encoding="utf-8"))
        steps = data["options"]["step"]
        for original, current in self._ORIGINAL_ELEVEN_TO_CURRENT_STEP_ID.items():
            assert current in steps, f"missing step {current} (successor of {original})"
            assert steps[current]["title"], f"empty title for {current}"

    def test_all_eleven_original_steps_have_a_real_async_step_method(self) -> None:
        for original, current in self._ORIGINAL_ELEVEN_TO_CURRENT_STEP_ID.items():
            assert hasattr(SmartShadingOptionsFlow, f"async_step_{current}"), current
