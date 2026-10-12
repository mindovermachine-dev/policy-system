"""Shared test doubles for the `ps_service.change_monitor` test package.

`tests/change_monitor/` is an importable package (it has an `__init__.py`),
so its per-file test modules share these hand-written doubles from here
instead of redeclaring them, mirroring `tests/company_merge/_fakes.py`.

`FakeGraph` / `FakeQueryResult` satisfy `ps_service.change_monitor.
falkordb_client.GraphHandle` / `GraphQueryResult` structurally: a test
scripts the rows each `query()` call returns and asserts the exact Cypher +
params afterwards, including "this component issued no write" (see
`FakeGraph.writes`).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn, Protocol

import redis.exceptions

from change_monitor._gateway_fakes import GraphSnapshot, RigKeyedGraph
from ps_service.api.ingestion_orchestration import PipelineStages
from ps_service.company_merge.models import MergeResult
from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
from ps_service.ingestion.models import IngestResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from change_monitor._gateway_fakes import SuccessionRig
    from ps_service.company_merge.governance_import import GovernanceImport
    from ps_service.ingestion.models import (
        FetchedRegulatoryInstrumentStructure,
        RegulatoryInstrumentMetadata,
    )
    from ps_service.logging import LogEmitter
    from ps_service.logging.emitter import TextSink

_WRITE_CLAUSES = ("MERGE", "CREATE", "SET ", "DELETE", "REMOVE")


class MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(
        self, *, filename: str = ..., fallback: TextSink | None = ...
    ) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class RecordedQuery:
    """One `(query, params)` pair a `FakeGraph` was called with."""

    query: str
    params: dict[str, object] | None


class FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally: one scripted row list.

    Each row is itself a list of column values, in the query's `RETURN`
    order -- the shape a real `falkordb.QueryResult.result_set` has.
    """

    def __init__(self, rows: list[list[object]]) -> None:
        """Script the rows this result yields from its `result_set`."""
        self._rows = rows

    @property
    def result_set(self) -> list[object]:
        """The scripted rows, one list of column values per row."""
        return list(self._rows)


class FakeGraph:
    """Satisfies `GraphHandle` structurally, recording every `query()` call.

    `results` is consumed in order, one `FakeQueryResult` per `query()`
    call; once exhausted every further call yields an empty result. Every
    call is appended to `calls`, so a test can assert the exact Cypher and
    params, and `writes` lets it assert this component issued no write at
    all (the poll is read-only).
    """

    def __init__(self, results: list[FakeQueryResult] | None = None) -> None:
        """Prime the scripted results (default: always an empty result)."""
        self.calls: list[RecordedQuery] = []
        self._results: deque[FakeQueryResult] = deque(results or [])

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Record `(q, params)` and return the next scripted result."""
        self.calls.append(RecordedQuery(q, params))
        if self._results:
            return self._results.popleft()
        return FakeQueryResult([])

    @property
    def writes(self) -> list[RecordedQuery]:
        """Recorded calls whose Cypher contains a write clause."""
        return [call for call in self.calls if _is_write(call.query)]


def _is_write(query: str) -> bool:
    """Whether `query`'s Cypher contains a node/edge/property write clause."""
    upper = query.upper()
    return any(clause in upper for clause in _WRITE_CLAUSES)


class RaisingGraph:
    """Satisfies `GraphHandle`; every `query()` raises `redis.exceptions.RedisError`.

    The exact exception shape a real unreachable/failing FalkorDB instance
    raises mid-call -- drives `succession._execute_query`'s
    `SuccessionPersistenceError` + `mark_unhealthy` path.
    """

    def __init__(self, error: redis.exceptions.RedisError | None = None) -> None:
        """Prime the error each `query()` call raises (default: a connection error)."""
        self._error: redis.exceptions.RedisError = error or redis.exceptions.ConnectionError(
            "Error 111 connecting to FalkorDB"
        )
        self.calls: list[RecordedQuery] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Record `(q, params)` and raise the primed `RedisError`."""
        self.calls.append(RecordedQuery(q, params))
        raise self._error


class FakeAdapter:
    """Satisfies `MetadataFetchingAdapter` (and so `IngestionAdapter`) structurally.

    One canned `FetchedRegulatoryInstrumentStructure` per identifier. Each
    method records into its own per-method log (`structure_calls`,
    `metadata_calls`) and both append to `calls`, the single chronological
    log across both methods; an empty `calls` proves no Cellar access at all.
    `fetch_regulatory_instrument_metadata` returns the canned structure's
    `metadata` without exposing its nodes, as the real adapter's
    metadata-only fetch does.
    """

    def __init__(
        self, structures_by_identifier: dict[str, FetchedRegulatoryInstrumentStructure]
    ) -> None:
        """Prime the canned structures keyed by identifier."""
        self._structures_by_identifier = structures_by_identifier
        self.calls: list[str] = []
        self.structure_calls: list[str] = []
        self.metadata_calls: list[str] = []

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        """Record `identifier` and return its canned structure."""
        self.calls.append(identifier)
        self.structure_calls.append(identifier)
        return self._structures_by_identifier[identifier]

    def fetch_regulatory_instrument_metadata(self, identifier: str) -> RegulatoryInstrumentMetadata:
        """Record `identifier` and return its canned structure's metadata."""
        self.calls.append(identifier)
        self.metadata_calls.append(identifier)
        return self._structures_by_identifier[identifier].metadata


