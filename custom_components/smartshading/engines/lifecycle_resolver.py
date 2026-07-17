"""Lifecycle profile resolution — v1.2.0-beta.1, Beta.1-T6. No Home
Assistant dependency, consistent with the rest of engines/.

Architecture
------------
This module owns exactly ONE responsibility: turning "the legacy flat
lifecycle config, the stored named profiles, and which one is active" into
a single, plain NightDayLifecycleConfig — the ONLY thing anything
downstream (the Coordinator's LifecycleEngine call, _active_profile(),
_evaluate_trigger()) ever sees:

    legacy flat lifecycle_config ─┐
    stored lifecycle_profiles ────┼─► resolve_lifecycle_config() ─► NightDayLifecycleConfig
    active_lifecycle_profile_id ──┘        (+ source/id/count, diagnostics-only)
                                              │
                                              ▼
                                    _active_profile() / LifecycleEngine
                                    (completely unchanged, unaware profiles exist)

This mirrors the T5 presence_engine.py separation: one narrow, pure
resolution step, called ONCE (in __init__.py, before the Coordinator is
constructed — see coordinator.py's existing `lifecycle_config:
NightDayLifecycleConfig` constructor parameter, which needed ZERO changes
for T6, since it already accepts exactly the type this resolver produces).

Resolution rule (deliberately the simplest rule that satisfies every T6
fallback requirement uniformly)
--------------------------------------------------------------------------
    no profiles configured at all           -> legacy flat config
    active_profile_id is None               -> legacy flat config
    active_profile_id set but unknown       -> legacy flat config (fallback)
    active_profile_id set and known         -> that profile's config

The legacy flat NightDayLifecycleConfig (SmartShadingConfigEntryData.
lifecycle_config) is NEVER removed, migrated, or rewritten by T6 — it
remains the permanent, always-present safety net. Every one of the T6
audit's required fallback scenarios ("profiles fehlt", "profiles ist
leer", "active_profile fehlt", "active_profile verweist auf unbekannte
ID") reduces to exactly this same one rule, rather than needing N
different fallback code paths: whenever the active profile cannot be
unambiguously resolved, fall back to the one config that is ALWAYS valid
and ALWAYS present. A more elaborate fallback (e.g. "pick the first stored
profile") was deliberately rejected — it would be less predictable (which
profile is "first"? insertion order? sorted by id?) and would remove the
one guarantee this rule provides: the fallback destination never depends
on what happens to be in `profiles` at all.

Field-level robustness for a stored profile's OWN NightDayLifecycleConfig
(missing/malformed individual fields) is handled entirely by
config_entry_data.py's existing `_lifecycle_config_from_storage()` — the
exact same function already used for the legacy flat field — applied
per-profile. This resolver never touches individual fields; it only ever
picks WHICH already-fully-parsed NightDayLifecycleConfig to use.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.lifecycle import NightDayLifecycleConfig
from ..models.lifecycle_profile import LifecycleProfile

SOURCE_LEGACY = "legacy"
SOURCE_STORED = "stored"
SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True)
class ResolvedLifecycleConfig:
    """Result of resolve_lifecycle_config() — the config to actually use,
    plus provenance metadata for diagnostics only (see
    diagnostics_builder.py lifecycle_profile_summary). Nothing in
    engines/lifecycle_engine.py or coordinator.py's decision logic ever
    reads `source`/`active_profile_id`/`profile_count` — only `config`."""

    config: NightDayLifecycleConfig
    source: str  # SOURCE_LEGACY | SOURCE_STORED | SOURCE_FALLBACK
    active_profile_id: str | None
    profile_count: int


def resolve_lifecycle_config(
    legacy_config: NightDayLifecycleConfig,
    profiles: dict[str, LifecycleProfile],
    active_profile_id: str | None,
) -> ResolvedLifecycleConfig:
    """Never raises. See module docstring for the full resolution rule."""
    profile_count = len(profiles)

    if not profiles or active_profile_id is None:
        return ResolvedLifecycleConfig(
            config=legacy_config, source=SOURCE_LEGACY,
            active_profile_id=None, profile_count=profile_count,
        )

    profile = profiles.get(active_profile_id)
    if profile is None:
        return ResolvedLifecycleConfig(
            config=legacy_config, source=SOURCE_FALLBACK,
            active_profile_id=active_profile_id, profile_count=profile_count,
        )

    return ResolvedLifecycleConfig(
        config=profile.config, source=SOURCE_STORED,
        active_profile_id=active_profile_id, profile_count=profile_count,
    )
