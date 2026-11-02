"""A1 -- Tier-1 Safety (Rain / Wind / Storm) must act without usable sun data.

Real Home Assistant, real config entry, real coordinator, real service
registry. `sun.sun` is made unusable in the three ways it can realistically
be (state `unknown` / `unavailable` without azimuth+elevation attributes, or
the entity missing entirely). Safety must still drive the cover to its
hardware-safe position; ordinary (non-safety) automation must stay
conservative and send nothing.

An AWNING is used because it has wind AND rain protection on by default and a
safe position (HA 0 = retracted) that differs from the cover's starting
position (HA 100 = extended), so every scenario needs a real movement.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    to_storage_dict,
)
from custom_components.smartshading.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_ZONE
from custom_components.smartshading.models.cover_group import CoverGroup, CoverHardwareType
from custom_components.smartshading.models.window import WindowConfig
from custom_components.smartshading.models.zone import ZoneConfig
from custom_components.smartshading.models.zone_execution_config import ZoneExecutionConfig

_NOON = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
_COVER = "cover.a1_awning"
_RAIN = "binary_sensor.a1_rain"
_WIND = "sensor.a1_wind"


def _entry(entry_id: str, *, hardware: CoverHardwareType = CoverHardwareType.AWNING) -> MockConfigEntry:
    zone = ZoneConfig(
        id="z1", name="Terrace",
        execution=ZoneExecutionConfig(learning_enabled=True, active_control_enabled=True),
    )
    window = WindowConfig(
        id="w1", name="Awning Window", zone_id="z1",
        azimuth=180.0, floor_level=0, cover_group_id="cg1",
    )
    group = CoverGroup(id="cg1", window_id="w1", cover_ids=[_COVER], hardware_type=hardware)
    data = SmartShadingConfigEntryData(
        name="A1 Zone", use_home_location=False,
        zones=[zone], windows=[window], cover_groups=[group],
        rain_sensor_id=_RAIN, wind_speed_sensor_id=_WIND,
    )
    return MockConfigEntry(
        domain=DOMAIN,
        data={**to_storage_dict(data), CONF_ENTRY_TYPE: ENTRY_TYPE_ZONE},
        entry_id=entry_id,
    )


class _FeedbackRecorder:
    """Real cover services that also write the resulting position back, like
    a real cover integration does."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.position_calls: list[dict] = []

    def install(self) -> None:
        async def _set_position(call) -> None:
            self.position_calls.append(dict(call.data))
            pos = call.data["position"]
            self.hass.states.async_set(
                call.data["entity_id"], "open" if pos > 0 else "closed",
                {"current_position": pos},
            )

        async def _set_tilt(call) -> None:
            return None

        self.hass.services.async_register("cover", "set_cover_position", _set_position)
        self.hass.services.async_register("cover", "set_cover_tilt_position", _set_tilt)


def _sun_valid(hass: HomeAssistant) -> None:
    # Sun in the north, window faces south: outside the solar sector, so no
    # comfort protection competes with the safety decision under test.
    hass.states.async_set("sun.sun", "above_horizon", {
        "azimuth": 0.0, "elevation": 30.0,
        "next_rising": "2026-08-12T05:00:00+00:00",
        "next_setting": "2026-08-11T19:00:00+00:00",
    })


def _sun_unusable(hass: HomeAssistant, kind: str) -> None:
    if kind == "missing":
        hass.states.async_remove("sun.sun")
    else:
        # No azimuth/elevation attributes -- exactly what HA reports for an
        # `unknown` / `unavailable` sun entity.
        hass.states.async_set("sun.sun", kind)


async def _setup(hass: HomeAssistant, entry_id: str, **kwargs):
    await hass.config.async_set_time_zone("UTC")
    _sun_valid(hass)
    hass.states.async_set(_RAIN, "off")
    hass.states.async_set(_WIND, "3", {"unit_of_measurement": "m/s"})
    hass.states.async_set(_COVER, "open", {"current_position": 100})
    entry = _entry(entry_id, **kwargs)
    entry.add_to_hass(hass)
    recorder = _FeedbackRecorder(hass)
    recorder.install()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    # Clear Startup Grace so the next cycle is genuinely dispatch-capable.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    recorder.position_calls.clear()
    return entry, coordinator, recorder


SUN_KINDS = ["unknown", "unavailable", "missing"]


