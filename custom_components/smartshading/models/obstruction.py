"""Per-window obstruction zone model.

An ObstructionZone describes an azimuth range where a physical obstruction
(mountain, tree, building, roof overhang) blocks direct sunlight within an
optional solar-elevation range.

Elevation range semantics
-------------------------
block_from_elevation_deg  — if set, the obstruction only applies while
                            sun elevation >= this value (high-sun blocking,
                            e.g. roof overhang at steep angles).
block_until_elevation_deg — if set, the obstruction only applies while
                            sun elevation <= this value (low-sun blocking,
                            e.g. neighbouring building or mountain).

When both are None the obstruction blocks at every elevation inside the
azimuth range.  When both are set, the zone blocks only while
block_from_elevation_deg <= elevation <= block_until_elevation_deg.
The boundaries are inclusive.

Examples
--------
block_from=None, block_until=18  → blocks at low sun (elevation ≤ 18°)
                                    suitable for mountain / wall / building.
block_from=45,   block_until=None → blocks at high sun (elevation ≥ 45°)
                                    suitable for roof overhang.
block_from=10,   block_until=55  → blocks only in the 10°–55° range.
block_from=None, block_until=None → blocks at every elevation in the azimuth range.

Azimuth ranges
--------------
Azimuth ranges support wrap-around (e.g. 330°..30° covers north).
Wrap-around detection: if azimuth_start_deg > azimuth_end_deg, the range
wraps through 0°/360°.

Multiple zones per window are evaluated OR-style: if any active zone
blocks the sun, direct exposure is treated as blocked for that window.

Migration
---------
The superseded field ``min_elevation_deg`` (blocked below that elevation)
maps to ``block_until_elevation_deg``.  The deserialization layer in
``config_entry_data._obstruction_zone_from_dict`` performs this migration
automatically for stored data that still carries the old field.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ObstructionZone:
    """One obstruction zone for a single window.

    Fields
    ------
    azimuth_start_deg         Start of the blocked azimuth range [0, 359].
    azimuth_end_deg           End of the blocked azimuth range [0, 359].
                              If start > end, the range wraps through north (0°).
    block_from_elevation_deg  Optional lower bound of the blocking elevation range.
                              Obstruction applies only at sun elevation >=
                              block_from_elevation_deg.  None = no lower bound.
    block_until_elevation_deg Optional upper bound of the blocking elevation range.
                              Obstruction applies only at sun elevation <=
                              block_until_elevation_deg.  None = no upper bound.
    enabled                   False = zone is stored but ignored; useful for
                              temporarily disabling a zone without deleting it.
    """

    azimuth_start_deg: float
    azimuth_end_deg: float
    block_from_elevation_deg: float | None = None
    block_until_elevation_deg: float | None = None
    enabled: bool = True

    def elevation_blocks(self, sun_elevation_deg: float) -> bool:
        """Return True when *sun_elevation_deg* falls inside this zone's elevation range.

        Both bounds are inclusive.  A missing bound is treated as no restriction
        in that direction (open interval on one or both sides).
        """
        if self.block_from_elevation_deg is not None and sun_elevation_deg < self.block_from_elevation_deg:
            return False
        if self.block_until_elevation_deg is not None and sun_elevation_deg > self.block_until_elevation_deg:
            return False
        return True
