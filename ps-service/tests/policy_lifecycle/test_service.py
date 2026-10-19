"""Tests for the `ps_service.policy_lifecycle.service` entry points.

Every write is a Graph Write Gateway group, so these tests run the real service
functions over the gateway rig (`LifecycleRig`: in-memory log and FalkorDB fakes, the true
boundaries) and assert on the graph the groups built, the log and the audit rows -- including that
the `applied` row precedes the append. The graph handle the service reads through is
the rig's read-only double; it refuses any write statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); mirrors `test_access_role_tools.py`'s own cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import (
    FlippingRead,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
    policy_manager_enforcer,
)
from ps_service.api.errors import AccessDeniedError
from ps_service.authz.models import AccessRole
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.domain_mapper.identity import policy_id as compute_policy_id
from ps_service.policy_lifecycle.audit_actions import (
    PolicyCreateDraftDetails,
    PolicyTransitionDetails,
)
from ps_service.policy_lifecycle.errors import (
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyGovernanceConflictError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
    PolicyStandardNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.service import (
    ControlDraftInput,
    PolicyApproveResult,
    StandardDraftInput,
    add_control_to_draft,
    add_standard_to_draft,
    approve_policy,
    create_policy_draft,
    get_policy,
    propose_policy,
    reject_policy,
    revert_policy_to_draft,
    update_control_draft,
    update_policy_draft,
    update_standard_draft,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policy_lifecycle._gateway_fakes import LifecycleRig

_ACTOR = ("alice", "https://issuer.example")
_TITLE = "Data Protection Policy"


def test_success_creates_policy_with_expected_shape_and_draft_children(
    lifecycle_rig: LifecycleRig,
) -> None:
    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        standards=(
            StandardDraftInput(
                title="Encryption Standard",
                controls=(ControlDraftInput(title="Key Rotation Check", control_type="automated"),),
            ),
        ),
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    assert result.title == _TITLE
    assert result.status == "draft"
    assert result.version == "1"
    assert result.owner_subject == _ACTOR[0]
    assert result.owner_issuer == _ACTOR[1]
    assert result.policy_id.startswith("pol_data_protection_policy_")
    assert len(result.standard_ids) == 1
    assert len(result.control_ids) == 1
    # One gateway group holds the Policy, the Standard and the Control.
    assert lifecycle_rig.logged_groups == 1
    nodes = lifecycle_rig.graph.nodes
    assert {label for (label, _) in nodes} == {"Policy", "Standard", "Control"}
    assert {key[0] for key in lifecycle_rig.graph.edges} == {"SUPPORTED_BY", "IMPLEMENTED_BY"}


def test_duplicate_title_raises_named_error_records_rejected_audit_and_skips_graph_write(
    lifecycle_rig: LifecycleRig,
) -> None:
    existing_id = compute_policy_id(_TITLE)
    lifecycle_rig.seed_policy(existing_id, title=_TITLE, owner=_ACTOR, legacy_edges=False)

    with pytest.raises(PolicyTitleAlreadyExistsError) as exc_info:
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.existing_policy_id == existing_id
    assert exc_info.value.title == _TITLE
    assert existing_id in str(exc_info.value)
    assert lifecycle_rig.store.groups == []
    assert len(lifecycle_rig.audit.rows) == 1
    rejected_call = lifecycle_rig.audit.rows[0]
    assert rejected_call.action == "policy.create_draft"
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "title_already_exists"


def test_audit_write_precedes_graph_write(lifecycle_rig: LifecycleRig) -> None:
    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    events = lifecycle_rig.events
    assert events.index("audit:policy.create_draft:applied") < next(
        i for i, event in enumerate(events) if "append" in event
    )


def test_graph_failure_after_audit_success_records_failed_event_and_raises(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("boom"))

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]


_OWNER = ("alice", "https://issuer.example")
_OTHER = ("bob", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")


@dataclass
class _ControlFixture:
    id: str
    title: str
    status: str
    control_type: str


@dataclass
class _StandardFixture:
    id: str
    title: str
    status: str
    controls: tuple[_ControlFixture, ...] = field(default_factory=tuple)


@dataclass
class _PolicyFixture:
    id: str
    title: str
    status: str
    version: str
    owner: tuple[str, str]
    standards: tuple[_StandardFixture, ...] = field(default_factory=tuple)


def _draft_policy(*, owner: tuple[str, str] = _OWNER, policy_id: str = "pol_x") -> _PolicyFixture:
    return _PolicyFixture(id=policy_id, title="X", status="draft", version="1", owner=owner)


def test_owner_reads_own_draft_successfully_with_full_tree(lifecycle_rig: LifecycleRig) -> None:
    policy = _PolicyFixture(
        id="pol_x",
        title="X",
        status="draft",
        version="1",
        owner=_OWNER,
        standards=(
            _StandardFixture(
                id="std_1",
                title="Encryption Standard",
                status="draft",
                controls=(
                    _ControlFixture(
                        id="ctrl_1", title="Key Rotation", status="draft", control_type="automated"
                    ),
                ),
            ),
        ),
    )
    graph = _rig_graph(lifecycle_rig, policy)

    result = get_policy(
        policy_id="pol_x",
        graph=graph,
        submitter_source=lambda: lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.owner_subject == _OWNER[0]
    assert result.owner_issuer == _OWNER[1]
    assert len(result.standards) == 1
    standard = result.standards[0]
    assert standard.standard_id == "std_1"
    assert len(standard.controls) == 1
    assert standard.controls[0].control_id == "ctrl_1"
    assert standard.controls[0].control_type == "automated"


def test_non_owner_reads_a_draft(lifecycle_rig: LifecycleRig) -> None:
    graph = _rig_graph(lifecycle_rig, _draft_policy())

    result = get_policy(
        policy_id="pol_x",
        graph=graph,
        submitter_source=lambda: lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.owner_subject == _OWNER[0]


@pytest.mark.parametrize("status", ["proposed", "approved", "deprecated"])
def test_non_draft_policy_is_readable_by_any_authenticated_caller(
    lifecycle_rig: LifecycleRig, status: str
) -> None:
    graph = _rig_graph(
        lifecycle_rig,
        _PolicyFixture(id="pol_y", title="Y", status=status, version="1", owner=_OWNER),
    )

    result = get_policy(
        policy_id="pol_y",
        graph=graph,
        submitter_source=lambda: lifecycle_rig.gateway,
    )

    assert result.status == status


def test_nonexistent_policy_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    graph = _rig_graph(lifecycle_rig, None)

    with pytest.raises(PolicyNotFoundError):
        get_policy(
            policy_id="pol_missing",
            graph=graph,
            submitter_source=lambda: lifecycle_rig.gateway,
        )


# --- `propose_policy` -------------------------------------


def _rig_graph(
    rig: LifecycleRig,
    policies: Mapping[str, _PolicyFixture] | _PolicyFixture | None,
    *,
    superseded_by: Mapping[str, str] | None = None,
    governed: Mapping[str, str] | None = None,
) -> PolicyReadGraph:
    """Seed `policies` (fork edges, governed Capabilities) into the rig; return its read handle.

    `superseded_by` maps a successor to its prior; `governed` maps a Capability to its governor.
    """
    if policies is None:
        fixtures: Mapping[str, _PolicyFixture] = {}
    elif isinstance(policies, _PolicyFixture):
        fixtures = {policies.id: policies}
    else:
        fixtures = policies
    for fixture in fixtures.values():
        rig.seed_policy(
            fixture.id,
            title=fixture.title,
            status=fixture.status,
            version=fixture.version,
            owner=fixture.owner,
            standards=tuple(
                SeedStandard(
                    standard.id,
                    standard.title,
                    status=standard.status,
                    controls=tuple(
                        SeedControl(
                            control.id,
                            control.title,
                            status=control.status,
                            control_type=control.control_type,
                        )
                        for control in standard.controls
                    ),
                )
                for standard in fixture.standards
            ),
            legacy_edges=False,
        )
    for successor_id, prior_id in (superseded_by or {}).items():
        rig.seed_supersedes(prior_id, successor_id, legacy=False)
    for capability_id, policy_id in (governed or {}).items():
        rig.seed_capability(capability_id)
        rig.seed_governs(capability_id, policy_id, legacy=False)
    return rig.read


def _status(rig: LifecycleRig, policy_id: str) -> object:
    return rig.graph.nodes[("Policy", policy_id)]["status"]


def _proposable_draft(
    *,
    owner: tuple[str, str] = _OWNER,
    policy_id: str = "pol_x",
    standards: tuple[_StandardFixture, ...] = (
        _StandardFixture(id="std_1", title="Encryption Standard", status="draft"),
    ),
) -> _PolicyFixture:
    return _PolicyFixture(
        id=policy_id, title="X", status="draft", version="1", owner=owner, standards=standards
    )


def test_owner_proposes_complete_draft_succeeds_and_cascades_whole_tree(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _proposable_draft(
        standards=(
            _StandardFixture(
                id="std_1",
                title="Encryption Standard",
                status="draft",
                controls=(
                    _ControlFixture(
                        id="ctrl_1", title="Key Rotation", status="draft", control_type="automated"
                    ),
                ),
            ),
        )
    )
    graph = _rig_graph(lifecycle_rig, policy)
    audit_store = lifecycle_rig.audit

    result = propose_policy(
        actor=_OWNER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert result.status == "proposed"
    assert result.standard_ids == ("std_1",)
    assert result.control_ids == ("ctrl_1",)
    assert lifecycle_rig.logged_groups == 1
    assert _status(lifecycle_rig, "pol_x") == "proposed"
    assert len(audit_store.rows) == 1
    applied_call = audit_store.rows[0]
    assert applied_call.action == "policy.propose"
    assert applied_call.outcome == "applied"
    assert applied_call.details["affected_node_ids"] == ["pol_x", "std_1", "ctrl_1"]
    assert applied_call.details["from_status"] == "draft"
    assert applied_call.details["to_status"] == "proposed"
    assert "reason_code" not in applied_call.details


def test_non_owner_attempt_is_rejected_with_rejected_audit(lifecycle_rig: LifecycleRig) -> None:
    graph = _rig_graph(lifecycle_rig, _proposable_draft())
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyDraftAccessDeniedError):
        propose_policy(
            actor=_OTHER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.actor_subject == _OTHER[0]
    assert rejected_call.action == "policy.propose"
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "access_denied"
    assert rejected_call.details["affected_node_ids"] == ["pol_x"]
    assert rejected_call.details["from_status"] == "draft"
    assert rejected_call.details["to_status"] == "draft"


def test_zero_standard_draft_is_rejected_as_incomplete_with_rejected_audit(
    lifecycle_rig: LifecycleRig,
) -> None:
    graph = _rig_graph(lifecycle_rig, _proposable_draft(standards=()))
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyIncompleteForProposalError):
        propose_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "incomplete_for_proposal"


def test_standard_with_zero_controls_still_succeeds(lifecycle_rig: LifecycleRig) -> None:
    policy = _proposable_draft(
        standards=(_StandardFixture(id="std_1", title="Encryption Standard", status="draft"),)
    )
    graph = _rig_graph(lifecycle_rig, policy)
    audit_store = lifecycle_rig.audit

    result = propose_policy(
        actor=_OWNER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.status == "proposed"
    assert result.standard_ids == ("std_1",)
    assert result.control_ids == ()
    assert lifecycle_rig.logged_groups == 1


def test_wrong_current_status_is_rejected_via_require_status(lifecycle_rig: LifecycleRig) -> None:
    policy = _proposable_draft()
    policy.status = "proposed"
    graph = _rig_graph(lifecycle_rig, policy)
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyInvalidStatusTransitionError):
        propose_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "invalid_status"
    assert rejected_call.details["from_status"] == "proposed"
    assert rejected_call.details["to_status"] == "proposed"


def test_propose_audit_write_precedes_graph_write(lifecycle_rig: LifecycleRig) -> None:
    graph = _rig_graph(lifecycle_rig, _proposable_draft())
    audit_store = lifecycle_rig.audit

    propose_policy(
        actor=_OWNER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        submitter=lifecycle_rig.gateway,
    )

    events = lifecycle_rig.events
    assert next(i for i, event in enumerate(events) if event.startswith("audit:")) < next(
        i for i, event in enumerate(events) if "append" in event
    )


def test_propose_graph_failure_after_audit_success_records_failed_event_and_raises(
    lifecycle_rig: LifecycleRig,
) -> None:
    graph = _rig_graph(lifecycle_rig, _proposable_draft())
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("boom"))
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        propose_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )

    assert [call.outcome for call in audit_store.rows] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.rows} == {"pol_x"}


def test_propose_nonexistent_policy_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    graph = _rig_graph(lifecycle_rig, None)
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyNotFoundError):
        propose_policy(
            actor=_OWNER,
            policy_id="pol_missing",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )


# --- `approve_policy` ---------------------------------

_MANAGER = ("carol", "https://issuer.example")
_OWNER_OTHER_ISSUER = (_OWNER[0], "https://other-issuer.example")


def _approvable_proposed(
    *,
    owner: tuple[str, str] = _OWNER,
    policy_id: str = "pol_x",
    status: str = "proposed",
    standards: tuple[_StandardFixture, ...] = (
        _StandardFixture(id="std_1", title="Encryption Standard", status="proposed"),
    ),
) -> _PolicyFixture:
    return _PolicyFixture(
        id=policy_id, title="X", status=status, version="1", owner=owner, standards=standards
    )


def test_policy_manager_non_owner_approves_successfully(lifecycle_rig: LifecycleRig) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    result = approve_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert result.status == "approved"
    assert result.standard_ids == ("std_1",)
    assert result.auto_deprecated_policy_id is None
    assert lifecycle_rig.logged_groups == 1
    assert _status(lifecycle_rig, "pol_x") == "approved"
    assert len(audit_store.rows) == 1
    applied_call = audit_store.rows[0]
    assert applied_call.action == "policy.approve"
    assert applied_call.outcome == "applied"
    assert applied_call.details["from_status"] == "proposed"
    assert applied_call.details["to_status"] == "approved"


def test_owner_who_also_holds_policy_manager_self_approval_is_blocked(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicySelfApprovalBlockedError):
        approve_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "self_approval_blocked"


def test_same_subject_different_issuer_approver_succeeds(lifecycle_rig: LifecycleRig) -> None:
    """Critical regression case: same `sub`, different `iss`, is a different person."""
    policy = _approvable_proposed(owner=_OWNER)
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER_OTHER_ISSUER, access_role=AccessRole.POLICY_MANAGER)

    result = approve_policy(
        actor=_OWNER_OTHER_ISSUER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=lifecycle_rig.gateway,
    )

    assert result.status == "approved"
    assert lifecycle_rig.logged_groups == 1
    assert len(audit_store.rows) == 1
    assert audit_store.rows[0].outcome == "applied"


def test_non_policy_manager_is_rejected_with_access_denied(lifecycle_rig: LifecycleRig) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()

    with pytest.raises(AccessDeniedError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert audit_store.rows == []


def test_policy_lifecycle_approve_denial_through_the_service_writes_no_access_gate_row(
    lifecycle_rig: LifecycleRig,
) -> None:
    graph = _rig_graph(lifecycle_rig, {"pol_x": _approvable_proposed()})

    with pytest.raises(AccessDeniedError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=lifecycle_rig.audit,
            enforce_policy_manager=policy_manager_enforcer(FakeAccessRoleStore()),
            submitter=lifecycle_rig.gateway,
        )

    assert [r.action for r in lifecycle_rig.audit.rows if r.action == "access.gate"] == []
    assert lifecycle_rig.audit.rows == []


def test_approve_wrong_current_status_is_rejected_via_require_status(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed(status="draft")
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyInvalidStatusTransitionError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "invalid_status"


def test_approve_audit_write_precedes_graph_write(lifecycle_rig: LifecycleRig) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    approve_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=lifecycle_rig.gateway,
    )

    events = lifecycle_rig.events
    assert next(i for i, event in enumerate(events) if event.startswith("audit:")) < next(
        i for i, event in enumerate(events) if "append" in event
    )


def test_approve_graph_failure_after_audit_success_records_failed_event_and_raises(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("boom"))
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert [call.outcome for call in audit_store.rows] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.rows} == {"pol_x"}


def test_approve_nonexistent_policy_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    graph = _rig_graph(lifecycle_rig, {})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyNotFoundError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_missing",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )


def test_approving_a_superseded_by_approved_prior_auto_deprecates_it_in_the_same_call(
    lifecycle_rig: LifecycleRig,
) -> None:
    successor = _approvable_proposed(policy_id="pol_new")
    prior = _approvable_proposed(policy_id="pol_old", status="approved")
    graph = _rig_graph(
        lifecycle_rig,
        {"pol_new": successor, "pol_old": prior},
        superseded_by={"pol_new": "pol_old"},
    )
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    result = approve_policy(
        actor=_MANAGER,
        policy_id="pol_new",
        graph=graph,
        audit_store=audit_store,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=lifecycle_rig.gateway,
    )

    assert result.status == "approved"
    assert result.auto_deprecated_policy_id == "pol_old"
    assert lifecycle_rig.logged_groups == 2
    assert _status(lifecycle_rig, "pol_new") == "approved"
    assert _status(lifecycle_rig, "pol_old") == "deprecated"
    assert len(audit_store.rows) == 2
    successor_call, prior_call = audit_store.rows
    assert successor_call.action == "policy.approve"
    assert successor_call.resource_id == "pol_new"
    assert successor_call.outcome == "applied"
    assert prior_call.action == "policy.auto_deprecate"
    assert prior_call.resource_id == "pol_old"
    assert prior_call.outcome == "applied"
    assert prior_call.details["from_status"] == "approved"
    assert prior_call.details["to_status"] == "deprecated"


@pytest.mark.parametrize("prior_status", ["draft", "proposed"])
def test_superseded_by_prior_not_yet_approved_is_left_untouched(
    lifecycle_rig: LifecycleRig, prior_status: str
) -> None:
    successor = _approvable_proposed(policy_id="pol_new")
    prior = _approvable_proposed(policy_id="pol_old", status=prior_status)
    graph = _rig_graph(
        lifecycle_rig,
        {"pol_new": successor, "pol_old": prior},
        superseded_by={"pol_new": "pol_old"},
    )
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    result = approve_policy(
        actor=_MANAGER,
        policy_id="pol_new",
        graph=graph,
        audit_store=audit_store,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=lifecycle_rig.gateway,
    )

    assert result.status == "approved"
    assert result.auto_deprecated_policy_id is None
    assert lifecycle_rig.logged_groups == 1
    assert _status(lifecycle_rig, "pol_new") == "approved"
    assert len(audit_store.rows) == 1
    assert audit_store.rows[0].action == "policy.approve"


def _approve_fork(rig: LifecycleRig, graph: PolicyReadGraph, policy_id: str) -> PolicyApproveResult:
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)
    return approve_policy(
        actor=_MANAGER,
        policy_id=policy_id,
        graph=graph,
        audit_store=rig.audit,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=rig.gateway,
    )


def _fork_of_approved_prior(rig: LifecycleRig, *, governed: dict[str, str]) -> PolicyReadGraph:
    successor = _approvable_proposed(policy_id="pol_new")
    prior = _approvable_proposed(policy_id="pol_old", status="approved")
    return _rig_graph(
        rig,
        {"pol_new": successor, "pol_old": prior},
        superseded_by={"pol_new": "pol_old"},
        governed=governed,
    )


def _governed(rig: LifecycleRig) -> dict[str, str]:
    """`{capability_id: governing_policy_id}` of the rig graph's `GOVERNED_BY` edges."""
    return {key[2]: key[4] for key in rig.graph.edges if key[0] == "GOVERNED_BY"}


