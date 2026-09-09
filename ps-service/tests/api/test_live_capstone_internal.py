"""GH #76, Slice 4 -- the live internal-regulation ingestion capstone.

``@pytest.mark.falkordb_live @pytest.mark.llm_live``: drives the real
``POST /ingestions`` route with a ``source: "internal"`` request for the
Authored Governance seed fixture (``authored-governance-seed.json`` -- a
single Capability -> Policy -> Standard -> Control chain, entirely human-
authored) against real FalkorDB and real Azure OpenAI, and asserts the
merged spine lands in a **disposable** single-tenant graph
(``policy_system_api_internal_capstone_test``) -- never the real, shared
``policy_system`` graph, whose node count is read before and after and
asserted unchanged. Structurally mirrors ``tests/api/test_live_capstone_external.py``
(same disposable-graph naming/cleanup pattern, same one-module-scoped-
fixture-many-assertions shape, same safety property).

**Why this still needs real LLM credentials (GH #76 CHANGES.md item 1)**:
GH #76 deletes the ``governance_derivation`` pipeline stage outright --
Policy/Standard/Control are now human-authored in the submitted document,
never LLM-minted (that is AC-BI-011, proven structurally: Slice 1 deletes
the stage and every one of its LLM call sites). But this test's pipeline is
``internal_ingestion -> merge``, and ``merge`` (Company Merge, S4) still
runs its semantic-dedup pass, which calls ``company_merge/dedup.py::
route_embedding`` -- a real, credentialed embedding call, independent of
governance derivation. So ``@pytest.mark.llm_live`` and its skip-guard stay;
dropping them would make this file collect and immediately error the first
time ``merge`` tries to embed something without credentials, rather than
skip cleanly.

**Structural-assertion style throughout** (node/edge counts and
cardinality) for the merge-layer proofs; **exact-id assertions** for the
governance layer specifically, since -- unlike the old LLM-derived shape --
Policy/Standard/Control are now exact-id-deterministic on every run (minted
by ``domain_mapper.identity.policy_id``/``standard_id``/``control_id``, pure
hashes over the fixture's own authored titles, never LLM output).

Proves, against real infrastructure -- no fakes/mocks anywhere in this
file:

1. ``test_internal_ingestion_populates_governance_spine`` -- a
   ``RegulatoryInstrument{source_type: 'internal'}`` exists; the authored
   Capability has a ``GOVERNED_BY`` edge to its Policy; the Policy has a
   ``SUPPORTED_BY`` edge to its Standard; the Control belongs to exactly one
   Standard (``IMPLEMENTED_BY`` cardinality); the full Regulation -> Role ->
   Obligation -> Capability -> Policy -> Standard -> Control traversal is
   reachable end to end (the "back to query" proof).
2. ``test_second_identical_ingestion_is_structural_no_op`` -- re-posting the
   exact same fixture is a structural no-op, **including the governance
   layer, by exact canonical id, with no ``xfail``**: because Policy/
   Standard/Control are now author-supplied and exact-id-deterministic
   (the same property Role/Requirement/Obligation/Capability already had),
   there is no LLM-wording non-determinism left for this layer to bound --
   the ``xfail`` the old LLM-derived version of this test needed is a
   genuine simplification GH #76 buys back, not a weakening of coverage.
3. ``test_dangling_edge_fixture_fails_closed_against_real_pipeline`` --
   AC-BI-008/009: the dedicated dangling-edge fixture
   (``authored-governance-dangling-edge.json``, a dangling ``SUPPORTED_BY``
   edge) 502s with ``failing_stage: "internal_ingestion"`` and writes
   NOTHING to its own ``{short}_native``/``{short}_baseline`` graphs or the
   disposable single-tenant graph -- proven against the real pipeline
   (the fast-suite fake-graph tests already prove the same thing without
   real infra).

All three run against the SAME module-scoped fixture / disposable graph
(one shared ``POST`` sequence) to bound LLM cost -- mirrors
``test_live_capstone_external.py``'s own one-fixture-many-assertions shape.
The dangling-edge attempt costs no extra LLM tokens for the
``internal_ingestion`` stage itself (referential-integrity validation
happens in ``ingest_internal_regulatory_instrument`` before any graph write
and before ``merge`` ever runs) -- it just cannot avoid needing credentials
for the *other* two assertions in this module, hence the marker staying put.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.config import load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle

# Captured at import time, before the autouse `_isolate_logging` fixture strips them.
_CHAT_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")
_EMBED_MODEL = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")

_DISPOSABLE_GRAPH = "policy_system_api_internal_capstone_test"
_REAL_GRAPH = "policy_system"
_ENDPOINT = "/ingestions"

_SEED_FIXTURE_PATH = "authored-governance/authored-governance-seed.json"
_SEED_RID = "AUTHGOV-1.0"
_SEED_SHORT_NAME = "AUTHGOV"
_SEED_REQUEST: dict[str, str] = {"source": "internal", "fixture_path": _SEED_FIXTURE_PATH}

_DANGLING_FIXTURE_PATH = "authored-governance/authored-governance-dangling-edge.json"
_DANGLING_RID = "AUTHGOV-DANGLING-1.0"
_DANGLING_SHORT_NAME = "AUTHGOV-DANGLING"
_DANGLING_REQUEST: dict[str, str] = {"source": "internal", "fixture_path": _DANGLING_FIXTURE_PATH}

_COUNT_ALL = "MATCH (n) RETURN count(n)"

# Fixed label / relationship-type allow-list -- safe to interpolate into Cypher
# (L2: labels/rel-types may come from fixed module-level constants).
_DETERMINISTIC_NODE_LABELS = ("RegulatoryInstrument", "Role", "Requirement", "Obligation")
_GOVERNANCE_NODE_LABELS = ("Capability", "Policy", "Standard", "Control")
_DETERMINISTIC_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY")
_GOVERNANCE_EDGE_TYPES = ("REQUIRES", "GOVERNED_BY", "SUPPORTED_BY", "IMPLEMENTED_BY")

pytestmark = [
    pytest.mark.falkordb_live,
    pytest.mark.llm_live,
    pytest.mark.skipif(
        not _CHAT_MODEL or not _EMBED_MODEL,
        reason="requires .env sourced (PS_LLMINTERFACE_MODEL/_EMBED_MODEL, AZURE_*)",
    ),
]


@dataclass(frozen=True, slots=True)
class _InternalCapstoneData:
    """Everything the three capstone tests read, captured by the one shared run."""

    response_1_status: int
    response_1_body: dict[str, object]
    response_2_status: int
    response_2_body: dict[str, object]
    snapshot_1: dict[str, int]
    snapshot_2: dict[str, int]
    dangling_status: int
    dangling_body: dict[str, object]
    dangling_native_count: int
    dangling_baseline_count: int
    dangling_regulatory_instrument_in_disposable: int
    real_policy_system_before: int
    real_policy_system_after: int
    disposable_graph: GraphHandle


def _count(graph: GraphHandle, query: str, params: dict[str, object] | None = None) -> int:
    """Run a ``RETURN count(...)`` query and return the single integer result."""
    rows = cast("list[list[object]]", graph.query(query, params=params).result_set)
    return cast("int", rows[0][0])


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    """Run a Cypher query and return its raw ``result_set`` rows."""
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _snapshot(
    graph: GraphHandle, labels: tuple[str, ...], rel_types: tuple[str, ...]
) -> dict[str, int]:
    """Per-label node counts + per-relationship-type edge counts for one graph."""
    counts = {
        f"node:{label}": _count(graph, f"MATCH (n:{label}) RETURN count(n)") for label in labels
    }
    counts.update(
        {
            f"edge:{rel}": _count(graph, f"MATCH ()-[r:{rel}]->() RETURN count(r)")
            for rel in rel_types
        }
    )
    return counts


def _full_snapshot(graph: GraphHandle) -> dict[str, int]:
    """The whole disposable graph's per-label/per-relationship-type counts."""
    deterministic = _snapshot(graph, _DETERMINISTIC_NODE_LABELS, _DETERMINISTIC_EDGE_TYPES)
    governance = _snapshot(graph, _GOVERNANCE_NODE_LABELS, _GOVERNANCE_EDGE_TYPES)
    return {**deterministic, **governance}


