"""`merge_baseline_graph` over the real engineering-practices seed.

The seed is ingested by the real `ingest_internal_regulatory_instrument` into a gateway rig's
`engprac_baseline` graph, then merged by the real `merge_baseline_graph`. Only the graph engines
(the rig's in-memory baseline, the merge rig's `policy_system` and its legacy read handle) and
the embedding LLM are doubles. Every Policy/Standard/Control the seed carries arrives `draft`,
owned by the caller, created-only and never converged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import GatewayRig

from company_merge._seed_merge_graphs import (
    EMBED_MODEL,
    SIMILARITY_THRESHOLD,
    BaselineGraphReader,
    OneHotEmbeddingCaller,
    ingest_seed,
    parse_seed,
)
from ps_service.company_merge.errors import (
    CompanyMergePersistenceError,
    GovernanceOwnerRequiredError,
)
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.merge import merge_baseline_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter
    from company_merge._gateway_fakes import MergeRig, RigReadHandle
    from ps_service.company_merge.models import MergeResult
    from ps_service.logging import LogEmitter

_SEED_PATH = (
    Path(__file__).resolve().parents[3]
    / "internal-sources"
    / "engineering-practices"
    / "engineering-practices-seed.json"
)
_OWNER = ("sub-owner-1", "https://idp.example")
_GOVERNANCE = GovernanceImport(owner=_OWNER)
_RID = "ENGPRAC-1.0"
_GOVERNANCE_LABELS = ("Policy", "Standard", "Control")


@pytest.fixture
def rig() -> Iterator[GatewayRig]:
    gateway_rig = GatewayRig()
    yield gateway_rig
    gateway_rig.close()


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


@pytest.fixture
def baseline(rig: GatewayRig) -> BaselineGraphReader:
    """The real engineering-practices seed, ingested, as the merge's baseline graph."""
    seed = parse_seed(json.loads(_SEED_PATH.read_text(encoding="utf-8")))
    return BaselineGraphReader(rig.graphs.open(ingest_seed(rig, seed)))


def _merge(
    baseline: BaselineGraphReader,
    store: RigReadHandle,
    emitter: LogEmitter,
    merge_rig: MergeRig,
    *,
    governance: GovernanceImport | None = _GOVERNANCE,
    call_embedding: OneHotEmbeddingCaller | None = None,
) -> MergeResult:
    return merge_baseline_graph(
        _RID,
        baseline_graph=baseline,
        single_tenant_graph=store,
        embed_model=EMBED_MODEL,
        similarity_threshold=SIMILARITY_THRESHOLD,
        call_embedding=call_embedding or OneHotEmbeddingCaller(),
        emitter=emitter,
        governance=governance,
        **merge_rig.merge_keywords(),
    )


def test_seed_tree_is_written_as_drafts_owned_by_the_caller(
    baseline: BaselineGraphReader, emitter: LogEmitter, merge_rig: MergeRig
) -> None:
    """11 Policies, 21 Standards, 39 Controls, all `draft`, with every edge."""
    store = merge_rig.legacy

    result = _merge(baseline, store, emitter, merge_rig)

    assert (len(store.nodes_of("Policy")), len(store.nodes_of("Standard"))) == (11, 21)
    assert len(store.nodes_of("Control")) == 39
    for label in _GOVERNANCE_LABELS:
        assert {props["status"] for props in store.nodes_of(label).values()} == {"draft"}
    for policy in store.nodes_of("Policy").values():
        assert (policy["owner_subject"], policy["owner_issuer"]) == _OWNER
    edge_counts = {
        edge: store.edge_count(edge)
        for edge in ("GOVERNED_BY", "SUPPORTED_BY", "IMPLEMENTED_BY", "OWNS", "VERIFIED_BY")
    }
    assert edge_counts == {
        "GOVERNED_BY": 12,
        "SUPPORTED_BY": 21,
        "IMPLEMENTED_BY": 39,
        "OWNS": 11,
        "VERIFIED_BY": 24,
    }
    assert (result.governance_policies, result.governance_standards) == (11, 21)
    assert result.governance_controls == 39


