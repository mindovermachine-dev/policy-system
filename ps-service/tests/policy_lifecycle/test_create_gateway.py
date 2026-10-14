"""Creating a Policy is ONE gateway group linked to its `applied` audit row.

`create_policy_draft` without a Capability claim: the `applied` `policy.create_draft` row is written
FIRST and its id anchors ONE group holding the Policy, its optional `SUPERSEDED_BY` edge
and every Standard and Control with their parent edges. A gateway failure after the row is a
`failed` row with the same details plus the persistence error; a value the gateway
cannot store is refused the same way. The group is atomic: a failure leaves no partial draft
(section 9 item 11), which the direct writer's one-statement-per-node sequence could not promise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from policy_lifecycle._gateway_fakes import SeedControl, SeedStandard
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.policy_lifecycle.errors import (
    PolicyLifecycleGraphUnavailableError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.service import (
    ControlDraftInput,
    PolicyDraftResult,
    StandardDraftInput,
    create_policy_draft,
)

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig
    from ps_service.graph_gateway.models import GraphLogGroup

_ACTOR = ("alice", "https://issuer.example")
_TITLE = "Data Protection Policy"
_PRIOR = "pol_prior_aaaaaa"

_TREE = (
    StandardDraftInput(
        title="Encryption",
        controls=(ControlDraftInput(title="Rotate keys", control_type="automated"),),
    ),
    StandardDraftInput(
        title="Backups",
        controls=(
            ControlDraftInput(title="Restore test"),
            ControlDraftInput(title="Offsite copy", control_type="automated"),
        ),
    ),
)


def _create(
    rig: LifecycleRig,
    *,
    standards: tuple[StandardDraftInput, ...] = (),
    supersedes_policy_id: str | None = None,
) -> PolicyDraftResult:
    return create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        standards=standards,
        supersedes_policy_id=supersedes_policy_id,
        graph=rig.read,
        audit_store=rig.audit,
        submitter=rig.gateway,
    )


def _ops(group: GraphLogGroup) -> list[str]:
    return [str(entry.content["op"]) for entry in group.entries]


def _seed_approved_prior(rig: LifecycleRig) -> None:
    rig.seed_policy(
        _PRIOR,
        title=_TITLE,
        status="approved",
        version="3",
        owner=_ACTOR,
        standards=(
            SeedStandard(
                "std_prior",
                "Encryption",
                status="approved",
                extra={"procedure": "rotate quarterly", "description": "d"},
                controls=(
                    SeedControl(
                        "ctl_prior",
                        "Rotate keys",
                        status="approved",
                        control_type="automated",
                        extra={"evidence_ref": "https://example.com/e"},
                    ),
                ),
            ),
        ),
        legacy_edges=False,
    )


def test_a_policy_with_children_is_one_group_of_nodes_then_parent_edges(
    lifecycle_rig: LifecycleRig,
) -> None:
    result = _create(lifecycle_rig, standards=_TREE)

    (group,) = lifecycle_rig.store.groups
    assert _ops(group).count("upsert_node") == 1 + 2 + 3  # Policy, 2 Standards, 3 Controls
    assert _ops(group).count("upsert_edge") == 2 + 3  # one parent edge per child
    assert len(result.standard_ids) == 2
    assert len(result.control_ids) == 3
    nodes = lifecycle_rig.graph.nodes
    assert nodes[("Policy", result.policy_id)]["status"] == "draft"
    assert nodes[("Standard", standard_id(result.policy_id, "Encryption"))]["status"] == "draft"
    assert (
        nodes[("Control", control_id(standard_id(result.policy_id, "Backups"), "Restore test"))][
            "type"
        ]
        == "manual"
    )


def test_the_children_hang_off_their_parents_by_identified_edges(
    lifecycle_rig: LifecycleRig,
) -> None:
    result = _create(lifecycle_rig, standards=_TREE[:1])

    std = standard_id(result.policy_id, "Encryption")
    ctl = control_id(std, "Rotate keys")
    edges = {key[5] for key in lifecycle_rig.graph.edges}
    assert edges == {
        f"SUPPORTED_BY:{result.policy_id}->{std}",
        f"IMPLEMENTED_BY:{std}->{ctl}",
    }


def test_a_bare_policy_is_one_group_of_one_node(lifecycle_rig: LifecycleRig) -> None:
    _create(lifecycle_rig)

    (group,) = lifecycle_rig.store.groups
    assert _ops(group) == ["upsert_node"]


def test_the_group_is_linked_to_the_applied_row_that_was_written_before_it(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The `applied` row precedes the graph write and anchors the group."""
    _create(lifecycle_rig, standards=_TREE)

    (group,) = lifecycle_rig.store.groups
    (applied,) = lifecycle_rig.audit.rows
    assert applied.outcome == "applied"
    assert lifecycle_rig.store.audit_link_of(group) == lifecycle_rig.audit.event_ids[0]
    events = lifecycle_rig.events
    assert events.index("audit:policy.create_draft:applied") < next(
        i for i, event in enumerate(events) if "log" in event and "append" in event
    )


