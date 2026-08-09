# © 2026 Cartman ApS. All rights reserved.
"""README.md Setup step 6 / PROGRESS.md "Next action": dry-run Stage 5's
risk-weighted sampling rule retroactively against the 162-instance graded
pool (tests/fixtures_stage5.py), and compare it to uniform-random sampling
of the same size -- the Success Criteria table's "Sampling efficiency"
check: "Stage 5's risk-weighted retroactive sample surfaces a higher
fraction of the known 33 failures than a uniform-random sample of the same
size -- if it doesn't, the risk-weighting rule itself needs revision before
it's trusted on live data."

This is a proxy test, not a live Stage 5 run -- see README's "What This Is
NOT": every failure in this pool is already known, so this cannot test
whether the audit catches a *new* failure kind. It can test whether the
*sampling rule* beats chance at surfacing known failures, which is the
question Setup step 6 actually asks.

Both samplers have a random component (the uniform-random sampler
entirely; the risk-weighted sampler's smaller baseline slice and its
tie-breaks within an over-full risk pool). A single draw of either is not
a reliable estimate on a pool this size, so this runs N_TRIALS independent
draws per sampler per sample size and reports the distribution, not one
number.

Run: /usr/bin/python3 tests/stage5_dry_run.py
"""

from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.stage5_sampling import (  # noqa: E402
    classify_risk,
    risk_weighted_sample,
    uniform_random_sample,
)
from tests.fixtures_stage5 import PIPELINE_INSTANCE_POOL  # noqa: E402

N_TRIALS = 2000
SAMPLE_SIZES = (20, 30, 45)  # ~12%, ~18%, ~28% of the 162-instance pool
SEED = 20260809  # today's date at spike-authoring time, fixed for reproducibility


def _summarize(hit_counts: list, sample_size: int) -> dict:
    fractions = [h / sample_size for h in hit_counts]
    return {
        "mean_hits": round(statistics.mean(hit_counts), 2),
        "median_hits": statistics.median(hit_counts),
        "min_hits": min(hit_counts),
        "max_hits": max(hit_counts),
        "mean_hit_fraction_of_sample": round(statistics.mean(fractions), 3),
        "mean_pct_of_all_33_failures_found": round(statistics.mean(hit_counts) / 33 * 100, 1),
    }


def report_risk_pool_composition(pool: list) -> dict:
    flags = [classify_risk(i) for i in pool]
    return {
        "pool_size": len(pool),
        "known_failures_in_pool": sum(1 for i in pool if i.is_known_failure),
        "stage1_disambiguation_flagged": sum(1 for f in flags if f.stage1_disambiguation),
        "multi_part_flagged": sum(1 for f in flags if f.multi_part),
        "comparison_shaped_flagged (dry-run heuristic, not a pipeline mechanism)": sum(
            1 for f in flags if f.comparison_shaped
        ),
        "any_flag (risk pool size)": sum(1 for f in flags if f.any_flag),
        "unflagged (baseline pool size)": sum(1 for f in flags if not f.any_flag),
    }


def run_trials(sample_size: int, rng: random.Random) -> dict:
    risk_hits, uniform_hits = [], []
    for _ in range(N_TRIALS):
        risk_hits.append(risk_weighted_sample(PIPELINE_INSTANCE_POOL, sample_size, rng).n_known_failures)
        uniform_hits.append(uniform_random_sample(PIPELINE_INSTANCE_POOL, sample_size, rng).n_known_failures)
    risk_summary = _summarize(risk_hits, sample_size)
    uniform_summary = _summarize(uniform_hits, sample_size)
    return {
        "sample_size": sample_size,
        "risk_weighted": risk_summary,
        "uniform_random": uniform_summary,
        "risk_weighted_beats_uniform_random": risk_summary["mean_hit_fraction_of_sample"]
        > uniform_summary["mean_hit_fraction_of_sample"],
    }


def main() -> None:
    rng = random.Random(SEED)
    print("=== Risk-pool composition (deterministic, not sampled) ===")
    for k, v in report_risk_pool_composition(PIPELINE_INSTANCE_POOL).items():
        print(f"  {k}: {v}")

    print(f"\n=== {N_TRIALS} trials per sampler per sample size, seed={SEED} ===")
    all_pass = True
    for size in SAMPLE_SIZES:
        result = run_trials(size, rng)
        all_pass = all_pass and result["risk_weighted_beats_uniform_random"]
        print(f"\n-- sample_size={size} --")
        print(f"  risk-weighted:  {result['risk_weighted']}")
        print(f"  uniform-random: {result['uniform_random']}")
        verdict = "PASS" if result["risk_weighted_beats_uniform_random"] else "FAIL"
        print(f"  risk-weighted beats uniform-random on mean hit-fraction: {verdict}")

    print(f"\n=== Overall: {'PASS' if all_pass else 'FAIL'} -- "
          f"{'risk-weighting beats chance at every tested sample size' if all_pass else 'risk-weighting rule needs revision, see README Success Criteria'}")


if __name__ == "__main__":
    main()
