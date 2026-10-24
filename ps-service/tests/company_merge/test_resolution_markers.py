"""Company Merge records each semantic Capability resolution as a `ResolvedCapability` marker.

When dedup resolves an incoming Capability `X` onto an existing `A` by similarity, `X` is never
minted. The merge leaves a `ResolvedCapability {id: X, resolves_to: A}` marker in the same gateway
group as the rewired edges, so the marker and the `REQUIRES` edge onto `A` are written together or
not at all. An exact or new resolution leaves no marker. These tests drive the real
`merge_baseline_graph` over the gateway rig and read the graph and the graph log it leaves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from company_merge._gateway_fakes import (
    FamilyABaseline,
    RigSubmitter,
    family_a_baseline_graph,
)
from company_merge._seed_merge_graphs import BaselineGraphReader, SingleTenantStore
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.dedup import read_existing_canonical_index
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import CanonicalResolution
from ps_service.company_merge.resolution_markers import build_resolution_marker_primitives
from ps_service.company_merge.reviewable_kinds import CAPABILITY
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.graph_gateway.errors import MissingTargetError
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import MergeRig
    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

_MERGE_THRESHOLD = 0.85
_INSTRUMENT = "REG-R-1.0"
_ROLE = "role_operator_rrrrrr"
_OBLIGATION = obligation_id(_ROLE, "Report incidents promptly.")
_INCOMING = capability_id("Incident Reporting")
_EXISTING = capability_id("Incident Notification")
# Both unit vectors: the incoming Capability scores 0.96 against `_EXISTING_VECTOR`, above the
# merge threshold, and 0.28 against `_FAR_VECTOR`, below it.
_INCOMING_VECTOR = (0.96, 0.28)
_EXISTING_VECTOR = (1.0, 0.0)
_FAR_VECTOR = (0.0, 1.0)
_MARKER = "ResolvedCapability"


class _FixedEmbedding:
    """An `EmbeddingCaller` giving every text `_INCOMING_VECTOR`."""

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del inputs, timeout
        return EmbeddingResponse(
            model=model,
            data=[Embedding(embedding=list(_INCOMING_VECTOR), index=0, object="embedding")],
        )


class _RefusingRequiresSubmitter(RigSubmitter):
    """The rig's gateway, except a group holding a `REQUIRES` upsert is refused."""

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        if any(isinstance(p, UpsertEdge) and p.type == "REQUIRES" for p in group.primitives):
            raise MissingTargetError("primitive 0 names an endpoint that does not exist")
        return super().submit_group(group)


def _baseline() -> FamilyABaseline:
    return FamilyABaseline(
        instrument_id=_INSTRUMENT,
        instrument_properties={"title": "Regulation R"},
        roles={_ROLE: "Operator"},
        obligations={_OBLIGATION: ("Report incidents promptly.", 0.9)},
        capabilities={_INCOMING: ("Incident Reporting", 0.8)},
        edges=(
            ("HAS", "Role", _ROLE, "Obligation", _OBLIGATION),
            ("REQUIRES", "Obligation", _OBLIGATION, "Capability", _INCOMING),
        ),
    )


def _seed_capability(merge_rig: MergeRig, cap_id: str, vector: tuple[float, float]) -> None:
    merge_rig.policy_system.seed_node(
        ("Capability",), cap_id, {"name": "Incident Notification", "embedding": list(vector)}
    )


def _merge(
    merge_rig: MergeRig, emitter: LogEmitter, submitter: GroupSubmitter | None = None
) -> None:
    keywords = merge_rig.merge_keywords()
    if submitter is not None:
        keywords["submitter"] = submitter
    merge_baseline_graph(
        _INSTRUMENT,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(_baseline())),
        single_tenant_graph=merge_rig.legacy,
        embed_model="fake-embed-model",
        merge_threshold=_MERGE_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=_FixedEmbedding(),
        emitter=emitter,
        **keywords,
    )


def _markers(merge_rig: MergeRig) -> dict[str, dict[str, object]]:
    return {
        node_id: props
        for (label, node_id), props in merge_rig.policy_system.nodes.items()
        if label == _MARKER
    }


def _requires_targets(merge_rig: MergeRig) -> set[str]:
    return {key[4] for key in merge_rig.policy_system.edges if key[0] == "REQUIRES"}


def test_build_resolution_marker_primitives_emits_one_upsert_per_semantic_resolution() -> None:
    resolutions = [
        CanonicalResolution("cap_x", "cap_a", "semantic", None, similarity=0.93),
        CanonicalResolution("cap_y", "cap_y", "exact", None),
        CanonicalResolution("cap_z", "cap_z", "new", (1.0, 0.0)),
        CanonicalResolution("cap_w", "cap_b", "semantic", None, similarity=0.91),
    ]

    primitives = build_resolution_marker_primitives(resolutions)

    assert primitives == (
        UpsertNode(label=_MARKER, id="cap_x", properties={"resolves_to": "cap_a"}),
        UpsertNode(label=_MARKER, id="cap_w", properties={"resolves_to": "cap_b"}),
    )
    for primitive in primitives:
        assert isinstance(primitive, UpsertNode)
        assert primitive.label == _MARKER
        assert primitive.embedding is None


