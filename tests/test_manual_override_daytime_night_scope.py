"""Manual Override daytime/night duration scope — v1.1.3.

Previous semantics: a manual override lasted until the next lifecycle
transition (DAY<->NIGHT<->MORNING), which in practice could mean many hours
during the day since transitions only happen roughly twice a day.

New semantics:
  Daytime: a manual override holds for a fixed duration (default 120 min),
    then normal automatic control (Solar/Heat/Glare/...) resumes.
  Night:   a manual override still holds until Morning — unchanged from
    before. The coordinator passes a long night-duration safety-net cap
    (default 720 min / 12h) to OverrideDetector.tick(), but the real
    release mechanism remains lifecycle_should_break_override() firing on
    the NIGHT -> MORNING transition (engines/lifecycle_guard.py),
    unaffected by this change.

This is a narrow addition to the existing OverrideDetector/ManualOverride
mechanism: a `scope` field on ManualOverride ("daytime" | "night", set by
the caller) plus the coordinator computing which duration to pass to
tick() based on the current lifecycle state. No new evaluator, no new
architecture, no options-UI changes; ManualOverrideEvaluator itself is
unchanged and scope-agnostic (it only reads override_position).

Safety, Absence, and Night Contact are unaffected by this change: Safety
still calls OverrideDetector.clear() unconditionally (Tier 1 always beats
a manual override, regardless of scope); Absence/Night Contact evaluate
independently of ManualOverride's scope field.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartshading.engines.override_detector import OverrideDetector
from custom_components.smartshading.engines.lifecycle_guard import (
    lifecycle_should_break_override,
)
from custom_components.smartshading.evaluators.manual_override_evaluator import (
    ManualOverrideEvaluator,
)
from custom_components.smartshading.models.lifecycle import LifecycleState
from custom_components.smartshading.models.manual_override import ManualOverride
from custom_components.smartshading.state_machine.states import ShadingState

_NOW = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
_TOLERANCE = 10
_DAYTIME_DURATION_MIN = 120
_NIGHT_DURATION_MIN = 720
_WINDOW = "w-terrace"
_PREV_STATE = ShadingState.LIGHT_SHADE
_TARGET = 50  # SmartShading target (internal), before the user moved it
_USER_POS = 10  # where the user manually moved the cover to (internal)


def _detector_past_warmup(window_id: str = _WINDOW) -> OverrideDetector:
    d = OverrideDetector()
    # One warmup tick (no delta) — matches _WARMUP_CYCLES_REQUIRED=1.
    d.tick(
        window_id=window_id, observed_position=_TARGET, smartshading_target=_TARGET,
        prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_DAYTIME_DURATION_MIN,
        now=_NOW,
    )
    return d


def _coordinator_scope_and_duration(lifecycle_state: LifecycleState) -> tuple[str, int]:
    """Mirror the coordinator's tick()-call-site scope/duration computation."""
    scope = "night" if lifecycle_state is LifecycleState.NIGHT else "daytime"
    duration = _NIGHT_DURATION_MIN if scope == "night" else _DAYTIME_DURATION_MIN
    return scope, duration


# ===========================================================================
# 1. Daytime override blocks automatic control immediately after the move.
# ===========================================================================

class TestDaytimeOverrideBlocksImmediately:
    def test_override_detected_and_active_right_after_the_manual_move(self):
        d = _detector_past_warmup()
        d.tick(
            window_id=_WINDOW, observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_DAYTIME_DURATION_MIN,
            now=_NOW, scope="daytime",
        )
        active = d.get(_WINDOW, _NOW)
        assert active is not None
        assert active.override_position == _USER_POS
        assert active.scope == "daytime"

    def test_evaluator_returns_manual_override_state_for_the_active_override(self):
        d = _detector_past_warmup()
        d.tick(
            window_id=_WINDOW, observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_DAYTIME_DURATION_MIN,
            now=_NOW, scope="daytime",
        )
        active = d.get(_WINDOW, _NOW)
        from custom_components.smartshading.models.window_decision_input import (
            WindowDecisionInput,
        )
        # ManualOverrideEvaluator only reads wdi.active_override — build a
        # minimal stand-in rather than a full WindowDecisionInput.
        class _WDI:
            def __init__(self, override, wid):
                self.active_override = override
                self.window_config = type("WC", (), {"id": wid})()
        decision = ManualOverrideEvaluator().evaluate(_WDI(active, _WINDOW))
        assert decision is not None
        assert decision.shading_state is ShadingState.MANUAL_OVERRIDE
        assert decision.target_position == _USER_POS


# ===========================================================================
# 2. Daytime override expires after 120 minutes.
# ===========================================================================

