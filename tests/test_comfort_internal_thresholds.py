"""Glare minimum exposure and heat release hysteresis are internal policy.

The two technical comfort settings ``glare_min_exposure_wm2`` and
``heat_protection_hysteresis_c`` are no longer user configuration.  The
algorithms stay; the effective values are fixed at 100.0 W/m2 and 1.0 degC no
matter what an old ConfigEntry still contains.

Two groups:

  * Characterization - the behavior with the default values.  It must be the
    same before and after the change.
  * Fixed policy - old ConfigEntries (custom, zero, negative, malformed values)
    resolve to the internal values, through the production loader and the
    production decision-input builder.  These fail against the previously
    configurable behavior.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.smartshading.config_entry_data import _comfort_config_from_storage
from custom_components.smartshading.engines.diagnostics_builder import (
    build_consolidated_diagnostics,
)
from custom_components.smartshading.engines.exposure_engine import WindowExposure
from custom_components.smartshading.engines.heat_hysteresis import resolve_heat_needed
from custom_components.smartshading.evaluators.glare_evaluator import GlareEvaluator
from custom_components.smartshading.evaluators.heat_evaluator import HeatEvaluator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

GLARE_MIN = 100.0
HEAT_HYSTERESIS = 1.0

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"

_WINDOW = WindowConfig(
    id="w-west", name="West", zone_id="z1", azimuth=270.0, floor_level=0, cover_group_id="cg"
)
_ZONE = ZoneConfig(id="z1", name="Living")

# Old stored comfort_config dicts: every kind of legacy value.
_LEGACY_VALUES = [
    pytest.param({"glare_min_exposure_wm2": 300.0, "heat_protection_hysteresis_c": 3.5}, id="custom"),
    pytest.param({"glare_min_exposure_wm2": 0, "heat_protection_hysteresis_c": 0}, id="zero"),
    pytest.param({"glare_min_exposure_wm2": -50.0, "heat_protection_hysteresis_c": -2.0}, id="negative"),
    pytest.param({"glare_min_exposure_wm2": 9999, "heat_protection_hysteresis_c": 99}, id="huge"),
    pytest.param({"glare_min_exposure_wm2": None, "heat_protection_hysteresis_c": None}, id="none"),
    pytest.param({"glare_min_exposure_wm2": "abc", "heat_protection_hysteresis_c": "x"}, id="strings"),
    pytest.param({"glare_min_exposure_wm2": [1], "heat_protection_hysteresis_c": {"a": 1}}, id="wrong-types"),
    pytest.param({"glare_min_exposure_wm2": float("nan"), "heat_protection_hysteresis_c": float("inf")}, id="non-finite"),
    pytest.param({}, id="missing"),
]


def _exposure(*, effective: float, low_angle: float = 0.0, measured: float = 0.0) -> WindowExposure:
    return WindowExposure(
        window_id=_WINDOW.id,
        timestamp=datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc),
        sun_azimuth=270.0, sun_elevation=20.0, is_above_horizon=True,
        is_in_tolerance_window=True, azimuth_delta_deg=0.0, direct_radiation_factor=1.0,
        elevation_clipped=False, theoretical_exposure=effective,
        learned_solar_impact_factor=1.0, seasonal_factor=1.0, effective_exposure=effective,
        measured_solar_wm2=measured, low_angle_direct_glare_wm2=low_angle,
    )


def _wdi(
    raw_comfort: dict | None = None,
    *,
    exposure=None,
    in_sector: bool = True,
    outdoor: float | None = None,
    indoor: float | None = None,
    heat_active: bool = False,
    state: ShadingState = ShadingState.OPEN,
    toggles: dict | None = None,
):
    """A decision input built exactly like production: stored dict -> loader ->
    ComfortConfig -> build_window_decision_input."""
    stored = {"heat_protection_enabled": False, "glare_protection_enabled": True}
    stored.update(toggles or {})
    stored.update(raw_comfort or {})
    return build_window_decision_input(
        window=_WINDOW, zone=_ZONE,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY,
        absence_active=False,
        current_shading_state=state,
        outdoor_temp_c=outdoor, indoor_temp_c=indoor,
        exposure=exposure,
        is_in_solar_sector=in_sector,
        comfort_config=_comfort_config_from_storage(stored),
        heat_previously_active=heat_active,
    )


def _glare(raw=None, **kw):
    return GlareEvaluator().evaluate(_wdi(raw, **kw))


def _heat(raw=None, *, outdoor, indoor=None, heat_active=False, exposure=None, in_sector=True):
    wdi = _wdi(
        raw, outdoor=outdoor, indoor=indoor, heat_active=heat_active, in_sector=in_sector,
        exposure=exposure, toggles={"heat_protection_enabled": True},
    )
    return HeatEvaluator().evaluate(wdi)


# ---------------------------------------------------------------------------
# Characterization: behavior with the default values
# ---------------------------------------------------------------------------

class TestDefaultValuesCharacterization:
    def test_defaults_resolve_to_the_documented_policy(self) -> None:
        behavior = _wdi().effective_behavior
        assert behavior.glare_min_exposure_wm2 == GLARE_MIN
        assert behavior.heat_hysteresis_c == HEAT_HYSTERESIS

    def test_glare_below_the_gate_does_not_fire(self) -> None:
        assert _glare(exposure=_exposure(effective=99.9)) is None

    def test_glare_at_the_gate_fires_with_the_light_floor(self) -> None:
        decision = _glare(exposure=_exposure(effective=100.0))
        assert decision is not None
        assert decision.shading_state is ShadingState.LIGHT_SHADE

    def test_glare_without_exposure_does_not_fire(self) -> None:
        assert _glare(exposure=None) is None

    @pytest.mark.parametrize("in_sector", [True, False])
    def test_solar_sector_boundary(self, in_sector: bool) -> None:
        decision = _glare(exposure=_exposure(effective=150.0), in_sector=in_sector)
        assert (decision is not None) is in_sector

    @pytest.mark.parametrize(
        ("low_angle", "expected"),
        [
            (100.0, ShadingState.LIGHT_SHADE),
            (179.9, ShadingState.LIGHT_SHADE),
            (180.0, ShadingState.NORMAL_SHADE),   # 1.8 x 100
            (299.9, ShadingState.NORMAL_SHADE),
            (300.0, ShadingState.STRONG_SHADE),   # 3.0 x 100
        ],
    )
    def test_glare_intensity_progression(self, low_angle: float, expected: ShadingState) -> None:
        decision = _glare(exposure=_exposure(effective=10.0, low_angle=low_angle, measured=200.0))
        assert decision is not None
        assert decision.shading_state is expected

    @pytest.mark.parametrize(
        ("low_angle", "expected"),
        [
            (250.0, ShadingState.STRONG_SHADE),   # exit ratio 2.5 holds STRONG
            (249.9, ShadingState.NORMAL_SHADE),
        ],
    )
    def test_strong_exit_hysteresis(self, low_angle: float, expected: ShadingState) -> None:
        decision = _glare(
            exposure=_exposure(effective=10.0, low_angle=low_angle, measured=200.0),
            state=ShadingState.STRONG_SHADE,
        )
        assert decision is not None
        assert decision.shading_state is expected

    def test_low_angle_needs_a_real_measured_beam(self) -> None:
        # low-angle estimate is only eligible with >= 40 W/m2 measured.
        assert _glare(exposure=_exposure(effective=10.0, low_angle=500.0, measured=39.9)) is None
        assert _glare(exposure=_exposure(effective=10.0, low_angle=500.0, measured=40.0)) is not None

    def test_heat_enters_at_the_thresholds(self) -> None:
        sun = _exposure(effective=200.0)
        assert _heat(outdoor=25.9, indoor=23.9, exposure=sun) is None
        assert _heat(outdoor=26.0, indoor=None, exposure=sun) is not None
        assert _heat(outdoor=None, indoor=24.0, exposure=sun) is not None

    def test_heat_release_uses_one_degree(self) -> None:
        sun = _exposure(effective=200.0)
        # entry 26 / 24 -> exit strictly below 25 / 23
        assert _heat(outdoor=25.0, indoor=23.0, heat_active=True, exposure=sun) is not None
        assert _heat(outdoor=24.9, indoor=22.9, heat_active=True, exposure=sun) is None

    def test_heat_oscillation_around_the_thresholds_is_stable(self) -> None:
        sun = _exposure(effective=200.0)
        active = False
        outcomes = []
        for outdoor in (25.9, 26.0, 25.5, 26.0, 25.0, 25.5, 24.9, 25.5, 26.0):
            result = resolve_heat_needed(
                outdoor_temp_c=outdoor, indoor_temp_c=None, outdoor_entry_c=26.0,
                indoor_entry_c=None, hysteresis_c=HEAT_HYSTERESIS, previously_active=active,
            )
            active = result.active
            outcomes.append(active)
        # in at 26.0, held through 25.5/25.0, out at 24.9, back in only at 26.0
        assert outcomes == [False, True, True, True, True, True, False, False, True]

    def test_heat_missing_sensor_data(self) -> None:
        sun = _exposure(effective=200.0)
        assert _heat(outdoor=None, indoor=None, heat_active=False, exposure=sun) is None
        assert _heat(outdoor=None, indoor=None, heat_active=True, exposure=sun) is not None

    def test_heat_exposure_floor_is_the_glare_floor(self) -> None:
        assert _heat(outdoor=30.0, exposure=_exposure(effective=99.9)) is None
        assert _heat(outdoor=30.0, exposure=_exposure(effective=100.0)) is not None

    def test_heat_floor_follows_a_lowered_light_threshold(self) -> None:
        """A learned/forecast-lowered light threshold lowers the heat floor:
        min(glare_min, light_threshold)."""
        wdi = _wdi(outdoor=30.0, exposure=_exposure(effective=60.0),
                   toggles={"heat_protection_enabled": True})
        lowered = dataclasses.replace(wdi.effective_behavior, light_shade_threshold_wm2=60.0)
        wdi = dataclasses.replace(wdi, effective_behavior=lowered)
        assert HeatEvaluator().evaluate(wdi) is not None
        raised = dataclasses.replace(wdi.effective_behavior, light_shade_threshold_wm2=400.0)
        wdi = dataclasses.replace(wdi, effective_behavior=raised)
        assert HeatEvaluator().evaluate(wdi) is None  # 60 < min(100, 400)


# ---------------------------------------------------------------------------
# Fixed policy: legacy storage can never change the effective values
# ---------------------------------------------------------------------------

class TestLegacyStorageResolvesToInternalPolicy:
    @pytest.mark.parametrize("raw", _LEGACY_VALUES)
    def test_loader_never_raises_and_policy_is_fixed(self, raw: dict) -> None:
        behavior = _wdi(raw).effective_behavior
        assert behavior.glare_min_exposure_wm2 == GLARE_MIN
        assert behavior.heat_hysteresis_c == HEAT_HYSTERESIS

    @pytest.mark.parametrize("raw", _LEGACY_VALUES)
    def test_glare_gate_ignores_legacy_value(self, raw: dict) -> None:
        assert _glare(raw, exposure=_exposure(effective=99.9)) is None
        assert _glare(raw, exposure=_exposure(effective=100.0)) is not None

    @pytest.mark.parametrize("raw", _LEGACY_VALUES)
    def test_glare_intensity_ignores_legacy_value(self, raw: dict) -> None:
        decision = _glare(raw, exposure=_exposure(effective=10.0, low_angle=300.0, measured=200.0))
        assert decision is not None
        assert decision.shading_state is ShadingState.STRONG_SHADE

    @pytest.mark.parametrize("raw", _LEGACY_VALUES)
    def test_heat_release_ignores_legacy_value(self, raw: dict) -> None:
        sun = _exposure(effective=200.0)
        assert _heat(raw, outdoor=25.0, indoor=23.0, heat_active=True, exposure=sun) is not None
        assert _heat(raw, outdoor=24.9, indoor=22.9, heat_active=True, exposure=sun) is None

    @pytest.mark.parametrize("raw", _LEGACY_VALUES)
    def test_heat_exposure_floor_ignores_legacy_value(self, raw: dict) -> None:
        assert _heat(raw, outdoor=30.0, exposure=_exposure(effective=99.9)) is None
        assert _heat(raw, outdoor=30.0, exposure=_exposure(effective=100.0)) is not None

    def test_repeated_loads_are_stable(self) -> None:
        raw = {"glare_min_exposure_wm2": 300.0, "heat_protection_hysteresis_c": 3.5}
        first = _comfort_config_from_storage(raw)
        second = _comfort_config_from_storage(raw)
        assert first == second
        assert _wdi(raw).effective_behavior.glare_min_exposure_wm2 == GLARE_MIN

    def test_unrelated_comfort_settings_are_still_read(self) -> None:
        loaded = _comfort_config_from_storage({
            "heat_protection_enabled": False,
            "glare_protection_enabled": False,
            "solar_gain_enabled": False,
            "heat_protection_indoor_temp_c": 22.0,
            "heat_protection_outdoor_temp_c": 28.5,
            "solar_gain_max_outdoor_temp_c": 10.0,
            "glare_min_exposure_wm2": 300.0,
            "heat_protection_hysteresis_c": 4.0,
        })
        assert loaded.heat_protection_enabled is False
        assert loaded.glare_protection_enabled is False
        assert loaded.solar_gain_enabled is False
        assert loaded.heat_protection_indoor_temp_c == 22.0
        assert loaded.heat_protection_outdoor_temp_c == 28.5
        assert loaded.solar_gain_max_outdoor_temp_c == 10.0

    def test_comfort_config_has_no_user_value_for_the_removed_settings(self) -> None:
        names = {f.name for f in dataclasses.fields(ComfortConfig)}
        assert "glare_min_exposure_wm2" not in names
        assert "heat_protection_hysteresis_c" not in names

    def test_storage_serializer_no_longer_writes_the_removed_keys(self) -> None:
        source = (_ROOT / "config_entry_data.py").read_text(encoding="utf-8")
        comfort_block = source.split('"comfort_config": {', 1)[1].split("},", 1)[0]
        assert "glare_min_exposure_wm2" not in comfort_block
        assert "heat_protection_hysteresis_c" not in comfort_block


# ---------------------------------------------------------------------------
# Diagnostics report the effective policy, not stored values
# ---------------------------------------------------------------------------

def _fake_coordinator(comfort_config: ComfortConfig):
    return SimpleNamespace(
        zones={}, windows={}, cover_groups={},
        _comfort_config=comfort_config, _heat_active={},
    )


class TestDiagnosticsShowTheEffectivePolicy:
    @pytest.mark.parametrize("raw", _LEGACY_VALUES)
    def test_heat_summary_reports_the_internal_hysteresis(self, raw: dict) -> None:
        diag = build_consolidated_diagnostics(
            _fake_coordinator(_comfort_config_from_storage(raw))
        )
        assert diag["heat_protection_summary"]["hysteresis_c"] == HEAT_HYSTERESIS

    def test_heat_summary_stays_serializable(self) -> None:
        diag = build_consolidated_diagnostics(_fake_coordinator(ComfortConfig()))
        json.dumps(diag["heat_protection_summary"])

    def test_glare_diagnostics_use_the_decision_inputs_effective_minimum(self) -> None:
        """The coordinator publishes the glare minimum from the decision input's
        behavior (the single place the policy is resolved), not from storage."""
        source = (_ROOT / "coordinator.py").read_text(encoding="utf-8")
        assert '"glare_min_exposure_wm2": wdi.effective_behavior.glare_min_exposure_wm2' in source


# ---------------------------------------------------------------------------
# One definition of the policy
# ---------------------------------------------------------------------------

class TestSingleDefinition:
    def test_constants_hold_the_policy(self) -> None:
        from custom_components.smartshading import const

        assert const.GLARE_MIN_EXPOSURE_WM2 == GLARE_MIN
        assert const.HEAT_RELEASE_HYSTERESIS_C == HEAT_HYSTERESIS

    def test_behavior_config_defaults_come_from_the_constants(self) -> None:
        from custom_components.smartshading import const
        from custom_components.smartshading.models.behavior_config import BehaviorConfig

        defaults = BehaviorConfig()
        assert defaults.glare_min_exposure_wm2 == const.GLARE_MIN_EXPOSURE_WM2
        assert defaults.heat_hysteresis_c == const.HEAT_RELEASE_HYSTERESIS_C

    def test_no_user_facing_constants_remain(self) -> None:
        from custom_components.smartshading import const

        for name in (
            "CONF_GLARE_MIN_EXPOSURE_WM2",
            "CONF_HEAT_PROTECTION_HYSTERESIS_C",
            "GLARE_MIN_EXPOSURE_MAX_WM2",
            "HEAT_PROTECTION_HYSTERESIS_MAX_C",
            "DEFAULT_GLARE_MIN_EXPOSURE_WM2",
            "DEFAULT_HEAT_PROTECTION_HYSTERESIS_C",
        ):
            assert not hasattr(const, name), name
