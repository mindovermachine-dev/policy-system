"""Tests for `ps_service.company_merge.merge.merge_baseline_graph`
(PLAN_REVIEWED.md §10 Increments 13-14): the top-level `MergeBaselineGraph`
orchestration wiring `graph_reader`/`dedup`/`graph_writer` together.

Fakes here are hand-written, satisfying `GraphHandle`/`EmbeddingCaller`
structurally -- mirroring `test_graph_reader.py`'s `_ScriptedFakeGraph`
dispatch-by-substring style for the baseline-graph side, and
`test_dedup_combined_resolution.py`'s `_ScriptedCallEmbedding` for the
embedding side. `_FakeSingleTenantGraph` additionally records every call
(read AND write) for the call-order proof (test (b)) and the edge-level
dedup-target reachability proof (test (c), Q3's fix).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.logging import bind_run_context

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85


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


# The two literal read queries dedup.read_existing_canonical_index issues --
# used to distinguish a "read" call from a "write" call in a fake
# single-tenant graph's call log (test (b)'s call-order proof).
_READ_MARKERS = ("RETURN n.id, n.text, n.embedding", "RETURN n.id, n.name, n.embedding")


def _is_read_call(call: _RecordedCall) -> bool:
    return any(marker in call.query for marker in _READ_MARKERS)


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s ten queries with its own
    scripted row set, dispatched by a distinctive substring -- mirrors
    `test_graph_reader.py`'s `_ScriptedFakeGraph` exactly, plus recording
    every call received (needed for test (d)'s zero-calls proof)."""

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
    """Answers `read_existing_canonical_index`'s two possible read queries
    with pre-seeded rows and records every call (read AND write) it
    receives, in order -- the call log a caller inspects for the call-order
    proof (test (b)) and the edge-level dedup-target reachability proof
    (test (c)).

    Increments 15/16 (PLAN_REVIEWED.md §10 Batch 8) additionally require
    this fake to ACCUMULATE state ACROSS two separate `merge_baseline_graph`
    calls against the SAME instance -- simulating two regulations merged one
    after another into the same single-tenant graph, the way a real
    FalkorDB graph would. `query()` therefore mutates internal
    Obligation/Capability tables on a mint (`ON CREATE SET`) or embedding
    backfill (`WHERE n.embedding IS NULL`) write, mirroring the real
    Cypher's own conditional-write semantics exactly (a mint against an id
    already present is a no-op, matching a real `MERGE ... ON CREATE SET`
    against an existing node firing no `SET` at all; a backfill against an
    already-non-`None` embedding is a no-op, matching the real `WHERE
    n.embedding IS NULL` guard) -- so a SECOND call's
    `read_existing_canonical_index` read sees everything the FIRST call
    wrote, exactly as AC-002/003/004 require."""

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
        """`MERGE ... ON CREATE SET` semantics: a node id already present in
        `table` fires no `SET` at all -- matches real FalkorDB, and is
        exactly what makes a second call's exact/semantic-matched
        resolution (which never even issues this query -- see
        `persist_canonical_nodes`, only a `match_kind="new"` resolution
        does) safe regardless."""
        assert params is not None
        node_id = cast(str, params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [node_id, properties.get(text_key), properties.get("embedding")]

    def _backfill(self, table: dict[str, list[object]], params: dict[str, object] | None) -> None:
        """`WHERE n.embedding IS NULL` semantics: a no-op when the id is
        absent from `table` or its embedding is already set -- matches real
        FalkorDB's own guard exactly (PLAN_REVIEWED.md §6.2)."""
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
        """Increments 15/16: the current set of distinct canonical
        Obligation node ids accumulated so far across however many
        `merge_baseline_graph` calls have run against this instance -- the
        direct proof that a cross-regulation exact/semantic match never
        mints a second node for the same canonical concept."""
        return frozenset(self._obligations)


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


def _everything_new_baseline_graph() -> _FakeBaselineGraph:
    """AC-001's "everything is new" fixture: one Role, one Requirement, one
    Obligation, one Capability, fully wired edges. No pre-existing canonical
    node exists anywhere, so nothing has anything to converge onto."""
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-1.0_req_art_1.1"
    obligation_text = "Report the incident to the competent authority."
    obligation_node_id = obligation_id(obligation_text)
    capability_name = "Incident Reporting Capability"
    capability_node_id = capability_id(capability_name)

    return _FakeBaselineGraph(
        regulation_properties={"id": "REG-1.0", "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9]],
        requirement_rows=[
            [requirement_node_id, "Must report incidents.", "requirement", 0.9, role_node_id]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[capability_node_id, capability_name, 0.8, None]],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, capability_node_id]],
    )


