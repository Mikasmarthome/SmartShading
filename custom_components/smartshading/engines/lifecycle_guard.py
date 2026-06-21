"""Pure lifecycle guard helper — lifecycle/override integration (Step 8c).

Kept as a standalone module so the coordinator can import and test it
without any HA dependencies.
"""
from __future__ import annotations

from ..models.lifecycle import LifecycleState


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
