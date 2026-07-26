# Changelog

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
