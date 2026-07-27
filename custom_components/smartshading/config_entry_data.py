"""Typed shape of what SmartShading persists in a Home Assistant
ConfigEntry (ARCHITECTURE.md §11: "Konfiguration -> HA Config Entries").

Pure dataclasses plus dict (de)serialization helpers - no Home Assistant
imports, so this stays testable the same way as the rest of the core
(models/, state_machine/, engines/, cover_control/). ConfigEntry.data must
be a plain JSON-serializable dict, so the Config Flow builds
SmartShadingConfigEntryData and converts it via to_storage_dict(); the
integration setup phase converts back via from_storage_dict().
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import time
from typing import Any

from .models.comfort import ComfortConfig
from .models.config import ShadePositionDefaults
from .models.cover_group import (
    CoverGroup,
    cover_hardware_type_from_str,
    cover_sync_mode_from_str,
)
from .models.lifecycle import (
    LifecycleScheduleMode,
    MorningTrigger,
    NightDayLifecycleConfig,
    NightTrigger,
    SunEvent,
)
from .models.manual_override import LEGACY_DURATION_MODE_MIGRATION, OverrideReleaseStrategy
from .models.obstruction import ObstructionZone
from .models.dispatch_config import (
    DEFAULT_MAX_TRAVEL_WAIT_S,
    DEFAULT_POST_TRAVEL_PAUSE_S,
    DEFAULT_START_INTERVAL_S,
    DispatchConfig,
    DispatchMode,
)
from .models.override_policy import OverridePolicyConfig
from .models.presence import PresencePolicy
from .models.window import WindowBehaviorMode, WindowConfig
from .models.zone import ZoneConfig
from .models.zone_execution_config import ZoneExecutionConfig


@dataclass
class SmartShadingConfigEntryData:
    """Everything the Config Flow collects for one SmartShading instance.

    weather_entity_id / solar_radiation_sensor_id / outdoor_temperature_sensor_id /
    cloud_cover_sensor_id / wind_speed_sensor_id are all optional (2026-06-16
    weather-input round) - there is exactly one shared weather source for
    the whole house, not per-window/per-zone, so these live here rather
    than in WindowConfig/ZoneConfig.

    lifecycle_config / presence_entity_ids / absence_delay_min (2026-06-16
    lifecycle-config round) are likewise house-wide, not per-window/zone.
    """

    name: str
    use_home_location: bool
    zones: list[ZoneConfig] = field(default_factory=list)
    windows: list[WindowConfig] = field(default_factory=list)
    cover_groups: list[CoverGroup] = field(default_factory=list)
    shade_position_defaults: ShadePositionDefaults = field(default_factory=ShadePositionDefaults)
    weather_entity_id: str | None = None
    solar_radiation_sensor_id: str | None = None
    outdoor_temperature_sensor_id: str | None = None
    cloud_cover_sensor_id: str | None = None
    wind_speed_sensor_id: str | None = None
    rain_sensor_id: str | None = None
    # EMA sensor smoothing (v1.2.0-beta.1, T4): optional, house-wide, same
    # scope as the weather sensors above. False/0.3 defaults reproduce
    # exact pre-T4 behavior for every existing config (EMA off = raw values
    # pass through unchanged).
    ema_enabled: bool = False
    ema_alpha: float = 0.3
    lifecycle_config: NightDayLifecycleConfig = field(
        default_factory=lambda: NightDayLifecycleConfig(id="default")
    )
    presence_entity_ids: list[str] = field(default_factory=list)
    absence_delay_min: int = 30
    # T21 Phase C3: a zone may defer absence_delay_min to the System entry's
    # global default. True only when the zone never explicitly configured
    # its own value — see config_flow.py's async_step_presence gate.
    absence_delay_use_system_default: bool = False
    # Presence evaluation policy (v1.2.0-beta.1, T5). ANY_HOME reproduces
    # pre-T5 behavior exactly — see models/presence.py.
    presence_policy: PresencePolicy = PresencePolicy.ANY_HOME
    # Comfort Engine (2026-06-17). Multiple indoor temperature sensors are
    # supported (v1.0); coordinator averages all valid readings. Empty list =
    # no sensor. Stored as a list; legacy single-sensor entries are migrated
    # transparently in from_storage_dict().
    indoor_temperature_sensor_ids: list[str] = field(default_factory=list)
    comfort_config: ComfortConfig = field(default_factory=ComfortConfig)
    # Manual Override policy (v1.2.0-beta.1, T7). Every field defaults to
    # exactly the pre-T7 legacy behavior — see models/override_policy.py.
    override_policy: OverridePolicyConfig = field(default_factory=OverridePolicyConfig)
    # Cover dispatch strategy (v1.2.0-beta.1, T11). Defaults reproduce the
    # pre-T11 fixed 2.0s global interval exactly — see models/dispatch_config.py.
    dispatch_config: DispatchConfig = field(default_factory=DispatchConfig)


def _time_to_storage(value: time | None) -> str | None:
    return value.isoformat() if value is not None else None


def _window_to_storage(window: WindowConfig) -> dict[str, Any]:
    """Convert a WindowConfig to a JSON-serializable dict.
    Uses asdict() for most fields and explicitly converts the behavior_mode Enum.
    """
    raw = asdict(window)
    raw["behavior_mode"] = window.behavior_mode.value
    return raw


def _obstruction_zone_from_dict(raw: dict[str, Any]) -> ObstructionZone:
    """Reconstruct an ObstructionZone from a stored dict. Never raises.

    Migration: old field ``min_elevation_deg`` (blocked *below* that elevation)
    maps to ``block_until_elevation_deg``.  New fields take precedence when
    present so re-saved data is forward-compatible.
    """
    block_from_raw = raw.get("block_from_elevation_deg")
    block_until_raw = raw.get("block_until_elevation_deg")
    # Migration: old min_elevation_deg → block_until when new fields absent
    if block_from_raw is None and block_until_raw is None:
        old_min = raw.get("min_elevation_deg")
        if old_min is not None:
            block_until_raw = old_min
    return ObstructionZone(
        azimuth_start_deg=float(raw.get("azimuth_start_deg", 0.0)),
        azimuth_end_deg=float(raw.get("azimuth_end_deg", 0.0)),
        block_from_elevation_deg=float(block_from_raw) if block_from_raw is not None else None,
        block_until_elevation_deg=float(block_until_raw) if block_until_raw is not None else None,
        enabled=bool(raw.get("enabled", True)),
    )


def _window_from_storage(raw: dict[str, Any]) -> WindowConfig:
    """Reconstruct a WindowConfig from a stored dict with Enum coercion."""
    behavior_mode_raw = raw.get("behavior_mode", WindowBehaviorMode.FULLY_AUTOMATIC.value)
    try:
        behavior_mode = WindowBehaviorMode(behavior_mode_raw)
    except ValueError:
        behavior_mode = WindowBehaviorMode.FULLY_AUTOMATIC

    # Reconstruct ObstructionZone objects from raw dicts (asdict() serializes them).
    raw_zones = raw.get("obstruction_zones") or []
    obstruction_zones = [
        _obstruction_zone_from_dict(z)
        for z in raw_zones
        if isinstance(z, dict)
    ]

    excluded = {"behavior_mode", "obstruction_zones"}
    fields = {k: v for k, v in raw.items() if k not in excluded}
    return WindowConfig(**fields, behavior_mode=behavior_mode, obstruction_zones=obstruction_zones)


def _time_from_storage(value: Any) -> time | None:
    """Never raises: missing, None, or malformed stored time -> None,
    same "never crash on stored data" principle as the rest of this
    module and WeatherEngine.parse_numeric_state()."""
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def _presence_policy_from_storage(value: Any) -> PresencePolicy:
    """Never raises: missing, non-string, or an unrecognized value ->
    ANY_HOME (the legacy default — reproduces pre-T5 behavior exactly for
    every existing config without a stored presence_policy key)."""
    if not isinstance(value, str):
        return PresencePolicy.ANY_HOME
    try:
        return PresencePolicy(value)
    except ValueError:
        return PresencePolicy.ANY_HOME


def _ema_alpha_from_storage(value: Any) -> float:
    """Never raises: missing, non-numeric, or out-of-[0.05, 1.0]-range ->
    the default 0.3, same "never crash on stored data, safe default on
    anything implausible" principle as the rest of this module."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.3
    if not (0.05 <= float(value) <= 1.0):
        return 0.3
    return float(value)


