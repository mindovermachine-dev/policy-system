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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

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


# The one literal read query dedup.read_existing_canonical_index issues
# (Capability only, since #42) -- used to distinguish a "read" call from a
# "write" call in a fake single-tenant graph's call log (test (b)'s
# call-order proof).
_READ_MARKERS = ("RETURN n.id, n.name, n.embedding",)


def _is_read_call(call: _RecordedCall) -> bool:
    return any(marker in call.query for marker in _READ_MARKERS)


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s ten queries with its own
    scripted row set, dispatched by a distinctive substring -- mirrors
    `test_graph_reader.py`'s `_ScriptedFakeGraph` exactly, plus recording
    every call received (needed for test (d)'s zero-calls proof).
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
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
            )
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
    wrote, exactly as AC-002/003/004 require.
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

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "MERGE (n:Obligation {id: $id}) SET n += $properties" in q:
            # #42: Obligation is a passthrough node -- unconditional SET,
            # keyed on id (which is Role-scoped, so a given id only ever
            # originates from one Role/regulation and its props are stable).
            self._set(self._obligations, params, "text")
            return _FakeQueryResult([])
        if "MERGE (n:Capability {id: $id}) ON CREATE SET" in q:
            self._mint(self._capabilities, params, "name")
            return _FakeQueryResult([])
        if "MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._capabilities, params)
            return _FakeQueryResult([])
        return _FakeQueryResult([[0]])

    def _set(
        self,
        table: dict[str, list[object]],
        params: dict[str, object] | None,
        text_key: str,
    ) -> None:
        """Unconditional `MERGE ... SET n += $properties` semantics."""
        assert params is not None
        node_id = cast("str", params["id"])
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [
            node_id,
            properties.get(text_key),
            properties.get("embedding"),
        ]

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
        does) safe regardless.
        """
        assert params is not None
        node_id = cast("str", params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [
            node_id,
            properties.get(text_key),
            properties.get("embedding"),
        ]

    def _backfill(self, table: dict[str, list[object]], params: dict[str, object] | None) -> None:
        """`WHERE n.embedding IS NULL` semantics: a no-op when the id is
        absent from `table` or its embedding is already set -- matches real
        FalkorDB's own guard exactly (PLAN_REVIEWED.md §6.2).
        """
        assert params is not None
        node_id = cast("str", params["id"])
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
        mints a second node for the same canonical concept.
        """
        return frozenset(self._obligations)


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


def _everything_new_baseline_graph() -> _FakeBaselineGraph:
    """AC-001's "everything is new" fixture: one Role, one Requirement, one
    Obligation, one Capability, fully wired edges. No pre-existing canonical
    node exists anywhere, so nothing has anything to converge onto.
    """
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-1.0_req_art_1.1"
    obligation_text = "Report the incident to the competent authority."
    obligation_node_id = obligation_id(role_node_id, obligation_text)
    capability_name = "Incident Reporting Capability"
    capability_node_id = capability_id(capability_name)

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-1.0", "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9]],
        requirement_rows=[
            [
                requirement_node_id,
                "Must report incidents.",
                "requirement",
                0.9,
                role_node_id,
            ]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[capability_node_id, capability_name, 0.8, None]],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, capability_node_id]],
    )


def test_everything_new_writes_every_node_type_directly(
    make_emitter: MakeEmitter,
) -> None:
    """(a) AC-001, "everything is new": an empty existing single-tenant
    graph -> every node type present afterward in the fake single-tenant
    graph's write log, directly (Obligation via unconditional passthrough
    SET since #42; Capability via match_kind="new", nothing to converge
    onto).
    """
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

    assert len(result.obligation_ids) == 1
    assert len(result.capability_canonical_ids) == 1
    assert result.near_misses == ()

    writes = single_tenant.writes()
    assert any("MERGE (n:RegulatoryInstrument" in c.query for c in writes)
    assert any("MERGE (n:Role" in c.query for c in writes)
    assert any("MERGE (n:Requirement" in c.query for c in writes)
    assert any("MERGE (n:Obligation {id: $id}) SET n += $properties" in c.query for c in writes)
    assert any("MERGE (n:Capability {id: $id}) ON CREATE SET" in c.query for c in writes)
    assert any("[:HAS]" in c.query for c in writes)
    assert any("[:SATISFIED_BY]" in c.query for c in writes)
    assert any("[:REQUIRES]" in c.query for c in writes)


