"""Increment 21 (#51a) -- the live external ingestion capstone.

``@pytest.mark.falkordb_live @pytest.mark.llm_live``: drives the real
``POST /ingestions`` route with a ``source: "catalog"`` request for the CRA
(CELEX ``32024R2847`` -- the smallest curated instrument) against real
FalkorDB and real Azure OpenAI, and asserts the merged compliance spine
lands in a **disposable** single-tenant graph
(``policy_system_api_capstone_test``) -- never the real, shared
``policy_system`` graph, whose node count is read before and after and
asserted unchanged (AC-BI-005).

**Bounded, not exhaustive** (cost/time mitigation, mirroring
``tests/domain_mapper/test_live_capstone.py``): the Domain Mapping Adapter is
wrapped in ``_LimitedDomainMappingAdapter`` -- only the first
``_EXTRACTION_UNIT_LIMIT`` ``ExtractionUnit``s (document order) are extracted.
This is injected purely through ``app.dependency_overrides`` +
``dataclasses.replace`` on the default ``PipelineDependencies`` -- no
production-code change.

All fixtures/helpers are local to this module (``tests/api/conftest.py`` is
owned by a concurrent increment). The autouse ``_isolate_logging`` fixture
``delenv``s ``PS_LLMINTERFACE_MODEL``/``_EMBED_MODEL`` for every test, so both
are captured at import time (before collection) and re-set via ``monkeypatch``
before the app's build-time ``load_config()`` runs -- otherwise the route hits
``IngestionConfigIncompleteError`` (503) and never runs the pipeline (M3).

**AC-BI-003 coverage.** The first half -- stages auto-chain, each consuming the
prior stage's output with no manual step -- is proven end to end by
``test_catalog_celex_ingestion_populates_merged_spine`` (the merged spine only
exists if all four stages ran in sequence). The second half -- re-ingesting the same
CELEX -- is no longer a re-extraction that must converge: since issue #193 the identity
check refuses a CELEX already in the graph with ``409 celex_already_ingested`` before any
stage runs, so the second POST changes nothing. That makes the graph snapshot after the
second POST exactly equal to the first, deterministically (the old LLM-drift ``xfail``,
issue #34, can no longer be reached through this route).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from api._fakes import install_compliance_officer_grant
from api._live_config import live_app_config
from ps_service.api.dependencies import provide_pipeline_dependencies
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.api.ingestion_orchestration import build_default_pipeline_dependencies
from ps_service.domain_mapper.adapters.cellar_eli import CellarEliDomainMappingAdapter
from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

    from ps_service.api.ingestion_orchestration import GraphHandle, PipelineDependencies
    from ps_service.company_merge.falkordb_client import GraphHandle as CompanyMergeGraphHandle
    from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
    from ps_service.domain_mapper.falkordb_client import GraphHandle as DomainMappingGraphHandle
    from ps_service.domain_mapper.models import ExtractionUnit

# Captured at import time, before the autouse `_isolate_logging` fixture strips them.
_CHAT_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")
_EMBED_MODEL = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")

_EXTRACTION_UNIT_LIMIT = 15
_DISPOSABLE_GRAPH = "policy_system_api_capstone_test"
_REAL_GRAPH = "policy_system"
_ENDPOINT = "/ingestions"
_CRA_CELEX = "32024R2847"
_CRA_REGULATORY_INSTRUMENT_ID = "CRA-1.0"
_CRA_REQUEST: dict[str, str] = {"source": "catalog", "celex": _CRA_CELEX, "short_name": "CRA"}
_COUNT_ALL = "MATCH (n) RETURN count(n)"
# The ingestion's own per-instrument graphs (named from the request's `short_name`): Company Merge
# reads `{short}_baseline`, so content a sibling module (or an earlier run) left in them -- e.g. the
# export capstone's 2-dimensional fake embeddings -- would be merged with this run's 3072-dimension
# real ones. This run owns them for its duration: dropped before it starts and after it ends.
_INSTRUMENT_GRAPHS = ("cra_baseline", "cra_native")

# Fixed label / relationship-type allow-list -- safe to interpolate into Cypher
# (L2: labels/rel-types may come from fixed module-level constants).
_NODE_LABELS = ("RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability")
_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES")

pytestmark = [
    pytest.mark.falkordb_live,
    pytest.mark.postgres_live,
    pytest.mark.llm_live,
    pytest.mark.skipif(
        not _CHAT_MODEL or not _EMBED_MODEL,
        reason="requires .env sourced (PS_LLMINTERFACE_MODEL/_EMBED_MODEL, AZURE_*)",
    ),
]


class _LimitedDomainMappingAdapter:
    """Wraps a real ``DomainMappingAdapter``, capping returned units to the first ``limit``.

    Satisfies ``DomainMappingAdapter`` structurally -- no production change is
    needed for this capstone's bounding requirement. Copied from
    ``tests/domain_mapper/test_live_capstone.py``.
    """

    def __init__(self, inner: DomainMappingAdapter, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    def read_native_units(
        self, graph: DomainMappingGraphHandle, regulatory_instrument_id: str
    ) -> tuple[ExtractionUnit, ...]:
        """Return the inner adapter's units, truncated to the first ``limit``."""
        return self._inner.read_native_units(graph, regulatory_instrument_id)[: self._limit]


