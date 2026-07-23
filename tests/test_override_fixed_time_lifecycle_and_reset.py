"""Fixed-time Manual Override interaction with lifecycle-break, manual
reset (Active Control re-enable), and Safety-clear — T7 review points 9,
26-29.

lifecycle_should_break_override() (engines/lifecycle_guard.py) is a pure
function with zero awareness of duration_mode — it only inspects
prev/new LifecycleState and break_enabled. This means its existing 32
tests (tests/test_lifecycle_override_break.py, unmodified) already prove
its own correctness in isolation; what T7 adds is proof that the
COORDINATOR-LEVEL wiring (OverrideDetector.clear() / suppress_next_override_tick())
behaves identically regardless of which duration_mode produced the
override being cleared.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from custom_components.smartshading.engines.lifecycle_guard import lifecycle_should_break_override
from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.models.lifecycle import LifecycleState
from custom_components.smartshading.models.manual_override import OverrideReleaseStrategy
from custom_components.smartshading.state_machine.states import ShadingState

_UTC = timezone.utc
_WARMUP_NOW = datetime(2026, 6, 15, 6, 0, tzinfo=_UTC)


def _detector_with_fixed_time_override(fixed_until: time = time(20, 0)) -> tuple[OverrideDetector, datetime]:
    det = OverrideDetector()
    det.tick(
        window_id="w1", observed_position=0, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=_WARMUP_NOW,
    )
    t0 = _WARMUP_NOW + timedelta(minutes=1)
    det.tick(
        window_id="w1", observed_position=40, smartshading_target=0,
        prev_state=ShadingState.OPEN, tolerance=10, duration_min=120, now=t0,
        release_strategy=OverrideReleaseStrategy.FIXED_TIME, fixed_until=fixed_until, now_local=t0,
    )
    return det, t0


class TestLifecycleBreakBeforeFixedTime:
    def test_break_enabled_ends_override_before_its_far_future_expiry(self):
        """expires_at (today 20:00, far in the future relative to t0 06:01)
        is irrelevant once a lifecycle transition fires with break_enabled=
        True — the override ends immediately via the coordinator's existing
        clear() wiring, exactly as for a legacy-mode override."""
        det, t0 = _detector_with_fixed_time_override()
        assert det.get("w1", t0) is not None
        assert det.get("w1", t0).expires_at == datetime(2026, 6, 15, 20, 0, tzinfo=_UTC)

        should_break = lifecycle_should_break_override(
            prev=LifecycleState.DAY, new=LifecycleState.NIGHT, break_enabled=True,
        )
        assert should_break is True
        det.clear("w1")  # what the coordinator does when should_break is True
        assert det.get("w1", t0) is None


class TestLifecycleBreakDisabled:
    def test_break_disabled_leaves_fixed_time_override_active_until_its_own_expiry(self):
        det, t0 = _detector_with_fixed_time_override(fixed_until=time(20, 0))
        should_break = lifecycle_should_break_override(
            prev=LifecycleState.DAY, new=LifecycleState.NIGHT, break_enabled=False,
        )
        assert should_break is False
        # Coordinator does NOT call clear() in this case — override survives.
        still_active = det.get("w1", t0)
        assert still_active is not None
        assert still_active.expires_at == datetime(2026, 6, 15, 20, 0, tzinfo=_UTC)

        # It only ends at its own fixed boundary.
        after_boundary = datetime(2026, 6, 15, 20, 1, tzinfo=_UTC)
        assert det.get("w1", after_boundary) is None


class TestManualResetClearsFixedTimeOverrideToo:
    def test_active_control_reset_pattern_clears_fixed_time_override(self):
        """Mirrors what the coordinator does when a zone's Active Control
        switch is re-enabled (coordinator.py ~L1990): clear() +
        suppress_next_override_tick(). Must work identically for a
        fixed-time-mode override — clear() has no duration_mode awareness,
        it simply removes whatever is stored."""
        det, t0 = _detector_with_fixed_time_override()
        det.clear("w1")
        det.suppress_next_override_tick("w1")
        assert det.get("w1", t0) is None
        # The suppression is consumed on the next tick — a real manual move
        # right after reset is NOT immediately re-detected this one cycle.
        det.tick(
            window_id="w1", observed_position=70, smartshading_target=0,
            prev_state=ShadingState.OPEN, tolerance=10, duration_min=120,
            now=t0 + timedelta(minutes=1),
        )
        assert det.get("w1", t0 + timedelta(minutes=1)) is None


class TestSafetyClearFixedTimeOverrideToo:
    def test_clear_removes_fixed_time_override_exactly_like_legacy(self):
        """Mirrors the coordinator's Tier-1-Safety clear() call
        (coordinator.py ~L3921) — Safety always beats an active override
        regardless of duration_mode."""
        det, t0 = _detector_with_fixed_time_override()
        assert det.get("w1", t0) is not None
        det.clear("w1")  # what the coordinator does when STORM_SAFE/WIND_SAFE fires
        assert det.get("w1", t0) is None