def _sun_event_from_storage(value: Any) -> SunEvent | None:
    """Never raises: missing, None, or an unrecognized value -> None (v1.2.0-
    beta.1's "no override" default — falls back to night_fixed_time /
    morning_fixed_time as entered, the safest possible fallback)."""
    if not isinstance(value, str):
        return None
    try:
        return SunEvent(value)
    except ValueError:
        return None


def _lifecycle_config_to_storage_dict(lifecycle: NightDayLifecycleConfig) -> dict[str, Any]:
    """Convert one NightDayLifecycleConfig to a plain, JSON-serializable
    dict — the single conversion used for the flat `lifecycle_config` key
    (Lifecycle Profiles, T6, were removed in T21 Phase C3; see
    _resolve_lifecycle_config_dict() for how a pre-C3 install's active
    profile is transparently migrated into this same shape on load)."""
    return {
        "id": lifecycle.id,
        "schedule_mode": lifecycle.schedule_mode.value,
        "night_enabled": lifecycle.night_enabled,
        "night_trigger": lifecycle.night_trigger.value,
        "night_sun_elevation_deg": lifecycle.night_sun_elevation_deg,
        "night_fixed_time": _time_to_storage(lifecycle.night_fixed_time),
        "night_position": lifecycle.night_position,
        "night_tilt": lifecycle.night_tilt,
        "morning_enabled": lifecycle.morning_enabled,
        "morning_trigger": lifecycle.morning_trigger.value,
        "morning_sun_elevation_deg": lifecycle.morning_sun_elevation_deg,
        "morning_fixed_time": _time_to_storage(lifecycle.morning_fixed_time),
        "morning_position": lifecycle.morning_position,
        "morning_tilt": lifecycle.morning_tilt,
        # Weekday schedule fields
        "weekday_night_fixed_time": _time_to_storage(lifecycle.weekday_night_fixed_time),
        "weekday_night_position": lifecycle.weekday_night_position,
        "weekday_morning_fixed_time": _time_to_storage(lifecycle.weekday_morning_fixed_time),
        "weekday_morning_position": lifecycle.weekday_morning_position,
        # Weekend schedule fields
        "weekend_night_fixed_time": _time_to_storage(lifecycle.weekend_night_fixed_time),
        "weekend_night_position": lifecycle.weekend_night_position,
        "weekend_morning_fixed_time": _time_to_storage(lifecycle.weekend_morning_fixed_time),
        "weekend_morning_position": lifecycle.weekend_morning_position,
        # Legacy fields (retained for storage round-trip compatibility)
        "weekday_enabled": lifecycle.weekday_enabled,
        "weekend_morning_delay_min": lifecycle.weekend_morning_delay_min,
        # Active months (v1.2.0-beta.1): None = unrestricted (all months).
        "active_months": lifecycle.active_months,
        # Sun events (v1.2.0-beta.1): only consulted when the matching
        # trigger is SUN_EVENT.
        "night_sun_event": (
            lifecycle.night_sun_event.value if lifecycle.night_sun_event is not None else None
        ),
        "morning_sun_event": (
            lifecycle.morning_sun_event.value if lifecycle.morning_sun_event is not None else None
        ),
        # Schedule clamp (v1.2.0-beta.1, T3): None = no restriction.
        "night_not_before": _time_to_storage(lifecycle.night_not_before),
        "night_not_after": _time_to_storage(lifecycle.night_not_after),
        "morning_not_before": _time_to_storage(lifecycle.morning_not_before),
        "morning_not_after": _time_to_storage(lifecycle.morning_not_after),
    }


