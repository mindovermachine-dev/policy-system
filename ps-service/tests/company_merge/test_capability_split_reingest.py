# pyright: reportPrivateUsage=false
"""After a split, every path that regenerates the split Capability resolves it exactly to itself.

The first ingest resolves Capability `X` onto `A` by similarity and leaves a
`ResolvedCapability {id: X, resolves_to: A}` marker. A signed split then mints `X` as its own
Capability and deletes the marker. A later ingest of another regulation, an amended version and a
restore all regenerate `X` (ids are content hashes): each finds `X` among the existing Capabilities,
resolves it `exact`, and so records no marker and no near-miss review for it.

The real Company Merge, split executor and restore run over one `CleanupRig` graph; the tests read
the graph, the graph log the gateway keeps and the dedup decisions the runs log.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest
from graph_cleanup._fakes import FakeApprovalStore
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_gateway._fakes import close_all_rigs
from restore._fixtures import build_restore_artifact
from restore._gateway_fakes import RigDb

from company_merge._gateway_fakes import (
    POLICY_SYSTEM,
    FamilyABaseline,
    RigReadHandle,
    family_a_baseline_graph,
)
from company_merge._resolution_fixtures import (
    AMENDED_VERSION,
    EMBED_MODEL,
    FIRST_VERSION,
    MERGE,
    RESOLVED_ID,
    RESOLVED_NAME,
    RESOLVED_VECTOR,
    TARGET_ID,
    ByNameEmbedding,
    obligation_of,
    seed_target,
    version_baseline,
)
from company_merge._seed_merge_graphs import BaselineGraphReader
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.dedup import DEDUP_DECISION_ACTION
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.domain_mapper.identity import obligation_id, role_id
from ps_service.export.models import (
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
    SerializedPropertyValue,
)
from ps_service.graph_cleanup.executors import execute_capability_split
from ps_service.graph_cleanup.service import create_capability_split_approval
from ps_service.logging import configure
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from falkordb import FalkorDB
    from graph_gateway._fakes import InMemoryGraph

    from company_merge._fakes import MakeEmitter, ReadLines
    from ps_service.restore.models import RestoreArtifact, RestoreOutcome

_MARKER = "ResolvedCapability"
_REVIEW = "PendingReview"
_FIRST_BASELINE = "amend1_baseline"
_INGESTED_BASELINE = "resolution_baseline"
_FRESH_INSTRUMENT = "FRESH-1.0"
_RESTORED_INSTRUMENT = "RESTORED-1.0"
_SPLIT_X_PROPERTIES: dict[str, object] = {
    "name": RESOLVED_NAME,
    "confidence": 0.8,
    "embedding": list(RESOLVED_VECTOR),
}


@pytest.fixture(autouse=True)
def _logging_configured() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    configure()


@pytest.fixture
def rig() -> Iterator[CleanupRig]:
    """A cleanup rig that can also take a restore's swap; its reconciler stops after the test."""
    yield CleanupRig(swap=True)
    close_all_rigs()


def _audit_event(rig: CleanupRig) -> str:
    """A fresh audit event id the rig's gateway accepts as a group's audit link."""
    audit_event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(audit_event_id)
    return audit_event_id


def _ingest(
    rig: CleanupRig, baseline: FamilyABaseline, caller: ByNameEmbedding, make_emitter: MakeEmitter
) -> Path:
    """Run the real Company Merge of `baseline` into the rig's graph; return its log's path."""
    emitter, log_path = make_emitter(filename=f"{baseline.instrument_id}.jsonl")
    merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=RigReadHandle(rig.graph),
        submitter=rig.gateway,
        single_tenant_graph_name=POLICY_SYSTEM,
        baseline_graph_name=_INGESTED_BASELINE,
        audit_event_id=_audit_event(rig),
        embed_model=EMBED_MODEL,
        merge_threshold=MERGE,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=caller,
        emitter=emitter,
    )
    emitter.flush()
    return log_path


