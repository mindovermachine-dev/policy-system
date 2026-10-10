"""Helpers of the `postgres_live` graph gateway tests (issue #207): audit rows for real appends."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from ps_service.audit.store import PsycopgAuditStore
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned


def committed_audit_event(prov: Provisioned) -> str:
    """Record and commit one audit row, so a standalone append can reference it."""
    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        return PsycopgAuditStore(prov.state_config()).record(
            cur,
            actor_subject="test-actor-subject",
            actor_issuer="https://issuer.example.com/",
            action="access_role.grant",
            resource_type="principal",
            resource_id=f"subject-{uuid.uuid4().hex[:8]}",
            outcome="applied",
            details={"access_role": "SystemAdmin"},
        )
