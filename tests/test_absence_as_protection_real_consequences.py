"""Real-consequence tests for classifying Absence as PROTECTION —
T7 pre-push review point 10.

With override_allow_protection_actions=True, an active manual override no
longer unconditionally blocks Absence — the same as Heat/Glare. This file
proves the consequence is genuine and consistent (not just Heat/Glare) and
covers the concrete scenarios named in the review: absence starting/ending
during an active override, a presence-reading flicker, protection-allowed-
comfort-blocked and the reverse.
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)


def _window() -> WindowConfig:
    return WindowConfig(id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1")


def _zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Zone")


def _override() -> ManualOverride:
    return ManualOverride(
        window_id="w1", override_position=20, started_at=_NOW, expires_at=_NOW.replace(hour=16),
        source="position_delta", overridden_state=ShadingState.OPEN, overridden_position=0,
    )


def _wdi(*, absence_active: bool, allow_protection: bool, allow_comfort: bool, presence_uncertain: bool = False, active_override=None):
    return build_window_decision_input(
        window=_window(), zone=_zone(),
        global_defaults=GlobalDefaults(absence_shading_enabled=True, absence_position=30),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY, absence_active=absence_active,
        current_shading_state=ShadingState.MANUAL_OVERRIDE if active_override else ShadingState.OPEN,
        outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
        comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
        active_override=active_override,
        override_allow_protection_actions=allow_protection,
        override_allow_comfort_actions=allow_comfort,
        presence_uncertain=presence_uncertain,
    )


class TestAbsenceBeginsDuringOverride:
    def test_protection_allowed_absence_fires_despite_override(self) -> None:
        wdi = _wdi(absence_active=True, allow_protection=True, allow_comfort=False, active_override=_override())
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.ABSENCE_CLOSED
        assert result.target_position == 70

    def test_protection_blocked_absence_does_not_fire(self) -> None:
        wdi = _wdi(absence_active=True, allow_protection=False, allow_comfort=False, active_override=_override())
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE


class TestAbsenceEndsDuringOverride:
    def test_absence_no_longer_active_falls_through_to_fallback(self) -> None:
        """Absence just ended (absence_active=False) — no Absence candidate
        exists any more; the fallback OPEN (COMFORT) is the only candidate,
        governed by allow_comfort, not allow_protection."""
        wdi = _wdi(absence_active=False, allow_protection=True, allow_comfort=False, active_override=_override())
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE  # comfort still blocked

        wdi2 = _wdi(absence_active=False, allow_protection=True, allow_comfort=True, active_override=_override())
        result2 = TierOrchestrator().evaluate_window(wdi2)
        assert result2.decided_by == "TierOrchestrator:fallback"  # now allowed via comfort


class TestPresenceFlickerToUncertain:
    def test_presence_uncertain_hold_still_blocked_even_with_protection_allowed(self) -> None:
        """A presence-reading flicker (all entities briefly unknown/
        unavailable) produces presence_uncertain=True -> the HOLD-tagged
        PresenceUncertain candidate, which is ALWAYS blocked while an
        override is active regardless of allow_protection/allow_comfort
        (see engines/manual_override_policy.py — HOLD is never auto-
        exempted, for legacy-parity reasons)."""
        wdi = _wdi(
            absence_active=False, allow_protection=True, allow_comfort=True,
            presence_uncertain=True, active_override=_override(),
        )
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE


class TestProtectionAllowedComfortBlocked:
    def test_absence_fires_solar_would_not_matter(self) -> None:
        wdi = _wdi(absence_active=True, allow_protection=True, allow_comfort=False, active_override=_override())
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.ABSENCE_CLOSED


class TestProtectionBlockedComfortAllowed:
    def test_absence_still_blocked_despite_comfort_allowed(self) -> None:
        """Absence (PROTECTION) winning candidate is blocked even though
        Comfort is allowed — the flags are category-specific, not a
        blanket "any allowance unlocks everything" switch."""
        wdi = _wdi(absence_active=True, allow_protection=False, allow_comfort=True, active_override=_override())
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.MANUAL_OVERRIDE


class TestNoActiveOverrideAbsenceUnaffected:
    def test_absence_behaves_normally_without_override(self) -> None:
        """Sanity: outside an active override, Absence fires exactly as
        before T7, regardless of the allow flags (they only matter when
        gating against an active override)."""
        wdi = _wdi(absence_active=True, allow_protection=False, allow_comfort=False, active_override=None)
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.ABSENCE_CLOSED