def _delete_graph_if_exists(db: FalkorDB, name: str) -> None:
    """Drop ``name`` from FalkorDB if it is currently present (best effort)."""
    if name in db.list_graphs():
        db.select_graph(name).delete()


@pytest.fixture(scope="module")
def capstone(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_InternalCapstoneData]:
    """Run the real internal-seed pipeline against real infrastructure, once, and capture state.

    Sequence, all against one disposable single-tenant graph
    (``_DISPOSABLE_GRAPH``, via the existing ``PS_FALKORDB_GRAPH`` override
    -- no new mechanism): the seed fixture is POSTed twice (item 1's
    proof, then item 2's no-op proof), then the dangling-edge fixture is
    POSTed once (item 3's fail-closed proof; it never reaches ``merge``, so
    it cannot touch the disposable graph either way).

    The Domain Mapper / Company Merge stages emit through the process-wide
    default emitter, so a real ``configure()``d facade is installed for the
    run -- mirrors ``test_live_capstone_external.py``'s own
    ``_atexit_registered`` save/restore exactly, so this live-only module
    can never poison ``tests/logging``'s once-only ``atexit`` assertion.

    Yields:
        The captured :class:`_InternalCapstoneData`.
    """
    assert _CHAT_MODEL is not None  # narrowed by the module skipif
    assert _EMBED_MODEL is not None

    monkeypatch = pytest.MonkeyPatch()
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]

    config = load_config()
    db = connect_from_config(config)
    real_before = _count(select_graph(db, _REAL_GRAPH), _COUNT_ALL)

    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", _CHAT_MODEL)
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", _EMBED_MODEL)
    if not os.environ.get("PS_COMPANYMERGE_SIMILARITY_THRESHOLD"):
        monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.85")
    monkeypatch.setenv("PS_FALKORDB_GRAPH", _DISPOSABLE_GRAPH)

    for graph_name in (
        _DISPOSABLE_GRAPH,
        native_graph_name(_SEED_SHORT_NAME),
        baseline_graph_name(_SEED_SHORT_NAME),
        native_graph_name(_DANGLING_SHORT_NAME),
        baseline_graph_name(_DANGLING_SHORT_NAME),
    ):
        _delete_graph_if_exists(db, graph_name)

    facade.configure(log_path=tmp_path_factory.mktemp("api_internal_capstone") / "capstone.jsonl")

    app = create_app(load_config())

    try:
        client = TestClient(app, raise_server_exceptions=False)
        response_1 = client.post(_ENDPOINT, json=_SEED_REQUEST)
        if response_1.status_code != 200:
            message = f"first ingestion POST returned {response_1.status_code}: {response_1.text}"
            pytest.fail(message)

        disposable_graph = select_graph(db, _DISPOSABLE_GRAPH)
        snapshot_1 = _full_snapshot(disposable_graph)

        response_2 = client.post(_ENDPOINT, json=_SEED_REQUEST)
        snapshot_2 = _full_snapshot(disposable_graph)

        dangling_response = client.post(_ENDPOINT, json=_DANGLING_REQUEST)

        dangling_native_count = _count(
            select_graph(db, native_graph_name(_DANGLING_SHORT_NAME)), _COUNT_ALL
        )
        dangling_baseline_count = _count(
            select_graph(db, baseline_graph_name(_DANGLING_SHORT_NAME)), _COUNT_ALL
        )
        dangling_in_disposable = _count(
            disposable_graph,
            "MATCH (n:RegulatoryInstrument {id: $id}) RETURN count(n)",
            {"id": _DANGLING_RID},
        )

        real_after = _count(select_graph(db, _REAL_GRAPH), _COUNT_ALL)

        yield _InternalCapstoneData(
            response_1_status=response_1.status_code,
            response_1_body=cast("dict[str, object]", response_1.json()),
            response_2_status=response_2.status_code,
            response_2_body=cast("dict[str, object]", response_2.json()),
            snapshot_1=snapshot_1,
            snapshot_2=snapshot_2,
            dangling_status=dangling_response.status_code,
            dangling_body=cast("dict[str, object]", dangling_response.json()),
            dangling_native_count=dangling_native_count,
            dangling_baseline_count=dangling_baseline_count,
            dangling_regulatory_instrument_in_disposable=dangling_in_disposable,
            real_policy_system_before=real_before,
            real_policy_system_after=real_after,
            disposable_graph=disposable_graph,
        )
    finally:
        monkeypatch.undo()
        for graph_name in (
            _DISPOSABLE_GRAPH,
            native_graph_name(_SEED_SHORT_NAME),
            baseline_graph_name(_SEED_SHORT_NAME),
            native_graph_name(_DANGLING_SHORT_NAME),
            baseline_graph_name(_DANGLING_SHORT_NAME),
        ):
            with contextlib.suppress(Exception):
                db.select_graph(graph_name).delete()
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


