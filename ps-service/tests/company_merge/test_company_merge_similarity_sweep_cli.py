"""Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s CLI surface
(`main()`, issue #29, PLAN.md §1 Increment 5 / §4, CHANGES.md row M2).

Loads the script by path via `importlib.util.spec_from_file_location`, exactly as
`test_run_live_merge_cli.py` documents doing for `tools/company-merge/run_live_merge.py`
(same non-package-script-by-path pattern, same `sys.modules` pre-registration).

CHANGES.md row M2's binding correction: this file does NOT reuse the full 21-pair
`DEFAULT_DATASET` to test the CLI itself -- it defines its own small, purpose-built 3-pair
fixture, scored by a hand-scripted fake `call_embedding` covering only that fixture's
texts, passed to `main()` via `--dataset`. `DEFAULT_DATASET`'s own 21-pair shape (10 match
/ 11 non-match) is instead checked directly, as a separate, cheap assertion, never by
running the full dataset through a fake embedding fixture.
"""

from __future__ import annotations

import importlib.util
import json
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

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "company-merge"
    / "company_merge_similarity_sweep.py"
)
_MODULE_NAME = "_company_merge_similarity_sweep_cli_under_test"


@pytest.fixture(autouse=True)
def _preserve_atexit_registered_guard() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """`main()` calls the real `Logging.configure()` once it reaches the sweep itself -- see
    `test_run_live_merge_cli.py`'s identical fixture docstring for why this guard is needed
    to avoid poisoning `tests/logging/test_facade_emit_log_entry.py`'s atexit-registration
    test.
    """
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


def _load_cli_module() -> ModuleType:
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
def cli_module() -> ModuleType:
    return _load_cli_module()


class _ScriptedCallEmbedding:
    """Hand-written `EmbeddingCaller` fake scripted per input text -- mirrors
    `test_similarity_threshold_sweep_scoring.py`'s `_ScriptedCallEmbedding`. Records every
    `(model, text)` call so a test can assert the CLI passed through a config-resolved
    model string, never a hardcoded literal.
    """

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append((model, text))
        vector = self._vectors_by_text.get(text)
        if vector is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def _unreachable_call_embedding(
    *, model: str, inputs: list[str], timeout: float
) -> EmbeddingResponse:
    del model, inputs, timeout
    raise AssertionError("call_embedding must never be called on this code path")


def _stub_config_loader(embed_model: str | None) -> ServiceConfig:
    """A `ServiceConfig`-shaped stub, returned by a fake `config_loader` -- only
    `llm_interface_embed_model` matters for `_resolve_model`; every other field is a
    minimal, valid placeholder.
    """
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_embed_model=embed_model,
    )


def _write_small_dataset_fixture(path: Path) -> dict[str, list[float]]:
    """A small, purpose-built 3-pair fixture (CHANGES.md row M2) -- distinct from, and much
    smaller than, the real 21-pair `DEFAULT_DATASET`. Writes the fixture as the sweep
    script's `--dataset` JSON shape and returns the `vectors_by_text` mapping a hermetic
    fake `call_embedding` needs to cover every unique text this fixture references.
    """
    pairs: list[dict[str, object]] = [
        {
            "text_a": "Identity Verification and Unauthorised Access Prevention",
            "text_b": "Access Control & Authentication",
            "label": "match",
            "cited_node_ids": ["cap_access_control_authentication_151816"],
            "rationale": "small CLI-test fixture pair -- not drawn from DEFAULT_DATASET",
        },
        {
            "text_a": "Supply Chain Security Management",
            "text_b": "Supply-Chain Traceability Records",
            "label": "non_match",
            "cited_node_ids": [
                "cap_supply_chain_security_management_d1f111",
                "cap_supply_chain_traceability_records_fd07d2",
            ],
            "rationale": "small CLI-test fixture pair -- not drawn from DEFAULT_DATASET",
        },
        {
            "text_a": "alpha",
            "text_b": "beta",
            "label": "non_match",
            "cited_node_ids": [],
            "rationale": "small CLI-test fixture pair -- orthogonal fixture texts",
        },
    ]
    path.write_text(json.dumps(pairs), encoding="utf-8")
    return {
        "Identity Verification and Unauthorised Access Prevention": [1.0, 0.0, 0.0, 0.0],
        "Access Control & Authentication": [0.9, 0.1, 0.0, 0.0],
        "Supply Chain Security Management": [0.0, 1.0, 0.0, 0.0],
        "Supply-Chain Traceability Records": [0.0, 0.0, 1.0, 0.0],
        "alpha": [1.0, 0.0],
        "beta": [0.0, 1.0],
    }


