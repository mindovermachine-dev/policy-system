# © 2026 Cartman ApS. All rights reserved.
"""Sanity checks for Stage 5's sampling module (pipeline/stage5_sampling.py)
and its retroactive pool (tests/fixtures_stage5.py). The actual dry-run
report (multi-thousand-trial Monte Carlo comparison against the Success
Criteria bar) lives in tests/stage5_dry_run.py, run separately -- these are
fast, deterministic checks that the building blocks behave as designed, not
a re-run of the dry-run itself.

No FalkorDB dependency -- pure question-text classification and in-memory
sampling.
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.stage5_sampling import (  # noqa: E402
    classify_risk,
    risk_weighted_sample,
    uniform_random_sample,
)
from tests.fixtures_stage5 import PIPELINE_INSTANCE_POOL  # noqa: E402


class TestPoolIntegrity(unittest.TestCase):
    def test_pool_size_and_known_failure_count(self):
        self.assertEqual(len(PIPELINE_INSTANCE_POOL), 162)
        self.assertEqual(sum(1 for i in PIPELINE_INSTANCE_POOL if i.is_known_failure), 33)

    def test_each_run_appears_with_expected_count(self):
        by_run = {}
        for i in PIPELINE_INSTANCE_POOL:
            by_run.setdefault(i.run, 0)
            by_run[i.run] += 1
        self.assertEqual(by_run, {"dev-v1": 54, "dev-v2b": 54, "held-out": 54})


class TestRiskClassifier(unittest.TestCase):
    def test_flags_status_term_cluster_question(self):
        by_id = {(i.question_id, i.run): i for i in PIPELINE_INSTANCE_POOL}
        sec_m2 = by_id[("SEC-M2", "held-out")]
        flags = classify_risk(sec_m2)
        self.assertTrue(flags.stage1_disambiguation)

    def test_does_not_flag_stage1_on_non_clustered_question(self):
        by_id = {(i.question_id, i.run): i for i in PIPELINE_INSTANCE_POOL}
        lc_e1 = by_id[("LC-E1", "dev-v1")]
        flags = classify_risk(lc_e1)
        self.assertFalse(flags.stage1_disambiguation)

    def test_flags_comparison_shaped_on_multi_regulation_question(self):
        by_id = {(i.question_id, i.run): i for i in PIPELINE_INSTANCE_POOL}
        co_h3 = by_id[("CO-H3", "held-out")]  # "Put all three regulations' ... side by side"
        flags = classify_risk(co_h3)
        self.assertTrue(flags.comparison_shaped)


class TestSamplers(unittest.TestCase):
    def test_samplers_return_requested_size(self):
        rng = random.Random(1)
        risk = risk_weighted_sample(PIPELINE_INSTANCE_POOL, 30, rng)
        uniform = uniform_random_sample(PIPELINE_INSTANCE_POOL, 30, rng)
        self.assertEqual(risk.n_sampled, 30)
        self.assertEqual(uniform.n_sampled, 30)

    def test_risk_weighted_never_exceeds_pool_known_failures(self):
        rng = random.Random(1)
        risk = risk_weighted_sample(PIPELINE_INSTANCE_POOL, 45, rng)
        self.assertLessEqual(risk.n_known_failures, 33)

    def test_risk_weighted_beats_uniform_random_on_mean_over_many_trials(self):
        # Single-draw comparisons are noisy on a pool this size (see
        # stage5_dry_run.py's own docstring) -- this test uses enough
        # trials to make the comparison stable without duplicating the
        # full multi-sample-size report.
        rng = random.Random(42)
        n_trials = 500
        risk_total = sum(risk_weighted_sample(PIPELINE_INSTANCE_POOL, 30, rng).n_known_failures for _ in range(n_trials))
        uniform_total = sum(uniform_random_sample(PIPELINE_INSTANCE_POOL, 30, rng).n_known_failures for _ in range(n_trials))
        self.assertGreater(risk_total / n_trials, uniform_total / n_trials)


if __name__ == "__main__":
    unittest.main()
