"""Unit-level idempotent re-run: calling `merge_baseline_graph` TWICE with IDENTICAL
inputs (same baseline graph, same fake `single_tenant_graph` state carried
between calls, same scripted `call_embedding` responses -- determinism
standing in for real embedding determinism) never grows the single-tenant
graph's node or edge set, and the second call's `MergeResult` is
field-for-field identical to the first's.

Fakes here are self-contained copies of `test_merge_baseline_graph.py`'s own
`_FakeBaselineGraph`/`_FakeSingleTenantGraph`/`_ScriptedCallEmbedding`
classes, which accumulate state across calls -- exactly the behavior needed
here. This mirrors the test package's convention of each test module carrying
its own local fake copies rather than importing across test modules (see e.g.
`test_dedup_combined_resolution.py`'s `_ScriptedSingleTenantGraph`/
`_ScriptedCallEmbedding`). `_FakeSingleTenantGraph` adds `capability_ids()`
(the Capability-side analogue of `obligation_ids()`), since both kinds'
node-id sets are checked for no-growth.

To exercise the SEMANTIC-match + embedding-backfill path on a re-run (not
just the trivial "second call's incoming id already equals the canonical
id, so it's an exact match and never even calls find_best_semantic_match"
path), this test pre-seeds one existing Obligation and one existing
Capability node (both with `embedding=None`) into the fake single-tenant
graph BEFORE the first call, and gives the baseline graph's own Obligation/
Capability DIFFERENT text/name from those pre-existing nodes, scripting
`call_embedding` so both score above threshold. This means every call
(first AND second) resolves via `match_kind="semantic"`, onto the
pre-existing canonical ids -- the scenario in which `DedupResult.
embedding_backfills`/`graph_writer.backfill_canonical_embeddings` actually
has something to do, so the "second run's backfill call for an
already-backfilled node doesn't change anything" claim has real content to
verify, not a vacuous one whose backfill dict is always empty by
construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from company_merge._fakes import MakeEmitter
    from company_merge._gateway_fakes import MergeRig
    from ps_service.graph_gateway.models import GraphLogEntry

import re
from dataclasses import dataclass, replace
from typing import cast

from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import MergeResult
from ps_service.domain_mapper.identity import capability_id, obligation_id

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85
_REGULATION_ID = "REG-IDEMPOTENT"


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

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


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


_EXISTENCE_READ = re.compile(
    r"^MATCH \((?:n|o):(\w+)\) WHERE (?:n|o)\.id IN \$ids RETURN (?:n|o)\.id$"
)


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s queries with its own
    scripted row set -- copied from `test_merge_baseline_graph.py`'s own
    `_FakeBaselineGraph` (dispatch-by-substring style).
    """

    def __init__(
        self,
        *,
        regulatory_instrument_properties: dict[str, object],
        role_rows: list[object],
        requirement_rows: list[object],
        obligation_rows: list[object],
        capability_rows: list[object],
        defines_rows: list[object],
        expresses_rows: list[object],
        has_rows: list[object],
        satisfied_by_rows: list[object],
        requires_rows: list[object],
    ) -> None:
        self._regulatory_instrument_properties = regulatory_instrument_properties
        self._role_rows = role_rows
        self._requirement_rows = requirement_rows
        self._obligation_rows = obligation_rows
        # Mutable, keyed by id, so backfill_incoming_capability_
        # embeddings' write has somewhere to land (mirrors
        # test_merge_baseline_graph.py's own `_FakeBaselineGraph`). A
        # 4-element row (a row without an embedding column) is padded with a
        # `None` embedding.
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows:
            row_list = list(cast("list[object]", row))
            if len(row_list) == 4:
                row_list.append(None)
            self._capabilities[cast("str", row_list[0])] = row_list
        self._defines_rows = defines_rows
        self._expresses_rows = expresses_rows
        self._has_rows = has_rows
        self._satisfied_by_rows = satisfied_by_rows
        self._requires_rows = requires_rows
        self.calls: list[str] = []
        self._gateway_graph: InMemoryGraph | None = None

    def attach_gateway(self, graph: InMemoryGraph) -> None:
        """The embedding cached on this baseline is a gateway group on `graph`."""
        self._gateway_graph = graph

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "[e:DEFINES]" in q:
            return _FakeQueryResult(self._defines_rows)
        if "[e:EXPRESSES]" in q:
            return _FakeQueryResult(self._expresses_rows)
        if "[:HAS]" in q:
            return _FakeQueryResult(self._has_rows)
        if "[:SATISFIED_BY]" in q:
            return _FakeQueryResult(self._satisfied_by_rows)
        if "[:REQUIRES]" in q:
            return _FakeQueryResult(self._requires_rows)
        if (
            "(n:Policy)" in q
            or "(n:Standard)" in q
            or "(n:Control)" in q
            or "(n:PracticeArea) RETURN" in q
            or "(n:RiskPath) RETURN" in q
            or "[:GOVERNED_BY]" in q
            or "[:SUPPORTED_BY]" in q
            or "[:IMPLEMENTED_BY]" in q
            or "[:COVERS]" in q
            or "[:OWNS]" in q
            or "[:MITIGATED_BY]" in q
            or "[:VERIFIED_BY]" in q
        ):
            # This fixture is
            # Capability-only; the governance/classification queries
            # `read_baseline_graph` now always issues answer empty
            # (external-baseline shape).
            return _FakeQueryResult([])
        if "n.role_id" in q:
            return _FakeQueryResult(self._requirement_rows)
        if "n.description" in q:
            return _FakeQueryResult(
                [
                    [*row[:4], self._embedding_of(cast("str", row[0]))]
                    for row in self._capabilities.values()
                ]
            )
        if "n.name, n.confidence" in q:
            return _FakeQueryResult(self._role_rows)
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult(self._obligation_rows)
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
            )
        if "WHERE n.id IN $ids RETURN n.id, n.embedding IS NULL" in q:
            # backfill_incoming_capability_embeddings' read of which of its ids
            # exist on THIS (incoming) baseline graph and have no embedding yet.
            assert params is not None
            return _FakeQueryResult(
                [
                    [i, self._embedding_of(i) is None]
                    for i in cast("list[str]", params["ids"])
                    if i in self._capabilities
                ]
            )
        raise AssertionError(f"unexpected query issued: {q!r}")

    def capability_embedding(self, node_id: str) -> tuple[float, ...] | None:
        """Test accessor: this baseline graph's own current cached
        `embedding` for Capability `node_id`, or `None` if never backfilled.
        """
        raw = self._embedding_of(node_id)
        return tuple(cast("list[float]", raw)) if raw is not None else None

    def _embedding_of(self, node_id: str) -> object | None:
        """The embedding the row holds, else the one a gateway group has cached on the graph."""
        row = self._capabilities[node_id]
        if row[4] is not None:
            return row[4]
        if self._gateway_graph is None:
            return None
        node = self._gateway_graph.nodes.get(("Capability", node_id))
        return node.get("embedding") if node is not None else None