def to_storage_dict(data: SmartShadingConfigEntryData) -> dict[str, Any]:
    """Convert to a plain, JSON-serializable dict for ConfigEntry.data.

    dataclasses.asdict() does not convert Enum members or datetime.time
    values to plain JSON-safe values, so CoverGroup.sync_mode and the
    NightDayLifecycleConfig trigger/time fields are explicitly converted
    here.
    """
    lifecycle = data.lifecycle_config
    return {
        "name": data.name,
        "use_home_location": data.use_home_location,
        "zones": [asdict(zone) for zone in data.zones],
        "windows": [_window_to_storage(w) for w in data.windows],
        "cover_groups": [
            {
                **asdict(group),
                "sync_mode": group.sync_mode.value,
                "hardware_type": group.hardware_type.value,
            }
            for group in data.cover_groups
        ],
        "shade_position_defaults": asdict(data.shade_position_defaults),
        "weather_entity_id": data.weather_entity_id,
        "solar_radiation_sensor_id": data.solar_radiation_sensor_id,
        "outdoor_temperature_sensor_id": data.outdoor_temperature_sensor_id,
        "cloud_cover_sensor_id": data.cloud_cover_sensor_id,
        "wind_speed_sensor_id": data.wind_speed_sensor_id,
        "rain_sensor_id": data.rain_sensor_id,
        "ema_enabled": data.ema_enabled,
        "ema_alpha": data.ema_alpha,
        "lifecycle_config": _lifecycle_config_to_storage_dict(lifecycle),
        "presence_entity_ids": data.presence_entity_ids,
        "absence_delay_min": data.absence_delay_min,
        "absence_delay_use_system_default": data.absence_delay_use_system_default,
        "presence_policy": data.presence_policy.value,
        "indoor_temperature_sensor_ids": data.indoor_temperature_sensor_ids,
        "comfort_config": {
            "heat_protection_enabled": data.comfort_config.heat_protection_enabled,
            "glare_protection_enabled": data.comfort_config.glare_protection_enabled,
            "solar_gain_enabled": data.comfort_config.solar_gain_enabled,
            "heat_protection_indoor_temp_c": data.comfort_config.heat_protection_indoor_temp_c,
            "heat_protection_outdoor_temp_c": data.comfort_config.heat_protection_outdoor_temp_c,
            "heat_protection_hysteresis_c": data.comfort_config.heat_protection_hysteresis_c,
            "solar_gain_max_outdoor_temp_c": data.comfort_config.solar_gain_max_outdoor_temp_c,
            "glare_min_exposure_wm2": data.comfort_config.glare_min_exposure_wm2,
        },
        "override_policy": {
            "release_strategy": data.override_policy.release_strategy.value,
            "fixed_until": _time_to_storage(data.override_policy.fixed_until),
            "allow_comfort_actions": data.override_policy.allow_comfort_actions,
            "allow_protection_actions": data.override_policy.allow_protection_actions,
            "duration_min": data.override_policy.duration_min,
            "night_duration_min": data.override_policy.night_duration_min,
            "detection_tolerance": data.override_policy.detection_tolerance,
            "safety_timeout_enabled": data.override_policy.safety_timeout_enabled,
        },
        "dispatch_config": {
            "mode": data.dispatch_config.mode.value,
            "start_interval_s": data.dispatch_config.start_interval_s,
            "max_travel_wait_s": data.dispatch_config.max_travel_wait_s,
            "post_travel_pause_s": data.dispatch_config.post_travel_pause_s,
            "zone_batching": data.dispatch_config.zone_batching,
        },
    }