class TestSafetyActsWithoutSun:
    async def test_control_rain_with_valid_sun_retracts_awning(self, hass) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass, "a1_ctrl")
            hass.states.async_set(_RAIN, "on")
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert rec.position_calls, "baseline: rain with valid sun must dispatch"
        assert all(c["entity_id"] == _COVER and c["position"] == 0 for c in rec.position_calls)

    @pytest.mark.parametrize("kind", SUN_KINDS)
    async def test_rain_acts_when_sun_unusable(self, hass, kind) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass, f"a1_rain_{kind}")
            _sun_unusable(hass, kind)
            hass.states.async_set(_RAIN, "on")
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert rec.position_calls, f"Rain must retract the awning with sun.sun {kind}"
        assert all(c["entity_id"] == _COVER and c["position"] == 0 for c in rec.position_calls)

    async def test_wind_is_enabled_by_default_for_an_awning_even_with_valid_sun(self, hass) -> None:
        """A3: the coordinator resolves Wind protection per window from the
        hardware default (awning on), so the 14 m/s WIND_SAFE guard fires in a
        default installation instead of only STORM_SAFE at >= 20 m/s."""
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass, "a1_wind_default")
            hass.states.async_set(_WIND, "16", {"unit_of_measurement": "m/s"})
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert rec.position_calls
        assert all(c["position"] == 0 for c in rec.position_calls)

    @pytest.mark.parametrize("kind", SUN_KINDS)
    async def test_wind_acts_when_sun_unusable(self, hass, kind) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass, f"a1_wind_{kind}")
            # Explicitly enable the (default-off) WIND_SAFE guard -- see the
            # test above; this test is about sun independence, not enablement.
            coordinator._wind_protection_enabled = True
            _sun_unusable(hass, kind)
            hass.states.async_set(_WIND, "16", {"unit_of_measurement": "m/s"})  # >= 14, < 20
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert rec.position_calls, f"Wind must retract the awning with sun.sun {kind}"
        assert all(c["position"] == 0 for c in rec.position_calls)

    @pytest.mark.parametrize("kind", SUN_KINDS)
    async def test_storm_acts_when_sun_unusable(self, hass, kind) -> None:
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass, f"a1_storm_{kind}")
            _sun_unusable(hass, kind)
            hass.states.async_set(_WIND, "25", {"unit_of_measurement": "m/s"})  # >= 20
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert rec.position_calls, f"Storm must retract the awning with sun.sun {kind}"
        assert all(c["position"] == 0 for c in rec.position_calls)


class TestNormalAutomationStaysConservativeWithoutSun:
    @pytest.mark.parametrize("kind", SUN_KINDS)
    async def test_no_safety_no_sun_sends_nothing(self, hass, kind) -> None:
        """No rain/wind: an unusable sun must not make ordinary automation
        move anything (no daytime-fallback open, no solar action)."""
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass, f"a1_cons_{kind}", hardware=CoverHardwareType.GENERIC)
            hass.states.async_set(_COVER, "open", {"current_position": 40})
            _sun_unusable(hass, kind)
            await hass.async_block_till_done()
            for _ in range(3):
                await coordinator.async_refresh()
                await hass.async_block_till_done()
        assert rec.position_calls == []


class TestSunReturnsFreshEvaluation:
    async def test_valid_to_unusable_to_valid_is_a_fresh_evaluation(self, hass) -> None:
        """valid -> unusable (rain starts, cover retracts) -> valid again,
        rain still on: the cover stays retracted and no stale pre-outage plan
        is replayed (nothing extends it)."""
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, rec = await _setup(hass, "a1_cycle")
            _sun_unusable(hass, "unavailable")
            hass.states.async_set(_RAIN, "on")
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert rec.position_calls and rec.position_calls[-1]["position"] == 0

            _sun_valid(hass)
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert all(c["position"] == 0 for c in rec.position_calls), rec.position_calls

    async def test_sun_usability_change_requests_a_refresh_by_itself(self, hass) -> None:
        """Losing and regaining usable sun data must each request an immediate
        re-evaluation; the routine per-minute sun updates must not."""
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, _ = await _setup(hass, "a1_sunreq")
            requested: list[int] = []
            original = coordinator.async_request_refresh

            async def spy() -> None:
                requested.append(1)
                await original()

            coordinator.async_request_refresh = spy

            # Routine update: azimuth/elevation move, usability unchanged.
            hass.states.async_set("sun.sun", "above_horizon", {
                "azimuth": 10.0, "elevation": 31.0,
                "next_rising": "2026-08-12T05:00:00+00:00",
                "next_setting": "2026-08-11T19:00:00+00:00",
            })
            await hass.async_block_till_done()
            assert requested == []

            _sun_unusable(hass, "unavailable")      # usable -> unusable
            await hass.async_block_till_done()
            assert len(requested) == 1

            _sun_unusable(hass, "unknown")           # unusable -> unusable
            await hass.async_block_till_done()
            assert len(requested) == 1

            _sun_valid(hass)                         # unusable -> usable
            await hass.async_block_till_done()
            assert len(requested) == 2

    async def test_sun_loss_invalidates_a_running_comfort_plan(self, hass) -> None:
        """Losing the sun bumps the dispatch generation and signals a running
        comfort dispatch (so a plan computed with sun data cannot go on
        executing); regaining it does not need to."""
        with freeze_time(_NOON, real_asyncio=True):
            _, coordinator, _ = await _setup(hass, "a1_sungen")

            async def no_refresh() -> None:
                return None

            # Isolate the listener's own effect from the cycle it requests
            # (a cycle bumps the generation by itself when it runs a plan).
            coordinator.async_request_refresh = no_refresh
            gen0 = coordinator._dispatch_generation
            _sun_unusable(hass, "unavailable")
            await hass.async_block_till_done()
            gen1 = coordinator._dispatch_generation
            _sun_valid(hass)
            await hass.async_block_till_done()
            gen2 = coordinator._dispatch_generation
        assert gen1 == gen0 + 1
        assert gen2 == gen1
