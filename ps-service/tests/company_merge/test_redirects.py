"""Company Merge follows `Merged<Kind>` markers through one resolver.

A cleanup merge deletes the absorbed node and leaves a `Merged<Kind> {id, merged_into}` marker.
A later merge whose baseline regenerates the absorbed id drops the node before any persist and
rewires its edges onto the terminal survivor, so the duplicate never returns. These tests drive
`resolve_redirects` directly and through the real `merge_baseline_graph`.

Fakes are hand-written and satisfy `GraphHandle` structurally (`_marker_graph.MarkerGraph`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from company_merge._gateway_fakes import RigReadHandle
from company_merge._marker_graph import MarkerGraph
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import (
    BaselineGraph,
    BaselineNode,
    CanonicalResolution,
    ProvenanceEdge,
)
from ps_service.company_merge.redirects import (
    compose_with_resolutions,
    follow_markers,
    read_markers,
    resolve_redirects,
)
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.logging import configure

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import MergeRig
    from ps_service.company_merge.models import MergeResult
    from ps_service.company_merge.redirects import MergeableKind

_THRESHOLD = 0.85
_ROLE = "role_h_operator_000001"
_OBLIGATION_ABSORBED = obligation_id(_ROLE, "Report incidents promptly.")
_OBLIGATION_SURVIVOR = obligation_id(_ROLE, "Report incidents")
_OBLIGATION_TERMINAL = obligation_id(_ROLE, "Report incidents at once")
_OTHER_OBLIGATION = obligation_id(_ROLE, "Keep records.")
_ROLE_ABSORBED = "role_operator_aaaaaa"
_ROLE_SURVIVOR = "role_operator_bbbbbb"
_ROLE_TERMINAL = "role_operator_cccccc"
_OTHER_ROLE = "role_auditor_dddddd"

_CAPABILITY_ABSORBED = capability_id("Incident Reporting")
_CAPABILITY_SURVIVOR = capability_id("Incident Notification")
_CAPABILITY_TERMINAL = capability_id("Incident Notification Service")
_OTHER_CAPABILITY = capability_id("Record Keeping")

_KINDS: tuple[MergeableKind, ...] = ("Obligation", "Role", "Capability")
_ABSORBED = {
    "Obligation": _OBLIGATION_ABSORBED,
    "Role": _ROLE_ABSORBED,
    "Capability": _CAPABILITY_ABSORBED,
}
_SURVIVOR = {
    "Obligation": _OBLIGATION_SURVIVOR,
    "Role": _ROLE_SURVIVOR,
    "Capability": _CAPABILITY_SURVIVOR,
}
_TERMINAL = {
    "Obligation": _OBLIGATION_TERMINAL,
    "Role": _ROLE_TERMINAL,
    "Capability": _CAPABILITY_TERMINAL,
}
_UNRELATED = {
    "Obligation": _OTHER_OBLIGATION,
    "Role": _OTHER_ROLE,
    "Capability": _OTHER_CAPABILITY,
}


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


class _Result:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


class _RegulatoryInstrumentNode:
    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _Baseline:
    """One Role with two Obligations, one Requirement and one Capability, as the mapper wrote them.

    `role_id` and `absorbed_obligation` are the ids a test makes the markers name.
    """

    def __init__(
        self,
        cap_id: str,
        *,
        role_id: str = _ROLE,
        absorbed_obligation: str = _OBLIGATION_ABSORBED,
        extra_capabilities: tuple[list[object], ...] = (),
    ) -> None:
        self._extra_capabilities = extra_capabilities
        self._cap_id = cap_id
        self._role = role_id
        self._obligation = absorbed_obligation

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        if "[:HAS]" in q:
            return _Result([[self._role, self._obligation], [self._role, _OTHER_OBLIGATION]])
        if "[:SATISFIED_BY]" in q:
            return _Result([["REG-H_req_art_1.1", self._obligation]])
        if "[:REQUIRES]" in q:
            return _Result([[self._obligation, self._cap_id]])
        if any(
            f"(n:{label})" in q
            for label in ("Policy", "Standard", "Control", "PracticeArea", "RiskPath")
        ):
            return _Result([])
        if "n.description" in q:
            return _Result(
                [[self._cap_id, "Incident Reporting", 0.8, None, None], *self._extra_capabilities]
            )
        if "(n:Obligation) RETURN" in q:
            return _Result(
                [
                    [self._obligation, "Report incidents promptly.", 0.9],
                    [_OTHER_OBLIGATION, "Keep records.", 0.9],
                ]
            )
        if "n.role_id" in q:
            return _Result([["REG-H_req_art_1.1", "Must report.", "requirement", 0.9, self._role]])
        if "n.name, n.confidence" in q:
            return _Result([[self._role, "Operator", 0.9, None]])
        if "[e:DEFINES]" in q:
            return _Result([[self._role, "Article 1(1)"]])
        if "[e:EXPRESSES]" in q:
            return _Result([["REG-H_req_art_1.1", "Article 1(1)"]])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _Result([[_RegulatoryInstrumentNode({"id": "REG-H", "title": "Test"})]])
        return _Result([])


def _node(node_id: str, text: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"text": text, "confidence": 0.9})


def _baseline_of(kind: MergeableKind, *ids: str, defines: tuple[str, ...] = ()) -> BaselineGraph:
    """A baseline holding one node per id of `kind` (and a `DEFINES` edge per `defines` id)."""
    nodes = tuple(_node(node_id, "x") for node_id in ids)
    return BaselineGraph(
        regulatory_instrument_id="REG-H",
        regulatory_instrument_properties={"id": "REG-H"},
        role_nodes=nodes if kind == "Role" else (),
        requirement_nodes=(),
        obligation_nodes=nodes if kind == "Obligation" else (),
        capability_nodes=nodes if kind == "Capability" else (),
        provenance_edges=tuple(ProvenanceEdge("DEFINES", target, "Art. 1") for target in defines),
        bare_edges=(),
    )


def _kept(baseline: BaselineGraph, kind: MergeableKind) -> list[str]:
    nodes = {
        "Role": baseline.role_nodes,
        "Obligation": baseline.obligation_nodes,
        "Capability": baseline.capability_nodes,
    }[kind]
    return [node.id for node in nodes]


def _markers(kind: str, markers: dict[str, str | None]) -> dict[str, dict[str, str | None]]:
    return {kind: markers}


@pytest.mark.parametrize("kind", _KINDS)
def test_read_markers_is_one_literal_read_returning_the_marker_map(kind: MergeableKind) -> None:
    graph = MarkerGraph(markers=_markers(kind, {"a": "s", "b": "s"}))

    found = read_markers(graph, kind)

    assert found == {"a": "s", "b": "s"}
    assert [q for q, _ in graph.calls] == [f"MATCH (m:Merged{kind}) RETURN m.id, m.merged_into"]


@pytest.mark.parametrize("kind", _KINDS)
def test_read_markers_rejects_a_marker_without_a_target(kind: MergeableKind) -> None:
    graph = MarkerGraph(markers=_markers(kind, {"a": None}))

    with pytest.raises(CompanyMergeValidationError) as excinfo:
        read_markers(graph, kind)

    assert f"Merged{kind}" in str(excinfo.value)


@pytest.mark.parametrize("kind", _KINDS)
def test_follow_markers_returns_an_id_with_no_marker_unchanged(kind: MergeableKind) -> None:
    assert follow_markers("x_1", {}, kind) == "x_1"


@pytest.mark.parametrize("kind", _KINDS)
def test_chain_resolves_to_terminal(kind: MergeableKind) -> None:
    graph = MarkerGraph(
        markers=_markers(
            kind, {_ABSORBED[kind]: _SURVIVOR[kind], _SURVIVOR[kind]: _TERMINAL[kind]}
        ),
        existing={kind: frozenset({_TERMINAL[kind]})},
    )

    resolution = resolve_redirects(graph, _baseline_of(kind, _ABSORBED[kind]))

    assert resolution.mapping == {_ABSORBED[kind]: _TERMINAL[kind]}


@pytest.mark.parametrize("kind", _KINDS)
def test_cycle_raises_before_any_write(kind: MergeableKind) -> None:
    graph = MarkerGraph(
        markers=_markers(kind, {_ABSORBED[kind]: _SURVIVOR[kind], _SURVIVOR[kind]: _ABSORBED[kind]})
    )

    with pytest.raises(CompanyMergeValidationError) as excinfo:
        resolve_redirects(graph, _baseline_of(kind, _ABSORBED[kind]))

    assert "cycle" in str(excinfo.value)
    assert all("MERGE" not in q for q, _ in graph.calls)


@pytest.mark.parametrize("kind", _KINDS)
def test_dangling_terminal_raises_before_any_write(kind: MergeableKind) -> None:
    graph = MarkerGraph(markers=_markers(kind, {_ABSORBED[kind]: _SURVIVOR[kind]}))

    with pytest.raises(CompanyMergeValidationError) as excinfo:
        resolve_redirects(graph, _baseline_of(kind, _ABSORBED[kind]))

    assert _SURVIVOR[kind] in str(excinfo.value)
    assert all("MERGE" not in q for q, _ in graph.calls)


@pytest.mark.parametrize("kind", _KINDS)
def test_terminal_in_the_baseline_is_accepted(kind: MergeableKind) -> None:
    graph = MarkerGraph(markers=_markers(kind, {_ABSORBED[kind]: _SURVIVOR[kind]}))

    resolution = resolve_redirects(graph, _baseline_of(kind, _ABSORBED[kind], _SURVIVOR[kind]))

    assert _kept(resolution.baseline, kind) == [_SURVIVOR[kind]]
    assert resolution.mapping == {_ABSORBED[kind]: _SURVIVOR[kind]}


@pytest.mark.parametrize("kind", _KINDS)
def test_resolution_drops_absorbed_node_and_maps_it_to_terminal(kind: MergeableKind) -> None:
    graph = MarkerGraph(
        markers=_markers(kind, {_ABSORBED[kind]: _SURVIVOR[kind]}),
        existing={kind: frozenset({_SURVIVOR[kind]})},
    )

    resolution = resolve_redirects(graph, _baseline_of(kind, _ABSORBED[kind], _UNRELATED[kind]))

    assert _kept(resolution.baseline, kind) == [_UNRELATED[kind]]
    assert resolution.mapping == {_ABSORBED[kind]: _SURVIVOR[kind]}
    [(_, params)] = [c for c in graph.calls if "n.id IN $ids" in c[0] and "DEFINES" not in c[0]]
    assert params == {"ids": [_SURVIVOR[kind]]}


@pytest.mark.parametrize("kind", _KINDS)
def test_baseline_without_markers_is_returned_unchanged_after_one_read_per_present_kind(
    kind: MergeableKind,
) -> None:
    graph = MarkerGraph(markers=_markers(kind, {"unrelated": "elsewhere"}))
    baseline = _baseline_of(kind, _UNRELATED[kind])

    resolution = resolve_redirects(graph, baseline)

    assert resolution.baseline is baseline
    assert resolution.mapping == {}
    assert [q for q, _ in graph.calls] == [f"MATCH (m:Merged{kind}) RETURN m.id, m.merged_into"]


@pytest.mark.parametrize("kind", _KINDS)
def test_a_kind_the_baseline_does_not_hold_issues_no_read(kind: MergeableKind) -> None:
    graph = MarkerGraph(markers=_markers(kind, {_ABSORBED[kind]: _SURVIVOR[kind]}))

    resolution = resolve_redirects(graph, _baseline_of(kind))

    assert resolution.mapping == {}
    assert graph.calls == []


def _defines_of(baseline: BaselineGraph) -> list[tuple[str, str]]:
    return [(e.target_id, e.source_ref) for e in baseline.provenance_edges]


def _baseline_with_defines(*edges: tuple[str, str]) -> BaselineGraph:
    """A baseline holding the Roles named by `edges` and a `DEFINES {source_ref}` per edge."""
    base = _baseline_of("Role", *(target for target, _ in edges))
    return replace(
        base,
        provenance_edges=tuple(ProvenanceEdge("DEFINES", t, ref) for t, ref in edges),
    )


def test_a_role_redirect_retargets_the_defines_edge_when_the_survivor_has_none() -> None:
    """The survivor is live only and holds no `DEFINES` from the instrument yet."""
    graph = MarkerGraph(
        markers=_markers("Role", {_ROLE_ABSORBED: _ROLE_SURVIVOR}),
        existing={"Role": frozenset({_ROLE_SURVIVOR})},
    )
    baseline = _baseline_with_defines((_ROLE_ABSORBED, "Art. 2"), (_OTHER_ROLE, "Art. 3"))

    resolution = resolve_redirects(graph, baseline)

    assert sorted(_defines_of(resolution.baseline)) == [
        (_OTHER_ROLE, "Art. 3"),
        (_ROLE_SURVIVOR, "Art. 2"),
    ]


def test_a_role_redirect_keeps_the_survivors_own_defines_source_ref() -> None:
    """Both Roles are in the baseline: the survivor's edge wins, the absorbed one is dropped."""
    graph = MarkerGraph(markers=_markers("Role", {_ROLE_ABSORBED: _ROLE_SURVIVOR}))
    baseline = _baseline_with_defines((_ROLE_SURVIVOR, "Art. 1"), (_ROLE_ABSORBED, "Art. 2"))

    resolution = resolve_redirects(graph, baseline)

    assert _defines_of(resolution.baseline) == [(_ROLE_SURVIVOR, "Art. 1")]


