"""Morning evaluator — restart/missed-event reconciliation, reload/config
round-trip, and "no separate dispatch pipeline" structural guards.
v1.2.0-beta.3, B3-010.

Restart/missed-event semantics being verified here are pre-existing,
UNCHANGED LifecycleEngine behavior (engines/lifecycle_engine.py's own module
docstring, "Night carryover after restart") -- MorningEvaluator only ever
consumes whatever lifecycle_state the engine produces each cycle, so a
correct restart/reload reconciliation was already guaranteed by the engine;
these tests prove MorningEvaluator does not undermine that guarantee by
somehow reapplying a stale morning_position outside of a genuine MORNING
cycle ("keine historische Bewegung blind nachholen").
"""
from __future__ import annotations

import ast
from datetime import datetime, time, timezone
from pathlib import Path

from custom_components.smartshading.config_entry_data import (
    _lifecycle_config_from_storage,
    to_storage_dict,
    SmartShadingConfigEntryData,
)
from custom_components.smartshading.engines.exposure_engine import WindowExposure
from custom_components.smartshading.engines.lifecycle_engine import LifecycleEngine
from custom_components.smartshading.evaluators.morning_evaluator import MorningEvaluator
from custom_components.smartshading.evaluators.tier_orchestrator import TierOrchestrator
from custom_components.smartshading.models.comfort import ComfortConfig
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import (
    LifecycleScheduleMode,
    LifecycleState,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
)
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_NO_COMFORT = ComfortConfig(
    heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=False,
)
_BASE = Path(__file__).resolve().parent.parent / "custom_components" / "smartshading"


def _config() -> NightDayLifecycleConfig:
    return NightDayLifecycleConfig(
        id="default",
        night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=time(22, 0),
        night_position=0,
        morning_trigger=MorningTrigger.FIXED_TIME, morning_fixed_time=time(6, 30),
        morning_position=100,
    )


def _window() -> WindowConfig:
    return WindowConfig(id="w1", name="W", zone_id="z1", azimuth=180.0,
                         floor_level=0, cover_group_id="cg1")


def _zone() -> ZoneConfig:
    return ZoneConfig(id="z1", name="Zone")


def _decide(now: datetime, config: NightDayLifecycleConfig, *, prev: LifecycleState):
    """Real path: LifecycleEngine -> active_profile -> WDI -> TierOrchestrator."""
    engine = LifecycleEngine()
    state = engine.get_lifecycle_state(
        now, sun_elevation_deg=None, config=config, previous_lifecycle_state=prev,
    )
    profile = engine.active_profile(now, config)
    from dataclasses import replace
    effective_config = replace(
        config, night_position=profile.night_position, morning_position=profile.morning_position,
    )
    wdi = build_window_decision_input(
        window=_window(), zone=_zone(),
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=effective_config,
        lifecycle_state=state,
        absence_active=False,
        current_shading_state=ShadingState.NIGHT_CLOSED,
        outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
        is_in_solar_sector=False,
        comfort_config=_NO_COMFORT,
    )
    return state, TierOrchestrator().evaluate_window(wdi)


# ---------------------------------------------------------------------------
# Missed morning event: HA restarted well after the trigger already passed
# ---------------------------------------------------------------------------