def _restored_artifact() -> RestoreArtifact:
    """A curated artifact of another regulation whose one Obligation `REQUIRES` `X`."""
    role = role_id("Operator", _RESTORED_INSTRUMENT)
    text = f"Report incidents under {_RESTORED_INSTRUMENT}."
    obligation = obligation_id(role, text)

    def edge(kind: str, source: tuple[str, str], target: tuple[str, str]) -> SerializedEdge:
        properties: dict[str, SerializedPropertyValue] = (
            {"source_ref": "Art. 1"} if kind == "DEFINES" else {}
        )
        return SerializedEdge(kind, source[0], source[1], target[0], target[1], properties)

    instrument = SerializedNode(
        "RegulatoryInstrument", {"id": _RESTORED_INSTRUMENT, "title": _RESTORED_INSTRUMENT}
    )
    baseline = SerializedGraph(
        nodes=(
            instrument,
            SerializedNode("Role", {"id": role, "name": "Operator", "confidence": 0.9}),
            SerializedNode("Obligation", {"id": obligation, "text": text, "confidence": 0.9}),
            SerializedNode(
                "Capability",
                {
                    "id": RESOLVED_ID,
                    "name": RESOLVED_NAME,
                    "confidence": 0.8,
                    "embedding": list(RESOLVED_VECTOR),
                },
            ),
        ),
        edges=(
            edge("DEFINES", ("RegulatoryInstrument", _RESTORED_INSTRUMENT), ("Role", role)),
            edge("HAS", ("Role", role), ("Obligation", obligation)),
            edge("REQUIRES", ("Obligation", obligation), ("Capability", RESOLVED_ID)),
        ),
    )
    return build_restore_artifact(
        instrument_id=_RESTORED_INSTRUMENT,
        short_name="RESTORED",
        native_graph=SerializedGraph(nodes=(instrument,), edges=()),
        baseline_graph=baseline,
    )


def _restored_obligation() -> str:
    role = role_id("Operator", _RESTORED_INSTRUMENT)
    return obligation_id(role, f"Report incidents under {_RESTORED_INSTRUMENT}.")


def _restore(rig: CleanupRig, make_emitter: MakeEmitter) -> RestoreOutcome:
    """Run the real `restore_instrument` of `_restored_artifact` over the rig's graph."""
    emitter, _ = make_emitter(filename=f"{_RESTORED_INSTRUMENT}.jsonl")
    outcome = restore_instrument(
        _restored_artifact(),
        db=cast("FalkorDB", RigDb(lambda: RigReadHandle(rig.graphs.open(POLICY_SYSTEM)))),
        single_tenant_graph_name=POLICY_SYSTEM,
        merge_threshold=MERGE,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        actor="test-actor",
        open_gateway=lambda: rig.gateway,
        audit_event_id=_audit_event(rig),
        emitter=emitter,
    )
    emitter.flush()
    return outcome


def _resolve_then_split(rig: CleanupRig, caller: ByNameEmbedding, make_emitter: MakeEmitter) -> int:
    """Resolve `X` onto `A` by a real ingest, split it out; return the graph log length after.

    The split reads `X`'s source baseline, so the first version's baseline graph is seeded with
    `X` and the `REQUIRES` edge it holds onto `X`.
    """
    seed_target(rig.graph)
    first = version_baseline(FIRST_VERSION)
    _ingest(rig, first, caller, make_emitter)
    assert rig.graph.nodes[(_MARKER, RESOLVED_ID)] == {"resolves_to": TARGET_ID}
    rig.seed_baseline(
        _FIRST_BASELINE,
        instrument_id=FIRST_VERSION,
        capabilities={RESOLVED_ID: _SPLIT_X_PROPERTIES},
    )
    rig.seed_baseline_requires(_FIRST_BASELINE, ((obligation_of(first), RESOLVED_ID),))

    store = FakeApprovalStore()
    approval = create_capability_split_approval(
        rig.read,
        rig.baseline_graphs(),
        x_id=RESOLVED_ID,
        acknowledge_governance_change=False,
        actor=OFFICER,
        base_url="https://ps.example.com",
        store=store,
    )
    assert store.mark_signed(approval.pending_approval_id)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    outcome = execute_capability_split(row, CONFIG, rig.dependencies(roles()))

    assert outcome == {"x_id": RESOLVED_ID, "a_id": TARGET_ID, "split": True}
    assert (_MARKER, RESOLVED_ID) not in rig.graph.nodes
    assert rig.graph.nodes[("Capability", RESOLVED_ID)]["name"] == RESOLVED_NAME
    return len(rig.store.groups)


