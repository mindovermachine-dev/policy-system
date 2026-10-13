"""Live FalkorDB proof of the `GOVERNED_BY` write paths (issue #185, S10; gateway-backed #211).

The rig tests (`test_claim_create_gateway.py`, `test_fork_repoint_gateway.py`) prove the groups'
contracts over in-memory fakes but cannot detect a difference between the in-memory graph and a
real FalkorDB. These tests run the REAL gateway (in-memory log) against a real FalkorDB graph and
are therefore the only proof that: a claim group lands every `GOVERNED_BY` edge, a refused claim
leaves the real graph untouched, and an approval's `DeleteEdge`/`UpsertEdge` pair moves each
Capability from the prior to the fork with exactly one governor, after the legacy, identity-less
edges were stamped.

Run with:
`uv run pytest ps-service/tests/policy_lifecycle/test_governed_by_live.py -m falkordb_live -q`
(deselected by default). Uses a uniquely named throwaway graph, deleted afterwards.

Issue #211 (S9): the claim create and the fork repoint are gateway groups. The graph is SEEDED
with the frozen legacy writer (`legacy_create_policy_draft`, `_legacy_reference.py`) and direct
statements, as a pre-gateway writer left it (identity-less edges); every write under test goes
through the gateway. Written, not run in the implementation sandbox (no FalkorDB).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_graphs import AUDIT_EVENT_ID, LiveGraph, LiveGraphs

from policy_lifecycle._legacy_reference import legacy_create_policy_draft
from ps_service.graph_gateway.connection import connect
from ps_service.policy_lifecycle.graph_submission import GraphWriteTarget, lifecycle_submitter
from ps_service.policy_lifecycle.graph_writer import (
    ControlDraft,
    StandardDraft,
    approve_fork_repoint,
    create_claimed_policy_draft,
    read_capability_governors,
    read_fork_governance,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle

pytestmark = pytest.mark.falkordb_live

_OWNER = ("live-owner", "https://issuer.example.com/")
_CAPS = ("cap_live_a", "cap_live_b")


@pytest.fixture
def live() -> Iterator[LiveGraph]:
    """A throwaway real graph, with the gateway in front of it and three Capabilities."""
    host, port = falkordb_endpoint()
    graphs = LiveGraphs(connect(host=host, port=port))
    graph = graphs.new()
    graph.store.audit_rows.add(AUDIT_EVENT_ID)
    for cap_id in (*_CAPS, "cap_live_c"):
        graph.handle.query("MERGE (:Capability {id: $id})", params={"id": cap_id})
    try:
        yield graph
    finally:
        graphs.close()


def _target(live: LiveGraph) -> GraphWriteTarget:
    return GraphWriteTarget(lifecycle_submitter(live.gateway), live.name)


def _rows(graph: GraphHandle, query: str, **params: object) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _governors(graph: GraphHandle) -> dict[str, list[str]]:
    """Every Capability id -> the ids of all Policies that govern it."""
    rows = _rows(
        graph,
        "MATCH (c:Capability) OPTIONAL MATCH (c)-[:GOVERNED_BY]->(p:Policy) "
        "RETURN c.id, collect(p.id)",
    )
    return {cast("str", cap): sorted(cast("list[str]", ids)) for cap, ids in rows}


def _statuses(graph: GraphHandle, policy_id: str) -> list[str]:
    """Sorted statuses of the Policy and every Standard/Control under it."""
    rows = _rows(
        graph,
        "MATCH (p:Policy {id: $id}) "
        "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
        "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
        "RETURN p.status, s.status, c.status",
        id=policy_id,
    )
    return sorted({cast("str", v) for row in rows for v in row if v is not None})


def _seed_approved_prior(graph: GraphHandle, policy_id: str, caps: tuple[str, ...]) -> None:
    legacy_create_policy_draft(graph, policy_id=policy_id, title="Prior", owner=_OWNER)
    graph.query("MATCH (p:Policy {id: $id}) SET p.status = 'approved'", params={"id": policy_id})
    for cap_id in caps:
        graph.query(
            "MATCH (c:Capability {id: $cap}), (p:Policy {id: $id}) MERGE (c)-[:GOVERNED_BY]->(p)",
            params={"cap": cap_id, "id": policy_id},
        )


def _create_proposed_fork(graph: GraphHandle, fork_id: str, prior_id: str) -> None:
    legacy_create_policy_draft(
        graph,
        policy_id=fork_id,
        title="Fork",
        owner=_OWNER,
        supersedes_policy_id=prior_id,
        version="2",
        standards=(
            StandardDraft(
                id="std_live",
                title="Std",
                controls=(ControlDraft(id="ctl_live", title="Ctl", control_type="manual"),),
            ),
        ),
    )
    graph.query(
        "MATCH (p:Policy {id: $id})-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->(c:Control) "
        "SET p.status = 'proposed', s.status = 'proposed', c.status = 'proposed'",
        params={"id": fork_id},
    )


def _claim(live: LiveGraph, policy_id: str, title: str) -> bool:
    return create_claimed_policy_draft(
        live.handle,
        _target(live),
        policy_id=policy_id,
        title=title,
        owner=_OWNER,
        capability_ids=_CAPS,
        audit_event_id=AUDIT_EVENT_ID,
    )


def test_fresh_create_with_two_capabilities_writes_one_governed_by_edge_each(
    live: LiveGraph,
) -> None:
    written = _claim(live, "pol_fresh", "Fresh")

    graph = live.handle
    assert written is True
    assert _governors(graph) == {
        "cap_live_a": ["pol_fresh"],
        "cap_live_b": ["pol_fresh"],
        "cap_live_c": [],
    }
    props = _rows(
        graph,
        "MATCH (p:Policy {id: 'pol_fresh'}) RETURN p.title, p.status, p.version, p.owner_subject",
    )
    assert props == [["Fresh", "draft", "1", "live-owner"]]
    assert read_capability_governors(graph, _CAPS) == {
        "cap_live_a": "pol_fresh",
        "cap_live_b": "pol_fresh",
    }


def test_fresh_create_with_an_already_governed_capability_writes_nothing(
    live: LiveGraph,
) -> None:
    _seed_approved_prior(live.handle, "pol_existing", ("cap_live_b",))

    written = _claim(live, "pol_loser", "Loser")

    graph = live.handle
    assert written is False
    assert _rows(graph, "MATCH (p:Policy {id: 'pol_loser'}) RETURN p.id") == []
    assert _governors(graph) == {
        "cap_live_a": [],
        "cap_live_b": ["pol_existing"],
        "cap_live_c": [],
    }


def test_approving_a_fork_moves_both_edges_and_cascades_the_whole_tree(
    live: LiveGraph,
) -> None:
    graph = live.handle
    _seed_approved_prior(graph, "pol_prior", _CAPS)
    _create_proposed_fork(graph, "pol_fork", "pol_prior")
    fork_governance = read_fork_governance(graph, "pol_fork")
    assert fork_governance is not None
    assert fork_governance.prior_id == "pol_prior"
    assert sorted(fork_governance.capability_ids) == list(_CAPS)

    applied = approve_fork_repoint(
        graph,
        _target(live),
        policy_id="pol_fork",
        prior_id=fork_governance.prior_id,
        capability_ids=fork_governance.capability_ids,
        target_status="approved",
        audit_event_id=AUDIT_EVENT_ID,
    )

    assert applied is True
    assert _governors(graph) == {
        "cap_live_a": ["pol_fork"],
        "cap_live_b": ["pol_fork"],
        "cap_live_c": [],
    }
    assert _statuses(graph, "pol_fork") == ["approved"]
    # The prior is left for the separate auto-deprecate cascade.
    assert _statuses(graph, "pol_prior") == ["approved"]


def test_a_stale_governed_set_makes_the_repoint_write_nothing(live: LiveGraph) -> None:
    graph = live.handle
    _seed_approved_prior(graph, "pol_prior", _CAPS)
    _seed_approved_prior(graph, "pol_third", ())
    _create_proposed_fork(graph, "pol_fork", "pol_prior")
    stale_ids = _CAPS
    # Another approval takes cap_live_b away from the prior after the read.
    graph.query(
        "MATCH (:Capability {id: 'cap_live_b'})-[r:GOVERNED_BY]->(:Policy {id: 'pol_prior'}) "
        "DELETE r"
    )
    graph.query(
        "MATCH (c:Capability {id: 'cap_live_b'}), (p:Policy {id: 'pol_third'}) "
        "MERGE (c)-[:GOVERNED_BY]->(p)"
    )
    before = _governors(graph)

    applied = approve_fork_repoint(
        graph,
        _target(live),
        policy_id="pol_fork",
        prior_id="pol_prior",
        capability_ids=stale_ids,
        target_status="approved",
        audit_event_id=AUDIT_EVENT_ID,
    )

    assert applied is False
    assert _governors(graph) == before
    assert _statuses(graph, "pol_fork") == ["proposed"]


def test_repoint_of_a_policy_with_no_standards_still_sets_its_status(
    live: LiveGraph,
) -> None:
    graph = live.handle
    _seed_approved_prior(graph, "pol_prior", ("cap_live_a",))
    legacy_create_policy_draft(
        graph,
        policy_id="pol_bare_fork",
        title="Bare fork",
        owner=_OWNER,
        supersedes_policy_id="pol_prior",
        version="2",
    )
    graph.query("MATCH (p:Policy {id: 'pol_bare_fork'}) SET p.status = 'proposed'")

    applied = approve_fork_repoint(
        graph,
        _target(live),
        policy_id="pol_bare_fork",
        prior_id="pol_prior",
        capability_ids=("cap_live_a",),
        target_status="approved",
        audit_event_id=AUDIT_EVENT_ID,
    )

    assert applied is True
    assert _statuses(graph, "pol_bare_fork") == ["approved"]
    assert _governors(graph)["cap_live_a"] == ["pol_bare_fork"]


def test_fresh_create_naming_a_merged_tombstone_writes_nothing_and_reads_it_as_absent(
    live: LiveGraph,
) -> None:
    graph = live.handle
    graph.query("MATCH (c:Capability {id: 'cap_live_a'}) SET c.status = 'merged'")

    assert read_capability_governors(graph, _CAPS) == {"cap_live_b": None}
    written = _claim(live, "pol_tomb", "Tomb")

    assert written is False
    assert _rows(graph, "MATCH (p:Policy {id: 'pol_tomb'}) RETURN p.id") == []