def test_approve_fork_moves_governed_capabilities_in_one_write_and_audits_ids(
    lifecycle_rig: LifecycleRig,
) -> None:
    graph = _fork_of_approved_prior(lifecycle_rig, governed={"c1": "pol_old", "c2": "pol_old"})
    audit_store = lifecycle_rig.audit

    result = _approve_fork(lifecycle_rig, graph, "pol_new")

    assert result.governed_capability_ids == ("c1", "c2")
    assert _governed(lifecycle_rig) == {"c1": "pol_new", "c2": "pol_new"}
    assert _status(lifecycle_rig, "pol_new") == "approved"
    # one repoint group for the successor, one plain cascade group for the auto-deprecated prior
    assert lifecycle_rig.logged_groups == 2
    assert _status(lifecycle_rig, "pol_old") == "deprecated"
    approve_call, deprecate_call = audit_store.rows
    assert (approve_call.action, approve_call.outcome) == ("policy.approve", "applied")
    assert approve_call.details["capability_ids"] == ["c1", "c2"]
    assert deprecate_call.action == "policy.auto_deprecate"
    assert deprecate_call.details.get("capability_ids", []) == []


def test_approve_fork_audit_precedes_the_repoint_write(lifecycle_rig: LifecycleRig) -> None:
    graph = _fork_of_approved_prior(lifecycle_rig, governed={"c1": "pol_old"})

    _approve_fork(lifecycle_rig, graph, "pol_new")

    events = lifecycle_rig.events
    assert next(i for i, event in enumerate(events) if event.startswith("audit:")) < next(
        i for i, event in enumerate(events) if "append" in event
    )


