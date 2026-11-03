"""A8 -- structural guards that keep every test visible to git and to CI.

* a new ``tests/**/test_*.py`` (and in particular a new real-HA test, at any
  depth) is never swallowed by an ignore rule,
* run artefacts below ``tests/`` stay ignored,
* no ignored test file exists on disk, every real-HA test file is tracked,
* the CI workflow runs the whole real-HA directory without any filter, cannot
  fail silently, and its per-file collection gate compares whole relative paths
  recursively (the gate's own script is executed here against simulated
  repositories and collect results, positive and negative),
* production docstrings and comments name only modules that exist.

The checks ask git itself (``git check-ignore``, ``git ls-files``), so they
follow the real ignore rules -- ``.gitignore``, ``.git/info/exclude`` and a
global excludes file -- instead of a list of file names.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "pytest.yml"
_INTEGRATION = _ROOT / "custom_components" / "smartshading"

# What pytest collects by default (python_files = test_*.py *_test.py) plus the
# files that configure the real-HA run.
_TEST_FILE = r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|conftest\.py|pytest\.ini|requirements[^/]*\.txt)$"


def _git(*args: str, cwd: Path = _ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _in_git_work_tree() -> bool:
    try:
        probe = _git("rev-parse", "--is-inside-work-tree")
    except OSError:
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "true"


pytestmark = pytest.mark.skipif(
    not _in_git_work_tree(), reason="needs a git work tree (source archive?)"
)


def _ignore_rule(path: str, cwd: Path = _ROOT) -> str | None:
    """The ignore rule that hides *path* (even if it does not exist), else None."""
    result = _git("check-ignore", "--no-index", "-v", "--", path, cwd=cwd)
    assert result.returncode in (0, 1), result.stderr
    return result.stdout.strip() or None


def _hidden_test_files(cwd: Path = _ROOT) -> list[str]:
    """Test files below ``tests/`` that exist on disk but are hidden by an ignore rule."""
    listing = _git("ls-files", "--others", "--ignored", "--exclude-standard", "--", "tests", cwd=cwd)
    assert listing.returncode == 0, listing.stderr
    return [
        line for line in listing.stdout.splitlines()
        if "__pycache__" not in line and ".pytest_cache" not in line and re.search(_TEST_FILE, line)
    ]


def _real_ha_files_on_disk_not_tracked(cwd: Path = _ROOT) -> list[str]:
    tracked = set(_git("ls-files", "--", "tests/real_ha", cwd=cwd).stdout.split())
    on_disk = {
        p.relative_to(cwd).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for p in (cwd / "tests" / "real_ha").rglob(pattern)
        if "__pycache__" not in p.parts
    }
    return sorted(on_disk - tracked)


class TestNewTestFilesAreNeverIgnored:
    @pytest.mark.parametrize("path", [
        "tests/real_ha/test_future_guard.py",
        "tests/real_ha/test_real_ha_future_scenario.py",
        "tests/real_ha/nested/test_future_guard.py",
        "tests/real_ha/nested/deeper/test_future_guard.py",
        "tests/real_ha/nested/conftest.py",
        "tests/real_ha/future_guard_test.py",
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
        "tests/real_ha/nested/__pycache__/x.pyc",
        "tests/__pycache__/x.pyc",
        "tests/real_ha/.pytest_cache/v/cache/nodeids",
        "tests/real_ha/nested/run.log",
        "tests/.coverage",
    ])
    def test_run_artefacts_stay_ignored(self, path: str) -> None:
        assert _ignore_rule(path) is not None, f"{path} should be ignored"


class TestNoTestFileHidesOnDisk:
    def test_no_ignored_test_file_exists(self) -> None:
        hidden = _hidden_test_files()
        assert hidden == [], f"test files hidden from git (and CI) by an ignore rule: {hidden}"

    def test_every_real_ha_test_file_on_disk_is_tracked_at_any_depth(self) -> None:
        assert (_ROOT / "tests" / "real_ha").is_dir()
        assert list((_ROOT / "tests" / "real_ha").glob("test_*.py")), "the real-HA suite must not be empty"
        assert _real_ha_files_on_disk_not_tracked() == []

    def test_no_tracked_artefact_below_tests(self) -> None:
        tracked = _git("ls-files", "--", "tests").stdout.split()
        artefacts = [
            p for p in tracked
            if re.search(r"(__pycache__|\.pytest_cache|\.coverage|\.pyc$|\.log$)", p)
        ]
        assert artefacts == []


def _repo_with(tmp_path: Path, *, files: list[str], ignore_rules: str = "") -> Path:
    """A throw-away git repository with the project's own ignore rules."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    assert _git("init", "-q", cwd=repo).returncode == 0
    (repo / ".gitignore").write_bytes((_ROOT / ".gitignore").read_bytes())
    with open(repo / ".gitignore", "a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n" + ignore_rules + "\n")
    for name in files:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_x():\n    pass\n", encoding="utf-8")
    _git("-c", "core.autocrlf=false", "add", "-A", cwd=repo)
    return repo