def test_a_semantic_match_writes_one_resolved_capability_naming_the_target(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    _seed_capability(merge_rig, _EXISTING, _EXISTING_VECTOR)

    _merge(merge_rig, make_emitter()[0])

    assert _markers(merge_rig) == {_INCOMING: {"resolves_to": _EXISTING}}
    assert ("Capability", _INCOMING) not in merge_rig.policy_system.nodes
    assert _requires_targets(merge_rig) == {_EXISTING}


def test_the_marker_is_in_the_same_group_as_the_requires_edge_onto_the_target(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    _seed_capability(merge_rig, _EXISTING, _EXISTING_VECTOR)

    _merge(merge_rig, make_emitter()[0])

    holding_marker = [
        group for group in merge_rig.groups if any(e.name == _MARKER for e in group.entries)
    ]
    [group] = holding_marker
    written = {(e.name, e.identity) for e in group.entries}
    assert (_MARKER, _INCOMING) in written
    assert ("REQUIRES", f"REQUIRES:{_OBLIGATION}->{_EXISTING}") in written


def test_a_refused_edge_group_leaves_neither_the_marker_nor_the_edge(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    _seed_capability(merge_rig, _EXISTING, _EXISTING_VECTOR)

    with pytest.raises(CompanyMergePersistenceError):
        _merge(merge_rig, make_emitter()[0], _RefusingRequiresSubmitter(merge_rig))

    assert _markers(merge_rig) == {}
    assert _requires_targets(merge_rig) == set()
    assert not any(e.name == _MARKER for e in merge_rig.logged())


@pytest.mark.parametrize(
    ("seeded_id", "vector"),
    [
        pytest.param(_INCOMING, _EXISTING_VECTOR, id="exact"),
        pytest.param(_EXISTING, _FAR_VECTOR, id="new"),
    ],
)
def test_new_and_exact_resolutions_write_no_marker(
    seeded_id: str, vector: tuple[float, float], merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    _seed_capability(merge_rig, seeded_id, vector)

    _merge(merge_rig, make_emitter()[0])

    assert _markers(merge_rig) == {}
    assert not any(e.name == _MARKER for e in merge_rig.logged())
    assert _requires_targets(merge_rig) == {_INCOMING}


def test_writing_a_marker_logs_its_ids_score_and_instrument(
    merge_rig: MergeRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    _seed_capability(merge_rig, _EXISTING, _EXISTING_VECTOR)
    emitter, log_path = make_emitter()

    _merge(merge_rig, emitter)
    emitter.flush()

    [entry] = [e for e in read_lines(log_path) if e.get("action") == "record_semantic_resolution"]
    assert entry["component"] == "company_merge"
    assert entry["entity_id"] == _INCOMING
    assert entry["outcome"] == "succeeded"
    assert entry["kind"] == "Capability"
    assert entry["resolves_to"] == _EXISTING
    assert entry["similarity"] == pytest.approx(0.96)
    assert entry["regulatory_instrument_id"] == _INSTRUMENT
    assert "Incident" not in str(entry)


def test_no_marker_entry_is_logged_when_the_marker_group_is_refused(
    merge_rig: MergeRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    _seed_capability(merge_rig, _EXISTING, _EXISTING_VECTOR)
    emitter, log_path = make_emitter()

    with pytest.raises(CompanyMergePersistenceError):
        _merge(merge_rig, emitter, _RefusingRequiresSubmitter(merge_rig))
    emitter.flush()

    assert not [e for e in read_lines(log_path) if e.get("action") == "record_semantic_resolution"]


def test_a_resolved_capability_is_never_a_semantic_candidate() -> None:
    """A marker carrying the incoming vector exactly still never enters the candidate index.

    The store answers the index read for the label it parses out of the query text, so only a
    `Capability`-scoped read leaves the marker out.
    """
    store = SingleTenantStore(
        nodes={
            (_MARKER, _INCOMING): {"resolves_to": _EXISTING, "embedding": list(_INCOMING_VECTOR)},
            ("Capability", _EXISTING): {
                "name": "Incident Notification",
                "embedding": list(_EXISTING_VECTOR),
            },
        }
    )

    index = read_existing_canonical_index(store, CAPABILITY)

    assert [node.id for node in index] == [_EXISTING]
    [statement] = store.statements
    assert statement.startswith("MATCH (n:Capability)")
