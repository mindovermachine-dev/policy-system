"""The gateway-backed lifecycle writes build the graph the frozen legacy writer built (issue #211).

Each scenario seeds a fixture Policy tree as a pre-gateway writer left it (identity-less edges),
runs the FROZEN legacy statements (`_legacy_reference`, against its interpreter) on a copy, runs
the real service functions over the gateway rig on the other, and compares the two graphs by
`canonical_digest` (AC-BI-002, AC-BI-010). "Modulo edge identity": the legacy graph's edges are
stamped by the same `{type}:{source}->{target}` formula before the comparison, as the gateway
stamps its own.

Met modulo edge identity; the live digest and live interpreter agreement are not verifiable here
(no FalkorDB in this sandbox): the `falkordb_live` anchor stays the live proof.

Families covered: draft Policy edit (S3), draft Standard/Control edit and add (S4),
governance backfill (S5), create without a claim (S6), claim create (S7), propose/reject/revert
cascades (S8), approve with fork repoint and auto-deprecate (S9). The full matrix (S11) adds the
chained transitions (create -> propose -> approve, with and without a fork and governed
Capabilities), a composite scenario that reaches every family in one run, identity-less legacy
edges ahead of the first group, and idempotency (a second pass changes and logs nothing).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import (
    LifecycleRig,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
)
from policy_lifecycle._legacy_reference import (
    LegacyRecordingGraph,
    legacy_add_control_to_standard,
    legacy_add_standard_to_policy,
    legacy_approve_fork_repoint,
    legacy_backfill_governance_status,
    legacy_cascade_status,
    legacy_create_policy_draft,
    legacy_update_control_fields,
    legacy_update_policy_fields,
    legacy_update_standard_fields,
)
from ps_service.authz.models import AccessRole
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.domain_mapper.identity import policy_id as compute_policy_id
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.policy_lifecycle.errors import PolicyCapabilityNotFoundError
from ps_service.policy_lifecycle.graph_writer import ControlDraft, StandardDraft
from ps_service.policy_lifecycle.service import (
    ControlDraftInput,
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
    from collections.abc import Callable, Mapping
    from typing import Literal

_OWNER = ("alice", "https://issuer.example")
_POLICY = "pol_x"


def _seed_tree(rig: LifecycleRig) -> None:
    rig.seed_policy(
        _POLICY,
        owner=_OWNER,
        extra={"description": "old", "scope_in": "in", "scope_out": "out"},
        standards=(
            SeedStandard(
                "std_1", "S1", controls=(SeedControl("ctl_1", "c1"), SeedControl("ctl_2", "c2"))
            ),
            SeedStandard("std_2", "S2"),
        ),
        legacy_edges=True,
    )


def _legacy_digest(rig: LifecycleRig, run: Callable[[LegacyRecordingGraph], object]) -> str:
    legacy = LegacyRecordingGraph.copy_of(rig.graph)
    run(legacy)
    return canonical_digest(legacy.as_gateway_graph())


@pytest.mark.parametrize(
    "fields",
    [
        {"description": "new"},
        {"scope_out": None},
        {"description": "new", "scope_out": None, "review_cadence": "annually"},
        {"normative_commitments": None},  # clearing a property the Policy never had
        {"description": "old"},  # a patch that changes nothing
    ],
    ids=["set", "clear", "set-and-clear", "clear-absent", "no-change"],
)
def test_a_policy_edit_leaves_the_graph_the_frozen_legacy_writer_left(
    lifecycle_rig: LifecycleRig, fields: Mapping[str, object]
) -> None:
    _seed_tree(lifecycle_rig)
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_update_policy_fields(legacy, policy_id=_POLICY, properties=fields),
    )

    update_policy_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        fields=fields,
        graph=PolicyReadGraph(lifecycle_rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=lifecycle_rig.gateway,
    )

    assert canonical_digest(lifecycle_rig.graph) == expected


_EDITS: list[dict[str, object]] = [
    {"description": "new"},
    {"procedure": None},
    {"description": "new", "procedure": None, "verification_notes": "checked"},
    {"verification_notes": None},  # clearing a property the node never had
    {"description": "old"},  # a patch that changes nothing
]
_EDIT_IDS = ["set", "clear", "set-and-clear", "clear-absent", "no-change"]
_CONTROL_EDITS: list[dict[str, object]] = [
    {("evidence_ref" if key == "procedure" else key): value for key, value in fields.items()}
    for fields in _EDITS
]


def _seed_children(rig: LifecycleRig) -> None:
    rig.seed_policy(
        _POLICY,
        owner=_OWNER,
        standards=(
            SeedStandard(
                "std_1",
                "S1",
                extra={"description": "old", "procedure": "proc"},
                controls=(
                    SeedControl("ctl_1", "c1", extra={"description": "old", "evidence_ref": "ref"}),
                ),
            ),
        ),
        legacy_edges=True,
    )


@pytest.mark.parametrize("fields", _EDITS, ids=_EDIT_IDS)
def test_a_standard_edit_leaves_the_graph_the_frozen_legacy_writer_left(
    lifecycle_rig: LifecycleRig, fields: Mapping[str, object]
) -> None:
    _seed_children(lifecycle_rig)
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_update_standard_fields(
            legacy, standard_id="std_1", properties=fields
        ),
    )

    update_standard_draft(
        actor=_OWNER,
        standard_id="std_1",
        fields=fields,
        graph=PolicyReadGraph(lifecycle_rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=lifecycle_rig.gateway,
    )

    assert canonical_digest(lifecycle_rig.graph) == expected


@pytest.mark.parametrize("fields", _CONTROL_EDITS, ids=_EDIT_IDS)
def test_a_control_edit_leaves_the_graph_the_frozen_legacy_writer_left(
    lifecycle_rig: LifecycleRig, fields: Mapping[str, object]
) -> None:
    _seed_children(lifecycle_rig)
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_update_control_fields(legacy, control_id="ctl_1", properties=fields),
    )

    update_control_draft(
        actor=_OWNER,
        control_id="ctl_1",
        fields=fields,
        graph=PolicyReadGraph(lifecycle_rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=lifecycle_rig.gateway,
    )

    assert canonical_digest(lifecycle_rig.graph) == expected


@pytest.mark.parametrize(
    "fields",
    [{}, {"implementation_status": "implemented"}, {"procedure": None, "description": "d"}],
    ids=["bare", "implementation-status", "none-valued-key"],
)
def test_adding_a_standard_leaves_the_graph_the_frozen_legacy_writer_left(
    lifecycle_rig: LifecycleRig, fields: Mapping[str, object]
) -> None:
    _seed_children(lifecycle_rig)
    new_id = standard_id(_POLICY, "Backups")
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_add_standard_to_policy(
            legacy, policy_id=_POLICY, standard_id=new_id, title="Backups", extra_properties=fields
        ),
    )

    add_standard_to_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        title="Backups",
        fields=fields,
        graph=PolicyReadGraph(lifecycle_rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=lifecycle_rig.gateway,
    )

    assert canonical_digest(lifecycle_rig.graph) == expected


@pytest.mark.parametrize(
    ("control_type", "fields"),
    [
        ("manual", {}),
        ("automated", {"implementation_status": "reviewed"}),
        ("manual", {"procedure": None}),
    ],
    ids=["bare", "implementation-status", "none-valued-key"],
)
def test_adding_a_control_leaves_the_graph_the_frozen_legacy_writer_left(
    lifecycle_rig: LifecycleRig,
    control_type: Literal["automated", "manual"],
    fields: Mapping[str, object],
) -> None:
    _seed_children(lifecycle_rig)
    new_id = control_id("std_1", "Restore test")
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_add_control_to_standard(
            legacy,
            standard_id="std_1",
            control_id=new_id,
            title="Restore test",
            control_type=control_type,
            extra_properties=fields,
        ),
    )

    add_control_to_draft(
        actor=_OWNER,
        standard_id="std_1",
        title="Restore test",
        control_type=control_type,
        fields=fields,
        graph=PolicyReadGraph(lifecycle_rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=lifecycle_rig.gateway,
    )

    assert canonical_digest(lifecycle_rig.graph) == expected


def _seed_for_backfill(rig: LifecycleRig, case: str) -> None:
    if case == "full-legacy-tree":
        rig.seed_policy(
            _POLICY,
            status="proposed",
            version=None,
            owner=_OWNER,
            standards=(
                SeedStandard(
                    "std_1",
                    "S1",
                    status=None,
                    controls=(
                        SeedControl("ctl_1", "c1", status=None),
                        SeedControl("ctl_2", "c2", status=None),
                    ),
                ),
                SeedStandard("std_2", "S2", status=None),
            ),
            legacy_edges=True,
        )
    elif case == "policy-without-standards":
        rig.seed_policy(_POLICY, version=None, owner=_OWNER, legacy_edges=True)
    elif case == "mixed-statuses":
        rig.seed_policy(
            _POLICY,
            status="approved",
            owner=_OWNER,
            standards=(
                SeedStandard(
                    "std_1",
                    "S1",
                    status="draft",
                    controls=(
                        SeedControl("ctl_1", "c1", status=None),
                        SeedControl("ctl_2", "c2", status="draft"),
                    ),
                ),
                SeedStandard("std_2", "S2", status=None),
            ),
            legacy_edges=True,
        )
    else:
        rig.seed_policy(
            _POLICY,
            owner=_OWNER,
            standards=(SeedStandard("std_1", "S1", controls=(SeedControl("ctl_1", "c1"),)),),
            # Nothing to fill logs nothing, not even a stamp (AC-BI-006), so the edges stay as
            # seeded: identified ones keep this case comparable with the legacy digest.
            legacy_edges=False,
        )


@pytest.mark.parametrize(
    "case",
    ["full-legacy-tree", "policy-without-standards", "mixed-statuses", "already-backfilled"],
)
def test_a_backfill_leaves_the_graph_the_frozen_legacy_backfill_left(
    lifecycle_rig: LifecycleRig, case: str
) -> None:
    _seed_for_backfill(lifecycle_rig, case)
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_backfill_governance_status(legacy, _POLICY),
    )

    get_policy(
        actor=_OWNER,
        policy_id=_POLICY,
        graph=lifecycle_rig.read,
        access_role_store=FakeAccessRoleStore(),
        submitter_source=lambda: lifecycle_rig.gateway,
    )

    assert canonical_digest(lifecycle_rig.graph) == expected


_CREATE_TITLE = "Data Protection Policy"


def _create(
    rig: LifecycleRig,
    *,
    standards: tuple[StandardDraftInput, ...] = (),
    supersedes_policy_id: str | None = None,
) -> None:
    create_policy_draft(
        actor=_OWNER,
        title=_CREATE_TITLE,
        standards=standards,
        supersedes_policy_id=supersedes_policy_id,
        graph=rig.read,
        audit_store=rig.audit,
        submitter=rig.gateway,
    )


def _drafts(
    new_policy_id: str, standards: tuple[StandardDraftInput, ...]
) -> tuple[StandardDraft, ...]:
    drafts: list[StandardDraft] = []
    for standard in standards:
        std_id = standard_id(new_policy_id, standard.title)
        controls = tuple(
            ControlDraft(
                id=control_id(std_id, control.title),
                title=control.title,
                control_type=control.control_type,
            )
            for control in standard.controls
        )
        drafts.append(StandardDraft(id=std_id, title=standard.title, controls=controls))
    return tuple(drafts)


@pytest.mark.parametrize(
    "standards",
    [
        (),
        (
            StandardDraftInput(
                title="Encryption",
                controls=(ControlDraftInput(title="Rotate keys", control_type="automated"),),
            ),
            StandardDraftInput(title="Backups"),
        ),
    ],
    ids=["empty-tree", "full-tree"],
)
def test_creating_a_policy_leaves_the_graph_the_frozen_legacy_writer_left(
    lifecycle_rig: LifecycleRig, standards: tuple[StandardDraftInput, ...]
) -> None:
    new_id = compute_policy_id(_CREATE_TITLE)
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_create_policy_draft(
            legacy,
            policy_id=new_id,
            title=_CREATE_TITLE,
            owner=_OWNER,
            standards=_drafts(new_id, standards),
        ),
    )

    _create(lifecycle_rig, standards=standards)

    assert canonical_digest(lifecycle_rig.graph) == expected


def test_forking_an_approved_policy_leaves_the_graph_the_frozen_legacy_writer_left(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy(
        "pol_prior",
        title=_CREATE_TITLE,
        status="approved",
        version="3",
        owner=_OWNER,
        standards=(
            SeedStandard(
                "std_prior",
                "Encryption",
                status="approved",
                extra={"procedure": "rotate quarterly"},
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
        legacy_edges=True,
    )
    new_id = compute_policy_id(_CREATE_TITLE, prior_policy_id="pol_prior")
    new_std = standard_id(new_id, "Encryption")
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_create_policy_draft(
            legacy,
            policy_id=new_id,
            title=_CREATE_TITLE,
            owner=_OWNER,
            supersedes_policy_id="pol_prior",
            version="4",
            standards=(
                StandardDraft(
                    id=new_std,
                    title="Encryption",
                    extra_properties={"procedure": "rotate quarterly"},
                    controls=(
                        ControlDraft(
                            id=control_id(new_std, "Rotate keys"),
                            title="Rotate keys",
                            control_type="automated",
                            extra_properties={"evidence_ref": "https://example.com/e"},
                        ),
                    ),
                ),
            ),
        ),
    )

    _create(lifecycle_rig, supersedes_policy_id="pol_prior")

    assert canonical_digest(lifecycle_rig.graph) == expected


@pytest.mark.parametrize("merged", [False, True], ids=["two-ungoverned-capabilities", "one-merged"])
def test_a_claim_leaves_the_graph_the_frozen_legacy_writer_left(
    lifecycle_rig: LifecycleRig, *, merged: bool
) -> None:
    lifecycle_rig.seed_capability("cap_a")
    lifecycle_rig.seed_capability("cap_b", status="merged" if merged else None)
    lifecycle_rig.seed_policy("pol_other", owner=_OWNER, standards=(SeedStandard("s", "S"),))
    new_id = compute_policy_id(_CREATE_TITLE)
    claimed: list[bool] = []
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: claimed.append(
            legacy_create_policy_draft(
                legacy,
                policy_id=new_id,
                title=_CREATE_TITLE,
                owner=_OWNER,
                capability_ids=("cap_a", "cap_b"),
            )
        ),
    )
    assert claimed == [not merged]

    def _claim() -> None:
        create_policy_draft(
            actor=_OWNER,
            title=_CREATE_TITLE,
            capability_ids=("cap_a", "cap_b"),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
        )

    if merged:
        with pytest.raises(PolicyCapabilityNotFoundError):
            _claim()
        # The legacy statement matched nothing; the pre-read refused earlier: both wrote nothing.
        assert lifecycle_rig.logged_groups == 0
    else:
        _claim()
        assert canonical_digest(lifecycle_rig.graph) == expected


_MANAGER = ("carol", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")


def _seed_status_tree(rig: LifecycleRig, policy_id: str, status: str) -> None:
    rig.seed_policy(
        policy_id,
        owner=_OWNER,
        status=status,
        standards=(
            SeedStandard(
                f"{policy_id}_s1",
                "S1",
                status=status,
                controls=(
                    SeedControl(f"{policy_id}_c1", "c1", status=status),
                    SeedControl(f"{policy_id}_c2", "c2", status=status),
                ),
            ),
            SeedStandard(f"{policy_id}_s2", "S2", status=status),
        ),
        legacy_edges=True,
    )


def _managers() -> FakeAccessRoleStore:
    roles = FakeAccessRoleStore()
    roles.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)
    return roles


@pytest.mark.parametrize("action", ["propose", "reject", "revert"])
def test_a_cascade_leaves_the_graph_the_frozen_legacy_cascade_left(
    lifecycle_rig: LifecycleRig, action: str
) -> None:
    source, target = ("draft", "proposed") if action == "propose" else ("proposed", "draft")
    _seed_status_tree(lifecycle_rig, _POLICY, source)
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_cascade_status(legacy, policy_id=_POLICY, target_status=target),
    )
    graph, audit, submitter = lifecycle_rig.read, lifecycle_rig.audit, lifecycle_rig.gateway

    if action == "propose":
        propose_policy(
            actor=_OWNER, policy_id=_POLICY, graph=graph, audit_store=audit, submitter=submitter
        )
    elif action == "reject":
        reject_policy(
            actor=_MANAGER,
            policy_id=_POLICY,
            graph=graph,
            audit_store=audit,
            access_role_store=_managers(),
            submitter=submitter,
        )
    else:
        revert_policy_to_draft(
            actor=_OWNER, policy_id=_POLICY, graph=graph, audit_store=audit, submitter=submitter
        )

    assert canonical_digest(lifecycle_rig.graph) == expected


@pytest.mark.parametrize(
    "governed", [("cap_a", "cap_b"), ()], ids=["fork-with-two-capabilities", "fork-with-none"]
)
def test_approving_a_fork_leaves_the_graph_the_frozen_legacy_repoint_and_cascades_left(
    lifecycle_rig: LifecycleRig, governed: tuple[str, ...]
) -> None:
    _seed_status_tree(lifecycle_rig, "pol_old", "approved")
    _seed_status_tree(lifecycle_rig, "pol_new", "proposed")
    lifecycle_rig.seed_supersedes("pol_old", "pol_new")
    for capability_id in governed:
        lifecycle_rig.seed_capability(capability_id)
        lifecycle_rig.seed_governs(capability_id, "pol_old")

    def _legacy(legacy: LegacyRecordingGraph) -> None:
        if governed:
            moved = legacy_approve_fork_repoint(
                legacy,
                policy_id="pol_new",
                prior_id="pol_old",
                capability_ids=governed,
                target_status="approved",
            )
            assert moved is True
        else:
            legacy_cascade_status(legacy, policy_id="pol_new", target_status="approved")
        legacy_cascade_status(legacy, policy_id="pol_old", target_status="deprecated")

    expected = _legacy_digest(lifecycle_rig, _legacy)

    approve_policy(
        actor=_MANAGER,
        policy_id="pol_new",
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        access_role_store=_managers(),
        submitter=lifecycle_rig.gateway,
    )

    assert canonical_digest(lifecycle_rig.graph) == expected


def test_approving_a_plain_policy_leaves_the_graph_the_frozen_legacy_cascade_left(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_status_tree(lifecycle_rig, _POLICY, "proposed")
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: legacy_cascade_status(legacy, policy_id=_POLICY, target_status="approved"),
    )

    approve_policy(
        actor=_MANAGER,
        policy_id=_POLICY,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        access_role_store=_managers(),
        submitter=lifecycle_rig.gateway,
    )

    assert canonical_digest(lifecycle_rig.graph) == expected


# -- S11: the full matrix --------------------------------------------------------------------------

_FULL_TREE = (
    StandardDraftInput(
        title="Encryption",
        controls=(ControlDraftInput(title="Rotate keys", control_type="automated"),),
    ),
    StandardDraftInput(title="Backups"),
)


def _approve(rig: LifecycleRig, policy_id: str) -> None:
    approve_policy(
        actor=_MANAGER,
        policy_id=policy_id,
        graph=rig.read,
        audit_store=rig.audit,
        access_role_store=_managers(),
        submitter=rig.gateway,
    )


def _propose(rig: LifecycleRig, policy_id: str) -> None:
    propose_policy(
        actor=_OWNER,
        policy_id=policy_id,
        graph=rig.read,
        audit_store=rig.audit,
        submitter=rig.gateway,
    )


def test_create_propose_approve_leaves_the_graph_the_frozen_legacy_chain_left(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-002: a non-fork Policy through create, propose and approve."""
    new_id = compute_policy_id(_CREATE_TITLE)
    expected = _legacy_digest(
        lifecycle_rig,
        lambda legacy: (
            legacy_create_policy_draft(
                legacy,
                policy_id=new_id,
                title=_CREATE_TITLE,
                owner=_OWNER,
                standards=_drafts(new_id, _FULL_TREE),
            ),
            legacy_cascade_status(legacy, policy_id=new_id, target_status="proposed"),
            legacy_cascade_status(legacy, policy_id=new_id, target_status="approved"),
        ),
    )

    _create(lifecycle_rig, standards=_FULL_TREE)
    _propose(lifecycle_rig, new_id)
    _approve(lifecycle_rig, new_id)

    assert canonical_digest(lifecycle_rig.graph) == expected