@dataclass(frozen=True, slots=True)
class _CapstoneData:
    """Everything the three capstone assertions read, captured by the one shared run."""

    response_1_status: int
    response_1_body: dict[str, object]
    response_2_status: int
    response_2_text: str
    snapshot_1: dict[str, int]
    snapshot_2: dict[str, int]
    real_policy_system_before: int
    real_policy_system_after: int
    disposable_graph: CompanyMergeGraphHandle


def _limited_mapping_adapter() -> DomainMappingAdapter:
    """Zero-arg factory for the bounded Cellar/ELI Domain Mapping Adapter."""
    return _LimitedDomainMappingAdapter(CellarEliDomainMappingAdapter(), _EXTRACTION_UNIT_LIMIT)


def _limited_dependencies() -> PipelineDependencies:
    """The production ``PipelineDependencies`` with only ``adapters.mapping`` bounded."""
    default = build_default_pipeline_dependencies()
    return replace(default, adapters=replace(default.adapters, mapping=_limited_mapping_adapter))


def _count(graph: GraphHandle, query: str, params: dict[str, object] | None = None) -> int:
    """Run a ``RETURN count(...)`` query and return the single integer result."""
    rows = cast("list[list[object]]", graph.query(query, params=params).result_set)
    return cast("int", rows[0][0])


def _snapshot(graph: GraphHandle) -> dict[str, int]:
    """Per-label node counts + per-relationship-type edge counts for one graph."""
    counts = {
        f"node:{label}": _count(graph, f"MATCH (n:{label}) RETURN count(n)")
        for label in _NODE_LABELS
    }
    counts.update(
        {
            f"edge:{rel}": _count(graph, f"MATCH ()-[r:{rel}]->() RETURN count(r)")
            for rel in _EDGE_TYPES
        }
    )
    return counts


def _delete_graph_if_exists(db: FalkorDB, name: str) -> None:
    """Drop ``name`` from FalkorDB if it is currently present (best effort)."""
    if name in db.list_graphs():
        db.select_graph(name).delete()


