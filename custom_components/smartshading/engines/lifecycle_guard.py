"""Pure lifecycle guard helper — lifecycle/override integration (Step 8c).

Kept as a standalone module so the coordinator can import and test it
without any HA dependencies.
"""
from __future__ import annotations

from ..models.lifecycle import LifecycleState
from ..state_machine.states import ShadingState


def lifecycle_should_break_override(
    *,
    prev: LifecycleState,
    new: LifecycleState,
    break_enabled: bool,
) -> bool:
    """Return True if an active manual override should be cleared by a lifecycle transition.

    Any state change (DAY→NIGHT, NIGHT→MORNING, MORNING→DAY, …) triggers the
    break when *break_enabled* is True.  The caller is responsible for calling
    ``OverrideDetector.clear()`` and updating ``active_override`` to None.
    """
    return break_enabled and (prev != new)


def should_allow_lifecycle_release(
    *,
    prev: LifecycleState,
    new: LifecycleState,
    current_shading_state: ShadingState,
    active_override: object | None,
    proposed_is_open: bool,
) -> bool:
    """True when ABSENCE_AND_SCHEDULE should dispatch the lifecycle-triggered OPEN.

    Fires whenever the current lifecycle is no longer NIGHT and the window is
    still in a state established by the night schedule.

    Two eligible states:

    NIGHT_CLOSED — state-based release (no prev=NIGHT requirement):
      The window was closed to night_position by NightEvaluator and is still
      there.  Release fires whenever lifecycle has left NIGHT, regardless of the
      previous lifecycle value in this session.  This covers both the normal
      live NIGHT→MORNING/DAY transition AND the post-restart scenario where
      prev was never NIGHT in the current session (coordinator initialises to
      DAY), as well as the NightHardHold interference path where the transition
      cycle had proposed_is_open=False and the release opportunity was missed.

    MANUAL_OVERRIDE — transition-based (prev=NIGHT required):
      The user moved the cover during the night and the override was just
      cleared by lifecycle_should_break_override.  The prev=NIGHT guard
      distinguishes "override held through the night, now morning" from a
      daytime override present at coordinator restart (where prev is DAY, not
      NIGHT) — we must not inadvertently release a daytime override.

    Called from the coordinator's behavior-mode dispatch suppression block.
    The caller must check ``_window_behavior is ABSENCE_AND_SCHEDULE`` before
    calling this helper.
    """
    if not proposed_is_open:
        return False
    if active_override is not None:
        return False
    # Current lifecycle must not be NIGHT (all paths share this guard).
    if new is LifecycleState.NIGHT:
        return False
    # NIGHT_CLOSED: state-based — fires whenever lifecycle has left NIGHT.
    if current_shading_state is ShadingState.NIGHT_CLOSED:
        return True
    # MANUAL_OVERRIDE: transition-based — requires a live NIGHT→MORNING/DAY edge
    # to avoid releasing daytime overrides after a coordinator restart.
    if current_shading_state is ShadingState.MANUAL_OVERRIDE:
        return prev is LifecycleState.NIGHT
    return False
