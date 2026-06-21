"""ShadingGroup harmonization — aligns final execution targets for windows
on the same facade / shading group within a zone.

Pure Python, no Home Assistant dependency.  Applied after per-window
CommandFilter, before dispatch.  The coordinator calls compute_harmonization()
once per cycle with all sun-path window candidates and receives a per-window
HarmonizationResult that it uses to (optionally) override target_position_ha
before building the execution plan.

DESIGN PRINCIPLES
-----------------
  Principle: Learning and Decision stay per-window.
  Only the final execution target is harmonized.

  Group key: (zone_id, shading_group_id) — zone-scoped, never global.
  Two windows in different zones with the same shading_group_id string
  form TWO independent groups.

HARMONIZATION LOGIC
-------------------
  harmonized_target = min(target_position_ha)  for all eligible members

  HA convention: 0 = closed (max shade), 100 = open (no shade).
  Lower value = more shading.  min() selects the most conservative target,
  i.e. the window that needs the most shade drives the whole group.

ELIGIBILITY CRITERIA
--------------------
  A window participates in harmonization only when ALL of:
    shading_group_id is not None
    execution_mode == "automatic"          (active control enabled)
    command_allowed is True                (CommandFilter permitted execution)
    target_position_ha is not None         (a concrete recommendation exists)
    is_safety is False                     (safety states are never harmonized)
    is_override_active is False            (override windows keep their own logic)
    cover_available is True                (unavailable covers are skipped)

  A group with < 2 eligible members is not harmonized.

SAFETY SEMANTICS
----------------
  Safety (STORM_SAFE / WIND_SAFE) is never propagated to the whole group.
  A safety window is excluded from harmonization and keeps its own safety
  target.  Other eligible windows in the same group are still harmonized
  among themselves.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShadingGroupCandidate:
    """Per-window input for ShadingGroup harmonization eligibility check.

    Built by the coordinator from the per-window CommandFilter result and
    WindowConfig after the first (computation) loop pass.
    """

    window_id: str
    zone_id: str
    shading_group_id: str | None
    execution_mode_value: str   # ExecutionMode.value: "automatic" / "recommendation_only"
    command_allowed: bool | None
    target_position_ha: int | None   # HA convention: 0=closed, 100=open
    is_safety: bool
    is_override_active: bool         # True when a manual override is in effect
    cover_available: bool | None
    # Hardware-constraint floor (Step 9G10f-b/c): the minimum open position
    # (HA convention) this window must respect, set by the daytime minimum
    # open clamp and/or anti-heat-buildup clamp.  Harmonization ensures the
    # final target_position_ha is always >= this floor, preventing the group
    # minimum from pulling a window below its hardware-derived constraint.
    # Default 0 (no floor) preserves full backward compatibility.
    min_position_floor_ha: int = 0
    # Solar-sector gate (Step 7): windows outside their azimuth tolerance window
    # have no sun-geometry reason to shade and must not be pulled into shade by
    # another group member.  Default True preserves backward compatibility.
    in_solar_sector: bool = True


@dataclass(frozen=True)
class HarmonizationResult:
    """Per-window ShadingGroup harmonization outcome.

    harmonized
        True when this window's target was CHANGED by group harmonization.
        False when: not in a group, group had < 2 eligible members, this
        window already had the minimum target (target unchanged), or any
        eligibility criterion was not met.

    final_target_position_ha
        The target to use for dispatch.  Equals the harmonized group minimum
        when harmonized=True; equals the original target otherwise.
        May be None when no recommendation is available.

    pre_harmonization_target_position_ha
        This window's own original target before harmonization.
        Populated only when harmonized=True; None otherwise.
    """

    harmonized: bool
    final_target_position_ha: int | None
    pre_harmonization_target_position_ha: int | None


def _is_eligible(candidate: ShadingGroupCandidate) -> bool:
    """Return True when *candidate* may participate in ShadingGroup harmonization."""
    return (
        candidate.shading_group_id is not None
        and candidate.execution_mode_value == "automatic"
        and candidate.command_allowed is True
        and candidate.target_position_ha is not None
        and not candidate.is_safety
        and not candidate.is_override_active
        and candidate.cover_available is True
        and candidate.in_solar_sector
    )


def compute_harmonization(
    candidates: dict[str, ShadingGroupCandidate],
) -> dict[str, HarmonizationResult]:
    """Compute per-window harmonization results for one coordinator cycle.

    Parameters
    ----------
    candidates:
        Mapping of window_id → ShadingGroupCandidate for all windows that
        completed the sun-path computation this cycle.

    Returns
    -------
    dict[str, HarmonizationResult]
        One entry per window_id in *candidates*.  Windows not in a group
        or with no harmonization applied have harmonized=False.

    Algorithm
    ---------
    1. Collect eligible windows per (zone_id, shading_group_id) key.
    2. For groups with ≥ 2 eligible members: harmonized_target = min(targets).
    3. Build per-window HarmonizationResult.
       - Windows where target already equals the minimum: harmonized=False
         (their target is unchanged; pre_harmonization_target_position_ha=None).
       - Windows where target is changed: harmonized=True.
    """
    # Step 1: collect eligible windows per group key.
    group_eligible: dict[tuple[str, str], list[str]] = {}
    for window_id, candidate in candidates.items():
        if not _is_eligible(candidate):
            continue
        # shading_group_id is not None here (guaranteed by _is_eligible)
        key = (candidate.zone_id, candidate.shading_group_id)  # type: ignore[arg-type]
        group_eligible.setdefault(key, []).append(window_id)

    # Step 2: compute harmonized target for groups with ≥ 2 eligible members.
    group_targets: dict[tuple[str, str], int] = {}
    for key, window_ids in group_eligible.items():
        if len(window_ids) < 2:
            continue
        targets = [candidates[wid].target_position_ha for wid in window_ids]
        valid = [t for t in targets if t is not None]
        if len(valid) < 2:
            continue
        group_targets[key] = min(valid)

    # Step 3: build per-window results.
    results: dict[str, HarmonizationResult] = {}
    for window_id, candidate in candidates.items():
        original = candidate.target_position_ha

        if not _is_eligible(candidate):
            results[window_id] = HarmonizationResult(
                harmonized=False,
                final_target_position_ha=original,
                pre_harmonization_target_position_ha=None,
            )
            continue

        key = (candidate.zone_id, candidate.shading_group_id)  # type: ignore[arg-type]
        if key not in group_targets:
            # Group exists but has fewer than 2 eligible members this cycle.
            results[window_id] = HarmonizationResult(
                harmonized=False,
                final_target_position_ha=original,
                pre_harmonization_target_position_ha=None,
            )
            continue

        harmonized_target = group_targets[key]
        # Apply per-window floor: daytime minimum + anti-heat-buildup minimum.
        # This guarantees the group cannot pull a window below its hardware
        # constraint even when another group member has a lower (or zero) target.
        final_ha = max(harmonized_target, candidate.min_position_floor_ha)
        if final_ha == original:
            # Target unchanged (already at or above group minimum and floor).
            results[window_id] = HarmonizationResult(
                harmonized=False,
                final_target_position_ha=original,
                pre_harmonization_target_position_ha=None,
            )
        else:
            results[window_id] = HarmonizationResult(
                harmonized=True,
                final_target_position_ha=final_ha,
                pre_harmonization_target_position_ha=original,
            )

    return results