class TestRecursiveDetectionInASimulatedRepository:
    def test_flat_and_nested_files_are_all_visible(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, files=[
            "tests/real_ha/test_flat.py", "tests/real_ha/nested/test_nested.py",
            "tests/real_ha/nested/deeper/test_deep.py",
        ])
        assert _hidden_test_files(repo) == []
        assert _real_ha_files_on_disk_not_tracked(repo) == []

    def test_an_ignored_nested_test_file_is_reported(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, files=["tests/real_ha/test_flat.py", "tests/real_ha/nested/test_nested.py"],
                          ignore_rules="tests/real_ha/nested/")
        assert _hidden_test_files(repo) == ["tests/real_ha/nested/test_nested.py"]
        assert _real_ha_files_on_disk_not_tracked(repo) == ["tests/real_ha/nested/test_nested.py"]

    def test_an_ignored_nested_test_file_is_reported_when_the_whole_tests_tree_is_ignored(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with(tmp_path, files=["tests/real_ha/nested/test_nested.py"], ignore_rules="tests/")
        assert _hidden_test_files(repo) == ["tests/real_ha/nested/test_nested.py"]


def _job_text(name: str) -> str:
    """Raw text of one job of the workflow (top-level keys under ``jobs:``)."""
    text = _WORKFLOW.read_text(encoding="utf-8").replace("\r\n", "\n")
    jobs = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    blocks = re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", "\n" + jobs)
    for block in blocks:
        if block.lstrip("\n").startswith(f"  {name}:"):
            return block
    raise AssertionError(f"job {name!r} not found in {_WORKFLOW.name}")


def _step_script(job: str, step_name: str) -> str:
    match = re.search(rf"- name: {re.escape(step_name)}\n\s+run: \|\n((?:(?:          .*)?\n)+)", job)
    assert match, f"step {step_name!r} not found"
    lines = match.group(1).splitlines()
    return "\n".join(line[10:] if line.startswith("          ") else line for line in lines) + "\n"


class TestCiRunsEveryRealHaTest:
    def test_real_ha_job_runs_the_whole_directory_without_a_filter(self) -> None:
        job = _job_text("real-ha")
        runs = re.findall(r"python -m pytest tests/real_ha\b[^\n]*", job)
        assert len(runs) >= 2, "collect step and run step expected"
        for line in runs:
            arguments = line.split("pytest", 1)[1]
            for forbidden in ("--ignore", "--deselect", " -k ", " -m ", "--lf", "::", "--maxfail", "-x "):
                assert forbidden not in arguments, f"filter {forbidden!r} in: {line}"
        assert any("--collect-only" in line for line in runs)
        assert any("--collect-only" not in line and "--timeout" in line for line in runs)

    def test_no_job_can_fail_silently(self) -> None:
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert "continue-on-error" not in text
        for line in _job_text("real-ha").splitlines():
            if "pytest" in line and "||" in line:
                pytest.fail(f"pytest result swallowed: {line.strip()}")

    def test_stub_job_excludes_only_the_real_ha_directory(self) -> None:
        job = _job_text("pytest")
        line = next(ln for ln in job.splitlines() if "pytest tests/" in ln)
        assert line.count("--ignore") == 1 and "--ignore=tests/real_ha" in line
        for forbidden in ("--deselect", " -k ", " -m ", "--lf"):
            assert forbidden not in line

    def test_workflow_triggers_on_develop_and_main(self) -> None:
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert re.search(r"push:\s*\n\s*branches: \[main, develop\]", text.replace("\r\n", "\n"))


def _find_bash() -> str | None:
    candidate = shutil.which("bash")
    if candidate and "system32" not in candidate.lower().replace("\\", "/"):
        return candidate
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            git_bash = Path(base) / "Git" / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash)
    return None


