"""Tests for `restore_instrument`'s D14/AC-BI-016 audit log entries (PLAN.md
Slice 5.9, MA2's exact `emit_log_entry` call shape).

AC-BI-004 (issue #163, IMPL_SLICE_8.md): the staging collaborators
`stage_graph`/`stage_and_finalize_policy_system_leg`/`_run_baseline_merge`
used to be monkeypatched into canned stubs here (a stateful internal
collaborator replaced by a raw mock). They now run FOR REAL, backed by an
in-memory FalkorDB-shaped test-data builder (`_FakeStagingFalkorDB` below) --
a generic, stateful fake recognizing exactly the query shapes the real
`ps_service.restore.populate.populate_graph` (writes) and
`ps_service.company_merge.graph_reader`/`graph_writer` (reads/writes)
collaborators issue, following the same "structural fake, not a mock"
precedent `test_restore_instrument_classification_passthrough.py`'s own
`_FakeBaselineGraph`/`_FakeSingleTenantGraph` already established for
`_run_baseline_merge`'s snapshot-side collaborators. `_FakeSingleTenantGraph`
here is that same file's class, duplicated per this test suite's existing
per-component convention, extended with a `.copy()` (`GRAPH.COPY`) method
`stage_and_finalize_policy_system_leg`'s WATCH-guarded finalize loop needs
that the classification-passthrough tests never exercised (they call
`_run_baseline_merge` directly, bypassing staging).

`raw_connection` is no longer monkeypatched either: `_FakeStagingFalkorDB`
carries a real (fake) `.connection` supporting `.pipeline()`/`.rename()`/
`.delete()`, so the real `ps_service.export.falkordb_connection.
raw_connection` accessor resolves it without patching.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import (
    InstrumentManifest,
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
)
from ps_service.export.serialize import checksum_bytes, to_json_bytes
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from falkordb import FalkorDB

_INSTRUMENT_ID = "RT59-1.0"
_ACTOR = "test-actor-5-9"
_EMPTY_GRAPH_BYTES = to_json_bytes(SerializedGraph(nodes=(), edges=()))
_SINGLE_TENANT_GRAPH_NAME = "test-single-tenant"


# --------------------------------------------------------------------------
# Fixtures: a generic, stateful in-memory FalkorDB-shaped test-data builder.
# --------------------------------------------------------------------------


class _FakeQueryResult:
    """Satisfies `GraphQueryResult`/the bare `.result_set` shape structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeRegulatoryInstrumentNode:
    """Satisfies `graph_reader._RegulatoryInstrumentNode` structurally -- only
    `.properties` is ever read.
    """

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


_CREATE_NODE_RE = re.compile(r"CREATE \(n:(?P<label>\w+)\) SET n = row")
_MERGE_EDGE_RE = re.compile(
    r"MATCH \(s:(?P<source_label>\w+) \{id: row\.source_id\}\), "
    r"\(t:(?P<target_label>\w+) \{id: row\.target_id\}\) "
    r"MERGE \(s\)-\[r:(?P<rel>\w+)\]->\(t\)"
)
_COUNT_RE = re.compile(r"^MATCH \(n:(?P<label>\w+)\) RETURN count\(n\) AS c$")
_WHOLE_NODE_READ_RE = re.compile(r"^MATCH \(n:(?P<label>\w+) \{id: \$(?P<param>\w+)\}\) RETURN n$")
_PROVENANCE_READ_RE = re.compile(
    r"^MATCH \(r:RegulatoryInstrument \{id: \$regulatory_instrument_id\}\)-\[e:(?P<rel>\w+)\]->"
    r"\(n:(?P<label>\w+)\) RETURN n\.id, e\.(?P<prop>\w+)$"
)
_EDGE_READ_RE = re.compile(
    r"^MATCH \(s:(?P<source_label>\w+)\)-\[:(?P<rel>\w+)\]->\(t:(?P<target_label>\w+)\) "
    r"RETURN s\.id, t\.id$"
)
_NODE_READ_RE = re.compile(
    r"^MATCH \(n:(?P<label>\w+)\)(?P<filter> WHERE n\.status = 'approved')? "
    r"RETURN (?P<cols>n\.\w+(?:, n\.\w+)*)$"
)
# issue #183 -- restore's unfiltered, whole-node governance read (`MATCH (n:Policy) RETURN n`).
_ALL_NODES_READ_RE = re.compile(r"^MATCH \(n:(?P<label>\w+)\) RETURN n$")
_VIVIFY_QUERY = "MATCH (n) WHERE false RETURN n"


