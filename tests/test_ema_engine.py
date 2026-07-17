"""Exponential Moving Average (EMA) pure engine — v1.2.0-beta.1, Beta.1-T4.

Pure unit coverage of engines/ema_engine.py's `ema_update()` function and
`EmaSmoother` class — no Home Assistant dependency, no coordinator involved
(see tests/test_coordinator_ema_integration.py for the wiring-level tests:
single insertion point, WindowDecisionInput propagation, restart-without-
persistence, and Lifecycle/Comfort/Protection regression).

Coverage:
  EMA-01  First valid sample seeds the EMA directly (no synthetic bias).
  EMA-02  Second sample blends via alpha.
  EMA-03  Multiple sequential samples converge as expected.
  EMA-04  alpha=1.0 disables smoothing (EMA always equals the latest sample).
  EMA-05  alpha near 0 barely moves the EMA.
  EMA-06  Unavailable-style None input never destroys existing EMA state.
  EMA-07  "unknown"-style None input never destroys existing EMA state.
  EMA-08  Bare None input never destroys existing EMA state.
  EMA-09  NaN input never destroys existing EMA state or NaN-poisons it.
  EMA-10  Infinity input never destroys existing EMA state.
  EMA-11  An outlier is damped, not adopted outright (proves smoothing math).
  EMA-12  Multiple named channels are fully independent (EmaSmoother).
  EMA-13  No double-smoothing: calling update() once per cycle per channel
          matches a single ema_update() call — repeated same-cycle calls
          would NOT match were the state mutated twice, so callers MUST
          call once per channel per cycle (documented contract; verified
          here by exact-value comparison against the pure function).
  EMA-14  reset() clears one channel without touching others.
  EMA-15  reset(None) clears every channel.
  EMA-16  Valid -> invalid -> valid transition resumes blending from the
          last valid EMA, not from the invalid gap.
  EMA-17  Bug-injection sanity check: a broken "always overwrite" EMA
          produces a different result than the real damped EMA for the
          same input sequence — proves the alpha blending is real.
"""
from __future__ import annotations

import math

from custom_components.smartshading.config_entry_data import (
    SmartShadingConfigEntryData,
    from_storage_dict,
    to_storage_dict,
)
from custom_components.smartshading.engines.ema_engine import EmaSmoother, ema_update


# ---------------------------------------------------------------------------
# EMA-01 .. EMA-05 — core algorithm.
# ---------------------------------------------------------------------------

class TestCoreAlgorithm:
    def test_first_sample_seeds_ema_directly(self):
        assert ema_update(None, 22.5, 0.3) == 22.5

    def test_second_sample_blends_via_alpha(self):
        first = ema_update(None, 20.0, 0.5)
        second = ema_update(first, 30.0, 0.5)
        assert second == 25.0  # 0.5*30 + 0.5*20

    def test_multiple_sequential_samples_converge(self):
        value: float | None = None
        for _ in range(50):
            value = ema_update(value, 100.0, 0.3)
        assert value is not None
        assert abs(value - 100.0) < 0.001  # converges toward a constant input

    def test_alpha_one_disables_smoothing(self):
        value = ema_update(None, 10.0, 1.0)
        value = ema_update(value, 999.0, 1.0)
        assert value == 999.0  # EMA always equals the latest sample

    def test_alpha_near_zero_barely_moves(self):
        value = ema_update(None, 10.0, 0.01)
        value = ema_update(value, 1000.0, 0.01)
        assert value is not None
        assert abs(value - 10.0) < 10.0  # moved only slightly toward 1000


# ---------------------------------------------------------------------------
# EMA-06 .. EMA-10 — invalid samples never destroy state.
# ---------------------------------------------------------------------------

