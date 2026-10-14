"""`MutationGroup.audit_event_id` is optional: a group with no audit event carries no link.

Policy Lifecycle's draft edits and status/version backfills have no `audit_events` row today, so
their groups are logged with a null link (the store and the nullable column already allow it). A
given id is still validated as a UUID and still anchors the group.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.edge_identity_backfill import backfill_edge_identity
from ps_service.graph_gateway.models import ExpectedPosition, MutationGroup, UpsertNode

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Protocol

    from ps_service.logging import LogEmitter

    class MakeEmitter(Protocol):
        """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

        def __call__(self) -> tuple[LogEmitter, Path]: ...

    class ReadLines(Protocol):
        """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

        def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"


def _group(
    node_id: str = "cap-1", *, audit_event_id: str | None = None, **extra: object
) -> MutationGroup:
    return MutationGroup.model_validate(
        {
            "graph": _GRAPH,
            "audit_event_id": audit_event_id,
            "primitives": [UpsertNode(label="Capability", id=node_id)],
            **extra,
        }
    )


def test_a_group_without_an_audit_event_id_is_valid_and_defaults_to_none() -> None:
    omitted = MutationGroup(graph=_GRAPH, primitives=(UpsertNode(label="Capability", id="cap-1"),))

    assert omitted.audit_event_id is None
    assert _group().audit_event_id is None


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "3f2b8c1e"])
def test_a_given_audit_event_id_must_still_be_a_uuid(bad: str) -> None:
    with pytest.raises(ValidationError):
        _group(audit_event_id=bad)


def test_a_group_with_no_audit_event_is_logged_with_a_null_link_and_applied() -> None:
    rig = GatewayRig()

    outcome = rig.gateway.submit_group(_group())

    assert outcome.status == "applied"
    (logged,) = rig.store.groups
    assert rig.store.audit_link_of(logged) is None
    assert rig.store.read_groups_by_audit_event(_AUDIT_EVENT_ID) == ()
    assert ("Capability", "cap-1") in rig.graphs.open(_GRAPH).nodes


def test_a_group_with_an_audit_event_id_is_still_linked_to_it() -> None:
    rig = GatewayRig()
    rig.store.audit_rows.add(_AUDIT_EVENT_ID)

    rig.gateway.submit_group(_group(audit_event_id=_AUDIT_EVENT_ID))

    (logged,) = rig.store.read_groups_by_audit_event(_AUDIT_EVENT_ID)
    assert rig.store.audit_link_of(logged) == _AUDIT_EVENT_ID


def test_a_group_with_no_audit_event_that_changes_nothing_reports_unchanged() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group())

    outcome = rig.gateway.submit_group(_group())

    assert (outcome.status, outcome.first_position) == ("unchanged", None)
    assert len(rig.store.groups) == 1


def test_preconditions_and_checkpoints_work_on_a_group_with_no_audit_event() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group())

    outcome = rig.gateway.submit_group(
        _group(
            "cap-2",
            preconditions=[ExpectedPosition(position=1)],
            checkpoint_requested=True,
        )
    )

    assert (outcome.status, outcome.checkpoint) == ("applied", "recorded")


def test_replay_applies_a_log_that_holds_groups_with_no_audit_link() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group())
    graph = rig.graphs.open(_GRAPH)
    graph.flush()

    rig.restart().replay_graph(_GRAPH)

    assert ("Capability", "cap-1") in graph.nodes


def test_an_in_transaction_group_with_no_audit_event_commits_and_applies() -> None:
    rig = GatewayRig()
    transaction = rig.store.begin()

    with rig.gateway.submit_group_in_transaction(transaction.cursor, _group()) as staged:
        transaction.commit()
        outcome = staged.complete()

    assert outcome.status == "applied"
    (logged,) = rig.store.groups
    assert rig.store.audit_link_of(logged) is None


def test_the_legacy_edge_stamp_can_be_logged_with_no_audit_link() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            primitives=(
                UpsertNode(label="Policy", id="p-1"),
                UpsertNode(label="Standard", id="s-1"),
            ),
        )
    )
    rig.graphs.open(_GRAPH).seed_legacy_edge(
        ("SUPPORTED_BY", "Policy", "p-1", "Standard", "s-1"), {}
    )

    outcome = rig.gateway.stamp_legacy_edge_identity(_GRAPH, None)

    assert (outcome.status, outcome.stamped) == ("applied", 1)
    stamp = rig.store.groups[-1]
    assert rig.store.audit_link_of(stamp) is None
    assert ("SUPPORTED_BY", "Policy", "p-1", "Standard", "s-1", "SUPPORTED_BY:p-1->s-1") in (
        rig.graphs.open(_GRAPH).edges
    )


def test_stamping_a_graph_with_nothing_to_stamp_logs_nothing_with_no_audit_link() -> None:
    rig = GatewayRig()

    outcome = backfill_edge_identity(rig.gateway, _GRAPH, None, reader=rig.graphs.open(_GRAPH))

    assert (outcome.status, rig.store.groups) == ("unchanged", [])


def test_log_lines_of_a_group_with_no_audit_event_omit_the_audit_event_id(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter=emitter)
    rig.gateway.submit_group(_group())
    rig.gateway.submit_group(_group())  # unchanged
    emitter.flush()

    lines = [line for line in read_lines(log_path) if line["action"] == "apply_group"]

    assert [line["outcome"] for line in lines] == ["success", "unchanged"]
    assert all("audit_event_id" not in line for line in lines)


def test_log_lines_of_a_group_with_an_audit_event_still_carry_the_id(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter=emitter)
    rig.store.audit_rows.add(_AUDIT_EVENT_ID)
    rig.gateway.submit_group(_group(audit_event_id=_AUDIT_EVENT_ID))
    emitter.flush()

    (line,) = [line for line in read_lines(log_path) if line["action"] == "apply_group"]

    assert line["audit_event_id"] == _AUDIT_EVENT_ID