class _FakeStagedGraph:
    """A generic, mutable in-memory graph store standing in for one
    FalkorDB-selected staged key (`stage_graph`'s `{short}_native`/
    `{short}_baseline` legs).

    Populated FOR REAL by `ps_service.restore.populate.populate_graph`'s two
    generic write templates (`UNWIND $rows AS row CREATE (n:{label}) SET n =
    row` / the matching edge `MERGE`), and read back FOR REAL by
    `ps_service.company_merge.graph_reader.read_baseline_graph`'s twenty-two
    fixed-literal read queries -- every one of which follows one of four
    regular shapes (a node-column projection, the whole-node RegulatoryInstrument
    read, a bare two-column edge read, or a RegulatoryInstrument-anchored
    provenance-edge read), so one regex-driven dispatcher answers all of
    them generically instead of hand-listing per-label branches. Not a
    general Cypher engine -- recognizes exactly the query shapes these two
    real modules issue, the same "structural fake, not a mock" precedent
    `_FakeBaselineGraph` (below) already established, just backed by
    generically-populated tables instead of constructor-supplied rows.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, dict[str, dict[str, object]]] = {}
        self._edges: dict[tuple[str, str, str], list[tuple[str, str, dict[str, object]]]] = {}

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        params = params or {}
        if q == _VIVIFY_QUERY:
            return _FakeQueryResult([])
        if match := _CREATE_NODE_RE.search(q):
            label = match.group("label")
            table = self._nodes.setdefault(label, {})
            for row in cast("list[dict[str, object]]", params["rows"]):
                table[cast("str", row["id"])] = dict(row)
            return _FakeQueryResult([])
        if match := _MERGE_EDGE_RE.search(q):
            key = (match.group("rel"), match.group("source_label"), match.group("target_label"))
            bucket = self._edges.setdefault(key, [])
            for row in cast("list[dict[str, object]]", params["rows"]):
                bucket.append(
                    (
                        cast("str", row["source_id"]),
                        cast("str", row["target_id"]),
                        dict(cast("dict[str, object]", row["properties"])),
                    )
                )
            return _FakeQueryResult([])
        if match := _COUNT_RE.match(q):
            return _FakeQueryResult([[len(self._nodes.get(match.group("label"), {}))]])
        if match := _WHOLE_NODE_READ_RE.match(q):
            row = self._nodes.get(match.group("label"), {}).get(
                cast("str", params[match.group("param")])
            )
            if row is None:
                return _FakeQueryResult([])
            return _FakeQueryResult([[_FakeRegulatoryInstrumentNode(dict(row))]])
        if match := _PROVENANCE_READ_RE.match(q):
            # `_edges` is keyed (rel, source_label, target_label); provenance
            # edges are always sourced from RegulatoryInstrument.
            key = (match.group("rel"), "RegulatoryInstrument", match.group("label"))
            wanted_source = cast("str", params["regulatory_instrument_id"])
            prop = match.group("prop")
            return _FakeQueryResult(
                [
                    [target_id, edge_properties.get(prop)]
                    for source_id, target_id, edge_properties in self._edges.get(key, [])
                    if source_id == wanted_source
                ]
            )
        if match := _EDGE_READ_RE.match(q):
            key = (match.group("rel"), match.group("source_label"), match.group("target_label"))
            return _FakeQueryResult(
                [
                    [source_id, target_id]
                    for source_id, target_id, _props in self._edges.get(key, [])
                ]
            )
        if match := _ALL_NODES_READ_RE.match(q):
            return _FakeQueryResult(
                [
                    [_FakeRegulatoryInstrumentNode(dict(row))]
                    for row in self._nodes.get(match.group("label"), {}).values()
                ]
            )
        if match := _NODE_READ_RE.match(q):
            label = match.group("label")
            approved_only = bool(match.group("filter"))
            columns = [c.split(".", 1)[1] for c in match.group("cols").split(", ")]
            rows: list[object] = []
            for row in self._nodes.get(label, {}).values():
                if approved_only and row.get("status") != "approved":
                    continue
                rows.append([row.get(column) for column in columns])
            return _FakeQueryResult(rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


class _FakeSingleTenantGraph:
    """Answers Capability/Policy existing-canonical-index reads and every
    `graph_writer` write query `_run_baseline_merge` issues against the
    single-tenant/snapshot graph -- mirrors `test_restore_instrument_
    classification_passthrough.py`'s own `_FakeSingleTenantGraph` exactly
    (duplicated per this test suite's per-component fake convention), plus
    a `.copy()` (`GRAPH.COPY`) method that file never needed: its tests call
    `_run_baseline_merge` directly, bypassing `staging.
    stage_and_finalize_policy_system_leg`'s own `snapshot_single_tenant`
    step, which this file's tests -- running the REAL staging orchestration
    -- do not.
    """

    def __init__(
        self,
        *,
        registry: dict[str, object] | None = None,
        capability_rows: list[object] | None = None,
        policy_rows: list[object] | None = None,
        practice_area_rows: list[object] | None = None,
        risk_path_rows: list[object] | None = None,
    ) -> None:
        self._registry = registry
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows or []:
            row_list = list(cast("list[object]", row))
            self._capabilities[cast("str", row_list[0])] = row_list
        self._policies: dict[str, list[object]] = {}
        for row in policy_rows or []:
            row_list = list(cast("list[object]", row))
            self._policies[cast("str", row_list[0])] = row_list
        self._standards: dict[str, list[object]] = {}
        self._controls: dict[str, list[object]] = {}
        self._practice_areas: dict[str, dict[str, object]] = {}
        for row in practice_area_rows or []:
            row_list = list(cast("list[object]", row))
            self._practice_areas[cast("str", row_list[0])] = dict(
                cast("dict[str, object]", row_list[1])
            )
        self._risk_paths: dict[str, dict[str, object]] = {}
        for row in risk_path_rows or []:
            row_list = list(cast("list[object]", row))
            self._risk_paths[cast("str", row_list[0])] = dict(
                cast("dict[str, object]", row_list[1])
            )
        self.calls: list[object] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append((q, params))
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "(n:Policy) RETURN n.id, n.title, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._policies.values()])
        if "MERGE (n:Standard {id: $id}) ON CREATE SET n += $properties" in q:
            self._set(self._standards, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:Control {id: $id}) ON CREATE SET n += $properties" in q:
            self._set(self._controls, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:Capability {id: $id}) ON CREATE SET" in q:
            self._mint(self._capabilities, params, "name")
            return _FakeQueryResult([])
        if "MERGE (n:Policy {id: $id}) ON CREATE SET" in q:
            self._mint(self._policies, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:PracticeArea {id: $id}) ON CREATE SET" in q:
            self._mint_properties(self._practice_areas, params)
            return _FakeQueryResult([])
        if "MERGE (n:RiskPath {id: $id}) ON CREATE SET" in q:
            self._mint_properties(self._risk_paths, params)
            return _FakeQueryResult([])
        if "MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._capabilities, params)
            return _FakeQueryResult([])
        if "MATCH (n:Policy {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._policies, params)
            return _FakeQueryResult([])
        if q == "UNWIND $ids AS id MATCH (n {id: id}) RETURN id":
            assert params is not None
            requested_ids = cast("list[str]", params["ids"])
            known_ids = (
                set(self._capabilities)
                | set(self._policies)
                | set(self._standards)
                | set(self._controls)
                | set(self._practice_areas)
                | set(self._risk_paths)
            )
            return _FakeQueryResult([[rid] for rid in requested_ids if rid in known_ids])
        return _FakeQueryResult([[0]])  # any other write (RegulatoryInstrument, edges, ...)

    def copy(self, clone: str) -> object:
        """`GRAPH.COPY` stand-in: snapshot this graph's full state under `clone`.

        Registers the clone into the shared registry `_FakeStagingFalkorDB`
        passed at construction, so a later `db.select_graph(clone)` returns
        this same snapshot object -- `stage_and_finalize_policy_system_leg`'s
        own `snapshot_single_tenant` -> `run_offline_merge(snapshot_name)` ->
        `select_company_merge_graph(db, snapshot_name)` call chain.
        """
        snapshot = _FakeSingleTenantGraph(
            registry=self._registry,
            capability_rows=[list(row) for row in self._capabilities.values()],
            policy_rows=[list(row) for row in self._policies.values()],
            practice_area_rows=[[pid, dict(props)] for pid, props in self._practice_areas.items()],
            risk_path_rows=[[rid, dict(props)] for rid, props in self._risk_paths.items()],
        )
        snapshot._standards = dict(self._standards)  # same-class internal copy
        snapshot._controls = dict(self._controls)  # same-class internal copy
        if self._registry is not None:
            self._registry[clone] = snapshot
        return snapshot

    def _set(
        self, table: dict[str, list[object]], params: dict[str, object] | None, text_key: str
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [node_id, properties.get(text_key), properties.get("embedding")]

    def _mint(
        self, table: dict[str, list[object]], params: dict[str, object] | None, text_key: str
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [node_id, properties.get(text_key), properties.get("embedding")]

    def _mint_properties(
        self, table: dict[str, dict[str, object]], params: dict[str, object] | None
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = dict(properties)

    def _backfill(self, table: dict[str, list[object]], params: dict[str, object] | None) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        row = table.get(node_id)
        if row is None or row[2] is not None:
            return
        row[2] = params["embedding"]


class _FakeWatchablePipeline:
    """Satisfies `_WatchablePipeline` structurally: a single-writer fake, so
    `watch()` never observes a conflicting change and `execute()` always
    succeeds -- these tests exercise the SUCCESS/single-real-failure paths,
    not the concurrency-retry loop itself (already proven live by
    `test_restore_instrument_concurrency_live.py`).
    """

    def __init__(self, registry: dict[str, object]) -> None:
        self._registry = registry
        self._queued: list[tuple[str, str]] = []

    def watch(self, *names: str) -> None:
        del names

    def multi(self) -> None:
        pass

    def rename(self, src: str, dst: str) -> object:
        self._queued.append((src, dst))
        return None

    def execute(self) -> list[object]:
        for src, dst in self._queued:
            self._registry[dst] = self._registry.pop(src)
        self._queued = []
        return []

    def reset(self) -> None:
        self._queued = []


class _FakeRawConnection:
    """Satisfies `_RawGraphConnection` structurally, backed by the same
    registry `_FakeStagingFalkorDB.select_graph` reads/writes -- so a
    `RENAME` (finalize) or `DELETE` (discard-on-failure) is a real mutation
    of the same in-memory graph-key store every `select_graph` call sees.
    """

    def __init__(self, registry: dict[str, object]) -> None:
        self._registry = registry

    def rename(self, src: str, dst: str) -> bool:
        self._registry[dst] = self._registry.pop(src)
        return True

    def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self._registry:
                del self._registry[name]
                deleted += 1
        return deleted

    def pipeline(self, *, transaction: bool = True) -> _FakeWatchablePipeline:
        del transaction
        return _FakeWatchablePipeline(self._registry)


class _FakeStagingFalkorDB:
    """The `db: FalkorDB` stand-in for these tests: a real (in-memory) graph-
    key registry, so `stage_graph`/`stage_and_finalize_policy_system_leg`/
    `_run_baseline_merge`/`raw_connection` all run unmocked against it.
    """

    def __init__(self, single_tenant_graph_name: str) -> None:
        self._graphs: dict[str, object] = {}
        self._graphs[single_tenant_graph_name] = _FakeSingleTenantGraph(registry=self._graphs)
        self.connection = _FakeRawConnection(self._graphs)

    def select_graph(self, name: str) -> object:
        if name not in self._graphs:
            self._graphs[name] = _FakeStagedGraph()
        return self._graphs[name]


def _fake_db() -> FalkorDB:
    return cast("FalkorDB", _FakeStagingFalkorDB(_SINGLE_TENANT_GRAPH_NAME))


# --------------------------------------------------------------------------
# Artifact builders
# --------------------------------------------------------------------------


def _manifest(*, baseline_bytes: bytes) -> InstrumentManifest:
    return InstrumentManifest(
        instrument_id=_INSTRUMENT_ID,
        celex=None,
        title="RT59",
        short_name="RT59",
        version="1.0",
        source_type="internal",
        jurisdiction=None,
        schema_version=DOMAIN_SCHEMA_VERSION,
        exported_at="2026-09-04T00:00:00Z",
        baseline_sha256=checksum_bytes(baseline_bytes),
        native_sha256=checksum_bytes(_EMPTY_GRAPH_BYTES),
    )


def _artifact(*, baseline_bytes: bytes = _EMPTY_GRAPH_BYTES) -> RestoreArtifact:
    return RestoreArtifact(
        manifest=_manifest(baseline_bytes=baseline_bytes),
        baseline_blob=baseline_bytes,
        native_blob=_EMPTY_GRAPH_BYTES,
    )


def _restore_log_entries(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if row["component"] == "restore" and row["action"] == "restore_instrument"
    ]


def test_succeeded_entry_carries_caller_and_schema_version(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    restore_instrument(
        _artifact(),
        db=_fake_db(),
        single_tenant_graph_name=_SINGLE_TENANT_GRAPH_NAME,
        similarity_threshold=0.9,
        actor=_ACTOR,
        emitter=emitter,
    )
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "succeeded"]
    for entry in entries:
        # `emit_log_entry`'s `extra` mapping is flattened directly into the
        # JSON payload (logging/models.py::LogEntry.to_json_line), so
        # "caller"/"schema_version" are top-level keys, not nested under an
        # "extra" key.
        assert entry["entity_id"] == _INSTRUMENT_ID
        assert entry["caller"] == _ACTOR
        assert entry["schema_version"] == DOMAIN_SCHEMA_VERSION
        assert "actor" not in entry  # MA2's explicit correction: never extra["actor"]
        # Issue #125/D-AUDIT: the upload path (this call passes no `source`)
        # must keep a byte-identical audit log shape -- no "source" key at all.
        assert "source" not in entry


_SOURCE_URL = (
    "https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/curated-content"
)


def test_started_and_succeeded_entries_carry_source_when_given(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Issue #125/D-AUDIT/AC-BI-011: `run_restoration_from_catalog_source` passes the
    resolved effective source URL, which every emitted audit log entry then carries.
    """
    emitter, log_path = make_emitter()

    restore_instrument(
        _artifact(),
        db=_fake_db(),
        single_tenant_graph_name=_SINGLE_TENANT_GRAPH_NAME,
        similarity_threshold=0.9,
        actor=_ACTOR,
        emitter=emitter,
        source=_SOURCE_URL,
    )
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "succeeded"]
    for entry in entries:
        assert entry["source"] == _SOURCE_URL