class TestMissedMorningEventAfterRestart:
    def test_restart_long_after_morning_trigger_lands_in_day_with_correct_baseline(self):
        """Coordinator restarts at 10:00 (morning trigger was 06:30).
        previous_lifecycle_state resets to DAY on a fresh coordinator, and
        by 10:00 both night-carryover and the NIGHT->MORNING branch (which
        requires previous=NIGHT) are unreachable -> DAY directly. This is
        the correct "currently right" answer per the engine's own
        documented one-cycle-transition-event design -- no historical replay
        of the missed transition itself.

        B3-010 correction (reverted): MorningEvaluator fires ONLY for the
        one-cycle MORNING transition -- never for DAY, regardless of how DAY
        was entered (see evaluators/morning_evaluator.py; the missed
        morning_position is simply never applied this cycle, matching
        BehaviorConfig.morning_position's own documented "None = pipeline
        takes over immediately" semantics). The plain fallback OPEN (or a
        genuinely active protective tier, see the next test) is the correct,
        current decision -- not a re-application of the missed
        morning_position."""
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=_UTC)  # Monday 10:00
        state, decision = _decide(now, _config(), prev=LifecycleState.DAY)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "TierOrchestrator:fallback"
        assert decision.target_position == 0

    def test_restart_long_after_morning_trigger_with_partial_position_is_still_plain_fallback(self):
        """Same restart scenario, but with a configured PARTIAL morning
        position -- the missed morning_position (HA 70 / internal 30) is
        simply not applied; DAY still resolves to the plain fallback OPEN
        (HA 100 / internal 0), never a "catch-up" application of the missed
        value."""
        from dataclasses import replace as _dc_replace
        config = _dc_replace(_config(), morning_position=70)  # HA 70 -> internal 30
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=_UTC)
        state, decision = _decide(now, config, prev=LifecycleState.DAY)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "TierOrchestrator:fallback"
        assert decision.target_position == 0, (
            "the missed morning_position (30) must never be synthesized "
            "here -- only a live, currently-firing tier may produce a "
            "non-fallback target"
        )

    def test_restart_long_after_morning_trigger_with_active_heat_protection(self):
        """The same restart scenario, but heat protection is ALREADY required
        at restart time -- the reconstructed decision must be the protective
        target directly, not morning_position, and not a transient OPEN
        first."""
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=_UTC)
        engine = LifecycleEngine()
        config = _config()
        state = engine.get_lifecycle_state(
            now, sun_elevation_deg=None, config=config, previous_lifecycle_state=LifecycleState.DAY,
        )
        profile = engine.active_profile(now, config)
        from dataclasses import replace
        effective_config = replace(
            config, night_position=profile.night_position, morning_position=profile.morning_position,
        )
        wdi = build_window_decision_input(
            window=_window(), zone=_zone(),
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=effective_config,
            lifecycle_state=state,
            absence_active=False,
            current_shading_state=ShadingState.NIGHT_CLOSED,
            outdoor_temp_c=30.0, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=True,
            comfort_config=ComfortConfig(
                heat_protection_enabled=True, glare_protection_enabled=False,
                solar_gain_enabled=False,
            ),
        )
        decision = TierOrchestrator().evaluate_window(wdi)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "HeatEvaluator"
        assert decision.shading_state is not ShadingState.OPEN

    def test_restart_during_night_carryover_then_live_morning_transition_still_correct(self):
        """Two-cycle proof: (1) restart at 00:15 correctly bootstraps NIGHT
        via carryover detection (pre-existing engine behavior), (2) the SAME
        coordinator's next cycle at 06:30 correctly sees previous=NIGHT and
        produces a real MORNING transition -- MorningEvaluator fires
        normally once the engine's own state tracking has caught up, no
        special-casing needed."""
        restart_now = datetime(2026, 8, 3, 0, 15, 0, tzinfo=_UTC)
        state_1, decision_1 = _decide(restart_now, _config(), prev=LifecycleState.DAY)
        assert state_1 is LifecycleState.NIGHT
        assert decision_1.decided_by == "NightEvaluator"

        later_now = datetime(2026, 8, 3, 6, 30, 0, tzinfo=_UTC)
        state_2, decision_2 = _decide(later_now, _config(), prev=state_1)
        assert state_2 is LifecycleState.MORNING
        assert decision_2.decided_by == "MorningEvaluator"
        assert decision_2.target_position == 0  # HA 100 -> internal 0

    def test_no_historical_catch_up_move_is_ever_synthesized(self):
        """There is no code path in MorningEvaluator/TierOrchestrator that
        looks at "how long ago was the missed morning" or attempts to
        reconstruct a historical position -- confirmed structurally: the
        evaluator's only inputs are the CURRENT lifecycle_state and the
        CURRENT resolved morning_position, nothing time-delta-based."""
        source = (_BASE / "evaluators" / "morning_evaluator.py").read_text(encoding="utf-8")
        assert "timedelta" not in source
        assert "elapsed" not in source
        assert "missed" not in source.lower()


# ---------------------------------------------------------------------------
# Reload: a genuine config-entry reload (async_unload_entry then
# async_setup_entry builds a brand-new coordinator instance, discarding the
# old one's in-RAM state) is a DISTINCT scenario from both a process restart
# and a storage round-trip -- proven structurally below, then exercised the
# same way TestMissedMorningEventAfterRestart exercises the restart case.
# ---------------------------------------------------------------------------

