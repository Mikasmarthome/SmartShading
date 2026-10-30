"""Cover ownership: which SmartShading window may command which cover.

A physical cover must be controlled by exactly one SmartShading window.  The
configuration can nevertheless contain the same cover more than once (several
windows, zones or config entries, or the same cover listed twice in one group);
this module decides, deterministically and from the stored configuration alone
(never from setup order), what each claim may do.

Identity
--------
The cover entity id, normalized with strip().lower().  Home Assistant entity ids
are always lower case, and ``hass.states.get`` lower-cases its argument, so this
is exactly the identity HA itself uses.  No registry id or device id is used:
the configuration stores entity ids, and every cover is addressed by that id.

Classification of a cover that is claimed by more than one party (a party is one
``(entry_id, window_id)``):

  identical   every party has the same hardware, safety, contact/override and
              target configuration.  Exactly one party - the smallest key - owns
              the cover; the others are DEDUPLICATED and do not send.
  conflict    the parties differ in at least one of those aspects.  Nobody may
              send normal (comfort) commands: every party is QUARANTINED for
              them.  Safety is not abandoned: when the hardware semantics and
              the rain safe position of all parties agree, ONE party (the
              smallest key) keeps sending Safety, with rain protection enabled
              if ANY party enabled it.  When the hardware semantics disagree the
              safe position would have to be guessed, so nobody sends.

All of this is a pure function of the claims; the registry only adds change
detection so coordinators can invalidate plans when roles change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ..models.cover_group import CoverHardwareType, default_hardware_settings

ROLE_ACTIVE = "active"
ROLE_DEDUPLICATED = "deduplicated"
ROLE_QUARANTINED = "quarantined"
ROLE_SAFETY_ONLY = "quarantined_safety_only"

KIND_UNIQUE = "unique"
KIND_IDENTICAL = "identical"
KIND_CONFLICT = "conflict"

ASPECT_HARDWARE = "hardware"
ASPECT_SAFETY = "safety"
ASPECT_CONTACT_OVERRIDE = "contact_override"
ASPECT_TARGETS = "targets"

_WINDOW_IDENTITY_KEYS = frozenset({"id", "name", "zone_id", "cover_group_id", "shading_group_id"})
_WINDOW_SAFETY_KEYS = frozenset({"rain_protection_enabled", "rain_safe_position_ha", "rain_release_delay_min"})
_ENTRY_SAFETY_KEYS = ("rain_sensor_id", "wind_speed_sensor_id", "weather_entity_id")
_ENTRY_TARGET_KEYS = (
    "shade_position_defaults", "lifecycle_config", "comfort_config",
    "solar_radiation_sensor_id", "outdoor_temperature_sensor_id",
    "cloud_cover_sensor_id", "indoor_temperature_sensor_ids",
)


def normalize_cover_id(value: Any) -> str:
    return str(value).strip().lower()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


@dataclass(frozen=True)
class CoverClaim:
    """One occurrence of a cover in the stored configuration."""

    entry_id: str
    window_id: str
    cover_id: str
    entry_title: str = ""
    zone_name: str = ""
    window_name: str = ""
    hardware: str = ""
    safety_sig: str = ""
    contact_sig: str = ""
    target_sig: str = ""
    rain_safe_ha: int | None = None
    rain_enabled: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.entry_id, self.window_id)

    @property
    def signature(self) -> tuple[str, str, str, str]:
        return (self.hardware, self.safety_sig, self.contact_sig, self.target_sig)

    def label(self) -> str:
        zone = self.zone_name or self.entry_title
        return f"{zone} / {self.window_name}" if zone else self.window_name or self.window_id


@dataclass(frozen=True)
class CoverOwnership:
    cover_id: str
    kind: str
    parties: tuple[tuple[str, str], ...]          # sorted distinct (entry_id, window_id)
    claims: tuple[CoverClaim, ...]
    aspects: tuple[str, ...] = ()
    safety_ok: bool = False
    rain_union: bool = False

    @property
    def owner(self) -> tuple[str, str] | None:
        return self.parties[0] if self.parties else None

    @property
    def issue_id(self) -> str:
        return "duplicate_cover_" + self.cover_id.replace(".", "_")


@dataclass(frozen=True)
class OwnershipResult:
    covers: Mapping[str, CoverOwnership] = field(default_factory=dict)

    def role(self, entry_id: str, window_id: str, cover_id: str) -> str:
        own = self.covers.get(normalize_cover_id(cover_id))
        if own is None or own.kind == KIND_UNIQUE:
            return ROLE_ACTIVE
        key = (entry_id, window_id)
        if own.kind == KIND_IDENTICAL:
            return ROLE_ACTIVE if key == own.owner else ROLE_DEDUPLICATED
        if key == own.owner and own.safety_ok:
            return ROLE_SAFETY_ONLY
        return ROLE_QUARANTINED

    def rain_union(self, entry_id: str, window_id: str, cover_id: str) -> bool:
        own = self.covers.get(normalize_cover_id(cover_id))
        return bool(own is not None and own.kind == KIND_CONFLICT and own.rain_union)

    def conflicts(self) -> list[CoverOwnership]:
        return [c for c in self.covers.values() if c.kind == KIND_CONFLICT]

    def duplicates(self) -> list[CoverOwnership]:
        return [c for c in self.covers.values() if c.kind in (KIND_IDENTICAL, KIND_CONFLICT)]


def classify(claims: Iterable[CoverClaim]) -> OwnershipResult:
    by_cover: dict[str, list[CoverClaim]] = {}
    for claim in claims:
        by_cover.setdefault(claim.cover_id, []).append(claim)
    covers: dict[str, CoverOwnership] = {}
    for cover_id, group in by_cover.items():
        ordered = tuple(sorted(group, key=lambda c: (c.entry_id, c.window_id)))
        parties = tuple(sorted({c.key for c in ordered}))
        # One representative claim per party (first occurrence), so the same
        # cover listed twice in ONE group is a single party, not a conflict.
        reps = [next(c for c in ordered if c.key == p) for p in parties]
        if len(parties) <= 1:
            covers[cover_id] = CoverOwnership(cover_id, KIND_UNIQUE, parties, ordered)
            continue
        aspects = []
        if len({c.hardware for c in reps}) > 1:
            aspects.append(ASPECT_HARDWARE)
        if len({c.safety_sig for c in reps}) > 1:
            aspects.append(ASPECT_SAFETY)
        if len({c.contact_sig for c in reps}) > 1:
            aspects.append(ASPECT_CONTACT_OVERRIDE)
        if len({c.target_sig for c in reps}) > 1:
            aspects.append(ASPECT_TARGETS)
        if not aspects:
            covers[cover_id] = CoverOwnership(cover_id, KIND_IDENTICAL, parties, ordered)
            continue
        safety_ok = (
            len({c.hardware for c in reps}) == 1
            and len({c.rain_safe_ha for c in reps}) == 1
        )
        covers[cover_id] = CoverOwnership(
            cover_id, KIND_CONFLICT, parties, ordered, tuple(aspects),
            safety_ok=safety_ok, rain_union=any(c.rain_enabled for c in reps),
        )
    return OwnershipResult(covers)


def build_claims(entries: Iterable[tuple[str, str, Mapping[str, Any]]]) -> list[CoverClaim]:
    """Claims from stored config entry data: ``(entry_id, title, data)`` per
    zone entry.  Tolerates missing or malformed parts (never raises)."""
    claims: list[CoverClaim] = []
    for entry_id, title, data in entries:
        try:
            claims.extend(_claims_of_entry(entry_id, title, data))
        except Exception:  # a broken entry must never break ownership
            continue
    return claims


def _claims_of_entry(entry_id: str, title: str, data: Mapping[str, Any]) -> list[CoverClaim]:
    groups = {g.get("id"): g for g in data.get("cover_groups", []) or [] if isinstance(g, Mapping)}
    groups_by_window = {g.get("window_id"): g for g in groups.values()}
    zones = {z.get("id"): z for z in data.get("zones", []) or [] if isinstance(z, Mapping)}
    entry_safety = [data.get(k) for k in _ENTRY_SAFETY_KEYS]
    entry_contact = data.get("override_policy")
    entry_targets = {k: data.get(k) for k in _ENTRY_TARGET_KEYS}
    out: list[CoverClaim] = []
    for window in data.get("windows", []) or []:
        if not isinstance(window, Mapping):
            continue
        window_id = str(window.get("id"))
        group = groups.get(window.get("cover_group_id")) or groups_by_window.get(window.get("id"))
        if not group:
            continue
        zone = zones.get(window.get("zone_id")) or {}
        hardware = str(group.get("hardware_type", ""))
        try:
            hw_defaults = default_hardware_settings(CoverHardwareType(hardware))
        except ValueError:
            hw_defaults = {}
        rain_override = window.get("rain_protection_enabled")
        rain_enabled = bool(
            rain_override if rain_override is not None
            else hw_defaults.get("rain_protection_enabled", False)
        )
        safety_sig = _canon([
            {k: window.get(k) for k in sorted(_WINDOW_SAFETY_KEYS)},
            entry_safety, (zone.get("execution") or {}).get("active_control_enabled"),
        ])
        contact_sig = _canon([
            {k: v for k, v in sorted(window.items())
             if "contact" in k or k.startswith("night_") or k == "window_open_night_position"},
            entry_contact,
        ])
        target_sig = _canon([
            {k: v for k, v in sorted(window.items())
             if k not in _WINDOW_IDENTITY_KEYS and k not in _WINDOW_SAFETY_KEYS
             and not ("contact" in k or k.startswith("night_") or k == "window_open_night_position")},
            {k: v for k, v in sorted(zone.items()) if k not in ("id", "name", "execution")},
            entry_targets, group.get("sync_mode"),
        ])
        for cover in group.get("cover_ids", []) or []:
            out.append(CoverClaim(
                entry_id=entry_id, window_id=window_id, cover_id=normalize_cover_id(cover),
                entry_title=str(title or ""), zone_name=str(zone.get("name") or ""),
                window_name=str(window.get("name") or ""), hardware=hardware,
                safety_sig=safety_sig, contact_sig=contact_sig, target_sig=target_sig,
                rain_safe_ha=window.get("rain_safe_position_ha"), rain_enabled=rain_enabled,
            ))
    return out


def covers_claimed_by_others(
    claims: Iterable[CoverClaim], cover_ids: Iterable[str], *, own_key: tuple[str, str] | None,
) -> list[str]:
    """Covers among ``cover_ids`` (or repeated inside it) already claimed by a
    party other than ``own_key`` - what the config/options flows reject."""
    wanted = [normalize_cover_id(c) for c in cover_ids]
    taken = {c.cover_id for c in claims if c.key != own_key}
    seen: set[str] = set()
    bad: list[str] = []
    for cover in wanted:
        if (cover in taken or cover in seen) and cover not in bad:
            bad.append(cover)
        seen.add(cover)
    return bad


class CoverOwnershipRegistry:
    """Integration-wide holder of the current ownership result.

    Lives in ``hass.data[DOMAIN]``.  ``update`` replaces the result atomically
    (single event-loop step) and notifies the coordinators of exactly those
    config entries whose roles changed, so they can invalidate their plans.
    """

    def __init__(self) -> None:
        self._result = OwnershipResult()
        self._listeners: dict[str, list[Callable[[], None]]] = {}
        self.generation = 0
        self.invalidations_notified = 0
        self.resolutions = 0

    @property
    def result(self) -> OwnershipResult:
        return self._result

    def role(self, entry_id: str, window_id: str, cover_id: str) -> str:
        return self._result.role(entry_id, window_id, cover_id)

    def rain_union(self, entry_id: str, window_id: str, cover_id: str) -> bool:
        return self._result.rain_union(entry_id, window_id, cover_id)

    def add_listener(self, entry_id: str, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.setdefault(entry_id, []).append(callback)

        def _remove() -> None:
            lst = self._listeners.get(entry_id, [])
            if callback in lst:
                lst.remove(callback)
            if not lst:
                self._listeners.pop(entry_id, None)

        return _remove

    def listener_count(self) -> int:
        return sum(len(v) for v in self._listeners.values())

    @staticmethod
    def _role_map(result: OwnershipResult) -> dict[tuple[str, str, str], tuple[str, bool]]:
        out: dict[tuple[str, str, str], tuple[str, bool]] = {}
        for cover_id, own in result.covers.items():
            if own.kind == KIND_UNIQUE:
                continue
            for party in own.parties:
                out[(party[0], party[1], cover_id)] = (
                    result.role(party[0], party[1], cover_id),
                    result.rain_union(party[0], party[1], cover_id),
                )
        return out

    def update(self, claims: Iterable[CoverClaim]) -> OwnershipResult:
        new = classify(claims)
        old_map, new_map = self._role_map(self._result), self._role_map(new)
        changed_entries = {
            key[0] for key in set(old_map) | set(new_map)
            if old_map.get(key, (ROLE_ACTIVE, False)) != new_map.get(key, (ROLE_ACTIVE, False))
        }
        resolved = len(self._result.conflicts()) > len(new.conflicts())
        self._result = new
        if changed_entries:
            self.generation += 1
            if resolved:
                self.resolutions += 1
            for entry_id in sorted(changed_entries):
                for callback in list(self._listeners.get(entry_id, [])):
                    self.invalidations_notified += 1
                    callback()
        return new

    def summary(self) -> dict[str, Any]:
        res = self._result
        return {
            "generation": self.generation,
            "covers_with_multiple_owners": len(res.duplicates()),
            "active_conflicts": len(res.conflicts()),
            "deduplicated_covers": len([c for c in res.covers.values() if c.kind == KIND_IDENTICAL]),
            "invalidations_notified": self.invalidations_notified,
            "resolutions": self.resolutions,
            "listeners": self.listener_count(),
        }