def _live(rig: CleanupRig) -> InMemoryGraph:
    """The rig's `policy_system` graph now: a restore swaps in a new graph object."""
    return rig.graphs.open(POLICY_SYSTEM)


def _requires_targets_of(rig: CleanupRig, obligation: str) -> set[str]:
    return {key[4] for key in _live(rig).edges if key[0] == "REQUIRES" and key[2] == obligation}


def _decisions_for_x(log: list[dict[str, object]]) -> list[object]:
    return [
        entry.get("outcome")
        for entry in log
        if entry.get("action") == DEDUP_DECISION_ACTION and entry.get("entity_id") == RESOLVED_ID
    ]


def _written_after(rig: CleanupRig, position: int) -> list[tuple[str, str]]:
    """Every `(name, identity)` the gateway logged on `policy_system` after `position` groups."""
    return [
        (entry.name, entry.identity)
        for group in rig.store.groups[position:]
        if group.graph == POLICY_SYSTEM
        for entry in group.entries
    ]


def _assert_x_resolved_exactly_to_itself(
    rig: CleanupRig,
    *,
    after_split: int,
    x_before: dict[str, object],
    obligation: str,
) -> None:
    """`X` is unchanged, the new `REQUIRES` lands on it, and no marker or review was written.

    Exact is the only resolution with that effect: a semantic one rewires the edge onto `A` and
    writes a marker, and a mint rewrites `X`.
    """
    written = _written_after(rig, after_split)
    assert written, "the regenerating run wrote nothing"
    assert ("Capability", RESOLVED_ID) not in written
    graph = _live(rig)
    assert graph.nodes[("Capability", RESOLVED_ID)] == x_before
    assert (_MARKER, RESOLVED_ID) not in graph.nodes
    assert [label for label, _ in graph.nodes if label in {_MARKER, _REVIEW}] == []
    assert [name for name, _ in written if name in {_MARKER, _REVIEW}] == []
    assert _requires_targets_of(rig, obligation) == {RESOLVED_ID}


def test_an_ingest_of_another_regulation_after_the_split_resolves_x_exactly_to_itself(
    rig: CleanupRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    caller = ByNameEmbedding()
    after_split = _resolve_then_split(rig, caller, make_emitter)
    x_before = dict(rig.graph.nodes[("Capability", RESOLVED_ID)])
    calls_before = len(caller.calls)
    fresh = version_baseline(_FRESH_INSTRUMENT)

    log_path = _ingest(rig, fresh, caller, make_emitter)

    _assert_x_resolved_exactly_to_itself(
        rig,
        after_split=after_split,
        x_before=x_before,
        obligation=obligation_of(fresh),
    )
    assert _decisions_for_x(read_lines(log_path)) == ["exact"]
    assert RESOLVED_NAME not in caller.calls[calls_before:]


def test_an_amended_version_after_the_split_resolves_x_exactly_to_itself(
    rig: CleanupRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    caller = ByNameEmbedding()
    after_split = _resolve_then_split(rig, caller, make_emitter)
    x_before = dict(rig.graph.nodes[("Capability", RESOLVED_ID)])
    amended = version_baseline(AMENDED_VERSION)

    log_path = _ingest(rig, amended, caller, make_emitter)

    _assert_x_resolved_exactly_to_itself(
        rig,
        after_split=after_split,
        x_before=x_before,
        obligation=obligation_of(amended),
    )
    assert _decisions_for_x(read_lines(log_path)) == ["exact"]
    assert _requires_targets_of(rig, obligation_of(version_baseline(FIRST_VERSION))) == {
        RESOLVED_ID
    }


def test_a_restore_after_the_split_resolves_x_exactly_to_itself(
    rig: CleanupRig, make_emitter: MakeEmitter
) -> None:
    after_split = _resolve_then_split(rig, ByNameEmbedding(), make_emitter)
    x_before = dict(rig.graph.nodes[("Capability", RESOLVED_ID)])

    outcome = _restore(rig, make_emitter)

    assert outcome.near_miss_reviews_created == 0
    _assert_x_resolved_exactly_to_itself(
        rig,
        after_split=after_split,
        x_before=x_before,
        obligation=_restored_obligation(),
    )
