"""T21 Phase D — duplication removals: shared pseudonymization metadata
helper, reason_codes.py as the canonical description source for
explainability.py, and diagnostics_builder._matrix's dropped legacy-alias
fields.
"""
from __future__ import annotations

from custom_components.smartshading.engines.diagnostics_privacy import (
    pseudonymization_metadata,
)
from custom_components.smartshading.engines import reason_codes as rc
from custom_components.smartshading.engines.explainability import _describe


class TestPseudonymizationMetadataHelper:
    def test_default_shape(self):
        meta = pseudonymization_metadata(stability_scope="config_entry")
        assert meta == {
            "algorithm": "hmac_sha256",
            "output_bits": 64,
            "namespace_separated": True,
            "stability_scope": "config_entry",
        }

    def test_optional_security_note(self):
        meta = pseudonymization_metadata(stability_scope="export", security_note="note")
        assert meta["security_note"] == "note"

    def test_security_note_omitted_when_not_given(self):
        meta = pseudonymization_metadata(stability_scope="export")
        assert "security_note" not in meta


class TestReasonCodesIsCanonicalDescriptionSource:
    def test_code_registered_in_reason_codes_uses_its_description(self):
        # "active_control_off" is registered in reason_codes.py's registry —
        # explainability._describe() must defer to it, not its own local table.
        assert _describe("active_control_off") == rc.description_for("active_control_off")
        assert rc.description_for("active_control_off") is not None

    def test_code_only_known_to_explainability_still_works(self):
        # "held_by_hysteresis" is NOT in reason_codes.py's registry — falls
        # back to explainability's own local table.
        assert rc.description_for("held_by_hysteresis") is None
        assert _describe("held_by_hysteresis") == "Heat hysteresis is holding the current protection state to avoid oscillation."

    def test_unknown_code_falls_back_to_humanized_code(self):
        assert _describe("totally_unknown_code_v99") == "totally unknown code v99"

    def test_none_code_returns_none(self):
        assert _describe(None) is None


class TestDiagnosticsMatrixLegacyAliasesRemoved:
    def test_matrix_entries_have_no_legacy_alias_keys(self):
        from custom_components.smartshading.engines.diagnostics_builder import (
            build_consolidated_diagnostics,
        )

        class _Coord:
            zones = {"z1": object()}
            windows = {}
            cover_groups = {}

            def effective_zone_execution(self, _zid):
                return type("Cfg", (), {"learning_enabled": True, "active_control_enabled": True})()

            def storage_diagnostics(self):
                return {}

        diag = build_consolidated_diagnostics(_Coord(), integration_version="test")
        matrix = diag["learning_active_control_matrix"]
        assert len(matrix) == 1
        entry = next(iter(matrix.values()))
        for legacy_key in (
            "observation_active", "shadow_evaluation_active",
            "real_experiments_allowed", "adoptions_allowed", "cover_commands_allowed",
        ):
            assert legacy_key not in entry, f"legacy alias '{legacy_key}' should be removed"
        # the real fields must still be present
        for real_key in ("runtime_mode", "learning_enabled", "active_control_enabled", "learning_allowed"):
            assert real_key in entry
