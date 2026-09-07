"""Tests for `ps_service.restore.restore_instrument`'s widened Policy
convergence (issue #54, S6/B6, AC-BI-021 -- PLAN.md §4.6/§6 S6).

`_run_baseline_merge` is restore's offline counterpart to
`company_merge.merge.merge_baseline_graph`'s live-path Policy convergence
(S4): both call the same `graph_writer.persist_*` functions in the same
write order, differing only in *which* dedup function resolves Policy --
the live path's `dedup.dedupe_canonical_nodes` (LLM-backed
`route_embedding`) versus the offline path's
`dedup.resolve_capability_convergence_offline` (artifact-supplied/cached
embeddings only, never `route_embedding`).

This file proves AC-BI-021 directly: restoring an internal instrument whose
baseline carries a Policy that should semantically converge onto an
already-existing canonical Policy resolves onto the SAME canonical Policy id
`merge_baseline_graph` (S4's live path) would have produced for the
identical scenario -- proven by running both against separately-seeded, but
otherwise identically-shaped, fake single-tenant graphs and comparing the
resulting canonical id (read off the rewired `GOVERNED_BY` edge's target,
since a semantic match mints no new Policy node to inspect directly).

Fakes here mirror `tests/company_merge/test_merge_baseline_graph.py`'s
`_FakeBaselineGraph`/`_FakeSingleTenantGraph` conventions exactly
(duplicated rather than imported cross-directory, matching this test
suite's existing per-component fake convention -- see
`tests/restore/conftest.py`'s own docstring), trimmed to the governance-only
content these tests exercise, plus a `_FakeDb` satisfying
`_run_baseline_merge`'s own `db.select_graph(name)` call shape
(`company_merge.falkordb_client.select_graph`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle

from dataclasses import dataclass

from litellm.types.utils import Embedding, EmbeddingResponse

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.domain_mapper.identity import policy_id

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


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s queries with its own
    scripted row set, dispatched by a distinctive substring -- mirrors
    `test_merge_baseline_graph.py`'s own `_FakeBaselineGraph` exactly,
    trimmed to the governance-layer content these tests exercise (no Role/
    Requirement/Obligation/regulatory-spine edges).
    """

    def __init__(
        self,
        *,
        regulatory_instrument_properties: dict[str, object],
        capability_rows: list[object],
        policy_rows: list[object],
        standard_rows: list[object],
        control_rows: list[object],
        governed_by_rows: list[object],
        supported_by_rows: list[object],
        implemented_by_rows: list[object],
    ) -> None:
        self._regulatory_instrument_properties = regulatory_instrument_properties
        self._capability_rows = capability_rows
        self._policy_rows = policy_rows
        self._standard_rows = standard_rows
        self._control_rows = control_rows
        self._governed_by_rows = governed_by_rows
        self._supported_by_rows = supported_by_rows
        self._implemented_by_rows = implemented_by_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "[e:DEFINES]" in q or "[e:EXPRESSES]" in q:
            return _FakeQueryResult([])
        if "[:HAS]" in q or "[:SATISFIED_BY]" in q or "[:REQUIRES]" in q:
            return _FakeQueryResult([])
        if "[:GOVERNED_BY]" in q:
            return _FakeQueryResult(self._governed_by_rows)
        if "[:SUPPORTED_BY]" in q:
            return _FakeQueryResult(self._supported_by_rows)
        if "[:IMPLEMENTED_BY]" in q:
            return _FakeQueryResult(self._implemented_by_rows)
        if "(n:Policy) RETURN" in q:
            return _FakeQueryResult(self._policy_rows)
        if "(n:Standard) RETURN" in q:
            return _FakeQueryResult(self._standard_rows)
        if "(n:Control) RETURN" in q:
            return _FakeQueryResult(self._control_rows)
        if "n.role_id" in q:
            return _FakeQueryResult([])  # Requirement
        if "n.description" in q:
            return _FakeQueryResult(self._capability_rows)
        if "n.name, n.confidence" in q:
            return _FakeQueryResult([])  # Role
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult([])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
            )
        raise AssertionError(f"unexpected query issued: {q!r}")


def _rows_by_id(rows: list[object]) -> dict[str, list[object]]:
    """Index pre-seeded `[id, ...]` rows by their own first column."""
    indexed: dict[str, list[object]] = {}
    for row in rows:
        row_list = list(cast("list[object]", row))
        indexed[cast("str", row_list[0])] = row_list
    return indexed


