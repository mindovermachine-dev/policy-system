"""Role/Requirement/Obligation dedup is out of scope for
`ps_service.company_merge`. Two independent proofs, mirroring
`tests/domain_mapper/test_ac008_out_of_scope.py`'s own precedent of pairing
a static-analysis check with direct positive/negative runtime cases. Obligation is a
passthrough node like Role/Requirement
(Role-scoped identity, a weak entity of exactly one Role), so
`dedupe_canonical_nodes` is invoked for Capability alone.

1. **Static proof** (secondary layer): `dedupe_canonical_
   nodes`'s `kind` parameter is genuinely `Literal["Capability"]`-typed,
   confirmed by introspecting the function's own resolved type hints
   (`typing.get_type_hints`) rather than merely reading the source -- this
   is a real, useful guarantee for ordinary statically-checked call sites,
   but only a lint-time property (a dynamically-constructed call, e.g. via
   `**kwargs` unpacking or a type-checker suppression comment, could still
   bypass it at runtime).

2. **Runtime proof** (primary enforcement): a
   hand-written wrapper around `ps_service.company_merge.dedup.
   dedupe_canonical_nodes` -- installed via `monkeypatch.setattr` (not
   `unittest.mock.patch`), delegating to the real implementation so
   `merge_baseline_graph`'s own behavior/return value is unaffected --
   records every `kind=` and `thresholds=` keyword argument it is called with. Asserts
   `merge_baseline_graph`'s only `dedupe_canonical_nodes` invocation is
   `kind="Capability"` -- never `"Role"`/`"Requirement"`/`"Obligation"`.
"""

from __future__ import annotations

import typing
from typing import cast

import pytest

from company_merge._gateway_fakes import (
    MergeRig,  # noqa: TC001 -- pytest resolves the fixture annotation
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge import dedup as dedup_module
from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.reviewable_kinds import ReviewableKind, ReviewThresholds
from ps_service.domain_mapper.identity import capability_id, obligation_id

if typing.TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import BaselineNode, DedupResult
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


class _FakeRegulatoryInstrumentNode:
    """Satisfies `graph_reader._RegulatoryInstrumentNode` structurally -- only
    `.properties` is ever read.
    """

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s queries with its own
    scripted row set -- one Role, one Requirement, one Obligation, one
    Capability, fully wired -- copied (self-contained, per this test
    package's own convention) from `test_merge_baseline_graph.py`'s
    `_FakeBaselineGraph`.
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
        if "(n:Policy) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(self._policy_rows, ("id", "title", "status", "confidence"))
            )
        if "(n:Standard) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(
                    self._standard_rows,
                    ("id", "title", "implementation_status", "confidence", "description"),
                )
            )
        if "(n:Control) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(
                    self._control_rows,
                    ("id", "type", "title", "implementation_status", "confidence", "description"),
                )
            )
        if "(n:PracticeArea) RETURN" in q:
            return _FakeQueryResult([])
        if "(n:RiskPath) RETURN" in q:
            return _FakeQueryResult([])
        if "[:COVERS]" in q or "[:OWNS]" in q or "[:MITIGATED_BY]" in q or "[:VERIFIED_BY]" in q:
            # This fixture carries no classification-layer content; the four
            # classification edge queries `read_baseline_graph` always issues
            # answer empty (external-baseline shape).
            return _FakeQueryResult([])
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

    @staticmethod
    def _node_rows(rows: list[object], keys: tuple[str, ...]) -> list[object]:
        """Governance nodes are read whole (`RETURN n`): one single-node row per scripted row.

        The scripted rows keep their column order; a `None` column is an absent property.
        """
        return [
            [
                _FakeRegulatoryInstrumentNode(
                    {
                        key: value
                        for key, value in zip(keys, cast("list[object]", row), strict=True)
                        if value is not None
                    }
                )
            ]
            for row in rows
        ]