# --- CHANGES.md row M2, part 1: small fixture, hand-scripted embedding, DI'd config ------


def test_cli_prints_one_row_per_threshold_and_uses_the_config_resolved_model(
    cli_module: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_path = tmp_path / "small_dataset.json"
    vectors_by_text = _write_small_dataset_fixture(dataset_path)
    call_embedding = _ScriptedCallEmbedding(vectors_by_text)
    recommendation_path = tmp_path / "SWEEP_RECOMMENDATION.md"

    def config_loader() -> ServiceConfig:
        return _stub_config_loader("stub-embed-model-xyz")

    exit_code = cli_module.main(
        [
            "--dataset",
            str(dataset_path),
            "--threshold-start",
            "0.70",
            "--threshold-stop",
            "0.72",
            "--threshold-step",
            "0.01",
            "--recommendation-path",
            str(recommendation_path),
        ],
        call_embedding=call_embedding,
        config_loader=config_loader,
    )

    assert exit_code == 0
    output_lines = capsys.readouterr().out.strip().splitlines()
    # 1 header row + 3 threshold rows (0.70, 0.71, 0.72), followed by the printed
    # recommendation block (PLAN.md §1 Increment 6) -- checked here only for the
    # per-threshold table's own shape; the recommendation block's own content is
    # covered by test_main_prints_the_recommendation_to_stdout_in_addition_to_the_table.
    table_rows = output_lines[:4]
    assert len(table_rows) == 4
    for expected_threshold in ("0.7000", "0.7100", "0.7200"):
        assert any(expected_threshold in line for line in table_rows[1:])
    # Every route_embedding call used the model resolved from the fake config_loader --
    # never a hardcoded literal (AC-BI-005).
    called_models = {model for model, _text in call_embedding.calls}
    assert called_models == {"stub-embed-model-xyz"}


def test_cli_explicit_model_flag_wins_over_config_loader(
    cli_module: ModuleType, tmp_path: Path
) -> None:
    dataset_path = tmp_path / "small_dataset.json"
    vectors_by_text = _write_small_dataset_fixture(dataset_path)
    call_embedding = _ScriptedCallEmbedding(vectors_by_text)
    recommendation_path = tmp_path / "SWEEP_RECOMMENDATION.md"

    def config_loader() -> ServiceConfig:
        return _stub_config_loader("should-never-be-used")

    exit_code = cli_module.main(
        [
            "--dataset",
            str(dataset_path),
            "--model",
            "explicit-cli-model",
            "--recommendation-path",
            str(recommendation_path),
        ],
        call_embedding=call_embedding,
        config_loader=config_loader,
    )

    assert exit_code == 0
    called_models = {model for model, _text in call_embedding.calls}
    assert called_models == {"explicit-cli-model"}


def test_cli_exits_1_when_model_unresolvable_and_never_calls_embedding(
    cli_module: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_path = tmp_path / "small_dataset.json"
    _write_small_dataset_fixture(dataset_path)

    def config_loader() -> ServiceConfig:
        return _stub_config_loader(None)

    exit_code = cli_module.main(
        ["--dataset", str(dataset_path)],
        call_embedding=_unreachable_call_embedding,
        config_loader=config_loader,
    )

    assert exit_code == 1
    assert "PS_LLMINTERFACE_EMBED_MODEL" in capsys.readouterr().err


# --- CHANGES.md row M2, part 2: --dataset's default is the built-in 21-pair dataset ------
# (checked by length/composition directly -- never by re-scoring it through a fake).


def test_default_dataset_is_the_built_in_21_pair_dataset(cli_module: ModuleType) -> None:
    default_dataset = cli_module.DEFAULT_DATASET

    assert len(default_dataset) == 21
    assert sum(1 for pair in default_dataset if pair.label == "match") == 10
    assert sum(1 for pair in default_dataset if pair.label == "non_match") == 11


def test_dataset_flag_defaults_to_none_selecting_the_built_in_dataset(
    cli_module: ModuleType,
) -> None:
    """The `--dataset` flag itself defaults to `None` -- `main()`'s own body (read
    directly, not re-exercised here per CHANGES.md row M2) uses `DEFAULT_DATASET`
    whenever `args.dataset is None`. Checked at the argparse level, cheaply, instead of
    re-running the full dataset through a fake embedding fixture.
    """
    args = cli_module._parse_args([])  # pyright: ignore[reportPrivateUsage] -- internal parser under test

    assert args.dataset is None


# --- PLAN.md §1 Increment 6, AC-BI-009: main() writes the recommendation file ----------


def test_main_writes_the_recommendation_file_at_the_given_recommendation_path(
    cli_module: ModuleType, tmp_path: Path
) -> None:
    """Uses `tmp_path`, never the real tracker path -- `--recommendation-path` is a real
    CLI flag precisely so a test never touches
    `.orchestrator/tracker/issue-29-similarity-threshold/SWEEP_RECOMMENDATION.md`.

    The small 3-pair fixture's true-match pair (`"Identity Verification..."` vs.
    `"Access Control & Authentication"`) scores ~0.994 cosine similarity -- at or above
    every threshold in `0.70`-`0.72`; both non-match pairs score `0.0` -- below every
    threshold in that range. So every swept threshold ties at precision=recall=f1=1.0,
    and `select_recommendation`'s higher-threshold-on-tie rule (CHANGES.md row m3)
    deterministically picks the highest threshold actually swept: `0.72`.
    """
    dataset_path = tmp_path / "small_dataset.json"
    vectors_by_text = _write_small_dataset_fixture(dataset_path)
    call_embedding = _ScriptedCallEmbedding(vectors_by_text)
    recommendation_path = tmp_path / "recommendation" / "SWEEP_RECOMMENDATION.md"

    def config_loader() -> ServiceConfig:
        return _stub_config_loader("stub-embed-model-xyz")

    exit_code = cli_module.main(
        [
            "--dataset",
            str(dataset_path),
            "--threshold-start",
            "0.70",
            "--threshold-stop",
            "0.72",
            "--threshold-step",
            "0.01",
            "--recommendation-path",
            str(recommendation_path),
        ],
        call_embedding=call_embedding,
        config_loader=config_loader,
    )

    assert exit_code == 0
    assert recommendation_path.exists()
    written = recommendation_path.read_text(encoding="utf-8")
    assert "0.720" in written
    assert "1.000" in written  # precision/recall/f1 all 1.0 at the winning threshold


def test_main_prints_the_recommendation_to_stdout_in_addition_to_the_table(
    cli_module: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_path = tmp_path / "small_dataset.json"
    vectors_by_text = _write_small_dataset_fixture(dataset_path)
    call_embedding = _ScriptedCallEmbedding(vectors_by_text)
    recommendation_path = tmp_path / "SWEEP_RECOMMENDATION.md"

    def config_loader() -> ServiceConfig:
        return _stub_config_loader("stub-embed-model-xyz")

    exit_code = cli_module.main(
        [
            "--dataset",
            str(dataset_path),
            "--threshold-start",
            "0.70",
            "--threshold-stop",
            "0.72",
            "--threshold-step",
            "0.01",
            "--recommendation-path",
            str(recommendation_path),
        ],
        call_embedding=call_embedding,
        config_loader=config_loader,
    )

    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert "Recommended threshold" in stdout
    assert "0.720" in stdout
