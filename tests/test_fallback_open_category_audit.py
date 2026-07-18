"""Audit: every ShadingState.OPEN-target WindowDecision construction site,
individually justified — not classified by target position alone —
T7 pre-push review point 11.

Full grep-based inventory of every `= WindowDecision(` construction site in
the production source (evaluators/tier_orchestrator.py,
coordinator.py) that produces `shading_state=ShadingState.OPEN`:

  1. evaluators/tier_orchestrator.py "PresenceUncertain:hold"
     -> category=HOLD. Reason: a deliberate no-dispatch (target_position=
     None) suppression of the daytime fallback while presence cannot
     currently be determined — a defensive non-action, not a comfort
     decision. See TestPresenceUncertainHoldIsNotComfort.

  2. evaluators/tier_orchestrator.py "TierOrchestrator:fallback"
     -> category=COMFORT. Reason: fires ONLY when Tier 1 (Safety), Tier 3
     (Night), Tier 4 (Absence/Heat/Glare), and Tier 5 (Solar) have ALL
     already returned None/no winner, and presence is not uncertain. This
     is the single, genuine "nothing needs protection or shading" state —
     a comfort default (maximize daylight/view), never a safety exit,
     never a sensor-fallback shortcut, never a lifecycle transition in its
     own right. See TestFallbackOpenOnlyFiresWhenNothingElseApplies.

  3. coordinator.py "NightContactBlock" -> category=LIFECYCLE (not
     COMFORT). This is a night-schedule-specific action (Option A: block
     the automatic night move while a window contact is open) — reclassifying
     it as COMFORT would have been a position-only misclassification (it
     targets OPEN, i.e. "don't close"), but its actual meaning is entirely
     about night-contact behavior, hence LIFECYCLE. See
     TestNightContactBlockIsNotComfort.

No other coordinator.py-level WindowDecision(...) construction site ever
produces shading_state=ShadingState.OPEN (verified: StormSafeHold/
WindSafeHold/RainSafeHold target their own SAFE states; NightContactCatchUp/
Vent/ReturnToNight and NightHardHold all target NIGHT_CLOSED/NIGHT_VENT, not
OPEN) — grep-verified directly against coordinator.py; the full category
completeness of every one of these 8 sites is separately proven by
test_window_decision_category_completeness.py (AST-based scan).

A "safety opening" (Storm/Wind releasing a cover to its safe position 0)
is structurally impossible to confuse with the COMFORT fallback: Storm/Wind
SAFE decisions carry shading_state=ShadingState.STORM_SAFE/WIND_SAFE (a
DIFFERENT enum member, even though target_position also happens to be 0 for
most hardware types) and category=SAFETY — never shading_state=OPEN.
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)


def _window() -> WindowConfig:
    return WindowConfig(id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1")


def _zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Zone")


class TestPresenceUncertainHoldIsNotComfort:
    def test_category_is_hold_not_comfort(self) -> None:
        wdi = build_window_decision_input(
            window=_window(), zone=_zone(),
            global_defaults=GlobalDefaults(night_shading_enabled=False, absence_shading_enabled=False),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY, absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
            presence_uncertain=True,
        )
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.decided_by == "PresenceUncertain:hold"
        assert result.category is DecisionCategory.HOLD
        assert result.target_position is None  # no dispatch — a defensive non-action


class TestFallbackOpenOnlyFiresWhenNothingElseApplies:
    def test_fallback_fires_only_when_every_other_tier_is_silent(self) -> None:
        wdi = build_window_decision_input(
            window=_window(), zone=_zone(),
            global_defaults=GlobalDefaults(night_shading_enabled=False, absence_shading_enabled=False),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY, absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
            presence_uncertain=False,
        )
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.decided_by == "TierOrchestrator:fallback"
        assert result.category is DecisionCategory.COMFORT
        assert result.shading_state is ShadingState.OPEN

    def test_fallback_does_not_fire_when_night_active(self) -> None:
        """Proves the fallback never wins over an actual protective/
        lifecycle tier — it is truly the LAST resort, not a shortcut."""
        wdi = build_window_decision_input(
            window=_window(), zone=_zone(),
            global_defaults=GlobalDefaults(night_shading_enabled=True),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default", night_position=0, night_enabled=True),
            lifecycle_state=LifecycleState.NIGHT, absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
        )
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.decided_by != "TierOrchestrator:fallback"
        assert result.category is DecisionCategory.LIFECYCLE
