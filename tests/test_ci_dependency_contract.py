"""Dependency contract: every external (non-stdlib, non-repo-internal)
top-level import actually used by tests/ or custom_components/smartshading/
must be covered by requirements-test.txt — the single source of truth the
CI Pytest workflow installs from (.github/workflows/pytest.yml).

Deliberately narrow and AST-based (not a "does pip freeze match" check,
which would be fragile against transitive dependencies, platform-specific
packages, or dev-only tools unrelated to running the suite). This test only
asks: "does every module this codebase actually `import`s at the top level
have an entry in requirements-test.txt, or is it explicitly and
deliberately exempted with a documented reason?"

`homeassistant` is the one deliberate exemption: the entire test suite
stubs it via tests/conftest.py and per-file sys.modules injection rather
than depending on the real (very large) `homeassistant` PyPI package — see
requirements-test.txt's own header comment for the full rationale.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REQUIREMENTS_FILE = _REPO_ROOT / "requirements-test.txt"

# Modules that are real top-level imports in this codebase but must NOT be
# installed as a test dependency, with the reason why. Do not add an entry
# here to silence a genuinely missing dependency — only for modules that
# are deliberately stubbed/mocked instead of installed.
_DELIBERATELY_STUBBED_NOT_INSTALLED = {
    "homeassistant": (
        "Stubbed via tests/conftest.py + per-file sys.modules injection — "
        "never actually imported for real. Installing the real package "
        "would pull in a very large dependency tree the suite never "
        "exercises."
    ),
}


def _stdlib_module_names() -> frozenset[str]:
    # sys.stdlib_module_names (3.10+) already excludes this project's own
    # top-level packages by construction (it only lists interpreter-bundled
    # modules), so no extra repo-internal exclusion is needed here.
    return frozenset(sys.stdlib_module_names)


def _collect_top_level_imports(root: Path) -> dict[str, str]:
    """Returns {module_name: one_example_file} for every top-level import
    (import X / from X import Y, X not relative) found under `root`."""
    found: dict[str, str] = {}
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    found.setdefault(top, str(path.relative_to(_REPO_ROOT)))
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import — repo-internal, not external
                if node.module is None:
                    continue
                top = node.module.split(".")[0]
                found.setdefault(top, str(path.relative_to(_REPO_ROOT)))
    return found


def _parse_requirements_names(path: Path) -> set[str]:
    """Best-effort package-name extraction from a plain requirements.txt
    (no markers/extras/VCS URLs used in this file — kept simple
    deliberately; see requirements-test.txt itself)."""
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip any version specifier (==, >=, etc.) if one is ever added.
        for sep in ("==", ">=", "<=", "~=", ">", "<", "!="):
            if sep in line:
                line = line.split(sep, 1)[0]
                break
        names.add(line.strip().lower())
    return names


class TestDependencyContract:
    def test_every_external_import_is_covered_or_deliberately_exempted(self) -> None:
        stdlib = _stdlib_module_names()
        requirements = _parse_requirements_names(_REQUIREMENTS_FILE)

        all_imports: dict[str, str] = {}
        for root in (_REPO_ROOT / "tests", _REPO_ROOT / "custom_components" / "smartshading"):
            all_imports.update({
                name: example
                for name, example in _collect_top_level_imports(root).items()
                if name not in all_imports
            })

        violations = []
        for name, example_file in sorted(all_imports.items()):
            if name in stdlib:
                continue
            if name == "custom_components":
                continue  # this repo's own package, imported by its absolute name in tests
            if name in _DELIBERATELY_STUBBED_NOT_INSTALLED:
                continue
            # No import-name -> PyPI-name remapping table: every current
            # external import (pytest, voluptuous) already matches its
            # requirements-test.txt entry verbatim. If a future dependency
            # ever needs one (e.g. import name != PyPI name), add a small,
            # explicitly-justified mapping here rather than reintroducing a
            # speculative table with no current effect.
            if name.lower() not in requirements:
                violations.append(
                    f"{name!r} (imported in {example_file}) has no matching entry in "
                    f"requirements-test.txt and is not in _DELIBERATELY_STUBBED_NOT_INSTALLED"
                )
        assert not violations, "\n".join(violations)

    def test_requirements_file_has_no_unused_entries(self) -> None:
        """Softer, informational-direction check: every package actually
        listed in requirements-test.txt should correspond to something the
        codebase really imports (tzdata is exempt — it backs zoneinfo's
        runtime data lookup, not a Python-level import name)."""
        requirements = _parse_requirements_names(_REQUIREMENTS_FILE)
        runtime_data_only_packages = {"tzdata"}

        all_imports: dict[str, str] = {}
        for root in (_REPO_ROOT / "tests", _REPO_ROOT / "custom_components" / "smartshading"):
            all_imports.update(_collect_top_level_imports(root))
        imported_names = {name.lower() for name in all_imports}

        unused = requirements - imported_names - runtime_data_only_packages
        assert not unused, f"requirements-test.txt lists packages never imported: {unused}"

    def test_zoneinfo_usage_implies_tzdata_dependency(self) -> None:
        """Any file that constructs zoneinfo.ZoneInfo(...) needs the
        `tzdata` package for portability (no bundled tz database in the
        stdlib) — if such usage ever appears, tzdata must stay listed."""
        uses_zoneinfo_construction = False
        for root in (_REPO_ROOT / "tests", _REPO_ROOT / "custom_components" / "smartshading"):
            for path in root.rglob("*.py"):
                if "ZoneInfo(" in path.read_text(encoding="utf-8"):
                    uses_zoneinfo_construction = True
                    break
        if uses_zoneinfo_construction:
            requirements = _parse_requirements_names(_REQUIREMENTS_FILE)
            assert "tzdata" in requirements