def test_a_taken_title_is_rejected_with_a_rejected_row_and_writes_nothing(
    lifecycle_rig: LifecycleRig,
) -> None:
    _create(lifecycle_rig)
    logged = lifecycle_rig.logged_groups

    with pytest.raises(PolicyTitleAlreadyExistsError):
        _create(lifecycle_rig)

    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "rejected"]
    assert lifecycle_rig.logged_groups == logged


def test_a_fork_links_the_prior_and_carries_its_content_as_draft_children(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_approved_prior(lifecycle_rig)

    result = _create(lifecycle_rig, supersedes_policy_id=_PRIOR)

    (group,) = lifecycle_rig.store.groups
    # Policy, Standard, Control nodes; SUPERSEDED_BY, SUPPORTED_BY, IMPLEMENTED_BY edges.
    assert sorted(_ops(group)) == ["upsert_edge"] * 3 + ["upsert_node"] * 3
    assert _ops(group)[:2] == ["upsert_node", "upsert_edge"]  # the Policy, then its supersede edge
    assert result.version == "4"
    assert (
        "SUPERSEDED_BY",
        "Policy",
        _PRIOR,
        "Policy",
        result.policy_id,
        f"SUPERSEDED_BY:{_PRIOR}->{result.policy_id}",
    ) in lifecycle_rig.graph.edges
    nodes = lifecycle_rig.graph.nodes
    (forked_standard,) = result.standard_ids
    (forked_control,) = result.control_ids
    assert nodes[("Standard", forked_standard)] == {
        "procedure": "rotate quarterly",
        "description": "d",
        "title": "Encryption",
        "status": "draft",
    }
    assert nodes[("Control", forked_control)] == {
        "evidence_ref": "https://example.com/e",
        "title": "Rotate keys",
        "type": "automated",
        "status": "draft",
    }
    assert nodes[("Standard", "std_prior")]["status"] == "approved"  # the prior is untouched


def test_a_forked_node_carrying_a_reserved_key_is_refused_with_a_failed_row(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The gateway refuses a key it cannot store."""
    _seed_approved_prior(lifecycle_rig)
    lifecycle_rig.graph.nodes[("Control", "ctl_prior")]["embedding"] = [0.1, 0.2]
    before = dict(lifecycle_rig.graph.nodes)

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _create(lifecycle_rig, supersedes_policy_id=_PRIOR)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]
    assert lifecycle_rig.logged_groups == 0
    assert lifecycle_rig.graph.nodes == before


def test_a_gateway_failure_after_the_applied_row_records_failed_with_the_same_details(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.store.fail_appends()

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _create(lifecycle_rig, standards=_TREE)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert raised.value.__cause__ is not None
    applied, failed = lifecycle_rig.audit.rows
    assert (applied.outcome, failed.outcome) == ("applied", "failed")
    assert failed.details == applied.details
    assert lifecycle_rig.graph.nodes == {}  # atomic: no partial draft


def test_a_group_committed_but_not_applied_is_a_failure_with_a_failed_row(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _create(lifecycle_rig)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]


def test_an_audit_store_that_cannot_report_the_event_id_is_a_wiring_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    class _PlainStore:
        def record_standalone(self, **kwargs: object) -> None:
            del kwargs

    with pytest.raises(TypeError):
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            graph=lifecycle_rig.read,
            audit_store=_PlainStore(),  # pyright: ignore[reportArgumentType]
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.logged_groups == 0


def test_a_legacy_prior_that_needs_a_fill_is_filled_before_the_fork(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The fork's prior read backfills through the same submitter, then the create group follows."""
    lifecycle_rig.seed_policy(
        _PRIOR,
        title=_TITLE,
        status="approved",
        version=None,
        owner=_ACTOR,
        standards=(SeedStandard("std_prior", "Encryption", status=None),),
        legacy_edges=False,
    )

    result = _create(lifecycle_rig, supersedes_policy_id=_PRIOR)

    fill, create = lifecycle_rig.store.groups
    assert set(_ops(fill)) == {"merge_property"}
    assert lifecycle_rig.store.audit_link_of(fill) is None
    assert lifecycle_rig.store.audit_link_of(create) == lifecycle_rig.audit.event_ids[0]
    assert result.version == "2"