def test_both_dedup_reads_complete_before_any_write_call(
    make_emitter: MakeEmitter,
) -> None:
    """(b) call-order proof: the Capability dedup pass completes (its
    read_existing_canonical_index read happens) before any write call
    appears in the fake single_tenant_graph's call log.
    """
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
    assert len(read_positions) == 1
    assert write_positions
    assert max(read_positions) < min(write_positions)


def test_capability_dedup_target_reused_obligation_passed_through_at_edge_level(
    make_emitter: MakeEmitter,
) -> None:
    """(c) Q3's fix, "the Capability dedup target is reused," proven
    end-to-end within ONE call: pre-seed the fake single_tenant_graph with
    one existing canonical Capability node BEFORE calling
    merge_baseline_graph. The Capability resolves via exact-key match (same
    name -> same capability_id hash) -> no new Capability node write, and the
    `REQUIRES` edge targets the pre-existing canonical id.

    The Obligation, since #42, is a passthrough node: it is written with an
    unconditional `SET` under its own baseline-local (Role-scoped) id, and
    the Role's `HAS` / Requirement's `SATISFIED_BY` edges target that same
    id -- never a "canonical" id, because there is no Obligation dedup.
    """
    emitter, _log_path = make_emitter()
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-1.0_req_art_1.1"

    obligation_text = "Report the security incident to the competent authority."
    obligation_node_id = obligation_id(role_node_id, obligation_text)

    shared_capability_name = "Encrypt Data At Rest"
    shared_capability_id = capability_id(shared_capability_name)

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-1.0", "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9]],
        requirement_rows=[
            [
                requirement_node_id,
                "Must report incidents.",
                "requirement",
                0.9,
                role_node_id,
            ]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[shared_capability_id, shared_capability_name, 0.8, None]],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, shared_capability_id]],
    )
    single_tenant = _FakeSingleTenantGraph(
        capability_rows=[[shared_capability_id, shared_capability_name, None]],
    )

    result = merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert result.obligation_ids == (obligation_node_id,)
    assert result.capability_canonical_ids == (shared_capability_id,)

    writes = single_tenant.writes()
    assert not any("MERGE (n:Capability {id: $id}) ON CREATE SET" in c.query for c in writes), (
        "no new Capability node should have been minted -- the dedup target was reused"
    )
    assert any("MERGE (n:Obligation {id: $id}) SET n += $properties" in c.query for c in writes), (
        "the Obligation is written straight through as a passthrough node"
    )

    has_writes = single_tenant.calls_matching("[:HAS]")
    assert len(has_writes) == 1
    assert has_writes[0].params == {
        "source_id": role_node_id,
        "target_id": obligation_node_id,
    }

    satisfied_by_writes = single_tenant.calls_matching("[:SATISFIED_BY]")
    assert len(satisfied_by_writes) == 1
    assert satisfied_by_writes[0].params == {
        "source_id": requirement_node_id,
        "target_id": obligation_node_id,
    }

    requires_writes = single_tenant.calls_matching("[:REQUIRES]")
    assert len(requires_writes) == 1
    assert requires_writes[0].params == {
        "source_id": obligation_node_id,
        "target_id": shared_capability_id,
    }


def test_missing_similarity_threshold_raises_before_any_call() -> None:
    """(d) B1's fix, runtime enforcement: similarity_threshold=None raises
    CompanyMergeConfigurationError; both the fake baseline_graph and fake
    single_tenant_graph receive ZERO calls of any kind (the check fires
    before even the Regulation read).
    """
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


def test_succeeded_log_entry_carries_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Increment 14(a): with bind_run_context("run-x"), the
    outcome="succeeded" entry for action="merge_baseline_graph" carries
    run_id="run-x".
    """
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


def test_dedup_decision_log_entries_carry_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Increment 14(b): at least one per-decision entry for
    action="dedupe_canonical_nodes" also carries run_id="run-x", with
    entity_id equal to one of the resolved nodes' incoming_id and outcome
    equal to its match_kind.
    """
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

    resolved_capability_id = result.capability_canonical_ids[0]
    matching = [e for e in dedup_entries if e.get("entity_id") == resolved_capability_id]
    assert len(matching) == 1
    assert matching[0]["outcome"] == "new"


# ---------------------------------------------------------------------------
# Increments 15-16 (PLAN_REVIEWED.md §10 Batch 8), adapted for issue #42.
# Across two separate merge_baseline_graph calls against the SAME
# accumulating _FakeSingleTenantGraph instance (two regulations merged one
# after another): Obligation is Role-scoped and passed through, never
# deduped, so two sources' duties are always distinct nodes; only Capability
# converges. The Capability REQUIRES-rewiring fix (Increment 12) is proven
# cross-regulation at the end of this section.
# ---------------------------------------------------------------------------