def test_everything_new_writes_every_node_type_directly(make_emitter) -> None:
    """(a) AC-001, "everything is new": an empty existing single-tenant
    graph -> every node type present afterward in the fake single-tenant
    graph's write log, directly (match_kind="new" for both Obligation and
    Capability, since nothing existed to converge onto)."""
    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph()

    result = merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert len(result.obligation_canonical_ids) == 1
    assert len(result.capability_canonical_ids) == 1
    assert result.near_misses == ()

    writes = single_tenant.writes()
    assert any("MERGE (n:RegulatoryInstrument" in c.query for c in writes)
    assert any("MERGE (n:Role" in c.query for c in writes)
    assert any("MERGE (n:Requirement" in c.query for c in writes)
    assert any(
        "MERGE (n:Obligation {id: $id}) ON CREATE SET" in c.query for c in writes
    )
    assert any(
        "MERGE (n:Capability {id: $id}) ON CREATE SET" in c.query for c in writes
    )
    assert any("[:HAS]" in c.query for c in writes)
    assert any("[:SATISFIED_BY]" in c.query for c in writes)
    assert any("[:REQUIRES]" in c.query for c in writes)


def test_both_dedup_reads_complete_before_any_write_call(make_emitter) -> None:
    """(b) call-order proof: dedupe_canonical_nodes for both kinds completes
    (both read_existing_canonical_index reads happen) before any write call
    appears in the fake single_tenant_graph's call log."""
    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph()

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    read_positions = [i for i, call in enumerate(single_tenant.calls) if _is_read_call(call)]
    write_positions = [i for i, call in enumerate(single_tenant.calls) if not _is_read_call(call)]
    assert len(read_positions) == 2
    assert write_positions
    assert max(read_positions) < min(write_positions)


