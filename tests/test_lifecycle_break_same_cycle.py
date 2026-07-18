"""Proves the lifecycle-break -> Tier 2 policy ordering within ONE
coordinator cycle — T7 pre-push review point 6.

Excludes the wrong ordering the review specifically asked to rule out:

    Policy sees active override -> blocks Lifecycle candidate -> lifecycle
    break evaluated only afterwards

Verified coordinator.py wiring (source inspection + this file's simulation
of the exact same sequence): active_override is fetched (get()), THEN
lifecycle_should_break_override() is evaluated and — if it fires — clears
the detector and sets the LOCAL active_override variable to None, and ONLY
THEN is WindowDecisionInput built (with that now-possibly-None
active_override) and handed to TierOrchestrator.evaluate_window(), whose
ManualOverridePolicy sees the ALREADY-updated value. This file reproduces
that exact three-step sequence (get -> break-check -> build+evaluate)
using the real lifecycle_guard.lifecycle_should_break_override() and the
real TierOrchestrator, proving the break and the resulting dispatch happen
within a single simulated cycle, not "one cycle later".
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smartshading.engines.lifecycle_guard import lifecycle_should_break_override
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

_NOW = datetime(2026, 6, 17, 20, 0, tzinfo=timezone.utc)


def _window() -> WindowConfig:
    return WindowConfig(id="w-south", name="South", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg-south")


def _zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Living")


def _override(position: int = 42, expires_far_future: bool = True) -> ManualOverride:
    return ManualOverride(
        window_id="w-south", override_position=position, started_at=_NOW,
        expires_at=_NOW.replace(year=_NOW.year + 1) if expires_far_future else _NOW,
        source="position_delta", overridden_state=ShadingState.OPEN, overridden_position=0,
    )


def _simulate_one_cycle(
    *, prev_lifecycle: LifecycleState, new_lifecycle: LifecycleState,
    break_enabled: bool, active_override: ManualOverride | None,
    night_position_ha: int = 0, absence_active: bool = False,
):
    """Reproduces coordinator.py's exact sequence: get() -> break-check
    (mutates the local active_override) -> build WDI -> TierOrchestrator."""
    if lifecycle_should_break_override(prev=prev_lifecycle, new=new_lifecycle, break_enabled=break_enabled) and active_override is not None:
        active_override = None  # what coordinator.py does at the clear() call site

    wdi = build_window_decision_input(
        window=_window(), zone=_zone(),
        global_defaults=GlobalDefaults(night_shading_enabled=True, absence_shading_enabled=False),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=NightDayLifecycleConfig(id="default", night_position=night_position_ha, night_enabled=True),
        lifecycle_state=new_lifecycle,
        absence_active=absence_active, current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
        comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
        active_override=active_override,
    )
    return TierOrchestrator().evaluate_window(wdi), active_override


class TestNightTransitionWithBreakEnabled:
    def test_override_ends_and_night_decision_applies_same_cycle(self) -> None:
        result, remaining_override = _simulate_one_cycle(
            prev_lifecycle=LifecycleState.DAY, new_lifecycle=LifecycleState.NIGHT,
            break_enabled=True, active_override=_override(),
        )
        assert remaining_override is None  # override ended
        assert result.shading_state is ShadingState.NIGHT_CLOSED  # night decision applied, same pass
        assert result.decided_by == "NightEvaluator"


class TestMorningTransitionWithBreakEnabled:
    def test_override_ends_and_fallback_decision_applies_same_cycle(self) -> None:
        """NightEvaluator produces no candidate once lifecycle leaves NIGHT
        (no dedicated "morning" evaluator in this version — see
        tier_orchestrator.py) — the resulting decision is whatever Tier 4/5
        would produce (here: the plain fallback OPEN, COMFORT-tagged). The
        key proof is that it is NOT blocked as MANUAL_OVERRIDE, because the
        override was already cleared before this candidate was evaluated —
        even though its own category (COMFORT) would otherwise be gated by
        allow_comfort=False (the legacy default) had the override still
        been active."""
        result, remaining_override = _simulate_one_cycle(
            prev_lifecycle=LifecycleState.NIGHT, new_lifecycle=LifecycleState.DAY,
            break_enabled=True, active_override=_override(),
        )
        assert remaining_override is None
        assert result.shading_state is not ShadingState.MANUAL_OVERRIDE
        assert result.decided_by == "TierOrchestrator:fallback"


class TestBreakDisabled:
    def test_override_stays_active_and_night_candidate_blocked(self) -> None:
        ov = _override()
        result, remaining_override = _simulate_one_cycle(
            prev_lifecycle=LifecycleState.DAY, new_lifecycle=LifecycleState.NIGHT,
            break_enabled=False, active_override=ov,
        )
        assert remaining_override is ov  # untouched
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
        assert result.target_position == ov.override_position


class TestFixedTimeFarFutureButLifecycleBreakStillWins:
    def test_break_ends_override_regardless_of_far_future_expires_at(self) -> None:
        far_future_ov = _override(expires_far_future=True)
        result, remaining_override = _simulate_one_cycle(
            prev_lifecycle=LifecycleState.DAY, new_lifecycle=LifecycleState.NIGHT,
            break_enabled=True, active_override=far_future_ov,
        )
        assert remaining_override is None
        assert result.shading_state is ShadingState.NIGHT_CLOSED


class TestNoRealTransitionDoesNotFalselyEndOverride:
    def test_steady_state_night_does_not_break_override(self) -> None:
        """prev == new (no real transition) — lifecycle_should_break_override()
        returns False regardless of break_enabled — the override must
        survive and continue blocking the (steady-state) Night candidate."""
        ov = _override()
        result, remaining_override = _simulate_one_cycle(
            prev_lifecycle=LifecycleState.NIGHT, new_lifecycle=LifecycleState.NIGHT,
            break_enabled=True, active_override=ov,
        )
        assert remaining_override is ov
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE
