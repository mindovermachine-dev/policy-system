# © 2026 Cartman ApS. All rights reserved.
"""Validates Stage 1 (alias_table.py) against the known target cases --
Setup step 4's discipline: a mechanism isn't trustworthy to route on until
it's checked against the specific case it claims to catch, AND the
specific non-regression case it must NOT trip on. See PROGRESS.md.

Run: /usr/bin/python3 -m unittest spikes.compliance-decision-pipeline.tests.test_stage1 -v
(or just run this file directly)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.alias_table import run_stage1  # noqa: E402
from tests.fixtures import STAGE1_CASES, STAGE1_ENTITY_TYPE_CASES  # noqa: E402


class TestStage1AliasTable(unittest.TestCase):
    def test_target_cases(self):
        for case in STAGE1_CASES:
            with self.subTest(id=case["id"]):
                result = run_stage1(case["id"], case["question"])
                flagged_terms = {m.canonical_term for m in result.term_matches}

                missing = case["must_flag_terms"] - flagged_terms
                self.assertFalse(
                    missing,
                    f"{case['id']}: expected to flag {missing}, "
                    f"got {flagged_terms}",
                )

                spurious = case["must_not_flag_terms"] & flagged_terms
                self.assertFalse(
                    spurious,
                    f"{case['id']}: must NOT flag {spurious} "
                    f"(non-regression case) but did -- {flagged_terms}",
                )

                # every flagged term in this curated cluster must carry a
                # definition and be marked for Stage 4 disambiguation --
                # a flag with no definition attached is useless downstream.
                for m in result.term_matches:
                    self.assertIsNotNone(m.definition, f"{case['id']}: {m.canonical_term} missing definition")
                    self.assertTrue(
                        m.disambiguation_required,
                        f"{case['id']}: {m.canonical_term} should require disambiguation",
                    )

    def test_entity_type_extraction(self):
        for case in STAGE1_ENTITY_TYPE_CASES:
            with self.subTest(id=case["id"]):
                result = run_stage1(case["id"], case["question"])
                self.assertEqual(
                    result.entity_type,
                    case["expected_entity_type"],
                    f"{case['id']}: expected entity_type="
                    f"{case['expected_entity_type']!r}, got {result.entity_type!r}",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
