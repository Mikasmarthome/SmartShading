"""ShadingGroup / multi-cover harmonization must never bypass the global
serial dispatch (lock + throttle).

Audit scope: two or more windows in the same zone with the same shading_group
harmonized to an IDENTICAL target_position_ha (or one window with multiple
assigned cover entities) must still produce SEPARATE physical dispatch
intents, each individually gated by the SAME shared GlobalSerialDispatch
(asyncio.Lock + minimum inter-dispatch interval) — exactly as the real
coordinator does at coordinator.py's Execution Pipeline Pass 2 (the
`async with self._serial_dispatch.lock: ...` block, run once per intent per
window, in a single sequential `for window_id, s in _window_states.items()`
loop with no gather/create_task).

This complements (does not duplicate) tests/test_phase_9g8_global_dispatch_throttle.py
(which exercises GlobalDispatchThrottle directly, without the asyncio.Lock)
and tests/test_v10_sequential_dispatch.py (which exercises GlobalSerialDispatch
in isolation, without real ShadingGroup harmonization or CommandFilter output).
Here the full real chain is used: compute_harmonization() -> CommandFilter ->
build_execution_plan() -> GlobalSerialDispatch (lock + throttle) ->
dispatch_cover_intent().

No Home Assistant import required (dispatch_cover_intent only needs a
MagicMock hass with services.async_call).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.smartshading.cover_control.command_filter import (
    CommandFilter,
    ExecutionCapability,
    ExecutionMode,
)
from custom_components.smartshading.cover_control.execution_plan import (
    build_execution_plan,
)
from custom_components.smartshading.cover_control.execution_result import (
    ExecutionStatus,
    build_blocked_result,
)
from custom_components.smartshading.cover_control.global_dispatch_throttle import (
    GlobalSerialDispatch,
)
from custom_components.smartshading.cover_control.ha_service_adapter import (
    dispatch_cover_intent,
)
from custom_components.smartshading.cover_control.shading_group_harmonizer import (
    ShadingGroupCandidate,
    compute_harmonization,
)

_NOW = datetime(2026, 6, 18, 15, 0, 0, tzinfo=timezone.utc)


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    return hass


def _cfr(
    *,
    target_internal: int,
    current_internal: int,
    is_safety: bool = False,
    tolerance: int = 3,
):
    """Real CommandFilterResult via CommandFilter().evaluate() — the exact
    function the coordinator calls before build_execution_plan()."""
    return CommandFilter().evaluate(
        target_position_internal=target_internal,
        current_position_internal=current_internal,
        execution_mode=ExecutionMode.AUTOMATIC,
        is_safety=is_safety,
        is_manual_override=False,
        is_cover_available=True,
        state_guard_allowed=True,
        execution_capability=ExecutionCapability(position_tolerance=tolerance),
        invert_position=False,
    )


def _intents_for_window(window_id: str, cover_entity_ids: list[str], filter_result):
    """One build_execution_plan() call per window — mirrors coordinator.py's
    per-window Pass 2 construction (one CommandFilterResult, N cover intents)."""
    plan = build_execution_plan(
        window_id=window_id,
        cover_entity_ids=cover_entity_ids,
        filter_result=filter_result,
        decided_by="TestEvaluator",
        now=_NOW,
    )
    return list(plan.intents)


async def _dispatch_one_intent(gsd: GlobalSerialDispatch, hass, intent, now_fn):
    """Exact mirror of coordinator.py's per-intent dispatch block (Execution
    Pipeline Pass 2): acquire the shared lock, wait out the throttle, dispatch,
    record on SENT. Blocked intents never touch the lock/throttle at all —
    same_position/no-op must not consume a dispatch slot."""
    if not intent.allowed:
        return build_blocked_result(intent, reason=f"command blocked: {intent.blocked_reason}"), False

    async with gsd.lock:
        wait = gsd.time_until_next_allowed()
        throttled = wait.total_seconds() > 0
        if throttled:
            await asyncio.sleep(wait.total_seconds())
        result = await dispatch_cover_intent(hass, intent, now_utc=now_fn())
        if result.status is ExecutionStatus.SENT:
            gsd.record_dispatch(now_fn())
    return result, throttled


async def _dispatch_all(gsd, hass, intents, now_fn=None):
    if now_fn is None:
        now_fn = lambda: _NOW  # noqa: E731
    out = []
    for intent in intents:
        out.append(await _dispatch_one_intent(gsd, hass, intent, now_fn))
    return out


def _candidate(window_id, *, zone_id="z1", group="south", target_ha, in_sector=True):
    return ShadingGroupCandidate(
        window_id=window_id,
        zone_id=zone_id,
        shading_group_id=group,
        execution_mode_value="automatic",
        command_allowed=True,
        target_position_ha=target_ha,
        is_safety=False,
        is_override_active=False,
        cover_available=True,
        in_solar_sector=in_sector,
    )


# ---------------------------------------------------------------------------
# A. Two windows, same shading_group, harmonized to an IDENTICAL target
# ---------------------------------------------------------------------------

class TestHarmonizedWindowsStillSerializeThroughGlobalDispatch:
    def test_two_harmonized_windows_share_lock_and_throttle(self):
        # Window A wants HA target 30 (internal 70), window B wants 10 (internal
        # 90, more shade). Harmonization pulls A down to the group minimum (10)
        # — both end up with the SAME final target_position_ha, exactly the
        # scenario in the audit request.
        candidates = {
            "win_a": _candidate("win_a", target_ha=30),
            "win_b": _candidate("win_b", target_ha=10),
        }
        harm = compute_harmonization(candidates)
        assert harm["win_a"].harmonized is True
        assert harm["win_a"].final_target_position_ha == 10
        assert harm["win_b"].final_target_position_ha == 10  # already at minimum

        # Both windows now build their OWN independent execution plan from the
        # harmonized target — exactly as coordinator.py does per window.
        cfr_a = _cfr(target_internal=100 - harm["win_a"].final_target_position_ha,
                     current_internal=30)  # genuinely needs to move
        cfr_b = _cfr(target_internal=100 - harm["win_b"].final_target_position_ha,
                     current_internal=50)  # genuinely needs to move
        intents = (
            _intents_for_window("win_a", ["cover.win_a"], cfr_a)
            + _intents_for_window("win_b", ["cover.win_b"], cfr_b)
        )
        assert len(intents) == 2

        gsd = GlobalSerialDispatch(min_interval=timedelta(seconds=0.05))
        hass = _make_hass()
        results = asyncio.run(_dispatch_all(gsd, hass, intents))

        assert [r[0].status for r in results] == [ExecutionStatus.SENT, ExecutionStatus.SENT]
        # First dispatch is immediate; the second — despite an IDENTICAL
        # harmonized target on a DIFFERENT window — must still wait for the
        # shared global throttle. Harmonization does not create a parallel
        # or unthrottled dispatch path.
        assert results[0][1] is False
        assert results[1][1] is True
        assert hass.services.async_call.call_count == 2

    def test_three_harmonized_windows_same_group_all_serialize(self):
        candidates = {
            f"win_{i}": _candidate(f"win_{i}", target_ha=20) for i in range(3)
        }
        harm = compute_harmonization(candidates)
        assert all(h.final_target_position_ha == 20 for h in harm.values())

        intents = []
        for i in range(3):
            cfr = _cfr(target_internal=100 - 20, current_internal=10 + i * 5)
            intents += _intents_for_window(f"win_{i}", [f"cover.win_{i}"], cfr)

        gsd = GlobalSerialDispatch(min_interval=timedelta(seconds=0.05))
        hass = _make_hass()
        results = asyncio.run(_dispatch_all(gsd, hass, intents))

        assert [r[1] for r in results] == [False, True, True]
        assert hass.services.async_call.call_count == 3


# ---------------------------------------------------------------------------
# B. One window, two assigned cover entities
# ---------------------------------------------------------------------------

class TestMultiCoverWindowStillSerializesThroughGlobalDispatch:
    def test_two_covers_one_window_share_lock_and_throttle(self):
        # A single window with two cover entities (e.g. a wide window with a
        # left/right shutter pair) gets ONE CommandFilterResult, but
        # build_execution_plan() must still produce one intent PER cover.
        cfr = _cfr(target_internal=80, current_internal=0)
        intents = _intents_for_window(
            "win_wide", ["cover.win_wide_left", "cover.win_wide_right"], cfr
        )
        assert len(intents) == 2
        assert intents[0].cover_entity_id != intents[1].cover_entity_id

        gsd = GlobalSerialDispatch(min_interval=timedelta(seconds=0.05))
        hass = _make_hass()
        results = asyncio.run(_dispatch_all(gsd, hass, intents))

        assert [r[0].status for r in results] == [ExecutionStatus.SENT, ExecutionStatus.SENT]
        assert results[0][1] is False
        assert results[1][1] is True, (
            "The second cover of the SAME window must still respect the "
            "global minimum inter-dispatch interval — no direct/parallel fire."
        )
        assert hass.services.async_call.call_count == 2


# ---------------------------------------------------------------------------
# C. same_position / no-op must not consume a dispatch slot
# ---------------------------------------------------------------------------

class TestSamePositionNoOpDoesNotConsumeDispatchSlot:
    def test_blocked_sibling_cover_skips_lock_other_cover_dispatches_normally(self):
        # One window, two covers: cover A is already at the target (blocked as
        # same_position, no real command needed); cover B genuinely needs to
        # move. Only cover B's dispatch should touch the global throttle.
        cfr_noop = _cfr(target_internal=80, current_internal=80)  # within tolerance
        cfr_move = _cfr(target_internal=80, current_internal=0)   # needs to move
        assert cfr_noop.allowed is False
        assert cfr_move.allowed is True

        intents = (
            _intents_for_window("win_a", ["cover.a_noop"], cfr_noop)
            + _intents_for_window("win_a", ["cover.a_move"], cfr_move)
        )

        gsd = GlobalSerialDispatch(min_interval=timedelta(seconds=0.05))
        hass = _make_hass()
        results = asyncio.run(_dispatch_all(gsd, hass, intents))

        assert results[0][0].status is ExecutionStatus.BLOCKED
        assert results[0][1] is False  # never entered the throttle gate at all
        assert results[1][0].status is ExecutionStatus.SENT
        assert results[1][1] is False  # first REAL dispatch this cycle — no wait
        # Only the genuinely-moved cover reaches HA.
        assert hass.services.async_call.call_count == 1

    def test_noop_cover_does_not_arm_throttle_for_next_real_dispatch(self):
        # A blocked (same_position) intent must not call record_dispatch —
        # the NEXT real dispatch (even moments later) should not be throttled
        # because of a no-op that never actually moved anything.
        cfr_noop = _cfr(target_internal=50, current_internal=50)
        cfr_move = _cfr(target_internal=80, current_internal=0)
        intents = (
            _intents_for_window("win_a", ["cover.a_noop"], cfr_noop)
            + _intents_for_window("win_b", ["cover.b_move"], cfr_move)
        )
        gsd = GlobalSerialDispatch(min_interval=timedelta(seconds=5.0))
        hass = _make_hass()
        results = asyncio.run(_dispatch_all(gsd, hass, intents))
        assert results[0][0].status is ExecutionStatus.BLOCKED
        assert results[1][0].status is ExecutionStatus.SENT
        assert results[1][1] is False, (
            "A same_position no-op must not arm the global throttle for the "
            "next window's genuine dispatch."
        )


# ---------------------------------------------------------------------------
# D. Safety: harmonization excludes safety, but safety dispatches still
#    sequence through the same lock/throttle (priority, not a timing bypass).
# ---------------------------------------------------------------------------

class TestSafetyAcrossHarmonizedWindowsStillSequenced:
    def test_safety_excluded_from_harmonization_but_still_throttled(self):
        # Safety candidates are never harmonized (is_safety=True is excluded
        # by _is_eligible), so each keeps its own safety target — but the
        # resulting dispatches must still share the lock/throttle.
        safety_candidate = ShadingGroupCandidate(
            window_id="win_safety", zone_id="z1", shading_group_id="south",
            execution_mode_value="automatic", command_allowed=True,
            target_position_ha=90, is_safety=True, is_override_active=False,
            cover_available=True,
        )
        normal_candidate = _candidate("win_normal", target_ha=20)
        harm = compute_harmonization({
            "win_safety": safety_candidate, "win_normal": normal_candidate,
        })
        # Only one eligible (non-safety) member in the group -> not harmonized.
        assert harm["win_safety"].harmonized is False
        assert harm["win_normal"].harmonized is False

        cfr_safety_a = _cfr(target_internal=10, current_internal=90, is_safety=True)
        cfr_safety_b = _cfr(target_internal=10, current_internal=90, is_safety=True)
        intents = (
            _intents_for_window("win_safety_a", ["cover.safety_a"], cfr_safety_a)
            + _intents_for_window("win_safety_b", ["cover.safety_b"], cfr_safety_b)
        )

        gsd = GlobalSerialDispatch(min_interval=timedelta(seconds=0.05))
        hass = _make_hass()
        results = asyncio.run(_dispatch_all(gsd, hass, intents))

        assert [r[0].status for r in results] == [ExecutionStatus.SENT, ExecutionStatus.SENT]
        assert results[0][1] is False
        assert results[1][1] is True, (
            "Safety has queue priority, not a timing exemption — the second "
            "safety dispatch must still respect the global minimum interval."
        )
        assert hass.services.async_call.call_count == 2
