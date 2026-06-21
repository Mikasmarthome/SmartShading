"""Capability Detector (ARCHITECTURE.md §6.1). Derives a CoverCapability
from a real Home Assistant cover entity.

This is deliberately the ONE place in the cover-related code that imports
Home Assistant - cover_control/ stays HA-independent and unit-testable
(see scripts/smoke_test_core.py). Only the pure classification logic
(classify_cover_profile) lives there; everything that needs `hass.states`
or the entity registry lives here.

No service calls, no commands - read-only detection.
"""
from __future__ import annotations

from homeassistant.components.cover import CoverEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .cover_control.cover_capabilities import CoverCapability, classify_cover_profile

# ARCHITECTURE.md §6.1 "Zuverlässigkeits-Heuristik": integration domains
# known to lack reliable position feedback even when they report
# SET_POSITION support. Deliberately small and conservative - false
# negatives here (treating an actually-reliable cover as unreliable) are
# far cheaper than false positives, since AssumedStateManager already
# degrades gracefully for reliable covers treated as unreliable, but not
# the other way round. Extend as real-world cases are found.
UNRELIABLE_INTEGRATIONS: frozenset[str] = frozenset({"somfy_rts"})


class CapabilityDetector:
    """ARCHITECTURE.md §6.1. detect() is read-only: it never sends a
    command, never subscribes to anything - just inspects the current
    state and the entity registry once.
    """

    def detect(self, hass: HomeAssistant, entity_id: str) -> CoverCapability:
        state = hass.states.get(entity_id)

        if state is None or state.state in ("unknown", "unavailable"):
            # Entity not (yet) available - assume the most conservative
            # capability shape rather than raising. Re-detected on the
            # next call if the coordinator chooses to retry (not done
            # automatically yet - see final report's open risks).
            return CoverCapability(
                entity_id=entity_id,
                supports_position=False,
                supports_tilt=False,
                supports_open_close_only=True,
                supports_stop=False,
                supports_open=False,
                supports_close=False,
                has_reliable_position_feedback=False,
                assumed_state=True,
            )

        features = state.attributes.get("supported_features", 0)
        supports_position = bool(features & CoverEntityFeature.SET_POSITION)
        supports_tilt = bool(features & CoverEntityFeature.SET_TILT_POSITION)
        supports_stop = bool(features & CoverEntityFeature.STOP)
        supports_open = bool(features & CoverEntityFeature.OPEN)
        supports_close = bool(features & CoverEntityFeature.CLOSE)

        assumed_state = bool(state.attributes.get("assumed_state", False))
        integration_domain = self._get_integration_domain(hass, entity_id)
        has_reliable_position_feedback = self._detect_reliability(
            assumed_state=assumed_state,
            integration_domain=integration_domain,
            supports_position=supports_position,
        )

        cover_profile = classify_cover_profile(
            supports_position=supports_position,
            supports_stop=supports_stop,
            supports_open=supports_open,
            supports_close=supports_close,
        )

        return CoverCapability(
            entity_id=entity_id,
            supports_position=supports_position,
            supports_tilt=supports_tilt,
            supports_open_close_only=not supports_position,
            supports_stop=supports_stop,
            supports_open=supports_open,
            supports_close=supports_close,
            has_reliable_position_feedback=has_reliable_position_feedback,
            assumed_state=assumed_state,
            cover_profile=cover_profile,
            device_class=state.attributes.get("device_class"),
        )

    @staticmethod
    def _get_integration_domain(hass: HomeAssistant, entity_id: str) -> str | None:
        entry = er.async_get(hass).async_get(entity_id)
        return entry.platform if entry is not None else None

    @staticmethod
    def _detect_reliability(
        assumed_state: bool,
        integration_domain: str | None,
        supports_position: bool,
    ) -> bool:
        """ARCHITECTURE.md §6.1 "Zuverlässigkeits-Heuristik"."""
        if assumed_state:
            return False
        if integration_domain in UNRELIABLE_INTEGRATIONS:
            return False
        if not supports_position:
            # Nothing to be "reliable" about - no continuous position to
            # report in the first place. AssumedStateManager still tracks
            # these via TravelTracker-based estimation once commands exist.
            return False
        return True
