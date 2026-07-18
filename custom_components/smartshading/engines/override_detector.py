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
from datetime import datetime, time, timedelta, timezone

from .override_fixed_time import compute_fixed_time_expiry
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
        # T7 review point 4: post-expiry re-arm baseline, FIXED_TIME mode
        # only (see _maybe_arm_post_expiry_baseline() for the full
        # rationale). Not used by legacy mode at all — legacy's existing,
        # intentionally-unchanged "several stale cycles later, a fresh
        # (short) override forms again" behavior
        # (tests/test_override_detector.py
        # TestOverrideDetectorTimeoutSuppression) is untouched.
        self._post_expiry_baseline: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, window_id: str, now: datetime) -> ManualOverride | None:
        """Return the active override for window_id, clearing it if expired.

        F30 field fix: a natural timeout clear also suppresses the next
        override-detection tick (see suppress_next_override_tick()) — the
        cover is still physically at the override position at this instant,
        and without this the very next tick() would immediately reinterpret
        that unmoved position as a brand-new override, before the
        now-unblocked automatic decision has had any real chance to
        dispatch and move the cover.
        """
        existing = self._active_overrides.get(window_id)
        if existing is not None and now >= existing.expires_at:
            del self._active_overrides[window_id]
            self._maybe_arm_post_expiry_baseline(existing)
            self.suppress_next_override_tick(window_id)
            return None
        return existing

    def _maybe_arm_post_expiry_baseline(self, expired: ManualOverride) -> None:
        """T7 review point 4: a FIXED_TIME override can expire with the
        cover still sitting at its old, un-moved manual position (no new
        user action, no dispatch has happened yet). Without this, the very
        next tick() cycle after the one-shot F30 suppression is consumed
        would see that unchanged position deviate from the automatic target
        and immediately re-arm a BRAND NEW override — for fixed_time mode,
        that means silently extending the hold by up to ~24h from a stale
        deviation alone, not a genuine new manual movement.

        Recording the just-expired override's own position as a baseline
        lets tick() (see the "existing is None" branch) recognize "this is
        still the same old deviation" and withhold detection until the
        observed position genuinely changes away from that baseline — at
        which point it IS a real new movement and re-arms normally.

        Scoped to FIXED_TIME only: legacy mode's existing "several stale
        cycles later, a fresh (short) override is expected again" behavior
        is unchanged (see class docstring / __init__ comment) — the
        consequence there (a modest duration_min-scale extension) does not
        warrant changing well-established, explicitly-tested behavior.
        """
        if expired.duration_mode == "fixed_time":
            self._post_expiry_baseline[expired.window_id] = expired.override_position

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
                # F30 field fix: a stale, already-expired persisted override is
                # dropped here without ever reaching _active_overrides — the
                # cover is presumably still at that old position, so also
                # suppress the first post-restart detection tick for this
                # window, same reasoning as the live natural-timeout clear
                # above. Belt-and-suspenders alongside the per-window warmup
                # guard in tick(), which already skips the very first tick.
                self.suppress_next_override_tick(ov.window_id)
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
        duration_mode: str = "legacy",
        fixed_until: time | None = None,
        now_local: datetime | None = None,
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
            duration_mode:        "legacy" (default) or "fixed_time" (T7). Selects
                                  how expires_at is computed for a NEW override
                                  (legacy: now + duration_min; fixed_time:
                                  compute_fixed_time_expiry(now, fixed_until)) and
                                  governs renewal semantics (see the "else" branch
                                  below) — the ManualOverride runtime object itself
                                  does not carry the mode; only expires_at matters
                                  once computed.
            fixed_until:          The configured local clock time an override should
                                  end at. Required (non-None) when
                                  duration_mode="fixed_time"; ignored otherwise.
            now_local:            `now`, converted to Home Assistant's configured
                                  local timezone (e.g. via dt_util.as_local(now)).
                                  Required (non-None) when duration_mode="fixed_time":
                                  fixed_until is a LOCAL wall-clock time (what the
                                  user configured, e.g. "08:00" means 8 AM in HA's
                                  own timezone, not UTC), so the fixed-time
                                  computation must run against `now_local`, not the
                                  UTC `now` — otherwise a non-UTC HA timezone would
                                  make the override expire at the wrong wall-clock
                                  instant. The computed local expiry is converted
                                  back to the same timezone convention as `now`
                                  (UTC) before being stored, so all other expires_at
                                  comparisons in this class stay apples-to-apples.
                                  If duration_mode="fixed_time" but now_local is not
                                  supplied, falls back to the legacy duration
                                  computation (safe default, never raises).
        """
        # Advance warmup counter.
        cycle_count = self._warmup_counters.get(window_id, 0)
        self._warmup_counters[window_id] = cycle_count + 1

        # Expiry check (also done in get(), but kept here for test isolation).
        # F30 field fix: same reasoning as get() — suppress this same call's
        # own detection below (consumed a few lines down), since the cover
        # has not had any chance to move away from the just-expired override
        # position yet.
        existing = self._active_overrides.get(window_id)
        if existing is not None and now >= existing.expires_at:
            del self._active_overrides[window_id]
            self._maybe_arm_post_expiry_baseline(existing)
            existing = None
            self.suppress_next_override_tick(window_id)

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
            # cover physically responds to a just-dispatched command.  This also
            # covers a T7 allowed Protection/Comfort pass-through dispatch made
            # while an override was active but has since naturally expired.
            if (
                smartshading_assumed is not None
                and abs(observed_position - smartshading_assumed) <= tolerance
            ):
                return
            # No target planned (behavior mode suppressed dispatch): no reference
            # to compare against, so override detection is skipped for this cycle.
            if smartshading_target is None:
                return
            # T7 review point 4: post-expiry re-arm baseline (FIXED_TIME only).
            # If the observed position still matches the just-expired
            # fixed-time override's own position (within tolerance), this is
            # the SAME stale deviation, not a genuine new manual move — do
            # NOT create a new override. The baseline is consumed (cleared)
            # only once the position genuinely changes away from it.
            baseline = self._post_expiry_baseline.get(window_id)
            if baseline is not None and abs(observed_position - baseline) <= tolerance:
                return
            if window_id in self._post_expiry_baseline:
                del self._post_expiry_baseline[window_id]
            # Check for a NEW override (none was active before this cycle, or the
            # previous one just expired above).  T7: expires_at is always freshly
            # computed here — for fixed_time mode this yields "today at
            # fixed_until" or "tomorrow at fixed_until" depending on whether that
            # instant has already passed, exactly the "new override after expiry
            # gets the next boundary" semantics (review point 8).
            if abs(observed_position - smartshading_target) > tolerance:
                self._active_overrides[window_id] = ManualOverride(
                    window_id=window_id,
                    override_position=observed_position,
                    started_at=now,
                    expires_at=_compute_new_expiry(
                        now=now, duration_min=duration_min,
                        duration_mode=duration_mode, fixed_until=fixed_until,
                        now_local=now_local,
                    ),
                    source="position_delta",
                    overridden_state=prev_state,
                    overridden_position=smartshading_target,
                    scope=scope,
                    duration_mode=duration_mode,
                )
        else:
            # T7: own-command guard also applies while an override is already
            # active.  Without this, SmartShading's own dispatch of an ALLOWED
            # Protection/Comfort action (override_allow_protection_actions /
            # override_allow_comfort_actions) while the override otherwise
            # remains in effect would be misread as a fresh manual movement by
            # the plain position-delta check below, incorrectly "renewing" the
            # override at the dispatched position and moving its expires_at —
            # even though expires_at must stay untouched by an allowed
            # pass-through dispatch (review points 10/11).
            if (
                smartshading_assumed is not None
                and abs(observed_position - smartshading_assumed) <= tolerance
            ):
                return
            # Override already active — check if the user moved it again (a real
            # renewal, not SmartShading's own dispatch, per the guard above).
            if abs(observed_position - existing.override_position) > tolerance:
                if duration_mode == "fixed_time":
                    # T7 review point 8: a renewal of an ALREADY-active
                    # fixed-time override must NOT move its fixed boundary —
                    # only a brand-new override (the "existing is None" branch
                    # above, reached after natural expiry) computes a fresh
                    # fixed_until occurrence.
                    new_expires_at = existing.expires_at
                else:
                    new_expires_at = now + timedelta(minutes=duration_min)
                self._active_overrides[window_id] = ManualOverride(
                    window_id=window_id,
                    override_position=observed_position,
                    started_at=now,
                    expires_at=new_expires_at,
                    source="position_delta",
                    # Preserve original overridden context for Learning.
                    overridden_state=existing.overridden_state,
                    overridden_position=existing.overridden_position,
                    scope=scope,
                    duration_mode=existing.duration_mode,
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
        # T7: a manual/safety clear also discards any pending post-expiry
        # re-arm baseline for this window — the override lifecycle just
        # ended explicitly, so there is nothing left to compare a "still
        # stale" position against.
        self._post_expiry_baseline.pop(window_id, None)


def _compute_new_expiry(
    *,
    now: datetime,
    duration_min: int,
    duration_mode: str,
    fixed_until: time | None,
    now_local: datetime | None,
) -> datetime:
    """expires_at for a brand-new override (see tick()'s single call site).

    fixed_until is a LOCAL wall-clock time, so the fixed-time computation
    must run against now_local (HA's configured timezone), not the UTC
    `now` — see tick()'s now_local docstring. The resulting local instant is
    converted back to UTC before being returned, matching every other
    expires_at value's timezone convention (all computed from/compared
    against a UTC `now` elsewhere in this class).
    """
    if duration_mode == "fixed_time" and fixed_until is not None and now_local is not None:
        local_expiry = compute_fixed_time_expiry(now=now_local, fixed_until=fixed_until)
        return local_expiry.astimezone(timezone.utc)
    return now + timedelta(minutes=duration_min)