def _lifecycle_config_from_storage(raw: dict[str, Any] | None) -> NightDayLifecycleConfig:
    """Backwards compatible: ConfigEntries created before this round have
    no `lifecycle_config` key at all -> full hardcoded defaults, never a
    KeyError/crash. Same for any individual field missing within it."""
    if not raw:
        return NightDayLifecycleConfig(id="default")

    try:
        night_trigger = NightTrigger(raw.get("night_trigger", NightTrigger.BOTH.value))
    except ValueError:
        night_trigger = NightTrigger.BOTH
    try:
        morning_trigger = MorningTrigger(raw.get("morning_trigger", MorningTrigger.BOTH.value))
    except ValueError:
        morning_trigger = MorningTrigger.BOTH
    try:
        schedule_mode = LifecycleScheduleMode(
            raw.get("schedule_mode", LifecycleScheduleMode.SAME_EVERY_DAY.value)
        )
    except ValueError:
        schedule_mode = LifecycleScheduleMode.SAME_EVERY_DAY
    night_sun_event = _sun_event_from_storage(raw.get("night_sun_event"))
    morning_sun_event = _sun_event_from_storage(raw.get("morning_sun_event"))

    defaults = NightDayLifecycleConfig(id=raw.get("id", "default"))
    return NightDayLifecycleConfig(
        id=raw.get("id", "default"),
        schedule_mode=schedule_mode,
        night_enabled=raw.get("night_enabled", defaults.night_enabled),
        night_trigger=night_trigger,
        night_sun_elevation_deg=raw.get("night_sun_elevation_deg", defaults.night_sun_elevation_deg),
        night_fixed_time=_time_from_storage(raw.get("night_fixed_time")),
        night_position=raw.get("night_position", defaults.night_position),
        night_tilt=raw.get("night_tilt"),
        morning_enabled=raw.get("morning_enabled", defaults.morning_enabled),
        morning_trigger=morning_trigger,
        morning_sun_elevation_deg=raw.get("morning_sun_elevation_deg", defaults.morning_sun_elevation_deg),
        morning_fixed_time=_time_from_storage(raw.get("morning_fixed_time")),
        morning_position=raw.get("morning_position", defaults.morning_position),
        morning_tilt=raw.get("morning_tilt"),
        # Weekday schedule fields (new in v1.0 — missing key → default)
        weekday_night_fixed_time=_time_from_storage(raw.get("weekday_night_fixed_time")),
        weekday_night_position=raw.get("weekday_night_position", defaults.weekday_night_position),
        weekday_morning_fixed_time=_time_from_storage(raw.get("weekday_morning_fixed_time")),
        weekday_morning_position=raw.get("weekday_morning_position", defaults.weekday_morning_position),
        # Weekend schedule fields (new in v1.0 — missing key → default)
        weekend_night_fixed_time=_time_from_storage(raw.get("weekend_night_fixed_time")),
        weekend_night_position=raw.get("weekend_night_position", defaults.weekend_night_position),
        weekend_morning_fixed_time=_time_from_storage(raw.get("weekend_morning_fixed_time")),
        weekend_morning_position=raw.get("weekend_morning_position", defaults.weekend_morning_position),
        # Legacy fields
        weekday_enabled=raw.get("weekday_enabled", defaults.weekday_enabled),
        weekend_morning_delay_min=raw.get("weekend_morning_delay_min", defaults.weekend_morning_delay_min),
        # Active months (v1.2.0-beta.1) — missing key (pre-beta configs) → None (unrestricted).
        active_months=raw.get("active_months", defaults.active_months),
        # Sun events (v1.2.0-beta.1) — an override, not a trigger value; missing
        # key or unrecognized value → None (no override — use fixed_time as-is),
        # never raises.
        night_sun_event=night_sun_event,
        morning_sun_event=morning_sun_event,
        # Schedule clamp (v1.2.0-beta.1, T3) — missing key or malformed stored
        # time -> None (no restriction), same _time_from_storage fallback
        # already used for every other stored time field, never raises.
        night_not_before=_time_from_storage(raw.get("night_not_before")),
        night_not_after=_time_from_storage(raw.get("night_not_after")),
        morning_not_before=_time_from_storage(raw.get("morning_not_before")),
        morning_not_after=_time_from_storage(raw.get("morning_not_after")),
    )


