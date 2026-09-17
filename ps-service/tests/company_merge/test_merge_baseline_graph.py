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

import httpx
import openai
import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge import dedup as dedup_module
from ps_service.company_merge.errors import (
    CompanyMergeConfigurationError,
    CompanyMergePersistenceError,
)
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.domain_mapper.identity import capability_id, obligation_id, practice_area_id
from ps_service.llm_interface.errors import LlmProviderError
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
_READ_MARKERS = ("RETURN n.id, n.name, n.embedding", "RETURN n.id, n.title, n.embedding")


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
        policy_rows: list[object] | None = None,
        standard_rows: list[object] | None = None,
        control_rows: list[object] | None = None,
        governed_by_rows: list[object] | None = None,
        supported_by_rows: list[object] | None = None,
        implemented_by_rows: list[object] | None = None,
        practice_area_rows: list[object] | None = None,
        risk_path_rows: list[object] | None = None,
        covers_rows: list[object] | None = None,
        owns_rows: list[object] | None = None,
        mitigated_by_rows: list[object] | None = None,
        verified_by_rows: list[object] | None = None,
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
        self._policy_rows = policy_rows or []
        self._standard_rows = standard_rows or []
        self._control_rows = control_rows or []
        self._governed_by_rows = governed_by_rows or []
        self._supported_by_rows = supported_by_rows or []
        self._implemented_by_rows = implemented_by_rows or []
        self._practice_area_rows = practice_area_rows or []
        self._risk_path_rows = risk_path_rows or []
        self._covers_rows = covers_rows or []
        self._owns_rows = owns_rows or []
        self._mitigated_by_rows = mitigated_by_rows or []
        self._verified_by_rows = verified_by_rows or []
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
        if "[:GOVERNED_BY]" in q:
            return _FakeQueryResult(self._governed_by_rows)
        if "[:SUPPORTED_BY]" in q:
            return _FakeQueryResult(self._supported_by_rows)
        if "[:IMPLEMENTED_BY]" in q:
            return _FakeQueryResult(self._implemented_by_rows)
        if "[:COVERS]" in q:
            return _FakeQueryResult(self._covers_rows)
        if "[:OWNS]" in q:
            return _FakeQueryResult(self._owns_rows)
        if "[:MITIGATED_BY]" in q:
            return _FakeQueryResult(self._mitigated_by_rows)
        if "[:VERIFIED_BY]" in q:
            return _FakeQueryResult(self._verified_by_rows)
        if "(n:Policy) RETURN" in q:
            return _FakeQueryResult(self._policy_rows)
        if "(n:Standard) RETURN" in q:
            return _FakeQueryResult(self._standard_rows)
        if "(n:Control) RETURN" in q:
            return _FakeQueryResult(self._control_rows)
        if "(n:PracticeArea) RETURN" in q:
            return _FakeQueryResult(self._practice_area_rows)
        if "(n:RiskPath) RETURN" in q:
            return _FakeQueryResult(self._risk_path_rows)
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
        policy_rows: list[object] | None = None,
        practice_area_rows: list[object] | None = None,
        risk_path_rows: list[object] | None = None,
    ) -> None:
        self._obligations: dict[str, list[object]] = {}
        for row in obligation_rows or []:
            row_list = list(cast("list[object]", row))
            self._obligations[cast("str", row_list[0])] = row_list
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
        # issue #106: `practice_area_rows`/`risk_path_rows` mirror
        # `capability_rows`/`policy_rows`'s "seed pre-existing rows" role,
        # but store the FULL properties dict per row (`[id, properties]`)
        # rather than a fixed `[id, text, embedding]` shape -- PracticeArea/
        # RiskPath have no embedding and richer properties than a single
        # text field, and AC-BI-007 needs to assert individual property
        # values (e.g. `description`) are unchanged after a merge.
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
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "(n:Policy) RETURN n.id, n.title, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._policies.values()])
        if "MERGE (n:Obligation {id: $id}) ON CREATE SET" in q:
            # #42: Obligation is a passthrough node, keyed on id (Role-scoped).
            # Issue #28 AC-BI-006's fix: `ON CREATE SET`, not unconditional
            # `SET` -- a recurring id (e.g. a second merge run against a
            # drifted baseline) must never overwrite the existing node.
            self._mint(self._obligations, params, "text")
            return _FakeQueryResult([])
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
            # issue #106, AC-BI-008: `validate_classification_edge_endpoints`'s
            # batched existence check -- answer with whichever requested ids
            # are actually present in ANY of this fake's tracked node tables
            # (mirrors a real `MATCH (n {id: id})` with no label filter).
            assert params is not None
            requested_ids = cast("list[str]", params["ids"])
            known_ids = (
                set(self._obligations)
                | set(self._capabilities)
                | set(self._policies)
                | set(self._standards)
                | set(self._controls)
                | set(self._practice_areas)
                | set(self._risk_paths)
            )
            return _FakeQueryResult([[rid] for rid in requested_ids if rid in known_ids])
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

    def _mint_properties(
        self,
        table: dict[str, dict[str, object]],
        params: dict[str, object] | None,
    ) -> None:
        """`MERGE ... ON CREATE SET` semantics over a FULL properties dict.

        Unlike `_mint` (which tracks only one text field + embedding, per
        Capability/Policy's fixed row shape), this preserves every property
        as-is -- used for PracticeArea/RiskPath (issue #106), whose
        properties are richer (`name`/`status`/`description`/`version`/
        `owner_id`/`risk_type`). A node id already present in `table` fires
        no `SET` at all -- matches real FalkorDB's `ON CREATE SET` and is
        the exact mechanism AC-BI-007 proves.
        """
        assert params is not None
        node_id = cast("str", params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = dict(properties)

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

    def practice_area_properties(self, node_id: str) -> dict[str, object] | None:
        """Issue #106: the current stored properties for a PracticeArea `node_id`.

        `None` if no `MERGE (n:PracticeArea {id: node_id}) ON CREATE SET`
        call has ever landed for this id -- a public read-back accessor,
        mirroring `obligation_ids()`'s role, so tests never reach into
        `_practice_areas` directly.
        """
        return self._practice_areas.get(node_id)

    def risk_path_properties(self, node_id: str) -> dict[str, object] | None:
        """Issue #106: the current stored properties for a RiskPath `node_id`.

        `None` if no `MERGE (n:RiskPath {id: node_id}) ON CREATE SET` call
        has ever landed for this id -- see `practice_area_properties`.
        """
        return self._risk_paths.get(node_id)


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


class _ScriptedCallEmbeddingWithFailure:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text` --
    a scripted `Exception` value is raised instead of returning a response,
    mirroring `test_dedup_abort_on_embedding_failure.py`'s
    `_ScriptedCallEmbedding` (AC-BI-010's own established idiom for this
    scenario). A separate class from `_ScriptedCallEmbedding` above, which
    every OTHER test in this file scripts with plain vectors only.
    """

    def __init__(self, vectors_by_text: dict[str, list[float] | Exception]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        scripted = self._vectors_by_text.get(text)
        if scripted is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        if isinstance(scripted, Exception):
            raise scripted
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=scripted, index=0, object="embedding")]
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
    graph's write log, directly (Obligation via passthrough `ON CREATE SET`
    since #42/#28 AC-BI-006; Capability via match_kind="new", nothing to
    converge onto).
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
    assert any("MERGE (n:Obligation {id: $id}) ON CREATE SET" in c.query for c in writes)
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

    The Obligation, since #42, is a passthrough node: it is written with
    `ON CREATE SET` (issue #28 AC-BI-006's fix) under its own baseline-local
    (Role-scoped) id, and the Role's `HAS` / Requirement's `SATISFIED_BY`
    edges target that same id -- never a "canonical" id, because there is no
    Obligation dedup.
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
    assert any("MERGE (n:Obligation {id: $id}) ON CREATE SET" in c.query for c in writes), (
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


# ---------------------------------------------------------------------------
# Issue #54, S4 -- Policy convergence + Standard/Control passthrough +
# governance-edge rewiring, guarded by `if graph.policy_nodes:`.
# ---------------------------------------------------------------------------


def _internal_baseline_with_governance() -> _FakeBaselineGraph:
    """One Capability, one Policy, one Standard, one Control, fully wired
    with `GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY` -- an internal-sourced
    baseline (`DeriveGovernanceArtifacts` ran). No Role/Requirement/
    Obligation content is needed for these tests -- only the governance
    layer's own merge behavior is under test.
    """
    capability_id_value = "cap_engineering_review_abc"
    policy_id_value = "pol_engineering_practices_xyz"
    standard_id_value = "std_pol_engineering_practices_xyz_v1"
    control_id_value = "ctrl_std_pol_engineering_practices_xyz_v1_manual"

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "ENGPRAC-3.0", "title": "Engineering Practices"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[capability_id_value, "Engineering Review Capability", 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        policy_rows=[
            [policy_id_value, "Engineering Practices Policy", "draft", 0.9],
        ],
        standard_rows=[
            [standard_id_value, "Code Review Standard", "draft", 0.85, None],
        ],
        control_rows=[
            [control_id_value, "manual", "Peer Review Control", "planned", 0.8, None],
        ],
        governed_by_rows=[[capability_id_value, policy_id_value]],
        supported_by_rows=[[policy_id_value, standard_id_value]],
        implemented_by_rows=[[standard_id_value, control_id_value]],
    )


def test_internal_baseline_merges_policy_standard_control_into_single_tenant(
    make_emitter: MakeEmitter,
) -> None:
    """S4: an internal-sourced baseline (`graph.policy_nodes` non-empty)
    runs the Policy dedup pass, mints the canonical Policy node, persists
    Standard/Control as passthrough, and rewrites `GOVERNED_BY`/
    `SUPPORTED_BY`/`IMPLEMENTED_BY` -- closing the loop to a real UC-3
    `Capability-[:GOVERNED_BY]->Policy-[:SUPPORTED_BY]->Standard-
    [:IMPLEMENTED_BY]->Control` traversal in the single-tenant graph.
    """
    emitter, _log_path = make_emitter()
    baseline = _internal_baseline_with_governance()
    single_tenant = _FakeSingleTenantGraph()

    result = merge_baseline_graph(
        "ENGPRAC-3.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert result.policy_canonical_ids == ("pol_engineering_practices_xyz",)

    writes = single_tenant.writes()
    assert any("MERGE (n:Policy {id: $id}) ON CREATE SET" in c.query for c in writes)
    assert any("MERGE (n:Standard {id: $id}) SET n += $properties" in c.query for c in writes)
    assert any("MERGE (n:Control {id: $id}) SET n += $properties" in c.query for c in writes)

    governed_by_writes = single_tenant.calls_matching("[:GOVERNED_BY]")
    assert len(governed_by_writes) == 1
    assert governed_by_writes[0].params == {
        "source_id": "cap_engineering_review_abc",
        "target_id": "pol_engineering_practices_xyz",
    }

    supported_by_writes = single_tenant.calls_matching("[:SUPPORTED_BY]")
    assert len(supported_by_writes) == 1
    assert supported_by_writes[0].params == {
        "source_id": "pol_engineering_practices_xyz",
        "target_id": "std_pol_engineering_practices_xyz_v1",
    }

    implemented_by_writes = single_tenant.calls_matching("[:IMPLEMENTED_BY]")
    assert len(implemented_by_writes) == 1
    assert implemented_by_writes[0].params == {
        "source_id": "std_pol_engineering_practices_xyz_v1",
        "target_id": "ctrl_std_pol_engineering_practices_xyz_v1_manual",
    }


def _baseline_graph_with_classification_nodes(
    *,
    practice_area_rows: list[object],
    risk_path_rows: list[object],
    extra_capability_rows: list[object] | None = None,
    covers_rows: list[object] | None = None,
    owns_rows: list[object] | None = None,
    mitigated_by_rows: list[object] | None = None,
    verified_by_rows: list[object] | None = None,
    policy_rows: list[object] | None = None,
    standard_rows: list[object] | None = None,
    control_rows: list[object] | None = None,
) -> _FakeBaselineGraph:
    """`_everything_new_baseline_graph()`'s regulatory spine, plus
    PracticeArea/RiskPath rows (issue #106) -- used by the AC-BI-003/007/
    008/009/011 tests below, which only care about the classification-layer
    behavior, not the regulatory spine itself. `extra_capability_rows`/
    `covers_rows`/etc. let a test add classification-edge content without
    every caller having to restate the whole regulatory spine.
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
        capability_rows=[
            [capability_node_id, capability_name, 0.8, None],
            *(extra_capability_rows or []),
        ],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, capability_node_id]],
        practice_area_rows=practice_area_rows,
        risk_path_rows=risk_path_rows,
        covers_rows=covers_rows,
        owns_rows=owns_rows,
        mitigated_by_rows=mitigated_by_rows,
        verified_by_rows=verified_by_rows,
        policy_rows=policy_rows,
        standard_rows=standard_rows,
        control_rows=control_rows,
    )


def test_practice_area_and_risk_path_nodes_pass_through_to_single_tenant(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-003 (PracticeArea+RiskPath halves): a baseline carrying
    PracticeArea/RiskPath nodes (with name/description/status/version/
    owner_id or risk_type) merges and both land in the single-tenant graph
    with every property intact -- issue #106's exact-identity passthrough
    (`graph_writer.persist_practice_area_and_risk_path_passthrough`).
    """
    emitter, _log_path = make_emitter()
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    risk_path_id_value = "rp_secure_build_release_d93f8a"

    baseline = _baseline_graph_with_classification_nodes(
        practice_area_rows=[
            [
                practice_area_id_value,
                "Secure SDLC",
                "active",
                "Secure development lifecycle practices",
                "1.0",
                "role_ciso",
            ]
        ],
        risk_path_rows=[
            [
                risk_path_id_value,
                "Secure Build & Release",
                "active",
                "Risks in the build/release pipeline",
                "operational",
                "2.0",
            ]
        ],
    )
    single_tenant = _FakeSingleTenantGraph()

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert single_tenant.practice_area_properties(practice_area_id_value) == {
        "name": "Secure SDLC",
        "status": "active",
        "description": "Secure development lifecycle practices",
        "version": "1.0",
        "owner_id": "role_ciso",
    }
    assert single_tenant.risk_path_properties(risk_path_id_value) == {
        "name": "Secure Build & Release",
        "status": "active",
        "description": "Risks in the build/release pipeline",
        "risk_type": "operational",
        "version": "2.0",
    }
    writes = single_tenant.writes()
    assert any("MERGE (n:PracticeArea {id: $id}) ON CREATE SET" in c.query for c in writes)
    assert any("MERGE (n:RiskPath {id: $id}) ON CREATE SET" in c.query for c in writes)


def test_practice_area_and_risk_path_properties_unchanged_when_node_already_exists(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-007 (both halves), CHANGES.md Appendix A3: a PracticeArea/
    RiskPath node that already exists in the single-tenant graph keeps its
    existing properties after the merge -- `MERGE ... ON CREATE SET`
    semantics, not an overwrite -- even though the incoming baseline node
    shares the same `id` but carries different property values. The
    `MERGE ... ON CREATE SET` query is still issued (proving
    `persist_practice_area_and_risk_path_passthrough` ran unconditionally);
    the no-op itself is FalkorDB's own `ON CREATE SET` guarantee, not an
    `if` in application code.
    """
    emitter, _log_path = make_emitter()
    practice_area_id_value = "pa_existing"
    risk_path_id_value = "rp_existing"

    baseline = _baseline_graph_with_classification_nodes(
        practice_area_rows=[
            [
                practice_area_id_value,
                "Secure SDLC",
                "active",
                "incoming-should-not-apply",
                "2.0",
                "role_incoming",
            ]
        ],
        risk_path_rows=[
            [
                risk_path_id_value,
                "Secure Build & Release",
                "active",
                "incoming-should-not-apply",
                "operational",
                "2.0",
            ]
        ],
    )
    single_tenant = _FakeSingleTenantGraph(
        practice_area_rows=[
            [
                practice_area_id_value,
                {
                    "name": "Secure SDLC",
                    "status": "active",
                    "description": "original",
                    "version": "1.0",
                    "owner_id": "role_ciso",
                },
            ]
        ],
        risk_path_rows=[
            [
                risk_path_id_value,
                {
                    "name": "Secure Build & Release",
                    "status": "active",
                    "description": "original",
                    "risk_type": "operational",
                    "version": "1.0",
                },
            ]
        ],
    )

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert single_tenant.practice_area_properties(practice_area_id_value) == {
        "name": "Secure SDLC",
        "status": "active",
        "description": "original",
        "version": "1.0",
        "owner_id": "role_ciso",
    }
    assert single_tenant.risk_path_properties(risk_path_id_value) == {
        "name": "Secure Build & Release",
        "status": "active",
        "description": "original",
        "risk_type": "operational",
        "version": "1.0",
    }
    assert single_tenant.calls_matching("MERGE (n:PracticeArea {id: $id}) ON CREATE SET")
    assert single_tenant.calls_matching("MERGE (n:RiskPath {id: $id}) ON CREATE SET")


def test_capability_near_miss_persists_pending_review_node(make_emitter: MakeEmitter) -> None:
    """Issue #35, Slice 1 (AC-BI-001/AC-BI-002): a below-threshold near-miss
    surfaced during the Capability dedup pass is persisted as a
    `PendingReview` node -- wired at the same call site
    `_log_dedup_decisions(capability_dedup, ...)` already logs it from
    (PLAN.md §4.1).
    """
    emitter, _log_path = make_emitter()
    existing_capability_id = "capability_existing_conduct_risk_assessment"
    existing_capability_name = "Conduct Risk Assessment Capability"
    incoming_capability_id = "capability_incoming_report_incident"
    incoming_capability_name = "Report Incident Capability"
    existing_vector = [1.0, 0.0]
    incoming_vector = [0.6, 0.8]  # cosine similarity with existing_vector is well below _THRESHOLD

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-NM", "title": "Test Regulation"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[incoming_capability_id, incoming_capability_name, 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
    )
    single_tenant = _FakeSingleTenantGraph(
        capability_rows=[[existing_capability_id, existing_capability_name, existing_vector]],
    )
    call_embedding = _ScriptedCallEmbedding({incoming_capability_name: incoming_vector})

    merge_baseline_graph(
        "REG-NM",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    pending_review_writes = single_tenant.calls_matching("CREATE (r:PendingReview")
    assert len(pending_review_writes) == 1
    params = pending_review_writes[0].params
    assert params is not None
    assert params["kind"] == "Capability"
    assert params["incoming_id"] == incoming_capability_id
    assert params["incoming_text"] == incoming_capability_name
    assert params["nearest_existing_id"] == existing_capability_id
    assert params["nearest_existing_text"] == existing_capability_name


def test_policy_near_miss_persists_pending_review_node(make_emitter: MakeEmitter) -> None:
    """Issue #35, Slice 1: same wiring proof as the Capability case above, but
    for the Policy pass's own `_log_dedup_decisions(policy_dedup, ...)` call
    site inside `_finish_policy_pass` (PLAN.md §4.1's second call site).
    """
    emitter, _log_path = make_emitter()
    existing_policy_id = "pol_existing_engineering_practices"
    existing_policy_title = "Engineering Practices Policy"
    incoming_policy_id = "pol_incoming_secure_development"
    incoming_policy_title = "Secure Development Policy"
    existing_vector = [1.0, 0.0]
    incoming_vector = [0.6, 0.8]  # cosine similarity with existing_vector is well below _THRESHOLD

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "ENGPRAC-NM", "title": "Engineering Practices"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        policy_rows=[[incoming_policy_id, incoming_policy_title, "draft", 0.9]],
    )
    single_tenant = _FakeSingleTenantGraph(
        policy_rows=[[existing_policy_id, existing_policy_title, existing_vector]],
    )
    call_embedding = _ScriptedCallEmbedding({incoming_policy_title: incoming_vector})

    merge_baseline_graph(
        "ENGPRAC-NM",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    pending_review_writes = single_tenant.calls_matching("CREATE (r:PendingReview")
    assert len(pending_review_writes) == 1
    params = pending_review_writes[0].params
    assert params is not None
    assert params["kind"] == "Policy"
    assert params["incoming_id"] == incoming_policy_id
    assert params["incoming_text"] == incoming_policy_title
    assert params["nearest_existing_id"] == existing_policy_id
    assert params["nearest_existing_text"] == existing_policy_title


def test_merge_result_reports_pending_review_count_including_policy_pass(
    make_emitter: MakeEmitter,
) -> None:
    """Issue #35, Slice 5 (AC-BI-010): `MergeResult.pending_review_count` is
    the run-scoped count of `PendingReview` nodes THIS call actually
    persisted -- one per `NearMissPair` surfaced by either the Capability or
    the Policy pass in this run (PLAN.md §5), not "every unresolved review
    ever" (which would need a fresh graph read this function has no other
    reason to perform). A below-threshold Capability pair and a
    below-threshold Policy pair both surface in the same run here, so
    `pending_review_count == 2`, matching the two `CREATE (r:PendingReview
    ...)` writes actually issued.
    """
    emitter, _log_path = make_emitter()
    existing_capability_id = "capability_existing_conduct_risk_assessment"
    existing_capability_name = "Conduct Risk Assessment Capability"
    incoming_capability_id = "capability_incoming_report_incident"
    incoming_capability_name = "Report Incident Capability"
    existing_policy_id = "pol_existing_engineering_practices"
    existing_policy_title = "Engineering Practices Policy"
    incoming_policy_id = "pol_incoming_secure_development"
    incoming_policy_title = "Secure Development Policy"
    existing_vector = [1.0, 0.0]
    incoming_vector = [0.6, 0.8]  # cosine similarity with existing_vector is well below _THRESHOLD

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-NM-2", "title": "Test Regulation"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[incoming_capability_id, incoming_capability_name, 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        policy_rows=[[incoming_policy_id, incoming_policy_title, "draft", 0.9]],
    )
    single_tenant = _FakeSingleTenantGraph(
        capability_rows=[[existing_capability_id, existing_capability_name, existing_vector]],
        policy_rows=[[existing_policy_id, existing_policy_title, existing_vector]],
    )
    call_embedding = _ScriptedCallEmbedding(
        {
            incoming_capability_name: incoming_vector,
            incoming_policy_title: incoming_vector,
        }
    )

    result = merge_baseline_graph(
        "REG-NM-2",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert result.pending_review_count == 2
    pending_review_writes = single_tenant.calls_matching("CREATE (r:PendingReview")
    assert len(pending_review_writes) == 2


def test_external_baseline_unaffected_by_policy_pass(make_emitter: MakeEmitter) -> None:
    """S4: an external-sourced baseline (`graph.policy_nodes == ()`) is a
    structural no-op for the whole Policy pass -- `MergeResult.
    policy_canonical_ids == ()`, no Policy dedup read ever issued, no
    Policy/Standard/Control write ever issued.
    """
    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()  # Capability-only, no governance rows
    single_tenant = _FakeSingleTenantGraph()

    result = merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert result.policy_canonical_ids == ()
    assert not single_tenant.calls_matching("(n:Policy) RETURN n.id, n.title, n.embedding")
    assert not any(
        "Policy" in c.query or "Standard" in c.query or "Control" in c.query
        for c in single_tenant.writes()
    )


# ---------------------------------------------------------------------------
# Issue #106 -- COVERS/OWNS/MITIGATED_BY/VERIFIED_BY edge rewiring,
# AC-BI-006's structural no-dedup guarantee, AC-BI-008's pre-write
# validation, AC-BI-009's structural no-op, AC-BI-010's abort-with-no-
# partial-write, and AC-BI-011's semantic log counts.
# ---------------------------------------------------------------------------


def test_external_baseline_unaffected_by_classification_pass(make_emitter: MakeEmitter) -> None:
    """AC-BI-009: an external-sourced baseline (no `internal_seed` content
    at all) issues NO query matching any of the six classification-layer
    markers -- checked against `single_tenant.calls` (every call, read AND
    write), the stronger claim `validate_classification_edge_endpoints`'s
    early-return (before issuing its own `UNWIND` read) makes true.
    """
    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()  # Capability-only, no classification rows
    single_tenant = _FakeSingleTenantGraph()

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert not any(
        label in c.query
        for c in single_tenant.calls
        for label in ("PracticeArea", "RiskPath", "COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY")
    )


def test_two_baselines_authoring_same_practice_area_name_converge_on_one_node(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006 (CHANGES.md Appendix A2): two independent baselines
    (simulating two separate `internal_seed` mints of the same PracticeArea
    `name`) arrive with the SAME content-hashed id, computed via the REAL
    `practice_area_id` -- not hand-assigned -- because that is how real
    convergence happens upstream of Company Merge (PLAN.md §1.1). A single
    `merge_baseline_graph` call never recomputes this id; two SEPARATE calls
    against the SAME `_FakeSingleTenantGraph` instance prove real
    cross-baseline convergence, using the fake's own documented cross-call
    accumulation support.
    """
    shared_id = practice_area_id("Secure SDLC")
    cap_a_name = "Encrypt Data At Rest Capability"
    cap_a_id = capability_id(cap_a_name)
    cap_b_name = "Rotate Encryption Keys Capability"
    cap_b_id = capability_id(cap_b_name)

    emitter, _log_path = make_emitter()
    single_tenant = _FakeSingleTenantGraph()

    baseline_a = _classification_only_baseline_graph(
        regulatory_instrument_id="REG-A",
        capability_rows=[[cap_a_id, cap_a_name, 0.8, None]],
        practice_area_rows=[[shared_id, "Secure SDLC", "draft", None, None, None]],
        covers_rows=[[shared_id, cap_a_id]],
    )
    baseline_b = _classification_only_baseline_graph(
        regulatory_instrument_id="REG-B",
        capability_rows=[[cap_b_id, cap_b_name, 0.8, None]],
        practice_area_rows=[[shared_id, "Secure SDLC", "draft", None, None, None]],
        covers_rows=[[shared_id, cap_b_id]],
    )
    # Orthogonal vectors -- cosine similarity 0, safely below _THRESHOLD --
    # so CAP_A/CAP_B never spuriously converge onto each other; only the
    # PracticeArea id (computed structurally, never via embedding) converges.
    call_embedding = _ScriptedCallEmbedding({cap_a_name: [1.0, 0.0], cap_b_name: [0.0, 1.0]})

    merge_baseline_graph(
        "REG-A",
        baseline_graph=baseline_a,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )
    merge_baseline_graph(
        "REG-B",
        baseline_graph=baseline_b,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    # Exactly one PracticeArea MERGE target across both calls.
    practice_area_merges = single_tenant.calls_matching("MERGE (n:PracticeArea {id: $id})")
    assert practice_area_merges
    assert {c.params["id"] for c in practice_area_merges if c.params is not None} == {shared_id}

    # Both baselines' COVERS edges resolve onto the same source id.
    covers_writes = single_tenant.calls_matching("[:COVERS]")
    assert {c.params["source_id"] for c in covers_writes if c.params is not None} == {shared_id}
    assert {c.params["target_id"] for c in covers_writes if c.params is not None} == {
        cap_a_id,
        cap_b_id,
    }

    # No RouteEmbedding-reachable call was ever made with the PracticeArea's
    # own name -- only the two Capability names, proving the PracticeArea
    # convergence above was never routed through any embedding comparison.
    assert set(call_embedding.calls) == {cap_a_name, cap_b_name}
    assert "Secure SDLC" not in call_embedding.calls


def test_merge_baseline_graph_never_dedupes_practice_area_or_risk_path(
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006, within-a-single-call structural proof (PLAN.md §4.5): a
    baseline carrying two PracticeArea rows that both mint to the SAME
    content-hashed id (simulating two separate incoming entries for the
    same `name`, converging structurally per PLAN §1.1) plus two COVERS
    edges from that shared id to two different Capabilities. Monkeypatching
    `dedup.dedupe_canonical_nodes` to record every `kind=` it is called with
    proves it is NEVER invoked with `"PracticeArea"`/`"RiskPath"` -- only
    `"Capability"` (this fixture carries no Policy content).
    """
    recorded_kinds: list[str] = []
    real_dedupe_canonical_nodes = dedup_module.dedupe_canonical_nodes

    def _recording_wrapper(*args: object, **kwargs: object) -> object:
        recorded_kinds.append(cast("str", kwargs["kind"]))
        return real_dedupe_canonical_nodes(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dedup_module, "dedupe_canonical_nodes", _recording_wrapper)

    shared_pa_id = practice_area_id("Secure SDLC")
    cap_a_id = "cap_encrypt_data_at_rest_abc"
    cap_b_id = "cap_rotate_encryption_keys_def"

    emitter, _log_path = make_emitter()
    baseline = _classification_only_baseline_graph(
        regulatory_instrument_id="REG-1.0",
        capability_rows=[
            [cap_a_id, "Encrypt Data At Rest Capability", 0.8, None],
            [cap_b_id, "Rotate Encryption Keys Capability", 0.8, None],
        ],
        # Two rows, same id -- simulating two separate incoming PracticeArea
        # entries that both minted the identical practice_area_id("Secure
        # SDLC") hash before Company Merge ever saw them (PLAN.md §1.1).
        practice_area_rows=[
            [shared_pa_id, "Secure SDLC", "draft", None, None, None],
            [shared_pa_id, "Secure SDLC", "draft", None, None, None],
        ],
        covers_rows=[[shared_pa_id, cap_a_id], [shared_pa_id, cap_b_id]],
    )
    single_tenant = _FakeSingleTenantGraph()
    # The single-tenant graph starts empty, so the FIRST capability minted
    # this run needs no embedding call at all (an empty working index
    # short-circuits `find_best_semantic_match`) -- but the SECOND
    # capability's own semantic-match scan is scored against the growing
    # same-run working index (which now contains the first mint), so it
    # still needs its own embedding call even though same-run mints are
    # never eligible merge targets (issue #30). Both names are scripted so
    # neither call falls through to a real, unconfigured LLM provider.
    call_embedding = _ScriptedCallEmbedding(
        {
            "Encrypt Data At Rest Capability": [1.0, 0.0],
            "Rotate Encryption Keys Capability": [0.0, 1.0],
        }
    )

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert recorded_kinds == ["Capability"]

    # Exactly one PracticeArea node: both MERGE calls target the SAME id
    # (the second is a database-engine no-op, `ON CREATE SET` against an
    # id already minted by the first).
    merges = single_tenant.calls_matching("MERGE (n:PracticeArea {id: $id}) ON CREATE SET")
    assert len(merges) == 2
    assert {c.params["id"] for c in merges if c.params is not None} == {shared_pa_id}

    # That one node carries both incoming rows' edges.
    covers_writes = single_tenant.calls_matching("[:COVERS]")
    assert {c.params["source_id"] for c in covers_writes if c.params is not None} == {shared_pa_id}
    assert {c.params["target_id"] for c in covers_writes if c.params is not None} == {
        cap_a_id,
        cap_b_id,
    }


def test_missing_classification_edge_endpoint_raises_before_any_classification_edge_write(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-008 (CHANGES.md row 5): a `COVERS` edge referencing an endpoint
    id absent from BOTH the baseline and the single-tenant graph raises
    `CompanyMergePersistenceError` before any of the four classification
    EDGE types is written -- checked via `calls_matching` on each of
    `[:COVERS]`/`[:OWNS]`/`[:MITIGATED_BY]`/`[:VERIFIED_BY]`, NOT a blanket
    "zero writes" claim, since the PracticeArea NODE write is accepted
    precedent to already have landed by this point (mirrors
    `persist_role_and_requirement_passthrough`'s own "nodes before edges"
    ordering).
    """
    emitter, _log_path = make_emitter()
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    baseline = _baseline_graph_with_classification_nodes(
        practice_area_rows=[[practice_area_id_value, "Secure SDLC", "active", None, None, None]],
        risk_path_rows=[],
        covers_rows=[[practice_area_id_value, "cap_never_persisted_anywhere"]],
    )
    single_tenant = _FakeSingleTenantGraph()

    with pytest.raises(CompanyMergePersistenceError, match="cap_never_persisted_anywhere"):
        merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            similarity_threshold=_THRESHOLD,
            emitter=emitter,
        )

    for marker in ("[:COVERS]", "[:OWNS]", "[:MITIGATED_BY]", "[:VERIFIED_BY]"):
        assert single_tenant.calls_matching(marker) == []
    # Accepted precedent: the PracticeArea node write already landed before
    # the edge-endpoint validation ran.
    assert single_tenant.calls_matching("MERGE (n:PracticeArea {id: $id}) ON CREATE SET")


def _baseline_graph_with_classification_content() -> _FakeBaselineGraph:
    """AC-BI-010 fixture (CHANGES.md Appendix A1): one NEW Capability (no
    existing match in the single-tenant graph, forcing an embedding call),
    one PracticeArea, one RiskPath, one COVERS edge, one MITIGATED_BY edge
    -- so a false "no classification writes happened" pass can't hide a bug
    where classification writes occur BEFORE the Capability dedup call.
    """
    incoming_capability_id = "cap_incoming_report_incident"
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    risk_path_id_value = "rp_secure_build_release_d93f8a"

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-BI-010", "title": "Test Regulation"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[incoming_capability_id, "Report Incident Capability", 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        practice_area_rows=[[practice_area_id_value, "Secure SDLC", "active", None, None, None]],
        risk_path_rows=[[risk_path_id_value, "Secure Build & Release", "active", None, None, None]],
        covers_rows=[[practice_area_id_value, incoming_capability_id]],
        mitigated_by_rows=[[risk_path_id_value, incoming_capability_id]],
    )


def test_no_classification_writes_when_capability_embedding_fails(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-010 (CHANGES.md Appendix A1): if Capability dedup's embedding
    call raises `LlmProviderError`, `merge_baseline_graph` never reaches
    `_persist_classification_passthrough` -- zero PracticeArea/RiskPath node
    writes and zero COVERS/OWNS/MITIGATED_BY/VERIFIED_BY edge writes have
    occurred by the time the exception propagates.
    """
    emitter, _log_path = make_emitter()
    baseline = _baseline_graph_with_classification_content()
    existing_capability_id = "capability_existing_conduct_risk_assessment"
    existing_capability_name = "Conduct Risk Assessment Capability"
    incoming_capability_name = "Report Incident Capability"
    single_tenant = _FakeSingleTenantGraph(
        # A non-empty existing index forces `find_best_semantic_match` to
        # actually attempt an embedding call for the incoming Capability
        # (an empty index would short-circuit with zero calls, per
        # `find_best_semantic_match`'s own docstring).
        capability_rows=[[existing_capability_id, existing_capability_name, [1.0, 0.0]]],
    )
    call_embedding = _ScriptedCallEmbeddingWithFailure(
        {
            incoming_capability_name: openai.APIConnectionError(
                request=httpx.Request("POST", "https://example.invalid")
            )
        }
    )

    with pytest.raises(LlmProviderError):
        merge_baseline_graph(
            "REG-BI-010",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            similarity_threshold=_THRESHOLD,
            call_embedding=call_embedding,
            emitter=emitter,
        )

    for marker in ("PracticeArea", "RiskPath", "COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY"):
        assert single_tenant.calls_matching(marker) == []


def test_succeeded_log_entry_carries_classification_write_counts(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-011: the `outcome="succeeded"` `merge_baseline_graph` log entry
    carries the six PracticeArea/RiskPath/classification-edge write counts
    via `extra=`, computed from a baseline fixture with a KNOWN, DIFFERENT
    count of each (2 PracticeArea, 1 RiskPath, 3 COVERS, 1 OWNS, 1
    MITIGATED_BY, 2 VERIFIED_BY) -- so a bug that swaps two counts cannot
    pass by coincidence.
    """
    emitter, log_path = make_emitter()
    pa_1, pa_2 = "pa_secure_sdlc_4a7c1d", "pa_secure_build_release_d93f8a"
    rp_1 = "rp_secure_build_release_d93f8a"
    cap_1, cap_2, cap_3 = "cap_one_abc", "cap_two_def", "cap_three_ghi"
    pol_1 = "pol_engineering_practices_xyz"
    ctrl_1, ctrl_2 = "ctrl_one_abc", "ctrl_two_def"

    baseline = _baseline_graph_with_classification_nodes(
        practice_area_rows=[
            [pa_1, "Secure SDLC", "active", None, None, None],
            [pa_2, "Secure Build & Release", "active", None, None, None],
        ],
        risk_path_rows=[[rp_1, "Ransomware Exposure", "active", None, None, None]],
        extra_capability_rows=[
            [cap_1, "Encrypt Data At Rest Capability", 0.8, None],
            [cap_2, "Rotate Encryption Keys Capability", 0.8, None],
            [cap_3, "Patch Management Capability", 0.8, None],
        ],
        policy_rows=[[pol_1, "Engineering Practices Policy", "draft", 0.9]],
        control_rows=[
            [ctrl_1, "manual", "Peer Review Control", "planned", 0.8, None],
            [ctrl_2, "automated", "Static Analysis Control", "planned", 0.8, None],
        ],
        covers_rows=[[pa_1, cap_1], [pa_1, cap_2], [pa_2, cap_3]],
        owns_rows=[[pa_1, pol_1]],
        mitigated_by_rows=[[rp_1, cap_1]],
        verified_by_rows=[[rp_1, ctrl_1], [rp_1, ctrl_2]],
    )
    single_tenant = _FakeSingleTenantGraph()
    # Four Capabilities land in this one baseline (the fixture's own spine
    # Capability plus cap_1/cap_2/cap_3): the single-tenant graph starts
    # empty, so the FIRST one minted needs no embedding call, but every
    # SUBSEQUENT one is scored against the growing same-run working index
    # (issue #30 -- a same-run mint is never an eligible merge target, but
    # scoring against it still requires an embedding call for both sides).
    # All four names are scripted so none falls through to a real,
    # unconfigured LLM provider.
    call_embedding = _ScriptedCallEmbedding(
        {
            "Incident Reporting Capability": [1.0, 0.0, 0.0, 0.0],
            "Encrypt Data At Rest Capability": [0.0, 1.0, 0.0, 0.0],
            "Rotate Encryption Keys Capability": [0.0, 0.0, 1.0, 0.0],
            "Patch Management Capability": [0.0, 0.0, 0.0, 1.0],
        }
    )

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        call_embedding=call_embedding,
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
    entry = succeeded[0]
    # `extra=` fields flatten onto the top-level JSON payload (`LogEntry.
    # to_json_line`), not nested under an "extra" key.
    assert entry["practice_area_count"] == 2
    assert entry["risk_path_count"] == 1
    assert entry["covers_count"] == 3
    assert entry["owns_count"] == 1
    assert entry["mitigated_by_count"] == 1
    assert entry["verified_by_count"] == 2


def _classification_only_baseline_graph(
    *,
    regulatory_instrument_id: str,
    capability_rows: list[object],
    practice_area_rows: list[object],
    covers_rows: list[object],
) -> _FakeBaselineGraph:
    """A minimal fixture carrying only Capability + PracticeArea/COVERS
    content -- no Role/Requirement/Obligation spine needed, mirroring
    `_internal_baseline_with_governance`'s own "governance-layer-only"
    shape. Used by the AC-BI-006 tests above, which only care about
    PracticeArea/Capability convergence.
    """
    return _FakeBaselineGraph(
        regulatory_instrument_properties={
            "id": regulatory_instrument_id,
            "title": f"Test Regulation {regulatory_instrument_id}",
        },
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=capability_rows,
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        practice_area_rows=practice_area_rows,
        covers_rows=covers_rows,
    )
