"""Tests for the review-band half of `tools/company-merge/company_merge_similarity_sweep.py`.

The sweep scores every labelled pair once and reports, for each candidate review floor, how
many true duplicates the floor surfaces (`floor <= score < merge`), how many distinct pairs
it surfaces as noise, and how many true duplicates it hides (`score < floor`). The `--floor`
option answers whether one chosen floor keeps every labelled true duplicate visible.

Hermetic: the script is loaded by path and every embedding comes from a scripted fake
`call_embedding`; nothing reaches a provider.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.config import ServiceConfig
from ps_service.logging import facade

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "tools" / "company-merge" / "company_merge_similarity_sweep.py"
_VALUES_PATH = _REPO_ROOT / "charts" / "policy-system" / "values.yaml"
_MODULE_NAME = "_company_merge_similarity_sweep_review_band_under_test"


@pytest.fixture(autouse=True)
def _preserve_atexit_registered_guard() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """`main()` configures the real logging facade; restore it so the facade's own
    atexit-registration test is not poisoned (same guard as the sibling CLI test file).
    """
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def sweep() -> ModuleType:
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


class _Scripted:
    """`EmbeddingCaller` fake returning a scripted vector per input text."""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = vectors_by_text

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del timeout
        vector = self._vectors_by_text[inputs[0]]
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def _config(embed_model: str | None = "stub-embed") -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_embed_model=embed_model,
    )


# Cosines are exact: text_a is [1, 0], so cos == first component / norm of text_b, and the
# norms below are 5, 5 and 25.
_PAIR_VECTORS: dict[str, tuple[str, list[float], str]] = {
    "match-high": ("match", [4.0, 3.0], "0.8"),
    "match-low": ("match", [3.0, 4.0], "0.6"),
    "distinct-noise": ("non_match", [7.0, 24.0], "0.28"),
}


def _write_dataset(path: Path) -> dict[str, list[float]]:
    entries: list[dict[str, object]] = []
    vectors: dict[str, list[float]] = {}
    for name, (label, vector_b, _score) in _PAIR_VECTORS.items():
        entries.append(
            {
                "text_a": f"{name}-a",
                "text_b": f"{name}-b",
                "label": label,
                "cited_node_ids": [],
                "rationale": "scripted review-band fixture",
            }
        )
        vectors[f"{name}-a"] = [1.0, 0.0]
        vectors[f"{name}-b"] = vector_b
    path.write_text(json.dumps(entries), encoding="utf-8")
    return vectors


def _run(
    sweep: ModuleType, tmp_path: Path, *extra: str
) -> tuple[int, Path]:  # (exit code, recommendation path)
    dataset = tmp_path / "dataset.json"
    vectors = _write_dataset(dataset)
    recommendation = tmp_path / "rec.md"
    code = sweep.main(
        [
            "--dataset",
            str(dataset),
            "--threshold-start",
            "0.70",
            "--threshold-stop",
            "0.71",
            "--recommendation-path",
            str(recommendation),
            *extra,
        ],
        call_embedding=_Scripted(vectors),
        config_loader=_config,
    )
    return code, recommendation


def _scored(sweep: ModuleType, label: str, score: float) -> object:
    pair = sweep.SimilarityPair(
        text_a="a", text_b="b", label=label, cited_node_ids=(), rationale="r"
    )
    return sweep.ScoredPair(pair=pair, score=score)


def _band_fixture(sweep: ModuleType) -> list[object]:
    return [
        _scored(sweep, "match", 0.95),
        _scored(sweep, "match", 0.90),  # exactly merge: auto-merged, not in the band
        _scored(sweep, "match", 0.70),
        _scored(sweep, "match", 0.55),
        _scored(sweep, "non_match", 0.60),
        _scored(sweep, "non_match", 0.55),
        _scored(sweep, "non_match", 0.40),
    ]


# --- score_dataset ----------------------------------------------------------------------


def test_score_dataset_returns_one_scored_pair_per_input_pair_in_order(
    sweep: ModuleType,
) -> None:
    pairs = tuple(
        sweep.SimilarityPair(
            text_a=f"{name}-a",
            text_b=f"{name}-b",
            label=label,
            cited_node_ids=(),
            rationale="r",
        )
        for name, (label, _vector, _score) in _PAIR_VECTORS.items()
    )
    vectors: dict[str, list[float]] = {}
    for name, (_label, vector_b, _score) in _PAIR_VECTORS.items():
        vectors[f"{name}-a"] = [1.0, 0.0]
        vectors[f"{name}-b"] = vector_b
    facade_configure = sys.modules[_MODULE_NAME].configure_logging
    facade_configure()

    scored = sweep.score_dataset(pairs, model="m", call_embedding=_Scripted(vectors))

    assert [item.pair for item in scored] == list(pairs)
    assert [item.score for item in scored] == [0.8, 0.6, 0.28]


# --- review_band_report -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("floor", "surfaced_matches", "surfaced_distinct", "hidden_matches"),
    [
        (0.30, 2, 3, 0),
        (0.50, 2, 2, 0),
        (0.55, 2, 2, 0),  # a pair scoring exactly the floor is surfaced
        (0.60, 1, 1, 1),  # 0.55 now hidden, the 0.60 non-match still surfaced
        (0.90, 0, 0, 2),  # floor == merge: the band is empty
    ],
)
def test_review_band_report_counts_pairs_per_floor_with_inclusive_floor_exclusive_merge(
    sweep: ModuleType,
    floor: float,
    surfaced_matches: int,
    surfaced_distinct: int,
    hidden_matches: int,
) -> None:
    report = sweep.review_band_report(_band_fixture(sweep), [floor], 0.90)

    assert len(report) == 1
    row = report[0]
    assert row.floor == floor
    assert row.merge_threshold == 0.90
    assert row.surfaced_true_matches == surfaced_matches
    assert row.surfaced_distinct_pairs == surfaced_distinct
    assert row.hidden_true_matches == hidden_matches
    # Matches below the merge threshold are independent of the floor: 0.70 and 0.55.
    assert row.matches_missed_by_merge == 2


def test_review_band_report_returns_one_row_per_floor_in_input_order(sweep: ModuleType) -> None:
    report = sweep.review_band_report(_band_fixture(sweep), [0.6, 0.3, 0.5], 0.90)

    assert [row.floor for row in report] == [0.6, 0.3, 0.5]


# --- CLI: per-pair scores, floor table, extremes ------------------------------------------


def test_cli_prints_every_pair_score_sorted_highest_first(
    sweep: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(sweep, tmp_path, "--merge-threshold", "0.9")

    assert code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if re.search(r"-a\b.*-b\b", line)]
    assert [line.split()[0] for line in lines] == ["0.8000", "0.6000", "0.2800"]
    assert "match-low-a" in lines[1]
    assert "non_match" in lines[2]


def test_cli_prints_floor_table_and_score_extremes(
    sweep: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(
        sweep,
        tmp_path,
        "--merge-threshold",
        "0.9",
        "--floor-start",
        "0.50",
        "--floor-stop",
        "0.70",
        "--floor-step",
        "0.10",
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "floor  surfaced_matches  surfaced_distinct  hidden_matches  missed_by_merge" in out
    floor_rows = [
        line.split()[:4] for line in out.splitlines() if re.match(r"0\.[567]0\s+\d", line)
    ]
    assert floor_rows == [
        ["0.50", "2", "0", "0"],
        ["0.60", "2", "0", "0"],
        ["0.70", "1", "0", "1"],
    ]
    assert "lowest true-match score: 0.6000" in out
    assert "highest true-match score: 0.8000" in out
    assert "lowest non-match score: 0.2800" in out
    assert "highest non-match score: 0.2800" in out


def test_cli_floor_range_defaults_to_start_030_and_stop_merge(
    sweep: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(sweep, tmp_path, "--merge-threshold", "0.32")

    assert code == 0
    floors = [
        line.split()[0]
        for line in capsys.readouterr().out.splitlines()
        if re.match(r"0\.3\d(\s+\d+){4}$", line)
    ]
    assert floors == ["0.30", "0.31", "0.32"]


# --- merge threshold default and source -------------------------------------------------


def test_merge_threshold_defaults_to_the_chart_constant_and_prints_the_source(
    sweep: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(sweep, tmp_path)

    assert code == 0
    assert sweep.DEFAULT_MERGE_THRESHOLD == 0.59
    assert "merge threshold 0.59 (source: default)" in capsys.readouterr().out


def test_merge_threshold_flag_overrides_the_default_and_prints_its_source(
    sweep: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(sweep, tmp_path, "--merge-threshold", "0.8")

    assert code == 0
    assert "merge threshold 0.8 (source: --merge-threshold)" in capsys.readouterr().out


def test_default_merge_threshold_equals_the_chart_capability_merge_threshold(
    sweep: ModuleType,
) -> None:
    match = re.search(
        r"^\s*capabilityMergeThreshold:\s*([0-9.]+)\s*$", _VALUES_PATH.read_text(), re.MULTILINE
    )

    assert match is not None
    assert float(match.group(1)) == sweep.DEFAULT_MERGE_THRESHOLD


# --- --floor PASS/FAIL ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("floor", "verdict", "exit_code"),
    [
        ("0.5", "PASS", 0),
        ("0.6", "PASS", 0),  # exactly the lowest true-match score
        ("0.61", "FAIL", 1),
    ],
)
def test_floor_option_passes_only_at_or_below_the_lowest_true_match_score(
    sweep: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    floor: str,
    verdict: str,
    exit_code: int,
) -> None:
    code, _ = _run(sweep, tmp_path, "--merge-threshold", "0.9", "--floor", floor)

    assert code == exit_code
    out = capsys.readouterr().out
    assert f"floor {floor}: {verdict}" in out
    assert "lowest true-match score 0.6000" in out


def test_floor_option_fails_a_floor_above_the_merge_threshold(
    sweep: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(sweep, tmp_path, "--merge-threshold", "0.5", "--floor", "0.55")

    assert code == 1
    assert "floor 0.55: FAIL" in capsys.readouterr().out


def test_floor_option_is_absent_by_default_and_prints_no_verdict(
    sweep: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(sweep, tmp_path)

    assert code == 0
    assert "PASS" not in capsys.readouterr().out


# --- Markdown -----------------------------------------------------------------------


def test_recommendation_markdown_has_a_review_band_section(
    sweep: ModuleType, tmp_path: Path
) -> None:
    code, recommendation = _run(
        sweep, tmp_path, "--merge-threshold", "0.9", "--floor-start", "0.5", "--floor-stop", "0.6"
    )

    assert code == 0
    text = recommendation.read_text(encoding="utf-8")
    assert "## Review band" in text
    assert "merge threshold 0.9 (source: --merge-threshold)" in text
    assert (
        "| floor | surfaced true matches | surfaced distinct pairs | hidden true matches |" in text
    )
    assert "| 0.50 | 2 | 0 | 0 |" in text
    assert "Lowest true-match score: 0.6000" in text


def test_render_recommendation_without_a_review_band_is_unchanged(sweep: ModuleType) -> None:
    result = sweep.ThresholdResult(
        threshold=0.8,
        precision=1.0,
        recall=1.0,
        f1=1.0,
        true_positive=1,
        false_positive=0,
        true_negative=1,
        false_negative=0,
    )

    assert "Review band" not in sweep.render_recommendation(result)


# --- docstring and --help ------------------------------------------------------------


def test_help_documents_the_review_band_options(
    sweep: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        sweep.main(["--help"])

    out = capsys.readouterr().out
    for fragment in (
        "--merge-threshold",
        "--floor-start",
        "--floor-stop",
        "--floor-step",
        "--floor ",
        "PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD",
        "review band",
    ):
        assert fragment in out
