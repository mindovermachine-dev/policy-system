"""Near misses found while restoring become `PendingReview` nodes and are reported.

The vertical thread from graph to response: the real `restore_instrument` runs against the
gateway rig (`restore._gateway_fakes.RestoreRig`) through the real `run_restoration` and its audited
terminal row. The graph is seeded so that restored Capabilities land just below the similarity
threshold of Capabilities the graph already holds.
"""

from __future__ import annotations

import base64
import dataclasses
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._gateway_fakes import POLICY_SYSTEM
from graph_gateway._fakes import close_all_rigs
from restore._fixtures import build_restore_artifact, small_graphs
from restore._gateway_fakes import RestoreRig

from api._audit_fakes import InMemoryAuditStore
from ps_service.api.models import RestorationRequest
from ps_service.api.restore_orchestration import RestoreDependencies, run_restoration
from ps_service.audit import AuditContext
from ps_service.config import ServiceConfig
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator

    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.api.graph_write_access import GraphWriteAccess
    from ps_service.restore.models import RestoreArtifact, RestoreOutcome

_ACTOR = ("actor-sub", "https://issuer.example.com/")
_INSTRUMENT_ID = "NM339-1.0"
_THRESHOLD = 0.85

# Two Capabilities the graph already holds, and two incoming ones scoring 0.8 against one each.
_EXISTING = {
    "cap_ex_a": ([1.0, 0.0, 0.0, 0.0], "Keep Records"),
    "cap_ex_b": ([0.0, 1.0, 0.0, 0.0], "Patch Fast"),
}
_INCOMING = {
    "cap_in_a": ([0.8, 0.6, 0.0, 0.0], "Retain Records", "cap_ex_a"),
    "cap_in_b": ([0.0, 0.8, 0.6, 0.0], "Patch Quickly", "cap_ex_b"),
}


@pytest.fixture
def rig() -> Iterator[RestoreRig]:
    configure()
    rig = RestoreRig()
    yield rig
    close_all_rigs()


def _artifact() -> RestoreArtifact:
    native, baseline = small_graphs(instrument_id=_INSTRUMENT_ID)
    capabilities = tuple(
        SerializedNode(
            "Capability",
            {"id": cap_id, "name": name, "confidence": 0.8, "embedding": embedding},
        )
        for cap_id, (embedding, name, _) in _INCOMING.items()
    )
    requires = tuple(
        SerializedEdge("REQUIRES", "Obligation", "obl_1", "Capability", cap_id, {})
        for cap_id in _INCOMING
    )
    graph = SerializedGraph(
        nodes=(*baseline.nodes, *capabilities), edges=(*baseline.edges, *requires)
    )
    return build_restore_artifact(
        instrument_id=_INSTRUMENT_ID,
        short_name="NM339",
        native_graph=native,
        baseline_graph=graph,
    )


def _seed_existing(rig: RestoreRig) -> None:
    for cap_id, (embedding, name) in _EXISTING.items():
        rig.policy_system.nodes[("Capability", cap_id)] = {
            "id": cap_id,
            "name": name,
            "embedding": embedding,
            "status": "active",
        }


def _request(artifact: RestoreArtifact) -> RestorationRequest:
    return RestorationRequest.model_validate(
        {
            "instrument_id": _INSTRUMENT_ID,
            "manifest": dataclasses.asdict(artifact.manifest),
            "baseline_blob_base64": base64.b64encode(artifact.baseline_blob).decode("ascii"),
            "native_blob_base64": base64.b64encode(artifact.native_blob).decode("ascii"),
        }
    )


def _run(rig: RestoreRig, store: InMemoryAuditStore):
    def _restore(artifact: RestoreArtifact, **kwargs: object) -> RestoreOutcome:
        return rig.restore(artifact, threshold=_THRESHOLD, owner=_ACTOR)

    dependencies = RestoreDependencies(
        open_db=lambda _config: cast("FalkorDB", rig.db),
        single_tenant_graph_name=lambda _config: POLICY_SYSTEM,
        restore=_restore,
        graph_write_access=lambda _config: cast("GraphWriteAccess", None),
    )
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        company_merge_similarity_threshold=_THRESHOLD,
        is_local_test_bypass_active=True,
    )
    return run_restoration(
        _request(_artifact()),
        config=config,
        actor="127.0.0.1",
        dependencies=dependencies,
        audit=AuditContext(_ACTOR, store),
    )


def test_AC_BI_001_AC_BI_003_near_misses_are_persisted_and_counted_end_to_end(
    rig: RestoreRig,
) -> None:
    _seed_existing(rig)
    store = InMemoryAuditStore()

    response = _run(rig, store)

    # The restore is applied by swap, so read the graph the rig holds now, not the seeded object.
    reviews = {
        key[1]: props
        for key, props in rig.graph(POLICY_SYSTEM).nodes.items()
        if key[0] == "PendingReview"
    }
    assert len(reviews) == len(_INCOMING)
    assert {(r["incoming_id"], r["nearest_existing_id"], r["kind"]) for r in reviews.values()} == {
        (incoming, nearest, "Capability") for incoming, (_, _, nearest) in _INCOMING.items()
    }
    assert response.near_miss_reviews_created == len(_INCOMING)
    assert response.model_dump()["near_miss_reviews_created"] == len(_INCOMING)
    terminal = [r for r in store.rows if r.details["status"] == "succeeded"]
    assert len(terminal) == 1
    assert terminal[0].details["near_miss_reviews_created"] == len(_INCOMING)
