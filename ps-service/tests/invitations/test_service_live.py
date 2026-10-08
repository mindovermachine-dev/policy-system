"""`user.invite` rows against a real Postgres (issue #195, F-2a). `postgres_live`-marked.

The invite flows through `invite_user_audited` into `PsycopgAuditStore` on an isolated schema and
is read back through `authz.service.list_audit_events` by action, actor and resource id.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore

from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit import AuditContext, AuditQueryFilters, PsycopgAuditStore
from ps_service.authz.service import list_audit_events
from ps_service.config import ServiceConfig, load_config
from ps_service.invitations.client import InvitationResult
from ps_service.invitations.service import invite_user_audited
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import LiteralString

_OWNER = ("owner-sub", "https://issuer.example.com/")
_ACTOR = ("admin-sub", "https://issuer.example.com/")
_EMAIL = "live-target@example.com"


@pytest.fixture(name="live_config")
def _live_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[ServiceConfig]:  # pyright: ignore[reportUnusedFunction]  # used by name
    """The real config pinned (via `PGOPTIONS`) to a throwaway schema with the audit migration."""
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )
    schema = f"invitations_test_{uuid.uuid4().hex}"
    with connect_from_config(config) as conn:
        conn.execute(cast("LiteralString", f'CREATE SCHEMA "{schema}"'))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=[MigrationSource("audit", AUDIT_MIGRATIONS_DIR)])
    try:
        yield config
    finally:
        monkeypatch.delenv("PGOPTIONS")
        with connect_from_config(config) as conn:
            conn.execute(cast("LiteralString", f'DROP SCHEMA "{schema}" CASCADE'))


def _send(config: ServiceConfig, email: str) -> InvitationResult:
    del config, email
    return InvitationResult(itoken="tok", invite_url="https://x/?itoken=tok")


@pytest.mark.postgres_live
def test_invite_user_row_is_readable_through_list_audit_events_against_postgres(
    live_config: ServiceConfig,
) -> None:
    store = PsycopgAuditStore(live_config)
    invite_user_audited(
        live_config, _EMAIL, audit=AuditContext(actor=_ACTOR, store=store), send_invitation=_send
    )
    roles = FakeAccessRoleStore(expected_owner=_OWNER)
    roles.bootstrap_first_owner(_OWNER)

    for filters in (
        AuditQueryFilters(action="user.invite"),
        AuditQueryFilters(actor_subject=_ACTOR[0], actor_issuer=_ACTOR[1]),
        AuditQueryFilters(resource_type="user", resource_id=_EMAIL),
    ):
        page = list_audit_events(
            _OWNER,
            filters=filters,
            cursor=None,
            page_size=10,
            access_role_store=roles,
            audit_store=store,
        )
        assert [(e.action, e.outcome, e.resource_id, e.details) for e in page.events] == [
            ("user.invite", "applied", _EMAIL, {"invitee_email": _EMAIL})
        ]