def test_everything_is_a_dedup_target_reuses_existing_ids_at_the_edge_level(make_emitter) -> None:
    """(c) Q3's fix, "everything is a dedup target," proven end-to-end
    within ONE call: pre-seed the fake single_tenant_graph with one existing
    canonical Obligation node and one existing canonical Capability node
    BEFORE calling merge_baseline_graph. The Capability resolves via
    exact-key match (same name -> same capability_id hash); the Obligation
    resolves via a mocked call_embedding scoring above threshold (different
    text, same baseline-local id differs from the pre-existing canonical
    id). Assert: NO new Obligation/Capability node write occurs (the dedup
    target was reused, never re-minted); the Role's HAS edge and the
    Requirement's SATISFIED_BY edge -- read directly from the fake graph's
    edge-write log, not from MergeResult's resolution table -- target the
    PRE-EXISTING canonical id."""
    emitter, _log_path = make_emitter()
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-1.0_req_art_1.1"

    existing_obligation_text = "Report the security incident to the competent authority."
    existing_obligation_id = obligation_id(existing_obligation_text)
    incoming_obligation_text = "Alert the competent authority about the security breach."
    incoming_obligation_id = obligation_id(incoming_obligation_text)
    assert incoming_obligation_id != existing_obligation_id

    shared_capability_name = "Encrypt Data At Rest"
    shared_capability_id = capability_id(shared_capability_name)

    baseline = _FakeBaselineGraph(
        regulation_properties={"id": "REG-1.0", "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9]],
        requirement_rows=[
            [requirement_node_id, "Must report incidents.", "requirement", 0.9, role_node_id]
        ],
        obligation_rows=[[incoming_obligation_id, incoming_obligation_text, 0.9]],
        capability_rows=[[shared_capability_id, shared_capability_name, 0.8, None]],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, incoming_obligation_id]],
        satisfied_by_rows=[[requirement_node_id, incoming_obligation_id]],
        requires_rows=[],
    )
    single_tenant = _FakeSingleTenantGraph(
        obligation_rows=[[existing_obligation_id, existing_obligation_text, None]],
        capability_rows=[[shared_capability_id, shared_capability_name, None]],
    )
    call_embedding = _ScriptedCallEmbedding(
        {
            incoming_obligation_text: [1.0, 0.0],
            existing_obligation_text: [1.0, 0.0],
        }
    )

    result = merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert result.obligation_canonical_ids == (existing_obligation_id,)
    assert result.capability_canonical_ids == (shared_capability_id,)

    writes = single_tenant.writes()
    assert not any(
        "MERGE (n:Obligation {id: $id}) ON CREATE SET" in c.query for c in writes
    ), "no new Obligation node should have been minted -- the dedup target was reused"
    assert not any(
        "MERGE (n:Capability {id: $id}) ON CREATE SET" in c.query for c in writes
    ), "no new Capability node should have been minted -- the dedup target was reused"

    has_writes = single_tenant.calls_matching("[:HAS]")
    assert len(has_writes) == 1
    assert has_writes[0].params == {"source_id": role_node_id, "target_id": existing_obligation_id}

    satisfied_by_writes = single_tenant.calls_matching("[:SATISFIED_BY]")
    assert len(satisfied_by_writes) == 1
    assert satisfied_by_writes[0].params == {
        "source_id": requirement_node_id,
        "target_id": existing_obligation_id,
    }


def test_missing_similarity_threshold_raises_before_any_call() -> None:
    """(d) B1's fix, runtime enforcement: similarity_threshold=None raises
    CompanyMergeConfigurationError; both the fake baseline_graph and fake
    single_tenant_graph receive ZERO calls of any kind (the check fires
    before even the Regulation read)."""
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph()

    with pytest.raises(CompanyMergeConfigurationError):
        merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            similarity_threshold=None,
        )

    assert baseline.calls == []
    assert single_tenant.calls == []


def test_succeeded_log_entry_carries_bound_run_id(make_emitter, read_lines) -> None:
    """Increment 14(a): with bind_run_context("run-x"), the
    outcome="succeeded" entry for action="merge_baseline_graph" carries
    run_id="run-x"."""
    emitter, log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph()

    with bind_run_context("run-x"):
        merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            similarity_threshold=_THRESHOLD,
            emitter=emitter,
        )
    emitter.flush()

    entries = read_lines(log_path)
    succeeded = [
        e
        for e in entries
        if e.get("action") == "merge_baseline_graph" and e.get("outcome") == "succeeded"
    ]
    assert len(succeeded) == 1
    assert succeeded[0]["run_id"] == "run-x"


def test_dedup_decision_log_entries_carry_bound_run_id(make_emitter, read_lines) -> None:
    """Increment 14(b): at least one per-decision entry for
    action="dedupe_canonical_nodes" also carries run_id="run-x", with
    entity_id equal to one of the resolved nodes' incoming_id and outcome
    equal to its match_kind."""
    emitter, log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph()

    with bind_run_context("run-x"):
        result = merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            similarity_threshold=_THRESHOLD,
            emitter=emitter,
        )
    emitter.flush()

    entries = read_lines(log_path)
    dedup_entries = [e for e in entries if e.get("action") == "dedupe_canonical_nodes"]
    assert dedup_entries
    for entry in dedup_entries:
        assert entry["run_id"] == "run-x"

    resolved_obligation_id = result.obligation_canonical_ids[0]
    matching = [e for e in dedup_entries if e.get("entity_id") == resolved_obligation_id]
    assert len(matching) == 1
    assert matching[0]["outcome"] == "new"