_BASH = _find_bash()


@pytest.mark.skipif(_BASH is None, reason="needs a POSIX bash (the CI step is a bash script)")
class TestCollectGateScriptAgainstSimulatedRuns:
    """Runs the workflow's own gate script, unchanged, in throw-away repositories."""

    @staticmethod
    def _run(tmp_path: Path, tracked: list[str], collected_ids: list[str]) -> subprocess.CompletedProcess:
        repo = _repo_with(tmp_path, files=tracked)
        (repo / "collected.txt").write_text(
            "\n".join(collected_ids) + "\n75 tests collected in 1s\n", encoding="utf-8")
        script = repo / "gate.sh"
        script.write_text(_step_script(_job_text("real-ha"), "Every tracked real-HA test file is collected"),
                          encoding="utf-8", newline="\n")
        return subprocess.run([_BASH, "-e", "gate.sh"], cwd=repo, capture_output=True, text=True, check=False)

    def test_complete_flat_layout_passes(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, ["tests/real_ha/test_a.py", "tests/real_ha/test_b.py"],
                           ["test_a.py::TestX::test_1", "test_b.py::test_2"])
        assert result.returncode == 0, result.stdout + result.stderr

    def test_complete_recursive_result_passes(self, tmp_path: Path) -> None:
        result = self._run(
            tmp_path,
            ["tests/real_ha/test_a.py", "tests/real_ha/nested/test_b.py", "tests/real_ha/nested/deeper/test_c.py"],
            ["test_a.py::t", "nested/test_b.py::t", "nested/deeper/test_c.py::t"],
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_ids_with_the_full_repository_prefix_pass_too(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, ["tests/real_ha/nested/test_b.py"], ["tests/real_ha/nested/test_b.py::t"])
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_nested_file_missing_from_the_collect_result_fails(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, ["tests/real_ha/test_a.py", "tests/real_ha/nested/test_b.py"],
                           ["test_a.py::t"])
        assert result.returncode != 0
        assert "not collected: tests/real_ha/nested/test_b.py" in result.stdout

    def test_same_basename_in_two_folders_is_not_confused(self, tmp_path: Path) -> None:
        tracked = ["tests/real_ha/one/test_same.py", "tests/real_ha/two/test_same.py"]
        both = self._run(tmp_path / "both", tracked, ["one/test_same.py::t", "two/test_same.py::t"])
        assert both.returncode == 0, both.stdout + both.stderr
        only_one = self._run(tmp_path / "one", tracked, ["one/test_same.py::t"])
        assert only_one.returncode != 0
        assert "not collected: tests/real_ha/two/test_same.py" in only_one.stdout

    def test_a_suffix_style_test_file_is_gated_as_well(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, ["tests/real_ha/nested/future_test.py"], ["test_other.py::t"])
        assert result.returncode != 0

    def test_an_empty_tracked_set_is_a_failure(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, [], ["test_a.py::t"])
        assert result.returncode != 0


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
