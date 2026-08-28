"""Increment 17 (PLAN_REVIEWED.md §10 Batch 9) -- AC-005, unit-level
idempotent re-run: calling `merge_baseline_graph` TWICE with IDENTICAL
inputs (same baseline graph, same fake `single_tenant_graph` state carried
between calls, same scripted `call_embedding` responses -- determinism
standing in for real embedding determinism) never grows the single-tenant
graph's node or edge set, and the second call's `MergeResult` is
field-for-field identical to the first's.

Fakes here are self-contained copies of `test_merge_baseline_graph.py`'s own
`_FakeBaselineGraph`/`_FakeSingleTenantGraph`/`_ScriptedCallEmbedding`
classes (that file's own docstring notes these were "already extended [in
Increments 15/16] to accumulate state across calls" -- exactly the behavior
this increment needs) -- mirrors this test package's own established
convention of each test module carrying its own local fake copies rather
than importing across test modules (see e.g. `test_dedup_combined_
resolution.py`'s own `_ScriptedSingleTenantGraph`/`_ScriptedCallEmbedding`).
`_FakeSingleTenantGraph` gains one small addition here, `capability_ids()`
(the direct Capability-side analogue of the original's `obligation_ids()`),
since this increment -- unlike Increments 15/16, which only ever exercised
the Obligation side across calls -- needs both kinds' node-id sets checked
for no-growth.

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

from dataclasses import dataclass
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


class _FakeRegulationNode:
    """Satisfies `graph_reader._RegulationNode` structurally -- only
    `.properties` is ever read."""

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


_READ_MARKERS = ("RETURN n.id, n.text, n.embedding", "RETURN n.id, n.name, n.embedding")


def _is_read_call(call: _RecordedCall) -> bool:
    return any(marker in call.query for marker in _READ_MARKERS)


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s queries with its own
    scripted row set -- copied from `test_merge_baseline_graph.py`'s own
    `_FakeBaselineGraph` (dispatch-by-substring style)."""

    def __init__(
        self,
        *,
        regulation_properties: dict[str, object],
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
        self._regulation_properties = regulation_properties
        self._role_rows = role_rows
        self._requirement_rows = requirement_rows
        self._obligation_rows = obligation_rows
        self._capability_rows = capability_rows
        self._defines_rows = defines_rows
        self._expresses_rows = expresses_rows
        self._has_rows = has_rows
        self._satisfied_by_rows = satisfied_by_rows
        self._requires_rows = requires_rows
        self.calls: list[str] = []

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
        if "n.role_id" in q:
            return _FakeQueryResult(self._requirement_rows)
        if "n.description" in q:
            return _FakeQueryResult(self._capability_rows)
        if "n.name, n.confidence" in q:
            return _FakeQueryResult(self._role_rows)
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult(self._obligation_rows)
        if "(n:RegulatoryInstrument {id: $regulation_id}) RETURN n" in q:
            return _FakeQueryResult([[_FakeRegulationNode(self._regulation_properties)]])
        raise AssertionError(f"unexpected query issued: {q!r}")


class _FakeSingleTenantGraph:
    """Accumulates Obligation/Capability state ACROSS calls, mirroring real
    `MERGE ... ON CREATE SET`/`WHERE n.embedding IS NULL` semantics exactly
    -- copied from `test_merge_baseline_graph.py`'s own `_FakeSingleTenantGraph`,
    plus `capability_ids()` (this increment's own small addition, the
    Capability-side analogue of the original's `obligation_ids()`)."""

    def __init__(
        self,
        *,
        obligation_rows: list[object] | None = None,
        capability_rows: list[object] | None = None,
    ) -> None:
        self._obligations: dict[str, list[object]] = {}
        for row in obligation_rows or []:
            row_list = list(cast("list[object]", row))
            self._obligations[cast(str, row_list[0])] = row_list
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows or []:
            row_list = list(cast("list[object]", row))
            self._capabilities[cast(str, row_list[0])] = row_list
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "(n:Obligation) RETURN n.id, n.text, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._obligations.values()])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "MERGE (n:Obligation {id: $id}) ON CREATE SET" in q:
            self._mint(self._obligations, params, "text")
            return _FakeQueryResult([])
        if "MERGE (n:Capability {id: $id}) ON CREATE SET" in q:
            self._mint(self._capabilities, params, "name")
            return _FakeQueryResult([])
        if "MATCH (n:Obligation {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._obligations, params)
            return _FakeQueryResult([])
        if "MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._capabilities, params)
            return _FakeQueryResult([])
        return _FakeQueryResult([[0]])

    def _mint(
        self,
        table: dict[str, list[object]],
        params: dict[str, object] | None,
        text_key: str,
    ) -> None:
        assert params is not None
        node_id = cast(str, params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [node_id, properties.get(text_key), properties.get("embedding")]

    def _backfill(self, table: dict[str, list[object]], params: dict[str, object] | None) -> None:
        assert params is not None
        node_id = cast(str, params["id"])
        row = table.get(node_id)
        if row is None or row[2] is not None:
            return
        row[2] = params["embedding"]

    def writes(self) -> list[_RecordedCall]:
        return [call for call in self.calls if not _is_read_call(call)]

    def calls_matching(self, substring: str) -> list[_RecordedCall]:
        return [call for call in self.calls if substring in call.query]

    def obligation_ids(self) -> frozenset[str]:
        return frozenset(self._obligations)

    def capability_ids(self) -> frozenset[str]:
        """This increment's own addition: the Capability-side analogue of
        `obligation_ids()` -- the current set of distinct canonical
        Capability node ids accumulated so far across however many
        `merge_baseline_graph` calls have run against this instance."""
        return frozenset(self._capabilities)


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text`."""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        assert len(input) == 1
        text = input[0]
        self.calls.append(text)
        vector = self._vectors_by_text.get(text)
        if vector is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def _edge_write_triples(single_tenant: _FakeSingleTenantGraph) -> set[tuple[str, str, str]]:
    """Every distinct `(source_id, relationship_type, target_id)` triple
    that has EVER been written into `single_tenant`'s edge-write log, read
    directly from each recorded write call's own query text and params --
    not a raw call-count assertion, since a `MERGE`-based fake graph is
    expected to reissue the identical call on every idempotent re-run.
    What must not grow across a re-run is the DISTINCT triple set, which is
    exactly what this helper computes."""
    triples: set[tuple[str, str, str]] = set()
    for call in single_tenant.writes():
        for relationship_type in ("HAS", "SATISFIED_BY", "REQUIRES"):
            if f"[:{relationship_type}]" in call.query:
                assert call.params is not None
                source_id = cast(str, call.params["source_id"])
                target_id = cast(str, call.params["target_id"])
                triples.add((source_id, relationship_type, target_id))
    return triples


def _idempotency_fixture() -> tuple[
    _FakeBaselineGraph, _FakeSingleTenantGraph, _ScriptedCallEmbedding, str, str
]:
    """One Role, one Requirement, one Obligation, one Capability, fully
    wired -- with ONE pre-existing Obligation and ONE pre-existing
    Capability already seeded into the single-tenant graph (both with
    `embedding=None`), and the baseline graph's own Obligation/Capability
    text/name deliberately DIFFERENT from those pre-existing nodes, scripted
    via `call_embedding` to score above `_THRESHOLD` against them. This
    means BOTH calls (first and second) resolve via `match_kind="semantic"`
    onto the pre-existing canonical ids -- never minting a new node -- so
    `embedding_backfills`/`backfill_canonical_embeddings` has real,
    non-vacuous work to do on the first call, whose "no-op on the second
    call" effect this test can then meaningfully verify.
    """
    role_id = "role_operator_xyz"
    requirement_id = "REG-IDEMPOTENT_req_art_1.1"

    existing_obligation_text = "Report the security incident to the competent authority."
    existing_obligation_id = obligation_id(existing_obligation_text)
    incoming_obligation_text = "Notify the competent authority about the security incident."
    incoming_obligation_id = obligation_id(incoming_obligation_text)
    assert incoming_obligation_id != existing_obligation_id

    existing_capability_name = "Incident Response Capability"
    existing_capability_id = capability_id(existing_capability_name)
    incoming_capability_name = "Security Incident Response Capability"
    incoming_capability_id = capability_id(incoming_capability_name)
    assert incoming_capability_id != existing_capability_id

    baseline = _FakeBaselineGraph(
        regulation_properties={"id": _REGULATION_ID, "title": "Test Regulation"},
        role_rows=[[role_id, "Operator", 0.9]],
        requirement_rows=[
            [requirement_id, "Must report incidents.", "requirement", 0.9, role_id]
        ],
        obligation_rows=[[incoming_obligation_id, incoming_obligation_text, 0.9]],
        capability_rows=[[incoming_capability_id, incoming_capability_name, 0.8, None]],
        defines_rows=[[role_id, "Article 1(1)"]],
        expresses_rows=[[requirement_id, "Article 1(1)"]],
        has_rows=[[role_id, incoming_obligation_id]],
        satisfied_by_rows=[[requirement_id, incoming_obligation_id]],
        requires_rows=[[incoming_obligation_id, incoming_capability_id]],
    )
    single_tenant = _FakeSingleTenantGraph(
        obligation_rows=[[existing_obligation_id, existing_obligation_text, None]],
        capability_rows=[[existing_capability_id, existing_capability_name, None]],
    )
    call_embedding = _ScriptedCallEmbedding(
        {
            incoming_obligation_text: [1.0, 0.0],
            existing_obligation_text: [1.0, 0.0],
            incoming_capability_name: [1.0, 0.0],
            existing_capability_name: [1.0, 0.0],
        }
    )
    return baseline, single_tenant, call_embedding, existing_obligation_id, existing_capability_id


def test_second_identical_call_produces_field_for_field_identical_merge_result(
    make_emitter,
) -> None:
    """The core AC-005 claim: `merge_baseline_graph`'s second call's resolved
    `MergeResult`, against IDENTICAL inputs (same baseline graph object, same
    accumulating fake single-tenant graph, same scripted embedding
    responses), is field-for-field identical to the first's -- `MergeResult`
    is a frozen dataclass, so `==` already compares every field
    (`regulation_id`, both canonical-id tuples, `near_misses`) recursively."""
    emitter, _log_path = make_emitter()
    baseline, single_tenant, call_embedding, existing_obligation_id, existing_capability_id = (
        _idempotency_fixture()
    )

    result_1 = merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )
    result_2 = merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert isinstance(result_1, MergeResult)
    assert result_1 == result_2
    assert result_2.obligation_canonical_ids == (existing_obligation_id,)
    assert result_2.capability_canonical_ids == (existing_capability_id,)
    assert result_2.near_misses == ()


def test_second_call_grows_neither_the_obligation_nor_capability_node_id_set(
    make_emitter,
) -> None:
    """The fake `single_tenant_graph`'s accumulated node-id set is IDENTICAL
    after both calls to what it was after the first call alone -- no new
    node id appears on the second call. Proven both by direct set comparison
    and by asserting zero `ON CREATE SET` mint calls occur on either call
    (both resolutions are `match_kind="semantic"` onto pre-existing ids, so
    neither call ever mints)."""
    emitter, _log_path = make_emitter()
    baseline, single_tenant, call_embedding, existing_obligation_id, existing_capability_id = (
        _idempotency_fixture()
    )

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )
    obligation_ids_after_first = single_tenant.obligation_ids()
    capability_ids_after_first = single_tenant.capability_ids()
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
    )

    assert single_tenant.obligation_ids() == obligation_ids_after_first
    assert single_tenant.capability_ids() == capability_ids_after_first
    assert not single_tenant.calls_matching("MERGE (n:Obligation {id: $id}) ON CREATE SET")
    assert not single_tenant.calls_matching("MERGE (n:Capability {id: $id}) ON CREATE SET")


def test_second_call_leaves_the_distinct_edge_triple_set_unchanged(make_emitter) -> None:
    """The fake `single_tenant_graph`'s edge-write log, deduplicated by
    `(source, type, target)` triple, is unchanged in SIZE after the second
    call. This is deliberately not a raw call-count assertion: a
    `MERGE`-based fake graph is expected to reissue the identical `HAS`/
    `SATISFIED_BY`/`REQUIRES` calls on every re-run (that's what makes it
    idempotent, not a bug) -- what must not grow is the distinct triple/id
    set the calls collectively describe."""
    emitter, _log_path = make_emitter()
    baseline, single_tenant, call_embedding, _existing_obligation_id, _existing_capability_id = (
        _idempotency_fixture()
    )

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )
    triples_after_first = _edge_write_triples(single_tenant)
    assert len(triples_after_first) == 3  # HAS, SATISFIED_BY, REQUIRES

    calls_before_second = len(single_tenant.calls)

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    triples_after_second = _edge_write_triples(single_tenant)
    assert triples_after_second == triples_after_first
    # The second call DID reissue the same three edge MERGE calls (expected,
    # idempotent MERGE behavior) -- this asserts that repetition happened,
    # distinguishing "unchanged because nothing ran" from "unchanged despite
    # running again".
    second_call_calls = single_tenant.calls[calls_before_second:]
    second_call_edge_writes = [
        c
        for c in second_call_calls
        if any(f"[:{rt}]" in c.query for rt in ("HAS", "SATISFIED_BY", "REQUIRES"))
    ]
    assert len(second_call_edge_writes) == 3


def test_second_call_makes_zero_further_embedding_backfill_writes(make_emitter) -> None:
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
    a genuine no-op in effect, not merely an identical-shaped call repeated
    (the limitation Increment 11's own shape-only test explicitly accepted).
    """
    emitter, _log_path = make_emitter()
    baseline, single_tenant, call_embedding, existing_obligation_id, existing_capability_id = (
        _idempotency_fixture()
    )

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )
    backfill_writes_after_first = single_tenant.calls_matching("WHERE n.embedding IS NULL")
    # Both pre-existing nodes had embedding=None going in -> both get backfilled.
    assert len(backfill_writes_after_first) == 2
    backfilled_ids_after_first = {
        cast(str, c.params["id"]) for c in backfill_writes_after_first if c.params is not None
    }
    assert backfilled_ids_after_first == {existing_obligation_id, existing_capability_id}

    # Every call the first run made to compute an embedding: incoming text/
    # name AND the (then-uncached) existing text/name, for both kinds -- 4
    # calls total.
    assert len(call_embedding.calls) == 4

    calls_before_second = len(single_tenant.calls)
    embedding_calls_before_second = len(call_embedding.calls)

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    second_call_calls = single_tenant.calls[calls_before_second:]
    second_call_backfill_writes = [c for c in second_call_calls if "WHERE n.embedding IS NULL" in c.query]
    assert second_call_backfill_writes == [], (
        "the second run must issue ZERO embedding-backfill write calls -- both "
        "existing nodes were already backfilled by the first run, so there is "
        "nothing left to compute or persist"
    )

    # The incoming side is never cached (Open Question 4) -- one fresh call
    # per kind for the incoming text/name is still expected on the second
    # run -- but the existing side (already cached from the first run) costs
    # nothing further: 2 new calls total, not 4.
    second_run_embedding_calls = call_embedding.calls[embedding_calls_before_second:]
    assert len(second_run_embedding_calls) == 2