@pytest.fixture(scope="module")
def capstone(
    tmp_path_factory: pytest.TempPathFactory, provisioned_graph_log: Provisioned
) -> Iterator[_CapstoneData]:
    """Run the real catalog ingestion pipeline once (twice, for the no-op check) and capture state.

    The Domain Mapper stages emit through the process-wide default emitter (the
    route passes no explicit emitter), so a real ``configure()``d facade is
    installed for the run. ``reset_for_tests()`` leaves the module-global
    ``_atexit_registered`` guard set; it is saved and restored here so this
    live-only module can never poison ``tests/logging``'s once-only ``atexit``
    assertion if the two are ever collected together.

    Yields:
        The captured :class:`_CapstoneData`.
    """
    assert _CHAT_MODEL is not None  # narrowed by the module skipif
    assert _EMBED_MODEL is not None

    monkeypatch = pytest.MonkeyPatch()
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", _CHAT_MODEL)
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", _EMBED_MODEL)
    if not os.environ.get("PS_COMPANYMERGE_SIMILARITY_THRESHOLD"):
        monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.85")
    monkeypatch.setenv("PS_FALKORDB_GRAPH", _DISPOSABLE_GRAPH)

    # Built after the env above is set: it layers the provisioned state Postgres (graph log and
    # audit trail) and the local-test auth bypass over `load_config()`.
    config = live_app_config(provisioned_graph_log)
    db = connect_from_config(config)
    real_before = _count(select_graph(db, _REAL_GRAPH), _COUNT_ALL)

    for stale_graph in (_DISPOSABLE_GRAPH, *_INSTRUMENT_GRAPHS):
        _delete_graph_if_exists(db, stale_graph)
    facade.configure(log_path=tmp_path_factory.mktemp("api_capstone") / "capstone.jsonl")

    install_compliance_officer_grant(monkeypatch, granted=True)  # the catalog route is role-gated
    app = create_app(config)
    app.dependency_overrides[provide_pipeline_dependencies] = _limited_dependencies
    # Every pipeline stage writes through the Graph Write Gateway (#208-#210). `TestClient` is not
    # entered, so the app lifespan never installs it: install the real one (PS state Postgres and
    # FalkorDB) the way a deployed service does. Not run in the implementation sandbox.
    install_graph_write_gateway(build_default_graph_write_gateway(config))

    try:
        client = TestClient(app, raise_server_exceptions=False)
        response_1 = client.post(_ENDPOINT, json=_CRA_REQUEST)
        if response_1.status_code != 200:
            message = f"first ingestion POST returned {response_1.status_code}: {response_1.text}"
            pytest.fail(message)

        disposable_graph = select_graph(db, _DISPOSABLE_GRAPH)
        snapshot_1 = _snapshot(disposable_graph)

        response_2 = client.post(_ENDPOINT, json=_CRA_REQUEST)
        snapshot_2 = _snapshot(disposable_graph)

        real_after = _count(select_graph(db, _REAL_GRAPH), _COUNT_ALL)

        yield _CapstoneData(
            response_1_status=response_1.status_code,
            response_1_body=cast("dict[str, object]", response_1.json()),
            response_2_status=response_2.status_code,
            response_2_text=response_2.text,
            snapshot_1=snapshot_1,
            snapshot_2=snapshot_2,
            real_policy_system_before=real_before,
            real_policy_system_after=real_after,
            disposable_graph=disposable_graph,
        )
    finally:
        install_graph_write_gateway(None)
        monkeypatch.undo()
        for graph_name in (_DISPOSABLE_GRAPH, *_INSTRUMENT_GRAPHS):
            with contextlib.suppress(Exception):
                db.select_graph(graph_name).delete()
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


def test_catalog_celex_ingestion_populates_merged_spine(capstone: _CapstoneData) -> None:
    """AC-BI-002: the catalog POST runs all four stages and writes the spine to the merged graph."""
    assert capstone.response_1_status == 200
    assert capstone.response_1_body["regulatory_instrument_id"] == _CRA_REGULATORY_INSTRUMENT_ID
    assert capstone.response_1_body["source"] == "catalog"
    assert [
        cast("dict[str, object]", stage)["stage"]
        for stage in cast("list[object]", capstone.response_1_body["stages"])
    ] == ["ingestion", "extraction", "derivation", "merge"]

    graph = capstone.disposable_graph
    params: dict[str, object] = {"id": _CRA_REGULATORY_INSTRUMENT_ID}
    assert _count(graph, "MATCH (n:RegulatoryInstrument {id: $id}) RETURN count(n)", params) == 1
    for label in ("Role", "Requirement", "Obligation", "Capability"):
        assert _count(graph, f"MATCH (n:{label}) RETURN count(n)") >= 1, label

    assert (
        _count(
            graph,
            "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)"
            "-[:REQUIRES]->(:Capability) RETURN count(*)",
            params,
        )
        >= 1
    ), "no Regulation->Role->Obligation->Capability spine traversal in the merged graph"
    assert (
        _count(
            graph,
            "MATCH (:RegulatoryInstrument {id: $id})-[:EXPRESSES]->(:Requirement)"
            "-[:SATISFIED_BY]->(:Obligation) RETURN count(*)",
            params,
        )
        >= 1
    ), "no Regulation->Requirement->Obligation traversal in the merged graph"


def test_second_catalog_ingestion_is_refused_and_changes_nothing(capstone: _CapstoneData) -> None:
    """AC-BI-003 (re-ingestion half): the same CELEX is refused 409 and the graph is untouched.

    The identity check (issue #193) rejects a CELEX already in the graph under any
    ``short_name`` before the Domain Mapper or Company Merge run, so no node or edge of any
    label changes -- an exact snapshot comparison, not a drift-tolerant one.
    """
    assert capstone.response_2_status == 409, capstone.response_2_text
    assert '"celex_already_ingested"' in capstone.response_2_text
    assert capstone.snapshot_2 == capstone.snapshot_1
    assert capstone.snapshot_2["node:RegulatoryInstrument"] == 1


def test_real_policy_system_graph_node_count_unchanged(capstone: _CapstoneData) -> None:
    """AC-BI-005: the shared ``policy_system`` graph is provably untouched by the whole run."""
    assert capstone.real_policy_system_after == capstone.real_policy_system_before
