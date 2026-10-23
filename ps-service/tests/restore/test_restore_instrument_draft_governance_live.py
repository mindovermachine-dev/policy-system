"""Live FalkorDB proof that a restored internal tree is a normal draft.

Restores the shipped `ENGPRAC-1.0` curated artifact into tokened scratch graphs (the
permanent `engprac_*`/`policy_system` graphs are never touched) and drives the REAL
policy-lifecycle service against the result:

- the restoring caller owns the imported drafts, proposes one, cannot approve their own
  import (four-eyes), and a different `PolicyManager` can -- approval cascades the whole
  tree and leaves every other restored Policy `draft`;
- restoring the same instrument again after that approval changes nothing: the approved
  tree keeps its status and owner, and no node or edge is duplicated.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from policy_lifecycle._gateway_fakes import policy_manager_enforcer

from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.models import AccessRole
from ps_service.export import catalog_writer
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.policy_lifecycle.errors import PolicySelfApprovalBlockedError
from ps_service.policy_lifecycle.graph_submission import lifecycle_submitter
from ps_service.policy_lifecycle.service import approve_policy, propose_policy
from ps_service.restore.audit_actions import INSTRUMENT_RESTORE_ACTION
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from collections.abc import Callable

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

    from ps_service.config import ServiceConfig

pytestmark = [pytest.mark.falkordb_live, pytest.mark.postgres_live]

_INSTRUMENT_DIR = Path(__file__).resolve().parents[3] / "curated-content" / "ENGPRAC-1.0"
_MERGE_THRESHOLD = 0.9
_OWNER = ("restorer@example.com", "https://idp.example/")
_MANAGER = ("manager@example.com", "https://idp.example/")
_ACTOR = "test-actor"


def _artifact(short_name: str) -> RestoreArtifact:
    """The shipped bytes and digests, restored unmodified under a tokened `short_name`."""
    manifest = replace(catalog_writer.read_manifest(_INSTRUMENT_DIR), short_name=short_name)
    return RestoreArtifact(
        manifest=manifest,
        baseline_blob=(_INSTRUMENT_DIR / "baseline.json").read_bytes(),
        native_blob=(_INSTRUMENT_DIR / "native.json").read_bytes(),
    )


def _graph_counts(
    rows: Callable[[FalkorDB, str, str], list[list[object]]], db: FalkorDB, graph_name: str
) -> dict[str, int]:
    """Node count per governance label plus the governance edge counts."""
    counts: dict[str, int] = {}
    for label in ("Policy", "Standard", "Control"):
        counts[label] = cast(
            "int", rows(db, graph_name, f"MATCH (n:{label}) RETURN count(n)")[0][0]
        )
    for relationship in ("GOVERNED_BY", "SUPPORTED_BY", "IMPLEMENTED_BY"):
        counts[relationship] = cast(
            "int", rows(db, graph_name, f"MATCH ()-[r:{relationship}]->() RETURN count(r)")[0][0]
        )
    return counts


def _restore(
    db: FalkorDB, emitter: object, short_name: str, graph_name: str, config: ServiceConfig
) -> None:
    # The restore writes through the real gateway; its groups are anchored to a real
    # `instrument.restore` audit row (the graph log's audit link is a foreign key into
    # `audit_events`).
    audit_event_id = PsycopgAuditStore(config).record_standalone_with_event_id(
        actor_subject=_OWNER[0],
        actor_issuer=_OWNER[1],
        action=INSTRUMENT_RESTORE_ACTION,
        resource_type="instrument",
        resource_id="ENGPRAC-1.0",
        outcome="applied",
        details={"instrument_id": "ENGPRAC-1.0", "status": "started", "source": "catalog"},
    )
    gateway = build_default_graph_write_gateway(config)
    restore_instrument(
        _artifact(short_name),
        db=db,
        single_tenant_graph_name=graph_name,
        merge_threshold=_MERGE_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        actor=_ACTOR,
        open_gateway=lambda: gateway,
        audit_event_id=audit_event_id,
        emitter=emitter,  # type: ignore[arg-type]
        owner=_OWNER,
    )


def _approve_one_restored_policy(db: FalkorDB, graph_name: str, config: ServiceConfig) -> str:
    """Propose and approve one restored Policy through the real lifecycle service."""
    graph = db.select_graph(graph_name)
    rows = cast(
        "list[list[object]]",
        graph.query(
            "MATCH (p:Policy)-[:SUPPORTED_BY]->(:Standard) RETURN DISTINCT p.id ORDER BY p.id"
        ).result_set,
    )
    assert rows, "sanity: at least one restored Policy has a Standard"
    target = cast("str", rows[0][0])
    store = FakeAccessRoleStore()
    store.grant(actor=_OWNER, target=_OWNER, access_role=AccessRole.POLICY_MANAGER)
    store.grant(actor=_OWNER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)
    # The lifecycle writes through the real gateway into `graph_name`, and its `applied` rows are
    # real audit rows (the graph log's audit link is a foreign key into `audit_events`).
    audit = PsycopgAuditStore(config)
    submitter = lifecycle_submitter(build_default_graph_write_gateway(config))

    propose_policy(
        actor=_OWNER,
        policy_id=target,
        graph=graph,
        audit_store=audit,
        submitter=submitter,
        graph_name=graph_name,
    )
    with pytest.raises(PolicySelfApprovalBlockedError):
        approve_policy(
            actor=_OWNER,
            policy_id=target,
            graph=graph,
            audit_store=audit,
            enforce_policy_manager=policy_manager_enforcer(store),
            submitter=submitter,
            graph_name=graph_name,
        )
    approve_policy(
        actor=_MANAGER,
        policy_id=target,
        graph=graph,
        audit_store=audit,
        enforce_policy_manager=policy_manager_enforcer(store),
        submitter=submitter,
        graph_name=graph_name,
    )
    return target


def _tree_statuses(db: FalkorDB, graph_name: str, policy_id: str) -> dict[str, set[str]]:
    graph = db.select_graph(graph_name)

    def _statuses(cypher: str) -> set[str]:
        rows = cast("list[list[object]]", graph.query(cypher, params={"pid": policy_id}).result_set)
        return {cast("str", row[0]) for row in rows}

    return {
        "Policy": _statuses("MATCH (p:Policy {id: $pid}) RETURN p.status"),
        "Standard": _statuses(
            "MATCH (:Policy {id: $pid})-[:SUPPORTED_BY]->(s:Standard) RETURN s.status"
        ),
        "Control": _statuses(
            "MATCH (:Policy {id: $pid})-[:SUPPORTED_BY]->(:Standard)-[:IMPLEMENTED_BY]->"
            "(c:Control) RETURN c.status"
        ),
    }


def test_restored_policy_can_be_proposed_and_approved_by_a_different_manager(
    provisioned_graph_log: Provisioned,
    live_falkordb: FalkorDB,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    """A restored draft is a normal draft; approval cascades its whole tree only."""
    emitter, _log_path = make_emitter()
    config = provisioned_graph_log.state_config()
    token = uuid.uuid4().hex[:12]
    short_name = f"ENGPRAC_L{token}"
    graph_name = f"__gh183_single_tenant_{token}__"
    targets = (f"{short_name.lower()}_native", f"{short_name.lower()}_baseline", graph_name)

    try:
        _restore(live_falkordb, emitter, short_name, graph_name, config)

        approved = _approve_one_restored_policy(live_falkordb, graph_name, config)

        assert _tree_statuses(live_falkordb, graph_name, approved) == {
            "Policy": {"approved"},
            "Standard": {"approved"},
            "Control": {"approved"},
        }
        policy_total = query_result_rows(
            live_falkordb, graph_name, "MATCH (p:Policy) RETURN count(p)"
        )[0][0]
        still_draft = query_result_rows(
            live_falkordb, graph_name, "MATCH (p:Policy) WHERE p.status = 'draft' RETURN count(p)"
        )[0][0]
        assert still_draft == cast("int", policy_total) - 1, (
            "approving one restored Policy must not touch the others"
        )
    finally:
        live_falkordb.connection.delete(*targets)


def test_second_restore_after_approval_leaves_the_approved_tree_and_counts_untouched(
    provisioned_graph_log: Provisioned,
    live_falkordb: FalkorDB,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    """A re-restore never resets an approved tree, owner, or duplicates anything."""
    emitter, _log_path = make_emitter()
    config = provisioned_graph_log.state_config()
    token = uuid.uuid4().hex[:12]
    short_name = f"ENGPRAC_R{token}"
    graph_name = f"__gh183_rerestore_{token}__"
    targets = (f"{short_name.lower()}_native", f"{short_name.lower()}_baseline", graph_name)

    try:
        _restore(live_falkordb, emitter, short_name, graph_name, config)
        approved = _approve_one_restored_policy(live_falkordb, graph_name, config)
        before = _graph_counts(query_result_rows, live_falkordb, graph_name)

        _restore(live_falkordb, emitter, short_name, graph_name, config)

        assert _tree_statuses(live_falkordb, graph_name, approved) == {
            "Policy": {"approved"},
            "Standard": {"approved"},
            "Control": {"approved"},
        }, "a re-restore must not reset an approved tree to draft"
        owner_rows = query_result_rows(
            live_falkordb,
            graph_name,
            f"MATCH (p:Policy {{id: '{approved}'}}) RETURN p.owner_subject, p.owner_issuer",
        )
        assert owner_rows == [list(_OWNER)]
        assert _graph_counts(query_result_rows, live_falkordb, graph_name) == before
    finally:
        live_falkordb.connection.delete(*targets)
