# © 2026 Cartman ApS. All rights reserved.
"""Spot-check for Stage 2 (structural.py) against known Miscount and
Completeness failure IDs. NOT a strict must-flag/must-not-flag gate the
way test_stage1.py is -- README's Setup step 4 names no Stage-2-specific
target cases (Stage 2's targets are actually caught downstream, at
Stage 4/composition). This is a report of how a naive keyword classifier
does against real failure-triggering question text, run as a regular
assertion suite but with expectations set from *reading* each question,
not from aspiration -- see PROGRESS.md for the honest coverage tally.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.structural import run_stage2  # noqa: E402

# (id, question, expected_count_shaped, expected_multi_part) -- expected
# values set by reading the question text against the actual regex
# patterns in structural.py, not by aspiration. A False here documents a
# real, known gap in the v0 keyword heuristic, not a bug to silently fix.
MISCOUNT_SPOT_CHECK = [
    ("AU-M2", "Show every path from a GDPR requirement down to a Control that verifies it.", False, True),  # "every"
    (
        "SEC-M3",
        "How many regulatory duties across CRA, NIS2, and GDPR land on our access-control/MFA capability — and which regulation actually says 'multi-factor authentication'?",
        True,
        False,
    ),
    ("EM-H2", "Give me a one-paragraph summary of our overall compliance posture I can bring to the board.", False, False),
]

COMPLETENESS_SPOT_CHECK = [
    (
        "SA-H2",
        "If a single capability of ours fails, which failure endangers the most obligations — and is that even the right way to think about criticality?",
        False,
        False,
    ),
    ("PM-H1", "NIS2 was updated — which of our Policies are now potentially out of date?", False, False),
    (
        "PM-H2",
        "GDPR's rule that staff may only process data on instructions routes through a deprecated policy — what are my options, and the risk of each?",
        False,
        True,
    ),  # "each"
    (
        "RM-H2",
        "If we benchmark our NIS2 Article 21 readiness against our GDPR Article 32 posture, where do we stand?",
        False,
        False,
    ),
]


class TestStage2SpotCheck(unittest.TestCase):
    def test_miscount_cases(self):
        for qid, text, exp_count, exp_multi in MISCOUNT_SPOT_CHECK:
            with self.subTest(id=qid):
                r = run_stage2(qid, text)
                self.assertEqual(r.count_shaped, exp_count, f"{qid}: count_shaped")
                self.assertEqual(r.multi_part, exp_multi, f"{qid}: multi_part")

    def test_completeness_cases(self):
        for qid, text, exp_count, exp_multi in COMPLETENESS_SPOT_CHECK:
            with self.subTest(id=qid):
                r = run_stage2(qid, text)
                self.assertEqual(r.count_shaped, exp_count, f"{qid}: count_shaped")
                self.assertEqual(r.multi_part, exp_multi, f"{qid}: multi_part")


if __name__ == "__main__":
    unittest.main(verbosity=2)