def _resolve_lifecycle_config_dict(raw_entry: dict[str, Any]) -> dict[str, Any] | None:
    """T21 Phase C3: Lifecycle Profiles (T6) were removed — the named-profile
    CRUD/selection UI added no field beyond another instance of
    NightDayLifecycleConfig, was never automatically switched, and the
    Coordinator only ever saw a single resolved config either way (see the
    T21 Phase C3 ownership analysis). This transparently resolves a pre-C3
    install's active profile (if any) into the plain "lifecycle_config" dict
    IN MEMORY on every load — never rewriting storage, never raising, and
    an unknown/malformed active_lifecycle_profile_id falls back to the
    legacy flat config exactly like resolve_lifecycle_config() (T6) did.
    to_storage_dict() no longer writes "lifecycle_profiles"/
    "active_lifecycle_profile_id" at all, so a config entry naturally stops
    carrying them forward the next time any OptionsFlow step saves.
    """
    active_id = raw_entry.get("active_lifecycle_profile_id")
    profiles = raw_entry.get("lifecycle_profiles")
    if not isinstance(active_id, str) or not active_id or not isinstance(profiles, dict):
        return raw_entry.get("lifecycle_config")
    profile = profiles.get(active_id)
    if not isinstance(profile, dict):
        return raw_entry.get("lifecycle_config")
    config_raw = profile.get("config")
    return config_raw if isinstance(config_raw, dict) else raw_entry.get("lifecycle_config")


def _read_indoor_sensor_ids(raw: dict[str, Any]) -> list[str]:
    """Read indoor temperature sensor IDs with backward compatibility.

    v1.0+: stored as ``indoor_temperature_sensor_ids`` (list[str]).
    Legacy: stored as ``indoor_temperature_sensor_id`` (str | None).
    Missing: returns empty list (no sensor configured).
    """
    if "indoor_temperature_sensor_ids" in raw:
        ids = raw["indoor_temperature_sensor_ids"]
        if isinstance(ids, list):
            return [s for s in ids if isinstance(s, str) and s]
        return []
    legacy = raw.get("indoor_temperature_sensor_id")
    if isinstance(legacy, str) and legacy:
        return [legacy]
    return []