class TestDaytimeOverrideExpiresAfter120Minutes:
    def _create(self) -> OverrideDetector:
        d = _detector_past_warmup()
        d.tick(
            window_id=_WINDOW, observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_DAYTIME_DURATION_MIN,
            now=_NOW, scope="daytime",
        )
        return d

    def test_still_active_just_before_120_minutes(self):
        d = self._create()
        assert d.get(_WINDOW, _NOW + timedelta(minutes=119)) is not None

    def test_expired_at_exactly_120_minutes(self):
        d = self._create()
        assert d.get(_WINDOW, _NOW + timedelta(minutes=_DAYTIME_DURATION_MIN)) is None

    def test_expired_well_after_120_minutes(self):
        d = self._create()
        assert d.get(_WINDOW, _NOW + timedelta(minutes=200)) is None


# ===========================================================================
# 3. After expiry, Solar/Glare/Heat (i.e. normal automatic control) resumes.
# ===========================================================================

class TestNormalControlResumesAfterExpiry:
    def test_no_active_override_means_evaluator_yields_to_normal_tiers(self):
        d = _detector_past_warmup()
        d.tick(
            window_id=_WINDOW, observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_DAYTIME_DURATION_MIN,
            now=_NOW, scope="daytime",
        )
        after_expiry = _NOW + timedelta(minutes=_DAYTIME_DURATION_MIN + 1)
        active = d.get(_WINDOW, after_expiry)
        assert active is None
        # ManualOverrideEvaluator is Tier 2 — with active_override=None it
        # returns None, which is exactly what lets Tier 4/5 (Absence/Heat/
        # Glare/Solar) produce the real decision for this cycle.
        from custom_components.smartshading.models.window_decision_input import (
            WindowDecisionInput,
        )
        class _WDI:
            def __init__(self, override, wid):
                self.active_override = override
                self.window_config = type("WC", (), {"id": wid})()
        decision = ManualOverrideEvaluator().evaluate(_WDI(active, _WINDOW))
        assert decision is None


# ===========================================================================
# 4. Night override does NOT expire after 120 minutes.
# ===========================================================================

class TestNightOverrideDoesNotExpireAfter120Minutes:
    def test_night_scope_survives_past_the_daytime_duration(self):
        d = _detector_past_warmup()
        d.tick(
            window_id=_WINDOW, observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_NIGHT_DURATION_MIN,
            now=_NOW, scope="night",
        )
        after_daytime_duration = _NOW + timedelta(minutes=_DAYTIME_DURATION_MIN + 30)
        active = d.get(_WINDOW, after_daytime_duration)
        assert active is not None
        assert active.scope == "night"

    def test_coordinator_scope_selection_picks_night_duration_at_night(self):
        scope, duration = _coordinator_scope_and_duration(LifecycleState.NIGHT)
        assert scope == "night"
        assert duration == _NIGHT_DURATION_MIN

    def test_coordinator_scope_selection_picks_daytime_duration_otherwise(self):
        for lc in (LifecycleState.DAY, LifecycleState.MORNING, LifecycleState.EVENING):
            scope, duration = _coordinator_scope_and_duration(lc)
            assert scope == "daytime"
            assert duration == _DAYTIME_DURATION_MIN


# ===========================================================================
# 5. Night override ends at Morning (unchanged lifecycle-break mechanism).
# ===========================================================================

class TestNightOverrideEndsAtMorning:
    def test_lifecycle_break_fires_on_night_to_morning_transition(self):
        assert lifecycle_should_break_override(
            prev=LifecycleState.NIGHT, new=LifecycleState.MORNING, break_enabled=True,
        ) is True

    def test_lifecycle_break_does_not_fire_while_still_night(self):
        assert lifecycle_should_break_override(
            prev=LifecycleState.NIGHT, new=LifecycleState.NIGHT, break_enabled=True,
        ) is False

    def test_night_override_still_active_right_before_morning_transition(self):
        # The duration timer alone must not have released it — only the
        # lifecycle transition does, for a night-scope override.
        d = _detector_past_warmup()
        d.tick(
            window_id=_WINDOW, observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_NIGHT_DURATION_MIN,
            now=_NOW, scope="night",
        )
        eight_hours_later = _NOW + timedelta(hours=8)
        assert d.get(_WINDOW, eight_hours_later) is not None


# ===========================================================================
# 6. Restart/Reload: scope survives to_dict/from_dict; old data degrades safely.
# ===========================================================================

