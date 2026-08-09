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


if __name__ == "__main__":
    unittest.main(verbosity=2)
