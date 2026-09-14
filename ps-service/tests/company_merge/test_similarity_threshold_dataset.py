"""Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s labeled dataset
(issue #29, PLAN.md §1 Increment 4, §3; CHANGES.md's "no PLAN.md edit" note on M1/M3).

Loads the script by path via `importlib.util.spec_from_file_location`, exactly as
`test_run_live_merge_cli.py` documents doing for `tools/company-merge/run_live_merge.py`
(same non-package-script-by-path pattern as `test_similarity_threshold_sweep_scoring.py`
already uses for this same script).

Covers, against the real `test-data/eu-regulations/{gdpr,nis2,cra}.json` fixtures (no
network, no FalkorDB -- these are the same static fixture files Company Merge's own
tests already read):

- AC-BI-001: `build_dataset()` returns at least 10 true-match pairs and at least 10
  true-non-match pairs, and every pair's `cited_node_ids` resolve to real Capability
  ids actually present in the loaded gdpr/nis2/cra JSON (membership checked by loading
  the files here directly, not merely trusted from the literal string).
- AC-BI-002: every pair's `rationale` is a non-empty string.
- AC-BI-003 (general form, corpus scale): a property test iterating every real
  identical-name id group found by scanning all 90 real Capability nodes (PLAN.md §3.3
  claims 19 -- independently re-counted here, not trusted blindly) asserts none of
  those names appear as a same-string (`text_a == text_b`) pair in the dataset.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "tools" / "company-merge" / "company_merge_similarity_sweep.py"
_MODULE_NAME = "_company_merge_similarity_sweep_dataset_under_test"
_EU_REGULATIONS_DIR = _REPO_ROOT / "test-data" / "eu-regulations"
_CAPABILITY_SOURCE_FILENAMES = ("gdpr.json", "nis2.json", "cra.json")


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


@pytest.fixture(autouse=True)
def _configure_logging_for_route_embedding() -> None:  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    """`build_dataset()` does no LLM Interface call itself, but loading the sweep
    module exercises the same module-level import surface as
    `test_similarity_threshold_sweep_scoring.py`; configuring here keeps both test
    files' fixture behavior identical and harmless (root conftest's `_isolate_logging`
    already redirects `PS_LOGGING_DIR` to `tmp_path` and resets the facade per test).
    """
    configure_logging()


def _real_capability_nodes() -> list[Any]:
    """Load every real Capability node directly from the three EU-regulation fixture
    files -- independent of the sweep script's own `_repeated_capability_names_from_
    corpus`, so this test suite doesn't just trust the implementation's own count.

    Returns `list[Any]` (mirroring the sweep script's own `json.loads`-flows-as-`Any`
    pattern, `company_merge_similarity_sweep.py`'s `_repeated_capability_names_from_
    corpus`): these are raw parsed-JSON node objects, not a typed domain model --
    `test-data/eu-regulations/*.json` is untyped fixture data, not a `ps_service`
    Pydantic model.
    """
    nodes: list[Any] = []
    for filename in _CAPABILITY_SOURCE_FILENAMES:
        graph = json.loads((_EU_REGULATIONS_DIR / filename).read_text(encoding="utf-8"))
        nodes.extend(node for node in graph["nodes"] if node.get("label") == "Capability")
    return nodes


@pytest.fixture(scope="module")
def real_capability_ids() -> frozenset[str]:
    return frozenset(node["properties"]["id"] for node in _real_capability_nodes())


@pytest.fixture(scope="module")
def real_identical_name_groups() -> dict[str, int]:
    """Every real Capability `name` shared by two or more of the 90 real nodes, with
    its occurrence count -- independently re-scanned here (not merely trusting
    PLAN.md's claim of 19).
    """
    names: list[str] = [node["properties"]["name"] for node in _real_capability_nodes()]
    counts = Counter(names)
    return {name: count for name, count in counts.items() if count >= 2}


# --- AC-BI-001: dataset size + real-node citation --------------------------------------


def test_dataset_has_at_least_ten_true_match_and_ten_true_non_match_pairs(
    sweep_module: ModuleType,
) -> None:
    pairs = sweep_module.build_dataset()

    true_match_pairs = [pair for pair in pairs if pair.label == "match"]
    true_non_match_pairs = [pair for pair in pairs if pair.label == "non_match"]

    assert len(true_match_pairs) >= 10
    assert len(true_non_match_pairs) >= 10


def test_ninety_real_capability_nodes_are_loaded_from_the_three_regulation_files() -> None:
    """Sanity check on this test file's own corpus loading -- PLAN.md §0 F2 confirms
    42 (GDPR) + 19 (NIS2) + 29 (CRA) = 90 real Capability node *entries* (raw JSON
    objects, counted before deduplicating by id); re-confirmed here directly rather
    than trusted from PLAN.md's prose.
    """
    assert len(_real_capability_nodes()) == 90


def test_real_capability_ids_dedupe_to_sixty_seven_distinct_ids(
    real_capability_ids: frozenset[str],
) -> None:
    """67 distinct ids, not 90: PLAN.md §3.3/§0 F2's 19 identical-name groups are each
    the *same* id repeated verbatim across 2-3 regulation files (confirmed by direct
    count: 90 raw node entries, 19 ids repeated -- 15 appearing in exactly 2 regulation
    files and 4 appearing in all 3 -- collapse to 67 distinct ids). `real_capability_ids`
    (a deduped frozenset) is what `cited_node_ids` membership is actually checked
    against below.
    """
    assert len(real_capability_ids) == 67


def test_every_pairs_cited_node_ids_resolve_to_real_capability_ids(
    sweep_module: ModuleType, real_capability_ids: frozenset[str]
) -> None:
    pairs = sweep_module.build_dataset()

    for pair in pairs:
        assert pair.cited_node_ids, f"pair {pair!r} cites no node ids"
        for node_id in pair.cited_node_ids:
            assert node_id in real_capability_ids, (
                f"cited_node_ids entry {node_id!r} (pair text_a={pair.text_a!r}, "
                f"text_b={pair.text_b!r}) is not a real Capability id present in "
                "gdpr.json/nis2.json/cra.json"
            )


def test_true_match_pairs_each_cite_exactly_one_real_node(sweep_module: ModuleType) -> None:
    """Every true-match pair is (paraphrase, real node's own name) -- exactly one real
    node is the paraphrase's target (PLAN.md §3.2's shape).
    """
    true_match_pairs = [pair for pair in sweep_module.build_dataset() if pair.label == "match"]

    for pair in true_match_pairs:
        assert len(pair.cited_node_ids) == 1


def test_true_non_match_pairs_each_cite_exactly_two_distinct_real_nodes(
    sweep_module: ModuleType,
) -> None:
    """Every true-non-match pair cites two real, distinct Capability nodes (PLAN.md
    §3.1's shape) -- never the same node cited twice.
    """
    true_non_match_pairs = [
        pair for pair in sweep_module.build_dataset() if pair.label == "non_match"
    ]

    for pair in true_non_match_pairs:
        assert len(pair.cited_node_ids) == 2
        assert pair.cited_node_ids[0] != pair.cited_node_ids[1]


# --- AC-BI-002: rationale required ------------------------------------------------------


def test_every_pair_has_a_non_empty_rationale(sweep_module: ModuleType) -> None:
    pairs = sweep_module.build_dataset()

    for pair in pairs:
        assert pair.rationale
        assert pair.rationale.strip()


# --- AC-BI-003, general form, at corpus scale -------------------------------------------


def test_exactly_nineteen_real_capability_names_are_shared_across_two_or_more_nodes(
    real_identical_name_groups: dict[str, int],
) -> None:
    """PLAN.md §3.3 claims 19 such names -- independently re-counted here by scanning
    all 90 real Capability nodes directly (not trusted blindly from the plan's prose).
    """
    assert len(real_identical_name_groups) == 19


@pytest.mark.parametrize(
    "shared_name",
    [
        "Data Minimisation",
        "Access Control & Authentication",
        "Data Encryption",
        "Data & Configuration Integrity Protection",
        "Availability & Resilience",
        "Cybersecurity Risk Management Program",
        "Business Continuity & Disaster Recovery",
        "Security Control Effectiveness Assessment",
        "Asset & Personnel Security Management",
        "Security Incident Reporting",
        "Incident Handling",
        "Vulnerability Reporting & User Communication",
        "Regulatory Cooperation",
        "Compliance Documentation Management",
        "Secure Data Removal & Portability",
        "Cybersecurity Risk Assessment Process",
        "Secure Development Lifecycle",
        "Vulnerability Management",
        "Coordinated Vulnerability Disclosure Policy",
    ],
)
def test_no_real_identical_name_group_appears_as_a_same_string_pair_in_the_dataset(
    sweep_module: ModuleType,
    real_identical_name_groups: dict[str, int],
    shared_name: str,
) -> None:
    """Property test over all 19 known identical-name id groups (PLAN.md §3.3):
    none of them may appear as a `text_a == text_b == shared_name` pair in the built
    dataset -- such a pair would resolve via Company Merge's own exact-key match
    before semantic scoring ever runs, testing nothing about the threshold.
    """
    assert shared_name in real_identical_name_groups, (
        f"fixture list is stale: {shared_name!r} is no longer one of the corpus's "
        "shared-name groups -- update this parametrize list against the real files"
    )
    pairs = sweep_module.build_dataset()

    violating_pairs = [pair for pair in pairs if pair.text_a == pair.text_b == shared_name]

    assert violating_pairs == []


def test_the_nineteen_parametrized_names_are_exactly_the_real_corpus_groups(
    real_identical_name_groups: dict[str, int],
) -> None:
    """Guards the property test above against silently drifting out of sync with the
    real corpus: the 19 names parametrized there must be exactly the 19 names this
    test file independently finds by scanning all 90 real nodes.
    """
    parametrized_names = {
        "Data Minimisation",
        "Access Control & Authentication",
        "Data Encryption",
        "Data & Configuration Integrity Protection",
        "Availability & Resilience",
        "Cybersecurity Risk Management Program",
        "Business Continuity & Disaster Recovery",
        "Security Control Effectiveness Assessment",
        "Asset & Personnel Security Management",
        "Security Incident Reporting",
        "Incident Handling",
        "Vulnerability Reporting & User Communication",
        "Regulatory Cooperation",
        "Compliance Documentation Management",
        "Secure Data Removal & Portability",
        "Cybersecurity Risk Assessment Process",
        "Secure Development Lifecycle",
        "Vulnerability Management",
        "Coordinated Vulnerability Disclosure Policy",
    }

    assert parametrized_names == set(real_identical_name_groups)