_CLASSIFICATION_COUNTS: dict[str, int] = {
    "practice_area_count": 2,
    "risk_path_count": 1,
    "covers_count": 3,
    "owns_count": 1,
    "mitigated_by_count": 1,
    "verified_by_count": 2,
}


def _classification_baseline_graph() -> SerializedGraph:
    """A real, non-empty internal-sourced baseline carrying exactly the
    PracticeArea/RiskPath/classification-edge content `_CLASSIFICATION_
    COUNTS` below expects: 2 PracticeArea, 1 RiskPath, 3 COVERS, 1 OWNS, 1
    MITIGATED_BY, 2 VERIFIED_BY -- staged and merged for real via
    `stage_graph`/`_run_baseline_merge` (no canned-return stub), so
    `restore_instrument`'s own audit log wiring is proven against the SAME
    real six-key `graph_writer.classification_write_counts` return value
    `test_restore_instrument_classification_passthrough.py` already proves
    correct for `_run_baseline_merge` in isolation.
    """
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": _INSTRUMENT_ID, "title": "RT59"}
            ),
            SerializedNode(
                label="PracticeArea",
                properties={"id": "pa_1", "name": "PA One", "status": "active"},
            ),
            SerializedNode(
                label="PracticeArea",
                properties={"id": "pa_2", "name": "PA Two", "status": "active"},
            ),
            SerializedNode(
                label="RiskPath", properties={"id": "rp_1", "name": "RP One", "status": "active"}
            ),
            SerializedNode(
                label="Capability", properties={"id": "cap_1", "name": "Cap One", "confidence": 0.9}
            ),
            SerializedNode(
                label="Capability", properties={"id": "cap_2", "name": "Cap Two", "confidence": 0.9}
            ),
            SerializedNode(
                label="Capability",
                properties={"id": "cap_3", "name": "Cap Three", "confidence": 0.9},
            ),
            SerializedNode(
                label="Policy",
                properties={
                    "id": "policy_1",
                    "title": "Policy One",
                    "status": "approved",
                    "confidence": 0.9,
                },
            ),
            SerializedNode(
                label="Control",
                properties={
                    "id": "ctrl_1",
                    "type": "preventive",
                    "title": "Ctrl One",
                    "implementation_status": "implemented",
                    "confidence": 0.9,
                    "status": "approved",
                },
            ),
            SerializedNode(
                label="Control",
                properties={
                    "id": "ctrl_2",
                    "type": "detective",
                    "title": "Ctrl Two",
                    "implementation_status": "implemented",
                    "confidence": 0.9,
                    "status": "approved",
                },
            ),
        ),
        edges=(
            SerializedEdge(
                relationship_type="COVERS",
                source_label="PracticeArea",
                source_id="pa_1",
                target_label="Capability",
                target_id="cap_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="COVERS",
                source_label="PracticeArea",
                source_id="pa_1",
                target_label="Capability",
                target_id="cap_2",
                properties={},
            ),
            SerializedEdge(
                relationship_type="COVERS",
                source_label="PracticeArea",
                source_id="pa_2",
                target_label="Capability",
                target_id="cap_3",
                properties={},
            ),
            SerializedEdge(
                relationship_type="OWNS",
                source_label="PracticeArea",
                source_id="pa_2",
                target_label="Policy",
                target_id="policy_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="MITIGATED_BY",
                source_label="RiskPath",
                source_id="rp_1",
                target_label="Capability",
                target_id="cap_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="VERIFIED_BY",
                source_label="RiskPath",
                source_id="rp_1",
                target_label="Control",
                target_id="ctrl_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="VERIFIED_BY",
                source_label="RiskPath",
                source_id="rp_1",
                target_label="Control",
                target_id="ctrl_2",
                properties={},
            ),
        ),
    )


