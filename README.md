<p align="center">
  <img src="https://raw.githubusercontent.com/Mikasmarthome/SmartShading/main/brand/logo.png" alt="SmartShading" width="256"/>
</p>

<h1 align="center">SmartShading</h1>
<p align="center"><strong>Intelligent local shading control for Home Assistant</strong></p>

<p align="center">
  <a href="https://hacs.xyz"><img src="https://img.shields.io/badge/HACS-Custom-orange.svg" alt="HACS Custom"/></a>
  <a href="https://github.com/Mikasmarthome/SmartShading/releases/latest"><img src="https://img.shields.io/badge/stable-v1.1.8-brightgreen.svg" alt="Stable release"/></a>
  <img src="https://img.shields.io/badge/status-stable-brightgreen.svg" alt="Stable"/>
  <img src="https://img.shields.io/badge/HA-2024.1%2B-brightgreen.svg" alt="Home Assistant 2024.1+"/>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"/></a>
</p>

---

> ⚠️ **Use at your own risk.** SmartShading is not affiliated with Home Assistant or Nabu Casa. It controls physical shading devices. Verify cover direction, hardware type, and safety sensors, and review the recommended positions in Learning Mode before enabling Active Control. SmartShading is one layer alongside your covers' own protections, not a replacement for them.

---

SmartShading models your home as **zones** (comfort context) that contain **windows** (sun-exposure
surfaces), each with one or more **assigned cover entities** (the physical covers). For every window it
continuously evaluates the sun's position, the configured weather and solar inputs, comfort goals, and
safety conditions, then produces a recommended cover position — and, when you allow it, sends the command
to the cover. **Everything runs locally in Home Assistant; no cloud service is required.**

## Why SmartShading?

- **Per-window intelligence** — sun azimuth and elevation are evaluated per window, so surfaces with
  different orientations can be in different shading states at the same time.
- **Safe by default** — out of the box SmartShading observes and recommends, but never moves a cover until
  you explicitly enable Active Control for a zone.
- **Local and privacy-first** — all calculations run inside Home Assistant; no cloud, no account.
- **Learns gradually** — conservative, confidence-gated adaptation refines recommendations over time.

**Capabilities:** per-window sun-exposure evaluation · optional weather/solar inputs · heat, glare and
solar-gain comfort goals · night, morning, presence and absence handling · storm and wind safety ·
manual-override awareness · local learning and forecast trust · on-demand Support and Research exports.

---

## Supported shading devices

SmartShading supports common shading hardware types:

- Roller shutters
- Venetian blinds / blinds with tilt
- Exterior screens
- Awnings
- Generic covers

The actual capabilities (position control, tilt control, position feedback) depend on the specific Home
Assistant cover entity you assign. SmartShading detects what each cover supports and adapts accordingly,
including covers that do not report a reliable position.

## Core concepts

### System Entry and Zone Entries

SmartShading uses two kinds of configuration entries: a single **System Entry** holds system-wide actions
(Support Export, Research Export, Debug logging), and **one Zone Entry per zone** holds that zone's
configuration and all of its zone and window entities. The System Entry is set up automatically.

### Zones, windows, and covers

A **zone** is the comfort context — typically a room. A **window** is a single sun-exposure surface with its
own orientation; sun exposure and learning are calculated per window, and windows are not separate devices.
Each window has one or more **assigned cover entities** — the physical covers driven for that window. Covers
only carry out what SmartShading decided for their window. Where useful, SmartShading coordinates covers
that shade the same surface so closely related covers settle to a consistent level.

### Learning Mode

Learning Mode lets SmartShading observe, learn, and adapt its recommendations. It defaults to **on**.
Turning it off pauses observation, learning, and adaptation, but does **not** delete learned data — existing
data is reused when you turn it back on. Learning Mode never moves a cover by itself; automatic movement
is controlled separately by Active Control.

### Active Control

Active Control lets SmartShading send cover commands automatically for a zone. It defaults to **off** and
must be enabled explicitly per zone. While it is off, SmartShading still computes recommendations and
diagnostics but does not move any cover. Enable it only when the covers in that zone are safe to operate
automatically.

### Behavior Modes

Each window has its own **Behavior Mode** (set under **Shading behavior** when editing the window), which
decides which situations may move that window's cover automatically. Safety conditions (wind, storm, rain)
and a recognized manual override always take priority, regardless of the selected mode.

