"""Export file retention for SmartShading — deletes expired export files after 24 hours.

Allowlisted filename patterns (strict regex, full-match required):
  smartshading_support_export_<UTC_TIMESTAMP>.json
  smartshading_research_export_<UTC_TIMESTAMP>.json
  smartshading_export_<TIMESTAMP>_<hex>.json   (legacy pattern)

The UTC timestamp embedded in the filename is the authoritative age source.
File mtime is used only as a fallback when no parseable timestamp is found.

Safety invariants
-----------------
  Only files inside the designated export directory are ever deleted.
  Each candidate path is resolved and validated to remain inside export_dir.
  Symlinks are never followed: only regular, non-symlink files are deleted.
  Deletion failure (PermissionError, OSError) is logged and skipped —
  it never prevents export creation or crashes SmartShading.
  Non-allowlisted files, .storage files, and unrelated JSON files are
  never touched.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

RETENTION_HOURS: int = 24

_ALLOWLISTED_PATTERNS = (
    re.compile(r"^smartshading_support_export_\d{8}T\d{6}Z(?:_[0-9a-f]+)?\.json$"),
    re.compile(r"^smartshading_research_export_\d{8}T\d{6}Z(?:_[0-9a-f]+)?\.json$"),
    re.compile(r"^smartshading_export_\d{8}T\d{6}_[0-9a-f]{6}\.json$"),
)

_TIMESTAMP_RE = re.compile(r"(\d{8}T\d{6}Z)")


def _is_allowlisted(filename: str) -> bool:
    return any(p.match(filename) for p in _ALLOWLISTED_PATTERNS)


def _parse_filename_timestamp(filename: str) -> datetime | None:
    """Extract and parse the UTC timestamp embedded in a SmartShading export filename.

    Returns None if no parseable UTC timestamp is found.
    """
    m = _TIMESTAMP_RE.search(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _file_age_seconds(filepath: Path, now: datetime) -> float:
    """Return the age of *filepath* in seconds.

    Prefers the timestamp parsed from the filename (authoritative).
    Falls back to the file's mtime if the filename has no parseable timestamp.
    Returns 0.0 on any error so the file is never mistakenly deleted.
    """
    ts = _parse_filename_timestamp(filepath.name)
    if ts is not None:
        return (now - ts).total_seconds()
    try:
        mtime = datetime.fromtimestamp(filepath.stat().st_mtime, tz=timezone.utc)
        return (now - mtime).total_seconds()
    except OSError:
        return 0.0


def cleanup_old_exports(export_dir: Path, now: datetime | None = None) -> int:
    """Delete SmartShading export files older than RETENTION_HOURS.

    Parameters
    ----------
    export_dir:
        The directory to scan (e.g. /config/www/).
    now:
        Current UTC time.  Defaults to ``datetime.now(timezone.utc)``.

    Returns
    -------
    int
        Number of files successfully deleted.

    Raises
    ------
    Never raises.  All errors are logged as warnings.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not export_dir.exists():
        return 0

    try:
        resolved_dir = export_dir.resolve()
    except OSError:
        _LOGGER.warning(
            "SmartShading: export retention: could not resolve export dir %s", export_dir
        )
        return 0

    threshold_seconds = RETENTION_HOURS * 3600
    deleted = 0

    try:
        candidates = list(export_dir.iterdir())
    except OSError:
        _LOGGER.warning(
            "SmartShading: export retention: could not list export dir %s", export_dir
        )
        return 0

    for filepath in candidates:
        # Never follow symlinks; only touch regular files.
        if filepath.is_symlink() or not filepath.is_file():
            continue

        if not _is_allowlisted(filepath.name):
            continue

        # Safety: resolved path must stay inside the export directory.
        try:
            resolved_file = filepath.resolve()
            if not str(resolved_file).startswith(str(resolved_dir)):
                _LOGGER.warning(
                    "SmartShading: export retention: skipping %s — resolved path escapes export dir",
                    filepath.name,
                )
                continue
        except OSError:
            continue

        age_seconds = _file_age_seconds(filepath, now)
        if age_seconds < threshold_seconds:
            continue

        try:
            filepath.unlink()
            deleted += 1
            _LOGGER.debug(
                "SmartShading: export retention: deleted %s (age %.0f s)", filepath.name, age_seconds
            )
        except OSError as exc:
            _LOGGER.warning(
                "SmartShading: export retention: could not delete %s: %s", filepath.name, exc
            )

    if deleted:
        _LOGGER.info(
            "SmartShading: export retention: deleted %d expired export file(s) from %s",
            deleted,
            export_dir,
        )

    return deleted