def test_a_role_redirect_drops_the_defines_edge_when_the_live_graph_already_holds_it() -> None:
    graph = MarkerGraph(
        markers=_markers("Role", {_ROLE_ABSORBED: _ROLE_SURVIVOR}),
        existing={"Role": frozenset({_ROLE_SURVIVOR})},
        defines={"REG-H": frozenset({_ROLE_SURVIVOR})},
    )
    baseline = _baseline_with_defines((_ROLE_ABSORBED, "Art. 2"), (_OTHER_ROLE, "Art. 3"))

    resolution = resolve_redirects(graph, baseline)

    assert _defines_of(resolution.baseline) == [(_OTHER_ROLE, "Art. 3")]


def test_a_live_define_of_another_instrument_does_not_drop_the_retargeted_edge() -> None:
    graph = MarkerGraph(
        markers=_markers("Role", {_ROLE_ABSORBED: _ROLE_SURVIVOR}),
        existing={"Role": frozenset({_ROLE_SURVIVOR})},
        defines={"REG-OTHER": frozenset({_ROLE_SURVIVOR})},
    )

    resolution = resolve_redirects(graph, _baseline_with_defines((_ROLE_ABSORBED, "Art. 2")))

    assert _defines_of(resolution.baseline) == [(_ROLE_SURVIVOR, "Art. 2")]


