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
  4a. if tier1_active:                detector.clear(window_id)  # Safety beats override
  4b. elif tier_decision.release_override: detector.clear(window_id)  # T10: strategy-triggered release
  4c. else:                           detector.tick(...)          # detect / renew

expires_at semantics now depend on release_strategy (v1.2.0-beta.1, T10 —
see engines/override_release.py and models/manual_override.OverrideReleaseStrategy):
  DURATION            — expires_at IS the real release mechanism.
  FIXED_TIME           — expires_at IS the real release mechanism (a local
                         clock time instead of a duration).
  LIFECYCLE /
  FIRST_COMFORT /
  FIRST_PROTECTION /
  FIRST_ANY_DECISION /
  MANUAL                — expires_at is an OPTIONAL defensive safety-net only
                         (see OverridePolicyConfig.safety_timeout_enabled);
                         the real release is lifecycle_guard.py, the Tier 2
                         policy's release_override signal, or an explicit
                         user action (SmartShadingCoordinator.
                         async_clear_manual_override()), none of which are
                         this class's concern — this class only ever reacts
                         to being told to clear() or to expires_at being
                         reached.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from .override_release import compute_expiry, extends_on_renewal, uses_post_expiry_baseline
from ..models.manual_override import ManualOverride, OverrideReleaseStrategy
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
            Called every cycle (non-Tier-1, non-strategy-release path) to
            detect or renew overrides.
        clear(window_id)
            Explicitly removes an override (called when Tier 1 Safety fires,
            when the Tier 2 policy signals a strategy-triggered release, or
            when the user explicitly clears it).
    """

    def __init__(self) -> None:
        self._active_overrides: dict[str, ManualOverride] = {}
        self._warmup_counters: dict[str, int] = {}
        self._suppress_ticks: set[str] = set()
        # T7 review point 4 (generalized in T10 — see
        # engines.override_release.uses_post_expiry_baseline()): post-expiry
        # re-arm baseline, every release_strategy except DURATION. Not used
        # by DURATION at all — DURATION's existing, intentionally-unchanged
        # "several stale cycles later, a fresh (short) override forms again"
        # behavior (tests/test_override_detector.py
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
        """T7 review point 4, generalized in T10: an override released via
        its own (optional, defensive) expires_at safety-net can expire with
        the cover still sitting at its old, un-moved manual position (no new
        user action, no dispatch has happened yet). Without this, the very
        next tick() cycle after the one-shot F30 suppression is consumed
        would see that unchanged position deviate from the automatic target
        and immediately re-arm a BRAND NEW override — for a long/unbounded
        single-boundary strategy, that means silently extending the hold by
        up to the full safety-timeout again, from a stale deviation alone,
        not a genuine new manual movement.

        Recording the just-expired override's own position as a baseline
        lets tick() (see the "existing is None" branch) recognize "this is
        still the same old deviation" and withhold detection until the
        observed position genuinely changes away from that baseline — at
        which point it IS a real new movement and re-arms normally.

        Scoped to every release_strategy except DURATION (see
        engines.override_release.uses_post_expiry_baseline()) — DURATION's
        existing "several stale cycles later, a fresh (short) override is
        expected again" behavior is unchanged (see class docstring /
        __init__ comment) — the consequence there (a modest duration_min-
        scale extension) does not warrant changing well-established,
        explicitly-tested behavior.
        """
        if uses_post_expiry_baseline(OverrideReleaseStrategy(expired.release_strategy)):
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
        release_strategy: OverrideReleaseStrategy = OverrideReleaseStrategy.LIFECYCLE,
        fixed_until: time | None = None,
        now_local: datetime | None = None,
        safety_timeout_enabled: bool = True,
    ) -> None:
        """Update override state for one window in one coordinator cycle.

        Must be called AFTER TierOrchestrator has produced tier_decision
        (uses tier_decision.target_position as smartshading_target).
        Must NOT be called when Tier 1 Safety (STORM_SAFE / WIND_SAFE) is
        active, nor when tier_decision.release_override is True — the
        Coordinator calls clear() in both of those cases instead.

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
            duration_min:         Minutes — the caller computes this per current
                                  lifecycle state (v1.1.3): a short fixed daytime
                                  value, or a longer night value. Its MEANING
                                  depends on release_strategy (v1.2.0-beta.1,
                                  T10): the actual duration for DURATION, a
                                  defensive fallback for FIXED_TIME, or an
                                  optional defensive safety-net for every other
                                  strategy (see engines/override_release.py).
            now:                  Current UTC timestamp.
            scope:                "daytime" or "night" (v1.1.3) — recorded on the
                                  ManualOverride for diagnostics; does not itself
                                  change detection/renewal behavior.
            release_strategy:     OverrideReleaseStrategy (v1.2.0-beta.1, T10;
                                  renamed from T7's duration_mode). Selects how
                                  expires_at is computed for a NEW override (see
                                  engines/override_release.compute_expiry()) and
                                  whether renewal extends it (extends_on_renewal()).
            fixed_until:          The configured local clock time an override should
                                  end at. Required (non-None) when
                                  release_strategy=FIXED_TIME; ignored otherwise.
            now_local:            `now`, converted to Home Assistant's configured
                                  local timezone (e.g. via dt_util.as_local(now)).
                                  Required (non-None) when release_strategy=FIXED_TIME:
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
                                  If release_strategy=FIXED_TIME but now_local is not
                                  supplied, falls back to the duration computation
                                  (safe default, never raises).
            safety_timeout_enabled: v1.2.0-beta.1, T10. Whether duration_min
                                  applies as a defensive maximum for LIFECYCLE /
                                  FIRST_COMFORT / FIRST_PROTECTION /
                                  FIRST_ANY_DECISION / MANUAL. Ignored for
                                  DURATION/FIXED_TIME (see
                                  engines/override_release.compute_expiry()).
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
            # T7 review point 4 (generalized in T10): post-expiry re-arm baseline.
            # If the observed position still matches the just-expired override's
            # own position (within tolerance), this is the SAME stale deviation,
            # not a genuine new manual move — do NOT create a new override. The
            # baseline is consumed (cleared) only once the position genuinely
            # changes away from it.
            baseline = self._post_expiry_baseline.get(window_id)
            if baseline is not None and abs(observed_position - baseline) <= tolerance:
                return
            if window_id in self._post_expiry_baseline:
                del self._post_expiry_baseline[window_id]
            # Check for a NEW override (none was active before this cycle, or the
            # previous one just expired above).  expires_at is always freshly
            # computed here — see engines/override_release.compute_expiry().
            if abs(observed_position - smartshading_target) > tolerance:
                self._active_overrides[window_id] = ManualOverride(
                    window_id=window_id,
                    override_position=observed_position,
                    started_at=now,
                    expires_at=compute_expiry(
                        strategy=release_strategy, now=now, now_local=now_local,
                        duration_min=duration_min, fixed_until=fixed_until,
                        safety_timeout_enabled=safety_timeout_enabled,
                    ),
                    source="position_delta",
                    overridden_state=prev_state,
                    overridden_position=smartshading_target,
                    scope=scope,
                    release_strategy=release_strategy.value,
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
                _existing_strategy = OverrideReleaseStrategy(existing.release_strategy)
                if extends_on_renewal(_existing_strategy):
                    new_expires_at = now + timedelta(minutes=duration_min)
                else:
                    # A renewal of an already-active non-extending-strategy
                    # override must NOT move its boundary — only a brand-new
                    # override (the "existing is None" branch above, reached
                    # after a release) computes a fresh expiry.
                    new_expires_at = existing.expires_at
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
                    release_strategy=existing.release_strategy,
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

        Called by the Coordinator when Tier 1 Safety (STORM_SAFE / WIND_SAFE
        / RAIN_SAFE) takes over, when the Tier 2 policy signals a strategy-
        triggered release (v1.2.0-beta.1, T10 — release_strategy in
        FIRST_COMFORT / FIRST_PROTECTION / FIRST_ANY_DECISION), or when the
        user explicitly clears a MANUAL-strategy override.
        """
        self._active_overrides.pop(window_id, None)
        # A manual/safety/strategy clear also discards any pending
        # post-expiry re-arm baseline for this window — the override
        # lifecycle just ended explicitly, so there is nothing left to
        # compare a "still stale" position against.
        self._post_expiry_baseline.pop(window_id, None)
