# © 2026 Cartman ApS. All rights reserved.
"""Stage 5 -- sampling rule (README.md, "Stage 5 -- Continuous audit and
mechanism growth", step 1: "Sample").

This is the only piece of Stage 5 this spike builds and dry-runs (PROGRESS.md
Setup step 6) -- audit/classify/promote/regression-check/version (steps 2-6)
are process, not code, and stay out of scope for v0.

Risk-weighted, not uniform, per README: "prioritize Stage 1 alias/near-match
flags, comparison/relation-shaped claims, and decomposed-and-composed
answers." Reuses the already-validated Stage 1/2 classifiers
(`run_stage1`, `run_stage2`) for the first and third signals. The middle
signal -- "comparison/relation-shaped claims" -- has no built classifier
anywhere in this pipeline (README's type table names it descriptively, by
example, not as a running mechanism); `_is_comparison_shaped` below is a
narrow keyword heuristic built *for this dry-run only*, not a Stage 1/2/4
mechanism, and is labeled as such everywhere it's surfaced.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import List, Sequence

from .alias_table import run_stage1
from .structural import run_stage2

_REGULATION_NAMES = (r"\bCRA\b", r"\bNIS ?2\b", r"\bGDPR\b")
_COMPARISON_KEYWORDS = (
    r"\bcompare\b", r"\bcomparing\b", r"\bversus\b", r"\bvs\.?\b",
    r"\bside by side\b", r"\bbenchmark\b", r"\boverlap\b",
    r"\bsimilar kind\b", r"\bconverge\b", r"\bboth\b.*\band\b",
)


def _is_comparison_shaped(text: str) -> bool:
    """Dry-run-only heuristic, not a pipeline mechanism -- see module
    docstring. Flags a question naming 2+ regulations by name, or using an
    explicit comparison/relation verb, as a stand-in for README's type E
    ("cross-entity/regulation comparison")."""
    reg_hits = sum(1 for pat in _REGULATION_NAMES if re.search(pat, text, re.IGNORECASE))
    if reg_hits >= 2:
        return True
    return any(re.search(pat, text, re.IGNORECASE) for pat in _COMPARISON_KEYWORDS)


@dataclass
class Instance:
    """One graded question-instance from the retroactive pool (one run of
    one question) -- the dry-run's stand-in for a "pipeline-verified
    answer" (README Stage 5 step 1). `is_known_failure` is ground truth
    pulled from the relevant RUNBOOK.md, not predicted."""

    question_id: str
    run: str
    text: str
    is_known_failure: bool


@dataclass
class RiskFlags:
    stage1_disambiguation: bool
    multi_part: bool
    comparison_shaped: bool

    @property
    def any_flag(self) -> bool:
        return self.stage1_disambiguation or self.multi_part or self.comparison_shaped

    @property
    def n_flags(self) -> int:
        return sum([self.stage1_disambiguation, self.multi_part, self.comparison_shaped])


def classify_risk(instance: Instance) -> RiskFlags:
    stage1 = run_stage1(instance.question_id, instance.text)
    stage2 = run_stage2(instance.question_id, instance.text)
    return RiskFlags(
        stage1_disambiguation=stage1.needs_disambiguation_check,
        multi_part=stage2.multi_part,
        comparison_shaped=_is_comparison_shaped(instance.text),
    )


@dataclass
class SampleResult:
    instances: List[Instance]
    n_known_failures: int

    @property
    def n_sampled(self) -> int:
        return len(self.instances)

    @property
    def hit_fraction_of_sample(self) -> float:
        return self.n_known_failures / len(self.instances) if self.instances else 0.0


def _known_failures(pool: Sequence[Instance]) -> List[Instance]:
    return [i for i in pool if i.is_known_failure]


def risk_weighted_sample(
    pool: Sequence[Instance],
    sample_size: int,
    rng: random.Random,
    baseline_fraction: float = 0.2,
) -> SampleResult:
    """README Stage 5 step 1: mostly the risk-flagged pool (ranked by how
    many of the three signals fire, ties broken by `rng` for
    reproducibility-with-a-seed rather than a fixed textual order), plus a
    smaller uniform-random baseline slice from whatever is left -- "covers
    shapes not yet hypothesized at all." Baseline is drawn from the
    remainder (not the full pool) so the partition doesn't double-count."""
    flagged = [(i, classify_risk(i)) for i in pool]
    risk_pool = [i for i, flags in flagged if flags.any_flag]
    remainder = [i for i, flags in flagged if not flags.any_flag]

    n_baseline = min(len(remainder), max(1, round(sample_size * baseline_fraction)))
    n_risk = sample_size - n_baseline

    if len(risk_pool) > n_risk:
        ranked = sorted(risk_pool, key=lambda i: (-classify_risk(i).n_flags, rng.random()))
        selected_risk = ranked[:n_risk]
    else:
        selected_risk = list(risk_pool)
        n_baseline = min(len(remainder), sample_size - len(selected_risk))

    selected_baseline = rng.sample(remainder, n_baseline) if n_baseline else []
    selected = selected_risk + selected_baseline

    if len(selected) < sample_size:
        leftover = [i for i in pool if i not in selected]
        top_up = rng.sample(leftover, min(sample_size - len(selected), len(leftover)))
        selected = selected + top_up

    return SampleResult(instances=selected, n_known_failures=sum(1 for i in selected if i.is_known_failure))


def uniform_random_sample(pool: Sequence[Instance], sample_size: int, rng: random.Random) -> SampleResult:
    selected = rng.sample(list(pool), min(sample_size, len(pool)))
    return SampleResult(instances=selected, n_known_failures=sum(1 for i in selected if i.is_known_failure))
