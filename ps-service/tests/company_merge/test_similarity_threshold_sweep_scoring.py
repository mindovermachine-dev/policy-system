"""Tests for `tools/company-merge/company_merge_similarity_sweep.py` (issue #29, PLAN.md
§1 Increments 1-4, CHANGES.md row M3's path, row m2's AST import-scan test, and row M1's
hermetic full-dataset sweep proof).

Loads the script by path via `importlib.util.spec_from_file_location`, exactly as
`test_run_live_merge_cli.py` documents doing for `tools/company-merge/run_live_merge.py`
(same non-package-script-by-path pattern, same `sys.modules` pre-registration).

Covers, hermetically (a hand-scripted fake `EmbeddingCaller`, zero network/FalkorDB):

- `score_pair` returns a value in `[-1.0, 1.0]` actually computed by the real
  `ps_service.company_merge.similarity.cosine_similarity` (AC-BI-004) -- proven both by
  an independent manual recomputation of the expected cosine similarity, and by an
  AST scan (mirrors `test_identity_reuse.py`'s convention) showing no dot-product/norm
  math is defined inside the sweep script itself, plus a direct object-identity check
  that the imported `cosine_similarity` is the real function, not a lookalike.
- `route_embedding` is called with `model=` equal to the value passed to `score_pair`,
  never a hardcoded literal (AC-BI-005).
- Every import in the sweep script is stdlib or `ps_service.*` -- no new third-party
  dependency (AC-BI-006, CHANGES.md row m2).
- `build_dataset()`'s AC-BI-003 exclusion mechanism, proven at corpus scale: a
  mechanical guard raises on a violating pair, computed against all 90 real Capability
  nodes -- not merely omitted by hand.
- `sweep_dataset()` fetches each unique text's embedding at most once across every pair
  AND every threshold (AC-BI-007, PLAN.md §1 Increment 2).
- `sweep_dataset()` lets a `LlmProviderError` from any `route_embedding` call propagate
  unchanged, aborting before any `ThresholdResult` is produced for any threshold
  (AC-BI-008, PLAN.md §1 Increment 3).
- CHANGES.md row M1: `sweep_dataset()` scores the complete, real 21-pair dataset
  cleanly across PLAN.md's default 0.70-0.95 step 0.01 threshold range, using a fully
  scripted fake `call_embedding` keyed by CHANGES.md row M1's exact deterministic
  SHA-256-based vector scheme -- before any CLI or live/network dependency exists.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import openai
import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.similarity import cosine_similarity as real_cosine_similarity
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from types import ModuleType

_TOOLS_COMPANY_MERGE_DIR = Path(__file__).resolve().parents[3] / "tools" / "company-merge"
_SCRIPT_PATH = _TOOLS_COMPANY_MERGE_DIR / "company_merge_similarity_sweep.py"
_MODULE_NAME = "_company_merge_similarity_sweep_under_test"

# Mirrors `test_identity_reuse.py`'s forbidden-function-definition scan: none of these
# names may ever be *defined* inside the sweep script -- cosine/dot-product/norm math
# belongs only in `ps_service.company_merge.similarity.cosine_similarity`.
_FORBIDDEN_SCORING_FUNCTION_NAMES = frozenset(
    {
        "cosine_similarity",
        "_cosine_similarity",
        "dot_product",
        "_dot_product",
        "magnitude",
        "_magnitude",
        "norm",
        "_norm",
    }
)


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


@pytest.fixture
def sweep_source() -> str:
    return _SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _configure_logging_for_route_embedding() -> None:  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    """`route_embedding` logs through the Logging component, which requires a configured
    default emitter before first use outside of `main.py`'s normal process startup
    (CONTRIBUTING.md's own "Sanity-check via the public API" snippet; PLAN.md §0 F5).
    The root conftest's `_isolate_logging` autouse fixture already points
    `PS_LOGGING_DIR` at a per-test `tmp_path` and resets the facade around each test, so
    calling this unconditionally here is safe and never touches the real log file.
    """
    configure_logging()


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text` -- mirrors
    `test_dedup_abort_on_embedding_failure.py`'s `_ScriptedCallEmbedding` pattern.
    Also records the `model` each call was made with, so a test can assert `score_pair`
    passed through the caller-supplied model rather than a hardcoded literal.
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


# --- score_pair: real cosine_similarity, real route_embedding model passthrough --------


def test_score_pair_returns_the_real_cosine_similarity_within_valid_range(
    sweep_module: ModuleType,
) -> None:
    vector_a = [1.0, 2.0, 3.0]
    vector_b = [4.0, 5.0, 6.0]
    call_embedding = _ScriptedCallEmbedding({"text one": vector_a, "text two": vector_b})
    pair = sweep_module.SimilarityPair(
        text_a="text one",
        text_b="text two",
        label="match",
        cited_node_ids=("cap_fixture",),
        rationale="test fixture -- not drawn from the real dataset",
    )

    score = sweep_module.score_pair(pair, model="fake-embed-model", call_embedding=call_embedding)

    # Independent recomputation (not calling cosine_similarity itself), proving score_pair
    # used the *real* function's actual output rather than some other value.
    dot_product = sum(a * b for a, b in zip(vector_a, vector_b, strict=True))
    magnitude_a = sum(a * a for a in vector_a) ** 0.5
    magnitude_b = sum(b * b for b in vector_b) ** 0.5
    expected = dot_product / (magnitude_a * magnitude_b)

    assert score == pytest.approx(expected)
    assert -1.0 <= score <= 1.0


def test_score_pair_calls_route_embedding_with_the_given_model_not_a_hardcoded_literal(
    sweep_module: ModuleType,
) -> None:
    call_embedding = _ScriptedCallEmbedding({"text one": [1.0, 0.0], "text two": [0.0, 1.0]})
    pair = sweep_module.SimilarityPair(
        text_a="text one",
        text_b="text two",
        label="non_match",
        cited_node_ids=(),
        rationale="test fixture -- not drawn from the real dataset",
    )

    sweep_module.score_pair(
        pair, model="a-distinctive-fixture-model-id", call_embedding=call_embedding
    )

    assert call_embedding.calls == [
        ("a-distinctive-fixture-model-id", "text one"),
        ("a-distinctive-fixture-model-id", "text two"),
    ]


# --- AC-BI-004: no reimplementation of cosine/dot-product/norm math -------------------


def test_no_similarity_math_is_defined_inside_the_sweep_script(sweep_source: str) -> None:
    tree = ast.parse(sweep_source, filename=str(_SCRIPT_PATH))
    violations = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _FORBIDDEN_SCORING_FUNCTION_NAMES
    ]
    assert violations == []


def test_forbidden_scoring_function_scan_flags_a_hypothetical_reimplementation() -> None:
    """Positive case: proves the scan actually catches a reimplementation, not just an
    absence of any function at all -- mirrors `test_identity_reuse.py`'s own positive case.
    """
    tree = ast.parse("def cosine_similarity(a, b):\n    return 0.0\n")

    violations = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _FORBIDDEN_SCORING_FUNCTION_NAMES
    ]

    assert violations == ["cosine_similarity"]


def test_sweep_script_reexports_the_same_cosine_similarity_function_object(
    sweep_module: ModuleType,
) -> None:
    """Byte-for-byte identity, not just behavioral equality: the sweep script imports
    (never wraps/reimplements) `ps_service.company_merge.similarity`'s own function
    object -- mirrors `test_identity_reuse.py`'s identical convention.
    """
    assert sweep_module.cosine_similarity is real_cosine_similarity


# --- AC-BI-006 / CHANGES.md row m2: no new third-party dependency ----------------------


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def test_every_import_in_the_sweep_script_is_stdlib_or_ps_service(sweep_source: str) -> None:
    tree = ast.parse(sweep_source, filename=str(_SCRIPT_PATH))
    imported_module_names = _imported_module_names(tree)
    assert imported_module_names, "expected at least one import to scan"
    for module_name in imported_module_names:
        top_level_package = module_name.split(".")[0]
        assert top_level_package in sys.stdlib_module_names or module_name.startswith(
            "ps_service."
        ), (
            f"disallowed non-stdlib, non-ps_service import: {module_name!r} -- AC-BI-006 "
            "forbids a new third-party dependency in the sweep script"
        )


def test_import_scan_flags_a_hypothetical_third_party_import() -> None:
    """Positive case: proves the import scan actually catches a disallowed import."""
    tree = ast.parse("import numpy\n")

    imported_module_names = _imported_module_names(tree)

    assert imported_module_names == ["numpy"]
    assert "numpy" not in sys.stdlib_module_names


# --- AC-BI-003: identical-Capability-name pair mechanically excluded ------------------


def test_build_dataset_returns_no_pair_with_identical_text_a_and_text_b(
    sweep_module: ModuleType,
) -> None:
    """Increment 4's full dataset (PLAN.md §3): every pair carries a non-empty
    rationale/cited_node_ids and never scores a Capability name against itself. Length
    and per-node-id coverage are proven at corpus scale by
    `test_similarity_threshold_dataset.py`, not re-asserted here.
    """
    pairs = sweep_module.build_dataset()

    assert all(pair.text_a != pair.text_b for pair in pairs)
    assert all(pair.rationale for pair in pairs)
    assert all(pair.cited_node_ids for pair in pairs)


def test_assert_dataset_excludes_real_identical_name_pairs_raises_on_a_violation(
    sweep_module: ModuleType,
) -> None:
    """Proves the AC-BI-003 exclusion guard `build_dataset()` runs is a real mechanical
    check that actively raises on a violation, not merely a check that happens to pass
    because the shipped dataset was curated correctly by hand.
    """
    violating_pair = sweep_module.SimilarityPair(
        text_a="Data Encryption",
        text_b="Data Encryption",
        label="match",
        cited_node_ids=("cap_data_encryption_0e50d3",),
        rationale="synthetic violation for this test -- not part of the shipped dataset",
    )

    with pytest.raises(sweep_module.SweepDatasetValidationError):
        sweep_module._assert_dataset_excludes_real_identical_name_pairs(  # pyright: ignore[reportPrivateUsage] -- internal guard under test
            (violating_pair,), frozenset({"Data Encryption"})
        )


def test_assert_dataset_excludes_real_identical_name_pairs_passes_the_real_dataset(
    sweep_module: ModuleType,
) -> None:
    """The guard, run against the real 90-node corpus, raises nothing for the shipped
    dataset -- `build_dataset()` itself already proves this by not raising, but this
    test isolates the guard call so a future regression here fails loudly and
    specifically, rather than only failing indirectly via `build_dataset()`.
    """
    pairs = sweep_module.build_dataset()
    repeated_names = sweep_module._repeated_capability_names_from_corpus()  # pyright: ignore[reportPrivateUsage] -- internal helper under test

    sweep_module._assert_dataset_excludes_real_identical_name_pairs(  # pyright: ignore[reportPrivateUsage] -- internal guard under test
        pairs, repeated_names
    )


def test_repeated_capability_names_from_corpus_matches_the_known_19_groups(
    sweep_module: ModuleType,
) -> None:
    """PLAN.md §3.3 claims 19 Capability names are shared verbatim across two or more
    of the 90 real nodes -- independently re-confirmed for this dispatch by scanning
    all 90 nodes directly. Spot-checks a few of the 19 by name.
    """
    repeated_names = sweep_module._repeated_capability_names_from_corpus()  # pyright: ignore[reportPrivateUsage] -- internal helper under test

    assert len(repeated_names) == 19
    assert "Data Encryption" in repeated_names
    assert "Access Control & Authentication" in repeated_names
    assert "Incident Handling" in repeated_names
    assert "Vulnerability Management" in repeated_names
    assert "Coordinated Vulnerability Disclosure Policy" in repeated_names


def test_is_identical_name_pair_flags_the_ac_bi_003_demonstration_pair(
    sweep_module: ModuleType,
) -> None:
    is_identical_name_pair = (
        sweep_module._is_identical_name_pair  # pyright: ignore[reportPrivateUsage] -- internal helper under test
    )
    assert is_identical_name_pair("Data Encryption", "Data Encryption") is True


def test_is_identical_name_pair_does_not_flag_a_genuine_non_match_pair(
    sweep_module: ModuleType,
) -> None:
    is_identical_name_pair = (
        sweep_module._is_identical_name_pair  # pyright: ignore[reportPrivateUsage] -- internal helper under test
    )
    assert (
        is_identical_name_pair(
            "Supply Chain Security Management", "Supply-Chain Traceability Records"
        )
        is False
    )


# --- AC-BI-007: embedding cache -- each unique text fetched at most once --------------


class _OnceOnlyCallEmbedding:
    """A hand-written `EmbeddingCaller` fake that raises `AssertionError` if it is ever
    invoked twice for the same input text -- mirrors `_ScriptedCallEmbedding`'s "no
    scripted response" failure shape (`test_dedup_abort_on_embedding_failure.py:73-74`)
    but flips the assertion around: here, a *repeated* real call is itself the bug being
    guarded against, proving `sweep_dataset`'s embedding cache is actually hit on reuse
    rather than merely counted.
    """

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self._already_called_texts: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        if text in self._already_called_texts:
            raise AssertionError(
                f"text {text!r} was fetched via route_embedding more than once -- the "
                "embedding cache should have reused the first result (AC-BI-007)"
            )
        self._already_called_texts.add(text)
        self.calls.append(text)
        vector = self._vectors_by_text.get(text)
        if vector is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def test_sweep_dataset_fetches_each_unique_text_at_most_once_across_pairs_and_thresholds(
    sweep_module: ModuleType,
) -> None:
    """Two pairs share one text (`"Access Control & Authentication"`, reused from
    Increment 1's own true-match seed pair as this pair's `text_b`, then again as a
    second pair's `text_a`) and the sweep is run over three thresholds -- a real
    duplication across both pairs AND thresholds, not a call-count mock.
    """
    shared_text = "Access Control & Authentication"
    pair_one = sweep_module.SimilarityPair(
        text_a="Identity Verification and Unauthorised Access Prevention",
        text_b=shared_text,
        label="match",
        cited_node_ids=("cap_access_control_authentication_151816",),
        rationale="reused from Increment 1's own seed true-match pair",
    )
    pair_two = sweep_module.SimilarityPair(
        text_a=shared_text,
        text_b="Supply-Chain Traceability Records",
        label="non_match",
        cited_node_ids=(
            "cap_access_control_authentication_151816",
            "cap_supply_chain_traceability_records_fd07d2",
        ),
        rationale="reuses the same real node's name as pair_one's text_b -- proves the "
        "embedding cache is hit, not re-fetched",
    )
    call_embedding = _OnceOnlyCallEmbedding(
        {
            "Identity Verification and Unauthorised Access Prevention": [1.0, 0.0, 0.0],
            shared_text: [0.0, 1.0, 0.0],
            "Supply-Chain Traceability Records": [0.0, 0.0, 1.0],
        }
    )

    results = sweep_module.sweep_dataset(
        (pair_one, pair_two),
        [0.70, 0.85, 0.95],
        model="fake-embed-model",
        call_embedding=call_embedding,
    )

    assert len(results) == 3
    assert {result.threshold for result in results} == {0.70, 0.85, 0.95}
    # 3 unique texts total across both pairs -- each fetched exactly once, regardless of
    # being referenced twice (shared_text) and swept over 3 thresholds.
    assert sorted(call_embedding.calls) == sorted(
        [
            "Identity Verification and Unauthorised Access Prevention",
            shared_text,
            "Supply-Chain Traceability Records",
        ]
    )


def test_sweep_dataset_result_metrics_are_computed_correctly_at_each_threshold(
    sweep_module: ModuleType,
) -> None:
    """Orthogonal vectors score 0.0 (below every threshold); identical vectors score 1.0
    (at or above every threshold) -- a hand-computable confusion matrix at two very
    different thresholds proves precision/recall/F1 are computed correctly, not just
    that a `ThresholdResult` is returned.
    """
    match_pair = sweep_module.SimilarityPair(
        text_a="alpha",
        text_b="alpha twin",
        label="match",
        cited_node_ids=("cap_fixture_alpha",),
        rationale="identical vectors -- always predicted match",
    )
    non_match_pair = sweep_module.SimilarityPair(
        text_a="beta",
        text_b="gamma",
        label="non_match",
        cited_node_ids=("cap_fixture_beta", "cap_fixture_gamma"),
        rationale="orthogonal vectors -- always predicted non-match",
    )
    call_embedding = _OnceOnlyCallEmbedding(
        {
            "alpha": [1.0, 0.0],
            "alpha twin": [1.0, 0.0],
            "beta": [1.0, 0.0],
            "gamma": [0.0, 1.0],
        }
    )

    results = sweep_module.sweep_dataset(
        (match_pair, non_match_pair),
        [0.5],
        model="fake-embed-model",
        call_embedding=call_embedding,
    )

    assert len(results) == 1
    result = results[0]
    assert result.threshold == 0.5
    assert result.true_positive == 1
    assert result.false_positive == 0
    assert result.true_negative == 1
    assert result.false_negative == 0
    assert result.precision == pytest.approx(1.0)
    assert result.recall == pytest.approx(1.0)
    assert result.f1 == pytest.approx(1.0)


# --- AC-BI-008: hard-fail -- no silent skip, no partial recommendation ----------------


class _ScriptedFailureCallEmbedding:
    """A hand-written `EmbeddingCaller` fake scripted per input `text` -- a scripted
    `Exception` value is raised instead of returning a response, mirroring
    `test_dedup_abort_on_embedding_failure.py`'s `_ScriptedCallEmbedding`.
    """

    def __init__(self, vectors_by_text: dict[str, list[float] | Exception]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        scripted = self._vectors_by_text.get(text)
        if scripted is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        if isinstance(scripted, Exception):
            raise scripted
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=scripted, index=0, object="embedding")]
        )


def test_sweep_dataset_aborts_without_partial_results_on_embedding_failure(
    sweep_module: ModuleType,
) -> None:
    """3 pairs referencing 3 distinct texts; the 2nd unique text's own embedding call
    raises `LlmProviderError`. The exception propagates out of `sweep_dataset` uncaught
    -- proven via `pytest.raises`, so `sweep_dataset` never returns and no
    `ThresholdResult` list exists even to inspect, for any of the 3 requested
    thresholds. The 3rd pair's text is never reached.
    """
    first_text = "Cybersecurity Incident Detection, Response and Containment"
    second_text = "Incident Handling"
    third_text = "never reached"
    pairs = (
        sweep_module.SimilarityPair(
            text_a=first_text,
            text_b=second_text,
            label="match",
            cited_node_ids=("cap_incident_handling_4cf73e",),
            rationale="first pair -- both texts attempted before the failure",
        ),
        sweep_module.SimilarityPair(
            text_a=second_text,
            text_b=third_text,
            label="non_match",
            cited_node_ids=("cap_incident_handling_4cf73e",),
            rationale="second pair -- text_a is a cache hit, text_b is never reached "
            "because second_text's own fetch already failed while scoring the first pair",
        ),
        sweep_module.SimilarityPair(
            text_a=third_text,
            text_b="also never reached",
            label="non_match",
            cited_node_ids=(),
            rationale="third pair -- never scored at all, since the failure happens "
            "while scoring the first pair",
        ),
    )
    call_embedding = _ScriptedFailureCallEmbedding(
        {
            first_text: [1.0, 0.0],
            second_text: openai.APIConnectionError(
                request=httpx.Request("POST", "https://example.invalid")
            ),
        }
    )

    with pytest.raises(LlmProviderError):
        sweep_module.sweep_dataset(
            pairs,
            [0.70, 0.85, 0.95],
            model="fake-embed-model",
            call_embedding=call_embedding,
        )

    # Only first_text and second_text were ever attempted -- third_text/"also never
    # reached" were never fetched, proving the abort happened mid-first-pair, before the
    # 2nd or 3rd pair was ever scored.
    assert call_embedding.calls == [first_text, second_text]


def test_sweep_dataset_never_calls_route_embedding_twice_even_when_a_later_pair_fails(
    sweep_module: ModuleType,
) -> None:
    """Companion to the cache test above: proves the hard-fail behavior and the
    embedding cache compose correctly -- a text already cached before the failure is
    never re-fetched, even though the overall sweep aborts.
    """
    shared_text = "Access Control & Authentication"
    failing_text = "this fetch fails"
    pairs = (
        sweep_module.SimilarityPair(
            text_a=shared_text,
            text_b=shared_text,
            label="match",
            cited_node_ids=("cap_access_control_authentication_151816",),
            rationale="both sides share one text -- cached after the first fetch",
        ),
        sweep_module.SimilarityPair(
            text_a=shared_text,
            text_b=failing_text,
            label="non_match",
            cited_node_ids=("cap_access_control_authentication_151816",),
            rationale="text_a is a cache hit; text_b's fetch fails",
        ),
    )
    call_embedding = _ScriptedFailureCallEmbedding(
        {
            shared_text: [1.0, 0.0],
            failing_text: openai.APIConnectionError(
                request=httpx.Request("POST", "https://example.invalid")
            ),
        }
    )

    with pytest.raises(LlmProviderError):
        sweep_module.sweep_dataset(
            pairs, [0.85], model="fake-embed-model", call_embedding=call_embedding
        )

    # shared_text fetched exactly once (cached on the 2nd pair's text_a), then
    # failing_text's own fetch raised.
    assert call_embedding.calls == [shared_text, failing_text]


# --- CHANGES.md row M1: hermetic full-dataset sweep proof ------------------------------


def _sha256_deterministic_vector(text: str) -> tuple[float, ...]:
    """CHANGES.md row M1's exact deterministic vector scheme -- a fixed 16-dim tuple,
    identical every time for the same string, effectively distinct across different
    strings (SHA-256 collision odds negligible at this scale). Not a scoring
    reimplementation: this only fabricates fake embedding *inputs* for the hermetic
    fake `call_embedding` below -- `cosine_similarity` itself is still the real,
    imported function.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()[:16]
    return tuple((byte / 127.5) - 1.0 for byte in digest)


class _FullDatasetScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake covering every unique text across the
    complete 21-pair dataset (`vectors_by_text` precomputed once, via
    `_sha256_deterministic_vector`, for every `text_a`/`text_b` in `build_dataset()`).
    Mirrors `_ScriptedCallEmbedding`/`_OnceOnlyCallEmbedding`'s style: raises
    `AssertionError` on any unscripted text, so an unexpected/mistyped dataset text
    fails loudly rather than silently returning an arbitrary vector.
    """

    def __init__(self, vectors_by_text: dict[str, tuple[float, ...]]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        vector = self._vectors_by_text.get(text)
        if vector is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        return EmbeddingResponse(
            model=model,
            data=[Embedding(embedding=list(vector), index=0, object="embedding")],
        )


def test_sweep_dataset_scores_the_full_real_dataset_cleanly_across_the_default_range(
    sweep_module: ModuleType,
) -> None:
    """CHANGES.md row M1: calls `sweep_dataset()` directly (no CLI) over the complete,
    real 21-pair dataset (10 match + 11 non-match, `build_dataset()`) with a fully
    scripted fake `call_embedding` covering every unique text via the exact
    deterministic SHA-256-based vector scheme CHANGES.md row M1 specifies, swept
    across PLAN.md's default 0.70-0.95 step 0.01 threshold range (26 thresholds).
    Proves the whole real dataset scores cleanly through the real
    `cosine_similarity`/cache path -- one `ThresholdResult` per threshold, zero
    exceptions, every metric within its valid [0.0, 1.0] range -- before any CLI or
    live/network dependency exists.
    """
    pairs = sweep_module.build_dataset()
    unique_texts = {text for pair in pairs for text in (pair.text_a, pair.text_b)}
    vectors_by_text = {text: _sha256_deterministic_vector(text) for text in unique_texts}
    call_embedding = _FullDatasetScriptedCallEmbedding(vectors_by_text)
    thresholds = [round(0.70 + 0.01 * step, 2) for step in range(26)]

    results = sweep_module.sweep_dataset(
        pairs, thresholds, model="fake-embed-model", call_embedding=call_embedding
    )

    assert len(pairs) == 21
    assert len(results) == len(thresholds) == 26
    assert [result.threshold for result in results] == thresholds
    for result in results:
        assert 0.0 <= result.precision <= 1.0
        assert 0.0 <= result.recall <= 1.0
        assert 0.0 <= result.f1 <= 1.0
        total = (
            result.true_positive
            + result.false_positive
            + result.true_negative
            + result.false_negative
        )
        assert total == len(pairs)
    # every unique text across all 21 pairs was fetched -- none skipped, none
    # fabricated outside the precomputed scheme.
    assert set(call_embedding.calls) == unique_texts