def _seed_governed_prior(rig: LifecycleRig) -> None:
    rig.seed_policy(
        "pol_prior",
        title=_CREATE_TITLE,
        status="approved",
        version="3",
        owner=_OWNER,
        standards=(
            SeedStandard(
                "std_prior",
                "Encryption",
                status="approved",
                extra={"procedure": "rotate quarterly"},
                controls=(SeedControl("ctl_prior", "Rotate keys", status="approved"),),
            ),
        ),
        legacy_edges=True,
    )
    for capability_id in ("cap_a", "cap_b"):
        rig.seed_capability(capability_id)
        rig.seed_governs(capability_id, "pol_prior")


def test_a_fork_lifecycle_with_governed_capabilities_leaves_the_graph_the_legacy_chain_left(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-002: fork create, propose, approve (repoint) and auto-deprecate of the prior."""
    _seed_governed_prior(lifecycle_rig)
    new_id = compute_policy_id(_CREATE_TITLE, prior_policy_id="pol_prior")
    new_std = standard_id(new_id, "Encryption")

    def _legacy(legacy: LegacyRecordingGraph) -> None:
        legacy_create_policy_draft(
            legacy,
            policy_id=new_id,
            title=_CREATE_TITLE,
            owner=_OWNER,
            supersedes_policy_id="pol_prior",
            version="4",
            standards=(
                StandardDraft(
                    id=new_std,
                    title="Encryption",
                    extra_properties={"procedure": "rotate quarterly"},
                    controls=(
                        ControlDraft(
                            id=control_id(new_std, "Rotate keys"),
                            title="Rotate keys",
                            control_type="manual",
                        ),
                    ),
                ),
            ),
        )
        legacy_cascade_status(legacy, policy_id=new_id, target_status="proposed")
        moved = legacy_approve_fork_repoint(
            legacy,
            policy_id=new_id,
            prior_id="pol_prior",
            capability_ids=("cap_a", "cap_b"),
            target_status="approved",
        )
        assert moved is True
        legacy_cascade_status(legacy, policy_id="pol_prior", target_status="deprecated")

    expected = _legacy_digest(lifecycle_rig, _legacy)

    _create(lifecycle_rig, supersedes_policy_id="pol_prior")
    _propose(lifecycle_rig, new_id)
    _approve(lifecycle_rig, new_id)

    assert canonical_digest(lifecycle_rig.graph) == expected
    governed = {key[2] for key in lifecycle_rig.graph.edges if key[0] == "GOVERNED_BY"}
    assert {key[4] for key in lifecycle_rig.graph.edges if key[0] == "GOVERNED_BY"} == {new_id}
    assert governed == {"cap_a", "cap_b"}


def _composite_service_run(rig: LifecycleRig, new_id: str, logging_std: str) -> None:
    """Every writer family in one run: create, edits, adds, cascades, reject, revert, approve."""
    graph = PolicyReadGraph(rig.graph)
    roles = FakeAccessRoleStore()
    _create(rig, standards=_FULL_TREE)
    update_policy_draft(
        actor=_OWNER,
        policy_id=new_id,
        fields={"description": "d", "scope_out": None},
        graph=graph,
        access_role_store=roles,
        submitter=rig.gateway,
    )
    add_standard_to_draft(
        actor=_OWNER,
        policy_id=new_id,
        title="Logging",
        fields={"procedure": "p"},
        graph=graph,
        access_role_store=roles,
        submitter=rig.gateway,
    )
    add_control_to_draft(
        actor=_OWNER,
        standard_id=logging_std,
        title="Review logs",
        control_type="manual",
        fields={},
        graph=graph,
        access_role_store=roles,
        submitter=rig.gateway,
    )
    update_standard_draft(
        actor=_OWNER,
        standard_id=logging_std,
        fields={"procedure": None, "description": "s"},
        graph=graph,
        access_role_store=roles,
        submitter=rig.gateway,
    )
    update_control_draft(
        actor=_OWNER,
        control_id=control_id(logging_std, "Review logs"),
        fields={"evidence_ref": "ref"},
        graph=graph,
        access_role_store=roles,
        submitter=rig.gateway,
    )
    _propose(rig, new_id)
    reject_policy(
        actor=_MANAGER,
        policy_id=new_id,
        graph=rig.read,
        audit_store=rig.audit,
        access_role_store=_managers(),
        submitter=rig.gateway,
    )
    _propose(rig, new_id)
    revert_policy_to_draft(
        actor=_OWNER,
        policy_id=new_id,
        graph=rig.read,
        audit_store=rig.audit,
        submitter=rig.gateway,
    )
    _propose(rig, new_id)
    _approve(rig, new_id)
    get_policy(
        actor=_OWNER,
        policy_id=new_id,
        graph=rig.read,
        access_role_store=roles,
        submitter_source=lambda: rig.gateway,
    )


def test_the_composite_scenario_reaches_every_family_and_leaves_the_legacy_graph(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-002, AC-BI-010: create, edit, add, cascade, reject, revert, approve in one run."""
    new_id = compute_policy_id(_CREATE_TITLE)
    logging_std = standard_id(new_id, "Logging")
    logging_ctl = control_id(logging_std, "Review logs")

    def _legacy(legacy: LegacyRecordingGraph) -> None:
        legacy_create_policy_draft(
            legacy,
            policy_id=new_id,
            title=_CREATE_TITLE,
            owner=_OWNER,
            standards=_drafts(new_id, _FULL_TREE),
        )
        legacy_update_policy_fields(
            legacy, policy_id=new_id, properties={"description": "d", "scope_out": None}
        )
        legacy_add_standard_to_policy(
            legacy,
            policy_id=new_id,
            standard_id=logging_std,
            title="Logging",
            extra_properties={"procedure": "p"},
        )
        legacy_add_control_to_standard(
            legacy,
            standard_id=logging_std,
            control_id=logging_ctl,
            title="Review logs",
            control_type="manual",
            extra_properties={},
        )
        legacy_update_standard_fields(
            legacy, standard_id=logging_std, properties={"procedure": None, "description": "s"}
        )
        legacy_update_control_fields(
            legacy, control_id=logging_ctl, properties={"evidence_ref": "ref"}
        )
        for status in ("proposed", "draft", "proposed", "draft", "proposed", "approved"):
            legacy_cascade_status(legacy, policy_id=new_id, target_status=status)

    expected = _legacy_digest(lifecycle_rig, _legacy)

    _composite_service_run(lifecycle_rig, new_id, logging_std)

    assert canonical_digest(lifecycle_rig.graph) == expected


@pytest.mark.parametrize("family", ["edit", "add-standard", "backfill"])
def test_a_second_pass_changes_nothing_and_logs_nothing(
    lifecycle_rig: LifecycleRig, family: str
) -> None:
    """Idempotency: the second run of a no-op-able write leaves the digest and the log alone."""
    roles = FakeAccessRoleStore()
    if family == "backfill":
        _seed_for_backfill(lifecycle_rig, "full-legacy-tree")
    else:
        _seed_tree(lifecycle_rig)

    def _run() -> None:
        graph = PolicyReadGraph(lifecycle_rig.graph)
        if family == "edit":
            update_policy_draft(
                actor=_OWNER,
                policy_id=_POLICY,
                fields={"description": "new", "scope_out": None},
                graph=graph,
                access_role_store=roles,
                submitter=lifecycle_rig.gateway,
            )
        elif family == "add-standard":
            add_standard_to_draft(
                actor=_OWNER,
                policy_id=_POLICY,
                title="Backups",
                fields={},
                graph=graph,
                access_role_store=roles,
                submitter=lifecycle_rig.gateway,
            )
        else:
            get_policy(
                actor=_OWNER,
                policy_id=_POLICY,
                graph=lifecycle_rig.read,
                access_role_store=roles,
                submitter_source=lambda: lifecycle_rig.gateway,
            )

    _run()
    digest, groups = canonical_digest(lifecycle_rig.graph), lifecycle_rig.logged_groups
    _run()

    assert canonical_digest(lifecycle_rig.graph) == digest
    assert lifecycle_rig.logged_groups == groups


def test_identity_less_legacy_edges_are_stamped_before_the_first_group(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Deliverable 7: one stamp group (no audit link) precedes the first edit group."""
    _seed_tree(lifecycle_rig)
    assert all(key[-1] == "" for key in lifecycle_rig.graph.edges)

    update_policy_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        fields={"description": "new"},
        graph=PolicyReadGraph(lifecycle_rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=lifecycle_rig.gateway,
    )

    assert lifecycle_rig.logged_groups == 2
    assert not lifecycle_rig.store.groups[0].audit_event_id  # the fake log stores None as ''
    assert all(key[-1] != "" for key in lifecycle_rig.graph.edges)