class _FakeSingleTenantGraph:
    """A minimal single-tenant graph fake: empty existing canonical index
    (every incoming Obligation/Capability is a first-time mint), records
    every call it receives. No embedding-backfill/mint state-mutation logic
    is needed for this test -- it only cares about which `kind`s
    `dedupe_canonical_nodes` was invoked for, not cross-call convergence.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "(n:Obligation) RETURN n.id, n.text, n.embedding" in q:
            return _FakeQueryResult([])
        if "MERGED_INTO" in q:
            return _FakeQueryResult([])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([])
        return _FakeQueryResult([])


def _everything_new_baseline_graph() -> _FakeBaselineGraph:
    """One Role, one Requirement, one Obligation, one Capability, fully
    wired -- nothing pre-exists in the single-tenant graph, so both dedup
    passes mint (no `call_embedding` needed: an empty existing index makes
    `find_best_semantic_match` return `None` with zero calls).
    """
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-AC008_req_art_1.1"
    obligation_text = "Report the incident to the competent authority."
    obligation_node_id = obligation_id(role_node_id, obligation_text)
    capability_name = "Incident Reporting Capability"
    capability_node_id = capability_id(capability_name)

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": _REGULATION_ID, "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9, None]],
        requirement_rows=[
            [requirement_node_id, "Must report incidents.", "requirement", 0.9, role_node_id]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[capability_node_id, capability_name, 0.8, None, None]],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, capability_node_id]],
    )


def _internal_baseline_with_governance() -> _FakeBaselineGraph:
    """An internal-sourced baseline -- one Capability, one
    Policy, one Standard, one Control, fully wired with `GOVERNED_BY`/
    `SUPPORTED_BY`/`IMPLEMENTED_BY` -- so `merge_baseline_graph`'s governance
    import actually runs. Mirrors
    `test_merge_baseline_graph.py`'s own `_internal_baseline_with_governance`
    fixture.
    """
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-AC008_req_art_1.1"
    obligation_text = "Report the incident to the competent authority."
    obligation_node_id = obligation_id(role_node_id, obligation_text)
    capability_name = "Incident Reporting Capability"
    capability_node_id = capability_id(capability_name)
    policy_node_id = "pol_incident_response_xyz"
    standard_node_id = "std_pol_incident_response_xyz_v1"
    control_node_id = "ctrl_std_pol_incident_response_xyz_v1_manual"

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": _REGULATION_ID, "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9, None]],
        requirement_rows=[
            [requirement_node_id, "Must report incidents.", "requirement", 0.9, role_node_id]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[capability_node_id, capability_name, 0.8, None, None]],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, capability_node_id]],
        policy_rows=[[policy_node_id, "Incident Response Policy", "draft", 0.9]],
        standard_rows=[[standard_node_id, "Incident Notification Standard", "draft", 0.85, None]],
        control_rows=[
            [control_node_id, "manual", "Incident Notification Control", "planned", 0.8, None]
        ],
        governed_by_rows=[[capability_node_id, policy_node_id]],
        supported_by_rows=[[policy_node_id, standard_node_id]],
        implemented_by_rows=[[standard_node_id, control_node_id]],
    )


def test_dedupe_canonical_nodes_kind_parameter_is_a_reviewable_kind_spec() -> None:
    """Static proof: `dedupe_canonical_nodes`'s `kind` parameter's resolved type hint is
    `ReviewableKind` (the spec), not a closed set of labels -- confirmed via `typing.get_type_hints`
    against the live function object, not merely by reading `dedup.py`'s source text. Which kinds
    exist is the registry's business; Role/Requirement/Obligation/Standard/Control have no spec
    and so cannot be deduped. The runtime proof below shows which spec `merge_baseline_graph`
    passes.
    """
    hints = typing.get_type_hints(dedupe_canonical_nodes)

    assert hints["kind"] is ReviewableKind
    assert hints["thresholds"] is ReviewThresholds


@pytest.mark.parametrize(
    ("baseline_factory", "expected_kinds"),
    [
        pytest.param(_internal_baseline_with_governance, ["Capability", "Role"], id="internal"),
        pytest.param(_everything_new_baseline_graph, ["Capability", "Role"], id="external"),
    ],
)
def test_merge_baseline_graph_calls_dedup_for_capability_and_role_only(
    baseline_factory: typing.Callable[[], _FakeBaselineGraph],
    expected_kinds: list[str],
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """Runtime proof, the actual enforcement mechanism: a hand-written wrapper -- not
    `unittest.mock.patch` -- installed over
    `ps_service.company_merge.dedup.dedupe_canonical_nodes` via `monkeypatch.
    setattr` records every `kind=` and `thresholds=` keyword argument `merge_baseline_graph`
    invokes it with, then delegates to the real implementation (so `merge_
    baseline_graph`'s own return value/behavior is unaffected by the
    wrapper's presence).

    Two cases: an internal-sourced baseline (`graph.policy_nodes`
    non-empty) and an external-sourced one both record `["Capability", "Role"]`
    only -- Capability merges above its threshold, Role is review-only (near
    misses cited, never merged). The governance tree is imported as drafts by
    `governance_import`, never deduped, so no Policy dedup pass exists on the
    live path. Neither case ever records `"Requirement"`, `"Obligation"`,
    `"Standard"`, or `"Control"` (all passthrough).

    `merge.py` calls `dedup.dedupe_canonical_nodes(...)` through the `dedup`
    module object (`from ps_service.company_merge import dedup, ...`), so
    patching the attribute on that module object is what a real caller's
    call actually resolves at call time -- no import-time rebinding to work
    around.
    """
    recorded_kinds: list[str] = []
    recorded_thresholds: list[ReviewThresholds] = []
    real_dedupe_canonical_nodes = dedup_module.dedupe_canonical_nodes

    def _recording_wrapper(
        incoming_nodes: tuple[BaselineNode, ...],
        *,
        kind: ReviewableKind,
        single_tenant_graph: GraphHandle,
        model: str,
        thresholds: ReviewThresholds,
        instrument_id: str | None = None,
        call_embedding: EmbeddingCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> DedupResult:
        """Explicit-signature wrapper (no `**kwargs`) matching `dedupe_
        canonical_nodes`'s own parameters exactly, so this stays fully typed
        without a type-checker suppression -- records `kind`, then
        delegates unchanged to the real implementation.
        """
        recorded_kinds.append(kind.label)
        recorded_thresholds.append(thresholds)
        return real_dedupe_canonical_nodes(
            incoming_nodes,
            kind=kind,
            single_tenant_graph=single_tenant_graph,
            model=model,
            thresholds=thresholds,
            instrument_id=instrument_id,
            call_embedding=call_embedding,
            emitter=emitter,
        )

    # detroit-exception: "Role/Requirement/Obligation dedup is out of scope" is itself
    # phrased as a call-shape claim, not a state/output one -- which `kind=` values
    # `merge_baseline_graph` invokes `dedupe_canonical_nodes` with, across the two parametrized
    # baseline shapes (governance-bearing vs. not), IS the specified behavior here (the
    # "interaction is the specified behavior" carve-out). This exercises a genuine
    # data-conditional branch decision, unlike a fixed, non-conditional invariant, which
    # would be asserted on real state instead.
    monkeypatch.setattr(dedup_module, "dedupe_canonical_nodes", _recording_wrapper)

    emitter, _log_path = make_emitter()
    baseline = baseline_factory()
    single_tenant = _FakeSingleTenantGraph()

    merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        governance=GovernanceImport(owner=("sub-1", "https://issuer.example")),
        **merge_rig.merge_keywords(),
    )

    assert recorded_kinds == expected_kinds
    # Each kind carries the thresholds its own guard built: Capability both, Role review only.
    assert recorded_thresholds == [
        ReviewThresholds(merge=_THRESHOLD, review=REVIEW_THRESHOLD),
        ReviewThresholds(merge=None, review=ROLE_REVIEW_THRESHOLD),
    ]
    assert "Requirement" not in recorded_kinds
    assert "Obligation" not in recorded_kinds
    assert "Standard" not in recorded_kinds
    assert "Control" not in recorded_kinds