def test_two_absorbed_roles_with_one_survivor_leave_one_defines_edge() -> None:
    graph = MarkerGraph(
        markers=_markers("Role", {_ROLE_ABSORBED: _ROLE_SURVIVOR, _ROLE_TERMINAL: _ROLE_SURVIVOR}),
        existing={"Role": frozenset({_ROLE_SURVIVOR})},
    )
    baseline = _baseline_with_defines((_ROLE_ABSORBED, "Art. 2"), (_ROLE_TERMINAL, "Art. 4"))

    resolution = resolve_redirects(graph, baseline)

    assert _defines_of(resolution.baseline) == [(_ROLE_SURVIVOR, "Art. 2")]


def test_the_live_defines_read_is_one_parameterized_read_only_for_a_redirected_role() -> None:
    graph = MarkerGraph(
        markers=_markers("Role", {_ROLE_ABSORBED: _ROLE_SURVIVOR}),
        existing={"Role": frozenset({_ROLE_SURVIVOR})},
    )

    resolve_redirects(graph, _baseline_with_defines((_ROLE_ABSORBED, "Art. 2")))
    reads = [(q, p) for q, p in graph.calls if "[:DEFINES]" in q]

    assert reads == [
        (
            (
                "MATCH (:RegulatoryInstrument {id: $instrument_id})-[:DEFINES]->(n:Role) "
                "WHERE n.id IN $ids RETURN n.id"
            ),
            {"instrument_id": "REG-H", "ids": [_ROLE_SURVIVOR]},
        )
    ]