def _third_party_takes_c2(rig: LifecycleRig) -> PolicyReadGraph:
    """A read double under which `c2` moves to `pol_third` right after the governance pre-read."""

    def _flip() -> None:
        del rig.graph.edges[
            next(k for k in rig.graph.edges if k[:3] == ("GOVERNED_BY", "Capability", "c2"))
        ]
        rig.seed_governs("c2", "pol_third", legacy=False)

    return FlippingRead(rig.graph, _flip, marker="collect(cap.id)")


def test_approve_fork_guard_mismatch_raises_conflict_and_writes_nothing_else(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Governed state changed after the pre-read -> no row, nothing moved."""
    _fork_of_approved_prior(lifecycle_rig, governed={"c1": "pol_old", "c2": "pol_old"})
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyGovernanceConflictError):
        _approve_fork(lifecycle_rig, _third_party_takes_c2(lifecycle_rig), "pol_new")

    assert lifecycle_rig.logged_groups == 0  # nothing at all was written
    assert _status(lifecycle_rig, "pol_new") == "proposed"
    assert _status(lifecycle_rig, "pol_old") == "approved"  # zero auto-deprecate
    assert _governed(lifecycle_rig) == {"c1": "pol_old", "c2": "pol_third"}
    assert [call.outcome for call in audit_store.rows] == ["applied", "failed"]
    failed = audit_store.rows[1]
    assert failed.action == "policy.approve"
    assert failed.details["reason_code"] == "governance_conflict"
    assert failed.details["capability_ids"] == []


def test_approve_fork_of_deprecated_prior_with_no_governed_caps_uses_plain_cascade(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Legacy forks without GOVERNED_BY edges stay approvable."""
    successor = _approvable_proposed(policy_id="pol_new")
    prior = _approvable_proposed(policy_id="pol_old", status="deprecated")
    graph = _rig_graph(
        lifecycle_rig,
        {"pol_new": successor, "pol_old": prior},
        superseded_by={"pol_new": "pol_old"},
    )
    audit_store = lifecycle_rig.audit

    result = _approve_fork(lifecycle_rig, graph, "pol_new")

    assert result.governed_capability_ids == ()
    assert _status(lifecycle_rig, "pol_new") == "approved"
    assert audit_store.rows[0].details["capability_ids"] == []


def test_approve_fork_when_sibling_fork_already_approved_leaves_caps_on_sibling(
    lifecycle_rig: LifecycleRig,
) -> None:
    """F2 approve finds zero caps on P (they moved to F1); plain cascade, caps stay."""
    f2 = _approvable_proposed(policy_id="pol_f2")
    prior = _approvable_proposed(policy_id="pol_old", status="deprecated")
    graph = _rig_graph(
        lifecycle_rig,
        {"pol_f2": f2, "pol_old": prior},
        superseded_by={"pol_f2": "pol_old"},
        governed={"c1": "pol_f1"},
    )

    result = _approve_fork(lifecycle_rig, graph, "pol_f2")

    assert result.governed_capability_ids == ()
    assert _governed(lifecycle_rig) == {"c1": "pol_f1"}
    assert _status(lifecycle_rig, "pol_f2") == "approved"


def test_approve_non_fork_reads_no_fork_capabilities_and_uses_plain_cascade(
    lifecycle_rig: LifecycleRig,
) -> None:
    graph = _rig_graph(lifecycle_rig, {"pol_x": _approvable_proposed()})

    result = _approve_fork(lifecycle_rig, graph, "pol_x")

    assert result.governed_capability_ids == ()
    assert _status(lifecycle_rig, "pol_x") == "approved"


def test_approve_gate_failure_rejects_before_any_governance_read(
    lifecycle_rig: LifecycleRig,
) -> None:
    graph = _fork_of_approved_prior(lifecycle_rig, governed={"c1": "pol_old"})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicySelfApprovalBlockedError):
        approve_policy(
            actor=_OWNER,
            policy_id="pol_new",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert not any("collect(cap.id)" in q for q in lifecycle_rig.read.statements)
    assert lifecycle_rig.store.groups == []


def test_approve_fork_repoint_graph_failure_records_failed_and_raises_unavailable(
    lifecycle_rig: LifecycleRig,
) -> None:
    graph = _fork_of_approved_prior(lifecycle_rig, governed={"c1": "pol_old"})
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("boom"))
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        _approve_fork(lifecycle_rig, graph, "pol_new")

    assert [call.outcome for call in audit_store.rows] == ["applied", "failed"]


