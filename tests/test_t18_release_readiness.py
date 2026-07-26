"""T18 — final beta release-readiness guards.

Prior to T18 there was no automated check at all for manifest.json
consistency or for the presence of the release artifacts a HACS/GitHub
release actually needs (README, LICENSE, brand images, etc.) — a
config_flow/translation change could silently break manifest.json's domain
or drop a required file without any test noticing. This file adds the
minimal guard rails the ticket's checklist calls for.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPONENT = _REPO_ROOT / "custom_components" / "smartshading"


class TestManifestConsistency:
    def test_manifest_is_valid_json(self) -> None:
        json.loads((_COMPONENT / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_has_all_required_fields(self) -> None:
        manifest = json.loads((_COMPONENT / "manifest.json").read_text(encoding="utf-8"))
        for field in ("domain", "name", "codeowners", "config_flow",
                      "documentation", "issue_tracker", "iot_class", "version"):
            assert field in manifest, f"manifest.json missing required field '{field}'"

    def test_manifest_domain_matches_const_domain(self) -> None:
        from custom_components.smartshading.const import DOMAIN
        manifest = json.loads((_COMPONENT / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["domain"] == DOMAIN, (
            f"manifest.json domain ({manifest['domain']!r}) does not match "
            f"const.DOMAIN ({DOMAIN!r}) — HA will not load this integration "
            "under the domain the code actually uses."
        )

    def test_manifest_version_matches_readme_badge(self) -> None:
        """The README's badge is explicitly labelled "Stable release" and
        links to /releases/latest — it represents whatever the actual
        published stable release is, not necessarily this branch's
        manifest.json version. On develop, manifest.json legitimately moves
        ahead to a pre-release version (e.g. "1.2.0-beta.1") while the
        stable badge correctly keeps pointing at the last real stable tag
        (e.g. "1.1.9") — the two are only required to match when
        manifest.json itself is NOT a pre-release version (i.e. on a
        release/main-track commit), which is when a drift between them
        would actually be a bug."""
        manifest = json.loads((_COMPONENT / "manifest.json").read_text(encoding="utf-8"))
        readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        version = manifest["version"]
        assert "stable-v" in readme, "README.md is missing its stable-version badge"
        if any(marker in version for marker in ("beta", "alpha", "rc")):
            return  # pre-release manifest version — badge legitimately differs
        assert f"stable-v{version}" in readme, (
            f"README.md's stable-version badge does not mention v{version} — "
            "manifest.json and README have drifted apart."
        )

    def test_manifest_requirements_is_a_list(self) -> None:
        manifest = json.loads((_COMPONENT / "manifest.json").read_text(encoding="utf-8"))
        assert isinstance(manifest["requirements"], list)


class TestReleaseArtifactsPresent:
    @pytest.mark.parametrize("relpath", [
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "hacs.json",
        "brand/icon.png",
        "brand/logo.png",
        "custom_components/smartshading/manifest.json",
        "custom_components/smartshading/brand/icon.png",
        "custom_components/smartshading/brand/logo.png",
        "custom_components/smartshading/strings.json",
        "custom_components/smartshading/services.yaml",
    ])
    def test_artifact_exists_and_is_non_empty(self, relpath: str) -> None:
        path = _REPO_ROOT / relpath
        assert path.exists(), f"Required release artifact missing: {relpath}"
        assert path.stat().st_size > 0, f"Required release artifact is empty: {relpath}"

    def test_hacs_json_is_valid_json(self) -> None:
        json.loads((_REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))

    def test_at_least_twenty_four_translation_files_present(self) -> None:
        translations_dir = _COMPONENT / "translations"
        files = list(translations_dir.glob("*.json"))
        assert len(files) >= 24, (
            f"Expected at least 24 translation files, found {len(files)}"
        )