class _FakeSingleTenantGraph:
    """Answers `read_existing_canonical_index`'s Capability/Policy read
    queries with pre-seeded rows and records every call (read AND write) it
    receives -- mirrors `test_merge_baseline_graph.py`'s own
    `_FakeSingleTenantGraph`, trimmed to Capability/Policy/Standard/Control.
    """

    def __init__(
        self,
        *,
        capability_rows: list[object] | None = None,
        policy_rows: list[object] | None = None,
    ) -> None:
        self._capabilities: dict[str, list[object]] = _rows_by_id(capability_rows or [])
        self._policies: dict[str, list[object]] = _rows_by_id(policy_rows or [])
        self._standards: dict[str, list[object]] = {}
        self._controls: dict[str, list[object]] = {}
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "(n:Policy) RETURN n.id, n.title, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._policies.values()])
        if "MERGE (n:Standard {id: $id}) SET n += $properties" in q:
            self._set(self._standards, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:Control {id: $id}) SET n += $properties" in q:
            self._set(self._controls, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:Capability {id: $id}) ON CREATE SET" in q:
            self._mint(self._capabilities, params, "name")
            return _FakeQueryResult([])
        if "MERGE (n:Policy {id: $id}) ON CREATE SET" in q:
            self._mint(self._policies, params, "title")
            return _FakeQueryResult([])
        if "MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._capabilities, params)
            return _FakeQueryResult([])
        if "MATCH (n:Policy {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._policies, params)
            return _FakeQueryResult([])
        return _FakeQueryResult([[0]])  # any other write (RegulatoryInstrument, edges, ...)

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

    def _backfill(self, table: dict[str, list[object]], params: dict[str, object] | None) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        row = table.get(node_id)
        if row is None or row[2] is not None:
            return
        row[2] = params["embedding"]

    def writes(self) -> list[_RecordedCall]:
        read_markers = ("RETURN n.id, n.name, n.embedding", "RETURN n.id, n.title, n.embedding")
        return [c for c in self.calls if not any(m in c.query for m in read_markers)]

    def calls_matching(self, substring: str) -> list[_RecordedCall]:
        return [call for call in self.calls if substring in call.query]


