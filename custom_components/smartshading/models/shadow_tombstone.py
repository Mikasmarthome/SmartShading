"""Compact shadow tombstones — LE 2.0 / Phase P10 completion.

A bounded, restart-safe provenance record for a shadow candidate that an
experiment/adoption references — WITHOUT persisting the full shadow time series.
A bare unresolvable id is too weak as durable provenance; a tombstone keeps the
exact id plus minimal evidence facts.  A missing tombstone never invalidates an
otherwise-valid adoption (provenance-only); a shadow alone never becomes
adoption evidence.

No Home Assistant import.  Frozen dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

SHADOW_TOMBSTONE_SCHEMA_VERSION: int = 1
TOMBSTONE_AGE_CAP_DAYS: int = 365

KIND_POSITION: str = "position"
KIND_STRATEGY: str = "strategy"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


@dataclass(frozen=True)
class ShadowTombstone:
    shadow_id: str
    kind: str                       # position | strategy
    window_id: str
    parameter_family: str           # intensity (P6) or strategy family (P9B)
    context_family: str
    config_generation: int = 0
    created_at: datetime | None = None
    expires_at: datetime | None = None
    confidence: float = 0.0
    reliability: float = 0.0
    terminal_status: str = ""
    schema_version: int = SHADOW_TOMBSTONE_SCHEMA_VERSION

    @property
    def tombstone_key(self) -> tuple[str, str]:
        return (self.kind, self.shadow_id)

    def to_dict(self) -> dict:
        return {
            "shadow_id": self.shadow_id, "kind": self.kind, "window_id": self.window_id,
            "parameter_family": self.parameter_family, "context_family": self.context_family,
            "config_generation": self.config_generation, "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at), "confidence": self.confidence,
            "reliability": self.reliability, "terminal_status": self.terminal_status,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ShadowTombstone":
        return cls(
            shadow_id=d["shadow_id"], kind=d.get("kind", KIND_STRATEGY),
            window_id=d.get("window_id", ""), parameter_family=d.get("parameter_family", ""),
            context_family=d.get("context_family", "global"),
            config_generation=int(d.get("config_generation", 0)),
            created_at=_parse(d.get("created_at")), expires_at=_parse(d.get("expires_at")),
            confidence=float(d.get("confidence", 0.0)), reliability=float(d.get("reliability", 0.0)),
            terminal_status=d.get("terminal_status", ""),
            schema_version=int(d.get("schema_version", SHADOW_TOMBSTONE_SCHEMA_VERSION)),
        )