def _single_obligation_baseline_graph(
    *,
    regulatory_instrument_id: str,
    role_id_value: str,
    requirement_id_value: str,
    obligation_text: str,
) -> _FakeBaselineGraph:
    """A minimal baseline graph fixture: one Role, one Requirement, one
    Obligation, `HAS`/`SATISFIED_BY` wired, no Capability.
    """
    obligation_node_id = obligation_id(role_id_value, obligation_text)
    return _FakeBaselineGraph(
        regulatory_instrument_properties={
            "id": regulatory_instrument_id,
            "title": f"Test Regulation {regulatory_instrument_id}",
        },
        role_rows=[[role_id_value, "Manufacturer", 0.9]],
        requirement_rows=[
            [
                requirement_id_value,
                "Must report incidents.",
                "requirement",
                0.9,
                role_id_value,
            ]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[],
        defines_rows=[[role_id_value, "Article 1(1)"]],
        expresses_rows=[[requirement_id_value, "Article 1(1)"]],
        has_rows=[[role_id_value, obligation_node_id]],
        satisfied_by_rows=[[requirement_id_value, obligation_node_id]],
        requires_rows=[],
    )


def test_cross_regulation_obligations_are_passed_through_never_deduped(
    make_emitter: MakeEmitter,
) -> None:
    """#42: two regulations whose duty text is IDENTICAL but whose Roles
    differ (Roles are regulation-scoped) produce two DISTINCT Obligation
    nodes -- each written straight through under its own Role-scoped id, with
    its own single `HAS` edge. Company Merge runs no exact-key or semantic
    dedup for Obligation, and makes zero embedding calls for it.
    """
    emitter, _log_path = make_emitter()
    shared_text = "Report the incident to the competent authority without undue delay."
    id_a = obligation_id("role_a_manufacturer", shared_text)
    id_b = obligation_id("role_b_provider", shared_text)
    assert id_a != id_b

    single_tenant = _FakeSingleTenantGraph()

    merge_baseline_graph(
        "REG-A",
        baseline_graph=_single_obligation_baseline_graph(
            regulatory_instrument_id="REG-A",
            role_id_value="role_a_manufacturer",
            requirement_id_value="REG-A_req_art_1.1",
            obligation_text=shared_text,
        ),
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )
    assert single_tenant.obligation_ids() == {id_a}

    call_embedding = _ScriptedCallEmbedding({})  # must never be called for Obligation
    result_b = merge_baseline_graph(
        "REG-B",
        baseline_graph=_single_obligation_baseline_graph(
            regulatory_instrument_id="REG-B",
            role_id_value="role_b_provider",
            requirement_id_value="REG-B_req_art_2.1",
            obligation_text=shared_text,
        ),
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert result_b.obligation_ids == (id_b,)
    assert result_b.near_misses == ()
    # Both distinct nodes now exist -- never merged.
    assert single_tenant.obligation_ids() == {id_a, id_b}
    # No embedding call was ever made for an Obligation.
    assert call_embedding.calls == []
    # No dedup read query for Obligation was ever issued.
    assert not single_tenant.calls_matching("(n:Obligation) RETURN n.id, n.text, n.embedding")

    has_writes = single_tenant.calls_matching("[:HAS]")
    assert any(c.params == {"source_id": "role_b_provider", "target_id": id_b} for c in has_writes)


def test_cross_regulation_capability_requires_edge_converges_via_exact_key_match(
    make_emitter: MakeEmitter,
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
    first_obligation_id = obligation_id("role_g_operator", first_obligation_text)
    first_regulatory_instrument = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-G", "title": "Test Regulation REG-G"},
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
        baseline_graph=first_regulatory_instrument,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )
    assert len(single_tenant.calls_matching("MERGE (n:Capability {id: $id}) ON CREATE SET")) == 1

    # The Obligation side needs no special handling: since #42 Obligation is
    # a passthrough node (no dedup pass, no call_embedding), so REG-H's
    # Obligation lands under its own Role-scoped id regardless of text.
    second_obligation_text = "Enforce multi-factor authentication for all users."
    second_obligation_id = obligation_id("role_h_operator", second_obligation_text)
    second_regulatory_instrument = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-H", "title": "Test Regulation REG-H"},
        role_rows=[["role_h_operator", "Operator", 0.9]],
        requirement_rows=[
            [
                "REG-H_req_art_10.1",
                "Must enforce MFA.",
                "requirement",
                0.9,
                "role_h_operator",
            ]
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
        baseline_graph=second_regulatory_instrument,
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
