"""T14: sanity/bounds tests for the existing Learning Progress display
(entities/zone_summary.py compute_learning_progress()).

T14 does not build a new learning-progress system — it verifies the
existing one stays bounded, uses only real data, and exposes an
understandable, limited set of status labels ("collecting"/"learning"/
"adapting"/"confident"), per the ticket's explicit "do not rebuild, just
verify" instruction.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# HA stubs — same technique as test_coordinator_parallel_dispatch.py, needed
# because entities/zone_summary.py imports coordinator.py at module scope,
# which in turn pulls in the full HA component surface.
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _StoreStub:
    def __init__(self, hass, version, key) -> None: pass
    async def async_load(self): return None
    async def async_save(self, data) -> None: pass
    async def async_remove(self) -> None: pass


_HA_STUBS = {
    "homeassistant": _stub("homeassistant"),
    "homeassistant.components": _stub("homeassistant.components"),
    "homeassistant.components.cover": _stub(
        "homeassistant.components.cover",
        CoverEntityFeature=type("CEF", (), {
            "SET_POSITION": 1, "SET_TILT_POSITION": 2, "OPEN": 4, "CLOSE": 8, "STOP": 16,
        }),
    ),
    "homeassistant.config_entries": _stub("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _stub(
        "homeassistant.core", HomeAssistant=object, Event=object, callback=lambda fn: fn,
    ),
    "homeassistant.helpers": _stub("homeassistant.helpers"),
    "homeassistant.helpers.entity_registry": _stub(
        "homeassistant.helpers.entity_registry", async_get=lambda *a, **k: None,
    ),
    "homeassistant.helpers.event": _stub(
        "homeassistant.helpers.event",
        async_track_state_change_event=lambda hass, entity_id, action: (lambda: None),
        async_track_point_in_time=lambda *a, **k: (lambda: None),
        async_call_later=lambda hass, delay, action: (lambda: None),
    ),
    "homeassistant.util": _stub("homeassistant.util"),
}
for _name, _mod in _HA_STUBS.items():
    sys.modules.setdefault(_name, _mod)

sys.modules.setdefault("homeassistant.util.dt", _stub(
    "homeassistant.util.dt",
    utcnow=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    as_local=lambda dt: dt,
))
sys.modules.setdefault("homeassistant.helpers.storage", _stub(
    "homeassistant.helpers.storage", Store=_StoreStub,
))

from custom_components.smartshading.engines.adaptation_layer import AdaptiveProfile
from custom_components.smartshading.entities.zone_summary import (
    _LEARNING_STATUS_ADAPTING,
    _LEARNING_STATUS_COLLECTING,
    _LEARNING_STATUS_CONFIDENT,
    _LEARNING_STATUS_LEARNING,
    compute_learning_progress,
)

_VALID_STATUSES = {
    _LEARNING_STATUS_COLLECTING, _LEARNING_STATUS_LEARNING,
    _LEARNING_STATUS_ADAPTING, _LEARNING_STATUS_CONFIDENT,
}


def _profile(**overrides) -> AdaptiveProfile:
    base = dict(
        learning_active=True, confidence_level="very_high",
        heat_sensitivity_factor=1.0, exposure_sensitivity_factor=1.0,
        solar_escalation_factor=1.0, adaptation_strength=0.9,
    )
    base.update(overrides)
    return AdaptiveProfile(**base)


def _coordinator_data(profiles: dict) -> object:
    return type("Data", (), {"adaptive_profiles": profiles})()


class TestLearningProgressBounds:
    def test_no_eligible_windows_returns_none_not_a_percentage(self) -> None:
        pct, attrs = compute_learning_progress([], None, {})
        assert pct is None
        assert attrs["status"] == _LEARNING_STATUS_COLLECTING

    def test_no_coordinator_data_returns_zero_not_none(self) -> None:
        pct, attrs = compute_learning_progress(["w1"], None)
        assert pct == 0
        assert attrs["status"] in _VALID_STATUSES

    def test_percent_is_always_within_zero_to_hundred(self) -> None:
        for strength in (0.0, 0.1, 0.5, 0.9, 1.0):
            data = _coordinator_data({"w1": _profile(adaptation_strength=strength)})
            pct, _ = compute_learning_progress(["w1"], data)
            assert pct is not None
            assert 0 <= pct <= 100

    def test_very_low_confidence_never_shows_high_progress(self) -> None:
        # A window with very_low confidence must be capped near zero
        # regardless of its raw adaptation_strength — a misleadingly high
        # percentage would be actively wrong, not just imprecise.
        data = _coordinator_data({"w1": _profile(
            confidence_level="very_low", adaptation_strength=1.0,
        )})
        pct, _ = compute_learning_progress(["w1"], data)
        assert pct == 0

    def test_higher_confidence_never_yields_lower_progress_than_lower_confidence(self) -> None:
        # Monotonicity sanity check — more confidence should never look
        # worse than less confidence for the same raw strength.
        low = _coordinator_data({"w1": _profile(confidence_level="medium", adaptation_strength=0.9)})
        high = _coordinator_data({"w1": _profile(confidence_level="very_high", adaptation_strength=0.9)})
        pct_low, _ = compute_learning_progress(["w1"], low)
        pct_high, _ = compute_learning_progress(["w1"], high)
        assert pct_high >= pct_low


class TestLearningProgressStatusLabels:
    """Only the four documented, understandable status labels may ever be
    produced — no leaking of internal confidence/reliability jargon."""

    @pytest.mark.parametrize("strength,confidence,expected", [
        (0.0, "very_high", _LEARNING_STATUS_COLLECTING),
        (0.3, "very_high", _LEARNING_STATUS_LEARNING),
        (0.6, "very_high", _LEARNING_STATUS_ADAPTING),
        (0.9, "very_high", _LEARNING_STATUS_CONFIDENT),
    ])
    def test_status_thresholds(self, strength: float, confidence: str, expected: str) -> None:
        data = _coordinator_data({"w1": _profile(
            confidence_level=confidence, adaptation_strength=strength,
        )})
        _, attrs = compute_learning_progress(["w1"], data)
        assert attrs["status"] == expected

    def test_status_is_always_one_of_the_four_documented_values(self) -> None:
        for strength in (0.0, 0.05, 0.25, 0.45, 0.65, 0.85, 1.0):
            data = _coordinator_data({"w1": _profile(adaptation_strength=strength)})
            _, attrs = compute_learning_progress(["w1"], data)
            assert attrs["status"] in _VALID_STATUSES

    def test_no_internal_jargon_leaks_into_status_value(self) -> None:
        # Structural guard: the status string itself must never contain raw
        # internal terms (confidence/reliability/composite/etc.) — those
        # concepts may exist as separate diagnostic attributes, but not as
        # part of the user-facing status label.
        forbidden = ("confidence", "reliability", "composite", "score", "outcome")
        for strength in (0.0, 0.3, 0.6, 0.9):
            data = _coordinator_data({"w1": _profile(adaptation_strength=strength)})
            _, attrs = compute_learning_progress(["w1"], data)
            status_lower = attrs["status"].lower()
            for term in forbidden:
                assert term not in status_lower


class TestLearningProgressOnlyRealData:
    def test_windows_without_a_profile_contribute_zero_not_skipped(self) -> None:
        # A window with no AdaptiveProfile yet must not be silently excluded
        # (which would inflate the average from real windows only) — it
        # contributes 0 strength, exactly like "no data yet".
        data = _coordinator_data({})  # no profile for w1 at all
        pct, attrs = compute_learning_progress(["w1"], data)
        assert pct == 0
        assert attrs["eligible_windows"] == 1

    def test_excluded_non_automatic_windows_are_reported_separately(self) -> None:
        from custom_components.smartshading.models.window import WindowBehaviorMode

        window_configs = {
            "w1": type("W", (), {"behavior_mode": WindowBehaviorMode.FULLY_AUTOMATIC})(),
            "w2": type("W", (), {"behavior_mode": WindowBehaviorMode.DISABLED_AUTOMATIC})(),
        }
        data = _coordinator_data({"w1": _profile()})
        pct, attrs = compute_learning_progress(["w1", "w2"], data, window_configs)
        assert attrs["eligible_windows"] == 1
        assert attrs["excluded_windows"] == 1