def test_succeeded_entry_carries_classification_write_counts(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-011 (restore path): `restore_instrument`'s own `"succeeded"`
    audit entry carries the same six PracticeArea/RiskPath/classification-
    edge write counts the live path's `merge_baseline_graph` attaches to its
    own entry (`graph_writer.classification_write_counts`) -- computed here
    by running the REAL `stage_graph` -> `stage_and_finalize_policy_system_
    leg` -> `_run_baseline_merge` sequence against a real, non-empty
    classification-bearing baseline (no canned-return stub standing in for
    any of them), threaded through `_run_offline_merge`'s `nonlocal
    classification_counts` capture into `_emit_restore_log`'s `extra=`
    (PLAN.md §5 point 2).
    """
    emitter, log_path = make_emitter()
    artifact = _artifact(baseline_bytes=to_json_bytes(_classification_baseline_graph()))

    restore_instrument(
        artifact,
        db=_fake_db(),
        single_tenant_graph_name=_SINGLE_TENANT_GRAPH_NAME,
        similarity_threshold=0.9,
        actor=_ACTOR,
        emitter=emitter,
        owner=("owner@example.com", "https://idp.example/"),
    )
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    succeeded = next(entry for entry in entries if entry["outcome"] == "succeeded")
    for key, expected in _CLASSIFICATION_COUNTS.items():
        assert succeeded[key] == expected
    # issue #183 / AC-BI-014: the draft-governance counts ride on the same entry.
    for key in (
        "governance_policies",
        "governance_standards",
        "governance_controls",
        "governance_status_overridden",
    ):
        assert key in succeeded
    # "started" never carries these -- no classification pass has run yet.
    started = next(entry for entry in entries if entry["outcome"] == "started")
    assert "practice_area_count" not in started


def _baseline_graph_with_unresolvable_classification_endpoint() -> SerializedGraph:
    """A baseline whose one COVERS edge references a Capability id that is
    never persisted anywhere (not minted by Capability dedup -- no
    Capability node exists in this baseline at all -- and never written to
    the snapshot graph by any other pass), naturally producing AC-BI-008's
    real `CompanyMergePersistenceError` from `graph_writer.validate_
    classification_edge_endpoints` -- the same real failure `test_restore_
    instrument_classification_passthrough.py::test_missing_classification_
    edge_endpoint_raises_before_any_classification_edge_write` proves in
    isolation, exercised here through the full real `restore_instrument`
    orchestration instead of an injected sentinel-raise stub.
    """
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": _INSTRUMENT_ID, "title": "RT59"}
            ),
            SerializedNode(
                label="PracticeArea",
                properties={"id": "pa_missing", "name": "PA Missing", "status": "active"},
            ),
        ),
        edges=(
            SerializedEdge(
                relationship_type="COVERS",
                source_label="PracticeArea",
                source_id="pa_missing",
                target_label="Capability",
                target_id="cap_never_persisted_anywhere",
                properties={},
            ),
        ),
    )


def test_failed_entry_recorded_with_no_succeeded_entry_when_merge_step_raises(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    artifact = _artifact(
        baseline_bytes=to_json_bytes(_baseline_graph_with_unresolvable_classification_endpoint())
    )

    with pytest.raises(CompanyMergePersistenceError, match="cap_never_persisted_anywhere"):
        restore_instrument(
            artifact,
            db=_fake_db(),
            single_tenant_graph_name=_SINGLE_TENANT_GRAPH_NAME,
            similarity_threshold=0.9,
            actor=_ACTOR,
            emitter=emitter,
        )
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "failed"]
    assert "succeeded" not in outcomes
    failed_entry = entries[-1]
    assert failed_entry["caller"] == _ACTOR
    assert failed_entry["schema_version"] == DOMAIN_SCHEMA_VERSION
    # Issue #125/D-AUDIT: no `source` was passed -- the upload path's audit
    # log shape stays byte-identical, including on the failure path.
    assert "source" not in failed_entry
