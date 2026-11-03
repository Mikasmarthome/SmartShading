"""Real-HA proof for B3-011 item 1 -- NightContact-Hold morning release.

engines/night_contact_hold.py's own docstring already documents the
release rule: NightContactHold.on_lifecycle_transition(night_active=False)
resets blocked_this_night/caught_up_this_night/night_vent_active, and this
is called every cycle in coordinator.py's per-window loop (unconditionally,
BEFORE evaluate()), so once _night_interval_active goes False the very next
evaluate() call returns PASS_THROUGH -- no separate release mechanism, no
second dispatch pipeline, the SAME shared per-window loop every other
decision goes through.

What was NOT previously proven end-to-end is that this actually happens
through a REAL coordinator that lived through a real Option-A block during
the night (not a fresh missed-event catch-up instance, which starts with an
empty _night_contact_holds dict and has nothing to release in the first
place -- see test_real_ha_morning_one_shot.py's own module docstring for
why that file's two-phase pattern always uses a fresh Phase-B coordinator).
This file keeps the SAME coordinator alive across the boundary instead.
"""
from __future__ import annotations

from datetime import datetime, time as dt_time, timezone

import pytest
from freezegun import freeze_time
from homeassistant.components.cover import CoverEntityFeature
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE
from custom_components.smartshading.models.cover_group import CoverGroup
from custom_components.smartshading.models.lifecycle import (
    LifecycleState, MorningTrigger, NightDayLifecycleConfig, NightTrigger,
)
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig


@pytest.fixture(autouse=True)
def _virtual_completion_time(virtual_completion_clock):
    """Time acceleration only: the real completion logic runs against a virtual
    clock (conftest), so an estimated travel time is not waited out.  This
    scenario dispatches a single cover, so no settle pause occurs and no pacing
    constant is touched."""


_NIGHT_TIME = dt_time(22, 0)
_MORNING_TIME = dt_time(6, 30)
_NIGHT_INSTANT = datetime(2026, 8, 10, 22, 0, 0, tzinfo=timezone.utc)
_MORNING_INSTANT = datetime(2026, 8, 11, 10, 0, 0, tzinfo=timezone.utc)


def _set_sun_above_horizon(hass) -> None:
    hass.states.async_set("sun.sun", "above_horizon", {
        "azimuth": 0.0, "elevation": 30.0,
        "next_rising": "2026-08-12T05:00:00+00:00",
        "next_setting": "2026-08-11T19:00:00+00:00",
    })