class TestMissedMorningEventAfterReload:
    def test_lifecycle_state_is_never_loaded_from_storage_only_ram_default(self):
        """Structural proof that a reload cannot carry NIGHT/MORNING RAM
        state forward: SmartShadingCoordinator.__init__ sets _lifecycle_state
        unconditionally to LifecycleState.DAY -- there is no storage/Store
        read feeding it. Combined with the fact that async_unload_entry +
        async_setup_entry constructs a brand-new SmartShadingCoordinator
        object (the old one, with whatever _lifecycle_state it had reached
        live, e.g. NIGHT, is simply discarded), this confirms reload and
        restart reach the IDENTICAL initial condition for lifecycle
        purposes -- not because they are the same event, but because neither
        one persists this particular field, unlike morning_position/
        night_position themselves (which DO round-trip via config storage,
        proven separately by TestReloadPreservesMorningPositionFields)."""
        source = (_BASE / "coordinator.py").read_text(encoding="utf-8")
        assert "self._lifecycle_state: LifecycleState = LifecycleState.DAY" in source
        # No storage/store read anywhere near the assignment would look like
        # e.g. "self._lifecycle_state = stored.get(...)" or "= store.load()".
        assert "self._lifecycle_state = store" not in source
        assert "self._lifecycle_state = stored" not in source

    def test_reload_after_morning_trigger_already_passed_is_plain_fallback(self):
        """The reload case itself: a fresh coordinator instance (as reload
        produces) whose FIRST real cycle runs at 10:00 -- well after the
        06:30 morning trigger, with a configured PARTIAL morning position.
        previous_lifecycle_state is DAY (the fresh instance's unconditional
        RAM default, proven above), so this reaches DAY directly via the
        exact same real LifecycleEngine -> WDI -> TierOrchestrator path a
        live cycle would use. MorningEvaluator never fires for DAY, so the
        missed morning_position (30) is never synthesized here -- this is a
        pure-function-level sanity check only; the authoritative real
        reload proof (two genuine SmartShadingCoordinator instances, a real
        async_shutdown() teardown, and an actual dispatch call) lives in
        test_b3010_real_restart_reload_dispatch.py's
        TestRealReloadAfterMissedTriggerReachesDispatch -- a storage
        round-trip or a source-string search alone is not accepted as reload
        proof (see TestReloadPreservesMorningPositionFields for the
        config-value-only round-trip, a separate, narrower claim)."""
        from dataclasses import replace as _dc_replace
        config = _dc_replace(_config(), morning_position=70)  # HA 70 -> internal 30
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=_UTC)
        state, decision = _decide(now, config, prev=LifecycleState.DAY)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "TierOrchestrator:fallback"
        assert decision.target_position == 0

    def test_reload_after_morning_trigger_with_active_sun_protection(self):
        """Same reload scenario, but glare/solar protection is already
        required at reload time -- the reconstructed decision must be the
        protective target directly, not morning_position."""
        now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=_UTC)
        engine = LifecycleEngine()
        config = _config()
        state = engine.get_lifecycle_state(
            now, sun_elevation_deg=None, config=config, previous_lifecycle_state=LifecycleState.DAY,
        )
        profile = engine.active_profile(now, config)
        from dataclasses import replace
        effective_config = replace(
            config, night_position=profile.night_position, morning_position=profile.morning_position,
        )
        wdi = build_window_decision_input(
            window=_window(), zone=_zone(),
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=effective_config,
            lifecycle_state=state,
            absence_active=False,
            current_shading_state=ShadingState.NIGHT_CLOSED,
            outdoor_temp_c=None, indoor_temp_c=None,
            exposure=WindowExposure(
                window_id="w1", timestamp=now,
                sun_azimuth=180.0, sun_elevation=45.0,
                is_above_horizon=True, is_in_tolerance_window=True,
                azimuth_delta_deg=0.0, direct_radiation_factor=1.0,
                elevation_clipped=False,
                theoretical_exposure=500.0, learned_solar_impact_factor=1.0,
                seasonal_factor=1.0, effective_exposure=500.0,
            ),
            is_in_solar_sector=True,
            comfort_config=ComfortConfig(
                heat_protection_enabled=False, glare_protection_enabled=False, solar_gain_enabled=True,
            ),
        )
        decision = TierOrchestrator().evaluate_window(wdi)
        assert state is LifecycleState.DAY
        assert decision.decided_by == "SolarEvaluator"
        assert decision.shading_state is not ShadingState.OPEN


