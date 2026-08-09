# © 2026 Cartman ApS. All rights reserved.
"""Validates pipeline/provenance.py -- the source_ref rendering the
pharma-auditor-acceptance-bar pass flagged as the concrete remaining gap
in (C) (see PROGRESS.md). Setup-step-4 discipline: validate the mechanism
against known target cases before trusting it in compose.py.

Requires FalkorDB reachable at localhost:6379 (same as test_stage4.py).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import provenance  # noqa: E402


class TestObligationProvenance(unittest.TestCase):
    def test_resolves_single_regulation_obligation(self):
        # SEC-H1 target case's NIS2-only MFA obligation (tests/fixtures.py
        # MFA_OBLIGATIONS) -- exactly one satisfying requirement.
        rows = provenance.resolve_obligation_provenance(
            "obl_deploy_multi_factor_authentication_and_secured_communication_138a1f"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["regulation_id"], "NIS2-1.0")
        self.assertEqual(rows[0]["source_ref"], "Art. 21(2), point (j)")
        self.assertEqual(rows[0]["requirement_id"], "NIS2-1.0_req_art_21.2j")

    def test_fabricated_obligation_id_resolves_to_nothing(self):
        # CO-H2-failing's deliberately-injected non-existent id -- must
        # come back empty, not error, so compose.py's rendering stays
        # honest about "no provenance found" rather than crashing.
        rows = provenance.resolve_obligation_provenance("obl_does_not_exist_deadbeef")
        self.assertEqual(rows, [])


class TestRequirementProvenance(unittest.TestCase):
    def test_resolves_direct_source_ref(self):
        # CO-H2's due-diligence requirement (tests/fixtures.py CO_H2_REQUIREMENT_IDS).
        rows = provenance.resolve_requirement_provenance("CRA-1.0_req_art_13.5")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["regulation_id"], "CRA-1.0")
        self.assertEqual(rows[0]["source_ref"], "Art. 13(5)")


class TestResolveSourceRefsDispatch(unittest.TestCase):
    def test_skips_non_obligation_non_requirement_ids(self):
        # Control, Capability, and bare Regulation ids must not be resolved
        # here -- see provenance.py's module docstring for why forcing a
        # chain for these is wrong, not just unbuilt.
        ids = [
            "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v1_manual",
            "cap_data_encryption_0e50d3",
            "CRA-1.0",
        ]
        self.assertEqual(provenance.resolve_source_refs(ids), {})

    def test_resolves_mixed_batch_and_skips_the_rest(self):
        ids = [
            "obl_deploy_multi_factor_authentication_and_secured_communication_138a1f",
            "cap_data_encryption_0e50d3",  # must be skipped
        ]
        resolved = provenance.resolve_source_refs(ids)
        self.assertEqual(set(resolved.keys()), {"obl_deploy_multi_factor_authentication_and_secured_communication_138a1f"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
