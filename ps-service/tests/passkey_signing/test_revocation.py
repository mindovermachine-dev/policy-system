"""Revoking an actor's signing passkeys: scope, audit row, rollback, and the store's statements.

The service operation runs against in-memory stores; the real store's SQL is checked here for
its actor scoping (both predicates in every statement, no inline SQL) and against a real
Postgres in `test_revocation_store_live.py`.
"""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore

import ps_service.passkey_signing.revocation as revocation_module
from passkey_signing._fakes import (
    FakePendingApprovalStore,
    FakeSigningCredentialStore,
    FakeSigningRevocationStore,
)
from ps_service.audit.errors import AuditPersistenceError, AuditTrailUnavailableError
from ps_service.passkey_signing.errors import SigningRevocationPersistenceError
from ps_service.passkey_signing.revocation import (
    ACTOR_SCOPED_STATEMENTS,
    PASSKEY_RESET_ACTOR,
    ActorSigningRevocation,
    read_actor_signing_state,
    revoke_actor_signing,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.logging.emitter import LogEmitter
    from ps_service.passkey_signing.models import PendingApprovalRow

    type ReadLines = Callable[[Path], list[dict[str, object]]]
    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]

SUBJECT = "owner@example.com"
ISSUER = "https://auth.example.test/application/o/ps/"
OTHER_ISSUER = "https://other.example.test/application/o/ps/"
OTHER_SUBJECT = "someone@example.com"
VOID_OUTCOME = {
    "error": "this approval was cancelled because the approver's signing passkeys were reset; "
    "ask for a new approval"
}


def _enrol(
    credentials: FakeSigningCredentialStore, subject: str, issuer: str, rp: str | None
) -> None:
    credentials.create_signing_credential(
        actor_subject=subject,
        actor_issuer=issuer,
        credential_id=f"{subject}|{issuer}|{rp}|{len(credentials.all_rows())}".encode(),
        public_key=b"cose-key",
        sign_count=0,
        rp_id=rp,
    )


def _owners(credentials: FakeSigningCredentialStore) -> list[tuple[str, str, str | None]]:
    return [(row.actor_subject, row.actor_issuer, row.rp_id) for row in credentials.all_rows()]


def _store_with_owner_and_neighbours() -> tuple[
    FakeSigningRevocationStore, FakeSigningCredentialStore
]:
    credentials = FakeSigningCredentialStore()
    for rp in ("ps.example.test", "eval.example.test", None):
        _enrol(credentials, SUBJECT, ISSUER, rp)
    _enrol(credentials, SUBJECT, OTHER_ISSUER, "ps.example.test")
    _enrol(credentials, OTHER_SUBJECT, ISSUER, "ps.example.test")
    return FakeSigningRevocationStore(credentials=credentials), credentials


def _revoke(
    store: FakeSigningRevocationStore,
    audit: InMemoryAuditStore,
    *,
    emitter: LogEmitter | None = None,
) -> ActorSigningRevocation:
    return revoke_actor_signing(
        actor_subject=SUBJECT,
        actor_issuer=ISSUER,
        revocation_store=store,
        audit_store=audit,
        emitter=emitter,
    )


def test_revoke_deletes_all_of_the_actors_credentials_under_every_rp_id() -> None:
    store, credentials = _store_with_owner_and_neighbours()

    result = _revoke(store, InMemoryAuditStore())

    assert result == ActorSigningRevocation(credentials_deleted=3, voided_approval_ids=())
    assert [owner for owner in _owners(credentials) if owner[:2] == (SUBJECT, ISSUER)] == []


def test_revoke_never_touches_another_subject_or_the_same_subject_at_another_issuer() -> None:
    store, credentials = _store_with_owner_and_neighbours()

    _revoke(store, InMemoryAuditStore())

    assert _owners(credentials) == [
        (SUBJECT, OTHER_ISSUER, "ps.example.test"),
        (OTHER_SUBJECT, ISSUER, "ps.example.test"),
    ]


def test_revoke_writes_one_passkey_revoke_row_with_the_system_actor_and_principal_resource() -> (
    None
):
    store, _ = _store_with_owner_and_neighbours()
    audit = InMemoryAuditStore()

    _revoke(store, audit)

    (row,) = audit.rows
    assert (row.actor_subject, row.actor_issuer) == PASSKEY_RESET_ACTOR
    assert PASSKEY_RESET_ACTOR == ("system:owner-passkey-reset", "system")
    assert (row.action, row.resource_type, row.resource_id) == (
        "passkey.revoke",
        "principal",
        SUBJECT,
    )
    assert row.outcome == "applied"
    assert row.details == {
        "revoked_issuer": ISSUER,
        "credentials_deleted": 3,
        "voided_approval_ids": [],
    }


