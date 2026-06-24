"""Central runtime-mode authority — derived from the two zone controls.

SmartShading exposes exactly two user-facing zone controls: Learning Mode
(``learning_enabled``) and Active Control (``active_control_enabled``).  Every
runtime behaviour — learning writes, shadow evaluation, adaptive influence on
the real decision, cover dispatch, bounded experiments, outcome recording — is
a deterministic consequence of these two flags and must be derived from this
single authority.  No subsystem may re-interpret the two flags independently.

There is intentionally NO third user-facing "experiments" control.  Real
bounded learning experiments are a derived internal capability that is only
granted in :data:`RuntimeMode.ADAPTIVE` and then only when every existing P7
eligibility/safety gate is additionally satisfied.

The four modes
--------------
=================  ========  =======  =================
Learning Mode      Active    Mode
=================  ========  =======  =================
OFF                OFF       INACTIVE
ON                 OFF       SHADOW_ONLY
OFF                ON        DETERMINISTIC
ON                 ON        ADAPTIVE
=================  ========  =======  =================

This module is pure (no Home Assistant imports) so it can be unit-tested and
imported from both the models and engine layers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeMode(str, Enum):
    """Deterministic runtime mode derived from the two zone controls."""

    INACTIVE = "inactive"
    """Learning OFF, Active Control OFF — no learning, no commands."""

    SHADOW_ONLY = "shadow_only"
    """Learning ON, Active Control OFF — learn + shadow-evaluate; never dispatch."""

    DETERMINISTIC = "deterministic"
    """Learning OFF, Active Control ON — rule-based control, no adaptation."""

    ADAPTIVE = "adaptive"
    """Learning ON, Active Control ON — full adaptive control + experiments."""


@dataclass(frozen=True)
class RuntimeModeAuthority:
    """Immutable capability set derived once per cycle from the two controls.

    Each boolean answers exactly one authority question.  Subsystems read these
    properties instead of recombining ``learning_enabled`` /
    ``active_control_enabled`` ad hoc.
    """

    mode: RuntimeMode
    learning_enabled: bool
    active_control_enabled: bool

    @property
    def learning_allowed(self) -> bool:
        """May observations update learned models / shadow proposals?

        SHADOW_ONLY and ADAPTIVE only (Learning Mode ON).
        """
        return self.learning_enabled

    @property
    def shadow_evaluation_allowed(self) -> bool:
        """May the adaptive shadow decision be computed for comparison/learning?

        SHADOW_ONLY and ADAPTIVE only (Learning Mode ON).
        """
        return self.learning_enabled

    @property
    def adaptive_reads_allowed(self) -> bool:
        """May learned values influence the *real* authoritative decision?

        ADAPTIVE only.  In SHADOW_ONLY the adaptive value is a shadow result and
        must never be dispatched, because Active Control is OFF.
        """
        return self.learning_enabled and self.active_control_enabled

    @property
    def adaptive_writes_allowed(self) -> bool:
        """May adoption/reduction/rollback take *real* control effect?

        ADAPTIVE only.  Learning Mode ON without Active Control keeps adoptions
        as suspended shadow candidates with no actuation authority.
        """
        return self.learning_enabled and self.active_control_enabled

    @property
    def real_control_allowed(self) -> bool:
        """May SmartShading issue cover service calls at all?

        DETERMINISTIC and ADAPTIVE only (Active Control ON).
        """
        return self.active_control_enabled

    @property
    def experiments_allowed(self) -> bool:
        """May a real bounded learning experiment be injected/dispatched?

        ADAPTIVE only — and additionally subject to every P7 eligibility gate.
        """
        return self.learning_enabled and self.active_control_enabled

    @property
    def outcomes_allowed(self) -> bool:
        """May learning-relevant outcomes be created/resolved?

        Learning Mode ON only.  Deterministic operation records execution
        diagnostics but no learning outcomes.
        """
        return self.learning_enabled

    def as_diagnostics(self) -> dict:
        """Privacy-safe authority snapshot for diagnostics/exports."""
        return {
            "runtime_mode": self.mode.value,
            "learning_enabled": self.learning_enabled,
            "active_control_enabled": self.active_control_enabled,
            "learning_allowed": self.learning_allowed,
            "shadow_evaluation_allowed": self.shadow_evaluation_allowed,
            "adaptive_reads_allowed": self.adaptive_reads_allowed,
            "adaptive_writes_allowed": self.adaptive_writes_allowed,
            "real_control_allowed": self.real_control_allowed,
            "experiments_allowed": self.experiments_allowed,
            "outcomes_allowed": self.outcomes_allowed,
        }


def derive_runtime_mode(
    learning_enabled: bool, active_control_enabled: bool
) -> RuntimeMode:
    """Map the two controls to the single runtime mode.

    Inputs are coerced to ``bool`` so that legacy/unknown/missing option values
    resolve safely (any truthy → True, any falsy/None → False).
    """
    learning = bool(learning_enabled)
    active = bool(active_control_enabled)
    if learning and active:
        return RuntimeMode.ADAPTIVE
    if learning and not active:
        return RuntimeMode.SHADOW_ONLY
    if active and not learning:
        return RuntimeMode.DETERMINISTIC
    return RuntimeMode.INACTIVE


def derive_authority(
    learning_enabled: bool, active_control_enabled: bool
) -> RuntimeModeAuthority:
    """Build the full capability authority from the two controls."""
    learning = bool(learning_enabled)
    active = bool(active_control_enabled)
    return RuntimeModeAuthority(
        mode=derive_runtime_mode(learning, active),
        learning_enabled=learning,
        active_control_enabled=active,
    )
