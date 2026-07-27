"""T21 Phase B — Manual Override release-mode simplification: migration and
active-override continuity protection.

T21 Phase B replaced the OptionsFlow's flat 7-value "release strategy"
dropdown with a simplified 4-concept model (release_mode + an optional
second-level sub-choice) — see models/manual_override.py's
OverrideReleaseMode/release_strategy_to_mode()/release_mode_to_strategy().

Critically, this is a PRESENTATION-LAYER change only:
  - The persisted ManualOverride.release_strategy string format (7 values)
    is completely UNCHANGED.
  - OverridePolicyConfig.release_strategy is still an OverrideReleaseStrategy
    (the original 7-value enum), completely UNCHANGED.
  - engines/override_release.py (compute_expiry, resolve_candidate_release,
    extends_on_renewal, uses_post_expiry_baseline) is completely UNCHANGED.

So there is no data migration to perform for existing installs — an active
override or a stored OverridePolicyConfig created by a pre-T21 build
restores and behaves IDENTICALLY after this change, because nothing about
its storage format or interpretation changed. This file proves exactly
that: for every one of the 7 legacy release_strategy values, persistence
round-trips and the real decision engine produce identical results before
and "after" T21 (there being no "after" difference to test against, since
the engine itself was never touched — the test instead proves the engine
still accepts and correctly interprets every legacy value, which is the
real continuity guarantee a beta user upgrading across this change needs).
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from custom_components.smartshading.engines.override_release import (
    compute_expiry,
    extends_on_renewal,
    resolve_candidate_release,
    uses_post_expiry_baseline,
)
from custom_components.smartshading.models.manual_override import (
    ManualOverride,
    OverrideReleaseStrategy,
)
from custom_components.smartshading.state_machine.states import DecisionCategory, ShadingState

_NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


class TestActiveOverridePersistenceUnaffectedForEveryLegacyStrategy:
    """A ManualOverride persisted (e.g. across an HA restart/reload) by ANY
    prior version of this integration — using any of the 7
    OverrideReleaseStrategy values — must restore to an identical object
    after T21 Phase B. This is the real "existing active overrides keep
    working after reload" guarantee, since the storage format never
    changed."""

    @pytest.mark.parametrize("strategy", [s.value for s in OverrideReleaseStrategy])
    def test_round_trip_preserves_every_legacy_strategy_value(self, strategy: str) -> None:
        original = ManualOverride(
            window_id="w1", override_position=42, started_at=_NOW,
            expires_at=_NOW, source="position_delta",
            overridden_state=ShadingState.OPEN, overridden_position=0,
            scope="daytime", release_strategy=strategy,
        )
        restored = ManualOverride.from_dict(original.to_dict())
        assert restored == original
        assert restored.release_strategy == strategy


class TestOverrideReleaseEngineUnaffectedForEveryLegacyStrategy:
    """The real decision engine (engines/override_release.py) must still
    accept and correctly interpret every one of the 7
    OverrideReleaseStrategy values exactly as before T21 Phase B — proving
    the OptionsFlow UI simplification never touched the engine those
    values are dispatched against."""

    @pytest.mark.parametrize("strategy", list(OverrideReleaseStrategy))
    def test_compute_expiry_never_raises_for_any_legacy_strategy(self, strategy) -> None:
        expiry = compute_expiry(
            strategy=strategy, now=_NOW, now_local=_NOW,
            duration_min=120, fixed_until=time(8, 0), safety_timeout_enabled=True,
        )
        assert expiry is not None

    def test_duration_still_extends_on_renewal(self) -> None:
        assert extends_on_renewal(OverrideReleaseStrategy.DURATION) is True

    @pytest.mark.parametrize("strategy", [
        OverrideReleaseStrategy.FIXED_TIME, OverrideReleaseStrategy.LIFECYCLE,
        OverrideReleaseStrategy.FIRST_COMFORT, OverrideReleaseStrategy.FIRST_PROTECTION,
        OverrideReleaseStrategy.FIRST_ANY_DECISION, OverrideReleaseStrategy.MANUAL,
    ])
    def test_only_duration_extends_on_renewal(self, strategy) -> None:
        assert extends_on_renewal(strategy) is False

    def test_first_comfort_releases_only_on_comfort_category(self) -> None:
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_COMFORT, category=DecisionCategory.COMFORT
        ) is True
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_COMFORT, category=DecisionCategory.PROTECTION
        ) is False

    def test_first_protection_releases_only_on_protection_category(self) -> None:
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_PROTECTION, category=DecisionCategory.PROTECTION
        ) is True
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_PROTECTION, category=DecisionCategory.COMFORT
        ) is False

    def test_first_any_decision_releases_on_either_category(self) -> None:
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_ANY_DECISION, category=DecisionCategory.COMFORT
        ) is True
        assert resolve_candidate_release(
            strategy=OverrideReleaseStrategy.FIRST_ANY_DECISION, category=DecisionCategory.PROTECTION
        ) is True

    def test_lifecycle_and_manual_never_match_a_candidate_release(self) -> None:
        for strategy in (OverrideReleaseStrategy.LIFECYCLE, OverrideReleaseStrategy.MANUAL,
                          OverrideReleaseStrategy.DURATION, OverrideReleaseStrategy.FIXED_TIME):
            for category in (DecisionCategory.COMFORT, DecisionCategory.PROTECTION):
                assert resolve_candidate_release(strategy=strategy, category=category) is False


class TestOverridePolicyConfigStorageUnaffected:
    """config_entry_data.py's stored OverridePolicyConfig round trip is
    untouched by T21 Phase B — its release_strategy field is still an
    OverrideReleaseStrategy, unaffected by the new UI-only OverrideReleaseMode."""

    def test_override_policy_config_still_uses_original_enum(self) -> None:
        from custom_components.smartshading.models.override_policy import OverridePolicyConfig

        cfg = OverridePolicyConfig(release_strategy=OverrideReleaseStrategy.FIRST_PROTECTION)
        assert cfg.release_strategy is OverrideReleaseStrategy.FIRST_PROTECTION
        assert isinstance(cfg.release_strategy, OverrideReleaseStrategy)