def _zone_from_storage(raw: dict[str, Any]) -> ZoneConfig:
    """Reconstruct a ZoneConfig, rebuilding its nested ZoneExecutionConfig.

    dataclasses.asdict() flattens ZoneConfig.execution to a plain dict, so a
    naive ZoneConfig(**raw) would leave `execution` as a dict and every
    `zone.execution.learning_enabled` access would raise AttributeError
    after a reload/restart.  Pop it out and rebuild the dataclass, mirroring
    how cover_groups rebuild their enums above.  Missing/None/malformed
    `execution` falls back to ZoneExecutionConfig defaults (learning on,
    active control off) — never raises.
    """
    fields = dict(raw)
    raw_execution = fields.pop("execution", None)
    if isinstance(raw_execution, dict):
        execution = ZoneExecutionConfig(
            # Two-control UX: learning_enabled is the merged learning master.
            # Tolerate a legacy observation_enabled key (pre-unification data).
            learning_enabled=raw_execution.get(
                "learning_enabled", raw_execution.get("observation_enabled", True)),
            active_control_enabled=raw_execution.get("active_control_enabled", False),
        )
    else:
        execution = ZoneExecutionConfig()
    return ZoneConfig(**fields, execution=execution)


def _comfort_config_from_storage(raw: dict[str, Any] | None) -> ComfortConfig:
    """Backwards compatible: ConfigEntries without a comfort_config key (created
    before this round) fall back to full ComfortConfig defaults, never KeyError.
    Individual missing keys within the dict are likewise handled gracefully."""
    if not raw:
        return ComfortConfig()
    return ComfortConfig(
        heat_protection_enabled=raw.get("heat_protection_enabled", True),
        glare_protection_enabled=raw.get("glare_protection_enabled", True),
        solar_gain_enabled=raw.get("solar_gain_enabled", True),
        heat_protection_indoor_temp_c=float(raw.get("heat_protection_indoor_temp_c", 24.0)),
        heat_protection_outdoor_temp_c=float(raw.get("heat_protection_outdoor_temp_c", 26.0)),
        heat_protection_hysteresis_c=float(raw.get("heat_protection_hysteresis_c", 1.0)),
        solar_gain_max_outdoor_temp_c=float(raw.get("solar_gain_max_outdoor_temp_c", 12.0)),
        glare_min_exposure_wm2=float(raw.get("glare_min_exposure_wm2", 100.0)),
    )


