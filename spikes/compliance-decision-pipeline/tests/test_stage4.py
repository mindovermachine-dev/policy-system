# © 2026 Cartman ApS. All rights reserved.
"""Validates Stage 4's four v0 sub-checks against live graph data --
requires FalkorDB reachable at localhost:6379, graph `policy_system` (see
PROGRESS.md "Environment"). Each check is run against both a must-flag
case (a real recorded failure) and a must-not-flag case (the
non-regression pair, usually the correct/golden answer), per Setup step
4's discipline: a mechanism unchecked against its own non-regression case
isn't trustworthy either.

Run: /usr/bin/python3 tests/test_stage4.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import fitness  # noqa: E402
from tests.fixtures import (  # noqa: E402
    REFERENCE_DATE,
    INCIDENT_POLICY_CONTROLS as _INCIDENT_POLICY_CONTROLS,
    DEPRECATED_PAST_DUE_CONTROL as _DEPRECATED_PAST_DUE_CONTROL,
    TRUE_OVERDUE_CONTROL as _TRUE_OVERDUE_CONTROL,
    MFA_OBLIGATIONS as _MFA_OBLIGATIONS,
    SBOM_CAPABILITY as _SBOM_CAPABILITY,
    MAX_FANOUT_CAPABILITY as _MAX_FANOUT_CAPABILITY,
    MAX_FANOUT_COUNT as _MAX_FANOUT_COUNT,
    CO_H2_REQUIREMENT_IDS as _CO_H2_REQUIREMENT_IDS,
    CO_H2_OBLIGATIONS as _CO_H2_OBLIGATIONS,
    DATA_ENCRYPTION_CAPABILITY as _DATA_ENCRYPTION_CAPABILITY,
    DATA_ENCRYPTION_OBLIGATIONS as _DATA_ENCRYPTION_OBLIGATIONS,
    SEC_H4_OVERCLAIMED_MFA_OBLIGATION as _SEC_H4_OVERCLAIMED_MFA_OBLIGATION,
    SEC_H4_OVERCLAIMED_LOGGING_OBLIGATION as _SEC_H4_OVERCLAIMED_LOGGING_OBLIGATION,
)


class TestRuleCheckOverdueExcludesDeprecated(unittest.TestCase):
    """Targets SEC-M2/SEC-M4."""

    def test_flags_when_deprecated_control_included(self):
        # Reproduces the actual SEC-M2/SEC-M4 failure: the true overdue
        # control plus the deprecated-but-past-due trap.
        answer = {_TRUE_OVERDUE_CONTROL, _DEPRECATED_PAST_DUE_CONTROL}
        result = fitness.check_overdue_excludes_deprecated(REFERENCE_DATE, answer)
        self.assertTrue(result.flagged, result.reason)

    def test_does_not_flag_correct_overdue_set(self):
        # Non-regression: the golden-correct overdue set (deprecated
        # control correctly excluded) must not trip the check.
        answer = {_TRUE_OVERDUE_CONTROL}
        result = fitness.check_overdue_excludes_deprecated(REFERENCE_DATE, answer)
        self.assertFalse(result.flagged, result.reason)

    def test_canonical_overdue_set_matches_ground_truth(self):
        self.assertEqual(fitness.canonical_overdue_set(REFERENCE_DATE), {_TRUE_OVERDUE_CONTROL})


class TestScopeMatchRegulationRouting(unittest.TestCase):
    """Targets AU-H4."""

    def test_flags_over_claimed_regulation(self):
        # Reproduces AU-H4: claiming NIS2/GDPR are undermined via the
        # Security Logging capability, when only CRA/HELVEX-SOP route
        # through it.
        result = fitness.check_regulation_scope("cap_security_logging_c4d9e2", {"CRA-1.0", "NIS2-1.0", "GDPR-1.0"})
        self.assertTrue(result.flagged, result.reason)
        self.assertIn("NIS2-1.0", result.evidence["claimed"])

    def test_does_not_flag_correctly_scoped_claim(self):
        # Non-regression: CRA/HELVEX-SOP alone is the golden-correct claim.
        result = fitness.check_regulation_scope("cap_security_logging_c4d9e2", {"CRA-1.0", "HELVEX-SOP-1.0"})
        self.assertFalse(result.flagged, result.reason)


class TestEntityTypeCrossCheck(unittest.TestCase):
    """Targets EM-E3."""

    def test_flags_granularity_mismatch(self):
        # Reproduces EM-E3: question entity-type "chain", answer counted
        # in "control".
        result = fitness.check_entity_type_match("chain", "control")
        self.assertTrue(result.flagged, result.reason)

    def test_does_not_flag_matching_unit(self):
        result = fitness.check_entity_type_match("chain", "chain")
        self.assertFalse(result.flagged, result.reason)


class TestExistenceGrounding(unittest.TestCase):
    """Targets SEC-E1 and SEC-H1 (2 of the 5 ambiguous dev-v2b cases)."""

    def test_sec_e1_confirms_real_control_set(self):
        query = (
            "MATCH (p:Policy {id: 'pol_incident_vulnerability_response_policy_9de859'})"
            "-[:SUPPORTED_BY]->(:Standard)-[:IMPLEMENTED_BY]->(c:Control) RETURN c.id"
        )
        result = fitness.check_existence(_INCIDENT_POLICY_CONTROLS, query)
        self.assertFalse(result.flagged, result.reason)
        self.assertEqual(set(result.evidence["retrieved_ids"]), _INCIDENT_POLICY_CONTROLS)

    def test_sec_e1_flags_a_fabricated_control_id(self):
        query = (
            "MATCH (p:Policy {id: 'pol_incident_vulnerability_response_policy_9de859'})"
            "-[:SUPPORTED_BY]->(:Standard)-[:IMPLEMENTED_BY]->(c:Control) RETURN c.id"
        )
        claimed = set(_INCIDENT_POLICY_CONTROLS) | {"ctrl_does_not_exist_deadbeef"}
        result = fitness.check_existence(claimed, query)
        self.assertTrue(result.flagged, result.reason)

    def test_sec_h1_confirms_real_seven_obligation_set(self):
        query = (
            "MATCH (o:Obligation)-[:REQUIRES]->"
            "(c:Capability {id: 'cap_access_control_authentication_151816'}) RETURN o.id"
        )
        result = fitness.check_existence(_MFA_OBLIGATIONS, query)
        self.assertFalse(result.flagged, result.reason)
        self.assertEqual(len(result.evidence["retrieved_ids"]), 7)


class TestScopeMatchRegulationRoutingSAH1(unittest.TestCase):
    """Targets SA-H1 -- reuses check_regulation_scope (same mechanism as
    AU-H4, different capability): the SBOM capability is required only by
    CRA today, so claiming NIS2/GDPR redundant coverage is an over-claim.
    """

    def test_flags_claimed_redundant_coverage_that_does_not_exist(self):
        result = fitness.check_regulation_scope(_SBOM_CAPABILITY, {"NIS2-1.0"})
        self.assertTrue(result.flagged, result.reason)

    def test_does_not_flag_correct_zero_redundant_coverage_claim(self):
        # Non-regression: golden-correct claim is CRA-only, zero NIS2/GDPR.
        result = fitness.check_regulation_scope(_SBOM_CAPABILITY, {"CRA-1.0"})
        self.assertFalse(result.flagged, result.reason)


class TestFanoutMaximum(unittest.TestCase):
    """Targets SA-H2 -- ranking grounding, not membership: is the claimed
    capability actually the one required by the most obligations, and is
    the claimed count right.
    """

    def test_flags_wrong_capability_claimed_as_maximum(self):
        result = fitness.check_fanout_maximum("cap_security_incident_reporting_449fa4", 30)
        self.assertTrue(result.flagged, result.reason)
        self.assertEqual(result.evidence["actual_capability_id"], _MAX_FANOUT_CAPABILITY)

    def test_does_not_flag_correct_maximum_and_count(self):
        result = fitness.check_fanout_maximum(_MAX_FANOUT_CAPABILITY, _MAX_FANOUT_COUNT)
        self.assertFalse(result.flagged, result.reason)


class TestExistenceGroundingCOH2(unittest.TestCase):
    """Targets CO-H2 -- the CRA obligations a correct 'beyond our own fix'
    answer must cite, independently re-derived via the Requirement->
    SATISFIED_BY->Obligation edge (not the golden text, and not any query
    that could have produced a hypothetical answer)."""

    def _query(self) -> str:
        ids = ", ".join(f"'{rid}'" for rid in _CO_H2_REQUIREMENT_IDS)
        return f"MATCH (r:Requirement)-[:SATISFIED_BY]->(o:Obligation) WHERE r.id IN [{ids}] RETURN o.id"

    def test_confirms_real_seven_obligation_set(self):
        result = fitness.check_existence(_CO_H2_OBLIGATIONS, self._query())
        self.assertFalse(result.flagged, result.reason)
        self.assertEqual(set(result.evidence["retrieved_ids"]), _CO_H2_OBLIGATIONS)

    def test_flags_a_missing_obligation(self):
        incomplete = set(_CO_H2_OBLIGATIONS) - {"obl_report_actively_exploited_vulnerabilities_8fd384"}
        claimed_with_fabrication = incomplete | {"obl_does_not_exist_deadbeef"}
        result = fitness.check_existence(claimed_with_fabrication, self._query())
        self.assertTrue(result.flagged, result.reason)


class TestExistenceGroundingSECH4(unittest.TestCase):
    """Targets SEC-H4 -- reuses check_existence, scoped to the failing
    control's own capability (cap_data_encryption) rather than the whole
    policy's capability set. Reproduces the RUNBOOK-recorded failure:
    'listed duties verified by the v2/v3 controls, not failing on Aug 15'
    -- i.e. cited obligations that require a *different* capability
    entirely. Golden text for SEC-H4 is not in this repo (held-out
    question); this is RUNBOOK-note-validated, not golden-validated, per
    PROGRESS.md.
    """

    def _query(self) -> str:
        return (
            "MATCH (o:Obligation)-[:REQUIRES]->"
            f"(c:Capability {{id: '{_DATA_ENCRYPTION_CAPABILITY}'}}) RETURN o.id"
        )

    def test_does_not_flag_the_real_encryption_obligation_set(self):
        result = fitness.check_existence(_DATA_ENCRYPTION_OBLIGATIONS, self._query())
        self.assertFalse(result.flagged, result.reason)
        self.assertEqual(set(result.evidence["retrieved_ids"]), _DATA_ENCRYPTION_OBLIGATIONS)

    def test_flags_mfa_and_logging_obligations_verified_by_other_controls(self):
        # Reproduces the actual over-claim: citing duties genuinely backed
        # by v2 (MFA) and v3 (logging), neither of which is the control
        # under test (v1, Encryption-at-Rest).
        over_claimed = {
            "obl_apply_pseudonymisation_and_encryption_as_controller_fc1f7e",
            _SEC_H4_OVERCLAIMED_MFA_OBLIGATION,
            _SEC_H4_OVERCLAIMED_LOGGING_OBLIGATION,
        }
        result = fitness.check_existence(over_claimed, self._query())
        self.assertTrue(result.flagged, result.reason)
        self.assertIn(_SEC_H4_OVERCLAIMED_MFA_OBLIGATION, result.evidence["claimed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
