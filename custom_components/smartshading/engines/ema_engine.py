"""Exponential Moving Average (EMA) for volatile sensor inputs — v1.2.0-beta.1,
Beta.1-T4. No Home Assistant dependency, consistent with the rest of engines/.

Architecture
------------
EMA smooths RAW, already-parsed numeric sensor readings at exactly ONE point
in the pipeline: where the Coordinator builds its per-cycle weather/indoor
inputs (coordinator.py `_read_weather_inputs()` / `_read_indoor_temperature()`).
Every downstream consumer (WeatherEngine, solar_source.classify_solar_source,
ExposureEngine, ComfortEngine, build_window_decision_input(), every Tier
evaluator) keeps receiving a plain `float | None` exactly as before — none of
them are aware EMA exists, and none of them need to change. This mirrors the
T1-T3 discipline of resolving a cross-cutting concern centrally so downstream
trigger/decision logic stays blind to it.

Only RAW sensor values are smoothed: outdoor temperature, solar radiation
(the measured sensor reading, not the derived weather-based estimate or the
final effective_radiation_wm2 solar_source.py selects between the two),
cloud cover, indoor temperature. Explicitly NEVER smoothed:
`solar_radiation_age_s` (staleness metadata, not a value),
`solar_radiation_unit` / `weather_condition` / `weather_condition_enum`
(categorical/string, not numeric), rain status (already a discrete
classification with its own `rain_release_delay_min` hysteresis — smoothing
a status enum makes no sense and would fight that existing hysteresis), any
already-derived/computed value (sun geometry, exposure, comfort assessment,
effective_radiation_wm2) — smoothing those would double-process a signal
that is itself already downstream of (or transitively benefits from, e.g.
the weather-based solar estimate uses the now-smoothed cloud cover) an EMA'd
raw input — and, deliberately, wind speed / wind gust (see "Wind is
excluded" below).

Wind is excluded (T4 pre-push review correction)
-------------------------------------------------
wind_speed / wind_gust feed Tier-1 safety/storm protection
(WindowDecisionInput.wind_speed_ms / wind_gust_ms) — damping a genuine gust
spike there risks delaying a protective cover retraction. No established
"immediate rise, gradual fall" or similar safety-preserving smoothing
semantics exist yet, so T4 deliberately scopes wind out entirely rather than
inventing unproven complexity on a safety path: coordinator.py reads and
passes wind_speed/wind_gust through completely unchanged, exactly as before
T4. Revisit only alongside a dedicated wind/storm-protection design.

Solar radiation is gated before it reaches EMA at all (T4 pre-push review
correction): coordinator.py's `_solar_reading_ema_eligible()` re-applies
solar_source.py's own range (MAX_PLAUSIBLE_SOLAR_WM2), staleness
(SOLAR_SENSOR_MAX_AGE_S), and unit-family (WeatherEngine.
is_plausible_solar_unit) criteria BEFORE a raw reading is allowed to update
the EMA state. Without this gate, a single implausible spike would still
get damped into (and corrupt) the running EMA average even though
classify_solar_source() correctly rejects it for that cycle's source
selection — the visible symptom would be hidden while the EMA's internal
state stayed wrong for many subsequent cycles. An ineligible reading is
passed through UNSMOOTHED (not substituted with the last-good EMA value)
so classify_solar_source() still sees and correctly rejects/falls back on
the actual current raw reading, exactly like before EMA existed; the EMA
channel's own state is left untouched, so the next eligible sample resumes
blending from where the last valid one left off. This duplicates only two
numeric comparisons (not classify_solar_source()'s source-selection logic,
which cannot run yet at this point in the cycle — it needs a per-window sun
geometry not computed until later) and reuses solar_source.py's own public
threshold constants rather than redefining them.

Algorithm
---------
ema_update() is the textbook one-step EMA:
    ema = alpha * new + (1 - alpha) * old
- The first valid sample SEEDS the EMA directly (`previous is None` ->
  return `new_value` unchanged) — no synthetic starting value, no zero-bias,
  no artificial "cold" temperature.
- An invalid sample (None, NaN, or +/-Infinity) never updates or destroys
  existing EMA state: it is simply ignored, returning `previous` unchanged.
  WeatherEngine.parse_numeric_state() already rejects non-numeric text, but
  `float("nan")` / `float("inf")` ARE valid Python float conversions it does
  NOT filter — this module's own math.isfinite() gate is what actually
  guards against those, independent of upstream text parsing.
- Range/staleness/unit-family plausibility (e.g. solar_source.py's
  MAX_PLAUSIBLE_SOLAR_WM2, SOLAR_SENSOR_MAX_AGE_S, lux-unit rejection) is
  intentionally NOT duplicated here. Those checks already run downstream on
  whatever raw-or-smoothed measured value classify_solar_source() receives
  (staleness is keyed to the underlying HA state's last_updated, which EMA
  never touches). Re-implementing a second, narrower plausibility gate here
  would be genuine duplication, not defense in depth — a temporary outlier
  is exactly what EMA already damps, and this module's only added contract
  is "never let an invalid sample corrupt the running average."

Alpha is passed into ema_update() per call, not baked into EmaSmoother's
state, precisely so a future time-constant-based formulation (alpha derived
from elapsed wall-clock time since the channel's last update, needed if the
Coordinator's fixed 5-minute update interval - DEFAULT_UPDATE_INTERVAL in
const.py - ever becomes configurable) is a drop-in change to how alpha is
computed at the call site; EmaSmoother's public shape does not need to
change.

Persistence
-----------
Deliberately NOT persisted across Home Assistant restarts (see T4 final
report "Persistenzentscheidung" for the full reasoning) — EmaSmoother is a
single per-Coordinator in-memory instance, mirroring PresenceDebouncer
(engines/lifecycle_engine.py). A restart re-seeds every channel from the
first fresh reading after startup, exactly the same "no synthetic bias"
principle the algorithm itself already guarantees for a brand-new channel.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def ema_update(previous: float | None, new_value: float | None, alpha: float) -> float | None:
    """Pure one-step EMA update. Never raises.

    - `new_value` is None or not finite (NaN/+-Infinity) -> invalid sample,
      ignored: returns `previous` unchanged.
    - `previous` is None -> first valid sample seeds the EMA: returns
      `new_value` as-is (no synthetic starting bias).
    - otherwise -> `alpha * new_value + (1 - alpha) * previous`.

    `alpha` is expected in (0, 1]; alpha=1.0 disables smoothing (the EMA
    always equals the latest valid sample). This function does not clamp or
    validate `alpha` — the caller (config validation / EmaSmoother callers)
    owns range enforcement, matching how the rest of this codebase separates
    pure math helpers from input validation.
    """
    if new_value is None or not math.isfinite(new_value):
        return previous
    if previous is None:
        return new_value
    return alpha * new_value + (1.0 - alpha) * previous


@dataclass
class EmaSmoother:
    """Per-Coordinator-instance stateful wrapper around ema_update() for N
    independently-named channels (e.g. "outdoor_temperature",
    "solar_radiation", "cloud_cover", "indoor_temperature" — NOT wind_speed/
    wind_gust, deliberately excluded, see module docstring "Wind is excluded").

    One instance lives on the Coordinator for its whole lifetime — one
    instance per integration, not persisted, exactly like PresenceDebouncer
    (engines/lifecycle_engine.py). Each channel keeps its own independent
    `float | None` state in a plain dict, so a channel that keeps receiving
    invalid readings never contaminates or resets any other channel.
    """

    _state: dict[str, float | None] = field(default_factory=dict)

    def update(self, channel: str, new_value: float | None, alpha: float) -> float | None:
        """Apply one EMA step for `channel` and return the new smoothed value
        (or `new_value`/previous state unchanged for an invalid sample —
        see ema_update())."""
        previous = self._state.get(channel)
        smoothed = ema_update(previous, new_value, alpha)
        self._state[channel] = smoothed
        return smoothed

    def reset(self, channel: str | None = None) -> None:
        """Clear one channel's state, or every channel's state if `channel`
        is None. Used when EMA is toggled off then back on, so re-enabling
        never resumes from stale pre-disable state."""
        if channel is None:
            self._state.clear()
        else:
            self._state.pop(channel, None)
