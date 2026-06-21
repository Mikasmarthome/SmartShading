"""TierOrchestrator — wires the Tier 1-5 evaluation pipeline for one window.

Responsibility: call each evaluator tier in the correct order, enforce the
early-exit rule for Tier 1 and Tier 3, collect Tier 4 Protection Floors,
and delegate position arbitration to PositionResolver.

Tier order:
  Tier 1 — Safety Guards          StormEvaluator, WindEvaluator       → early exit
  Tier 2 — Manual Override        ManualOverrideEvaluator             → early exit
  Tier 3 — Lifecycle Phase Gate   NightEvaluator                      → early exit
  Tier 4 — Protection Floors      AbsenceEvaluator, HeatEvaluator, GlareEvaluator
  Tier 5 — Comfort Pipeline       SolarEvaluator
  PositionResolver                max(tier4 floors, tier5)
  Fallback                        OPEN (no evaluator active)

Tier 1 currently contains Storm Protection and Wind Protection only.
Frost Protection is deliberately excluded until a Cover-Type model exists
in WindowConfig / CoverCapability (see state_machine/states.py for details).

Tier 2 contains Manual Override detection.  Override detection (position
delta comparison) lives in OverrideDetector (engines/override_detector.py);
this evaluator only acts as the pipeline gate.

Invariants:
  - INV-18: evaluators receive a pre-resolved WindowDecisionInput; no config
    traversal or HA access happens inside this class.
  - All evaluators are stateless; instances are created once in __init__ and
    reused across calls.
  - evaluate_window() always returns a WindowDecision — never None.
    The caller (Coordinator) always receives a concrete decision.

Scope:
  - No Coordinator dependency.
  - No StateGuard dependency (StateGuard is wired by the Coordinator).
  - No HA imports.
  - No config resolution.
"""
from __future__ import annotations

from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import ShadingState
from .absence_evaluator import AbsenceEvaluator
from .glare_evaluator import GlareEvaluator
from .heat_evaluator import HeatEvaluator
from .manual_override_evaluator import ManualOverrideEvaluator
from .night_evaluator import NightEvaluator
from .position_resolver import PositionResolver
from .solar_evaluator import SolarEvaluator
from .storm_evaluator import StormEvaluator
from .wind_evaluator import WindEvaluator

_OPEN_POSITION = 0  # internal convention: 0 = open, 100 = fully shaded


class TierOrchestrator:
    """Orchestrates the Tier 1-5 evaluation pipeline for one window per cycle.

    Usage:
        orchestrator = TierOrchestrator()
        decision = orchestrator.evaluate_window(wdi)
    """

    def __init__(self) -> None:
        # Tier 1 — Safety Guards
        self._storm = StormEvaluator()
        self._wind = WindEvaluator()
        # Tier 2 — Manual Override
        self._manual_override = ManualOverrideEvaluator()
        # Tier 3 — Lifecycle Phase Gate
        self._night = NightEvaluator()
        # Tier 4 — Protection Floors
        self._absence = AbsenceEvaluator()
        self._heat = HeatEvaluator()
        self._glare = GlareEvaluator()
        # Tier 5 — Comfort Pipeline
        self._solar = SolarEvaluator()

    def evaluate_window(self, wdi: WindowDecisionInput) -> WindowDecision:
        """Evaluate one window and return a concrete WindowDecision.

        The returned decision encodes what the window should do — position,
        shading state, and which evaluator decided it.  The Coordinator applies
        StateGuard after this call to suppress rapid state changes.

        Args:
            wdi: Pre-resolved runtime contract for this window (INV-18).

        Returns:
            A WindowDecision.  Never None — falls back to OPEN if no tier fires.
        """
        # --- Tier 1: Safety Guards (sequential early-exit, highest priority) --
        # Storm is checked before Wind: STORM_SAFE > WIND_SAFE in priority order.
        # If storm fires, wind is never consulted (correct and more efficient).
        storm_result = self._storm.evaluate(wdi)
        if storm_result is not None:
            return storm_result

        wind_result = self._wind.evaluate(wdi)
        if wind_result is not None:
            return wind_result

        # --- Tier 2: Manual Override (early exit) -----------------------------
        override_result = self._manual_override.evaluate(wdi)
        if override_result is not None:
            return override_result

        # --- Tier 3: Lifecycle Phase Gate (early exit) ------------------------
        night_result = self._night.evaluate(wdi)
        if night_result is not None:
            return night_result

        # --- Tier 4: Protection Floors (all run, positions compared) ----------
        tier4_results: list[WindowDecision | None] = [
            self._absence.evaluate(wdi),
            self._heat.evaluate(wdi),
            self._glare.evaluate(wdi),
        ]

        # --- Tier 5: Comfort Pipeline -----------------------------------------
        tier5_result = self._solar.evaluate(wdi)

        # --- Position arbitration ---------------------------------------------
        winner = PositionResolver.resolve([*tier4_results, tier5_result])
        if winner is not None:
            return winner

        # --- Fallback: OPEN ---------------------------------------------------
        return WindowDecision(
            window_id=wdi.window_config.id,
            shading_state=ShadingState.OPEN,
            target_position=_OPEN_POSITION,
            decided_by="TierOrchestrator:fallback",
        )
