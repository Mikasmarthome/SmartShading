"""OverrideDetector — detects and manages per-window manual overrides.

Owned by SmartShadingCoordinator.  Stateful: lives between update cycles.
Not an evaluator — does not produce WindowDecision objects.

In-memory only: overrides are lost on HA restart (by design in this version).
Persistence via hass.storage is a Phase 2 extension.

Override detection strategy:
  Each coordinator cycle, compare the observed cover position (HA state,
  converted to internal convention) with SmartShading's proposed target
  position (TierOrchestrator output).  A delta > override_detection_tolerance
  after the warmup period signals that the user moved the cover manually.

  A warmup guard of _WARMUP_CYCLES_REQUIRED cycles prevents false positives
  immediately after HA restart when SmartShading has not yet established a
  stable evaluation baseline.

Coordinator call sequence per window, per cycle:
  1. active_override = detector.get(window_id, now)     # expiry check
  2. wdi = build_window_decision_input(active_override=active_override, ...)
  3. tier_decision = orchestrator.evaluate_window(wdi)
  4a. if tier1_active: detector.clear(window_id)        # Safety beats override
  4b. else:            detector.tick(...)                # detect / renew
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..models.manual_override import ManualOverride
from ..state_machine.states import ShadingState

_LOGGER = logging.getLogger(__name__)

_WARMUP_CYCLES_REQUIRED = 1


class OverrideDetector:
    """Detects, renews, and expires manual overrides for all windows.

    One instance is shared across all windows in the Coordinator.

    Public interface:
        get(window_id, now)  →  ManualOverride | None
            Returns the active override and clears it if expired.
        tick(...)
            Called every cycle (non-Tier-1 path) to detect or renew overrides.
        clear(window_id)
            Explicitly removes an override (called when Tier 1 Safety fires).
    """

    def __init__(self) -> None:
        self._active_overrides: dict[str, ManualOverride] = {}
        self._warmup_counters: dict[str, int] = {}
        self._suppress_ticks: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, window_id: str, now: datetime) -> ManualOverride | None:
        """Return the active override for window_id, clearing it if expired."""
        existing = self._active_overrides.get(window_id)
        if existing is not None and now >= existing.expires_at:
            del self._active_overrides[window_id]
            return None
        return existing

    # ------------------------------------------------------------------
    # Restart-safe persistence
    # ------------------------------------------------------------------

    def active_overrides_snapshot(self, now: datetime) -> list[dict]:
        """Serialize currently-active (non-expired) overrides for persistence."""
        return [
            ov.to_dict()
            for ov in self._active_overrides.values()
            if now < ov.expires_at
        ]

    def restore_active_overrides(self, raw: list, now: datetime) -> list[ManualOverride]:
        """Restore persisted active overrides, dropping any that already expired.

        Restored before the first dispatch decision so a manual movement made
        before an HA restart is honoured instead of being re-asserted.  A
        corrupt/old entry is skipped individually (never raises).  Returns the
        list of restored overrides (so the caller can seed the assumed-state
        last-commanded reference for post-expiry re-detection).
        """
        restored: list[ManualOverride] = []
        for entry in raw or []:
            try:
                ov = ManualOverride.from_dict(entry)
            except Exception as exc:
                # F7: a dropped entry here means a pre-restart manual override is
                # NOT restored — SmartShading may then re-assert an automatic
                # position over what the user had deliberately set.  Behavior is
                # unchanged (still skipped); this makes the loss visible.
                _LOGGER.warning(
                    "OverrideDetector: could not restore an override entry "
                    "(%s: %s) — treating as not overridden", type(exc).__name__, exc,
                )
                continue
            if now >= ov.expires_at:
                continue  # stale — do not resurrect
            self._active_overrides[ov.window_id] = ov
            restored.append(ov)
        return restored

    def tick(
        self,
        *,
        window_id: str,
        observed_position: int | None,
        smartshading_target: int | None,
        smartshading_assumed: int | None = None,
        prev_state: ShadingState,
        tolerance: int,
        duration_min: int,
        now: datetime,
        scope: str = "daytime",
    ) -> None:
        """Update override state for one window in one coordinator cycle.

        Must be called AFTER TierOrchestrator has produced tier_decision
        (uses tier_decision.target_position as smartshading_target).
        Must NOT be called when Tier 1 Safety (STORM_SAFE / WIND_SAFE) is
        active — the Coordinator calls clear() instead in that case.

        Args:
            window_id:            The window being evaluated.
            observed_position:    Actual cover position in internal convention
                                  (0=open, 100=shaded); None if unknown/unavailable.
            smartshading_target:  TierOrchestrator's target_position (internal);
                                  None when the behavior mode suppresses dispatch
                                  (ABSENCE_ONLY / DISABLED_AUTOMATIC hold).
            smartshading_assumed: SmartShading's last commanded position in internal
                                  convention (from AssumedStateManager); None if not
                                  yet available (e.g. first cycle after HA restart).
            prev_state:           Window's ShadingState before this cycle
                                  (stored on the ManualOverride for Learning).
            tolerance:            Minimum delta to declare an override.
            duration_min:         Override duration in minutes — the caller computes
                                  this per current lifecycle state (v1.1.3): a short
                                  fixed daytime duration, or a long night safety-net
                                  (the real night release is the Morning lifecycle
                                  transition via lifecycle_should_break_override(),
                                  not this duration).
            now:                  Current UTC timestamp.
            scope:                "daytime" or "night" (v1.1.3) — recorded on the
                                  ManualOverride for diagnostics; does not itself
                                  change detection/renewal behavior.
        """
        # Advance warmup counter.
        cycle_count = self._warmup_counters.get(window_id, 0)
        self._warmup_counters[window_id] = cycle_count + 1

        # Expiry check (also done in get(), but kept here for test isolation).
        existing = self._active_overrides.get(window_id)
        if existing is not None and now >= existing.expires_at:
            del self._active_overrides[window_id]
            existing = None

        # Warmup guard: no detection in the first N cycles after HA start.
        if cycle_count < _WARMUP_CYCLES_REQUIRED:
            return

        # One-shot suppression: skip detection for one cycle when Active Control
        # was just enabled to avoid a false positive from a cover that was already
        # at a non-target position from a previous shading session.
        if window_id in self._suppress_ticks:
            self._suppress_ticks.discard(window_id)
            return

        # Fail-safe: no observed position → no detection.
        if observed_position is None:
            return

        if existing is None:
            # Own-command guard: if the cover is at SmartShading's last commanded
            # position (within tolerance), the new target simply changed on
            # SmartShading's side — the user did NOT interfere.  This prevents the
            # permanent false-override loop that occurs when tick() runs before the
            # cover physically responds to a just-dispatched command.
            if (
                smartshading_assumed is not None
                and abs(observed_position - smartshading_assumed) <= tolerance
            ):
                return
            # No target planned (behavior mode suppressed dispatch): no reference
            # to compare against, so override detection is skipped for this cycle.
            if smartshading_target is None:
                return
            # Check for a new override.
            if abs(observed_position - smartshading_target) > tolerance:
                self._active_overrides[window_id] = ManualOverride(
                    window_id=window_id,
                    override_position=observed_position,
                    started_at=now,
                    expires_at=now + timedelta(minutes=duration_min),
                    source="position_delta",
                    overridden_state=prev_state,
                    overridden_position=smartshading_target,
                    scope=scope,
                )
        else:
            # Override already active — check if user moved again (renewal).
            if abs(observed_position - existing.override_position) > tolerance:
                self._active_overrides[window_id] = ManualOverride(
                    window_id=window_id,
                    override_position=observed_position,
                    started_at=now,
                    expires_at=now + timedelta(minutes=duration_min),
                    source="position_delta",
                    # Preserve original overridden context for Learning.
                    overridden_state=existing.overridden_state,
                    overridden_position=existing.overridden_position,
                    scope=scope,
                )

    def suppress_next_override_tick(self, window_id: str) -> None:
        """Suppress override detection for one tick for the given window.

        Called when Active Control is enabled for a zone so that the very
        first evaluation cycle does not produce a false manual-override signal
        from a cover that was already at a non-target position (e.g. left at
        25% from a previous shading session while SmartShading now recommends
        OPEN after the sun left the solar sector).

        The suppression is consumed on the next tick() call and then cleared,
        so subsequent cycles evaluate normally.
        """
        self._suppress_ticks.add(window_id)

    def clear(self, window_id: str) -> None:
        """Explicitly remove an active override.

        Called by the Coordinator when Tier 1 Safety (STORM_SAFE / WIND_SAFE)
        takes over — Safety always beats a manual override.
        """
        self._active_overrides.pop(window_id, None)
