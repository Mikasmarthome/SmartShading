# Changelog

## v1.2.0-beta.3.1

**This is a pre-release.** It is intended for testing on suitable Home
Assistant systems, not for production use. Please take a backup before
installing. Stable users should stay on the current stable release (v1.1.13);
installing the beta requires deliberately opting into a pre-release.

Practical verification of this exact Beta 3.1 release in a live installation is
still pending. Feedback and diagnostics exports are welcome via
[GitHub Issues](https://github.com/Mikasmarthome/SmartShading/issues).

No configuration changes and no manual migration are required. Existing
installations upgrade without reconfiguration.

### Changes since v1.2.0-beta.3

- **Zone by zone.** Normal cover movements are serialized globally across all
  zones. The movements one zone currently needs run as one uninterrupted
  package that is never interleaved with another zone's package. Packages are
  processed in the order in which the zones became ready (first in, first
  out). A zone has at most one package waiting; a newer evaluation replaces it
  and the old targets are never carried out.
- **Intermediate targets (1–99 %)** are completed one cover after another, with
  one 2 s settle pause between movements. This also holds from the last cover
  of one zone to the first cover of the next. The pause is applied once, not
  twice, at a hand-over.
- **Fully open / fully closed (0 % / 100 %)** are started at least 2 s apart
  across the whole installation, without waiting for the movement to complete.
  For mixed targets, the command that was actually sent last decides when the
  next one may start.
- **Each cover on its own.** Every cover is checked individually before it is
  sent. A cover that is already at its target, already travelling there,
  unavailable or not controlled by this zone is skipped without causing a
  waiting time for the others.
- **Manual stop of a cover.** A manual stop of a further cover of a group is
  recognised more reliably, also in the regular evaluation cycle after the
  cover's own-motion window has ended.
- **No unintended re-commanding.** A recognised manual stop keeps protecting
  the cover beyond the own-motion window, even if the target changes, until it
  is released like a manual override (duration, fixed time, lifecycle, manual
  clear or one of the `FIRST_*` release strategies). Safety is never held back
  by it, and the other covers of the group keep moving.
- **Restart.** A recognised stop is kept across a restart or reload with its
  original end time. Expired or no longer valid entries are discarded, and no
  old command is replayed.
- **Consistent `FIRST_*` release.** The `FIRST_COMFORT`, `FIRST_PROTECTION` and
  `FIRST_ANY_DECISION` strategies release a stopped cover by the same rule as
  the window's override.
- **Unreported own movements.** A cover's own movement whose state was not
  reported in time is no longer read as a manual override.
- **Tests.** Real Home Assistant regression tests were added for zone order and
  timing, per-cover handling, manual stop and its release, and restart.

### Notes and limits

- Recognising a manual stop requires usable cover state feedback and may happen
  during a later evaluation cycle, not instantly.
- Radio-controlled (RTS) covers without reliable physical feedback keep their
  existing limitations: their positions are computed estimates, not physically
  confirmed motor positions.
- SmartShading cannot guarantee that covers are physically at rest, and radio
  delivery is not guaranteed.

### Please test

Before installing, take a backup. After installing, check in your own
installation:

1. An intermediate position (1–99 %) with several zones: the movements must run
   one cover after another, zone by zone.
2. 0 % / 100 % moves: starts are at least 2 s apart.
3. Stop a cover of a group by hand: it must not be driven again until the
   override ends, while the other covers continue.
4. Reload the integration after such a stop: the stopped cover stays protected.
5. Your normal automation and, only where it can be tested safely, Safety.

## v1.2.0-beta.3

**This is a pre-release.** It is intended for testing on suitable Home
Assistant systems, not for production use. Please take a backup before
installing. Stable users should stay on the current stable release (v1.1.13);
installing the beta requires deliberately opting into a pre-release.

The real-world test in a live installation is still open. This beta is not
declared stable-ready and has not been fully tested on real devices yet.
Feedback and diagnostics exports are welcome via
[GitHub Issues](https://github.com/Mikasmarthome/SmartShading/issues).

### Highlights

- **Rain, wind and storm safety, also without a usable sun.** Tier-1 safety
  now runs when `sun.sun` data is missing or unusable, rain is treated as
  Tier-1 safety in the state guard and transitions, rain-safe targets and
  exterior screen safety positions were corrected, wind protection is wired
  into the production path, and wind/storm events trigger an immediate
  reaction.
- **Global safety priority and plan preemption.** Safety commands are admitted
  ahead of comfort commands by one global, priority-aware admission. A safety
  condition interrupts a running comfort plan, including its completion waits
  and settle pauses. Interrupted comfort commands are not replayed.
- **Duplicate-cover ownership.** A cover can only be controlled by one
  SmartShading assignment. Conflicting duplicate assignments are quarantined
  and reported as Home Assistant repairs instead of being driven by two zones.
- **Trustworthy travel-end detection.** The end of a cover movement is
  recognised from real state reports. A timeout is never treated as success:
  the rest of that group is stopped (other zones continue).
- **One fixed driving rule inside a synchronized group:**
  - Normal targets between 1 % and 99 % run one cover at a time. Each cover
    must report completion, followed by a 2 s settle pause, before the next
    cover starts.
  - Normal end positions 0 % and 100 % are started with a 2 s spacing between
    starts, without waiting for completion.
  - Safety always takes precedence over both.
- **Removed dispatch mode and pacing selection.** The old dispatch mode and
  pacing options are gone from the UI and translations. Previously stored
  dispatch settings are ignored; no migration is needed.
- **Lifecycle hardening.** Unload, reload and recovery were hardened, hazard
  states are refreshed immediately, and diagnostics references stay
  resolvable across reloads.
- **Diagnostics.** The diagnostics download is bounded by one shared size
  budget with linear-time size accounting (this includes the correction
  already released in v1.1.13), and lifecycle state is now reported.
- **Morning one-shot position** in the shared dispatch pipeline, and a fix for
  a crash when the morning position resolved to `None`.
- **Compatibility.** `smartshading.clear_manual_override` works again on
  current Home Assistant releases (relocated target API, with a fallback for
  older versions). `integration_type` and documentation metadata were aligned
  with the stable line.
- **Tests and CI.** The test suite is now fully tracked in the repository. CI
  runs the stub suite and a suite against a real Home Assistant installation,
  with a gate that fails when a tracked real-HA test file is not collected.

### Notes and limits

- Beta 3 does not yet contain the dynamic percent model planned for Beta 4.
- Existing learning data is preserved; this release adds no new travel-time
  learning.
- Computed positions for radio-controlled (RTS) covers are estimates, not
  physically confirmed motor positions.
- Radio delivery is not guaranteed, and no general Safety reaction time is
  promised.

### Please test

Before installing, take a backup. After installing, check in your own
installation:

1. An intermediate position (1–99 %) with several covers: the driving order
   must be one cover after another.
2. 0 % / 100 % moves: starts are about 2 s apart.
3. Your normal automation.
4. Reloading the integration and downloading the official diagnostics.
5. Safety behaviour, only where it can be tested safely.

## v1.2.0-beta.2.1

**This is a pre-release.** It is a narrow compatibility hotfix on top of
v1.2.0-beta.2's current `develop` state (including in-progress, not yet
finalized Beta 3 work) — not a completed Beta 3 milestone.

- Restored compatibility with current Home Assistant releases: SmartShading
  failed to import at all under Home Assistant 2026.8 with
  `ImportError: cannot import name 'async_extract_referenced_entity_ids'
  from 'homeassistant.helpers.service'` — that function was removed from
  Home Assistant core after being moved to a new
  `homeassistant.helpers.target` module. `smartshading.clear_manual_override`'s
  target resolution now uses the current API on a recent Home Assistant, and
  automatically falls back to the older API on Home Assistant versions
  still within this integration's documented "2024.1+" minimum that predate
  the move — encapsulated in a single small adapter, with no change to
  entity/device/area target resolution behavior on either path.
- Added a regression test that imports the integration against a real,
  installed Home Assistant package rather than only the test suite's
  existing lightweight stubs, specifically so this class of upstream API
  removal is caught in the future.

## v1.2.0-beta.2

**This is a pre-release.** It is intended for testing on suitable Home
Assistant systems, not for production use. Please take a backup before
installing. Feedback and diagnostics/support exports are welcome via
[GitHub Issues](https://github.com/Mikasmarthome/SmartShading/issues).
Stable users should stay on the current stable release; installing the beta
requires deliberately opting into a pre-release rather than the default
HACS/stable installation.

### Highlights

- Deterministic dispatch ordering and pacing: comfort cover commands are now
  planned and executed through a single, pure dispatch plan (fixed-priority
  sort: fully-open moves before intermediate moves, then a stable per-zone/
  per-cover order) for both sequential/spaced and parallel dispatch modes,
  replacing the previous implicit ordering and fixing a lock-span bug where
  the global dispatch lock was held across the entire completion wait and
  pacing pause instead of only around the dispatch call itself.
- Safety always preempts comfort dispatch, including across cycles: a newly
  arriving safety condition (storm, wind, rain, manual override, absence,
  etc.) now promptly interrupts an already-running comfort dispatch plan —
  mid-pacing-interval, mid-completion-wait, or mid-throttle-wait — in every
  dispatch mode, instead of only being checked at the start of a cycle.
- Fixed several cases where a presence, contact, night/morning-lifecycle, or
  Active Control toggle event could be silently delayed or dropped while a
  comfort dispatch was still in flight, due to Home Assistant's coordinator
  refresh debouncer holding a lock a fresh event needed.
- Corrected a diagnostics field that could report the dispatch subsystem as
  healthy even while real completion timeouts were present elsewhere in the
  same diagnostics output.
- Closed a narrow shutdown race where a just-triggered background save of
  learning data could keep running invisibly to the coordinator's own
  shutdown/unload sequence instead of being tracked and awaited like every
  other background task.
- Hardened the Learning Mode toggle and pending-outcome lifecycle: interrupted
  observations, experiments, and adoptions are now consistently reconciled
  across Learning Mode on/off toggles, coordinator restarts, and window
  removal, with expanded regression coverage for the real persistence
  round-trip (save/restore) of these records.
- Simplified configuration: Cover Dispatch pacing, Manual Override policy, and
  presence absence-delay timing can now be set once as System-wide defaults
  and overridden per zone only where needed, instead of being repeated in
  every zone's configuration. The zone options menu is reorganized into
  grouped submenus. The Manual Override release-strategy choice is presented
  as 4 concepts with sub-choices instead of a flat 7-value list (stored
  configuration format is unchanged).
- Removed the unused, never-triggered "strategy adoption" subsystem and the
  already-unreachable legacy manual-override evaluator; the active
  experiment/adoption (P7/P8) systems are unaffected.
- Reduced repeated-hold noise in the Support Export timeline (a repeated
  identical hold no longer produces one entry per cycle) and made
  explainability/decision-trace/timeline target and influence reporting
  consistent by resolving them from a single shared source instead of three
  independently-drifting implementations.
- Continued expansion of the automated test suite, including new dispatch
  ordering/pacing, cross-cycle safety preemption, event-triggered refresh,
  Learning Mode toggle, and restart-persistence regression tests.

### Upgrade notes

- Existing configurations and learning data are migrated/restored
  automatically; no manual reconfiguration should be required.
- Named Lifecycle Profiles (an alternative Night/Day schedule preset you
  could switch between) have been removed. Whichever schedule was actually
  active on your install is carried over unchanged as the zone's plain
  schedule; any other, inactive stored profiles are not carried over. If you
  used this feature, please verify each zone's Night/Day schedule after
  upgrading.
- If you notice unexpected behavior after upgrading, please save a
  diagnostics or support export before reporting it.
- Returning to the stable release afterward may require restoring from a
  backup if beta-generated data is not compatible with an older stable
  version; no automatic downgrade path to older stored data is guaranteed.

### Notes

No breaking changes to configuration storage are intended in this release;
the configuration UI has been reorganized (see Highlights and Upgrade notes
above).

## v1.2.0-beta.1

**This is a pre-release.** It is intended for testing on suitable Home
Assistant systems, not for production use. Please take a backup before
installing. Feedback and diagnostics/support exports are welcome via
[GitHub Issues](https://github.com/Mikasmarthome/SmartShading/issues).
Stable users should stay on the current stable release; installing the beta
requires deliberately opting into a pre-release rather than the default
HACS/stable installation.

### Highlights

- More robust cover dispatch orchestration: configurable pacing (parallel,
  spaced, or one-after-another) with optional zone batching, and hardened
  task-handle tracking so a dispatch in progress cannot race an unload.
- More deterministic behavior across multiple zones and covers, including
  clearer dispatch-generation guarding against stale in-flight commands.
- Improved manual override strategies and release handling.
- Consolidated multi-objective learning and adaptation: a single composite
  outcome score and a single confidence authority replace several
  previously separate, overlapping computations.
- Expanded explainability and diagnostics: structured decision explanations
  and richer, privacy-safe diagnostic detail.
- Assumed-state confidence and drift handling for covers without reliable
  position feedback (e.g. Somfy RTS) is now fully wired into dispatch
  decisions, persisted across restarts, and exposed in diagnostics.
- Persistence and restore improvements across the learning store and the
  forecast store, including safer handling of malformed or partial restored
  data.
- Reload/unload/task-lifecycle stability: background refresh tasks are now
  tracked and cleanly cancelled on unload instead of racing teardown.
- Continued refinement of Support Export and Research Export.
- Config flow, options flow, and general UX wording improvements.
- Full translation cleanup across all 24 supported languages — no missing,
  orphaned, or untranslated keys remain.
- A broad dead-code and architecture cleanup pass, removing confirmed-unused
  code while documenting deliberately reserved or backward-compatible pieces.
- A large expansion of the automated test suite, including new lifecycle,
  migration, persistence, and release-readiness tests.

### Upgrade notes

- Existing configurations and learning data are migrated/restored
  automatically; no manual reconfiguration should be required.
- If you notice unexpected behavior after upgrading, please save a
  diagnostics or support export before reporting it.
- Returning to the stable release afterward may require restoring from a
  backup if beta-generated data is not compatible with an older stable
  version.

### Notes

No breaking changes to configuration are intended in this release.
