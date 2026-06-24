"""Tier-Order Resolver — LE 2.0 / Phase P9A.

Projects the FINAL effective per-intensity target set onto a safe monotone
order so that no adaptive change (manual preference, persistent adoption,
bounded experiment, learned position) can ever invert the semantic stages.

HA convention: 0 = closed, 100 = open.  More shading = lower value.
Required order:  Strong ≤ Normal ≤ Light  (equal adjacent values allowed).

This is a pure projection of the *effective* set only.  It NEVER rewrites the
stored configuration and is fully visible in provenance/diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TierOrderProjection:
    light_ha: int
    normal_ha: int
    strong_ha: int
    projected: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "light_ha": self.light_ha, "normal_ha": self.normal_ha,
            "strong_ha": self.strong_ha, "projected": self.projected,
            "notes": list(self.notes),
        }


def project_tier_order(
    light_ha: int, normal_ha: int, strong_ha: int, *, min_gap: int = 0
) -> TierOrderProjection:
    """Return a monotone (Strong ≤ Normal ≤ Light) effective set.

    Projection makes a higher-protection stage never MORE OPEN than a lower one:
        normal := min(normal, light - min_gap)
        strong := min(strong, normal - min_gap)
    Light (most open, user/manual authority) is the anchor and is never moved.
    Equal adjacent positions are allowed when min_gap == 0.
    """
    notes: list[str] = []
    new_light = light_ha
    new_normal = normal_ha
    new_strong = strong_ha

    cap_normal = new_light - min_gap
    if new_normal > cap_normal:
        notes.append("normal_projected_to_light")
        new_normal = cap_normal
    cap_strong = new_normal - min_gap
    if new_strong > cap_strong:
        notes.append("strong_projected_to_normal")
        new_strong = cap_strong

    # Clamp into valid HA range defensively (min_gap could push below 0).
    new_normal = max(0, min(100, new_normal))
    new_strong = max(0, min(100, new_strong))
    projected = (new_light, new_normal, new_strong) != (light_ha, normal_ha, strong_ha)
    return TierOrderProjection(
        light_ha=new_light, normal_ha=new_normal, strong_ha=new_strong,
        projected=projected, notes=tuple(notes),
    )