def test_approve_fork_audit_payloads_validate_against_the_registered_model(
    lifecycle_rig: LifecycleRig,
) -> None:
    _fork_of_approved_prior(lifecycle_rig, governed={"c1": "pol_old", "c2": "pol_old"})
    audit_store = lifecycle_rig.audit
    with pytest.raises(PolicyGovernanceConflictError):
        _approve_fork(lifecycle_rig, _third_party_takes_c2(lifecycle_rig), "pol_new")

    for call in audit_store.rows:
        PolicyTransitionDetails.model_validate(dict(call.details))


# --- `reject_policy` --------------------------------------
#
# Mirrors `approve_policy`'s own test list exactly (same
# test shapes), with the target status reversed to `"draft"` and the action
# `"policy.reject"` -- proving the gate stack (RBAC, `block_self_approval`,
# `require_status`) is genuinely shared with `approve_policy`, not
# reimplemented differently. No auto-deprecation test: `reject_policy` never
# calls `find_approved_prior` (that lookup is approve-only).


def test_policy_manager_non_owner_rejects_successfully_and_cascades_to_draft(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    result = reject_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.standard_ids == ("std_1",)
    assert lifecycle_rig.logged_groups == 1
    assert _status(lifecycle_rig, "pol_x") == "draft"
    assert len(audit_store.rows) == 1
    applied_call = audit_store.rows[0]
    assert applied_call.action == "policy.reject"
    assert applied_call.outcome == "applied"
    assert applied_call.details["from_status"] == "proposed"
    assert applied_call.details["to_status"] == "draft"


def test_owner_who_also_holds_policy_manager_self_rejection_is_blocked(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicySelfApprovalBlockedError):
        reject_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "self_approval_blocked"


def test_same_subject_different_issuer_rejecter_succeeds(lifecycle_rig: LifecycleRig) -> None:
    """Critical regression case: same `sub`, different `iss`, is a different person."""
    policy = _approvable_proposed(owner=_OWNER)
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER_OTHER_ISSUER, access_role=AccessRole.POLICY_MANAGER)

    result = reject_policy(
        actor=_OWNER_OTHER_ISSUER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=lifecycle_rig.gateway,
    )

    assert result.status == "draft"
    assert lifecycle_rig.logged_groups == 1
    assert len(audit_store.rows) == 1
    assert audit_store.rows[0].outcome == "applied"


def test_non_policy_manager_rejecter_is_rejected_with_access_denied(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()

    with pytest.raises(AccessDeniedError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert audit_store.rows == []


def test_reject_wrong_current_status_is_rejected_via_require_status(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed(status="draft")
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyInvalidStatusTransitionError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "invalid_status"


def test_reject_audit_write_precedes_graph_write(lifecycle_rig: LifecycleRig) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    reject_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=lifecycle_rig.gateway,
    )

    events = lifecycle_rig.events
    assert next(i for i, event in enumerate(events) if event.startswith("audit:")) < next(
        i for i, event in enumerate(events) if "append" in event
    )


def test_reject_graph_failure_after_audit_success_records_failed_event_and_raises(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("boom"))
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )

    assert [call.outcome for call in audit_store.rows] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.rows} == {"pol_x"}


def test_reject_nonexistent_policy_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    graph = _rig_graph(lifecycle_rig, {})
    audit_store = lifecycle_rig.audit
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyNotFoundError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_missing",
            graph=graph,
            audit_store=audit_store,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=lifecycle_rig.gateway,
        )


# --- `revert_policy_to_draft` ------------------------------
#
# Structurally distinct from `approve_policy`/`reject_policy`: owner-only, no
# `PolicyManager` RBAC gate at all. The non-owner test below deliberately
# grants the non-owner caller `PolicyManager` to prove this is genuinely
# owner-only, not role-gated.


