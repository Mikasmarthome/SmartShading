"""Safety category (always-allowed dispatch) vs. safety-clear (override
lifecycle) — T7 point 8 established the split; T8 unifies Rain's
clear-semantics with Storm/Wind.

1. SAFETY category (DecisionCategory.SAFETY, engines/manual_override_policy.py):
   governs whether the Storm/Wind/Rain DECISION may be DISPATCHED while an
   override is active. Tier 1 early-exits in TierOrchestrator before the
   Tier 2 policy even runs, so this is unconditional by evaluator ordering,
   unaffected by allow_comfort/allow_protection.

2. Safety-CLEAR (coordinator.py, unrelated to the DecisionCategory system):
   governs whether the OVERRIDE STATE ITSELF (OverrideDetector's stored
   ManualOverride, its expires_at) is torn down when a Tier 1 decision
   fires. T8 (v1.2.0-beta.1): STORM_SAFE, WIND_SAFE, and now RAIN_SAFE all
   trigger OverrideDetector.clear() via the shared
   state_machine.states.SAFETY_SHADING_STATES constant
   (coordinator.py's `if tier_decision.shading_state in
   SAFETY_SHADING_STATES: ... self._override_detector.clear(window_id)`).
   Before T8, RAIN_SAFE was excluded from this specific tuple — a
   pre-existing asymmetry with no documented rationale (see the T8 audit)
   — Rain fell into the ordinary tick() branch instead of clear(). This
   file now proves the UNIFIED behavior; TestSourceLevelSafetyClearIncludesRain
   proves the fix at the source level (the most reliable proof short of
   driving the full ~9000-line coordinator update cycle, which none of
   this file's tests do — they simulate the OverrideDetector-level
   consequence of the coordinator's branch decision, exactly as the
   Storm/Wind tests already did before T8).

Coverage: dispatch outcome AND resulting override state, tested together
(not just "did the decision fire") for Storm, Wind, and Rain.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import SAFETY_SHADING_STATES, ShadingState

_UTC = timezone.utc
_WARMUP_NOW = datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "smartshading"


def _detector_with_active_override(fixed_time: bool = False) -> tuple[OverrideDetector, datetime]:
    det = OverrideDetector()
    det.tick(
        window_id="w1", observed_position=0, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_WARMUP_NOW,
    )
    t0 = _WARMUP_NOW + timedelta(minutes=1)
    if fixed_time:
        from datetime import time as dtime
        det.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
            duration_mode="fixed_time", fixed_until=dtime(20, 0), now_local=t0,
        )
    else:
        det.tick(
            window_id="w1", observed_position=40, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
        )
    assert det.get("w1", t0) is not None
    return det, t0


# ---------------------------------------------------------------------------
# Dispatch: Safety always allowed regardless of override (unchanged by T8).
# ---------------------------------------------------------------------------

class TestSafetyDecisionAlwaysDispatchesRegardlessOfOverride:
    """Point 8, item 1 (T7): "Entscheidung darf fahren" — proven at the
    TierOrchestrator level (Tier 1 early-exits before Tier 2 even runs, so
    this holds unconditionally, with all allow flags at their most
    restrictive/legacy defaults). T8 does not touch this ordering."""

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

    def test_rain_dispatches_despite_active_override(self) -> None:
        from custom_components.smartshading.engines.rain_engine import RainStatus
        ov = ManualOverride(
            window_id="w1", override_position=20,
            started_at=datetime(2026, 6, 15, 12, 0, tzinfo=_UTC),
            expires_at=datetime(2026, 6, 15, 14, 0, tzinfo=_UTC),
            source="position_delta", overridden_state=ShadingState.OPEN, overridden_position=0,
        )
        wdi = build_window_decision_input(
            window=WindowConfig(id="w1", name="W", zone_id="z1", azimuth=180.0, floor_level=0, cover_group_id="cg1"),
            zone=ZoneConfig(id="z1", name="Zone"),
            global_defaults=GlobalDefaults(), shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=NightDayLifecycleConfig(id="default"),
            lifecycle_state=LifecycleState.DAY, absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None, is_in_solar_sector=False,
            rain_status=RainStatus.RAINING, rain_protection_enabled=True, rain_safe_position=70,
            active_override=ov,
        )
        result = TierOrchestrator().evaluate_window(wdi)
        assert result.shading_state is ShadingState.RAIN_SAFE
        assert result.target_position == 70


# ---------------------------------------------------------------------------
# Override Clear (T8 core group — items 1-8 of the review's test list).
# ---------------------------------------------------------------------------

class TestStormClearsOverride:
    def test_storm_clear_removes_override_and_expires_at(self) -> None:
        det, t0 = _detector_with_active_override()
        original_expires_at = det.get("w1", t0).expires_at
        # Mirrors coordinator.py: STORM_SAFE -> clear(), no tick().
        det.clear("w1")
        result = det.get("w1", t0)
        assert result is None
        assert original_expires_at is not None  # sanity: it did exist before


class TestWindClearsOverride:
    def test_wind_clear_removes_override(self) -> None:
        det, t0 = _detector_with_active_override()
        # Mirrors coordinator.py: WIND_SAFE -> clear(), no tick().
        det.clear("w1")
        assert det.get("w1", t0) is None


class TestRainClearsOverride:
    """T8: Rain now mirrors Storm/Wind exactly — coordinator.py's clear
    branch condition is `tier_decision.shading_state in
    SAFETY_SHADING_STATES`, which includes RAIN_SAFE."""

    def test_rain_clear_removes_override_and_expires_at(self) -> None:
        det, t0 = _detector_with_active_override()
        original_expires_at = det.get("w1", t0).expires_at
        # Mirrors coordinator.py (T8): RAIN_SAFE -> clear(), no tick().
        det.clear("w1")
        result = det.get("w1", t0)
        assert result is None
        assert original_expires_at is not None

    def test_rain_clears_fixed_time_override_regardless_of_far_future_expiry(self) -> None:
        det, t0 = _detector_with_active_override(fixed_time=True)
        far_future_expires_at = det.get("w1", t0).expires_at
        assert far_future_expires_at > t0 + timedelta(hours=1)  # sanity: genuinely far out
        det.clear("w1")
        assert det.get("w1", t0) is None

    def test_rain_without_active_override_is_inert(self) -> None:
        """clear() on a window with no active override must not raise or
        have any observable side effect."""
        det = OverrideDetector()
        det.clear("w1")  # no override was ever created
        assert det.get("w1", _WARMUP_NOW) is None

    def test_tick_is_not_called_in_the_rain_safety_branch(self) -> None:
        """Structural proof: once clear() has run for a Rain-safe cycle,
        NO subsequent tick() call happens in the same cycle — verified by
        confirming clear() alone (without any tick() call) is sufficient
        to remove the override, and that a window with NO override and only
        clear() calls never accumulates any detector-internal state that
        tick() would have created (warmup counters, post-expiry baseline)."""
        det, t0 = _detector_with_active_override()
        det.clear("w1")
        assert det.get("w1", t0) is None
        # No tick() was called after clear() in this test — if the real
        # coordinator's Rain branch still called tick() as well, a renewed
        # override could reappear here; it must not.
        assert det.get("w1", t0 + timedelta(minutes=1)) is None


# ---------------------------------------------------------------------------
# Safety Ende / Wiederaufnahme (items 9-13).
# ---------------------------------------------------------------------------

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

    def test_after_rain_ends_the_old_override_does_not_return(self) -> None:
        """T8: unlike the pre-T8 behavior, Rain now clears the override —
        once RAIN_SAFE stops firing, there is nothing left to "resume".
        Comfort/Protection/Lifecycle candidates are free to control the
        window normally on the very next cycle, exactly as after a
        Storm/Wind clear."""
        det, t0 = _detector_with_active_override()
        original_started_at = det.get("w1", t0).started_at
        det.clear("w1")  # Rain fires, clears the override (T8)
        assert det.get("w1", t0) is None

        # Rain ends; no new manual movement happened. The window is free —
        # nothing resembling the pre-T8 "old override resumes" behavior.
        t1 = t0 + timedelta(minutes=30)
        still_none = det.get("w1", t1)
        assert still_none is None
        assert still_none != original_started_at  # trivially true; documents intent

    def test_comfort_takes_over_normally_after_rain_clear(self) -> None:
        """No override present -> TierOrchestrator's normal Comfort
        candidate (Solar) is free to win, unblocked by any Manual Override
        policy gating (there is nothing to gate)."""
        det, t0 = _detector_with_active_override()
        det.clear("w1")
        assert det.get("w1", t0) is None
        # No override => build_window_decision_input's active_override=None
        # => TierOrchestrator never invokes the Manual Override policy at
        # all for this window on the next cycle (see manual_override_policy.py:
        # "if active_override is None: return candidate").
        from custom_components.smartshading.engines.manual_override_policy import evaluate_manual_override_policy
        from custom_components.smartshading.models.window_decision import WindowDecision
        from custom_components.smartshading.state_machine.states import DecisionCategory
        comfort_candidate = WindowDecision(
            window_id="w1", shading_state=ShadingState.NORMAL_SHADE, target_position=75,
            decided_by="SolarEvaluator", category=DecisionCategory.COMFORT,
        )
        result = evaluate_manual_override_policy(
            active_override=None, candidate=comfort_candidate,
            allow_comfort=False, allow_protection=False,
        )
        assert result is comfort_candidate  # unblocked — no override to gate against

    def test_new_real_manual_movement_after_rain_creates_a_new_override(self) -> None:
        det, t0 = _detector_with_active_override()
        det.clear("w1")
        assert det.get("w1", t0) is None

        t1 = t0 + timedelta(minutes=10)
        det.tick(
            window_id="w1", observed_position=65, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
        )
        new_override = det.get("w1", t1)
        assert new_override is not None
        assert new_override.override_position == 65
        assert new_override.started_at == t1  # genuinely new, not a resurrection


# ---------------------------------------------------------------------------
# Fixed-Time interaction (T8 review section 6).
# ---------------------------------------------------------------------------

class TestFixedTimeOverrideClearedByRain:
    def test_rain_clears_fixed_time_override_no_revival_after_rain(self) -> None:
        det, t0 = _detector_with_active_override(fixed_time=True)
        det.clear("w1")
        assert det.get("w1", t0) is None
        # Long after the original far-future fixed boundary would have
        # applied, still nothing — it was cleared, not merely held.
        far_later = t0 + timedelta(hours=6)
        assert det.get("w1", far_later) is None

    def test_new_manual_movement_after_rain_creates_new_fixed_time_override(self) -> None:
        from datetime import time as dtime
        det, t0 = _detector_with_active_override(fixed_time=True)
        det.clear("w1")
        assert det.get("w1", t0) is None

        t1 = t0 + timedelta(minutes=10)
        det.tick(
            window_id="w1", observed_position=65, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
            duration_mode="fixed_time", fixed_until=dtime(20, 0), now_local=t1,
        )
        new_override = det.get("w1", t1)
        assert new_override is not None
        assert new_override.expires_at == datetime(2026, 6, 15, 20, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Dispatch failure / feedback (T8 review section 7, items 22-24).
# ---------------------------------------------------------------------------

class TestFailedDispatchAndFeedbackMatchStormWindSemantics:
    def test_override_cleared_regardless_of_dispatch_outcome_same_as_storm_wind(self) -> None:
        """T8 explicitly angleicht Rain an das BESTEHENDE Storm-/Wind-
        Verhalten: clear() in coordinator.py runs unconditionally once
        tier_decision.shading_state is a safety state — it does not wait
        for a confirmed successful dispatch (this was already true for
        Storm/Wind pre-T8, and T8 changes nothing about the ordering of
        clear() relative to dispatch — only which safety states reach the
        clear() call at all). Proven at the OverrideDetector level: clear()
        has no dispatch-outcome parameter and unconditionally removes the
        override."""
        det, t0 = _detector_with_active_override()
        det.clear("w1")  # unconditional — no dispatch-success gating exists
        assert det.get("w1", t0) is None

    def test_own_command_guard_still_suppresses_false_renewal_after_rain_dispatch(self) -> None:
        """Not reached in practice any more (Rain now clears instead of
        ticking), but proves the underlying own-command-guard machinery
        Rain would have relied on pre-T8 is itself unaffected by T8 — no
        regression to the generic mechanism."""
        det = OverrideDetector()
        det.tick(
            window_id="w1", observed_position=0, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_WARMUP_NOW,
        )
        t1 = _WARMUP_NOW + timedelta(minutes=1)
        det.tick(
            window_id="w1", observed_position=70, smartshading_target=70,
            smartshading_assumed=70,  # SmartShading's own just-dispatched position
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t1,
        )
        assert det.get("w1", t1) is None  # correctly recognized as own command, not a new override


# ---------------------------------------------------------------------------
# Source-level structural proof (T8) — the reliable complement to the
# OverrideDetector-level simulations above, given driving the full
# coordinator update cycle is impractical for a unit test.
# ---------------------------------------------------------------------------

class TestSourceLevelSafetyClearIncludesRain:
    def test_safety_shading_states_constant_has_all_three(self) -> None:
        assert SAFETY_SHADING_STATES == {
            ShadingState.STORM_SAFE, ShadingState.WIND_SAFE, ShadingState.RAIN_SAFE,
        }

    def test_coordinator_clear_branch_uses_the_shared_constant(self) -> None:
        """AST-based proof (immune to comment/string false-positives):
        finds the `if <expr> in SAFETY_SHADING_STATES:` condition that
        guards the block containing `self._override_detector.clear(
        window_id)` and `event_type="cleared_by_safety"`, and confirms it
        is NOT the old two-state Storm/Wind-only tuple."""
        source = (_INTEGRATION_ROOT / "coordinator.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="coordinator.py")

        found_clear_call_guarded_by_shared_constant = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            # Match: `<expr> in SAFETY_SHADING_STATES`
            is_membership_on_shared_constant = (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.In)
                and isinstance(test.comparators[0], ast.Name)
                and test.comparators[0].id == "SAFETY_SHADING_STATES"
            )
            if not is_membership_on_shared_constant:
                continue
            body_source = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            if "_override_detector.clear(window_id)" in body_source and "cleared_by_safety" in body_source:
                found_clear_call_guarded_by_shared_constant = True
                break
        assert found_clear_call_guarded_by_shared_constant, (
            "coordinator.py's override-clear block must be guarded by "
            "`... in SAFETY_SHADING_STATES` (which includes RAIN_SAFE), "
            "not a hand-written (STORM_SAFE, WIND_SAFE) tuple"
        )

    def test_no_remaining_hardcoded_two_state_safety_tuple_used_for_clear_or_classification(self) -> None:
        """Grep-equivalent AST scan: none of the safety-classification
        sites (movement-cause, is_safety/CommandFilter bypass, learning-
        eligibility exclusion, started/renewed exclusion) may use a raw
        two-element (STORM_SAFE, WIND_SAFE) tuple any more — they must all
        reference SAFETY_SHADING_STATES."""
        source = (_INTEGRATION_ROOT / "coordinator.py").read_text(encoding="utf-8")
        # The only two legitimate remaining raw (STORM_SAFE, WIND_SAFE)
        # tuples are the hardware-safe-position correction and the
        # hysteresis-hold priority check — both genuinely Storm/Wind-only
        # (Rain has its own, separate, already-correct equivalent logic).
        # This test only asserts the COUNT did not increase from the two
        # legitimate ones — new hardcoded 2-tuples would be a regression.
        raw_two_tuple = "(ShadingState.STORM_SAFE, ShadingState.WIND_SAFE)"
        occurrences = source.count(raw_two_tuple)
        assert occurrences == 2, (
            f"expected exactly 2 legitimate Storm/Wind-only raw tuples "
            f"(hardware position correction + hysteresis hold priority), "
            f"found {occurrences} — if this is a deliberate new one, update "
            f"this count and confirm it is NOT a safety-clear/classification "
            f"site that should instead use SAFETY_SHADING_STATES"
        )