def test_every_governance_write_is_create_only(
    baseline: BaselineGraphReader, emitter: LogEmitter, merge_rig: MergeRig
) -> None:
    """One group of 71 nodes the first time, and nothing the second time."""
    store = merge_rig.legacy

    _merge(baseline, store, emitter, merge_rig)

    governance_entries = [
        entry
        for group in merge_rig.groups
        for entry in group.entries
        if entry.name in _GOVERNANCE_LABELS
    ]
    assert len(governance_entries) == 11 + 21 + 39
    assert all(entry.content["op"] == "upsert_node" for entry in governance_entries)
    assert len({entry.group_id for entry in governance_entries}) == 1

    _merge(baseline, store, emitter, merge_rig)

    assert [
        entry
        for group in merge_rig.groups
        for entry in group.entries
        if entry.name in _GOVERNANCE_LABELS
    ] == governance_entries


def test_an_approved_tree_is_untouched_by_a_reingest(
    baseline: BaselineGraphReader, emitter: LogEmitter, merge_rig: MergeRig
) -> None:
    """A second merge leaves an approved Policy, its Standards and Controls alone."""
    store = merge_rig.legacy
    _merge(baseline, store, emitter, merge_rig)
    approved = {"status": "approved", "owner_subject": "someone-else"}
    for label in _GOVERNANCE_LABELS:
        for node_id in store.nodes_of(label):
            store.nodes[(label, node_id)].update(approved)

    _merge(baseline, store, emitter, merge_rig)

    for label in _GOVERNANCE_LABELS:
        for props in store.nodes_of(label).values():
            assert props["status"] == "approved"
            assert props["owner_subject"] == "someone-else"
    assert len(store.nodes_of("Policy")) == 11


def test_a_near_title_policy_is_a_separate_draft_and_nothing_is_embedded_for_it(
    baseline: BaselineGraphReader, emitter: LogEmitter, merge_rig: MergeRig
) -> None:
    """A different id is never converged onto, even with an embedding to compare."""
    store = merge_rig.legacy
    store.nodes[("Policy", "pol_existing_engineering_practices")] = {
        "title": "Engineering Practice Policy",
        "status": "approved",
        "embedding": [1.0] * 512,
    }
    embedder = OneHotEmbeddingCaller()

    _merge(baseline, store, emitter, merge_rig, call_embedding=embedder)

    policies = store.nodes_of("Policy")
    assert len(policies) == 12
    assert policies["pol_existing_engineering_practices"]["status"] == "approved"
    seed_titles = {
        props["title"]
        for node_id, props in policies.items()
        if node_id != "pol_existing_engineering_practices"
    }
    assert seed_titles.isdisjoint(embedder.calls)
    review_kinds = {
        params["kind"]
        for statement, params in zip(store.statements, store.params_seen, strict=True)
        if "PendingReview" in statement
    }
    assert "Policy" not in review_kinds


def test_an_ownerless_merge_of_a_governance_baseline_is_refused_before_any_governance_write(
    baseline: BaselineGraphReader, emitter: LogEmitter, merge_rig: MergeRig
) -> None:
    store = merge_rig.legacy

    with pytest.raises(GovernanceOwnerRequiredError):
        _merge(baseline, store, emitter, merge_rig, governance=None)

    assert store.nodes_of("Policy") == {}


def test_an_edge_to_a_node_that_exists_nowhere_still_fails_the_merge(
    rig: GatewayRig, baseline: BaselineGraphReader, emitter: LogEmitter, merge_rig: MergeRig
) -> None:
    """A dangling endpoint is still `CompanyMergePersistenceError`."""
    graph = rig.graphs.open("engprac_baseline")
    verified_control = next(edge[4] for edge in graph.edges if edge[0] == "VERIFIED_BY")
    del graph.nodes[("Control", verified_control)]
    store = merge_rig.legacy

    with pytest.raises(CompanyMergePersistenceError):
        _merge(baseline, store, emitter, merge_rig)