def test_owner_reverts_own_proposed_policy_succeeds_and_cascades_to_draft(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit

    result = revert_policy_to_draft(
        actor=_OWNER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.standard_ids == ("std_1",)
    assert lifecycle_rig.logged_groups == 1
    assert _status(lifecycle_rig, "pol_x") == "draft"
    assert len(audit_store.rows) == 1
    applied_call = audit_store.rows[0]
    assert applied_call.action == "policy.revert"
    assert applied_call.outcome == "applied"
    assert applied_call.details["from_status"] == "proposed"
    assert applied_call.details["to_status"] == "draft"


def test_revert_non_owner_policy_manager_is_rejected_proving_owner_only_not_role_gated(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A `PolicyManager` who is not the owner is still rejected -- this action has no RBAC gate."""
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyDraftAccessDeniedError):
        revert_policy_to_draft(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "access_denied"


def test_revert_wrong_current_status_is_rejected_via_require_status(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed(status="draft")
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyInvalidStatusTransitionError):
        revert_policy_to_draft(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert len(audit_store.rows) == 1
    rejected_call = audit_store.rows[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "invalid_status"


def test_revert_audit_write_precedes_graph_write(lifecycle_rig: LifecycleRig) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    audit_store = lifecycle_rig.audit

    revert_policy_to_draft(
        actor=_OWNER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        submitter=lifecycle_rig.gateway,
    )

    events = lifecycle_rig.events
    assert next(i for i, event in enumerate(events) if event.startswith("audit:")) < next(
        i for i, event in enumerate(events) if "append" in event
    )


def test_revert_graph_failure_after_audit_success_records_failed_event_and_raises(
    lifecycle_rig: LifecycleRig,
) -> None:
    policy = _approvable_proposed()
    graph = _rig_graph(lifecycle_rig, {"pol_x": policy})
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("boom"))
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        revert_policy_to_draft(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )

    assert [call.outcome for call in audit_store.rows] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.rows} == {"pol_x"}


def test_revert_nonexistent_policy_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    graph = _rig_graph(lifecycle_rig, {})
    audit_store = lifecycle_rig.audit

    with pytest.raises(PolicyNotFoundError):
        revert_policy_to_draft(
            actor=_OWNER,
            policy_id="pol_missing",
            graph=graph,
            audit_store=audit_store,
            submitter=lifecycle_rig.gateway,
        )


# --- `update_policy_draft` ---


def _seed_rig(rig: LifecycleRig, policy: _PolicyFixture | None, **extra: object) -> PolicyReadGraph:
    """Seed `policy` (no children) into the rig; return the handle the service reads through."""
    if policy is not None:
        rig.seed_policy(
            policy.id,
            title=policy.title,
            status=policy.status,
            version=policy.version,
            owner=policy.owner,
            extra=extra,
            legacy_edges=False,
        )
    return PolicyReadGraph(rig.graph)


def test_update_policy_draft_applies_only_supplied_fields(lifecycle_rig: LifecycleRig) -> None:
    """Only the supplied fields are written, nothing else."""
    graph = _seed_rig(lifecycle_rig, _draft_policy(), description="old", scope_in="kept")
    store = FakeAccessRoleStore()

    result = update_policy_draft(
        actor=_OWNER,
        policy_id="pol_x",
        fields={"description": "updated description"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert result.updated_fields == ("description",)
    assert lifecycle_rig.logged_groups == 1
    node = lifecycle_rig.graph.nodes[("Policy", "pol_x")]
    assert node == {
        "title": "X",
        "status": "draft",
        "version": "1",
        "owner_subject": _OWNER[0],
        "owner_issuer": _OWNER[1],
        "description": "updated description",
        "scope_in": "kept",
    }


def test_update_policy_draft_clears_a_field_via_explicit_none(lifecycle_rig: LifecycleRig) -> None:
    graph = _seed_rig(lifecycle_rig, _draft_policy(), scope_out="to be cleared")
    store = FakeAccessRoleStore()

    result = update_policy_draft(
        actor=_OWNER,
        policy_id="pol_x",
        fields={"scope_out": None},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.updated_fields == ("scope_out",)
    assert lifecycle_rig.logged_groups == 1
    assert "scope_out" not in lifecycle_rig.graph.nodes[("Policy", "pol_x")]


def test_update_policy_draft_rejects_non_owner_non_elevated(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner without an elevated role is rejected."""
    graph = _seed_rig(lifecycle_rig, _draft_policy())
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_policy_draft(
            actor=_OTHER,
            policy_id="pol_x",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )
    assert lifecycle_rig.logged_groups == 0


def test_update_policy_draft_allows_system_owner_override(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner `SystemOwner` still succeeds."""
    graph = _seed_rig(lifecycle_rig, _draft_policy())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = update_policy_draft(
        actor=_OTHER,
        policy_id="pol_x",
        fields={"description": "edited by system owner"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert lifecycle_rig.logged_groups == 1


def test_update_policy_draft_rejects_non_draft_status(lifecycle_rig: LifecycleRig) -> None:
    """A target that is not `draft` is rejected."""
    policy = _PolicyFixture(id="pol_x", title="X", status="proposed", version="1", owner=_OWNER)
    graph = _seed_rig(lifecycle_rig, policy)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        update_policy_draft(
            actor=_OWNER,
            policy_id="pol_x",
            fields={"description": "too late"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert lifecycle_rig.logged_groups == 0


def test_update_policy_draft_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    """A missing target raises the not-found error."""
    graph = _seed_rig(lifecycle_rig, None)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyNotFoundError):
        update_policy_draft(
            actor=_OWNER,
            policy_id="pol_missing",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_update_policy_draft_graph_failure_raises_translated_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The 'graph unavailable' failure mode."""
    graph = _seed_rig(lifecycle_rig, _draft_policy())
    lifecycle_rig.store.fail_appends()
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        update_policy_draft(
            actor=_OWNER,
            policy_id="pol_x",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


# --- `add_standard_to_draft` / `update_standard_draft` / `add_control_to_draft` /
# --- `update_control_draft` -------------


def _seed_tree_rig(
    rig: LifecycleRig,
    *,
    owner: tuple[str, str] = _OWNER,
    policy_status: str = "draft",
    standard_status: str | None = "draft",
    control_status: str | None = "draft",
    policy: bool = True,
    standard: bool = True,
    control: bool = False,
) -> PolicyReadGraph:
    """Seed `pol_x` (+ `std-1`, + `ctrl-1`) into the rig; return the handle the service reads."""
    if policy:
        controls = (
            (SeedControl("ctrl-1", "Key Rotation Check", status=control_status),) if control else ()
        )
        standards = (
            (
                SeedStandard(
                    "std-1", "Encryption Standard", status=standard_status, controls=controls
                ),
            )
            if standard
            else ()
        )
        rig.seed_policy(
            "pol_x",
            title="X",
            status=policy_status,
            version="1",
            owner=owner,
            standards=standards,
            legacy_edges=False,
        )
    return PolicyReadGraph(rig.graph)


def test_add_standard_to_draft_creates_standard_with_deterministic_id(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The returned `standard_id` matches the id formula deterministically."""
    graph = _seed_tree_rig(lifecycle_rig, standard=False)
    store = FakeAccessRoleStore()

    result = add_standard_to_draft(
        actor=_OWNER,
        policy_id="pol_x",
        title="Encryption Standard",
        fields={},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert result.title == "Encryption Standard"
    assert result.status == "draft"
    assert result.standard_id == standard_id("pol_x", "Encryption Standard")
    assert lifecycle_rig.logged_groups == 1
    assert lifecycle_rig.graph.nodes[("Standard", result.standard_id)] == {
        "implementation_status": "draft",
        "title": "Encryption Standard",
        "status": "draft",
    }


def test_add_standard_to_draft_implementation_status_independent_of_governance_status(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Governance `status` is always `draft`, `implementation_status`
    defaults to `draft` too unless the caller supplied a different one.
    """
    graph = _seed_tree_rig(lifecycle_rig, standard=False)
    store = FakeAccessRoleStore()

    result = add_standard_to_draft(
        actor=_OWNER,
        policy_id="pol_x",
        title="Encryption Standard",
        fields={"implementation_status": "implemented"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.status == "draft"
    props = lifecycle_rig.graph.nodes[("Standard", result.standard_id)]
    assert props["status"] == "draft"
    assert props["implementation_status"] == "implemented"


def test_add_standard_to_draft_rejects_non_owner_non_elevated(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner without an elevated role is rejected."""
    graph = _seed_tree_rig(lifecycle_rig, standard=False)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        add_standard_to_draft(
            actor=_OTHER,
            policy_id="pol_x",
            title="Encryption Standard",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )
    assert lifecycle_rig.logged_groups == 0


def test_add_standard_to_draft_allows_system_owner_override(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner `SystemOwner` still succeeds."""
    graph = _seed_tree_rig(lifecycle_rig, standard=False)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = add_standard_to_draft(
        actor=_OTHER,
        policy_id="pol_x",
        title="Encryption Standard",
        fields={},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.policy_id == "pol_x"
    assert lifecycle_rig.logged_groups == 1


def test_add_standard_to_draft_rejects_non_draft_parent_status(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A parent Policy that is not `draft` is rejected."""
    graph = _seed_tree_rig(lifecycle_rig, policy_status="proposed", standard=False)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        add_standard_to_draft(
            actor=_OWNER,
            policy_id="pol_x",
            title="Encryption Standard",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert lifecycle_rig.logged_groups == 0


def test_add_standard_to_draft_raises_not_found_for_missing_parent_policy(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A missing parent Policy raises the not-found error."""
    graph = _seed_tree_rig(lifecycle_rig, policy=False)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyNotFoundError):
        add_standard_to_draft(
            actor=_OWNER,
            policy_id="pol_missing",
            title="Encryption Standard",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_add_standard_to_draft_graph_failure_raises_translated_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The 'graph unavailable' failure mode."""
    graph = _seed_tree_rig(lifecycle_rig, standard=False)
    lifecycle_rig.store.fail_appends()
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        add_standard_to_draft(
            actor=_OWNER,
            policy_id="pol_x",
            title="Encryption Standard",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


# --- `update_standard_draft` ---------------------------


def test_update_standard_draft_applies_only_supplied_fields(lifecycle_rig: LifecycleRig) -> None:
    """Only the supplied fields are written."""
    graph = _seed_tree_rig(lifecycle_rig)
    store = FakeAccessRoleStore()

    result = update_standard_draft(
        actor=_OWNER,
        standard_id="std-1",
        fields={"description": "updated description"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.standard_id == "std-1"
    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert lifecycle_rig.logged_groups == 1
    assert lifecycle_rig.graph.nodes[("Standard", "std-1")] == {
        "title": "Encryption Standard",
        "status": "draft",
        "description": "updated description",
    }


def test_update_standard_draft_rejects_non_owner_non_elevated(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner without an elevated role is rejected."""
    graph = _seed_tree_rig(lifecycle_rig)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_standard_draft(
            actor=_OTHER,
            standard_id="std-1",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )
    assert lifecycle_rig.logged_groups == 0


def test_update_standard_draft_ownership_is_transitive_from_parent_policy(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Proves the gate genuinely reads the parent-Policy traversal
    row, not some nonexistent Standard-level ownership field: a non-owner of
    the PARENT POLICY is rejected even though the Standard being mutated has
    no ownership field of its own.
    """
    graph = _seed_tree_rig(lifecycle_rig, owner=_OWNER)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_standard_draft(
            actor=_OTHER,
            standard_id="std-1",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_update_standard_draft_allows_system_owner_override(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner `SystemOwner` still succeeds."""
    graph = _seed_tree_rig(lifecycle_rig)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = update_standard_draft(
        actor=_OTHER,
        standard_id="std-1",
        fields={"description": "edited by system owner"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.standard_id == "std-1"
    assert lifecycle_rig.logged_groups == 1


def test_update_standard_draft_rejects_non_draft_status(lifecycle_rig: LifecycleRig) -> None:
    """The STANDARD's own status gates this, not the parent Policy's."""
    graph = _seed_tree_rig(lifecycle_rig, standard_status="proposed")
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        update_standard_draft(
            actor=_OWNER,
            standard_id="std-1",
            fields={"description": "too late"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert lifecycle_rig.logged_groups == 0


def test_update_standard_draft_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    """A missing target raises the not-found error."""
    graph = _seed_tree_rig(lifecycle_rig, policy=False)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyStandardNotFoundError):
        update_standard_draft(
            actor=_OWNER,
            standard_id="std-missing",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_update_standard_draft_graph_failure_raises_translated_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The 'graph unavailable' failure mode."""
    graph = _seed_tree_rig(lifecycle_rig)
    lifecycle_rig.store.fail_appends()
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        update_standard_draft(
            actor=_OWNER,
            standard_id="std-1",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_update_standard_draft_backfills_legacy_null_status_before_gating(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A legacy Standard minted before any
    backfilling call (e.g. via the internal-seed adapter) can still have a
    `NULL` own `status` even though its parent Policy is genuinely `draft`.
    Without the backfill-then-reread step (`_read_standard_with_parent_backfilled`),
    this call would be spuriously rejected with
    `PolicyInvalidStatusTransitionError` (current_status=None) even though
    the Standard is legitimately draft-eligible once backfilled.
    """
    graph = _seed_tree_rig(lifecycle_rig, standard_status=None)
    store = FakeAccessRoleStore()

    result = update_standard_draft(
        actor=_OWNER,
        standard_id="std-1",
        fields={"description": "now editable"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.standard_id == "std-1"
    assert lifecycle_rig.graph.nodes[("Standard", "std-1")]["status"] == "draft"
    assert lifecycle_rig.graph.nodes[("Standard", "std-1")]["description"] == "now editable"
    assert lifecycle_rig.logged_groups == 2  # the backfill group, then the edit


# --- `add_control_to_draft` ----------------------------


def test_add_control_to_draft_creates_control_with_deterministic_id(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The returned `control_id` matches the id formula deterministically."""
    graph = _seed_tree_rig(lifecycle_rig)
    store = FakeAccessRoleStore()

    result = add_control_to_draft(
        actor=_OWNER,
        standard_id="std-1",
        title="Key Rotation Check",
        control_type="automated",
        fields={},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.standard_id == "std-1"
    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.control_id == control_id("std-1", "Key Rotation Check")
    assert lifecycle_rig.logged_groups == 1
    assert lifecycle_rig.graph.nodes[("Control", result.control_id)] == {
        "implementation_status": "planned",
        "title": "Key Rotation Check",
        "type": "automated",
        "status": "draft",
    }


def test_add_control_to_draft_defaults_implementation_status_planned_not_draft(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Control's own `implementation_status` default is `"planned"`,
    the one deliberate divergence from `add_standard_to_draft`'s own `"draft"`
    default -- easy to copy-paste wrong.
    """
    graph = _seed_tree_rig(lifecycle_rig)
    store = FakeAccessRoleStore()

    result = add_control_to_draft(
        actor=_OWNER,
        standard_id="std-1",
        title="Key Rotation Check",
        control_type="manual",
        fields={},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.status == "draft"
    props = lifecycle_rig.graph.nodes[("Control", result.control_id)]
    assert props["status"] == "draft"
    assert props["implementation_status"] == "planned"


def test_add_control_to_draft_rejects_non_owner_non_elevated(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner without an elevated role is rejected."""
    graph = _seed_tree_rig(lifecycle_rig)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        add_control_to_draft(
            actor=_OTHER,
            standard_id="std-1",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )
    assert lifecycle_rig.logged_groups == 0


def test_add_control_to_draft_ownership_is_transitive_from_parent_policy(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Proves the gate genuinely reads the parent-Policy traversal
    row, not some nonexistent Standard-level ownership field -- mirrors
    `test_update_standard_draft_ownership_is_transitive_from_parent_policy`.
    """
    graph = _seed_tree_rig(lifecycle_rig, owner=_OWNER)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        add_control_to_draft(
            actor=_OTHER,
            standard_id="std-1",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_add_control_to_draft_allows_system_owner_override(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner `SystemOwner` still succeeds."""
    graph = _seed_tree_rig(lifecycle_rig)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = add_control_to_draft(
        actor=_OTHER,
        standard_id="std-1",
        title="Key Rotation Check",
        control_type="manual",
        fields={},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.standard_id == "std-1"
    assert lifecycle_rig.logged_groups == 1


def test_add_control_to_draft_rejects_non_draft_parent_status(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The STANDARD's own status gates this, not the parent Policy's."""
    graph = _seed_tree_rig(lifecycle_rig, standard_status="proposed")
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        add_control_to_draft(
            actor=_OWNER,
            standard_id="std-1",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert lifecycle_rig.logged_groups == 0


def test_add_control_to_draft_raises_not_found_for_missing_parent_standard(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A missing parent Standard raises the not-found error."""
    graph = _seed_tree_rig(lifecycle_rig, policy=False)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyStandardNotFoundError):
        add_control_to_draft(
            actor=_OWNER,
            standard_id="std-missing",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_add_control_to_draft_graph_failure_raises_translated_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The 'graph unavailable' failure mode."""
    graph = _seed_tree_rig(lifecycle_rig)
    lifecycle_rig.store.fail_appends()
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        add_control_to_draft(
            actor=_OWNER,
            standard_id="std-1",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_add_control_to_draft_backfills_legacy_null_status_before_gating(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A legacy Standard minted before any
    backfilling call can still have a `NULL` own `status` even though its
    parent Policy is genuinely `draft`. Without the backfill-then-reread step
    (`_read_standard_with_parent_backfilled`), this call would be spuriously
    rejected with `PolicyInvalidStatusTransitionError` (current_status=None)
    even though the Standard is legitimately draft-eligible once backfilled.
    """
    graph = _seed_tree_rig(lifecycle_rig, standard_status=None)
    store = FakeAccessRoleStore()

    result = add_control_to_draft(
        actor=_OWNER,
        standard_id="std-1",
        title="Key Rotation Check",
        control_type="manual",
        fields={},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.standard_id == "std-1"
    assert lifecycle_rig.graph.nodes[("Standard", "std-1")]["status"] == "draft"
    assert lifecycle_rig.logged_groups == 2  # the backfill group, then the add


# --- `update_control_draft` -----------------------------


def test_update_control_draft_applies_only_supplied_fields(lifecycle_rig: LifecycleRig) -> None:
    """Only the supplied fields are written."""
    graph = _seed_tree_rig(lifecycle_rig, control=True)
    store = FakeAccessRoleStore()

    result = update_control_draft(
        actor=_OWNER,
        control_id="ctrl-1",
        fields={"description": "updated description"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.control_id == "ctrl-1"
    assert result.standard_id == "std-1"
    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert lifecycle_rig.logged_groups == 1
    assert lifecycle_rig.graph.nodes[("Control", "ctrl-1")] == {
        "title": "Key Rotation Check",
        "type": "manual",
        "status": "draft",
        "description": "updated description",
    }


def test_update_control_draft_rejects_non_owner_non_elevated(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner without an elevated role is rejected."""
    graph = _seed_tree_rig(lifecycle_rig, control=True)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_control_draft(
            actor=_OTHER,
            control_id="ctrl-1",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )
    assert lifecycle_rig.logged_groups == 0


def test_update_control_draft_ownership_traverses_two_hops_not_one(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Proves `update_control_draft` genuinely walks `Policy -[:SUPPORTED_BY]->
    Standard -[:IMPLEMENTED_BY]-> Control`: neither the Control nor its parent
    Standard has an ownership field, only the ROOT Policy two hops up does, so
    the owner succeeds and a non-owner is still correctly rejected.
    """
    graph = _seed_tree_rig(lifecycle_rig, owner=_OWNER, control=True)
    store = FakeAccessRoleStore()

    result = update_control_draft(
        actor=_OWNER,
        control_id="ctrl-1",
        fields={"description": "edited"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.control_id == "ctrl-1"
    assert result.policy_id == "pol_x"
    assert result.standard_id == "std-1"

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_control_draft(
            actor=_OTHER,
            control_id="ctrl-1",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_update_control_draft_allows_system_owner_override(lifecycle_rig: LifecycleRig) -> None:
    """A non-owner `SystemOwner` still succeeds."""
    graph = _seed_tree_rig(lifecycle_rig, control=True)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = update_control_draft(
        actor=_OTHER,
        control_id="ctrl-1",
        fields={"description": "edited by system owner"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.control_id == "ctrl-1"
    assert lifecycle_rig.logged_groups == 1


def test_update_control_draft_rejects_non_draft_status(lifecycle_rig: LifecycleRig) -> None:
    """The CONTROL's own status gates this, not the root Policy's."""
    graph = _seed_tree_rig(lifecycle_rig, control=True, control_status="proposed")
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        update_control_draft(
            actor=_OWNER,
            control_id="ctrl-1",
            fields={"description": "too late"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert lifecycle_rig.logged_groups == 0


def test_update_control_draft_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    """A missing target raises the not-found error."""
    graph = _seed_tree_rig(lifecycle_rig, policy=False)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyControlNotFoundError):
        update_control_draft(
            actor=_OWNER,
            control_id="ctrl-missing",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_update_control_draft_graph_failure_raises_translated_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The 'graph unavailable' failure mode."""
    graph = _seed_tree_rig(lifecycle_rig, control=True)
    lifecycle_rig.store.fail_appends()
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        update_control_draft(
            actor=_OWNER,
            control_id="ctrl-1",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
            submitter=lifecycle_rig.gateway,
        )


def test_update_control_draft_backfills_legacy_null_status_before_gating(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Two-hop case: a legacy Control minted
    before any backfilling call can still have a `NULL` own `status` even
    though its root Policy is genuinely `draft`. Without the backfill-then-
    reread step (`_read_control_with_parent_backfilled`), this call would be
    spuriously rejected with `PolicyInvalidStatusTransitionError`
    (current_status=None) even though the Control is legitimately
    draft-eligible once backfilled.
    """
    graph = _seed_tree_rig(lifecycle_rig, control=True, control_status=None)
    store = FakeAccessRoleStore()

    result = update_control_draft(
        actor=_OWNER,
        control_id="ctrl-1",
        fields={"description": "now editable"},
        graph=graph,
        access_role_store=store,
        submitter=lifecycle_rig.gateway,
    )

    assert result.control_id == "ctrl-1"
    assert lifecycle_rig.graph.nodes[("Control", "ctrl-1")]["status"] == "draft"
    assert lifecycle_rig.logged_groups == 2  # the backfill group, then the edit


# --- `create_policy_draft`'s `supersedes_policy_id` fork --

_PRIOR_POLICY_ID = "pol_data_protection_policy_prior01"
_PRIOR_STANDARD_ID = "std_encryption_standard_prior01"
_PRIOR_CONTROL_ID = "ctrl_key_rotation_prior01"


def _seed_prior(rig: LifecycleRig, *, status: str = "approved", version: str = "3") -> None:
    """The approved prior tree of the fork tests, as the rig graph holds it.

    Both children still carry the SOURCE's own `"approved"` status, proving forked
    children come out `"draft"`, never copied from the source.
    """
    rig.seed_policy(
        _PRIOR_POLICY_ID,
        title=_TITLE,
        status=status,
        version=version,
        owner=_ACTOR,
        standards=(
            SeedStandard(
                _PRIOR_STANDARD_ID,
                "Encryption Standard",
                status="approved",
                extra={"procedure": "rotate keys quarterly"},
                controls=(
                    SeedControl(
                        _PRIOR_CONTROL_ID,
                        "Key Rotation",
                        status="approved",
                        control_type="automated",
                        extra={"evidence_ref": "https://example.com/evidence"},
                    ),
                ),
            ),
        ),
        legacy_edges=False,
    )


def test_supersede_fork_mints_new_policy_with_successor_version(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Version arithmetic, string-typed: prior `"5"` -> new `"6"`
    (a non-`"1"`-adjacent value, so this cannot pass by coincidence with the
    ordinary-draft default).
    """
    _seed_prior(lifecycle_rig, version="5")

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    assert result.version == "6"
    assert result.superseded_policy_id == _PRIOR_POLICY_ID
    assert lifecycle_rig.graph.nodes[("Policy", result.policy_id)]["version"] == "6"


def test_supersede_fork_writes_superseded_by_edge_prior_to_new(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_prior(lifecycle_rig)

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    edges = [key for key in lifecycle_rig.graph.edges if key[0] == "SUPERSEDED_BY"]
    assert len(edges) == 1
    assert edges[0][1:5] == ("Policy", _PRIOR_POLICY_ID, "Policy", result.policy_id)


def test_supersede_fork_forks_full_standard_control_content_not_just_titles(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_prior(lifecycle_rig)

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    nodes = lifecycle_rig.graph.nodes
    (forked_standard,) = result.standard_ids
    (forked_control,) = result.control_ids
    assert nodes[("Standard", forked_standard)]["procedure"] == "rotate keys quarterly"
    assert nodes[("Control", forked_control)]["evidence_ref"] == "https://example.com/evidence"


def test_supersede_fork_forked_children_are_draft_not_copied_from_approved_source(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Governance status is re-derived as `"draft"`, never copied
    from the source Standard/Control's own `"approved"` status.
    """
    _seed_prior(lifecycle_rig)

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    nodes = lifecycle_rig.graph.nodes
    assert nodes[("Standard", result.standard_ids[0])]["status"] == "draft"
    assert nodes[("Control", result.control_ids[0])]["status"] == "draft"


def test_supersede_fork_never_writes_to_prior_tree_nodes(lifecycle_rig: LifecycleRig) -> None:
    """The superseded Policy's own content, and its original
    Standard/Control children, are never mutated by the fork -- the prior tree's nodes are
    exactly as they were seeded, and the only prior id in the new group is the
    `SUPERSEDED_BY` edge's own SOURCE endpoint.
    """
    _seed_prior(lifecycle_rig)
    prior_ids = {_PRIOR_POLICY_ID, _PRIOR_STANDARD_ID, _PRIOR_CONTROL_ID}
    before = {key: dict(value) for key, value in lifecycle_rig.graph.nodes.items()}

    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    for key, properties in before.items():
        assert lifecycle_rig.graph.nodes[key] == properties
    (group,) = lifecycle_rig.store.groups
    node_ids = {e.identity for e in group.entries if e.content["op"] == "upsert_node"}
    assert not node_ids & prior_ids
    prior_edges = [e for e in group.entries if e.name == "SUPERSEDED_BY"]
    assert [e.identity.split("->")[0] for e in prior_edges] == [f"SUPERSEDED_BY:{_PRIOR_POLICY_ID}"]


def test_supersede_missing_prior_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    """A nonexistent `supersedes_policy_id` is rejected before any
    write or audit event.
    """
    with pytest.raises(PolicyNotFoundError):
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            supersedes_policy_id="pol_missing",
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    assert lifecycle_rig.store.groups == []
    assert lifecycle_rig.audit.rows == []


def test_supersede_non_approved_prior_raises_named_error(lifecycle_rig: LifecycleRig) -> None:
    """An existing-but-not-`approved` prior is rejected before any
    write or audit event, no fork attempted.
    """
    _seed_prior(lifecycle_rig, status="draft")
    groups_before = len(lifecycle_rig.store.groups)

    with pytest.raises(PolicySupersedePriorNotApprovedError) as exc_info:
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            supersedes_policy_id=_PRIOR_POLICY_ID,
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.policy_id == _PRIOR_POLICY_ID
    assert exc_info.value.actual_status == "draft"
    assert len(lifecycle_rig.store.groups) == groups_before
    assert lifecycle_rig.audit.rows == []


def test_supersede_records_audit_event_with_supersedes_policy_id(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The fork's `applied` `policy.create_draft` audit event's
    own `details.supersedes_policy_id` is asserted directly -- not just that
    no exception was raised.
    """
    _seed_prior(lifecycle_rig)

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    applied_rows = [row for row in lifecycle_rig.audit.rows if row.outcome == "applied"]
    assert len(applied_rows) == 1
    assert applied_rows[0].action == "policy.create_draft"
    assert applied_rows[0].resource_id == result.policy_id
    assert applied_rows[0].details["supersedes_policy_id"] == _PRIOR_POLICY_ID


def test_ordinary_create_policy_draft_still_has_null_supersedes_policy_id_in_audit_details(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Regression guard: the ordinary (non-fork) path's audit details still
    carry `supersedes_policy_id`, present-but-`None`.
    """
    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    assert lifecycle_rig.audit.rows[0].details.get("supersedes_policy_id") is None


# --- Fresh drafts claim Capabilities via `capability_ids` --------


def test_fresh_draft_with_capability_ids_issues_guarded_claim_and_audits_ids(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_capability("cap_a")
    lifecycle_rig.seed_capability("cap_b")

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        capability_ids=("cap_a", "cap_b"),
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    governed = {key[2] for key in lifecycle_rig.graph.edges if key[0] == "GOVERNED_BY"}
    assert governed == {"cap_a", "cap_b"}
    assert {key[4] for key in lifecycle_rig.graph.edges if key[0] == "GOVERNED_BY"} == {
        result.policy_id
    }
    assert len(lifecycle_rig.audit.rows) == 1
    assert lifecycle_rig.audit.rows[0].outcome == "applied"
    assert lifecycle_rig.audit.rows[0].details["capability_ids"] == ["cap_a", "cap_b"]
    events = lifecycle_rig.events
    assert events.index("audit:policy.create_draft:applied") < next(
        i for i, event in enumerate(events) if "append" in event
    )


def test_fresh_draft_with_duplicate_capability_ids_dedupes_preserving_order(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_capability("cap_a")
    lifecycle_rig.seed_capability("cap_b")

    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        capability_ids=("cap_b", "cap_a", "cap_b"),
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    assert lifecycle_rig.audit.rows[0].details["capability_ids"] == ["cap_b", "cap_a"]
    assert len([key for key in lifecycle_rig.graph.edges if key[0] == "GOVERNED_BY"]) == 2


def test_fresh_draft_without_capability_ids_reads_no_governors_and_audits_empty_ids(
    lifecycle_rig: LifecycleRig,
) -> None:
    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    assert lifecycle_rig.audit.rows[0].details["capability_ids"] == []
    assert not any("GOVERNED_BY" in key for key in lifecycle_rig.graph.edges)
    assert not any("RETURN cap.id, g.id" in q for q in lifecycle_rig.read.statements)


def test_missing_capability_id_raises_named_error_rejected_audit_and_zero_writes(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_capability("cap_a")

    with pytest.raises(PolicyCapabilityNotFoundError) as exc_info:
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            capability_ids=("cap_a", "cap_missing"),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.capability_ids == ("cap_missing",)
    assert "cap_missing" in str(exc_info.value)
    assert lifecycle_rig.store.groups == []
    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["rejected"]
    assert lifecycle_rig.audit.rows[0].details["reason_code"] == "capability_not_found"
    assert lifecycle_rig.audit.rows[0].details["capability_ids"] == []


def test_already_governed_capability_raises_named_error_rejected_audit_and_zero_writes(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_capability("cap_a")
    lifecycle_rig.seed_capability("cap_b")
    lifecycle_rig.seed_policy("pol_other", owner=_ACTOR, legacy_edges=False)
    lifecycle_rig.seed_governs("cap_b", "pol_other", legacy=False)
    groups_before = len(lifecycle_rig.store.groups)

    with pytest.raises(PolicyCapabilityAlreadyGovernedError) as exc_info:
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            capability_ids=("cap_a", "cap_b"),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    assert exc_info.value.capability_ids == ("cap_b",)
    assert "cap_b" in str(exc_info.value)
    assert len(lifecycle_rig.store.groups) == groups_before
    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["rejected"]
    assert lifecycle_rig.audit.rows[0].details["reason_code"] == "capability_already_governed"


def test_lost_claim_race_records_failed_audit_with_empty_ids_and_writes_no_children(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_capability("cap_a")
    lifecycle_rig.hooked.before_submit(lifecycle_rig.append_unrelated_group)

    with pytest.raises(PolicyCapabilityAlreadyGovernedError):
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            standards=(StandardDraftInput(title="Encryption Standard"),),
            capability_ids=("cap_a",),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.hooked,
        )

    # Only the unrelated group is in the log: no Policy, edge, Standard or Control of the claim.
    assert {label for (label, _) in lifecycle_rig.graph.nodes} == {"Capability"}
    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]
    assert lifecycle_rig.audit.rows[0].details["capability_ids"] == ["cap_a"]
    assert lifecycle_rig.audit.rows[1].details["capability_ids"] == []
    assert lifecycle_rig.audit.rows[1].details["reason_code"] == "capability_already_governed"


def test_fork_ignores_capability_ids_reads_no_governors_and_writes_no_governed_by(
    lifecycle_rig: LifecycleRig,
) -> None:
    """`capability_ids` is silently ignored when `supersedes_policy_id` is set."""
    _seed_prior(lifecycle_rig)

    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        capability_ids=("cap_a",),
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    assert not any(key[0] == "GOVERNED_BY" for key in lifecycle_rig.graph.edges)
    assert lifecycle_rig.audit.rows[0].details["capability_ids"] == []


def test_every_capability_claim_audit_payload_validates_against_the_registered_model(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Rejected, applied and failed `policy.create_draft` payloads are valid details.

    A `failed` event for a lost race carries a `reason_code` (rejected AND
    failed may carry one) and `capability_ids=()`.
    """
    lifecycle_rig.seed_capability("cap_a")
    lifecycle_rig.hooked.before_submit(lifecycle_rig.append_unrelated_group)
    with pytest.raises(PolicyCapabilityAlreadyGovernedError):
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            capability_ids=("cap_a",),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.hooked,
        )
    with pytest.raises(PolicyCapabilityNotFoundError):
        create_policy_draft(
            actor=_ACTOR,
            title="Other Policy",
            capability_ids=("cap_missing",),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    rows = lifecycle_rig.audit.rows
    parsed = [PolicyCreateDraftDetails.model_validate(row.details) for row in rows]

    assert [row.outcome for row in rows] == ["applied", "failed", "rejected"]
    assert parsed[0].capability_ids == ("cap_a",)
    assert parsed[1].reason_code == "capability_already_governed"
    assert parsed[1].capability_ids == ()
    assert parsed[2].reason_code == "capability_not_found"