def _override_policy_from_storage(raw: dict[str, Any] | None) -> OverridePolicyConfig:
    """Backwards compatible: ConfigEntries without an override_policy key
    (every pre-T7 config) fall back to full OverridePolicyConfig defaults —
    release_strategy=LIFECYCLE, allow_comfort_actions=False, allow_
    protection_actions=False — reproducing pre-T7 behavior exactly. No
    migration, no rewrite: invalid/unknown values fall back safely
    field-by-field, never raising and never crashing the whole ConfigEntry.

    v1.2.0-beta.1, T10: a stored entry from T7-T9 (duration_mode +
    break_on_lifecycle, no release_strategy key) is migrated in-memory on
    every load, without ever rewriting storage:
      - break_on_lifecycle=True (the T7 default) -> LIFECYCLE. This is
        deliberately NOT duration_mode-dependent: break_on_lifecycle was
        already a stronger, independent release condition layered on top of
        whatever duration_mode was set, and in practice fired before
        duration_min in almost every real scenario (see
        models/manual_override.py's pre-T10 scope-field docstring: "night:
        held until the Morning lifecycle transition; expires_at is a
        generous safety-net... not the real release mechanism"). Mapping it
        to LIFECYCLE reproduces this exact effective behavior; duration_min/
        night_duration_min become the safety-timeout they always effectively
        were.
      - break_on_lifecycle=False -> the stored duration_mode is migrated via
        LEGACY_DURATION_MODE_MIGRATION ("legacy"->DURATION, "fixed_time"->
        FIXED_TIME) — lifecycle-break was explicitly disabled, so behavior
        stays governed purely by the configured duration/fixed-time expiry,
        unchanged.
    A stored release_strategy key (T10+) is used directly and always wins.
    """
    if not raw:
        return OverridePolicyConfig()

    _raw_strategy = raw.get("release_strategy")
    if _raw_strategy is not None:
        try:
            release_strategy = OverrideReleaseStrategy(_raw_strategy)
        except ValueError:
            release_strategy = OverrideReleaseStrategy.LIFECYCLE
    elif bool(raw.get("break_on_lifecycle", True)):
        release_strategy = OverrideReleaseStrategy.LIFECYCLE
    else:
        release_strategy = OverrideReleaseStrategy(
            LEGACY_DURATION_MODE_MIGRATION.get(
                raw.get("duration_mode", "legacy"), OverrideReleaseStrategy.DURATION.value)
        )

    fixed_until = _time_from_storage(raw.get("fixed_until"))
    # A FIXED_TIME strategy with no valid configured clock time is not
    # actionable — fall back to DURATION rather than crash or silently do
    # nothing at runtime (deterministic, documented fallback per T7 review
    # point 15, preserved unchanged in spirit for T10).
    if release_strategy is OverrideReleaseStrategy.FIXED_TIME and fixed_until is None:
        release_strategy = OverrideReleaseStrategy.DURATION
    return OverridePolicyConfig(
        release_strategy=release_strategy,
        fixed_until=fixed_until,
        allow_comfort_actions=bool(raw.get("allow_comfort_actions", False)),
        allow_protection_actions=bool(raw.get("allow_protection_actions", False)),
        duration_min=_safe_int(raw.get("duration_min"), 120),
        night_duration_min=_safe_int(raw.get("night_duration_min"), 720),
        detection_tolerance=_safe_int(raw.get("detection_tolerance"), 10),
        safety_timeout_enabled=bool(raw.get("safety_timeout_enabled", True)),
    )


def resolve_zone_override_policy(
    zone_raw: dict[str, Any], system_raw: dict[str, Any] | None
) -> OverridePolicyConfig:
    """T21 Phase C2: a zone whose stored override_policy explicitly opts
    into "use_system_default" — or never configured this step at all, the
    same migration-safe default config_flow.py's gate step uses — defers to
    the System entry's default policy; otherwise its own explicit policy
    (unchanged since T7/T10) wins. detection_tolerance is always the zone's
    own stored value regardless (hardware-noise property of this zone's own
    covers, stays zone-only — see the Phase C2 ownership analysis)."""
    stored = zone_raw.get("override_policy") or {}
    use_system_default = bool(stored.get("use_system_default", "release_strategy" not in stored))
    zone_policy = _override_policy_from_storage(stored)
    if not use_system_default:
        return zone_policy
    system_stored = (system_raw or {}).get("system_override_policy")
    system_policy = _override_policy_from_storage(system_stored)
    return replace(system_policy, detection_tolerance=zone_policy.detection_tolerance)


def resolve_zone_dispatch_config(
    zone_raw: dict[str, Any], system_raw: dict[str, Any] | None
) -> DispatchConfig:
    """T21 Phase C2: a zone that already has its own explicit dispatch_config
    (any pre-C2 install that visited the old zone-level Dispatch step) keeps
    using it unchanged, byte-for-byte; every other zone (new, or one that
    never touched Dispatch) uses the System entry's default — Cover Dispatch
    is a hardware/RF-pacing property of the shared dispatch pipeline, not a
    per-zone concern (see the Phase C2 ownership analysis)."""
    stored = zone_raw.get("dispatch_config")
    if stored:
        return _dispatch_config_from_storage(stored)
    system_stored = (system_raw or {}).get("system_dispatch_config")
    return _dispatch_config_from_storage(system_stored)


def resolve_zone_absence_delay_min(
    zone_raw: dict[str, Any], system_raw: dict[str, Any] | None
) -> int:
    """T21 Phase C3: a zone whose stored "absence_delay_use_system_default"
    is True defers to the System entry's global default; otherwise its own
    stored absence_delay_min (unchanged since before T21) wins. Migration:
    every existing zone already has a concrete absence_delay_min value
    (a required field, always written by to_storage_dict()) — there is no
    "never configured" signal to key a byte-for-byte-safe default off of
    the way there was for override_policy/dispatch_config, so the flag
    itself defaults to False (use my own stored value) unless explicitly
    set True, which is exactly what "no behavior change for any existing
    install" requires."""
    zone_value = _safe_int(zone_raw.get("absence_delay_min"), 30)
    if not bool(zone_raw.get("absence_delay_use_system_default", False)):
        return zone_value
    system_stored = (system_raw or {}).get("system_absence_delay_min")
    return _safe_int(system_stored, 30)


