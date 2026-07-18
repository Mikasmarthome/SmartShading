"""Full-cycle proof: an ALLOWED Protection/Comfort passthrough dispatch
does not clear or renew the active override, and a later, still-disallowed
category continues to be blocked — T7 pre-push review point 9.

Combines TierOrchestrator (policy decision) with OverrideDetector.tick()
(own-command-guard) across multiple simulated cycles, exactly mirroring
what coordinator.py does: policy decides -> (assumed) dispatch -> next
cycle's tick() call with smartshading_assumed reflecting that dispatch.
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
_NOW = datetime(2026, 6, 17, 14, 0, tzinfo=_UTC)


def _window() -> WindowConfig:
    return WindowConfig(id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1")


def _zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Zone")


def _override(position: int = 20) -> ManualOverride:
    return ManualOverride(
        window_id="w1", override_position=position, started_at=_NOW,
        expires_at=_NOW + timedelta(hours=2), source="position_delta",
        overridden_state=ShadingState.OPEN, overridden_position=0,
    )


def _absence_wdi(active_override, allow_protection: bool, allow_comfort: bool = False):
    return build_window_decision_input(
        window=_window(), zone=_zone(),
        global_defaults=GlobalDefaults(absence_shading_enabled=True, absence_position=30),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=NightDayLifecycleConfig(id="default"),
        lifecycle_state=LifecycleState.DAY, absence_active=True,
        current_shading_state=ShadingState.MANUAL_OVERRIDE,
        outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
        active_override=active_override,
        override_allow_protection_actions=allow_protection,
        override_allow_comfort_actions=allow_comfort,
    )


class TestProtectionPassthroughFullCycle:
    def test_protection_wins_dispatches_and_override_survives_unchanged(self) -> None:
        override = _override(20)
        det = OverrideDetector()
        # Warmup + establish the active override in the detector too, so
        # its own state (expires_at) can be tracked across "cycles".
        det.tick(window_id="w1", observed_position=0, smartshading_target=0,
                 prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_NOW - timedelta(minutes=5))
        det.tick(window_id="w1", observed_position=20, smartshading_target=0,
                 prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_NOW - timedelta(minutes=4))
        original = det.get("w1", _NOW)
        assert original is not None

        # Cycle 1: policy allows Protection (Absence) through.
        wdi = _absence_wdi(original, allow_protection=True)
        decision = TierOrchestrator().evaluate_window(wdi)
        assert decision.shading_state is ShadingState.ABSENCE_CLOSED
        assert decision.target_position == 70  # HA 30 -> internal 70

        # Coordinator dispatches to 70; next cycle's tick() sees the cover
        # having settled there, matching SmartShading's own last command.
        t1 = _NOW + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=70, smartshading_target=70,
            smartshading_assumed=70, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t1,
        )
        after_dispatch = det.get("w1", t1)
        assert after_dispatch is not None
        assert after_dispatch.override_position == original.override_position  # unchanged
        assert after_dispatch.expires_at == original.expires_at  # unchanged
        assert after_dispatch.started_at == original.started_at  # same instance, not renewed

        # Cycle 2: a Comfort candidate (Solar) would want to fire, but
        # allow_comfort is still False -> must still be blocked.
        wdi2 = build_window_decision_input(
            window=_window(), zone=_zone(),
            global_defaults=GlobalDefaults(absence_shading_enabled=False),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY, absence_active=False,
            current_shading_state=ShadingState.MANUAL_OVERRIDE,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
            active_override=after_dispatch, override_allow_protection_actions=True, override_allow_comfort_actions=False,
        )
        decision2 = TierOrchestrator().evaluate_window(wdi2)
        assert decision2.shading_state is ShadingState.MANUAL_OVERRIDE
        assert decision2.target_position == after_dispatch.override_position


class TestComfortPassthroughFullCycle:
    def test_comfort_wins_dispatches_and_override_survives_unchanged(self) -> None:
        override = _override(20)
        det = OverrideDetector()
        det.tick(window_id="w1", observed_position=0, smartshading_target=0,
                 prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_NOW - timedelta(minutes=5))
        det.tick(window_id="w1", observed_position=20, smartshading_target=0,
                 prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_NOW - timedelta(minutes=4))
        original = det.get("w1", _NOW)
        assert original is not None

        wdi = _absence_wdi(original, allow_protection=False, allow_comfort=True)
        # No absence active here -> fall through to fallback OPEN (COMFORT).
        wdi = build_window_decision_input(
            window=_window(), zone=_zone(),
            global_defaults=GlobalDefaults(absence_shading_enabled=False),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY, absence_active=False,
            current_shading_state=ShadingState.MANUAL_OVERRIDE,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
            comfort_config=ComfortConfig(heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False),
            active_override=original, override_allow_comfort_actions=True, override_allow_protection_actions=False,
        )
        decision = TierOrchestrator().evaluate_window(wdi)
        assert decision.decided_by == "TierOrchestrator:fallback"
        assert decision.target_position == 0

        t1 = _NOW + timedelta(minutes=2)
        det.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            smartshading_assumed=0, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t1,
        )
        after_dispatch = det.get("w1", t1)
        assert after_dispatch is not None
        assert after_dispatch.override_position == original.override_position
        assert after_dispatch.expires_at == original.expires_at

        # A subsequent Protection candidate (Absence) at allow_protection=False
        # must still be blocked.
        wdi2 = _absence_wdi(after_dispatch, allow_protection=False, allow_comfort=True)
        decision2 = TierOrchestrator().evaluate_window(wdi2)
        assert decision2.shading_state is ShadingState.MANUAL_OVERRIDE


class TestNoOpAllowedCandidateDoesNotTouchOverride:
    def test_allowed_candidate_with_no_actual_position_change_is_inert(self) -> None:
        """An allowed candidate whose target position already equals the
        cover's current (override) position requires no real movement —
        tick() must not interpret this as anything other than "matches the
        override position" (the existing-override branch's own delta check
        against existing.override_position, unaffected by allow flags)."""
        det = OverrideDetector()
        det.tick(window_id="w1", observed_position=0, smartshading_target=0,
                 prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_NOW - timedelta(minutes=5))
        det.tick(window_id="w1", observed_position=20, smartshading_target=0,
                 prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_NOW - timedelta(minutes=4))
        original = det.get("w1", _NOW)

        t1 = _NOW + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=20, smartshading_target=20,  # allowed candidate == override position already
            smartshading_assumed=None, prev_state=ShadingState.MANUAL_OVERRIDE,
            tolerance=10, duration_min=120, now=t1,
        )
        after = det.get("w1", t1)
        assert after is not None
        assert after.started_at == original.started_at  # not renewed
        assert after.expires_at == original.expires_at


class TestFailedDispatchDoesNotTouchOverride:
    def test_no_tick_call_means_no_state_change(self) -> None:
        """A failed dispatch means the cover never actually reaches the
        allowed target — the coordinator would simply observe the SAME
        (unchanged) position next cycle. Since that still matches the
        existing override's own position, tick()'s delta check against
        existing.override_position finds no change -> no renewal, no
        clear. The override is simply unaffected by a dispatch that
        never took physical effect."""
        det = OverrideDetector()
        det.tick(window_id="w1", observed_position=0, smartshading_target=0,
                 prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_NOW - timedelta(minutes=5))
        det.tick(window_id="w1", observed_position=20, smartshading_target=0,
                 prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_NOW - timedelta(minutes=4))
        original = det.get("w1", _NOW)

        t1 = _NOW + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=20,  # dispatch to 70 "failed" -> cover still at 20
            smartshading_target=70, smartshading_assumed=None,
            prev_state=ShadingState.MANUAL_OVERRIDE, tolerance=10, duration_min=120, now=t1,
        )
        after = det.get("w1", t1)
        assert after.started_at == original.started_at
        assert after.expires_at == original.expires_at