class _CoverServiceRecorder:
    def __init__(self, hass) -> None:
        self.hass = hass
        self.position_calls: list[dict] = []

    def install(self) -> None:
        async def _set_position(call) -> None:
            entity_id = call.data["entity_id"]
            position = call.data["position"]
            self.position_calls.append(dict(call.data))
            self.hass.states.async_set(
                entity_id, "open" if position > 0 else "closed",
                {
                    "current_position": position,
                    "supported_features": CoverEntityFeature.SET_POSITION,
                },
            )

        async def _set_tilt(call) -> None:
            pass

        self.hass.services.async_register("cover", "set_cover_position", _set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", _set_tilt)


def _entry_data() -> dict:
    """One window, a contact sensor, Option A (night_block_on_window_open)
    enabled. morning_position must be a real int whenever morning_enabled is
    True -- config_flow.py's CONF_MORNING_POSITION is vol.Required with a
    default, so a real installation can never reach morning_enabled=True
    with morning_position=None; models/window_decision_input.py's own
    build_window_decision_input() does not guard against that combination
    (confirmed: it crashes _ha_to_internal(None) unconditionally whenever
    morning_enabled is True), so this test deliberately never constructs
    that unreachable-via-UI combination."""
    zone = ZoneConfig(
        id="zoneNC", name="Living Room",
        execution=ZoneExecutionConfig(learning_enabled=True, active_control_enabled=True),
    )
    window = WindowConfig(
        id="windowNC", name="South Window", zone_id="zoneNC",
        azimuth=180.0, floor_level=0, cover_group_id="cgNC",
        contact_sensor_entity_id="binary_sensor.contactNC",
        night_block_on_window_open=True,
    )
    cover_group = CoverGroup(id="cgNC", window_id="windowNC", cover_ids=["cover.nc1"])
    lifecycle_config = NightDayLifecycleConfig(
        id="default",
        night_enabled=True, night_trigger=NightTrigger.FIXED_TIME, night_fixed_time=_NIGHT_TIME,
        night_position=0,
        morning_enabled=True, morning_trigger=MorningTrigger.FIXED_TIME,
        morning_fixed_time=_MORNING_TIME, morning_position=70,
    )
    data = SmartShadingConfigEntryData(
        name="Test Zone NightContact", use_home_location=False,
        zones=[zone], windows=[window], cover_groups=[cover_group],
        lifecycle_config=lifecycle_config,
    )
    return to_storage_dict(data)


class TestRealHANightContactMorningRelease:
    async def test_blocked_hold_releases_at_morning_and_a_fresh_decision_is_dispatched(
        self, hass,
    ) -> None:
        await hass.config.async_set_time_zone("UTC")
        _set_sun_above_horizon(hass)
        hass.states.async_set("cover.nc1", "closed", {
            "current_position": 0,
            "supported_features": CoverEntityFeature.SET_POSITION,
        })
        # Contact OPEN ("on") for the whole night -- Option A must BLOCK the
        # real night move and never mark caught_up.
        hass.states.async_set("binary_sensor.contactNC", "on")

        recorder = _CoverServiceRecorder(hass)
        recorder.install()
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={**_entry_data(), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
            entry_id="e_nightcontact",
        )
        entry.add_to_hass(hass)

        with freeze_time(_NIGHT_INSTANT, real_asyncio=True) as frozen:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            coordinator = entry.runtime_data.coordinator
            assert coordinator._lifecycle_state is LifecycleState.NIGHT, (
                "Phase A must reach a genuine NIGHT state for the block to be real"
            )

            hold = coordinator._night_contact_holds.get("windowNC")
            assert hold is not None, "NightContactHold must exist for a contact-configured window"
            assert hold.blocked_this_night is True, (
                "Option A must have blocked the night move while the contact "
                "was open -- otherwise this test isn't proving what it claims to"
            )
            assert hold.caught_up_this_night is False
            # The block must be real: no dispatch was ever sent for the night move.
            assert recorder.position_calls == []

            # Cross the boundary WITHOUT unloading -- the SAME coordinator
            # instance, so its in-memory NightContactHold state is the thing
            # actually being tested (a fresh Phase-B coordinator, like
            # test_real_ha_morning_one_shot.py's own _setup_zone(), would
            # start with an empty _night_contact_holds dict and prove nothing
            # about release).
            frozen.move_to(_MORNING_INSTANT)
            # See _setup_zone_no_jump()'s own docstring in
            # test_real_ha_morning_one_shot.py for the full root cause: tear
            # down the real-but-frozen-derived lifecycle boundary timer
            # before it can be reached by a real select() wait -- this test
            # drives the transition directly via async_refresh() instead.
            coordinator.async_teardown_lifecycle_boundary_timer()

            await coordinator.async_refresh()
            await hass.async_block_till_done()

        assert coordinator._lifecycle_state is not LifecycleState.NIGHT, (
            "the coordinator's own lifecycle state must have actually left NIGHT"
        )
        assert hold.blocked_this_night is False, (
            "the Option-A block must be released once the night phase ends"
        )
        assert hold.caught_up_this_night is False, (
            "release is not a catch-up -- no morning_position is configured, "
            "so nothing should be marked caught_up either"
        )

        # The fresh decision must be the REAL current one -- MorningEvaluator's
        # own configured target (70), reached through the SAME shared
        # PositionResolver pool B3-010 wired it into, not the stale
        # NightContactBlock (shading_state=OPEN, target=None) artifact, and
        # not silently nothing.
        assert len(recorder.position_calls) == 1, (
            f"exactly one real dispatch reflecting the fresh post-release "
            f"decision, got {recorder.position_calls}"
        )
        assert recorder.position_calls[0]["entity_id"] == "cover.nc1"
        assert recorder.position_calls[0]["position"] == 70, (
            "the released decision must be MorningEvaluator's real configured "
            "target (70), proving NightContactBlock is no longer in the way "
            "and a genuinely fresh decision -- not the stale block -- was made"
        )

        # A further cycle must not re-dispatch -- the released decision is
        # stable, not a one-shot artifact that gets re-sent every cycle.
        with freeze_time(_MORNING_INSTANT, real_asyncio=True):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert len(recorder.position_calls) == 1, (
            f"no repeated dispatch on a later cycle, got {recorder.position_calls}"
        )
