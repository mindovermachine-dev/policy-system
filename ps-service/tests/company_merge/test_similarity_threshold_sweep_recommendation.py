"""Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s recommendation
writer (issue #29, PLAN.md §1 Increment 6, AC-BI-009, CHANGES.md row m3's accepted
higher-threshold F1 tie-break).

Loads the script by path via `importlib.util.spec_from_file_location`, exactly as
`test_similarity_threshold_sweep_scoring.py`/`test_similarity_threshold_dataset.py`
already do for this same script.

Covers, hermetically (hand-constructed `ThresholdResult`s, no embedding calls):

- `select_recommendation` picks the unambiguous highest-`f1` entry when there is one.
- `select_recommendation`'s tie-break rule (CHANGES.md row m3): on an F1 tie, the
  HIGHER threshold wins -- proven with a deliberate tie case, not merely documented.
- `select_recommendation` raises on an empty `results` list -- nothing to recommend.
- `render_recommendation`'s output string names the exact recommended threshold value
  and all three metrics (precision/recall/f1), each formatted to 3 decimal places, plus
  the TP/FP/TN/FN counts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "company-merge"
    / "company_merge_similarity_sweep.py"
)
_MODULE_NAME = "_company_merge_similarity_sweep_recommendation_under_test"


def _load_sweep_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MODULE_NAME]
        raise
    return module


@pytest.fixture
def sweep_module() -> ModuleType:
    return _load_sweep_module()


# --- select_recommendation: unambiguous best-F1 case -----------------------------------


def test_select_recommendation_picks_the_unambiguous_highest_f1_entry(
    sweep_module: ModuleType,
) -> None:
    low_f1 = sweep_module.ThresholdResult(
        threshold=0.70,
        precision=0.5,
        recall=0.5,
        f1=0.5,
        true_positive=5,
        false_positive=5,
        true_negative=5,
        false_negative=5,
    )
    best_f1 = sweep_module.ThresholdResult(
        threshold=0.85,
        precision=0.9,
        recall=0.9,
        f1=0.9,
        true_positive=9,
        false_positive=1,
        true_negative=9,
        false_negative=1,
    )
    mid_f1 = sweep_module.ThresholdResult(
        threshold=0.90,
        precision=0.8,
        recall=0.7,
        f1=0.7466666666666667,
        true_positive=7,
        false_positive=2,
        true_negative=8,
        false_negative=3,
    )

    picked = sweep_module.select_recommendation([low_f1, best_f1, mid_f1])

    assert picked is best_f1


# --- select_recommendation: tie-break rule (CHANGES.md row m3) -------------------------


def test_select_recommendation_breaks_an_f1_tie_toward_the_higher_threshold(
    sweep_module: ModuleType,
) -> None:
    """Two thresholds tie exactly on F1 -- the rule (documented in
    `select_recommendation`'s own docstring) is to prefer the HIGHER threshold, biasing
    the recommendation toward precision (fewer false Capability merges).
    """
    lower_threshold_tie = sweep_module.ThresholdResult(
        threshold=0.80,
        precision=0.75,
        recall=0.75,
        f1=0.75,
        true_positive=6,
        false_positive=2,
        true_negative=8,
        false_negative=2,
    )
    higher_threshold_tie = sweep_module.ThresholdResult(
        threshold=0.88,
        precision=0.75,
        recall=0.75,
        f1=0.75,
        true_positive=6,
        false_positive=2,
        true_negative=8,
        false_negative=2,
    )

    picked = sweep_module.select_recommendation([lower_threshold_tie, higher_threshold_tie])

    assert picked is higher_threshold_tie


def test_select_recommendation_tie_break_is_order_independent(sweep_module: ModuleType) -> None:
    """The higher-threshold-on-tie rule must not be an accident of input ordering --
    reversing the list from the test above must still pick the higher threshold.
    """
    lower_threshold_tie = sweep_module.ThresholdResult(
        threshold=0.80,
        precision=0.75,
        recall=0.75,
        f1=0.75,
        true_positive=6,
        false_positive=2,
        true_negative=8,
        false_negative=2,
    )
    higher_threshold_tie = sweep_module.ThresholdResult(
        threshold=0.88,
        precision=0.75,
        recall=0.75,
        f1=0.75,
        true_positive=6,
        false_positive=2,
        true_negative=8,
        false_negative=2,
    )

    picked = sweep_module.select_recommendation([higher_threshold_tie, lower_threshold_tie])

    assert picked is higher_threshold_tie


def test_select_recommendation_raises_on_empty_results(sweep_module: ModuleType) -> None:
    with pytest.raises(ValueError, match="at least one"):
        sweep_module.select_recommendation([])


# --- render_recommendation: exact threshold + all three metrics, fixed precision -------


def test_render_recommendation_contains_the_exact_threshold_and_all_three_metrics(
    sweep_module: ModuleType,
) -> None:
    result = sweep_module.ThresholdResult(
        threshold=0.87,
        precision=0.923456,
        recall=0.815678,
        f1=0.866123,
        true_positive=9,
        false_positive=1,
        true_negative=10,
        false_negative=2,
    )

    rendered = sweep_module.render_recommendation(result)

    assert "0.870" in rendered
    assert "0.923" in rendered
    assert "0.816" in rendered
    assert "0.866" in rendered
    assert "9" in rendered
    assert "True positive: 9" in rendered
    assert "False positive: 1" in rendered
    assert "True negative: 10" in rendered
    assert "False negative: 2" in rendered


def test_render_recommendation_returns_a_short_markdown_block(sweep_module: ModuleType) -> None:
    """Sanity check on the "short Markdown block" shape PLAN.md/AC-BI-009 call for --
    a Markdown heading and a bounded line count, not an essay.
    """
    result = sweep_module.ThresholdResult(
        threshold=0.85,
        precision=1.0,
        recall=1.0,
        f1=1.0,
        true_positive=10,
        false_positive=0,
        true_negative=11,
        false_negative=0,
    )

    rendered = sweep_module.render_recommendation(result)

    assert rendered.startswith("#")
    assert len(rendered.splitlines()) <= 20
