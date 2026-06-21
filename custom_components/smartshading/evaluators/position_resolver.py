"""PositionResolver — selects the most restrictive decision from multiple candidates.

Responsibility: given a sequence of WindowDecision candidates (Tier 4 Protection
Floors and the Tier 5 pipeline result), return the single decision whose
target_position is highest — i.e. the most shaded.

Internal position convention (0 = open, 100 = fully shaded):
    max(target_position) is correct — a higher number means more shading.

Scope:
  - Pure function: no HA state, no config resolution, no async.
  - Accepts None entries (unevaluated or disabled tiers) and ignores them.
  - Accepts WindowDecision entries with target_position=None and ignores them.
  - Returns None when no candidate provides a concrete position
    (→ TierOrchestrator interprets this as OPEN).
  - Tie-breaking: when two candidates have the same target_position,
    the first one in the input sequence wins (Python's max() stability).
"""
from __future__ import annotations

from collections.abc import Sequence

from ..models.window_decision import WindowDecision


class PositionResolver:
    """Selects the most-shaded WindowDecision from a mixed candidate list."""

    @staticmethod
    def resolve(decisions: Sequence[WindowDecision | None]) -> WindowDecision | None:
        """Return the decision with the highest target_position.

        Args:
            decisions: Tier 4 floor decisions + Tier 5 result, in any order.
                       None entries are silently skipped.

        Returns:
            The WindowDecision with the highest target_position, or None if
            no candidate carries a concrete position.
        """
        candidates = [
            d for d in decisions
            if d is not None and d.target_position is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.target_position)  # type: ignore[return-value]
