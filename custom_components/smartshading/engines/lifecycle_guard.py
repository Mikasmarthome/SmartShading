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

    Fires only on NIGHT→MORNING/DAY transitions when:
    - the tier proposed OPEN (proposed_is_open=True),
    - no active manual override remains,
    - the window's last recorded state was either NIGHT_CLOSED (normal path)
      or MANUAL_OVERRIDE (night-override path: user moved cover during the night,
      override was just cleared by lifecycle_should_break_override).

    Called from the coordinator's behavior-mode dispatch suppression block.
    The caller must check ``_window_behavior is ABSENCE_AND_SCHEDULE`` before
    calling this helper.
    """
    if not proposed_is_open:
        return False
    if active_override is not None:
        return False
    # Transition must be OUT of NIGHT (prev=NIGHT, new≠NIGHT).
    if prev is not LifecycleState.NIGHT or new is LifecycleState.NIGHT:
        return False
    return current_shading_state in (ShadingState.NIGHT_CLOSED, ShadingState.MANUAL_OVERRIDE)