def test_a_baseline_without_a_redirected_role_issues_no_defines_read() -> None:
    graph = MarkerGraph(markers=_markers("Role", {"unrelated": _ROLE_SURVIVOR}))

    resolve_redirects(graph, _baseline_with_defines((_OTHER_ROLE, "Art. 3")))

    assert not any("[:DEFINES]" in q for q, _ in graph.calls)


def test_an_obligation_redirect_leaves_the_defines_edges_alone() -> None:
    graph = MarkerGraph(
        markers=_markers("Obligation", {_OBLIGATION_ABSORBED: _OBLIGATION_SURVIVOR}),
        existing={"Obligation": frozenset({_OBLIGATION_SURVIVOR})},
    )
    baseline = _baseline_of("Obligation", _OBLIGATION_ABSORBED, defines=(_ROLE,))

    resolution = resolve_redirects(graph, baseline)

    assert resolution.baseline.provenance_edges == baseline.provenance_edges


@pytest.mark.parametrize("kind", _KINDS)
def test_one_log_entry_with_counts_when_something_is_redirected(
    kind: MergeableKind, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    graph = MarkerGraph(
        markers=_markers(kind, {_ABSORBED[kind]: _SURVIVOR[kind]}),
        existing={kind: frozenset({_SURVIVOR[kind]})},
    )

    resolve_redirects(graph, _baseline_of(kind, _ABSORBED[kind], _UNRELATED[kind]), emitter=emitter)
    emitter.flush()

    [entry] = [e for e in read_lines(log_path) if e.get("action") == "resolve_merge_redirects"]
    assert entry["component"] == "company_merge"
    assert entry["entity_id"] == "REG-H"
    assert entry["outcome"] == "succeeded"
    assert entry["redirected_count"] == 1
    assert entry[f"redirected_{kind.lower()}s"] == 1


def test_no_log_entry_is_emitted_when_nothing_is_redirected(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    resolve_redirects(MarkerGraph(), _baseline_of("Obligation", _OTHER_OBLIGATION), emitter=emitter)
    emitter.flush()

    assert [e for e in read_lines(log_path) if e.get("action") == "resolve_merge_redirects"] == []


def _ingest(
    merge_rig: MergeRig,
    single_tenant: MarkerGraph,
    make_emitter: MakeEmitter,
    kind: MergeableKind = "Obligation",
) -> MergeResult:
    emitter, _log_path = make_emitter()
    baseline = _Baseline(
        _CAPABILITY_ABSORBED,
        role_id=_ROLE_ABSORBED if kind == "Role" else _ROLE,
    )
    return merge_baseline_graph(
        "REG-H",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model="fake-embed-model",
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )


def _edge_pairs(merge_rig: MergeRig, relationship: str) -> list[dict[str, str]]:
    return [
        {"source_id": key[2], "target_id": key[4]}
        for key in merge_rig.policy_system.edges
        if key[0] == relationship
    ]


def _edge_endpoints(merge_rig: MergeRig) -> set[str]:
    return {key[i] for key in merge_rig.policy_system.edges for i in (2, 4)}


def _minted(merge_rig: MergeRig, label: str) -> list[object]:
    return [e.identity for g in merge_rig.groups for e in g.entries if e.name == label]


@pytest.mark.parametrize("kind", _KINDS)
def test_ingest_resolves_absorbed_id_to_survivor(
    kind: MergeableKind, make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """A regenerated absorbed node is never minted; its edges land on the survivor."""
    single_tenant = MarkerGraph(
        markers=_markers(kind, {_ABSORBED[kind]: _SURVIVOR[kind]}),
        existing={kind: frozenset({_SURVIVOR[kind]})},
        gateway_graph=merge_rig.policy_system,
    )

    _ingest(merge_rig, single_tenant, make_emitter, kind)

    assert _ABSORBED[kind] not in _minted(merge_rig, kind)
    assert _SURVIVOR[kind] in _edge_endpoints(merge_rig)
    assert not any(_ABSORBED[kind] in (key[2], key[4]) for key in merge_rig.policy_system.edges)
    assert not any(_ABSORBED[kind] in str(e.identity) for e in merge_rig.logged())
    assert _minted(merge_rig, "PendingReview") == []


def test_ingest_lands_every_obligation_edge_on_the_surviving_obligation(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    cap_id = capability_id("Incident Reporting")
    single_tenant = MarkerGraph(
        markers=_markers("Obligation", {_OBLIGATION_ABSORBED: _OBLIGATION_SURVIVOR}),
        existing={"Obligation": frozenset({_OBLIGATION_SURVIVOR})},
        gateway_graph=merge_rig.policy_system,
    )

    result = _ingest(merge_rig, single_tenant, make_emitter)

    assert _minted(merge_rig, "Obligation") == [_OTHER_OBLIGATION]
    assert result.obligation_ids == (_OTHER_OBLIGATION,)
    assert {"source_id": _ROLE, "target_id": _OBLIGATION_SURVIVOR} in _edge_pairs(merge_rig, "HAS")
    assert _edge_pairs(merge_rig, "SATISFIED_BY") == [
        {"source_id": "REG-H_req_art_1.1", "target_id": _OBLIGATION_SURVIVOR}
    ]
    assert _edge_pairs(merge_rig, "REQUIRES") == [
        {"source_id": _OBLIGATION_SURVIVOR, "target_id": cap_id}
    ]


def test_ingest_lands_has_and_defines_on_the_surviving_role(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    single_tenant = MarkerGraph(
        markers=_markers("Role", {_ROLE_ABSORBED: _ROLE_SURVIVOR}),
        existing={"Role": frozenset({_ROLE_SURVIVOR})},
        gateway_graph=merge_rig.policy_system,
    )

    _ingest(merge_rig, single_tenant, make_emitter, "Role")

    assert _minted(merge_rig, "Role") == []
    assert sorted(p["source_id"] for p in _edge_pairs(merge_rig, "HAS")) == [
        _ROLE_SURVIVOR,
        _ROLE_SURVIVOR,
    ]
    assert _edge_pairs(merge_rig, "DEFINES") == [
        {"source_id": "REG-H", "target_id": _ROLE_SURVIVOR}
    ]


@pytest.mark.parametrize("kind", _KINDS)
def test_ingest_follows_a_chain_to_the_terminal(
    kind: MergeableKind, make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    single_tenant = MarkerGraph(
        markers=_markers(
            kind, {_ABSORBED[kind]: _SURVIVOR[kind], _SURVIVOR[kind]: _TERMINAL[kind]}
        ),
        existing={kind: frozenset({_TERMINAL[kind]})},
        gateway_graph=merge_rig.policy_system,
    )

    _ingest(merge_rig, single_tenant, make_emitter, kind)

    assert _TERMINAL[kind] in _edge_endpoints(merge_rig)
    assert not any(_SURVIVOR[kind] in (key[2], key[4]) for key in merge_rig.policy_system.edges)


@pytest.mark.parametrize("kind", _KINDS)
def test_ingest_with_a_dangling_marker_raises_before_any_write(
    kind: MergeableKind, make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    single_tenant = MarkerGraph(
        markers=_markers(kind, {_ABSORBED[kind]: _SURVIVOR[kind]}),
        gateway_graph=merge_rig.policy_system,
    )

    with pytest.raises(CompanyMergeValidationError):
        _ingest(merge_rig, single_tenant, make_emitter, kind)

    assert merge_rig.groups == []


def test_ingest_keeps_the_defines_edge_to_a_live_only_terminal_role(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The terminal Role is not in the baseline; the live graph holds it, so the edge is written."""
    single_tenant = MarkerGraph(
        markers=_markers("Role", {_ROLE_ABSORBED: _ROLE_TERMINAL}),
        existing={"Role": frozenset({_ROLE_TERMINAL})},
        gateway_graph=merge_rig.policy_system,
    )

    _ingest(merge_rig, single_tenant, make_emitter, "Role")

    assert {"source_id": "REG-H", "target_id": _ROLE_TERMINAL} in _edge_pairs(merge_rig, "DEFINES")


def test_ingest_without_markers_mints_every_baseline_obligation(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    result = _ingest(merge_rig, MarkerGraph(), make_emitter)

    assert result.obligation_ids == (_OBLIGATION_ABSORBED, _OTHER_OBLIGATION)
    assert len(_minted(merge_rig, "Obligation")) == 2


def test_ingest_lands_requires_on_the_surviving_capability(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    single_tenant = MarkerGraph(
        markers=_markers("Capability", {_CAPABILITY_ABSORBED: _CAPABILITY_SURVIVOR}),
        existing={"Capability": frozenset({_CAPABILITY_SURVIVOR})},
        gateway_graph=merge_rig.policy_system,
    )

    _ingest(merge_rig, single_tenant, make_emitter, "Capability")

    assert _minted(merge_rig, "Capability") == []
    assert _edge_pairs(merge_rig, "REQUIRES") == [
        {"source_id": _OBLIGATION_ABSORBED, "target_id": _CAPABILITY_SURVIVOR}
    ]


def test_a_redirected_capability_counts_as_matched_in_the_merge_result(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    single_tenant = MarkerGraph(
        markers=_markers("Capability", {_CAPABILITY_ABSORBED: _CAPABILITY_SURVIVOR}),
        existing={"Capability": frozenset({_CAPABILITY_SURVIVOR})},
        gateway_graph=merge_rig.policy_system,
    )

    result = _ingest(merge_rig, single_tenant, make_emitter, "Capability")

    assert result.capability_canonical_ids == (_CAPABILITY_SURVIVOR,)
    assert result.matched_capability_count == 1
    assert result.new_capability_count == 0
    assert result.obligation_ids == (_OBLIGATION_ABSORBED, _OTHER_OBLIGATION)


def test_compose_maps_a_terminal_that_dedup_resolved_elsewhere() -> None:
    resolutions = [
        CanonicalResolution("cap_terminal", "cap_existing", "semantic", None),
        CanonicalResolution("cap_other", "cap_other", "new", None),
    ]

    composed = compose_with_resolutions(
        {"cap_absorbed": "cap_terminal", "role_a": "role_b"}, resolutions
    )

    assert composed == {"cap_absorbed": "cap_existing", "role_a": "role_b"}


class _ConstantEmbedding:
    """An `EmbeddingCaller` giving every text the same vector, so any two texts converge."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del timeout
        self.calls.append(inputs[0])
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=[1.0, 0.0], index=0, object="embedding")]
        )


def _merge_over_rig(
    merge_rig: MergeRig, baseline: _Baseline, caller: _ConstantEmbedding, make_emitter: MakeEmitter
) -> MergeResult:
    emitter, _log_path = make_emitter()
    return merge_baseline_graph(
        "REG-H",
        baseline_graph=baseline,
        single_tenant_graph=RigReadHandle(merge_rig.policy_system),
        embed_model="fake-embed-model",
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=caller,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )


def test_a_redirected_terminal_that_dedup_maps_elsewhere_lands_on_the_dedup_canonical_id(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The survivor is in the baseline and converges onto a Capability the graph already holds."""
    existing = capability_id("Incident Notification Gateway")
    graph = merge_rig.policy_system
    graph.seed_node(("Capability",), existing, {"name": "Notification", "embedding": [1.0, 0.0]})
    graph.seed_node(
        ("MergedCapability",), _CAPABILITY_ABSORBED, {"merged_into": _CAPABILITY_SURVIVOR}
    )
    baseline = _Baseline(
        _CAPABILITY_ABSORBED,
        extra_capabilities=([_CAPABILITY_SURVIVOR, "Incident Notification", 0.8, None, None],),
    )

    result = _merge_over_rig(merge_rig, baseline, _ConstantEmbedding(), make_emitter)

    assert _minted(merge_rig, "Capability") == []
    assert _edge_pairs(merge_rig, "REQUIRES") == [
        {"source_id": _OBLIGATION_ABSORBED, "target_id": existing}
    ]
    assert result.capability_canonical_ids == (existing, existing)


def test_a_dangling_marker_raises_before_any_embedding_call(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The index is not empty, so the unredirected Capability would be embedded if dedup ran."""
    graph = merge_rig.policy_system
    graph.seed_node(("Capability",), _OTHER_CAPABILITY, {"name": "Record", "embedding": [1.0, 0.0]})
    graph.seed_node(
        ("MergedObligation",), _OBLIGATION_ABSORBED, {"merged_into": _OBLIGATION_SURVIVOR}
    )
    caller = _ConstantEmbedding()

    with pytest.raises(CompanyMergeValidationError):
        _merge_over_rig(merge_rig, _Baseline(_CAPABILITY_ABSORBED), caller, make_emitter)

    assert caller.calls == []
    assert merge_rig.groups == []


def test_an_absorbed_capability_is_not_a_semantic_candidate(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """The absorbed node is gone, so only the survivor can be what a fresh Capability matches."""
    graph = merge_rig.policy_system
    graph.seed_node(
        ("Capability",), _CAPABILITY_SURVIVOR, {"name": "Notification", "embedding": [1.0, 0.0]}
    )
    graph.seed_node(
        ("MergedCapability",), _CAPABILITY_ABSORBED, {"merged_into": _CAPABILITY_SURVIVOR}
    )

    result = _merge_over_rig(
        merge_rig, _Baseline(_OTHER_CAPABILITY), _ConstantEmbedding(), make_emitter
    )

    assert _minted(merge_rig, "Capability") == []
    assert result.capability_canonical_ids == (_CAPABILITY_SURVIVOR,)
    assert _CAPABILITY_ABSORBED not in _edge_endpoints(merge_rig)