class _FakeDb:
    """Satisfies `_run_baseline_merge`'s own `db.select_graph(name)` call
    shape (`company_merge.falkordb_client.select_graph`) -- a plain name ->
    `GraphHandle` lookup, standing in for `_run_baseline_merge`'s two real
    `select_company_merge_graph(db, ...)` calls (the staged baseline graph,
    then the snapshot).
    """

    def __init__(self, graphs: dict[str, GraphHandle]) -> None:
        self._graphs = graphs

    def select_graph(self, name: str) -> GraphHandle:
        return self._graphs[name]


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text` --
    mirrors `test_dedup_semantic_match.py`'s own fake exactly. Used only for
    the LIVE path's `merge_baseline_graph` call -- the restore/offline path
    never accepts one at all.
    """

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


def _governance_baseline(
    *, instrument_id: str, capability_id_value: str, policy_id_value: str, policy_title: str
) -> _FakeBaselineGraph:
    """One Capability, one Policy, one Standard, one Control, fully wired
    with `GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY` -- an internal-sourced
    baseline (mirrors `test_merge_baseline_graph.py`'s own
    `_internal_baseline_with_governance` fixture shape).
    """
    standard_id_value = f"std_{policy_id_value}_v1"
    control_id_value = f"ctrl_{standard_id_value}_manual"
    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": instrument_id, "title": "Engineering Practices"},
        capability_rows=[[capability_id_value, "Engineering Review Capability", 0.8, None]],
        policy_rows=[[policy_id_value, policy_title, "draft", 0.9]],
        standard_rows=[[standard_id_value, "Code Review Standard", "draft", 0.85, None]],
        control_rows=[[control_id_value, "manual", "Peer Review Control", "planned", 0.8, None]],
        governed_by_rows=[[capability_id_value, policy_id_value]],
        supported_by_rows=[[policy_id_value, standard_id_value]],
        implemented_by_rows=[[standard_id_value, control_id_value]],
    )


def test_internal_baseline_restore_converges_policy_matching_live_path(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-021: restoring the same internal instrument through
    `_run_baseline_merge` (S6, offline) resolves its Policy onto the SAME
    canonical id `merge_baseline_graph` (S4, live) would have produced for
    the identical scenario -- both converge, via semantic match, onto an
    already-existing canonical Policy rather than minting a second one.
    """
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    capability_id_value = "cap_engineering_review_abc"

    existing_title = "Engineering Practices Policy"
    existing_policy_id = policy_id(existing_title)
    incoming_title = "Engineering Practice Policy"  # reworded -> distinct baseline-local id
    incoming_policy_id = policy_id(incoming_title)
    assert incoming_policy_id != existing_policy_id

    existing_embedding: list[float] = [1.0, 0.0]
    incoming_embedding_vector: list[float] = [1.0, 0.0]

    # -- (a) the live path: merge_baseline_graph, as S4's ingestion pipeline
    # would call it, against a single-tenant graph pre-seeded with the
    # existing canonical Policy.
    baseline_live = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value=capability_id_value,
        policy_id_value=incoming_policy_id,
        policy_title=incoming_title,
    )
    live_single_tenant = _FakeSingleTenantGraph(
        policy_rows=[[existing_policy_id, existing_title, list(existing_embedding)]],
    )
    call_embedding = _ScriptedCallEmbedding({incoming_title: incoming_embedding_vector})

    live_result = merge_baseline_graph(
        instrument_id,
        baseline_graph=baseline_live,
        single_tenant_graph=live_single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )
    assert live_result.policy_canonical_ids == (existing_policy_id,)

    live_governed_by = live_single_tenant.calls_matching("[:GOVERNED_BY]")
    assert len(live_governed_by) == 1
    assert live_governed_by[0].params is not None
    live_canonical_policy_id = cast("str", live_governed_by[0].params["target_id"])
    assert live_canonical_policy_id == existing_policy_id

    # -- (b) the restore path: _run_baseline_merge (S6, offline), against a
    # SEPARATE but identically-seeded single-tenant snapshot, using an
    # artifact-supplied Policy embedding instead of a live route_embedding
    # call.
    baseline_restore = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value=capability_id_value,
        policy_id_value=incoming_policy_id,
        policy_title=incoming_title,
    )
    restore_snapshot = _FakeSingleTenantGraph(
        policy_rows=[[existing_policy_id, existing_title, list(existing_embedding)]],
    )
    baseline_staged_name = "engprac_baseline__restoring__token"
    snapshot_name = "policy_system__restoring__token"
    fake_db = cast(
        "FalkorDB",
        _FakeDb({baseline_staged_name: baseline_restore, snapshot_name: restore_snapshot}),
    )

    restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        fake_db,
        baseline_staged_name,
        instrument_id,
        {},  # no artifact-supplied Capability embeddings -- Capability mints new either way
        _THRESHOLD,
        snapshot_name,
        emitter,
        {incoming_policy_id: tuple(incoming_embedding_vector)},
    )

    restore_governed_by = restore_snapshot.calls_matching("[:GOVERNED_BY]")
    assert len(restore_governed_by) == 1
    assert restore_governed_by[0].params is not None
    restore_canonical_policy_id = cast("str", restore_governed_by[0].params["target_id"])

    # The actual AC-BI-021 proof: identical canonical Policy id, both paths.
    assert restore_canonical_policy_id == live_canonical_policy_id == existing_policy_id
    assert not restore_snapshot.calls_matching("MERGE (n:Policy {id: $id}) ON CREATE SET"), (
        "no new Policy node should have been minted by the restore path either -- "
        "the semantic-match target was reused, exactly as the live path reused it"
    )


def test_external_baseline_restore_unaffected_by_policy_pass(make_emitter: MakeEmitter) -> None:
    """An external-sourced restore (`baseline.policy_nodes == ()`) is a
    structural no-op for the whole Policy pass -- mirrors
    `test_merge_baseline_graph.py::test_external_baseline_unaffected_by_policy_pass`,
    exercised for `_run_baseline_merge` instead of `merge_baseline_graph`.
    """
    emitter, _log_path = make_emitter()
    instrument_id = "RT60-1.0"
    capability_id_value = "cap_no_governance"

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": instrument_id, "title": "RT60"},
        capability_rows=[[capability_id_value, "Some Capability", 0.8, None]],
        policy_rows=[],
        standard_rows=[],
        control_rows=[],
        governed_by_rows=[],
        supported_by_rows=[],
        implemented_by_rows=[],
    )
    snapshot = _FakeSingleTenantGraph()
    baseline_staged_name = "rt60_baseline__restoring__token"
    snapshot_name = "policy_system__restoring__token"
    fake_db = cast("FalkorDB", _FakeDb({baseline_staged_name: baseline, snapshot_name: snapshot}))

    restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        fake_db,
        baseline_staged_name,
        instrument_id,
        {},
        _THRESHOLD,
        snapshot_name,
        emitter,
    )

    assert not snapshot.calls_matching("(n:Policy) RETURN n.id, n.title, n.embedding")
    assert not any(
        "Policy" in c.query or "Standard" in c.query or "Control" in c.query
        for c in snapshot.writes()
    )
