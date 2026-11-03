"""A8 -- structural guards that keep every test visible to git and to CI.

* a new ``tests/**/test_*.py`` (and in particular a new real-HA test) is never
  swallowed by an ignore rule,
* run artefacts below ``tests/`` stay ignored,
* no ignored test file exists on disk, every real-HA test file is tracked,
* the CI workflow runs the whole real-HA directory without any filter and has a
  per-file collection gate (not just a count),
* production docstrings and comments name only modules that exist.

The checks ask git itself (``git check-ignore``), so they follow the real ignore
rules -- ``.gitignore``, ``.git/info/exclude`` and a global excludes file --
instead of a list of file names.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "pytest.yml"
_INTEGRATION = _ROOT / "custom_components" / "smartshading"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=_ROOT, capture_output=True, text=True, check=False,
    )


def _in_git_work_tree() -> bool:
    try:
        probe = _git("rev-parse", "--is-inside-work-tree")
    except OSError:
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "true"


pytestmark = pytest.mark.skipif(
    not _in_git_work_tree(), reason="needs a git work tree (source archive?)"
)


def _ignore_rule(path: str) -> str | None:
    """The ignore rule that hides *path* (even if it does not exist), else None."""
    result = _git("check-ignore", "--no-index", "-v", "--", path)
    assert result.returncode in (0, 1), result.stderr
    return result.stdout.strip() or None


class TestNewTestFilesAreNeverIgnored:
    @pytest.mark.parametrize("path", [
        "tests/real_ha/test_future_guard.py",
        "tests/real_ha/test_real_ha_future_scenario.py",
        "tests/real_ha/conftest.py",
        "tests/real_ha/pytest.ini",
        "tests/real_ha/requirements-real-ha.txt",
        "tests/test_future_guard.py",
        "tests/some_new_subdir/test_future_guard.py",
        "tests/conftest.py",
    ])
    def test_hypothetical_path_is_not_ignored(self, path: str) -> None:
        assert _ignore_rule(path) is None, f"{path} would be ignored by: {_ignore_rule(path)}"

    @pytest.mark.parametrize("path", [
        "tests/real_ha/__pycache__/x.pyc",
        "tests/__pycache__/x.pyc",
        "tests/real_ha/.pytest_cache/v/cache/nodeids",
        "tests/real_ha/run.log",
        "tests/.coverage",
    ])
    def test_run_artefacts_stay_ignored(self, path: str) -> None:
        assert _ignore_rule(path) is not None, f"{path} should be ignored"


class TestNoTestFileHidesOnDisk:
    def test_no_ignored_test_file_exists(self) -> None:
        listing = _git("ls-files", "--others", "--ignored", "--exclude-standard", "--", "tests")
        assert listing.returncode == 0, listing.stderr
        hidden = [
            line for line in listing.stdout.splitlines()
            if "__pycache__" not in line
            and ".pytest_cache" not in line
            and re.search(r"(^|/)(test_[^/]*\.py|conftest\.py|pytest\.ini|requirements[^/]*\.txt)$", line)
        ]
        assert hidden == [], f"test files hidden from git (and CI) by an ignore rule: {hidden}"

    def test_every_real_ha_test_file_on_disk_is_tracked(self) -> None:
        tracked = set(_git("ls-files", "tests/real_ha").stdout.split())
        on_disk = {
            p.relative_to(_ROOT).as_posix() for p in (_ROOT / "tests" / "real_ha").glob("test_*.py")
        }
        assert on_disk, "the real-HA suite must not be empty"
        assert sorted(on_disk - tracked) == []


def _job_text(name: str) -> str:
    """Raw text of one job of the workflow (top-level keys under ``jobs:``)."""
    text = _WORKFLOW.read_text(encoding="utf-8").replace("\r\n", "\n")
    jobs = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    blocks = re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", "\n" + jobs)
    for block in blocks:
        if block.lstrip("\n").startswith(f"  {name}:"):
            return block
    raise AssertionError(f"job {name!r} not found in {_WORKFLOW.name}")


class TestCiRunsEveryRealHaTest:
    def test_real_ha_job_runs_the_whole_directory_without_a_filter(self) -> None:
        job = _job_text("real-ha")
        runs = re.findall(r"python -m pytest tests/real_ha\b[^\n]*", job)
        assert len(runs) >= 2, "collect step and run step expected"
        for line in runs:
            arguments = line.split("pytest", 1)[1]
            for forbidden in ("--ignore", "--deselect", " -k ", " -m ", "--lf", "::"):
                assert forbidden not in arguments, f"filter {forbidden!r} in: {line}"
        assert any("--collect-only" in line for line in runs)
        assert any("--collect-only" not in line and "--timeout" in line for line in runs)

    def test_collection_is_checked_per_tracked_file_not_only_by_count(self) -> None:
        job = _job_text("real-ha")
        assert "git ls-files 'tests/real_ha/test_*.py'" in job
        assert 'basename "$f")::" collected.txt' in job

    def test_stub_job_excludes_only_the_real_ha_directory(self) -> None:
        job = _job_text("pytest")
        line = next(ln for ln in job.splitlines() if "pytest tests/" in ln)
        assert line.count("--ignore") == 1 and "--ignore=tests/real_ha" in line
        for forbidden in ("--deselect", " -k ", " -m ", "--lf"):
            assert forbidden not in line

    def test_workflow_triggers_on_develop_and_main(self) -> None:
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert re.search(r"push:\s*\n\s*branches: \[main, develop\]", text.replace("\r\n", "\n"))


_MODULE_PATH = re.compile(
    r"\b((?:cover_control|engines|evaluators|models|state_machine|entities)/[a-z][a-z0-9_]*\.py)\b"
)


class TestDocstringsNameExistingModules:
    def test_production_text_references_only_existing_module_paths(self) -> None:
        missing: list[str] = []
        for source in sorted(_INTEGRATION.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            for ref in sorted(set(_MODULE_PATH.findall(source.read_text(encoding="utf-8")))):
                if not (_INTEGRATION / ref).is_file():
                    missing.append(f"{source.relative_to(_ROOT).as_posix()} -> {ref}")
        assert missing == [], missing

    def test_the_travel_estimate_lives_in_dispatch_completion(self) -> None:
        text = (_INTEGRATION / "cover_control" / "dispatch_completion.py").read_text(encoding="utf-8")
        assert "travel_tracker" not in text
        assert "def estimate_travel_duration_s(" in text