class KeyedGraph:
    """In-memory graph content keyed like a MERGE: nodes `(label, id)`, edges `(type, src, dst)`.

    The shared storage of the stateful graph doubles. `merge_node` / `merge_edge` have Cypher
    `MERGE` semantics (a repeat write of the same key never adds a second node or edge), which is
    what the hermetic retry proof counts on. `snapshot` returns a deep copy for before/after diffs.
    """

    def __init__(self, events: list[str] | None = None) -> None:
        """Start empty, optionally logging writes onto a shared `events` list."""
        self.nodes: dict[tuple[str, str], dict[str, object]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, object]] = {}
        self.calls: list[RecordedQuery] = []
        # Ordered log of the graph WRITES (`write:...`); a test may share this list with a stage
        # recorder so stage calls and writes land on one totally ordered timeline.
        self.events: list[str] = events if events is not None else []

    def merge_node(
        self, label: str, node_id: str, props: dict[str, object], *, on_create_only: bool = False
    ) -> bool:
        """`MERGE (n:label {id}) SET n += props` (or `ON CREATE SET`); `True` when created."""
        key = (label, node_id)
        created = key not in self.nodes
        if created:
            self.nodes[key] = {"id": node_id, **props}
        elif not on_create_only:
            self.nodes[key].update(props)
        return created

    def merge_edge(self, edge_type: str, src: str, dst: str) -> bool:
        """`MERGE (src)-[:edge_type]->(dst)`; `True` when created."""
        key = (edge_type, src, dst)
        created = key not in self.edges
        if created:
            self.edges[key] = {}
        return created

    def count_nodes(self, label: str) -> int:
        """Number of nodes carrying `label`."""
        return sum(1 for node_label, _ in self.nodes if node_label == label)

    def snapshot(self) -> GraphSnapshot:
        """A deep copy of the current content."""
        return GraphSnapshot(
            nodes={key: dict(props) for key, props in self.nodes.items()},
            edges={key: dict(props) for key, props in self.edges.items()},
        )


class StatefulBaselineGraph(KeyedGraph):
    """The `{short}_baseline` graph of a `StatefulGraphSet`: written and read by the stage doubles
    through the keyed API only, never through Cypher (a `query()` call is a test failure).
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Fail loudly: nothing in the sweep path should query the baseline directly."""
        self.calls.append(RecordedQuery(q, params))
        raise AssertionError(f"StatefulBaselineGraph does not model this statement:\n{q}")


@dataclass(slots=True)
class StatefulGraphSet:
    """The three graphs of one instrument family, with one shared ordered `events` timeline.

    `native` and `single_tenant` are the process gateway rig's `{short}_native` and `policy_system`
    graphs, written by the stage doubles the way the stage components leave them (the succession
    itself writes them through the gateway); `baseline` is the Domain Mapper output. All three
    apply MERGE-by-(label, id) semantics, so a repeated stage write never duplicates a node or
    edge. `events` is the rig's own ordered event list (stage calls and log appends).
    """

    rig: SuccessionRig
    events: list[str] = field(init=False)
    native: RigKeyedGraph = field(init=False)
    baseline: StatefulBaselineGraph = field(init=False)
    single_tenant: RigKeyedGraph = field(init=False)

    def __post_init__(self) -> None:
        """Build the three graphs around the rig's timeline."""
        self.events = self.rig.rig.events
        self.native = RigKeyedGraph(self.rig.native)
        self.baseline = StatefulBaselineGraph(self.events)
        self.single_tenant = RigKeyedGraph(self.rig.single_tenant)


@dataclass(frozen=True, slots=True)
class VersionContent:
    """What one instrument version contains, by logical key (the LLM's deterministic stand-in)."""

    obligations: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()