def test_internal_ingestion_populates_governance_spine(capstone: _InternalCapstoneData) -> None:
    """AC-BI-004 through AC-BI-007: the internal POST runs both stages against real infra.

    A ``RegulatoryInstrument{source_type: 'internal'}`` exists; every
    Capability has >=1 ``GOVERNED_BY`` edge; every Policy has >=1
    ``SUPPORTED_BY`` edge; every Control belongs to EXACTLY ONE Standard;
    the full Regulation -> Role -> Obligation -> Capability -> Policy ->
    Standard -> Control chain is reachable in one traversal (the "back to
    query" proof). The pipeline is now two stages, not three --
    ``governance_derivation`` no longer exists (GH #76).
    """
    assert capstone.response_1_status == 200
    assert capstone.response_1_body["regulatory_instrument_id"] == _SEED_RID
    assert capstone.response_1_body["source"] == "internal"
    assert [
        cast("dict[str, object]", stage)["stage"]
        for stage in cast("list[object]", capstone.response_1_body["stages"])
    ] == ["internal_ingestion", "merge"]
    assert all(
        cast("dict[str, object]", stage)["status"] == "succeeded"
        for stage in cast("list[object]", capstone.response_1_body["stages"])
    )

    graph = capstone.disposable_graph
    params: dict[str, object] = {"id": _SEED_RID}
    assert (
        _count(
            graph,
            "MATCH (n:RegulatoryInstrument {id: $id, source_type: 'internal'}) RETURN count(n)",
            params,
        )
        == 1
    ), "no internal-source RegulatoryInstrument in the merged graph"

    # --- every Capability has >=1 GOVERNED_BY edge ---
    total_capabilities = _count(graph, "MATCH (c:Capability) RETURN count(c)")
    capabilities_with_governed_by = _count(
        graph, "MATCH (c:Capability)-[:GOVERNED_BY]->(:Policy) RETURN count(DISTINCT c)"
    )
    assert total_capabilities >= 1, "no Capability nodes in the merged graph"
    assert capabilities_with_governed_by == total_capabilities, (
        f"{total_capabilities - capabilities_with_governed_by} of {total_capabilities} "
        "Capabilities have no GOVERNED_BY edge"
    )

    # --- every Policy has >=1 SUPPORTED_BY edge ---
    total_policies = _count(graph, "MATCH (p:Policy) RETURN count(p)")
    policies_with_supported_by = _count(
        graph, "MATCH (p:Policy)-[:SUPPORTED_BY]->(:Standard) RETURN count(DISTINCT p)"
    )
    assert total_policies >= 1, "no Policy nodes in the merged graph"
    assert policies_with_supported_by == total_policies, (
        f"{total_policies - policies_with_supported_by} of {total_policies} "
        "Policies have no SUPPORTED_BY edge"
    )

    # --- every Control belongs to EXACTLY ONE Standard (IMPLEMENTED_BY cardinality) ---
    control_rows = _query_rows(
        graph,
        "MATCH (c:Control) OPTIONAL MATCH (s:Standard)-[:IMPLEMENTED_BY]->(c) "
        "RETURN c.id, count(s)",
    )
    assert control_rows, "no Control nodes in the merged graph"
    for control_id_value, standard_count in control_rows:
        assert cast("int", standard_count) == 1, (
            f"Control {control_id_value!r} belongs to {standard_count} Standards, "
            "expected exactly 1"
        )

    # --- full chain traversal reachable (the "back to query" proof) ---
    assert (
        _count(
            graph,
            "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)"
            "-[:REQUIRES]->(:Capability)-[:GOVERNED_BY]->(:Policy)-[:SUPPORTED_BY]->(:Standard)"
            "-[:IMPLEMENTED_BY]->(:Control) RETURN count(*)",
            params,
        )
        >= 1
    ), "no Regulation->Role->Obligation->Capability->Policy->Standard->Control chain in the graph"