class TestRestartPersistenceOfScope:
    def test_to_dict_from_dict_roundtrip_preserves_scope(self):
        ov = ManualOverride(
            window_id=_WINDOW, override_position=_USER_POS, started_at=_NOW,
            expires_at=_NOW + timedelta(minutes=_NIGHT_DURATION_MIN),
            source="position_delta", overridden_state=_PREV_STATE,
            overridden_position=_TARGET, scope="night",
        )
        restored = ManualOverride.from_dict(ov.to_dict())
        assert restored.scope == "night"
        assert restored == ov

    def test_pre_v113_persisted_entry_without_scope_key_degrades_to_daytime(self):
        # Simulates a dict persisted before the `scope` field existed.
        legacy_dict = {
            "window_id": _WINDOW,
            "override_position": _USER_POS,
            "started_at": _NOW.isoformat(),
            "expires_at": (_NOW + timedelta(minutes=_DAYTIME_DURATION_MIN)).isoformat(),
            "source": "position_delta",
            "overridden_state": _PREV_STATE.value,
            "overridden_position": _TARGET,
        }
        restored = ManualOverride.from_dict(legacy_dict)
        assert restored.scope == "daytime"

    def test_restore_active_overrides_drops_stale_entries_regardless_of_scope(self):
        d = OverrideDetector()
        stale = ManualOverride(
            window_id=_WINDOW, override_position=_USER_POS,
            started_at=_NOW - timedelta(hours=20),
            expires_at=_NOW - timedelta(hours=8),  # already expired
            source="position_delta", overridden_state=_PREV_STATE,
            overridden_position=_TARGET, scope="night",
        )
        restored = d.restore_active_overrides([stale.to_dict()], _NOW)
        assert restored == []
        assert d.get(_WINDOW, _NOW) is None


# ===========================================================================
# 7. Safety still overrides Manual Override unconditionally.
# ===========================================================================

class TestSafetyStillOverridesManualOverrideRegardlessOfScope:
    def test_clear_removes_a_daytime_scope_override(self):
        d = _detector_past_warmup()
        d.tick(
            window_id=_WINDOW, observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_DAYTIME_DURATION_MIN,
            now=_NOW, scope="daytime",
        )
        assert d.get(_WINDOW, _NOW) is not None
        d.clear(_WINDOW)
        assert d.get(_WINDOW, _NOW) is None

    def test_clear_removes_a_night_scope_override(self):
        d = _detector_past_warmup()
        d.tick(
            window_id=_WINDOW, observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_NIGHT_DURATION_MIN,
            now=_NOW, scope="night",
        )
        assert d.get(_WINDOW, _NOW) is not None
        d.clear(_WINDOW)
        assert d.get(_WINDOW, _NOW) is None


# ===========================================================================
# 8. Manual Override does not break Absence/Safety/NightContact independence.
# ===========================================================================

class TestManualOverrideScopeDoesNotAffectOtherPriorityPaths:
    def test_manual_override_evaluator_is_scope_agnostic(self):
        # The evaluator decision does not depend on `scope` at all -- proves
        # this v1.1.3 addition cannot change MANUAL_OVERRIDE dispatch
        # behavior, only how long the override state persists.
        for scope in ("daytime", "night"):
            ov = ManualOverride(
                window_id=_WINDOW, override_position=_USER_POS, started_at=_NOW,
                expires_at=_NOW + timedelta(minutes=60),
                source="position_delta", overridden_state=_PREV_STATE,
                overridden_position=_TARGET, scope=scope,
            )
            from custom_components.smartshading.models.window_decision_input import (
                WindowDecisionInput,
            )
            class _WDI:
                def __init__(self, override, wid):
                    self.active_override = override
                    self.window_config = type("WC", (), {"id": wid})()
            decision = ManualOverrideEvaluator().evaluate(_WDI(ov, _WINDOW))
            assert decision.shading_state is ShadingState.MANUAL_OVERRIDE
            assert decision.target_position == _USER_POS

    def test_different_windows_track_scope_independently(self):
        d = OverrideDetector()
        for w in ("w-day", "w-night"):
            d.tick(
                window_id=w, observed_position=_TARGET, smartshading_target=_TARGET,
                prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_DAYTIME_DURATION_MIN,
                now=_NOW,
            )
        d.tick(
            window_id="w-day", observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_DAYTIME_DURATION_MIN,
            now=_NOW, scope="daytime",
        )
        d.tick(
            window_id="w-night", observed_position=_USER_POS, smartshading_target=_TARGET,
            prev_state=_PREV_STATE, tolerance=_TOLERANCE, duration_min=_NIGHT_DURATION_MIN,
            now=_NOW, scope="night",
        )
        assert d.get("w-day", _NOW).scope == "daytime"
        assert d.get("w-night", _NOW).scope == "night"
        # Daytime one expires well before the night one.
        later = _NOW + timedelta(minutes=_DAYTIME_DURATION_MIN + 5)
        assert d.get("w-day", later) is None
        assert d.get("w-night", later) is not None
