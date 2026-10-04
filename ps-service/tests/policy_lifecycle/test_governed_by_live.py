"""Live FalkorDB proof of the `GOVERNED_BY` write paths (issue #185, S10).

The fakes in `test_graph_writer.py`/`test_service.py` prove the statements'
contracts (one query, guard before every write keyword, no row on mismatch)
but cannot detect a Cypher syntax or semantic error. These tests run the REAL
statements against a real FalkorDB at 127.0.0.1:6379 and are therefore the
only proof that: `NOT (cap)-[:GOVERNED_BY]->(:Policy)` is accepted, a failed
`WITH ... WHERE` guard filters every downstream write, `DELETE` inside
`FOREACH` works, and `OPTIONAL MATCH` + `SET` on null after `FOREACH` is a
no-op rather than an error. If a statement is rejected, use the fallback
variants in `.orchestrator/tracker/issue-185/CHANGES.md` (A2/A2b) while
keeping the same one-statement, guard-first invariants.

Run with:
`uv run pytest ps-service/tests/policy_lifecycle/test_governed_by_live.py -m falkordb_live -q`
(deselected by default). Uses a dedicated throwaway graph, deleted before
and after each test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ps_service.ingestion.falkordb_client import FalkorDB, connect, select_graph
from ps_service.policy_lifecycle.graph_writer import (
    ControlDraft,
    StandardDraft,
    approve_fork_repoint,
    create_policy_draft,
    read_capability_governors,
    read_fork_governance,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.ingestion.falkordb_client import GraphHandle

pytestmark = pytest.mark.falkordb_live

_LIVE_TEST_GRAPH_NAME = "policy_lifecycle_governed_by_live_test"
_OWNER = ("live-owner", "https://issuer.example.com/")
_CAPS = ("cap_live_a", "cap_live_b")


def _delete_graph_if_exists(db: FalkorDB, name: str) -> None:
    if name in db.list_graphs():
        db.select_graph(name).delete()


@pytest.fixture
def graph() -> Iterator[GraphHandle]:
    db = connect(host="127.0.0.1", port=6379)
    _delete_graph_if_exists(db, _LIVE_TEST_GRAPH_NAME)
    handle = select_graph(db, _LIVE_TEST_GRAPH_NAME)
    for cap_id in (*_CAPS, "cap_live_c"):
        handle.query("MERGE (:Capability {id: $id})", params={"id": cap_id})
    try:
        yield handle
    finally:
        _delete_graph_if_exists(db, _LIVE_TEST_GRAPH_NAME)


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
    create_policy_draft(graph, policy_id=policy_id, title="Prior", owner=_OWNER)
    graph.query("MATCH (p:Policy {id: $id}) SET p.status = 'approved'", params={"id": policy_id})
    for cap_id in caps:
        graph.query(
            "MATCH (c:Capability {id: $cap}), (p:Policy {id: $id}) MERGE (c)-[:GOVERNED_BY]->(p)",
            params={"cap": cap_id, "id": policy_id},
        )


def _create_proposed_fork(graph: GraphHandle, fork_id: str, prior_id: str) -> None:
    create_policy_draft(
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


def test_fresh_create_with_two_capabilities_writes_one_governed_by_edge_each(
    graph: GraphHandle,
) -> None:
    written = create_policy_draft(
        graph, policy_id="pol_fresh", title="Fresh", owner=_OWNER, capability_ids=_CAPS
    )

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
    graph: GraphHandle,
) -> None:
    _seed_approved_prior(graph, "pol_existing", ("cap_live_b",))

    written = create_policy_draft(
        graph, policy_id="pol_loser", title="Loser", owner=_OWNER, capability_ids=_CAPS
    )

    assert written is False
    assert _rows(graph, "MATCH (p:Policy {id: 'pol_loser'}) RETURN p.id") == []
    assert _governors(graph) == {
        "cap_live_a": [],
        "cap_live_b": ["pol_existing"],
        "cap_live_c": [],
    }


def test_approving_a_fork_moves_both_edges_and_cascades_the_whole_tree(
    graph: GraphHandle,
) -> None:
    _seed_approved_prior(graph, "pol_prior", _CAPS)
    _create_proposed_fork(graph, "pol_fork", "pol_prior")
    fork_governance = read_fork_governance(graph, "pol_fork")
    assert fork_governance is not None
    assert fork_governance.prior_id == "pol_prior"
    assert sorted(fork_governance.capability_ids) == list(_CAPS)

    applied = approve_fork_repoint(
        graph,
        policy_id="pol_fork",
        prior_id=fork_governance.prior_id,
        capability_ids=fork_governance.capability_ids,
        target_status="approved",
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


def test_a_stale_governed_set_makes_the_repoint_write_nothing(graph: GraphHandle) -> None:
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
        policy_id="pol_fork",
        prior_id="pol_prior",
        capability_ids=stale_ids,
        target_status="approved",
    )

    assert applied is False
    assert _governors(graph) == before
    assert _statuses(graph, "pol_fork") == ["proposed"]


def test_repoint_of_a_policy_with_no_standards_still_sets_its_status(
    graph: GraphHandle,
) -> None:
    _seed_approved_prior(graph, "pol_prior", ("cap_live_a",))
    create_policy_draft(
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
        policy_id="pol_bare_fork",
        prior_id="pol_prior",
        capability_ids=("cap_live_a",),
        target_status="approved",
    )

    assert applied is True
    assert _statuses(graph, "pol_bare_fork") == ["approved"]
    assert _governors(graph)["cap_live_a"] == ["pol_bare_fork"]
