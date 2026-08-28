# Changelog

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
