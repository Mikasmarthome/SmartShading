"""TierOrchestrator — wires the Tier 1-5 evaluation pipeline for one window.

Responsibility: call each evaluator tier in the correct order, enforce the
early-exit rule for Tier 1 and Tier 3, collect Tier 4 Protection Floors,
delegate position arbitration to PositionResolver, and apply the central
Manual Override policy gate.

Tier order:
  Tier 1  — Safety Guards          StormEvaluator, WindEvaluator, RainEvaluator → early exit
  Tier 3  — Lifecycle Phase Gate   NightEvaluator                      → early exit
  Tier 3  — Lifecycle Phase Gate   MorningEvaluator (candidate, NOT early exit — see below)
  Tier 4  — Protection Floors      AbsenceEvaluator, HeatEvaluator, GlareEvaluator
  Tier 5  — Comfort Pipeline       SolarEvaluator
  PositionResolver                 max(morning candidate, tier4 floors, tier5)
  Fallback                         OPEN / PresenceUncertain hold (no evaluator active)
  Tier 2  — Manual Override Policy ManualOverridePolicy (engines/manual_override_policy.py)

Tier 1 currently contains Storm Protection, Wind Protection and Rain Protection.
Frost Protection is deliberately excluded until a Cover-Type model exists
in WindowConfig / CoverCapability (see state_machine/states.py for details).

MorningEvaluator (v1.2.0-beta.3, B3-010) deliberately does NOT early-exit like
NightEvaluator: night protection always wins outright, but morning_position is
only correct when nothing more protective is required at that exact moment.
It is folded into the SAME PositionResolver.resolve() call as Tier 4/5 below,
so the existing max(target_position) rule picks a Heat/Glare/Absence floor or
an already-required Solar shade over morning_position automatically — no
second arbitration mechanism, no early return, no separate dispatch path.
While wdi.presence_uncertain is True, it is excluded from that pool (treated
as if it hadn't fired) ONLY when it would represent an opening-equivalent
move — for the same reason the daytime fallback OPEN already refuses to
actively open in that case (see the presence-uncertain branch below):
guessing "not absent" is riskiest exactly when presence cannot be confirmed.
A Morning target that would instead numerically CLOSE the window further
than wdi.current_position_internal (the Coordinator-resolved best-known
current position — reliable feedback, or an assumed position only when
trustworthy; None when genuinely unknown, the conservative fallback) is a
genuinely protective move and is NOT excluded — suppressing it too would
deny a legitimate protective improvement precisely when it is most needed.

Tier 2 — Manual Override Policy (v1.2.0-beta.1, T7 restructure):
  Tier 1 still runs FIRST and early-exits exactly as before — Safety always
  beats an active override, unchanged.

  Tiers 3-5 are now ALWAYS evaluated to produce a candidate WindowDecision
  (each one already carries its DecisionCategory), regardless of whether an
  override is active. This is safe because every Tier 3-5 evaluator (and
  PositionResolver) is a pure, stateless function of WindowDecisionInput —
  no HA access, no runtime-state mutation, no Learning writes, no timers, no
  dispatch (verified by the T7 pre-implementation side-effect audit; see
  each evaluator's own module docstring "Scope" section).

  The one true gate — "is this decision allowed to proceed given the
  currently active override, if any?" — is then applied ONCE, centrally, by
  evaluate_manual_override_policy(). No other tier or evaluator contains an
  `if active_override:` check; ManualOverrideEvaluator (evaluators/
  manual_override_evaluator.py) still exists as a small standalone/tested
  unit but is no longer called from this pipeline — evaluate_manual_override_policy()
  constructs the same MANUAL_OVERRIDE shape itself when blocking a candidate.

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

from ..engines.manual_override_policy import evaluate_manual_override_policy
from ..models.window_decision import WindowDecision
from ..models.window_decision_input import WindowDecisionInput
from ..state_machine.states import DecisionCategory, ShadingState
from .absence_evaluator import AbsenceEvaluator
from .glare_evaluator import GlareEvaluator
from .heat_evaluator import HeatEvaluator
from .morning_evaluator import MorningEvaluator
from .night_evaluator import NightEvaluator
from .position_resolver import PositionResolver
from .rain_evaluator import RainEvaluator
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
        self._rain = RainEvaluator()
        # Tier 3 — Lifecycle Phase Gate
        self._night = NightEvaluator()
        self._morning = MorningEvaluator()
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
        # Priority order: STORM_SAFE (1) > WIND_SAFE (2) > RAIN_SAFE (3).
        # Storm is checked first; if it fires, wind and rain are skipped.
        # Override-immune by ordering: this runs before the Tier 2 policy gate
        # below, so Safety always wins regardless of override state (unchanged).
        storm_result = self._storm.evaluate(wdi)
        if storm_result is not None:
            return storm_result

        wind_result = self._wind.evaluate(wdi)
        if wind_result is not None:
            return wind_result

        rain_result = self._rain.evaluate(wdi)
        if rain_result is not None:
            return rain_result

        # --- Tier 3: Lifecycle Phase Gate (early exit) ------------------------
        night_result = self._night.evaluate(wdi)
        if night_result is not None:
            candidate = night_result
        else:
            # --- Tier 3 (cont.): Morning candidate, NOT an early exit --------
            # See module docstring "MorningEvaluator" note above: folded into
            # the same PositionResolver pool as Tier 4/5 below.
            #
            # Direction-aware presence-uncertain exclusion (B3-010
            # correction, numeric): only an OPENING-equivalent Morning move
            # is excluded while presence is uncertain, matching the
            # daytime-fallback OPEN's own presence-uncertain caution — a
            # Morning target is NOT structurally guaranteed to be less
            # protective than the current position. A CLOSING move (target
            # more shaded than the window's actual current position) is a
            # genuine protective improvement and must not be suppressed.
            #
            # wdi.current_position_internal is the Coordinator-resolved,
            # already-internal-convention best-known current position
            # (reliable feedback, or an assumed position ONLY when
            # AssumedStateManager.is_position_trustworthy() — never a raw
            # guess). Comparing it numerically against morning_result's own
            # target_position (same internal convention, 0=open/100=shaded)
            # is the actual, magnitude-correct direction check — e.g. HA 50
            # -> HA 70 (internal 50 -> internal 30) is correctly OPENING
            # (target < current), not closing, regardless of the coarse
            # ShadingState.OPEN label both positions might share.
            #
            # None (genuinely unknown/untrustworthy position) is the
            # explicit conservative Unknown-path: `>` against None is
            # impossible, so is_closing_move is False and the Morning
            # candidate is suppressed exactly like the opening case —
            # never a numeric guess from missing data.
            morning_result = self._morning.evaluate(wdi)
            if morning_result is not None and wdi.presence_uncertain:
                is_closing_move = (
                    wdi.current_position_internal is not None
                    and morning_result.target_position is not None
                    and morning_result.target_position > wdi.current_position_internal
                )
                if not is_closing_move:
                    morning_result = None

            # --- Tier 4: Protection Floors (all run, positions compared) ------
            tier4_results: list[WindowDecision | None] = [
                self._absence.evaluate(wdi),
                self._heat.evaluate(wdi),
                self._glare.evaluate(wdi),
            ]

            # --- Tier 5: Comfort Pipeline ---------------------------------
            tier5_result = self._solar.evaluate(wdi)

            # --- Position arbitration ---------------------------------------
            winner = PositionResolver.resolve([morning_result, *tier4_results, tier5_result])
            if winner is not None:
                candidate = winner
            elif wdi.presence_uncertain:
                # --- Presence-uncertain hold (instead of the daytime fallback open) --
                # No safety / night / absence / heat / glare / solar candidate fired,
                # so the only remaining action would be the daytime fallback that opens
                # the cover fully.  If presence is configured but cannot currently be
                # determined (every presence entity unknown/unavailable, e.g. right
                # after a restart), do NOT actively open: absence might in fact be
                # active.  Hold the current position (target None → no dispatch) until
                # presence is known.  This only ever suppresses this non-protective
                # fallback open — every protective/required decision already returned
                # above.
                candidate = WindowDecision(
                    window_id=wdi.window_config.id,
                    shading_state=ShadingState.OPEN,
                    target_position=None,
                    decided_by="PresenceUncertain:hold",
                    category=DecisionCategory.HOLD,
                )
            else:
                # --- Fallback: OPEN ---------------------------------------------
                candidate = WindowDecision(
                    window_id=wdi.window_config.id,
                    shading_state=ShadingState.OPEN,
                    target_position=_OPEN_POSITION,
                    decided_by="TierOrchestrator:fallback",
                    category=DecisionCategory.COMFORT,
                )

        # --- Tier 2: Manual Override Policy (single central gate) -------------
        return evaluate_manual_override_policy(
            active_override=wdi.active_override,
            candidate=candidate,
            allow_comfort=wdi.effective_behavior.override_allow_comfort_actions,
            allow_protection=wdi.effective_behavior.override_allow_protection_actions,
            release_strategy=wdi.effective_behavior.override_release_strategy,
        )
