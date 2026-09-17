"""Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s labeled dataset
(issue #29, PLAN.md §1 Increment 4, §3; CHANGES.md's "no PLAN.md edit" note on M1/M3;
issue #100 Slice 2 repoints this file's own independent corpus-loading fixtures from
`test-data/eu-regulations/*.json` to `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json`).

Loads the script by path via `importlib.util.spec_from_file_location`, exactly as
`test_run_live_merge_cli.py` documents doing for `tools/company-merge/run_live_merge.py`
(same non-package-script-by-path pattern as `test_similarity_threshold_sweep_scoring.py`
already uses for this same script).

Covers, against the real `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json` files (no
network, no FalkorDB -- read via `parse_serialized_graph_json`, an independent read path
from the sweep script's own `_load_curated_content_capability_nodes()`, so this test
suite doesn't just trust the implementation's own count):

- AC-BI-001: `build_dataset()` returns at least 10 true-match pairs and at least 10
  true-non-match pairs, and every pair's `cited_node_ids` resolve to real Capability
  ids actually present in curated-content (membership checked by loading the baseline
  exports here directly, not merely trusted from the literal string).
- AC-BI-002: every pair's `rationale` is a non-empty string.
- AC-BI-003 (general form, corpus scale): a property test iterating every real
  identical-name id group found by scanning all 778 real Capability node entries
  across curated-content (10 such groups -- independently re-counted here, not
  trusted blindly) asserts none of those names appear as a same-string
  (`text_a == text_b`) pair in the dataset.

Issue #100 Slice 2 note: `test_every_pairs_cited_node_ids_resolve_to_real_capability_ids`
and the two cardinality tests below (`test_true_match_pairs_each_cite_exactly_one_real_
node`/`test_true_non_match_pairs_each_cite_exactly_two_distinct_real_nodes`) are expected
to go red at the end of Slice 2 (PLAN.md Slice 2 "Red-then-green"): `real_capability_ids`
now resolves against curated-content, but `build_dataset()` still returns Slice-3-pending
old-corpus-citing `_CANDIDATE_SPECS`. Slice 3 re-derives those specs against real
curated-content ids and turns them green again.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.export.serialize import parse_serialized_graph_json
from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from types import ModuleType

    from ps_service.export.models import SerializedNode

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "tools" / "company-merge" / "company_merge_similarity_sweep.py"
_MODULE_NAME = "_company_merge_similarity_sweep_dataset_under_test"
_CURATED_CONTENT_DIR = _REPO_ROOT / "curated-content"
_CURATED_CONTENT_INSTRUMENT_DIRS = ("GDPR-1.0", "NIS2-1.0", "CRA-1.0")


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


def _real_capability_nodes() -> list[SerializedNode]:
    """Load every real Capability node directly from the three curated-content
    baseline exports -- independent of the sweep script's own
    `_load_curated_content_capability_nodes()`, so this test suite doesn't just
    trust the implementation's own count.

    Returns `list[SerializedNode]`: each instrument's `baseline.json` is parsed via
    `parse_serialized_graph_json` (the same real, typed codec the production code
    uses, imported directly here rather than via the sweep module) and filtered to
    `label == "Capability"`.
    """
    nodes: list[SerializedNode] = []
    for instrument_dir in _CURATED_CONTENT_INSTRUMENT_DIRS:
        path = _CURATED_CONTENT_DIR / instrument_dir / "baseline.json"
        graph = parse_serialized_graph_json(path.read_bytes())
        nodes.extend(node for node in graph.nodes if node.label == "Capability")
    return nodes


@pytest.fixture(scope="module")
def real_capability_ids() -> frozenset[str]:
    return frozenset(cast("str", node.properties["id"]) for node in _real_capability_nodes())


@pytest.fixture(scope="module")
def real_identical_name_groups() -> dict[str, int]:
    """Every real Capability `name` shared by two or more of the 778 real Capability
    node entries across curated-content, with its occurrence count -- independently
    re-scanned here (not merely trusting PLAN.md's claim of 10).
    """
    names: list[str] = [cast("str", node.properties["name"]) for node in _real_capability_nodes()]
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


def test_seven_hundred_seventy_eight_real_capability_nodes_load_from_curated_content() -> None:
    """Sanity check on this test file's own corpus loading -- 169 (GDPR) + 87 (NIS2) +
    522 (CRA) = 778 real Capability node *entries* (raw nodes, counted before
    deduplicating by id); re-confirmed here directly rather than trusted from
    BASELINE.md's prose.
    """
    assert len(_real_capability_nodes()) == 778


def test_real_capability_ids_dedupe_to_seven_hundred_sixty_seven_distinct_ids(
    real_capability_ids: frozenset[str],
) -> None:
    """767 distinct ids, not 778: 9 names are shared across exactly 2 regulation files
    (`cap_capstone_seeded_incident_notification_capability_79ff14`,
    `cap_conflict_of_interest_management_c331b1`, `cap_consultation_workflow_1530ef`,
    `cap_contact_information_provision_c92eab`,
    `cap_regulatory_consultation_workflow_568d2f`, `cap_resource_provisioning_9fb37b`,
    `cap_information_protection_1f54d5`, `cap_regulatory_reporting_workflow_52af1f`,
    `cap_resource_allocation_management_6a2597`), and 1
    (`cap_regulatory_notification_workflow_37f17e`) is shared across all 3 -- 9 + 1 =
    10. `real_capability_ids` (a deduped frozenset) is what `cited_node_ids`
    membership is actually checked against below.
    """
    assert len(real_capability_ids) == 767


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
                "curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json"
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


def test_exactly_ten_real_capability_names_are_shared_across_two_or_more_nodes(
    real_identical_name_groups: dict[str, int],
) -> None:
    """10 such names -- independently re-counted here by scanning all 778 real
    Capability node entries across curated-content directly (not trusted blindly
    from the plan's prose).
    """
    assert len(real_identical_name_groups) == 10


@pytest.mark.parametrize(
    "shared_name",
    [
        "Capstone Seeded Incident Notification Capability",
        "Conflict of Interest Management",
        "Consultation Workflow",
        "Contact Information Provision",
        "Regulatory Consultation Workflow",
        "Regulatory Notification Workflow",
        "Resource Provisioning",
        "Information Protection",
        "Regulatory Reporting Workflow",
        "Resource Allocation Management",
    ],
)
def test_no_real_identical_name_group_appears_as_a_same_string_pair_in_the_dataset(
    sweep_module: ModuleType,
    real_identical_name_groups: dict[str, int],
    shared_name: str,
) -> None:
    """Property test over all 10 known identical-name id groups (issue #100 §1's
    table): none of them may appear as a `text_a == text_b == shared_name` pair in
    the built dataset -- such a pair would resolve via Company Merge's own exact-key
    match before semantic scoring ever runs, testing nothing about the threshold.
    """
    assert shared_name in real_identical_name_groups, (
        f"fixture list is stale: {shared_name!r} is no longer one of the corpus's "
        "shared-name groups -- update this parametrize list against the real files"
    )
    pairs = sweep_module.build_dataset()

    violating_pairs = [pair for pair in pairs if pair.text_a == pair.text_b == shared_name]

    assert violating_pairs == []


def test_the_ten_parametrized_names_are_exactly_the_real_corpus_groups(
    real_identical_name_groups: dict[str, int],
) -> None:
    """Guards the property test above against silently drifting out of sync with the
    real corpus: the 10 names parametrized there must be exactly the 10 names this
    test file independently finds by scanning all 778 real nodes.
    """
    parametrized_names = {
        "Capstone Seeded Incident Notification Capability",
        "Conflict of Interest Management",
        "Consultation Workflow",
        "Contact Information Provision",
        "Regulatory Consultation Workflow",
        "Regulatory Notification Workflow",
        "Resource Provisioning",
        "Information Protection",
        "Regulatory Reporting Workflow",
        "Resource Allocation Management",
    }

    assert parametrized_names == set(real_identical_name_groups)