class StatefulStages:
    """Ingest / extract / derive / merge doubles applying real keyed writes to a `StatefulGraphSet`.

    Per version (`rid`): ingestion writes the `RegulatoryInstrument` plus one article into
    native; extraction writes the instrument and one `Requirement` per obligation key into
    baseline; derivation writes version-owned `Obligation` / `Capability` nodes into baseline;
    merge reads those back and MERGEs canonical `OBL-<key>` / `CAP-<key>` nodes into
    `policy_system` (a key an earlier version already merged is matched, not duplicated).
    `arm_failure(stage, partial=True)` makes the named stage write half its items and then raise,
    once. Only the LLM / Cellar boundary is absent; the Cypher-facing code under test is real.
    """

    def __init__(self, graphs: StatefulGraphSet, content: Mapping[str, VersionContent]) -> None:
        """Prime the graphs and each version's content (keyed by instrument id)."""
        self._graphs = graphs
        self._content = content
        self._armed: tuple[str, bool] | None = None
        self.failures_fired = 0

    def arm_failure(self, stage: str, *, partial: bool = True) -> None:
        """Make `stage` raise `RuntimeError` on its next call (after half its writes if partial)."""
        self._armed = (stage, partial)

    def pipeline_stages(self) -> PipelineStages:
        """The doubles as the pipeline's `PipelineStages` seam."""
        return PipelineStages(
            ingest=self.ingest,
            extract=self.extract,
            derive=self.derive,
            merge=self.merge,
            ingest_internal=self._no_internal,
        )

    def run_all(self, identifier: str, short_name: str, version: str) -> None:
        """Seed a version through all four stages (used to build the prior, never armed)."""
        rid = self.ingest(
            identifier, short_name, version=version, graph=self._graphs.native
        ).regulatory_instrument_id
        self.extract(rid)
        self.derive(rid)
        self.merge(rid)

    def _apply(self, stage: str, writes: list[Callable[[], object]]) -> None:
        self._graphs.events.append(f"stage:{stage}")
        armed = self._armed
        if armed is not None and armed[0] == stage:
            self._armed = None
            self.failures_fired += 1
            for write in writes[: len(writes) // 2 if armed[1] else 0]:
                write()
            raise RuntimeError(f"{stage} blew up")
        for write in writes:
            write()

    def ingest(
        self,
        identifier: str,
        short_name: str,
        *,
        version: str,
        adapter: object = None,
        graph: object = None,
        submitter: object = None,
        audit_event_id: str = "",
        run_id: str | None = None,
        emitter: object = None,
    ) -> IngestResult:
        """Write the version's `RegulatoryInstrument` and an article into native."""
        _ = (identifier, adapter, graph, submitter, audit_event_id, emitter)
        rid = f"{short_name}-{version}"
        native = self._graphs.native
        self._apply(
            "ingestion",
            [
                lambda: native.merge_node(
                    "RegulatoryInstrument",
                    rid,
                    {"status": "active", "instrument_type": "regulation"},
                ),
                lambda: native.merge_node("Article", f"{rid}:art1", {"rid": rid}),
                lambda: native.merge_edge("HAS", rid, f"{rid}:art1"),
            ],
        )
        return IngestResult(
            regulatory_instrument_id=rid, run_id=run_id or "stateful-run", counts={}
        )

    def extract(
        self,
        regulatory_instrument_id: str,
        *,
        adapter: object = None,
        native_graph: object = None,
        submitter: object = None,
        baseline_graph_name: str | None = None,
        audit_event_id: str | None = None,
        model: str = "",
        call_completion: object = None,
        emitter: object = None,
    ) -> ExtractionResult:
        """Write the instrument and one `Requirement` per obligation key into baseline."""
        rid = regulatory_instrument_id
        _ = (adapter, native_graph, model, call_completion, emitter)
        _ = (submitter, baseline_graph_name, audit_event_id)
        baseline = self._graphs.baseline
        writes: list[Callable[[], object]] = [
            lambda: baseline.merge_node(
                "RegulatoryInstrument", rid, {"status": "active", "instrument_type": "regulation"}
            )
        ]
        keys = self._content[rid].obligations
        for key in keys:
            req = f"{rid}:req:{key}"
            writes.append(
                lambda req=req, key=key: baseline.merge_node(
                    "Requirement", req, {"rid": rid, "key": key}
                )
            )
            writes.append(lambda req=req: baseline.merge_edge("EXPRESSES", rid, req))
        self._apply("extraction", writes)
        return ExtractionResult(
            regulatory_instrument_id=rid,
            role_node_ids={},
            requirement_ids=tuple(f"{rid}:req:{key}" for key in keys),
            candidate_count=len(keys),
            skipped_unit_count=0,
            requirement_id_collisions=(),
        )

    def derive(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: object = None,
        submitter: object = None,
        baseline_graph_name: str | None = None,
        audit_event_id: str | None = None,
        model: str = "",
        call_completion: object = None,
        emitter: object = None,
    ) -> DerivationResult:
        """Write version-owned Obligation / Capability nodes for this version's Requirements."""
        rid = regulatory_instrument_id
        _ = (baseline_graph, model, call_completion, emitter)
        _ = (submitter, baseline_graph_name, audit_event_id)
        baseline = self._graphs.baseline
        req_keys = sorted(
            str(props["key"])
            for (label, _id), props in baseline.nodes.items()
            if label == "Requirement" and props["rid"] == rid
        )
        writes: list[Callable[[], object]] = []
        for key in req_keys:
            writes.append(
                lambda key=key: baseline.merge_node(
                    "Obligation", f"{rid}:obl:{key}", {"rid": rid, "key": key}
                )
            )
            writes.append(
                lambda key=key: baseline.merge_edge(
                    "SATISFIED_BY", f"{rid}:obl:{key}", f"{rid}:req:{key}"
                )
            )
        capabilities = self._content[rid].capabilities
        for key in capabilities:
            writes.append(
                lambda key=key: baseline.merge_node(
                    "Capability", f"{rid}:cap:{key}", {"rid": rid, "key": key}
                )
            )
        self._apply("derivation", writes)
        return DerivationResult(
            regulatory_instrument_id=rid,
            obligation_node_ids=tuple(f"{rid}:obl:{key}" for key in req_keys),
            capability_node_ids=tuple(f"{rid}:cap:{key}" for key in capabilities),
            unmatched_requirement_ids=(),
            unmatched_obligation_ids=(),
        )

    def merge(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: object = None,
        single_tenant_graph: object = None,
        submitter: object = None,
        single_tenant_graph_name: str = "",
        baseline_graph_name: str = "",
        audit_event_id: str = "",
        embed_model: str = "",
        similarity_threshold: float | None = None,
        call_embedding: object = None,
        emitter: object = None,
        governance: GovernanceImport | None = None,
    ) -> MergeResult:
        """MERGE the version's canonical Obligations / Capabilities into `policy_system`."""
        rid = regulatory_instrument_id
        _ = (
            baseline_graph,
            single_tenant_graph,
            submitter,
            single_tenant_graph_name,
            baseline_graph_name,
            audit_event_id,
            embed_model,
            similarity_threshold,
            call_embedding,
            emitter,
        )
        baseline, tenant = self._graphs.baseline, self._graphs.single_tenant
        obligation_keys = sorted(
            str(props["key"])
            for (label, _id), props in baseline.nodes.items()
            if label == "Obligation" and props["rid"] == rid
        )
        capability_keys = sorted(
            str(props["key"])
            for (label, _id), props in baseline.nodes.items()
            if label == "Capability" and props["rid"] == rid
        )
        new_obligations = [
            k for k in obligation_keys if ("Obligation", f"OBL-{k}") not in tenant.nodes
        ]
        new_capabilities = [
            k for k in capability_keys if ("Capability", f"CAP-{k}") not in tenant.nodes
        ]
        writes: list[Callable[[], object]] = [
            lambda: tenant.merge_node(
                "RegulatoryInstrument",
                rid,
                {"status": "active", "instrument_type": "regulation"},
                on_create_only=True,
            )
        ]
        for key in obligation_keys:
            writes.append(
                lambda key=key: tenant.merge_node(
                    "Obligation", f"OBL-{key}", {}, on_create_only=True
                )
            )
            writes.append(lambda key=key: tenant.merge_edge("EXPRESSES", rid, f"OBL-{key}"))
        for key in capability_keys:
            writes.append(
                lambda key=key: tenant.merge_node(
                    "Capability", f"CAP-{key}", {}, on_create_only=True
                )
            )
            writes.append(lambda key=key: tenant.merge_edge("REQUIRES", rid, f"CAP-{key}"))
        self._apply("merge", writes)
        return MergeResult(
            regulatory_instrument_id=rid,
            obligation_ids=tuple(f"OBL-{key}" for key in obligation_keys),
            capability_canonical_ids=tuple(f"CAP-{key}" for key in capability_keys),
            near_misses=(),
            new_obligation_count=len(new_obligations),
            new_capability_count=len(new_capabilities),
            matched_capability_count=len(capability_keys) - len(new_capabilities),
        )

    def _no_internal(
        self,
        seed: object,
        *,
        submitter: object = None,
        native_graph_name: object = None,
        baseline_graph_name: object = None,
        audit_event_id: object = None,
        emitter: object = None,
    ) -> NoReturn:
        _ = (seed, submitter, native_graph_name, baseline_graph_name, audit_event_id)
        _ = emitter
        raise AssertionError("the internal-seed pipeline is not part of an amendment re-ingest")