# ---------------------------------------------------------------------------
# Increments 15-16 (PLAN_REVIEWED.md §10 Batch 8) -- AC-002/003/004 proven
# ACROSS two separate merge_baseline_graph calls against the SAME
# accumulating _FakeSingleTenantGraph instance, simulating two different
# regulations merged one after another into the same single-tenant graph.
# This is distinct from Increment 13(c) above (`test_
# everything_is_a_dedup_target_reuses_existing_ids_at_the_edge_level`), which
# already proved dedup-target reuse WITHIN one call against a pre-seeded
# graph -- these tests prove the same convergence property when the
# "pre-existing" canonical node was itself written by a PRIOR
# merge_baseline_graph call, the literal cross-regulation scenario
# AC-002/003/004 describe.
# ---------------------------------------------------------------------------


def _single_obligation_baseline_graph(
    *,
    regulation_id: str,
    role_id_value: str,
    requirement_id_value: str,
    obligation_text: str,
) -> _FakeBaselineGraph:
    """A minimal baseline graph fixture: one Role, one Requirement, one
    Obligation, `HAS`/`SATISFIED_BY` wired, no Capability -- everything
    Increments 15/16's cross-regulation Obligation-side tests need.
    Capability-side convergence (the Increment 12 REQUIRES-rewiring fix) is
    exercised separately, below."""
    obligation_node_id = obligation_id(obligation_text)
    return _FakeBaselineGraph(
        regulation_properties={"id": regulation_id, "title": f"Test Regulation {regulation_id}"},
        role_rows=[[role_id_value, "Manufacturer", 0.9]],
        requirement_rows=[
            [requirement_id_value, "Must report incidents.", "requirement", 0.9, role_id_value]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[],
        defines_rows=[[role_id_value, "Article 1(1)"]],
        expresses_rows=[[requirement_id_value, "Article 1(1)"]],
        has_rows=[[role_id_value, obligation_node_id]],
        satisfied_by_rows=[[requirement_id_value, obligation_node_id]],
        requires_rows=[],
    )


def test_cross_regulation_exact_key_merge_reuses_canonical_node(make_emitter) -> None:
    """Increment 15 -- AC-002, exact-key cross-regulation merge: TWO
    SEPARATE `merge_baseline_graph` calls, in sequence, against the SAME
    fake `single_tenant_graph` (simulating two regulations merged one after
    another). The second regulation's Obligation carries the EXACT SAME
    duty text as the first's -> the same `obligation_id` -> exact-key match
    on the second call, no new node minted, only its edges rewired onto the
    first call's canonical id.

    Assert: the fake graph's accumulated node-write log shows exactly ONE
    `MERGE ... ON CREATE SET` for the canonical id (from the first call
    only -- the second call's resolution is `match_kind="exact"`, which
    `persist_canonical_nodes` never writes a node for at all); the second
    call's `HAS`/`SATISFIED_BY` edges target the canonical id; no edge left
    referencing a node never written (the canonical id is the only
    Obligation node ever minted).
    """
    emitter, _log_path = make_emitter()
    shared_text = "Report the incident to the competent authority without undue delay."
    canonical_id = obligation_id(shared_text)

    single_tenant = _FakeSingleTenantGraph()

    first_regulation = _single_obligation_baseline_graph(
        regulation_id="REG-A",
        role_id_value="role_a_manufacturer",
        requirement_id_value="REG-A_req_art_1.1",
        obligation_text=shared_text,
    )
    merge_baseline_graph(
        "REG-A",
        baseline_graph=first_regulation,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    second_regulation = _single_obligation_baseline_graph(
        regulation_id="REG-B",
        role_id_value="role_b_provider",
        requirement_id_value="REG-B_req_art_2.1",
        obligation_text=shared_text,
    )
    result_b = merge_baseline_graph(
        "REG-B",
        baseline_graph=second_regulation,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert result_b.obligation_canonical_ids == (canonical_id,)

    mint_writes = single_tenant.calls_matching("MERGE (n:Obligation {id: $id}) ON CREATE SET")
    assert len(mint_writes) == 1, "exactly one mint, from REG-A's call -- REG-B exact-matched"
    assert mint_writes[0].params is not None
    assert mint_writes[0].params["id"] == canonical_id

    # No edge left referencing a node never written: the canonical id is the
    # ONLY Obligation node that exists in the accumulated fake graph.
    assert single_tenant.obligation_ids() == {canonical_id}

    has_writes = single_tenant.calls_matching("[:HAS]")
    assert any(
        c.params == {"source_id": "role_b_provider", "target_id": canonical_id} for c in has_writes
    )
    satisfied_by_writes = single_tenant.calls_matching("[:SATISFIED_BY]")
    assert any(
        c.params == {"source_id": "REG-B_req_art_2.1", "target_id": canonical_id}
        for c in satisfied_by_writes
    )


def test_cross_regulation_semantic_match_merges_onto_first_canonical_id(make_emitter) -> None:
    """Increment 16 Scenario A -- AC-003, semantic-not-exact cross-regulation
    match: the second regulation's Obligation has DIFFERENT text (a
    different `obligation_id`) from the first's, but a mocked
    `call_embedding` scores their similarity >= threshold -> merged onto the
    first call's canonical id, same edge-repointing proof as Increment 15.

    Also exercises B2's across-run embedding-backfill mechanism end to end:
    the first call's canonical node was minted with no embedding (nothing
    existed to compare against yet); the second call's semantic comparison
    computes one for it and persists it via `backfill_canonical_embeddings`,
    not merely in-memory.
    """
    emitter, _log_path = make_emitter()
    first_text = "Notify the supervisory authority of the personal data breach."
    second_text = "Alert the relevant regulator about the data breach incident."
    first_id = obligation_id(first_text)
    second_id = obligation_id(second_text)
    assert first_id != second_id

    single_tenant = _FakeSingleTenantGraph()

    first_regulation = _single_obligation_baseline_graph(
        regulation_id="REG-C",
        role_id_value="role_c_controller",
        requirement_id_value="REG-C_req_art_33.1",
        obligation_text=first_text,
    )
    merge_baseline_graph(
        "REG-C",
        baseline_graph=first_regulation,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )
    assert single_tenant.obligation_ids() == {first_id}

    second_regulation = _single_obligation_baseline_graph(
        regulation_id="REG-D",
        role_id_value="role_d_processor",
        requirement_id_value="REG-D_req_art_33.2",
        obligation_text=second_text,
    )
    call_embedding = _ScriptedCallEmbedding({first_text: [1.0, 0.0], second_text: [1.0, 0.0]})
    result_d = merge_baseline_graph(
        "REG-D",
        baseline_graph=second_regulation,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert result_d.obligation_canonical_ids == (first_id,)
    assert result_d.near_misses == ()

    # No second Obligation node was ever minted -- the dedup target was
    # reused, converged onto via a semantic (not exact-key) match.
    assert single_tenant.obligation_ids() == {first_id}
    mint_writes = single_tenant.calls_matching("MERGE (n:Obligation {id: $id}) ON CREATE SET")
    assert len(mint_writes) == 1, "exactly one mint, from REG-C's call -- REG-D semantic-matched"

    has_writes = single_tenant.calls_matching("[:HAS]")
    assert any(
        c.params == {"source_id": "role_d_processor", "target_id": first_id} for c in has_writes
    )
    satisfied_by_writes = single_tenant.calls_matching("[:SATISFIED_BY]")
    assert any(
        c.params == {"source_id": "REG-D_req_art_33.2", "target_id": first_id}
        for c in satisfied_by_writes
    )

    # Bonus: B2's across-run embedding-backfill mechanism actually fired --
    # the first call's canonical node had no embedding; the second call's
    # semantic-match comparison computed one for it and persisted it via
    # backfill_canonical_embeddings (a distinct write from the mint above).
    backfill_writes = single_tenant.calls_matching("WHERE n.embedding IS NULL")
    assert any(c.params is not None and c.params.get("id") == first_id for c in backfill_writes)


def test_cross_regulation_below_threshold_surfaces_near_miss_keeps_both_nodes(
    make_emitter, read_lines
) -> None:
    """Increment 16 Scenario B -- AC-004, below-threshold cross-regulation
    near-miss: the second regulation's Obligation has different text from
    the first's, and a mocked `call_embedding` scores their similarity BELOW
    threshold -> both are kept as SEPARATE canonical nodes (never merged),
    and the pair is surfaced as a `NearMissPair` on the second call's
    `MergeResult`, with a corresponding `outcome="near_miss"` structured log
    entry naming the near-missed incoming id.

    Note on scope: the current `merge_baseline_graph` implementation emits
    this log entry with only `entity_id`/`outcome`/`run_id` -- it does not
    pass `extra=` carrying both ids' texts (see `merge.py`'s near-miss
    emission loop). This test asserts what is actually emitted; it does not
    assert on `extra` content, since that would require a production-code
    change out of this batch's scope (see IMPL_15_16.md for this noted as a
    discovered gap, not silently fixed).
    """
    emitter, log_path = make_emitter()
    first_text = "Retain transaction records for five years."
    second_text = "Conduct an annual penetration test of critical systems."
    first_id = obligation_id(first_text)
    second_id = obligation_id(second_text)
    assert first_id != second_id

    single_tenant = _FakeSingleTenantGraph()

    first_regulation = _single_obligation_baseline_graph(
        regulation_id="REG-E",
        role_id_value="role_e_operator",
        requirement_id_value="REG-E_req_art_5.1",
        obligation_text=first_text,
    )
    with bind_run_context("run-e"):
        merge_baseline_graph(
            "REG-E",
            baseline_graph=first_regulation,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            similarity_threshold=_THRESHOLD,
            emitter=emitter,
        )

    second_regulation = _single_obligation_baseline_graph(
        regulation_id="REG-F",
        role_id_value="role_f_operator",
        requirement_id_value="REG-F_req_art_12.1",
        obligation_text=second_text,
    )
    call_embedding = _ScriptedCallEmbedding({first_text: [1.0, 0.0], second_text: [0.0, 1.0]})
    with bind_run_context("run-f"):
        result_f = merge_baseline_graph(
            "REG-F",
            baseline_graph=second_regulation,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            similarity_threshold=_THRESHOLD,
            call_embedding=call_embedding,
            emitter=emitter,
        )
    emitter.flush()

    assert len(result_f.near_misses) == 1
    near_miss = result_f.near_misses[0]
    assert near_miss.incoming_id == second_id
    assert near_miss.nearest_existing_id == first_id
    assert near_miss.similarity < _THRESHOLD

    # Never merged: two DISTINCT canonical Obligation nodes exist.
    assert single_tenant.obligation_ids() == {first_id, second_id}
    mint_writes = single_tenant.calls_matching("MERGE (n:Obligation {id: $id}) ON CREATE SET")
    assert len(mint_writes) == 2, "both regulations' Obligations were minted -- never merged"

    entries = read_lines(log_path)
    near_miss_entries = [
        e
        for e in entries
        if e.get("action") == "dedupe_canonical_nodes"
        and e.get("outcome") == "near_miss"
        and e.get("run_id") == "run-f"
    ]
    assert len(near_miss_entries) == 1
    assert near_miss_entries[0]["entity_id"] == second_id


def test_cross_regulation_capability_requires_edge_converges_via_exact_key_match(
    make_emitter,
) -> None:
    """Increment 16, extra scenario (PLAN_REVIEWED.md §6.2's "Orchestrator
    correction," found during Increment 12): Capability-side cross-regulation
    convergence via a `REQUIRES` edge. An earlier `persist_rewired_edges`
    design only ever rewrote an edge's Obligation-typed endpoint, leaving a
    `REQUIRES` edge's Capability-typed TARGET pointing at its baseline-local
    id whenever that Capability resolved onto an existing canonical node --
    a silent dangling reference, since a matched Capability is never minted
    as its own node. Increment 12's own test suite proved the fix WITHIN one
    call; this test proves it holds ACROSS two separate
    `merge_baseline_graph` calls too -- the exact cross-regulation scenario
    AC-002/003 describe, applied to Capability instead of Obligation.
    """
    emitter, _log_path = make_emitter()
    shared_capability_name = "Multi-Factor Authentication Capability"
    canonical_capability_id = capability_id(shared_capability_name)

    single_tenant = _FakeSingleTenantGraph()

    first_obligation_text = "Implement strong authentication for remote access."
    first_obligation_id = obligation_id(first_obligation_text)
    first_regulation = _FakeBaselineGraph(
        regulation_properties={"id": "REG-G", "title": "Test Regulation REG-G"},
        role_rows=[["role_g_operator", "Operator", 0.9]],
        requirement_rows=[
            [
                "REG-G_req_art_9.1",
                "Must secure remote access.",
                "requirement",
                0.9,
                "role_g_operator",
            ]
        ],
        obligation_rows=[[first_obligation_id, first_obligation_text, 0.9]],
        capability_rows=[[canonical_capability_id, shared_capability_name, 0.8, None]],
        defines_rows=[["role_g_operator", "Article 9(1)"]],
        expresses_rows=[["REG-G_req_art_9.1", "Article 9(1)"]],
        has_rows=[["role_g_operator", first_obligation_id]],
        satisfied_by_rows=[["REG-G_req_art_9.1", first_obligation_id]],
        requires_rows=[[first_obligation_id, canonical_capability_id]],
    )
    merge_baseline_graph(
        "REG-G",
        baseline_graph=first_regulation,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )
    assert (
        len(single_tenant.calls_matching("MERGE (n:Capability {id: $id}) ON CREATE SET")) == 1
    )

    # Same Obligation text as REG-G's, deliberately: this test's own focus is
    # the Capability side (an exact-key match needs no call_embedding at
    # all), so the Obligation side is kept as a trivial exact match too --
    # a DIFFERENT, semantically-unrelated Obligation text here would send
    # the Obligation dedup pass into find_best_semantic_match with no
    # call_embedding scripted, an unrelated live-network concern this test
    # does not exist to exercise (see Increment 15/16's other tests for
    # dedicated Obligation-side exact/semantic/near-miss coverage).
    second_obligation_text = first_obligation_text
    second_obligation_id = first_obligation_id
    second_regulation = _FakeBaselineGraph(
        regulation_properties={"id": "REG-H", "title": "Test Regulation REG-H"},
        role_rows=[["role_h_operator", "Operator", 0.9]],
        requirement_rows=[
            ["REG-H_req_art_10.1", "Must enforce MFA.", "requirement", 0.9, "role_h_operator"]
        ],
        obligation_rows=[[second_obligation_id, second_obligation_text, 0.9]],
        capability_rows=[[canonical_capability_id, shared_capability_name, 0.8, None]],
        defines_rows=[["role_h_operator", "Article 10(1)"]],
        expresses_rows=[["REG-H_req_art_10.1", "Article 10(1)"]],
        has_rows=[["role_h_operator", second_obligation_id]],
        satisfied_by_rows=[["REG-H_req_art_10.1", second_obligation_id]],
        requires_rows=[[second_obligation_id, canonical_capability_id]],
    )
    result_h = merge_baseline_graph(
        "REG-H",
        baseline_graph=second_regulation,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert result_h.capability_canonical_ids == (canonical_capability_id,)
    # No second Capability node was ever minted -- the dedup target was
    # reused via exact-key match (same capability name -> same
    # capability_id hash across both regulations).
    mint_writes = single_tenant.calls_matching("MERGE (n:Capability {id: $id}) ON CREATE SET")
    assert len(mint_writes) == 1

    requires_writes = single_tenant.calls_matching("[:REQUIRES]")
    assert any(
        c.params == {"source_id": second_obligation_id, "target_id": canonical_capability_id}
        for c in requires_writes
    ), (
        "REQUIRES edge's Capability TARGET must be rewritten to the canonical id "
        "across the two calls -- the exact Increment 12 bug scenario, proven "
        "cross-regulation"
    )
