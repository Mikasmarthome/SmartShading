"""Tests for MorningEvaluator (Tier 3 Lifecycle Phase Gate counterpart to
NightEvaluator) — v1.2.0-beta.3, B3-010.

MorningEvaluator's contract:
  - MORNING + morning_position configured → WindowDecision(OPEN, morning_position)
  - Any other lifecycle state, with a still-pending Morning Reconciliation
    target for this window → WindowDecision(OPEN, that target) (B3-010
    Morning Reconciliation re-propose -- see TestMorningReconciliationRepropose)
  - Any other lifecycle state, no pending reconciliation target → None
  - morning_position is None (morning_enabled=False) and no pending
    reconciliation target → None

Unlike NightEvaluator, MorningEvaluator is NOT an early-exit tier — that
arbitration-participation contract (folded into the same PositionResolver
pool as Tier 4/5, excluded while presence is uncertain) is TierOrchestrator's
responsibility and is covered in test_tier_orchestrator.py, not here. This
file mirrors test_night_evaluator.py's scope precisely: MorningEvaluator in
isolation, with zero knowledge of any other tier.
"""
from __future__ import annotations

import pytest

from custom_components.smartshading.evaluators.morning_evaluator import MorningEvaluator
from custom_components.smartshading.models.config import GlobalDefaults, ShadePositionDefaults
from custom_components.smartshading.models.lifecycle import LifecycleState, NightDayLifecycleConfig
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.window_decision_input import build_window_decision_input
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def evaluator() -> MorningEvaluator:
    return MorningEvaluator()


@pytest.fixture()
def window() -> WindowConfig:
    return WindowConfig(
        id="window-south", name="South", zone_id="zone-1",
        azimuth=180.0, floor_level=0, cover_group_id="cg-south",
    )


@pytest.fixture()
def zone() -> ZoneConfig:
    return ZoneConfig(id="zone-1", name="Living Room")


def _wdi(
    window: WindowConfig,
    zone: ZoneConfig,
    lifecycle_state: LifecycleState,
    *,
    morning_position_ha: int = 100,   # HA convention → converted to internal in builder
    morning_enabled: bool = True,
):
    """Build a WindowDecisionInput with the given lifecycle state and morning config."""
    lifecycle_config = NightDayLifecycleConfig(
        id="default",
        morning_position=morning_position_ha,
        morning_enabled=morning_enabled,
    )
    return build_window_decision_input(
        window=window,
        zone=zone,
        global_defaults=GlobalDefaults(),
        shade_position_defaults=ShadePositionDefaults(),
        lifecycle_config=lifecycle_config,
        lifecycle_state=lifecycle_state,
        absence_active=False,
        current_shading_state=ShadingState.OPEN,
        outdoor_temp_c=None,
        indoor_temp_c=None,
        exposure=None,
        is_in_solar_sector=False,
    )


# ---------------------------------------------------------------------------
# Core contract: MORNING phase
# ---------------------------------------------------------------------------

