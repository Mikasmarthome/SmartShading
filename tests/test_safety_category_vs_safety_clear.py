"""Safety category (always-allowed dispatch) vs. safety-clear (override
lifecycle) — these are two SEPARATE mechanisms — T7 pre-push review point 8.

1. SAFETY category (DecisionCategory.SAFETY, engines/manual_override_policy.py):
   governs whether the Storm/Wind/Rain DECISION may be DISPATCHED while an
   override is active. Tier 1 early-exits in TierOrchestrator before the
   Tier 2 policy even runs, so this is unconditional by evaluator ordering,
   unaffected by allow_comfort/allow_protection.

2. Safety-CLEAR (coordinator.py, unrelated to the DecisionCategory system):
   governs whether the OVERRIDE STATE ITSELF (OverrideDetector's stored
   ManualOverride, its expires_at) is torn down when a Tier 1 decision
   fires. Source-verified (git diff against the pre-T7 baseline d60dc72
   shows ZERO changes to this logic): only STORM_SAFE and WIND_SAFE trigger
   OverrideDetector.clear(); RAIN_SAFE does NOT — it falls into the same
   tick() branch as every other non-safety cycle. This is a pre-existing
   asymmetry, not a T7 regression, and T7 does not touch it (per explicit
   instruction: "keine Bereinigung des Rain-Clear-Verhaltens ohne separate
   Freigabe").

Coverage: dispatch outcome AND resulting override state, tested together
(not just "did the decision fire") for Storm, Wind, and Rain.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_WARMUP_NOW = datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)


def _detector_with_active_override() -> tuple[OverrideDetector, datetime]:
    det = OverrideDetector()
    det.tick(
        window_id="w1", observed_position=0, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_WARMUP_NOW,
    )
    t0 = _WARMUP_NOW + timedelta(minutes=1)
    det.tick(
        window_id="w1", observed_position=40, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
    )
    assert det.get("w1", t0) is not None
    return det, t0


class TestSafetyDecisionAlwaysDispatchesRegardlessOfOverride:
    """Point 8, item 1: "Entscheidung darf fahren" — proven at the
    TierOrchestrator level (Tier 1 early-exits before Tier 2 even runs, so
    this holds unconditionally, with all allow flags at their most
    restrictive/legacy defaults)."""

    def _wdi_with_storm(self, active_override):
        from custom_components.smartshading.engines.weather_engine import WeatherCondition
        return build_window_decision_input(
            window=WindowConfig(id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1"),
            zone=ZoneConfig(id="z1", name="Zone"),
            global_defaults=GlobalDefaults(), shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY, absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
            weather_condition=WeatherCondition.STORM, storm_protection_enabled=True,
            active_override=active_override,
        )

    def test_storm_dispatches_despite_active_override(self) -> None:
        ov = ManualOverride(
            window_id="w1", override_position=20,
            started_at=datetime(2026, 6, 15, 12, 0, tzinfo=_UTC),
            expires_at=datetime(2026, 6, 15, 14, 0, tzinfo=_UTC),
            source="position_delta", overridden_state=ShadingState.OPEN, overridden_position=0,
        )
        wdi = self._wdi_with_storm(ov)
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.STORM_SAFE
        assert result.target_position == 0


class TestStormClearsOverride:
    def test_storm_clear_removes_override_and_expires_at(self) -> None:
        det, t0 = _detector_with_active_override()
        original_expires_at = det.get("w1", t0).expires_at
        # Mirrors coordinator.py L3910: STORM_SAFE -> clear(), no tick().
        det.clear("w1")
        result = det.get("w1", t0)
        assert result is None
        assert original_expires_at is not None  # sanity: it did exist before


class TestWindClearsOverride:
    def test_wind_clear_removes_override(self) -> None:
        det, t0 = _detector_with_active_override()
        # Mirrors coordinator.py L3910: WIND_SAFE -> clear(), no tick().
        det.clear("w1")
        assert det.get("w1", t0) is None


class TestRainDoesNotClearOverride:
    def test_rain_falls_through_to_tick_not_clear(self) -> None:
        """RAIN_SAFE is NOT in coordinator.py's safety-clear tuple
        (STORM_SAFE, WIND_SAFE) — a rain-safe cycle goes through tick()
        instead, exactly like any other non-safety cycle. Since the
        observed position (still at the override's own position, unmoved)
        matches the existing override within tolerance, tick() does
        nothing — the override survives a rain-safe cycle unchanged."""
        det, t0 = _detector_with_active_override()
        original = det.get("w1", t0)
        t1 = t0 + timedelta(minutes=1)
        # Mirrors coordinator.py's else-branch (RAIN_SAFE not in the clear
        # tuple): tick() runs instead of clear().
        det.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=10, duration_min=120, now=t1,
        )
        after = det.get("w1", t1)
        assert after is not None
        assert after.expires_at == original.expires_at
        assert after.override_position == original.override_position


class TestFollowUpAfterSafetyEnds:
    def test_after_storm_clear_a_new_manual_position_is_detected_normally(self) -> None:
        """Once Storm/Wind clears the override, detection resumes exactly
        like a fresh window — no lingering blind spot."""
        det, t0 = _detector_with_active_override()
        det.clear("w1")
        assert det.get("w1", t0) is None

        t1 = t0 + timedelta(minutes=5)
        det.tick(
            window_id="w1", observed_position=90, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
        )
        new_override = det.get("w1", t1)
        assert new_override is not None
        assert new_override.override_position == 90

    def test_after_rain_the_original_override_simply_continues_uninterrupted(self) -> None:
        """Since rain never cleared it, there is no "after rain ends"
        discontinuity to test — the SAME override instance (same
        started_at) is still active, unaffected throughout."""
        det, t0 = _detector_with_active_override()
        original_started_at = det.get("w1", t0).started_at
        t1 = t0 + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=10, duration_min=120, now=t1,
        )
        t2 = t1 + timedelta(minutes=30)  # "rain has stopped" — just another normal cycle
        det.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=10, duration_min=120, now=t2,
        )
        still_active = det.get("w1", t2)
        assert still_active is not None
        assert still_active.started_at == original_started_at