class TestInvalidSamplesNeverDestroyState:
    def test_none_after_valid_keeps_previous(self):
        value = ema_update(None, 25.0, 0.3)
        assert ema_update(value, None, 0.3) == value

    def test_unavailable_style_none_keeps_previous(self):
        # Coordinator translates "unavailable" HA states to None before ever
        # reaching ema_update() — this proves the None contract it relies on.
        value = ema_update(None, 25.0, 0.3)
        assert ema_update(value, None, 0.3) == value

    def test_unknown_style_none_keeps_previous(self):
        value = ema_update(None, 25.0, 0.3)
        assert ema_update(value, None, 0.3) == value

    def test_nan_keeps_previous_and_does_not_poison_state(self):
        value = ema_update(None, 25.0, 0.3)
        after_nan = ema_update(value, float("nan"), 0.3)
        assert after_nan == value
        assert not math.isnan(after_nan)
        # A subsequent valid sample must still blend normally (state wasn't poisoned).
        after_valid = ema_update(after_nan, 30.0, 0.3)
        assert after_valid == ema_update(value, 30.0, 0.3)

    def test_infinity_keeps_previous(self):
        value = ema_update(None, 25.0, 0.3)
        assert ema_update(value, float("inf"), 0.3) == value
        assert ema_update(value, float("-inf"), 0.3) == value

    def test_first_sample_invalid_stays_none(self):
        assert ema_update(None, None, 0.3) is None
        assert ema_update(None, float("nan"), 0.3) is None


# ---------------------------------------------------------------------------
# EMA-11 — outlier damping.
# ---------------------------------------------------------------------------

class TestOutlierDamping:
    def test_single_outlier_is_damped_not_adopted(self):
        value = ema_update(None, 200.0, 0.3)  # steady baseline
        after_outlier = ema_update(value, 5000.0, 0.3)  # one wild spike
        assert after_outlier == 200.0 + 0.3 * (5000.0 - 200.0)
        assert after_outlier < 5000.0  # damped, not adopted outright
        assert after_outlier > 200.0


# ---------------------------------------------------------------------------
# EMA-12 .. EMA-15 — EmaSmoother: independent named channels.
# ---------------------------------------------------------------------------

class TestEmaSmootherChannels:
    def test_channels_are_independent(self):
        smoother = EmaSmoother()
        outdoor = smoother.update("outdoor_temperature", 10.0, 0.3)
        solar = smoother.update("solar_radiation", 500.0, 0.3)
        assert outdoor == 10.0
        assert solar == 500.0
        smoother.update("solar_radiation", None, 0.3)  # invalid sample for solar only
        assert smoother.update("outdoor_temperature", 12.0, 0.3) == ema_update(10.0, 12.0, 0.3)

    def test_calling_once_per_cycle_matches_pure_function(self):
        smoother = EmaSmoother()
        expected = ema_update(None, 42.0, 0.4)
        actual = smoother.update("wind_speed", 42.0, 0.4)
        assert actual == expected
        expected2 = ema_update(expected, 44.0, 0.4)
        actual2 = smoother.update("wind_speed", 44.0, 0.4)
        assert actual2 == expected2

    def test_reset_single_channel_leaves_others_untouched(self):
        smoother = EmaSmoother()
        smoother.update("outdoor_temperature", 10.0, 0.3)
        smoother.update("solar_radiation", 500.0, 0.3)
        smoother.reset("outdoor_temperature")
        # Reset channel reseeds fresh (no synthetic bias) on next valid sample.
        assert smoother.update("outdoor_temperature", 99.0, 0.3) == 99.0
        # Untouched channel keeps blending from its prior state.
        assert smoother.update("solar_radiation", 600.0, 0.3) == ema_update(500.0, 600.0, 0.3)

    def test_reset_all_channels(self):
        smoother = EmaSmoother()
        smoother.update("outdoor_temperature", 10.0, 0.3)
        smoother.update("solar_radiation", 500.0, 0.3)
        smoother.reset()
        assert smoother.update("outdoor_temperature", 99.0, 0.3) == 99.0
        assert smoother.update("solar_radiation", 777.0, 0.3) == 777.0


# ---------------------------------------------------------------------------
# EMA-16 — valid -> invalid -> valid resumes from the last valid EMA.
# ---------------------------------------------------------------------------

