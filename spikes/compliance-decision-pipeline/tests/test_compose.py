# © 2026 Cartman ApS. All rights reserved.
"""Validates the three-block output composer (pipeline/compose.py)
against the same end-to-end scenarios tests/run_target_cases.py prints for
inspection -- this file is the pass/fail version of that demonstration.

Two README.md success criteria live here specifically:
- "Three-block completeness" -- every output has non-empty (A)/(B)/(C).
- "No false auto-pass" -- every *-failing scenario (a real recorded
  transcript failure) must not compose into a confident statement.

Requires FalkorDB reachable at localhost:6379 (same as test_stage4.py).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.run_target_cases import build_scenarios  # noqa: E402


class TestThreeBlockComposer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenarios = dict(build_scenarios())

    def test_every_scenario_is_three_block_complete(self):
        for label, output in self.scenarios.items():
            with self.subTest(label=label):
                self.assertTrue(output.is_complete, f"{label}: missing (A), (B), or (C)")

    def test_failing_scenarios_never_get_a_confident_statement(self):
        # No false auto-pass: every *-failing scenario reproduces a real
        # recorded transcript failure (SEC-M2/SEC-M4/AU-H4/EM-E3, see
        # PROGRESS.md) -- none may compose into the confident-type text.
        for label, output in self.scenarios.items():
            if not label.endswith("-failing"):
                continue
            with self.subTest(label=label):
                self.assertNotEqual(
                    output.confidence_statement,
                    "Given the data currently in the system, this is correct.",
                    f"{label}: a known failure was delivered as verified",
                )
                self.assertIn("Fitness gate failed", output.confidence_statement)
                self.assertTrue(output.answer.startswith("[FLAGGED -- not verified]"))

    def test_golden_scenarios_get_a_confident_statement(self):
        # Type G is the one exception: README's Output posture makes the
        # hedge the *default* for G even once the fitness gate clears
        # (SA-H2's ~50%-on-both-tool-surfaces ceiling has no known
        # trigger-based fix to gate on) -- so a golden G scenario still
        # gets "[Draft, unverified]", never the confident text.
        for label, output in self.scenarios.items():
            if not label.endswith("-golden"):
                continue
            with self.subTest(label=label):
                if output.question_id == "SA-H2":
                    self.assertNotEqual(output.confidence_statement, "Given the data currently in the system, this is correct.")
                    self.assertTrue(output.answer.startswith("[Draft, unverified]"))
                else:
                    self.assertEqual(
                        output.confidence_statement,
                        "Given the data currently in the system, this is correct.",
                        f"{label}: a correct answer was not delivered with confidence",
                    )
                self.assertFalse(output.answer.startswith("[FLAGGED"))

    def test_verification_data_carries_the_contradicting_evidence(self):
        # (C) must be legible enough that a reader could catch the error
        # themselves -- spot-check the two clearest cases (README's Block
        # (C) sufficiency success criterion).
        au_h4 = self.scenarios["AU-H4-failing"]
        routed = au_h4.verification_data["fitness_checks"][0]["evidence"]["routed_regulations"]
        self.assertEqual(set(routed), {"CRA-1.0", "HELVEX-SOP-1.0"})

        em_e3 = self.scenarios["EM-E3-failing"]
        self.assertEqual(em_e3.verification_data["entity_type"], "chain")
        cross_check_evidence = em_e3.verification_data["fitness_checks"][0]["evidence"]
        self.assertEqual(cross_check_evidence["answer_counting_unit"], "control")

    def test_verification_data_carries_source_ref_provenance_for_obligation_claims(self):
        # The acceptance-bar pass's concrete finding (PROGRESS.md): (C) must
        # resolve every claimed Obligation id to its regulation-article
        # source_ref, not just the structured graph values. SEC-H1-golden
        # cites 7 real obligation ids -- each must resolve.
        sec_h1 = self.scenarios["SEC-H1-golden"]
        source_refs = sec_h1.verification_data["source_refs"]
        mfa_obligation = "obl_deploy_multi_factor_authentication_and_secured_communication_138a1f"
        self.assertIn(mfa_obligation, source_refs)
        self.assertEqual(source_refs[mfa_obligation][0]["regulation_id"], "NIS2-1.0")
        self.assertEqual(source_refs[mfa_obligation][0]["source_ref"], "Art. 21(2), point (j)")

    def test_source_refs_omit_control_and_capability_ids(self):
        # Control/Capability ids (SEC-M2's overdue Controls, SA-H2's
        # fanout Capability) must not appear in source_refs -- see
        # provenance.py's module docstring for why forcing one would be
        # wrong, not just unbuilt.
        sec_m2 = self.scenarios["SEC-M2-failing"]
        self.assertEqual(sec_m2.verification_data["source_refs"], {})

    def test_mandatory_check_not_performed_fails_closed_not_vacuously(self):
        # Stage 3's concrete fix (pipeline/routing.py, PROGRESS.md): AU-M4
        # composes with zero Stage 4 checks (no mechanism exists yet for
        # "stale" disambiguation) -- before Stage 3's enforcement existed,
        # FitnessResult.passed would have been vacuously True on an empty
        # check list, and this would have shipped as a confident answer.
        au_m4 = self.scenarios["AU-M4-unbuilt-check"]
        self.assertFalse(au_m4.answer.startswith("[Draft"))
        self.assertTrue(au_m4.answer.startswith("[FLAGGED -- not verified]"))
        self.assertIn("mandatory", au_m4.confidence_statement.lower())
        self.assertIn("stale_chain_strict_reading", au_m4.confidence_statement)
        self.assertNotEqual(
            au_m4.confidence_statement,
            "Given the data currently in the system, this is correct.",
        )
        self.assertEqual(au_m4.verification_data["fitness_checks"], [])
        self.assertTrue(au_m4.is_complete)

    def test_em_m4_root_cause_misattribution_fails_closed(self):
        # Granularity precision's other half (PROGRESS.md): EM-M4's
        # mismatch is a root-cause misattribution, not a counting-unit one
        # -- check_evidence_gap_root_cause, not check_entity_type_match.
        em_m4_failing = self.scenarios["EM-M4-failing"]
        self.assertTrue(em_m4_failing.answer.startswith("[FLAGGED -- not verified]"))
        self.assertIn("evidence gap", em_m4_failing.confidence_statement)
        checks = em_m4_failing.verification_data["fitness_checks"]
        self.assertTrue(any(c["check_name"] == "evidence_gap_root_cause" and c["flagged"] for c in checks))

    def test_em_m4_correct_root_cause_split_gets_confident_statement(self):
        em_m4_golden = self.scenarios["EM-M4-golden"]
        self.assertEqual(
            em_m4_golden.confidence_statement,
            "Given the data currently in the system, this is correct.",
        )
        self.assertFalse(em_m4_golden.answer.startswith("[FLAGGED"))

    def test_routing_recorded_in_verification_data(self):
        # (C) should carry Stage 3's own decision, not just Stage 4's --
        # an auditor should be able to see *why* a check was mandatory,
        # not just that one was or wasn't run.
        sec_m2 = self.scenarios["SEC-M2-failing"]
        routing_info = sec_m2.verification_data["routing"]
        self.assertEqual(routing_info["path"], "direct_mandatory_check")
        self.assertEqual(routing_info["mandatory_check_names"], ["rule_overdue_excludes_deprecated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
