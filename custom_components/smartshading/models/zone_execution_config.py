"""Zone execution configuration — per-zone control mode flags.

Introduced in Step 9G5a. Controls whether a zone participates in
observation/learning (observation_enabled) and whether active cover
control is enabled (active_control_enabled).

These two flags are intentionally independent so that all four
combinations are supported — see the table below.

DEFAULT DESIGN INTENT
---------------------
  observation_enabled  = True   (safe: SmartShading observes and learns)
  active_control_enabled = False  (safe: no covers move until opt-in)

After first install, SmartShading immediately starts building its
understanding of the house (observation) and generating recommendations,
but never moves a cover automatically until the user explicitly enables
active control. This keeps a low barrier to
install and a high barrier to actuation.

COMBINATION TABLE
-----------------
  observation_enabled | active_control_enabled | Behaviour
  -------------------- | ---------------------- | ---------
  False                | False                  | Zone inactive — no learning, no commands
  True                 | False   [DEFAULT]       | Observe + learn + recommend; no cover movement
  True                 | True                   | Full: observe + learn + recommend + move covers
  False                | True                   | Rule-based control only, no learning/adaptation

The (False, True) combination is explicitly valid: SmartShading
controls covers using only the configured/default values, without the
Learning Engine or Adaptive Profile. Useful for users who want
deterministic, non-adaptive control.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ZoneExecutionConfig:
    """Per-zone execution mode flags (Step 9G5a).

    Both fields default to the safe post-install experience:
    observation on, active control off.
    """

    observation_enabled: bool = True
    """When True, SmartShading collects learning/observation data, runs the
    Learning Engine (LE v1.0), applies the Adaptive Profile to BehaviorConfig,
    and generates diagnostic data.

    When False:
      - No Learning Store writes (transitions, overrides, outcomes, snapshots)
      - No PendingOutcome creation or resolution
      - No Learning Pipeline execution
      - No Adaptation Application (BehaviorConfig is used as resolved from config)
      - _NEUTRAL_ADAPTIVE_PROFILE is always used
      - Rule-based evaluation (TierOrchestrator, StateGuard) still runs normally
    """

    active_control_enabled: bool = False
    """When True, SmartShading may issue cover.set_cover_position service calls,
    subject to CommandFilter, StateGuard, Safety, and ExecutionMode checks.

    When False:
      - ExecutionMode is always RECOMMENDATION_ONLY for this zone
      - CommandFilter blocks all commands (BLOCKED_RECOMMENDATION_ONLY)
      - No HA service calls are sent; no cover moves
      - Decisions and target positions are computed and available in diagnostics

    active_control_enabled=True does NOT guarantee a cover command is sent.
    CommandFilter, StateGuard, manual override, and cover availability checks
    all apply. It only means that allowed commands are dispatched.
    """
