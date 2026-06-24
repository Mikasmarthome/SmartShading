"""Zone execution configuration — the two central per-zone control flags.

SmartShading exposes exactly two zone controls: Learning Mode
(learning_enabled) and Active Control (active_control_enabled).

These two flags are intentionally independent so that all four
combinations are supported — see the table below.

DEFAULT DESIGN INTENT
---------------------
  learning_enabled       = True   (safe: SmartShading observes and learns)
  active_control_enabled = False  (safe: no covers move until opt-in)

After first install, SmartShading immediately starts building its
understanding of the house (learning) and generating recommendations,
but never moves a cover automatically until the user explicitly enables
active control. This keeps a low barrier to
install and a high barrier to actuation.

COMBINATION TABLE
-----------------
  learning_enabled | active_control_enabled | Behaviour
  ---------------- | ---------------------- | ---------
  False            | False                  | Zone inactive — no learning, no commands
  True             | False   [DEFAULT]       | Learn + recommend + shadow; no cover movement
  True             | True                   | Full: learn + recommend + move covers + bounded experiments
  False            | True                   | Rule-based deterministic control only, no learning/adaptation

The (False, True) combination is explicitly valid: SmartShading
controls covers using only the configured/default values, without the
Learning Engine or Adaptive Profile. Useful for users who want
deterministic, non-adaptive control.
"""
from __future__ import annotations

from dataclasses import dataclass

from .runtime_mode import RuntimeMode, RuntimeModeAuthority, derive_authority


@dataclass(frozen=True)
class ZoneExecutionConfig:
    """Per-zone execution mode flags — the two central zone controls.

    Both fields default to the safe post-install experience:
    learning on, active control off.
    """

    learning_enabled: bool = True
    """Learning Mode (LE 2.0 master authority; UI label "Lernmodus").

    When True, SmartShading evaluates decisions and outcomes, learns thermal
    responses and user preferences, generates shadow proposals, prepares
    possible improvements, and — only together with active_control_enabled and
    all safety/eligibility gates — may run strictly-bounded learning experiments
    (P7) and, later, validated adoption (P8).

    When False:
      - No Learning Store writes (transitions, overrides, outcomes, snapshots)
      - No PendingOutcome creation or resolution
      - No Learning Pipeline / model updates
      - No new shadow proposals
      - No new learning experiments; a running experiment is logically aborted
      - No LE 2.0 thermal adaptive authority and no experiment-adopted targets
      - _NEUTRAL_ADAPTIVE_PROFILE is always used
      - Stored learned data / shadow / experiment history are preserved
      - Rule-based deterministic evaluation (TierOrchestrator, StateGuard) still
        runs normally and may control covers when active_control_enabled
    """

    active_control_enabled: bool = False
    """Active Control (UI label "Aktive Steuerung").

    When True, SmartShading may issue cover.set_cover_position service calls,
    subject to CommandFilter, StateGuard, Safety, and ExecutionMode checks.

    When False:
      - ExecutionMode is always RECOMMENDATION_ONLY for this zone
      - CommandFilter blocks all commands (BLOCKED_RECOMMENDATION_ONLY)
      - No HA service calls are sent; no cover moves
      - Decisions and target positions are computed and available in diagnostics

    active_control_enabled=True does NOT guarantee a cover command is sent.
    CommandFilter, StateGuard, manual override, and cover availability checks
    all apply. It only means that allowed commands are dispatched.

    A real bounded learning experiment requires BOTH learning_enabled and
    active_control_enabled to be True (plus reliable feedback and every P7
    eligibility gate).  There is no separate experiments flag.
    """

    @property
    def runtime_mode(self) -> RuntimeMode:
        """The single derived runtime mode for this zone (central authority)."""
        return self.authority.mode

    @property
    def authority(self) -> RuntimeModeAuthority:
        """The full capability authority derived from the two controls.

        Subsystems must read capability properties from here instead of
        recombining the two flags independently.
        """
        return derive_authority(self.learning_enabled, self.active_control_enabled)