def _safe_int(value: Any, default: int) -> int:
    """Never raises: missing/non-numeric stored value -> the given default."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    """Never raises: missing/non-numeric/out-of-range stored value -> the
    given default (T11 dispatch timing fields — a malformed or manually-
    edited ConfigEntry must never crash setup, same discipline as
    _safe_int())."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):  # NaN/inf guard
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _dispatch_config_from_storage(raw: dict[str, Any] | None) -> DispatchConfig:
    """Backwards compatible: a ConfigEntry without a dispatch_config key
    (every pre-T11 config) falls back to DispatchConfig defaults — mode=
    SPACED at start_interval_s=2.0, reproducing the previously hardcoded,
    always-on DEFAULT_GLOBAL_DISPATCH_INTERVAL_SECONDS exactly. No
    migration, no rewrite: an invalid/unknown stored value falls back
    field-by-field, never raising and never crashing the whole ConfigEntry.
    """
    if not raw:
        return DispatchConfig()
    try:
        mode = DispatchMode(raw.get("mode", DispatchMode.SPACED.value))
    except ValueError:
        mode = DispatchMode.SPACED
    return DispatchConfig(
        mode=mode,
        start_interval_s=_safe_float(
            raw.get("start_interval_s"), DEFAULT_START_INTERVAL_S,
            minimum=0.0, maximum=30.0,
        ),
        max_travel_wait_s=_safe_float(
            raw.get("max_travel_wait_s"), DEFAULT_MAX_TRAVEL_WAIT_S,
            minimum=5.0, maximum=300.0,
        ),
        post_travel_pause_s=_safe_float(
            raw.get("post_travel_pause_s"), DEFAULT_POST_TRAVEL_PAUSE_S,
            minimum=0.0, maximum=10.0,
        ),
        zone_batching=bool(raw.get("zone_batching", False)),
    )


def from_storage_dict(raw: dict[str, Any]) -> SmartShadingConfigEntryData:
    """Reconstruct typed dataclasses from a stored ConfigEntry.data dict."""
    return SmartShadingConfigEntryData(
        name=raw["name"],
        use_home_location=raw.get("use_home_location", True),
        zones=[_zone_from_storage(zone) for zone in raw.get("zones", [])],
        windows=[_window_from_storage(w) for w in raw.get("windows", [])],
        cover_groups=[
            CoverGroup(**{
                **group,
                "sync_mode": cover_sync_mode_from_str(group.get("sync_mode")),
                "hardware_type": cover_hardware_type_from_str(group.get("hardware_type")),
            })
            for group in raw.get("cover_groups", [])
        ],
        shade_position_defaults=ShadePositionDefaults(**raw.get("shade_position_defaults", {})),
        weather_entity_id=raw.get("weather_entity_id"),
        solar_radiation_sensor_id=raw.get("solar_radiation_sensor_id"),
        outdoor_temperature_sensor_id=raw.get("outdoor_temperature_sensor_id"),
        cloud_cover_sensor_id=raw.get("cloud_cover_sensor_id"),
        wind_speed_sensor_id=raw.get("wind_speed_sensor_id"),
        rain_sensor_id=raw.get("rain_sensor_id"),
        ema_enabled=bool(raw.get("ema_enabled", False)),
        ema_alpha=_ema_alpha_from_storage(raw.get("ema_alpha")),
        lifecycle_config=_lifecycle_config_from_storage(_resolve_lifecycle_config_dict(raw)),
        presence_entity_ids=raw.get("presence_entity_ids", []),
        absence_delay_min=raw.get("absence_delay_min", 30),
        absence_delay_use_system_default=bool(raw.get("absence_delay_use_system_default", False)),
        presence_policy=_presence_policy_from_storage(raw.get("presence_policy")),
        indoor_temperature_sensor_ids=_read_indoor_sensor_ids(raw),
        comfort_config=_comfort_config_from_storage(raw.get("comfort_config")),
        override_policy=_override_policy_from_storage(raw.get("override_policy")),
        dispatch_config=_dispatch_config_from_storage(raw.get("dispatch_config")),
    )
