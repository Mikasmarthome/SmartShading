"""Named lifecycle profiles — v1.2.0-beta.1, Beta.1-T6. Pure dataclass, no
Home Assistant dependency, consistent with the rest of models/.

A LifecycleProfile is a NAMED WRAPPER around the existing
NightDayLifecycleConfig (models/lifecycle.py) — T6 does not introduce a new,
parallel schedule-field dataclass. NightDayLifecycleConfig already IS "the
lifecycle configuration" (schedule_mode, night/morning triggers, elevations,
fixed times, positions, tilts, active_months, sun events, clamps — see its
own docstring), so a user Profile is simply "one more named instance of
that same type", reusing every existing field, every existing storage
helper (_lifecycle_config_from_storage/_lifecycle_config_to_storage_dict in
config_entry_data.py), and every existing engine (_active_profile(),
_evaluate_trigger(), clamp_time(), sun-event resolution) completely
unchanged.

Naming note: deliberately NOT called "Profile" alone — engines/
lifecycle_engine.py already has an internal `_ScheduleProfile` NamedTuple
and an `_active_profile()` function/`LifecycleEngine.active_profile()`
method, both pre-existing, unrelated concepts (the per-cycle
weekday/weekend/sun-event/clamp-resolved night/morning time+position, not a
user-facing named configuration). `LifecycleProfile` (this class) is the
user-facing concept; nothing in engines/ ever imports or references it —
see engines/lifecycle_resolver.py, which resolves a LifecycleProfile down
to a plain NightDayLifecycleConfig BEFORE anything lifecycle-engine-related
ever runs, so no engine or evaluator needs to know profiles exist at all.

profile_id is a stable, generated identifier (uuid4 hex, matching the
existing `f"zone_{uuid.uuid4().hex}"` convention already used for zone IDs
in config_flow.py) — never derived from display_name. This sidesteps every
display-name edge case (whitespace, casing, renaming, duplicates) by
construction: display_name is free text with no identity role whatsoever;
two profiles MAY legitimately share a display_name, exactly as the T6
audit's own "technisch erlaubt bei stabilen IDs" preference specifies.
"""
from __future__ import annotations

from dataclasses import dataclass

from .lifecycle import NightDayLifecycleConfig


@dataclass
class LifecycleProfile:
    """One named, complete lifecycle configuration a user can select as the
    active schedule. T6 profiles are always COMPLETE (every
    NightDayLifecycleConfig field is written when a profile is created or
    edited via the OptionsFlow) — field-level fallbacks in storage
    deserialization exist purely for robustness against old/hand-edited
    data, not as an advertised partial-profile/inheritance feature (see
    config_entry_data.py's per-profile deserialization for the exact
    fallback: a missing field within a stored profile falls back to
    NightDayLifecycleConfig's own dataclass defaults, deliberately NOT to
    the entry's separate legacy flat lifecycle_config — inheriting from
    that would reintroduce the partial-profile semantics T6 explicitly
    avoids).
    """

    profile_id: str
    display_name: str
    config: NightDayLifecycleConfig
