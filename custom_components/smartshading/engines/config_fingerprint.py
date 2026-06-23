"""Config fingerprint — LE 2.0 / Phase P2.7.

A deterministic, versioned fingerprint over the learning-relevant configuration
of a window.  Used to detect configuration changes that invalidate pending
outcomes and (later) shadow/experiment eligibility.

Determinism guarantees:
  - canonical JSON serialization (sorted keys, fixed separators)
  - explicit FINGERPRINT_VERSION prefix so the algorithm can evolve
  - SHA-256 (never Python's salted hash())
  - no random salt → identical config always yields the identical fingerprint
    across restarts and processes

Privacy:
  - Sensor entity IDs are folded INTO the hash (they affect learning validity)
    but never appear in clear text anywhere.
  - The fingerprint itself is suitable for the internal store and the local
    Support Export.  The Research Export must expose only config_generation /
    config_changed (see ConfigGenerationTracker), never the fingerprint.

No Home Assistant imports.  No I/O.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

FINGERPRINT_VERSION: int = 1


def _canonical(value: object) -> str:
    """Canonical, stable JSON for hashing.

    sort_keys ensures field-order independence; separators avoid whitespace
    variance; default=str makes enums / unusual types deterministic.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def compute_config_fingerprint(fields: dict) -> str:
    """Return ``"v{N}:{sha256hex16}"`` for the learning-relevant *fields*.

    Recommended fields (caller-provided, all optional/None-tolerant):
      behavior_mode, cover_hardware_type, azimuth, geometry,
      light_position, normal_position, strong_position,
      heat_outdoor_threshold_c, heat_indoor_threshold_c,
      light_threshold_wm2, normal_threshold_wm2, strong_threshold_wm2,
      sensor_ids (list[str] — folded in, never exposed),
      zone_id, shading_group_id
    """
    payload = _canonical(fields).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"v{FINGERPRINT_VERSION}:{digest}"


@dataclass
class ConfigGenerationTracker:
    """Maps a window's fingerprint to a monotonic generation counter.

    Persisted so the generation survives restarts.  When a window's fingerprint
    changes, its generation increments — the Research Export exposes only this
    integer (and a config_changed flag), never the fingerprint, so no
    house-recognizable value leaves the system.
    """

    # window_id -> last known fingerprint
    _fingerprints: dict[str, str]
    # window_id -> current generation (>=1 once seen)
    _generations: dict[str, int]

    def __init__(self) -> None:
        self._fingerprints = {}
        self._generations = {}

    def observe(self, window_id: str, fingerprint: str) -> tuple[int, bool]:
        """Record *fingerprint* for *window_id*.

        Returns (generation, changed).  changed is True only when the
        fingerprint differs from the previously stored one (not on first sight).
        """
        prev = self._fingerprints.get(window_id)
        if prev is None:
            self._fingerprints[window_id] = fingerprint
            self._generations[window_id] = 1
            return 1, False
        if prev == fingerprint:
            return self._generations[window_id], False
        # Changed
        self._fingerprints[window_id] = fingerprint
        self._generations[window_id] = self._generations.get(window_id, 1) + 1
        return self._generations[window_id], True

    def generation(self, window_id: str) -> int:
        return self._generations.get(window_id, 0)

    def fingerprint(self, window_id: str) -> str | None:
        return self._fingerprints.get(window_id)

    def to_storage_dict(self) -> dict:
        return {
            "fingerprint_version": FINGERPRINT_VERSION,
            "windows": {
                wid: {"fingerprint": self._fingerprints[wid], "generation": self._generations[wid]}
                for wid in self._fingerprints
            },
        }

    @classmethod
    def from_storage_dict(cls, data: object) -> "ConfigGenerationTracker":
        tracker = cls()
        if not isinstance(data, dict):
            return tracker
        windows = data.get("windows", {})
        if not isinstance(windows, dict):
            return tracker
        for wid, entry in windows.items():
            if not isinstance(entry, dict):
                continue
            fp = entry.get("fingerprint")
            gen = entry.get("generation")
            if isinstance(fp, str) and isinstance(gen, int):
                tracker._fingerprints[str(wid)] = fp
                tracker._generations[str(wid)] = gen
        return tracker

    def remove_window(self, window_id: str) -> None:
        self._fingerprints.pop(window_id, None)
        self._generations.pop(window_id, None)