def test_second_identical_ingestion_is_structural_no_op(capstone: _InternalCapstoneData) -> None:
    """Re-posting the exact same fixture is a structural no-op -- governance layer included.

    The Regulation/Role/Requirement/Obligation/Capability layer was already
    CODE-guaranteed identical across both runs (minted from the fixture's
    own literal names via pure-hash identity functions, never LLM output).

    GH #76 extends that same guarantee to Policy/Standard/Control: they are
    now human-authored in the submitted document and minted by
    ``domain_mapper.identity.policy_id``/``standard_id``/``control_id`` --
    pure functions of the fixture's own titles, not LLM output -- so
    re-ingesting the identical fixture twice converges the governance layer
    too, by exact canonical id. Unlike the old LLM-derived version of this
    test (which had to ``xfail``-bound the governance layer's cross-run
    convergence against live wording non-determinism), no ``xfail`` is
    needed here: convergence is asserted unconditionally, over the WHOLE
    snapshot (deterministic layer and governance layer alike).
    """
    assert capstone.response_2_status == 200
    assert capstone.response_2_body["regulatory_instrument_id"] == _SEED_RID

    assert capstone.snapshot_2 == capstone.snapshot_1, (
        "re-ingesting the identical authored-governance fixture changed the merged graph's "
        f"per-label/per-edge-type counts: {capstone.snapshot_1} -> {capstone.snapshot_2}"
    )
    assert capstone.snapshot_1["node:RegulatoryInstrument"] == 1