class TestValidInvalidValidTransition:
    def test_resumes_blending_from_last_valid_value(self):
        smoother = EmaSmoother()
        smoother.update("outdoor_temperature", 20.0, 0.5)
        v1 = smoother.update("outdoor_temperature", 22.0, 0.5)  # -> 21.0
        v2 = smoother.update("outdoor_temperature", None, 0.5)  # invalid, unchanged
        assert v2 == v1
        v3 = smoother.update("outdoor_temperature", None, 0.5)  # still invalid
        assert v3 == v1
        v4 = smoother.update("outdoor_temperature", 24.0, 0.5)  # resumes from v1
        assert v4 == ema_update(v1, 24.0, 0.5)


# ---------------------------------------------------------------------------
# EMA-17 — bug-injection sanity check.
# ---------------------------------------------------------------------------

class TestSanityCheck:
    def test_broken_overwrite_ema_diverges_from_real_ema(self):
        """A deliberately-broken 'EMA' that just overwrites instead of
        blending must diverge from the real implementation for the same
        input sequence — proves the alpha-weighted math in ema_update() is
        actually exercised by these tests, not a tautology."""

        def _broken_overwrite(previous, new_value, alpha):
            return new_value if new_value is not None else previous

        real_value: float | None = None
        broken_value: float | None = None
        for sample in (10.0, 50.0, 12.0, 48.0, 11.0):
            real_value = ema_update(real_value, sample, 0.3)
            broken_value = _broken_overwrite(broken_value, sample, 0.3)
        assert real_value != broken_value


# ---------------------------------------------------------------------------
# Config storage round-trip: ema_enabled / ema_alpha on SmartShadingConfigEntryData.
# ---------------------------------------------------------------------------

class TestConfigStorageRoundTrip:
    def test_default_is_disabled_with_default_alpha(self):
        data = SmartShadingConfigEntryData(name="Test", use_home_location=True)
        assert data.ema_enabled is False
        assert data.ema_alpha == 0.3

    def test_explicit_values_survive_round_trip(self):
        data = SmartShadingConfigEntryData(
            name="Test", use_home_location=True, ema_enabled=True, ema_alpha=0.6,
        )
        stored = to_storage_dict(data)
        assert stored["ema_enabled"] is True
        assert stored["ema_alpha"] == 0.6
        restored = from_storage_dict(stored)
        assert restored.ema_enabled is True
        assert restored.ema_alpha == 0.6

    def test_missing_keys_default_safely(self):
        """Pre-T4 configs (no ema_* keys at all) -> disabled, default alpha,
        byte-for-byte the pre-T4 behavior."""
        raw = {"name": "Test", "use_home_location": True}
        restored = from_storage_dict(raw)
        assert restored.ema_enabled is False
        assert restored.ema_alpha == 0.3

    def test_malformed_alpha_falls_back_to_default(self):
        raw = {"name": "Test", "use_home_location": True, "ema_alpha": "not-a-number"}
        restored = from_storage_dict(raw)
        assert restored.ema_alpha == 0.3

    def test_out_of_range_alpha_falls_back_to_default(self):
        raw_low = {"name": "Test", "use_home_location": True, "ema_alpha": 0.0}
        raw_high = {"name": "Test", "use_home_location": True, "ema_alpha": 1.5}
        assert from_storage_dict(raw_low).ema_alpha == 0.3
        assert from_storage_dict(raw_high).ema_alpha == 0.3

    def test_boolean_alpha_value_falls_back_to_default(self):
        """bool is a subclass of int in Python — explicitly rejected so a
        stray `true`/`false` in ema_alpha never silently becomes 1.0/0.0."""
        raw = {"name": "Test", "use_home_location": True, "ema_alpha": True}
        assert from_storage_dict(raw).ema_alpha == 0.3

    def test_malformed_enabled_defaults_via_bool_coercion(self):
        raw = {"name": "Test", "use_home_location": True, "ema_enabled": None}
        assert from_storage_dict(raw).ema_enabled is False