# ---------------------------------------------------------------------------
# Reload: config storage round-trip preserves the three B3-010 fields
# ---------------------------------------------------------------------------

class TestReloadPreservesMorningPositionFields:
    def test_storage_round_trip_preserves_shared_weekday_weekend_positions(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True,
            lifecycle_config=NightDayLifecycleConfig(
                id="default", schedule_mode=LifecycleScheduleMode.WEEKDAY_WEEKEND,
                morning_position=77,
                weekday_morning_position=33,
                weekend_morning_position=88,
            ),
        )
        stored = to_storage_dict(data)
        restored = _lifecycle_config_from_storage(stored["lifecycle_config"])
        assert restored.morning_position == 77
        assert restored.weekday_morning_position == 33
        assert restored.weekend_morning_position == 88

    def test_reload_does_not_require_a_migration_or_version_bump(self):
        """B3-010 confirmed no schema/version change is needed: morning_position
        et al. already existed in storage before this change (write side was
        already fully wired, per config_flow.py) -- only the runtime
        evaluation side was missing. A pre-existing stored config with these
        keys already present round-trips unchanged."""
        pre_existing_stored = {
            "id": "default",
            "morning_position": 55, "weekday_morning_position": 20,
            "weekend_morning_position": 90, "morning_enabled": True,
        }
        restored = _lifecycle_config_from_storage(pre_existing_stored)
        assert restored.morning_position == 55
        assert restored.weekday_morning_position == 20
        assert restored.weekend_morning_position == 90


# ---------------------------------------------------------------------------
# No separate Morning dispatch pipeline
# ---------------------------------------------------------------------------

class TestNoSeparateMorningDispatchPipeline:
    def test_morning_evaluator_module_has_no_ha_or_dispatch_imports(self):
        """Structural guard matching NightEvaluator's own documented scope
        (INV-18): MorningEvaluator must have zero Coordinator/HA/dispatch
        coupling -- it is a pure function of WindowDecisionInput, exactly
        like every other Tier 3-5 evaluator, not a new subsystem."""
        source = (_BASE / "evaluators" / "morning_evaluator.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
        assert not any("homeassistant" in m for m in imported_modules)
        assert not any("coordinator" in m for m in imported_modules)
        assert not any("dispatch" in m for m in imported_modules)
        assert "async" not in source  # no coroutine, no timer, no task

    def test_tier_orchestrator_evaluate_window_signature_unchanged(self):
        """evaluate_window() still takes exactly one argument (the WDI) and
        returns a plain WindowDecision -- B3-010 added a candidate INSIDE
        the existing single-window evaluation, not a new parallel entry
        point, queue, or batch API."""
        import inspect
        sig = inspect.signature(TierOrchestrator.evaluate_window)
        params = [p for p in sig.parameters if p != "self"]
        assert params == ["wdi"]

    def test_morning_decision_dispatches_through_the_same_generation_tracked_path(self):
        """Once TierOrchestrator returns a Morning-originated WindowDecision,
        nothing distinguishes it from any other decision for the purposes of
        dispatch/generation/preemption downstream (coordinator.py) -- it is
        a plain WindowDecision with category=LIFECYCLE, exactly the same
        shape NightEvaluator already produces and the existing dispatch/
        generation/preemption machinery (T22) already handles correctly."""
        now = datetime(2026, 8, 3, 6, 30, 0, tzinfo=_UTC)
        _, decision = _decide(now, _config(), prev=LifecycleState.NIGHT)
        night_decision_shape = _decide(
            datetime(2026, 8, 3, 22, 30, 0, tzinfo=_UTC), _config(), prev=LifecycleState.DAY,
        )[1]
        assert type(decision) is type(night_decision_shape)
        assert set(vars(decision).keys()) == set(vars(night_decision_shape).keys())