def test_revoke_with_nothing_to_revoke_still_writes_a_zero_count_row() -> None:
    store = FakeSigningRevocationStore(credentials=FakeSigningCredentialStore())
    audit = InMemoryAuditStore()

    result = _revoke(store, audit)

    assert result == ActorSigningRevocation(credentials_deleted=0, voided_approval_ids=())
    (row,) = audit.rows
    assert row.outcome == "applied"
    assert row.details == {
        "revoked_issuer": ISSUER,
        "credentials_deleted": 0,
        "voided_approval_ids": [],
    }


def test_an_unwritable_audit_row_leaves_every_credential_in_place() -> None:
    store, credentials = _store_with_owner_and_neighbours()
    before = _owners(credentials)
    audit = InMemoryAuditStore(fail_on_outcome={"applied": AuditPersistenceError("x")})

    with pytest.raises(AuditTrailUnavailableError):
        _revoke(store, audit)

    assert _owners(credentials) == before
    assert audit.rows == []


def test_an_unwritable_audit_row_logs_neither_subject_nor_issuer(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    store, _ = _store_with_owner_and_neighbours()
    audit = InMemoryAuditStore(fail_on_outcome={"applied": AuditPersistenceError("x")})
    emitter, log_path = make_emitter()

    with pytest.raises(AuditTrailUnavailableError):
        _revoke(store, audit, emitter=emitter)

    emitter.flush()
    lines = read_lines(log_path)
    assert lines, "the failed audit write is logged"
    for line in lines:
        assert line.get("entity_id") is None
        assert SUBJECT not in str(line)
        assert ISSUER not in str(line)


def test_a_failed_commit_after_the_audit_row_writes_a_failed_follow_up_row() -> None:
    store, credentials = _store_with_owner_and_neighbours()
    store.fail_commit = True
    before = _owners(credentials)
    audit = InMemoryAuditStore()

    with pytest.raises(SigningRevocationPersistenceError):
        _revoke(store, audit)

    assert [row.outcome for row in audit.rows] == ["applied", "failed"]
    applied, failed = audit.rows
    assert "reason_code" not in applied.details
    assert failed.details == {
        "revoked_issuer": ISSUER,
        "credentials_deleted": 3,
        "voided_approval_ids": [],
        "reason_code": "signing_store_unavailable",
    }
    assert (failed.actor_subject, failed.actor_issuer) == PASSKEY_RESET_ACTOR
    assert _owners(credentials) == before


def test_a_store_failure_before_the_audit_row_writes_no_row() -> None:
    store, credentials = _store_with_owner_and_neighbours()
    store.fail_before_audit = True
    before = _owners(credentials)
    audit = InMemoryAuditStore()

    with pytest.raises(SigningRevocationPersistenceError):
        _revoke(store, audit)

    assert audit.rows == []
    assert _owners(credentials) == before


def test_reading_the_signing_state_counts_only_the_actors_rows_and_writes_nothing() -> None:
    store, credentials = _store_with_owner_and_neighbours()
    pending = FakePendingApprovalStore()
    for subject, issuer in ((SUBJECT, ISSUER), (SUBJECT, OTHER_ISSUER), (OTHER_SUBJECT, ISSUER)):
        pending.create_pending_approval(
            tool_name="merge-capabilities",
            normalized_args={},
            actor_subject=subject,
            actor_issuer=issuer,
            display_summary={},
        )
    store.pending = pending
    before = _owners(credentials)

    state = read_actor_signing_state(
        actor_subject=SUBJECT, actor_issuer=ISSUER, revocation_store=store
    )

    assert state.credentials == 3
    assert len(state.pending_approval_ids) == 1
    assert _owners(credentials) == before


def _request_approval(
    pending: FakePendingApprovalStore, subject: str, issuer: str
) -> PendingApprovalRow:
    row, _code = pending.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={"survivor_id": "a", "absorbed_id": "b"},
        actor_subject=subject,
        actor_issuer=issuer,
        display_summary={},
    )
    return row


def _expire(pending: FakePendingApprovalStore, approval_id: str) -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)
    pending.replace_rows(
        tuple(
            replace(row, expires_at=past) if row.id == approval_id else row
            for row in pending.all_rows()
        )
    )