class TestMorningEvaluatorMorningPhase:
    def test_morning_phase_returns_decision(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.MORNING)
        result = evaluator.evaluate(wdi)
        assert result is not None

    def test_morning_phase_returns_open_state(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.MORNING)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.OPEN

    def test_morning_phase_returns_correct_window_id(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.MORNING)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.window_id == window.id

    def test_morning_phase_target_position_comes_from_effective_behavior(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # morning_position=100 HA (fully open) -> 0 internal (fully open)
        wdi = _wdi(window, zone, LifecycleState.MORNING, morning_position_ha=100)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == wdi.effective_behavior.morning_position
        assert result.target_position == 0  # internal convention: 0=open, 100=shaded

    def test_morning_phase_partial_position(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # morning_position=70 HA (70% open) -> 30 internal (30% shaded)
        wdi = _wdi(window, zone, LifecycleState.MORNING, morning_position_ha=70)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 30

    def test_decided_by_is_morning_evaluator(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.MORNING)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.decided_by == "MorningEvaluator"

    def test_category_is_lifecycle(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """Must be tagged LIFECYCLE — the same category NightEvaluator uses —
        so an active Manual Override blocks it exactly like a Night decision
        (existing override_break_on_lifecycle semantics, unchanged)."""
        wdi = _wdi(window, zone, LifecycleState.MORNING)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.category is DecisionCategory.LIFECYCLE

    def test_target_tilt_is_none(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.MORNING)
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_tilt is None


# ---------------------------------------------------------------------------
# Core contract: DAY/NIGHT/EVENING all return None — MORNING is the ONLY
# state this evaluator produces a candidate for.
# ---------------------------------------------------------------------------

class TestMorningEvaluatorOtherStatesReturnNone:
    """MORNING is documented and implemented (engines/lifecycle_engine.py,
    BehaviorConfig.morning_position docstring: "target position ON THE
    MORNING TRANSITION (one-cycle event)") as a genuine one-cycle transition
    event, not a persisted state. MorningEvaluator must fire for exactly
    that one cycle and never again — an ordinary DAY cycle has no Morning
    candidate at all; the immediately-following Fallback/Open proposal
    dispatching a real second movement is instead solved at the throttling
    layer (engines/comfort_movement_hold.py's NON_PRIORITY_DECIDERS, not by
    turning morning_position into a permanent DAY-state candidate here)."""

    def test_day_phase_returns_none(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.DAY)
        assert evaluator.evaluate(wdi) is None

    def test_night_phase_returns_none(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        wdi = _wdi(window, zone, LifecycleState.NIGHT)
        assert evaluator.evaluate(wdi) is None

    def test_evening_phase_returns_none(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # EVENING is a documented placeholder state; MorningEvaluator must ignore it
        wdi = _wdi(window, zone, LifecycleState.EVENING)
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Core contract: morning position disabled
# ---------------------------------------------------------------------------

class TestMorningEvaluatorDisabled:
    def test_morning_disabled_returns_none(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        # morning_enabled=False -> build_window_decision_input sets morning_position=None
        wdi = _wdi(window, zone, LifecycleState.MORNING, morning_enabled=False)
        assert wdi.effective_behavior.morning_position is None
        assert evaluator.evaluate(wdi) is None


# ---------------------------------------------------------------------------
# Scope boundary: MorningEvaluator has zero knowledge of other tiers
# ---------------------------------------------------------------------------

class TestMorningEvaluatorScopeBoundary:
    def test_active_override_field_is_never_read(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """active_override is always None in MVP. MorningEvaluator must not
        read it — the orchestrator's central Tier 2 gate is responsible for
        checking Manual Override, not this evaluator."""
        wdi = _wdi(window, zone, LifecycleState.MORNING)
        assert wdi.active_override is None  # always None in MVP
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.OPEN

    def test_absence_active_does_not_affect_result(
        self, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """AbsenceEvaluator's domain (absence_active) must not influence
        MorningEvaluator — arbitration against Absence is TierOrchestrator's
        job, not this evaluator's."""
        evaluator = MorningEvaluator()
        lifecycle_config = NightDayLifecycleConfig(
            id="default", morning_position=100, morning_enabled=True,
        )
        wdi_absent = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=lifecycle_config,
            lifecycle_state=LifecycleState.MORNING,
            absence_active=True,        # <- AbsenceEvaluator's domain
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
        )
        result = evaluator.evaluate(wdi_absent)
        assert result is not None
        assert result.shading_state is ShadingState.OPEN

    def test_presence_uncertain_field_is_never_read(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """presence_uncertain exclusion is TierOrchestrator's job (it must
        exclude a fired MorningEvaluator result from the arbitration pool
        while presence is uncertain) — MorningEvaluator itself always
        produces its candidate regardless, exactly like every other tier
        evaluator."""
        lifecycle_config = NightDayLifecycleConfig(
            id="default", morning_position=100, morning_enabled=True,
        )
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=lifecycle_config,
            lifecycle_state=LifecycleState.MORNING,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            presence_uncertain=True,
        )
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.shading_state is ShadingState.OPEN


class TestMorningReconciliationRepropose:
    """B3-010 Morning Reconciliation: MorningEvaluator re-proposes the
    still-pending reconciliation target on DAY (and any non-MORNING)
    cycles, replacing the old "fires once, ComfortMovementHold throttles
    the rest" design."""

    def test_day_cycle_with_pending_target_reproposes_it(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        lifecycle_config = NightDayLifecycleConfig(id="default", morning_position=None, morning_enabled=False)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=lifecycle_config,
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            morning_reconciliation_pending_target_internal=30,
        )
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 30
        assert result.decided_by == "MorningEvaluator"
        assert result.category == DecisionCategory.LIFECYCLE

    def test_day_cycle_without_pending_target_returns_none(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        lifecycle_config = NightDayLifecycleConfig(id="default", morning_position=None, morning_enabled=False)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=lifecycle_config,
            lifecycle_state=LifecycleState.DAY,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            morning_reconciliation_pending_target_internal=None,
        )
        assert evaluator.evaluate(wdi) is None

    def test_morning_cycle_prefers_fresh_morning_position_over_pending_field(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """On the genuine MORNING transition cycle itself, the fresh
        configured morning_position wins -- the pending-reconciliation
        branch is only ever reached for non-MORNING cycles."""
        lifecycle_config = NightDayLifecycleConfig(id="default", morning_position=70, morning_enabled=True)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=lifecycle_config,
            lifecycle_state=LifecycleState.MORNING,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            morning_reconciliation_pending_target_internal=99,
        )
        result = evaluator.evaluate(wdi)
        assert result is not None
        # 70 HA -> internal 30, the freshly resolved morning_position, not
        # the stale-looking pending field.
        assert result.target_position == 30

    def test_night_cycle_with_pending_target_still_reproposes(
        self, evaluator: MorningEvaluator, window: WindowConfig, zone: ZoneConfig
    ) -> None:
        """The pending-reconciliation branch checks only "not MORNING", not
        specifically DAY -- NightEvaluator's own early-exit already wins
        arbitration for NIGHT regardless, so this is safe and exercises
        the actual condition precisely."""
        lifecycle_config = NightDayLifecycleConfig(id="default", morning_position=None, morning_enabled=False)
        wdi = build_window_decision_input(
            window=window, zone=zone,
            global_defaults=GlobalDefaults(),
            shade_position_defaults=ShadePositionDefaults(),
            lifecycle_config=lifecycle_config,
            lifecycle_state=LifecycleState.NIGHT,
            absence_active=False,
            current_shading_state=ShadingState.OPEN,
            outdoor_temp_c=None, indoor_temp_c=None, exposure=None,
            is_in_solar_sector=False,
            morning_reconciliation_pending_target_internal=30,
        )
        result = evaluator.evaluate(wdi)
        assert result is not None
        assert result.target_position == 30
