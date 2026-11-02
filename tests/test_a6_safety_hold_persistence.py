"""A6 -- the pure rules for storing and restoring the safety release holds."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smartshading.engines.safety_hold import SafetyHold
from custom_components.smartshading.engines.safety_hold_persistence import (
    FUTURE_SKEW_S,
    PAYLOAD_VERSION,
    RESTORE_MAX_AGE_S,
    holds_signature,
    parse_holds,
    serialize_holds,
)

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def _hold(age_s: float | None, hold_s: float = 600.0) -> SafetyHold:
    hold = SafetyHold(_hold_s=hold_s)
    if age_s is not None:
        hold._last_triggered = NOW - timedelta(seconds=age_s)
    return hold


def _payload(**kinds) -> dict:
    return {"version": PAYLOAD_VERSION, "saved_at": NOW.isoformat(),
            "holds": {"storm": {}, "wind": {}, "rain": {}, **kinds}}


def _parse(payload, *, windows=("w1", "w2"), enabled=lambda kind, wid: True):
    return parse_holds(payload, now=NOW, window_ids=set(windows), protection_enabled=enabled)


class TestSerialize:
    def test_only_latched_holds_are_stored_with_their_trigger_time(self):
        payload = serialize_holds(
            {"rain": {"w1": _hold(120.0), "w2": _hold(None)}, "wind": {}, "storm": {"w1": _hold(30.0)}},
            now=NOW)
        assert payload["version"] == PAYLOAD_VERSION
        assert payload["holds"]["rain"] == {"w1": (NOW - timedelta(seconds=120)).isoformat()}
        assert payload["holds"]["storm"] == {"w1": (NOW - timedelta(seconds=30)).isoformat()}
        assert payload["holds"]["wind"] == {}

    def test_the_signature_ignores_the_save_time_but_not_the_holds(self):
        holds = {"rain": {"w1": _hold(60.0)}}
        a = serialize_holds(holds, now=NOW)
        b = serialize_holds(holds, now=NOW + timedelta(minutes=3))
        assert holds_signature(a) == holds_signature(b)
        c = serialize_holds({"rain": {"w1": _hold(30.0)}}, now=NOW)
        assert holds_signature(a) != holds_signature(c)

    def test_a_round_trip_restores_the_same_trigger_times(self):
        payload = serialize_holds({"rain": {"w1": _hold(300.0)}, "storm": {"w2": _hold(10.0)}}, now=NOW)
        result = _parse(payload)
        assert result.restored["rain"]["w1"] == NOW - timedelta(seconds=300)
        assert result.restored["storm"]["w2"] == NOW - timedelta(seconds=10)
        assert result.stats["restored"] == 2


class TestRestoreRules:
    def test_no_payload_restores_nothing(self):
        result = _parse(None)
        assert result.restored == {} and result.stats["payload_found"] is False

    @pytest.mark.parametrize("payload", ["text", 5, [], {"version": 99, "holds": {}}, {"version": 1, "holds": []}])
    def test_an_unreadable_or_foreign_payload_restores_nothing(self, payload):
        result = _parse(payload)
        assert result.restored == {}

    def test_a_hold_older_than_the_limit_is_not_restored(self):
        old = (NOW - timedelta(seconds=RESTORE_MAX_AGE_S + 1)).isoformat()
        result = _parse(_payload(rain={"w1": old}))
        assert result.restored == {} and result.stats["expired"] == 1

    def test_a_hold_just_inside_the_limit_is_restored(self):
        edge = (NOW - timedelta(seconds=RESTORE_MAX_AGE_S - 1)).isoformat()
        assert _parse(_payload(rain={"w1": edge})).stats["restored"] == 1

    def test_a_time_far_in_the_future_is_a_clock_problem_and_not_restored(self):
        future = (NOW + timedelta(seconds=FUTURE_SKEW_S + 60)).isoformat()
        result = _parse(_payload(wind={"w1": future}))
        assert result.restored == {} and result.stats["clock_skew"] == 1

    def test_a_small_clock_skew_is_clamped_to_now(self):
        slightly = (NOW + timedelta(seconds=30)).isoformat()
        assert _parse(_payload(wind={"w1": slightly})).restored["wind"]["w1"] == NOW

    @pytest.mark.parametrize("raw", ["not a time", "2026-11-02T12:00:00", 12, None])
    def test_an_invalid_time_is_dropped(self, raw):
        result = _parse(_payload(rain={"w1": raw}))
        assert result.restored == {} and result.stats["invalid"] == 1

    def test_an_unknown_window_is_dropped(self):
        result = _parse(_payload(rain={"gone": NOW.isoformat()}))
        assert result.restored == {} and result.stats["unknown_window"] == 1

    def test_a_hold_of_a_protection_that_is_off_is_dropped(self):
        result = _parse(
            _payload(wind={"w1": NOW.isoformat()}, storm={"w1": NOW.isoformat()}),
            enabled=lambda kind, wid: kind != "wind")
        assert set(result.restored) == {"storm"} and result.stats["protection_off"] == 1

    def test_one_bad_entry_does_not_hide_the_good_ones(self):
        result = _parse(_payload(rain={"w1": "garbage", "w2": NOW.isoformat()}))
        assert result.restored == {"rain": {"w2": NOW}}
