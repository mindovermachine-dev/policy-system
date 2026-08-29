"""Increment 18 (PLAN_REVIEWED.md §9 / §10 Batch 9) -- AC-008 confirmation:
Role/Requirement/Obligation dedup is out of scope for
`ps_service.company_merge`. Two independent proofs, mirroring
`tests/domain_mapper/test_ac008_out_of_scope.py`'s own precedent of pairing
a static-analysis check with direct positive/negative runtime cases. Since
issue #42 Obligation joined Role/Requirement as a passthrough node
(Role-scoped identity, a weak entity of exactly one Role), so
`dedupe_canonical_nodes` is invoked for Capability alone.

1. **Static proof** (secondary layer, PLAN_REVIEWED.md §9): `dedupe_canonical_
   nodes`'s `kind` parameter is genuinely `Literal["Capability"]`-typed,
   confirmed by introspecting the function's own resolved type hints
   (`typing.get_type_hints`) rather than merely reading the source -- this
   is a real, useful guarantee for ordinary statically-checked call sites,
   but only a lint-time property (a dynamically-constructed call, e.g. via
   `**kwargs` unpacking or a `# type: ignore` comment, could still bypass it
   at runtime).

2. **Runtime proof** (primary enforcement, PLAN_REVIEWED.md §9): a
   hand-written wrapper around `ps_service.company_merge.dedup.
   dedupe_canonical_nodes` -- installed via `monkeypatch.setattr` (not
   `unittest.mock.patch`), delegating to the real implementation so
   `merge_baseline_graph`'s own behavior/return value is unaffected --
   records every `kind=` keyword argument it is called with. Asserts
   `merge_baseline_graph`'s only `dedupe_canonical_nodes` invocation is
   `kind="Capability"` -- never `"Role"`/`"Requirement"`/`"Obligation"`.
"""

from __future__ import annotations

import typing
from typing import Literal

import pytest

from ps_service.company_merge import dedup as dedup_module
from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.falkordb_client import GraphHandle
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import BaselineNode, DedupResult
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.llm_interface.client import EmbeddingCaller
from ps_service.logging.emitter import LogEmitter

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85
_REGULATION_ID = "REG-AC008"


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


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s queries with its own
    scripted row set -- one Role, one Requirement, one Obligation, one
    Capability, fully wired -- copied (self-contained, per this test
    package's own convention) from `test_merge_baseline_graph.py`'s
    `_FakeBaselineGraph`."""

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
    """A minimal single-tenant graph fake: empty existing canonical index
    (every incoming Obligation/Capability is a first-time mint), records
    every call it receives. No embedding-backfill/mint state-mutation logic
    is needed for this test -- it only cares about which `kind`s
    `dedupe_canonical_nodes` was invoked for, not cross-call convergence."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "(n:Obligation) RETURN n.id, n.text, n.embedding" in q:
            return _FakeQueryResult([])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([])
        return _FakeQueryResult([])


def _everything_new_baseline_graph() -> _FakeBaselineGraph:
    """One Role, one Requirement, one Obligation, one Capability, fully
    wired -- nothing pre-exists in the single-tenant graph, so both dedup
    passes mint (no `call_embedding` needed: an empty existing index makes
    `find_best_semantic_match` return `None` with zero calls)."""
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-AC008_req_art_1.1"
    obligation_text = "Report the incident to the competent authority."
    obligation_node_id = obligation_id(role_node_id, obligation_text)
    capability_name = "Incident Reporting Capability"
    capability_node_id = capability_id(capability_name)

    return _FakeBaselineGraph(
        regulation_properties={"id": _REGULATION_ID, "title": "Test Regulation"},
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


def test_dedupe_canonical_nodes_kind_parameter_is_literal_capability() -> None:
    """Static proof: `dedupe_canonical_nodes`'s `kind` parameter's resolved
    type hint is exactly `Literal["Capability"]` -- confirmed via
    `typing.get_type_hints`/`typing.get_origin`/`typing.get_args` against the
    live function object, not merely by reading `dedup.py`'s source text.
    Pylance strict mode rejects any other literal at every
    statically-checked call site -- a genuine but lint-time-only guarantee
    (PLAN_REVIEWED.md §9's N2 fix), which is why this test exists alongside,
    never instead of, the runtime proof below."""
    hints = typing.get_type_hints(dedupe_canonical_nodes)
    kind_hint = hints["kind"]

    assert typing.get_origin(kind_hint) is Literal
    assert typing.get_args(kind_hint) == ("Capability",)


def test_merge_baseline_graph_calls_dedup_once_for_capability_only(
    monkeypatch: pytest.MonkeyPatch, make_emitter
) -> None:
    """Runtime proof, the actual enforcement mechanism (PLAN_REVIEWED.md §9):
    a hand-written wrapper -- not `unittest.mock.patch` -- installed over
    `ps_service.company_merge.dedup.dedupe_canonical_nodes` via `monkeypatch.
    setattr` records every `kind=` keyword argument `merge_baseline_graph`
    invokes it with, then delegates to the real implementation (so `merge_
    baseline_graph`'s own return value/behavior is unaffected by the
    wrapper's presence). Asserts the recorded sequence is exactly
    `["Capability"]` -- never `"Role"`, `"Requirement"`, or `"Obligation"`
    (all passthrough since #42), never called twice.

    `merge.py` calls `dedup.dedupe_canonical_nodes(...)` through the `dedup`
    module object (`from ps_service.company_merge import dedup, ...`), so
    patching the attribute on that module object is what a real caller's
    call actually resolves at call time -- no import-time rebinding to work
    around.
    """
    recorded_kinds: list[str] = []
    real_dedupe_canonical_nodes = dedup_module.dedupe_canonical_nodes

    def _recording_wrapper(
        incoming_nodes: tuple[BaselineNode, ...],
        *,
        kind: Literal["Capability"],
        single_tenant_graph: GraphHandle,
        model: str,
        threshold: float,
        call_embedding: EmbeddingCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> DedupResult:
        """Explicit-signature wrapper (no `**kwargs`) matching `dedupe_
        canonical_nodes`'s own parameters exactly, so this stays fully typed
        without a `# type: ignore` escape hatch -- records `kind`, then
        delegates unchanged to the real implementation."""
        recorded_kinds.append(kind)
        return real_dedupe_canonical_nodes(
            incoming_nodes,
            kind=kind,
            single_tenant_graph=single_tenant_graph,
            model=model,
            threshold=threshold,
            call_embedding=call_embedding,
            emitter=emitter,
        )

    monkeypatch.setattr(dedup_module, "dedupe_canonical_nodes", _recording_wrapper)

    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph()

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert recorded_kinds == ["Capability"]
    assert "Role" not in recorded_kinds
    assert "Requirement" not in recorded_kinds
    assert "Obligation" not in recorded_kinds