def _store_with_pending_approvals() -> tuple[
    FakeSigningRevocationStore, FakePendingApprovalStore, list[str]
]:
    store, _ = _store_with_owner_and_neighbours()
    pending = FakePendingApprovalStore()
    owners = [_request_approval(pending, SUBJECT, ISSUER).id for _ in range(2)]
    _expire(pending, owners[1])
    _request_approval(pending, SUBJECT, OTHER_ISSUER)
    _request_approval(pending, OTHER_SUBJECT, ISSUER)
    signed = _request_approval(pending, SUBJECT, ISSUER)
    assert pending.mark_signed(signed.id)
    pending.set_outcome(signed.id, {"survivor_id": "a"})
    store.pending = pending
    return store, pending, owners


def _approval_states(
    pending: FakePendingApprovalStore,
) -> list[tuple[str, str, str, dict[str, object] | None]]:
    return [
        (row.actor_subject, row.actor_issuer, row.status, row.outcome) for row in pending.all_rows()
    ]


def test_revoke_voids_every_pending_approval_with_the_fixed_ask_again_outcome() -> None:
    store, pending, owners = _store_with_pending_approvals()

    result = _revoke(store, InMemoryAuditStore())

    assert result.voided_approval_ids == tuple(owners)
    for approval_id in owners:
        row = pending.get_by_id(approval_id)
        assert row is not None
        assert (row.status, row.outcome) == ("signed", VOID_OUTCOME)


def test_signed_approvals_and_other_actors_pending_approvals_are_untouched() -> None:
    store, pending, _ = _store_with_pending_approvals()

    _revoke(store, InMemoryAuditStore())

    assert _approval_states(pending)[2:] == [
        (SUBJECT, OTHER_ISSUER, "pending", None),
        (OTHER_SUBJECT, ISSUER, "pending", None),
        (SUBJECT, ISSUER, "signed", {"survivor_id": "a"}),
    ]


def test_audit_row_lists_the_voided_approval_ids() -> None:
    store, _, owners = _store_with_pending_approvals()
    audit = InMemoryAuditStore()

    _revoke(store, audit)

    (row,) = audit.rows
    assert row.details == {
        "revoked_issuer": ISSUER,
        "credentials_deleted": 3,
        "voided_approval_ids": owners,
    }


def test_an_unwritable_audit_row_voids_no_approval() -> None:
    store, pending, _ = _store_with_pending_approvals()
    before = _approval_states(pending)
    audit = InMemoryAuditStore(fail_on_outcome={"applied": AuditPersistenceError("x")})

    with pytest.raises(AuditTrailUnavailableError):
        _revoke(store, audit)

    assert _approval_states(pending) == before


def test_a_failed_commit_lists_the_uncommitted_voided_ids_and_voids_nothing() -> None:
    store, pending, owners = _store_with_pending_approvals()
    store.fail_commit = True
    before = _approval_states(pending)
    audit = InMemoryAuditStore()

    with pytest.raises(SigningRevocationPersistenceError):
        _revoke(store, audit)

    assert [row.details["voided_approval_ids"] for row in audit.rows] == [owners, owners]
    assert _approval_states(pending) == before


_SUBJECT_PREDICATE = "actor_subject = %(actor_subject)s"
_ISSUER_PREDICATE = "actor_issuer = %(actor_issuer)s"


def test_every_revocation_statement_is_scoped_by_subject_and_issuer() -> None:
    assert len(ACTOR_SCOPED_STATEMENTS) >= 4
    assert any("UPDATE pending_approvals" in statement for statement in ACTOR_SCOPED_STATEMENTS)
    for statement in ACTOR_SCOPED_STATEMENTS:
        normalised = re.sub(r"\s+", " ", statement)
        where = normalised.split(" WHERE ", 1)
        assert len(where) == 2, statement
        assert _SUBJECT_PREDICATE in where[1], statement
        assert _ISSUER_PREDICATE in where[1], statement
        assert " OR " not in where[1].upper(), statement


def test_the_store_executes_only_the_scoped_statements() -> None:
    source = Path(revocation_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    scoped = {
        name
        for name, value in vars(revocation_module).items()
        if isinstance(value, str) and value in ACTOR_SCOPED_STATEMENTS
    }
    assert len(scoped) == len(ACTOR_SCOPED_STATEMENTS)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    ]
    assert calls, "the store executes its statements through cursor.execute"
    for call in calls:
        first = call.args[0] if call.args else None
        assert isinstance(first, ast.Name), ast.unparse(call)
        assert first.id in scoped, ast.unparse(call)
