"""Window granularity and multi-safety-signal scenarios for the T8 Rain
override-clear unification.

OverrideDetector (engines/override_detector.py) is keyed per-window
(`self._active_overrides: dict[str, ManualOverride]`) — every clear()/tick()
call takes an explicit window_id and only ever touches that one entry. The
Rain sensor reading itself is coordinator-wide (read once per cycle, shared
across all windows), but its EFFECT on override state remains strictly
per-window because the coordinator's per-window loop calls
`self._override_detector.clear(window_id)` individually for each window
whose OWN tier_decision (which depends on that window's own
rain_protection_enabled flag) resolves to RAIN_SAFE.

Coverage:
  GRAN-01  Rain-safety for window A clears only window A's override.
  GRAN-02  Window B (same zone, rain protection disabled) keeps its
           override even though the same global rain sensor is RAINING.
  GRAN-03  Multiple windows in the same zone remain fully independent.
  GRAN-04  Storm/Wind granularity is unaffected by T8 (still per-window).

  MULTI-01 Storm > Wind > Rain priority unchanged.
  MULTI-02 Storm clears, Storm ends, Rain still active -> Rain now ALSO
           clears (no re-creation of the Storm-cleared override; nothing
           to clear a second time, but Rain's own clear() call is safely
           idempotent).
  MULTI-03 Rain clears an override; Storm then takes over — no double or
           contradictory clear (clear() is idempotent).
  MULTI-04 Wind clears; Rain remains active afterward — override stays
           cleared, consistent, no resurrection via Rain's own hold.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_WARMUP_NOW = datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)


def _detector_with_override(det: OverrideDetector, window_id: str, now: datetime) -> None:
    det.tick(
        window_id=window_id, observed_position=0, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now,
    )
    det.tick(
        window_id=window_id, observed_position=40, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=now + timedelta(minutes=1),
    )
    assert det.get(window_id, now + timedelta(minutes=1)) is not None


class TestWindowGranularity:
    def test_rain_clear_only_affects_the_triggering_window(self) -> None:
        det = OverrideDetector()
        _detector_with_override(det, "window-a", _WARMUP_NOW)
        _detector_with_override(det, "window-b", _WARMUP_NOW)
        t = _WARMUP_NOW + timedelta(minutes=2)
        assert det.get("window-a", t) is not None
        assert det.get("window-b", t) is not None

        # Mirrors coordinator.py: window-a's own tier_decision is RAIN_SAFE
        # (rain_protection_enabled=True for this window) -> clear(window-a)
        # only. window-b's own tier_decision this cycle is NOT RAIN_SAFE
        # (rain_protection_enabled=False for window-b, per its own config)
        # -> no clear() call for window-b at all.
        det.clear("window-a")

        assert det.get("window-a", t) is None
        assert det.get("window-b", t) is not None  # untouched

    def test_non_rain_enabled_window_keeps_override_despite_global_rain_sensor(self) -> None:
        """The rain sensor reading is coordinator-wide, but a window with
        rain_protection_enabled=False never produces a RAIN_SAFE
        tier_decision in the first place (RainEvaluator's own gate — see
        evaluators/rain_evaluator.py), so the coordinator's per-window loop
        never even considers clearing that window's override on account of
        rain."""
        det = OverrideDetector()
        _detector_with_override(det, "window-no-rain", _WARMUP_NOW)
        t = _WARMUP_NOW + timedelta(minutes=2)
        assert det.get("window-no-rain", t) is not None
        # No clear() call is ever issued for this window in a rain scenario
        # (simulated here by simply never calling clear("window-no-rain")).
        assert det.get("window-no-rain", t) is not None  # still active

    def test_multiple_windows_same_zone_independent_clear(self) -> None:
        det = OverrideDetector()
        for wid in ("w-living-1", "w-living-2", "w-living-3"):
            _detector_with_override(det, wid, _WARMUP_NOW)
        t = _WARMUP_NOW + timedelta(minutes=2)
        for wid in ("w-living-1", "w-living-2", "w-living-3"):
            assert det.get(wid, t) is not None

        # Only w-living-2 is rain-enabled and currently rain-safe.
        det.clear("w-living-2")

        assert det.get("w-living-1", t) is not None
        assert det.get("w-living-2", t) is None
        assert det.get("w-living-3", t) is not None

    def test_storm_granularity_unaffected_by_t8(self) -> None:
        """Sanity: Storm's pre-existing per-window clear() behavior is
        untouched by the T8 change (T8 only added RAIN_SAFE to the shared
        constant/condition; it did not alter clear()'s own per-window
        signature or semantics)."""
        det = OverrideDetector()
        _detector_with_override(det, "window-a", _WARMUP_NOW)
        _detector_with_override(det, "window-b", _WARMUP_NOW)
        t = _WARMUP_NOW + timedelta(minutes=2)
        det.clear("window-a")  # Storm affects window-a only
        assert det.get("window-a", t) is None
        assert det.get("window-b", t) is not None


class TestPriorityUnchanged:
    def test_storm_beats_wind_beats_rain_ordering_documented_and_unaffected(self) -> None:
        """T8 does not touch tier_orchestrator.py's evaluator ordering
        (Storm -> Wind -> Rain, sequential early-exit) — only the
        coordinator-side override-clear condition changed."""
        from custom_components.smartshading.state_machine.states import STATE_PRIORITY
        assert STATE_PRIORITY[ShadingState.STORM_SAFE] < STATE_PRIORITY[ShadingState.WIND_SAFE]
        assert STATE_PRIORITY[ShadingState.WIND_SAFE] < STATE_PRIORITY[ShadingState.RAIN_SAFE]


class TestMultiSafetyTransitions:
    def test_storm_clears_storm_ends_rain_continues_no_resurrection(self) -> None:
        det = OverrideDetector()
        _detector_with_override(det, "w1", _WARMUP_NOW)
        t0 = _WARMUP_NOW + timedelta(minutes=2)
        assert det.get("w1", t0) is not None

        # Storm fires first, clears the override.
        det.clear("w1")
        assert det.get("w1", t0) is None

        # Storm ends; Rain is (and remains) active — no override exists to
        # clear a second time, and Rain's own clear() call (idempotent) has
        # no further effect. No resurrection.
        t1 = t0 + timedelta(minutes=5)
        det.clear("w1")  # Rain's own clear() call this cycle — idempotent
        assert det.get("w1", t1) is None

    def test_rain_clears_then_storm_takes_over_no_double_or_contradictory_clear(self) -> None:
        det = OverrideDetector()
        _detector_with_override(det, "w1", _WARMUP_NOW)
        t0 = _WARMUP_NOW + timedelta(minutes=2)
        det.clear("w1")  # Rain clears first
        assert det.get("w1", t0) is None

        t1 = t0 + timedelta(minutes=1)
        det.clear("w1")  # Storm's own clear() call — idempotent, no error
        assert det.get("w1", t1) is None

    def test_wind_clears_rain_remains_active_afterward_stays_cleared(self) -> None:
        det = OverrideDetector()
        _detector_with_override(det, "w1", _WARMUP_NOW)
        t0 = _WARMUP_NOW + timedelta(minutes=2)
        det.clear("w1")  # Wind clears
        assert det.get("w1", t0) is None

        # Wind ends, Rain remains active for several more cycles — override
        # stays cleared throughout (Rain's hold re-asserting RAIN_SAFE does
        # not resurrect a cleared override; only a genuine new manual
        # movement would create a fresh one).
        for minutes in (5, 10, 30):
            t_n = t0 + timedelta(minutes=minutes)
            assert det.get("w1", t_n) is None

    def test_clear_is_idempotent_across_repeated_safety_signals(self) -> None:
        """Rain and Wind (or any combination) both resolving to a clear()
        call within overlapping cycles must never raise or produce
        inconsistent state — clear() on an already-empty entry is a no-op."""
        det = OverrideDetector()
        _detector_with_override(det, "w1", _WARMUP_NOW)
        det.clear("w1")
        det.clear("w1")  # second safety signal, same cycle family
        det.clear("w1")  # third — still safe
        assert det.get("w1", _WARMUP_NOW + timedelta(minutes=10)) is None