class _FakeSingleTenantGraph:
    """Answers Obligation/Capability reads ACROSS calls from its seeded rows plus the gateway graph.

    (Writes are gateway groups, so it never interprets one.) Copied from
    `test_merge_baseline_graph.py`'s own `_FakeSingleTenantGraph`,
    plus `capability_ids()` (the
    Capability-side analogue of the original's `obligation_ids()`).
    """

    def __init__(
        self,
        *,
        obligation_rows: list[object] | None = None,
        capability_rows: list[object] | None = None,
    ) -> None:
        self._obligations: dict[str, list[object]] = {}
        for row in obligation_rows or []:
            row_list = list(cast("list[object]", row))
            self._obligations[cast("str", row_list[0])] = row_list
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows or []:
            row_list = list(cast("list[object]", row))
            self._capabilities[cast("str", row_list[0])] = row_list
        self.calls: list[_RecordedCall] = []
        self._gateway_graph: InMemoryGraph | None = None

    def attach_gateway(self, graph: InMemoryGraph) -> None:
        """The nodes a merge submits through the gateway live in `graph`.

        The rows seeded into this fake are nodes the graph already holds, so they are seeded into
        `graph` too: an edge to one of them is valid, and the existence reads see one state.
        """
        self._gateway_graph = graph
        for label, table in (("Obligation", self._obligations), ("Capability", self._capabilities)):
            for node_id in table:
                if (label, node_id) not in graph.nodes:
                    graph.seed_node((label,), node_id, {})

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "MERGED_INTO" in q or "(m:Merged" in q:  # no tombstones/markers
            return _FakeQueryResult([])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult(self._capability_index_rows())
        existence = _EXISTENCE_READ.match(q)
        if existence is not None:
            # the existing-node reads (`count_new_obligations`, the create-only and edge-endpoint
            # reads): every write is a gateway group, so the graph answers
            assert params is not None
            held: set[str] = (
                set(self.obligation_ids()) if existence.group(1) == "Obligation" else set()
            )
            if self._gateway_graph is not None:
                held |= {i for lbl, i in self._gateway_graph.nodes if lbl == existence.group(1)}
            return _FakeQueryResult([[i] for i in cast("list[str]", params["ids"]) if i in held])
        if "WHERE n.id IN $ids RETURN n.id, n.embedding IS NULL" in q:
            # The canonical backfill's read (its write is a gateway group)
            assert params is not None
            return _FakeQueryResult(
                [
                    [i, self._embedding_of(i) is None]
                    for i in cast("list[str]", params["ids"])
                    if i in self._capabilities or i in self._gateway_capabilities()
                ]
            )
        return _FakeQueryResult([[0]])

    def _gateway_capabilities(self) -> dict[str, dict[str, object]]:
        """The Capability nodes the merge's gateway groups have written."""
        if self._gateway_graph is None:
            return {}
        return {
            node_id: props
            for (label, node_id), props in self._gateway_graph.nodes.items()
            if label == "Capability"
        }

    def _capability_index_rows(self) -> list[object]:
        rows: dict[str, list[object]] = {key: list(row) for key, row in self._capabilities.items()}
        for key, row in rows.items():
            row[2] = self._embedding_of(key)
        for node_id, props in self._gateway_capabilities().items():
            rows.setdefault(node_id, [node_id, props.get("name"), props.get("embedding")])
        return list(rows.values())

    def _embedding_of(self, node_id: str) -> object | None:
        """The seeded row's embedding, else the one a gateway group has set on the node."""
        row = self._capabilities.get(node_id)
        if row is not None and row[2] is not None:
            return row[2]
        node = self._gateway_capabilities().get(node_id)
        return node.get("embedding") if node is not None else None

    def calls_matching(self, substring: str) -> list[_RecordedCall]:
        return [call for call in self.calls if substring in call.query]

    def obligation_ids(self) -> frozenset[str]:
        gateway_ids = {
            node_id
            for label, node_id in (self._gateway_graph.nodes if self._gateway_graph else {})
            if label == "Obligation"
        }
        return frozenset(self._obligations) | gateway_ids

    def capability_ids(self) -> frozenset[str]:
        """This increment's own addition: the Capability-side analogue of
        `obligation_ids()` -- the current set of distinct canonical
        Capability node ids accumulated so far across however many
        `merge_baseline_graph` calls have run against this instance.
        """
        return frozenset(self._capabilities) | frozenset(self._gateway_capabilities())


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text`."""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
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
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def _edge_write_triples(rig: MergeRig) -> set[tuple[str, str, str]]:
    """Every distinct `(source_id, relationship_type, target_id)` triple `policy_system` holds.

    Read from the rig's graph (an edge is a gateway primitive, not a statement
    the fake single-tenant graph receives). What must not grow across a re-run is the DISTINCT
    triple set, which is exactly what this helper computes.
    """
    return {
        (key[2], key[0], key[4])
        for key in rig.policy_system.edges
        if key[0] in ("HAS", "SATISFIED_BY", "REQUIRES")
    }


def _edge_entry_count(rig: MergeRig) -> int:
    """How many edge entries `policy_system`'s log holds."""
    return sum(1 for e in rig.logged() if e.name in ("HAS", "SATISFIED_BY", "REQUIRES"))


def _idempotency_fixture(
    rig: MergeRig,
) -> tuple[_FakeBaselineGraph, _FakeSingleTenantGraph, _ScriptedCallEmbedding, str, str, str]:
    """One Role, one Requirement, one Obligation, one Capability, fully
    wired. The Obligation is a passthrough node (written straight
    through under its Role-scoped id, never deduped). The Capability side
    still exercises the SEMANTIC-match + embedding-backfill path on a re-run:
    ONE pre-existing Capability is seeded into the single-tenant graph (with
    `embedding=None`), the baseline's own Capability name is deliberately
    DIFFERENT, and `call_embedding` scores them above `_THRESHOLD` -- so both
    calls resolve the Capability via `match_kind="semantic"` onto the
    pre-existing id, giving `backfill_canonical_embeddings` real, non-vacuous
    work on the first call.
    """
    role_id = "role_operator_xyz"
    requirement_id = "REG-IDEMPOTENT_req_art_1.1"

    obligation_text = "Report the security incident to the competent authority."
    obligation_node_id = obligation_id(role_id, obligation_text)

    existing_capability_name = "Incident Response Capability"
    existing_capability_id = capability_id(existing_capability_name)
    incoming_capability_name = "Security Incident Response Capability"
    incoming_capability_id = capability_id(incoming_capability_name)
    assert incoming_capability_id != existing_capability_id

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": _REGULATION_ID, "title": "Test Regulation"},
        role_rows=[[role_id, "Operator", 0.9]],
        requirement_rows=[[requirement_id, "Must report incidents.", "requirement", 0.9, role_id]],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[incoming_capability_id, incoming_capability_name, 0.8, None]],
        defines_rows=[[role_id, "Article 1(1)"]],
        expresses_rows=[[requirement_id, "Article 1(1)"]],
        has_rows=[[role_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, incoming_capability_id]],
    )
    single_tenant = _FakeSingleTenantGraph(
        capability_rows=[[existing_capability_id, existing_capability_name, None]],
    )
    call_embedding = _ScriptedCallEmbedding(
        {
            incoming_capability_name: [1.0, 0.0],
            existing_capability_name: [1.0, 0.0],
        }
    )
    baseline.attach_gateway(rig.graphs.open(rig.baseline_graph_name))
    single_tenant.attach_gateway(rig.policy_system)
    return (
        baseline,
        single_tenant,
        call_embedding,
        obligation_node_id,
        existing_capability_id,
        incoming_capability_id,
    )


def test_second_identical_call_produces_field_for_field_identical_merge_result(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """The core claim: `merge_baseline_graph`'s second call's resolved
    `MergeResult`, against IDENTICAL inputs (same baseline graph object, same
    accumulating fake single-tenant graph, same scripted embedding
    responses), is field-for-field identical to the first's -- `MergeResult`
    is a frozen dataclass, so `==` already compares every field
    (`regulatory_instrument_id`, both canonical-id tuples, `near_misses`) recursively.
    """
    emitter, _log_path = make_emitter()
    (
        baseline,
        single_tenant,
        call_embedding,
        existing_obligation_id,
        existing_capability_id,
        _incoming_capability_id,
    ) = _idempotency_fixture(merge_rig)

    result_1 = merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )
    result_2 = merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert isinstance(result_1, MergeResult)
    # `new_obligation_count` is the DELTA a run makes: the first run creates the
    # Obligation, the second finds it, so the counts differ and only the rest is compared whole.
    assert (result_1.new_obligation_count, result_2.new_obligation_count) == (1, 0)
    assert replace(result_2, new_obligation_count=result_1.new_obligation_count) == result_1
    assert result_2.obligation_ids == (existing_obligation_id,)
    assert result_2.capability_canonical_ids == (existing_capability_id,)
    assert result_2.near_misses == ()


def _obligation_entry_count(rig: MergeRig) -> int:
    """How many Obligation entries the rig's gateway has logged on `policy_system`."""
    return sum(1 for group in rig.groups for entry in group.entries if entry.name == "Obligation")


def test_second_call_grows_neither_the_obligation_nor_capability_node_id_set(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """The fake `single_tenant_graph`'s accumulated node-id set is IDENTICAL
    after both calls to what it was after the first call alone -- no new
    node id appears on the second call.

    Proven by direct set comparison for both kinds, plus a zero-mint-call
    check for Capability specifically: `persist_canonical_nodes` only ever
    submits a group for a `match_kind="new"` resolution, and
    both resolutions here are `match_kind="semantic"` onto pre-existing ids,
    so neither call ever mints one. Obligation has no such call-level
    signal to check -- every baseline
    Obligation node is a create-only candidate on EVERY run (unlike
    Capability's conditional mint), and the existing one is left out
    of the gateway group, which the `obligation_ids()` set comparison above
    (fake plus the rig's graph) and the empty Obligation group list prove.
    """
    emitter, _log_path = make_emitter()
    (
        baseline,
        single_tenant,
        call_embedding,
        existing_obligation_id,
        existing_capability_id,
        _incoming_capability_id,
    ) = _idempotency_fixture(merge_rig)

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )
    obligation_ids_after_first = single_tenant.obligation_ids()
    capability_ids_after_first = single_tenant.capability_ids()
    obligation_entries_after_first = _obligation_entry_count(merge_rig)
    assert obligation_ids_after_first == {existing_obligation_id}
    assert capability_ids_after_first == {existing_capability_id}

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert single_tenant.obligation_ids() == obligation_ids_after_first
    assert single_tenant.capability_ids() == capability_ids_after_first
    # no Capability is minted on either run; the only Capability entry is the first run's
    # embedding backfill of the existing node, and the second run adds none
    assert [
        (e.identity, e.content["properties"])
        for g in merge_rig.groups
        for e in g.entries
        if e.name == "Capability"
    ] == [(existing_capability_id, {})]
    assert obligation_entries_after_first == 1
    assert _obligation_entry_count(merge_rig) == obligation_entries_after_first


def test_second_call_leaves_the_distinct_edge_triple_set_unchanged(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """The fake `single_tenant_graph`'s edge-write log, deduplicated by
    `(source, type, target)` triple, is unchanged in SIZE after the second
    call. This is deliberately not a raw call-count assertion: a
    `MERGE`-based fake graph is expected to reissue the identical `HAS`/
    `SATISFIED_BY`/`REQUIRES` calls on every re-run (that's what makes it
    idempotent, not a bug) -- what must not grow is the distinct triple/id
    set the calls collectively describe.
    """
    emitter, _log_path = make_emitter()
    (
        baseline,
        single_tenant,
        call_embedding,
        _existing_obligation_id,
        _existing_capability_id,
        _incoming_capability_id,
    ) = _idempotency_fixture(merge_rig)

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )
    triples_after_first = _edge_write_triples(merge_rig)
    assert len(triples_after_first) == 3  # HAS, SATISFIED_BY, REQUIRES
    assert _edge_entry_count(merge_rig) == 3

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    triples_after_second = _edge_write_triples(merge_rig)
    assert triples_after_second == triples_after_first
    # The second call re-submitted the same three edges, and the gateway logged nothing for them
    # (nothing changed): "unchanged because nothing ran" is excluded by the group count below.
    assert _edge_entry_count(merge_rig) == 3
    assert len(_edge_write_triples(merge_rig)) == 3
    assert len(merge_rig.policy_system.edges) == 3 + 2  # plus DEFINES and EXPRESSES


def _embedding_entries(rig: MergeRig) -> list[GraphLogEntry]:
    """The logged entries that set an embedding on a Capability of `policy_system`."""
    return [e for e in rig.logged() if e.name == "Capability" and e.embedding is not None]


def test_second_call_makes_zero_further_embedding_backfill_writes(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """The embedding-backfill effect, not just its call shape: the fake
    graph's `WHERE n.embedding IS NULL` handler mutates real in-memory state
    (`_backfill`, guarding on `row[2] is not None` -- mirrors FalkorDB's own
    guard exactly), so by the time the second call's `read_existing_
    canonical_index` runs, both pre-existing nodes already carry a non-`None`
    embedding (persisted by the FIRST call's backfill). `find_best_semantic_
    match` therefore makes zero fresh-embedding calls for either existing
    entry on the second call (proven via `call_embedding.calls`), which means
    `dedupe_canonical_nodes` folds nothing new into `embedding_backfills` for
    them, which means `graph_writer.backfill_canonical_embeddings` issues
    ZERO `WHERE n.embedding IS NULL` write calls at all on the second call --
    a genuine no-op in effect
    (not merely an identical-shaped call repeated).

    The INCOMING side is cached too, via
    `graph_writer.backfill_incoming_capability_embeddings` writing onto
    `baseline`'s own Capability node (this fixture's `_FakeBaselineGraph`,
    same `WHERE n.embedding IS NULL` guard) after the first call. The second
    call's `read_baseline_graph` therefore reads that cached embedding back,
    so `find_best_semantic_match` needs ZERO fresh `route_embedding` calls at
    all on the second run.
    """
    emitter, _log_path = make_emitter()
    (
        baseline,
        single_tenant,
        call_embedding,
        _existing_obligation_id,
        existing_capability_id,
        incoming_capability_id,
    ) = _idempotency_fixture(merge_rig)

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )
    backfill_writes_after_first = _embedding_entries(merge_rig)
    # The one pre-existing Capability node had embedding=None going in -> it
    # gets backfilled. (Obligation is a passthrough node -- no
    # embedding, no backfill.)
    assert len(backfill_writes_after_first) == 1
    backfilled_ids_after_first = {entry.identity for entry in backfill_writes_after_first}
    assert backfilled_ids_after_first == {existing_capability_id}

    # Every call the first run made to compute an embedding: the incoming
    # Capability name AND the (then-uncached) existing Capability name -- 2
    # calls total.
    assert len(call_embedding.calls) == 2

    embedding_calls_before_second = len(call_embedding.calls)

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    second_call_backfill_writes = _embedding_entries(merge_rig)[len(backfill_writes_after_first) :]
    assert second_call_backfill_writes == [], (
        "the second run must issue ZERO embedding-backfill write calls -- both "
        "existing nodes were already backfilled by the first run, so there is "
        "nothing left to compute or persist"
    )

    # Both sides are cached from the first run -- the
    # existing Capability's embedding AND the
    # incoming Capability's own embedding. Zero further
    # route_embedding calls of any kind on the second run.
    second_run_embedding_calls = call_embedding.calls[embedding_calls_before_second:]
    assert second_run_embedding_calls == []

    # And the incoming side's own backfill write actually landed on the
    # baseline graph's own node during the first run, not just
    # "zero calls happened to be needed" by coincidence.
    assert baseline.capability_embedding(incoming_capability_id) == (1.0, 0.0)
