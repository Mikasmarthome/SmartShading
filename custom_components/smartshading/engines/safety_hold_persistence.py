"""Persistence of the Rain / Wind / Storm release holds across a restart (A6).

The release holds (``SafetyHold``) keep a safety state after the danger itself
has gone: a dry cooldown for rain, ten minutes for wind and storm, and an
unlimited extension while the sensor has no reading.  They live in memory, so a
restart used to forget them: a cover that was held in its safe position could be
extended again as soon as the integration came back.

This module holds only the pure part: turning the latched holds into a small
payload and turning a stored payload back into holds.  The rules:

* Only holds that are latched are stored, each with the UTC time of its last
  trigger.  The payload carries a version and the time it was saved.
* A hold is not restored when it is older than ``RESTORE_MAX_AGE_S`` (it has long
  expired or can no longer be vouched for), when its time lies in the future
  beyond a small clock skew (a clock jump), when its window is unknown, or when
  the protection it belongs to is switched off for that window.  Anything
  unreadable is dropped, never guessed.
* A restored hold keeps its ORIGINAL trigger time.  The normal hold logic then
  decides on the first cycle: a sensor that confirms "safe" releases it at once
  if its time has elapsed, a sensor without a reading extends it (the existing
  rule), and a hold that is still running continues for the rest of its time.
  Nothing here releases a hold and nothing here makes one permanent.

No new Home Assistant import happens at module level; the store adapter imports
``Store`` lazily.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .safety_hold import SafetyHold

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "smartshading_safety_holds"
STORAGE_VERSION = 1
PAYLOAD_VERSION = 1

KIND_STORM = "storm"
KIND_WIND = "wind"
KIND_RAIN = "rain"
KINDS = (KIND_STORM, KIND_WIND, KIND_RAIN)

# A hold older than this is not restored: after this long nobody can vouch for the
# state any more, and a sensor that still reads "safe" would have released it anyway.
RESTORE_MAX_AGE_S = 6 * 3600.0
# A stored time further in the future than this means the clock moved: not trusted.
FUTURE_SKEW_S = 120.0
# Delay of the coalesced write while holds change.
SAVE_DELAY_S = 10.0


@dataclass
class RestoreResult:
    """The holds to restore (kind -> window id -> last trigger) and how the stored
    payload was handled."""

    restored: dict[str, dict[str, datetime]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)


def _empty_stats() -> dict[str, Any]:
    return {
        "payload_found": False,
        "restored": 0,
        "expired": 0,
        "invalid": 0,
        "unknown_window": 0,
        "protection_off": 0,
        "clock_skew": 0,
        "restored_by_kind": {kind: 0 for kind in KINDS},
        "saved_at": None,
    }


def serialize_holds(
    holds_by_kind: Mapping[str, Mapping[str, SafetyHold]], *, now: datetime,
) -> dict[str, Any]:
    """The payload for the latched holds (``{}`` per kind when none is latched)."""
    out: dict[str, dict[str, str]] = {}
    for kind in KINDS:
        latched = {
            window_id: hold._last_triggered.astimezone(timezone.utc).isoformat()
            for window_id, hold in (holds_by_kind.get(kind) or {}).items()
            if hold._last_triggered is not None
        }
        out[kind] = latched
    return {
        "version": PAYLOAD_VERSION,
        "saved_at": now.astimezone(timezone.utc).isoformat(),
        "holds": out,
    }


def holds_signature(payload: Mapping[str, Any]) -> str:
    """Content of the payload without its save time, to write only on change."""
    holds = payload.get("holds") or {}
    return repr([(kind, sorted((holds.get(kind) or {}).items())) for kind in KINDS])


def parse_holds(
    payload: Any,
    *,
    now: datetime,
    window_ids: set[str],
    protection_enabled: Callable[[str, str], bool],
) -> RestoreResult:
    """Holds worth restoring from a stored payload.  Never raises."""
    result = RestoreResult(stats=_empty_stats())
    stats = result.stats
    if not isinstance(payload, Mapping):
        return result
    stats["payload_found"] = True
    if payload.get("version") != PAYLOAD_VERSION:
        # An unknown (older or newer) layout is not interpreted.
        stats["invalid"] += 1
        return result
    stats["saved_at"] = payload.get("saved_at") if isinstance(payload.get("saved_at"), str) else None
    holds = payload.get("holds")
    if not isinstance(holds, Mapping):
        stats["invalid"] += 1
        return result
    now_utc = now.astimezone(timezone.utc)
    for kind in KINDS:
        entries = holds.get(kind)
        if not isinstance(entries, Mapping):
            continue
        for window_id, raw in entries.items():
            if not isinstance(window_id, str) or not isinstance(raw, str):
                stats["invalid"] += 1
                continue
            try:
                triggered = datetime.fromisoformat(raw)
            except ValueError:
                stats["invalid"] += 1
                continue
            if triggered.tzinfo is None:
                stats["invalid"] += 1
                continue
            triggered = triggered.astimezone(timezone.utc)
            if window_id not in window_ids:
                stats["unknown_window"] += 1
                continue
            if not protection_enabled(kind, window_id):
                stats["protection_off"] += 1
                continue
            age_s = (now_utc - triggered).total_seconds()
            if age_s < -FUTURE_SKEW_S:
                stats["clock_skew"] += 1
                continue
            if age_s > RESTORE_MAX_AGE_S:
                stats["expired"] += 1
                continue
            result.restored.setdefault(kind, {})[window_id] = min(triggered, now_utc)
            stats["restored"] += 1
            stats["restored_by_kind"][kind] += 1
    return result


class SafetyHoldStore:
    """The Home Assistant ``Store`` behind the pure functions above.

    Every method is safe to call: a storage problem is logged and reported as
    "nothing stored" / "not saved", never raised into the evaluation."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._key = f"{STORAGE_KEY}_{entry_id}"
        self._store: Any = None

    def _ensure(self) -> Any:
        if self._store is None:
            from homeassistant.helpers.storage import Store  # lazy HA import

            self._store = Store(self._hass, STORAGE_VERSION, self._key)
        return self._store

    async def async_load(self) -> Any:
        try:
            return await self._ensure().async_load()
        except Exception:  # noqa: BLE001 - a broken file must not block the start
            _LOGGER.warning("SmartShading: safety hold storage could not be read", exc_info=True)
            return None

    def schedule_save(self, payload: Mapping[str, Any]) -> bool:
        """Coalesced write of ``payload`` after ``SAVE_DELAY_S``."""
        try:
            store = self._ensure()
            delay_save = getattr(store, "async_delay_save", None)
            if delay_save is None:
                return False
            delay_save(lambda: dict(payload), SAVE_DELAY_S)
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.debug("SmartShading: safety hold save could not be scheduled", exc_info=True)
            return False

    async def async_save_now(self, payload: Mapping[str, Any]) -> bool:
        try:
            await self._ensure().async_save(dict(payload))
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.warning("SmartShading: safety hold storage could not be written", exc_info=True)
            return False

    async def async_remove(self) -> None:
        try:
            await self._ensure().async_remove()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("SmartShading: safety hold storage could not be removed", exc_info=True)


__all__ = [
    "KINDS", "KIND_RAIN", "KIND_STORM", "KIND_WIND", "PAYLOAD_VERSION",
    "RESTORE_MAX_AGE_S", "RestoreResult", "SafetyHoldStore",
    "holds_signature", "parse_holds", "serialize_holds",
]