def test_dangling_edge_fixture_fails_closed_against_real_pipeline(
    capstone: _InternalCapstoneData,
) -> None:
    """AC-BI-008/009: the dangling-edge fixture 502s against the REAL pipeline, with zero writes.

    Proven directly against real FalkorDB -- not the fast-fake proof
    ``tests/ingestion/adapters/internal_seed/test_persist.py`` already
    gives, and not merely the route-level fake-dependency proof
    ``tests/api/test_ingestions_internal.py`` gives either. Retargeted at
    the new ``authored-governance-dangling-edge.json`` fixture (a dangling
    ``SUPPORTED_BY`` edge), the new edge type this issue introduces.
    """
    assert capstone.dangling_status == 502
    error = cast("dict[str, object]", capstone.dangling_body["error"])
    assert error["failing_stage"] == "internal_ingestion"
    assert error["code"]
    assert error["message"]

    assert capstone.dangling_native_count == 0, (
        "the dangling-edge fixture's own native graph gained a write despite failing closed"
    )
    assert capstone.dangling_baseline_count == 0, (
        "the dangling-edge fixture's own baseline graph gained a write despite failing closed"
    )
    assert capstone.dangling_regulatory_instrument_in_disposable == 0, (
        "the dangling-edge fixture's RegulatoryInstrument leaked into the merged single-tenant "
        "graph despite the internal_ingestion stage failing before merge ever ran"
    )


def test_real_policy_system_graph_node_count_unchanged(capstone: _InternalCapstoneData) -> None:
    """Safety property: the real ``policy_system`` graph is provably untouched by this run."""
    assert capstone.real_policy_system_after == capstone.real_policy_system_before