| Behavior Mode | What it does |
|---|---|
| **Fully automatic** | Full control: safety, night/morning schedule, presence/absence, and the configured heat/glare/solar-gain/learning behavior all apply. |
| **Absence and schedule** | Presence/absence handling and the night/morning schedule stay active, but daytime heat/glare/solar-gain shading is not applied. |
| **Absence only** | Only presence/absence handling applies (the window closes for absence and releases when presence returns); the night/morning schedule and daytime comfort shading are not applied. |
| **Automation disabled** | No automatic shading decisions for this window; only safety conditions and manual-override recognition still apply. |

All position values reported for a window follow the same convention regardless of its Behavior Mode:
`0 = closed`, `100 = open` (see [Position and tilt semantics](#position-and-tilt-semantics)).

## Recommendations

For every window, SmartShading publishes a recommended cover position and the current shading state,
regardless of whether Active Control is enabled. With Active Control off, these let you review what
SmartShading would do; with it on, the same recommendation is what SmartShading attempts to apply, subject
to cover availability, safety, manual override, and timing safeguards.

## Position and tilt semantics

SmartShading follows the standard Home Assistant convention for all user-facing values:

- **Position:** `0 = closed`, `100 = open`
- **Tilt:** `0 = closed`, `100 = open`

Every position and target shown by SmartShading entities — including recommendations and target positions —
uses this convention.

## Safety behavior

Safety conditions take priority over normal automatic shading decisions:

- Safety decisions have the highest priority.
- Wind and storm behavior depends on the configured cover hardware type, so the protective position suits
  the kind of cover.
- An active safety condition remains latched for a release period rather than clearing the instant the
  triggering value drops.
- A temporarily unavailable sensor does not release a safety state that is already active.
- When a safety state clears, SmartShading does not automatically restore the previous position; it
  calculates a fresh normal decision for the current conditions.

Safety behavior depends on an appropriate cover hardware type and correct sensor values. SmartShading cannot
guarantee the physical safety of any specific hardware; treat it as one layer alongside the cover's own
protections, not a replacement for them.

## Manual Override

When you move a cover manually, SmartShading recognizes the manual override and holds back automatic
commands for that window so it does not fight your action. Automatic decisions resume after the override
period, and safety conditions still take priority while an override is held.

## Night and morning behavior

SmartShading can apply a night position during configured night hours and transition to daytime behavior in
the morning. Night and morning timing is configurable (fixed time, sun elevation, or both, with optional
weekday/weekend schedules), and the night behavior is independent of the normal daytime logic so it stays
predictable.

## Heat, glare, and solar behavior

SmartShading balances complementary comfort goals that can pull in opposite directions:

- **Heat protection** — may shade before a room is already hot when sun position, weather, and temperature
  signals indicate likely warming. This is intentionally preventive, not reactive.
- **Glare protection** — can reduce direct sun in rooms when the window is within the sun's sector.
- **Solar gain** — in cool conditions, may keep shading more open to allow passive warming through glass.

Heat protection takes priority when overheating is likely, and safety conditions always override comfort
goals. Each goal is optional and configured under Options → Comfort settings.

## Presence and absence

SmartShading can use optional presence input per zone. During absence it can apply a more conservative
position, and it returns to normal comfort behavior when presence is detected again. Presence is optional;
without it, SmartShading assumes someone is present.

## Learning Engine

SmartShading can learn from its own observations and from your manual adjustments, then adapt
recommendations over time:

- Learning is **local** to your Home Assistant instance; no cloud service is involved.
- Learning is **per window**, so each surface adapts to its own conditions.
- Manual adjustments and their outcomes inform future recommendations.
- Adaptation is gated by **confidence** and applied to **shading targets** conservatively and gradually.
- Learning data uses **bounded retention** and is pruned over time.
- Learning data is **not** deleted when Learning Mode is disabled.

Learning refines recommendations over time. It does not promise perfect personalization, instant results, or
a guaranteed comfort outcome.

## Forecast learning

In addition to per-window learning, SmartShading maintains a local notion of how much to trust forecast
inputs, derived from observed outcomes. Forecast trust informs diagnostics and the exports; it is bounded
and stored locally like the rest of the learning data.

## Entities

SmartShading creates entities per zone and per window. Internal calibration values are kept internal and are
not exposed.

### Zone entities

| Entity | Type | Description |
|--------|------|-------------|
| Zone Summary | Sensor | Compact, machine-readable overview of the zone (active / recommendation-only / disabled / override / safety) with aggregate counts. |
| Learning Progress | Sensor | 0–100% indicator of how much adaptation the zone's learning has reached so far. 0% means SmartShading is still collecting data; 100% means full confidence and adaptation strength — not a claim of perfect decisions. |
| Shading Result | Sensor | Zone-level quality rating for past shading decisions (excellent/good/acceptable/poor), based on resolved outcomes. Shows "unknown" until enough outcomes have been collected. |
| Learning Mode | Switch | Enables observation, learning, and adaptation for the zone. Default on. |
| Active Control | Switch | Enables automatic cover commands for the zone. Default off. |

### Window entities

| Entity | Type | Description |
|--------|------|-------------|
| Recommendation | Sensor | The recommended cover position for the window, with detailed execution context as attributes. |
| State | Sensor | The current shading state, with the human-readable reason and supporting details as attributes. |
| Exposure | Sensor | Effective solar exposure for the window, with the geometry breakdown as attributes. |
| Cover position | Sensor | Best-known cover position (reported or estimated) with capability details. |
| Solar sector | Binary sensor | Whether the window is currently within the sun's sector (geometry only). |
| Override active | Binary sensor | Whether a manual override is currently holding the window's cover. |

### System entities

| Entity | Type | Description |
|--------|------|-------------|
| Create Support Export | Button | Writes a local support file for troubleshooting. |
| Create Research Export | Button | Writes a local, anonymized technical analysis file. |
| Debug logging | Switch | Temporarily increases log detail for diagnosis. |

## Diagnostics and exports

SmartShading provides two local exports, each created only when you press the corresponding button on the
System Entry:

- **Support Export** — a focused snapshot intended to help with troubleshooting. Created on demand, stored
  locally, and not uploaded.
- **Research Export** — a more detailed, anonymized technical view of learning and decision relationships.
  Created manually, stored locally, not uploaded, intended for you to review before sharing, and
  automatically removed after 24 hours.

Both exports are written to your Home Assistant `config/www` directory and are automatically removed after
24 hours. SmartShading never uploads them. The **Debug logging** switch is a temporary diagnostic aid — turn
it on while investigating an issue and off again afterwards.

Both exports include the **installed integration version** and a **history-metadata** block so the time span
and coverage are explicit:

- oldest and newest record timestamps,
- total available and exported record counts,
- whether the data was truncated and the cap reason,
- the store scope the export was read from.

The two exports read different sources, which is reflected in the store scope:

- **Support Export** is a **runtime-recent** diagnostic snapshot. It reflects recent activity since the last
  start and **resets on restart or reload** — so shortly after a restart it only shows recent data.
- **Research Export** uses the **persistent learning history**, so it reflects what has accumulated over time.

No-dispatch outcomes are reported with registered reason codes, including `same_position`,
`no_target_position`, `recommendation_only`, and `guard_action_interval`, so a "no command this cycle" result
is always explained rather than appearing as an unknown code.

SmartShading keeps a persistent, bounded learning decision history. Retention is age-based up to 365 days and
is additionally bounded by per-window caps, so the history is designed to support seasonal learning that
grows with real runtime. Because a hard per-window cap also applies, the export history metadata shows whether
records were truncated or capped rather than implying that a full 365 days is always exported in full.

## Privacy

- SmartShading runs locally in Home Assistant.
- No SmartShading cloud account is required.
- SmartShading does not automatically upload exports or any other data.
- Support and Research Exports are created only when you press the corresponding button.
- Export files are written under `config/www`.
- Export files are automatically removed after 24 hours.
- The Research Export contains anonymized technical learning data.
- Review export files before sharing them, so you can confirm they contain only what you intend to share.

The exports are designed to avoid raw private data, but no export can be guaranteed to be completely
anonymous — please review before sharing.

## Installation

### HACS (custom repository)

1. Add the SmartShading repository (`https://github.com/Mikasmarthome/SmartShading`) as a **custom
   repository** in HACS.
2. Select **Integration** as the repository category.
3. Install SmartShading.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services**.
6. Add the **SmartShading** integration.

SmartShading is installed as a custom repository; it is not part of the default HACS store.

### Manual installation

1. Copy `custom_components/smartshading` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Add the **SmartShading** integration under **Settings → Devices & Services**.

## Initial setup

1. Create the SmartShading **System Entry** (system-wide actions).
2. Add your first **Zone**.
3. Add one or more **Windows** and their **Covers** to the zone.
4. Optionally configure weather, solar, presence, and comfort inputs.
5. Keep **Learning Mode** enabled so SmartShading produces recommendations and can adapt.
6. Enable **Active Control** only when you want SmartShading to move covers automatically.

Every controllable shading element belongs to a zone, and there is one Zone Entry per zone. A minimal setup
needs only your covers, the window configuration, and Home Assistant's own sun and location data. Optional
sensors (weather, solar radiation, indoor temperature, presence) improve decisions but are not required.

### After setup: advanced zone settings

The initial setup covers the basic zone configuration. After creating a zone, open the zone's settings via
the gear icon to adjust schedule, night behavior, and other advanced options — see [Configuration](#configuration)
below for the full list.

## Configuration

SmartShading is configured through the Home Assistant UI (config flow). After the initial setup you can
adjust most runtime settings through the integration's options (three-dot menu → Configure):

| What you can change | Where |
|---|---|
| Weather entity and solar sensor inputs | Options → Weather and solar inputs |
| Night and morning trigger settings | Options → Schedule settings |
| Presence entities and absence delay | Options → Presence settings |
| Comfort goals and temperature thresholds | Options → Comfort settings |
| Shade-position defaults | Options → Shading behavior |
| Learning Mode per zone | Zone switch entity |
| Active Control per zone | Zone switch entity |

Windows and their assigned cover entities can be added, edited, or removed through the options flow of the
relevant zone. Some structural changes are made by adding another SmartShading entry, and a few may require
removing and re-adding the zone's entry.

Editing a window is organized into four focused pages, reached from a menu after you pick the window:

- **Basics & cover** — name, floor level, orientation, and the assigned cover entities and hardware type.
- **Shading behavior** — behavior mode, absence position, and the light/normal/strong shade positions.
- **Solar sector & obstruction zones** — the optional manual sun sector and up to three obstruction zones.
- **Window contact & night ventilation** — contact sensors, night-open blocking, and ventilation behavior.

Each page saves only its own settings and keeps the others untouched. A window can use **multiple contact
sensors**; the night/ventilation logic then reacts to the aggregated contact state (open if any selected
sensor is open). Existing single-contact configurations remain compatible.

When referring to covers or sensors, use your own entity IDs. Generic examples:

```text
cover.example_living_room_blind
sensor.example_outdoor_temperature
```

## Recommended first setup

1. Add SmartShading and configure at least one zone, window, and assigned cover entity.
2. Leave Active Control **off** and watch the Recommendation sensor and Zone Summary for a while.
3. Confirm that the recommended positions match what you would expect for the sun and weather.
4. Enable Active Control for one zone and verify the cover behaves as intended.
5. Expand to additional zones once you are confident.

## Updating

- **HACS:** update SmartShading from HACS, then restart Home Assistant.
- **Manual:** replace the `custom_components/smartshading` directory with the new version, then restart Home
  Assistant.

Your zones, windows, options, and learned data are preserved across updates.

## Troubleshooting

**No entities appear** — make sure Home Assistant was restarted after installation, then add the integration
under Settings → Devices & Services.

**A zone is unavailable** — check that the cover entities assigned to that zone's windows exist and are
available, then reload the integration.

**Recommendations appear, but covers do not move** — Active Control may be off for that zone (it is off by
default); the cover may be unavailable, or a manual override or safety state may take priority; shortly after
a restart, a brief startup grace period and minimum action intervals apply.

**Cover does not move although a recommendation exists** — check, in order:
- Active Control is off for the zone (recommendation-only mode: SmartShading computes a target but never
  dispatches it).
- The cover entity is unavailable.
- The Command Filter is holding the command back — the target may be within the configured position
  tolerance of the current position, or the minimum action interval since the last command hasn't elapsed
  yet.
- A manual override or a night-contact hold is currently active for that window.
- The global dispatch queue is briefly spacing this command out from another window's command (a short,
  expected delay, not a stuck state).

  A Support Export shows the exact reason for a held/skipped command (`command_blocked_reason` and related
  fields), so you do not have to guess between these causes.

**Cover stays closed / shutter stays down** — check, in order:
- The window's **Behavior Mode** — some modes intentionally skip daytime shading (see
  [Behavior Modes](#behavior-modes)).
- **Active Control** is enabled for the zone.
- Whether a **manual override** is currently held for that window.
- The **night/morning schedule** — the window may still be inside its configured night interval.
- The **absence** state — the zone may be in an absence position waiting for presence to return.
- The window's **contact sensor** state, if configured — an open contact can hold a window at its current
  position until it is confirmed closed again.
- A Support Export or the Recommendation/State sensor attributes for that window show the current reason and
  target position, so you can see exactly which of the above applies.

**Cover moves unexpectedly** — check, in order:
- A **safety condition** (wind, storm, or rain) — safety always takes priority and can move a cover even
  when Active Control was just enabled.
- The **night/morning schedule** — a scheduled transition may have just triggered.
- An **absence return** — presence returning ends the absence position and releases the cover back to normal
  behavior.
- The window's **Behavior Mode** — confirm it matches what you expect for that window.
- Whether a previous **manual override** has just expired, handing control back to SmartShading.
- A Support Export shows the decision and reason for the specific cycle in question.

**Active Control is disabled** — it is per zone and off by default; enable it on the zone's Active Control
switch once you are confident the covers are safe to operate automatically.

**Cover position looks inverted** — SmartShading uses the standard Home Assistant convention: `0 = closed`,
`100 = open`. Check that your cover entity reports position the same way.

**Physical position doesn't quite match the reported percentage** — SmartShading sends standard Home
Assistant cover positions (`0 = closed`, `100 = open`); how accurately the physical cover reaches that
position depends on the underlying cover integration's own calibration and position feedback (e.g. a
time-based cover's runtime calibration, or a lack of real position feedback). SmartShading does not replace
or perform your cover integration's own motor/runtime calibration — check that integration's setup if the
physical position is consistently off.

**Optional sensors are unavailable** — weather, solar, indoor temperature, and presence inputs are optional;
if one is unavailable, SmartShading falls back to its normal logic and continues to produce recommendations.

**Creating a Support Export** — press **Create Support Export** on the System Entry. The file is written
under `config/www` and removed automatically after 24 hours. For anything you cannot explain from the
Recommendation/State sensor attributes alone, a Support Export is the better starting point — it bundles the
relevant context in one place instead of raw sensor attributes, and is a better format to attach when
[asking for help](#contributing). Review a Support Export yourself before sharing it, and avoid posting one
publicly unless you have checked its contents first.

**Debug logging** — turn on the **Debug logging** switch only while investigating an issue, and turn it off
again afterwards.

## Uninstallation

1. Remove the SmartShading Zone Entries and the System Entry from **Settings → Devices & Services**.
   Removing the entries also removes their stored learning data.
2. If you installed manually, delete the `custom_components/smartshading` directory and restart Home
   Assistant. If you installed through HACS, remove it from HACS.

## Languages

SmartShading includes user-interface translations for:
Bulgarian, Catalan, Chinese (Simplified), Czech, Danish, Dutch, English, Finnish, French, German, Greek,
Hungarian, Italian, Norwegian Bokmål, Polish, Portuguese, Romanian, Russian, Slovak, Slovenian, Spanish,
Swedish, Turkish, and Ukrainian.

Home Assistant selects the matching translation automatically based on your user language and falls back to
English when a language is unavailable.

## Contributing

**Bugs** → [GitHub Issues](https://github.com/Mikasmarthome/SmartShading/issues): Home Assistant version, SmartShading version, what happened, relevant log lines (`Settings → System → Logs → smartshading`), and, if possible, a Support Export.

**Features** → Issues with the `enhancement` label.

**Especially welcome:** Testing with different cover types, facade orientations, solar sensors, rain sensors, window contact setups, and translations.

**Setup or behavior questions?** Open a GitHub Discussion and include your cover type, window orientation, selected sensors, behavior mode, and a short description of what you expected SmartShading to do.

**Useful discussions:**
- [Getting help & support](https://github.com/Mikasmarthome/SmartShading/discussions/1)
- [Feedback & real-world experience](https://github.com/Mikasmarthome/SmartShading/discussions/2)

## License

SmartShading is released under the MIT License. See [LICENSE](LICENSE).
