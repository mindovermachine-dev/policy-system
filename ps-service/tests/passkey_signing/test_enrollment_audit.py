# pyright: reportPrivateUsage=false
"""Audit rows of the passkey ceremony: enrolment writes `passkey.enroll`, nothing else does.

A successful enrolment writes one best-effort `passkey.enroll` row after the credential insert,
attributed to the `(sub, iss)` bound to the pending approval (the caller is identified by the
approval code, not by authentication). Every rejected step, enrolment or signing, writes no row
and is logged in the operator log without a subject. A signed action writes no `passkey.sign`
row: its own component audits the effect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore
from authz._fakes import FakeAccessRoleStore
from fastapi.testclient import TestClient
from pydantic import ValidationError
from webauthn.helpers import base64url_to_bytes

from passkey_signing import test_enrollment_ceremony as enrol_support
from passkey_signing import test_signing_ceremony as sign_support
from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from ps_service.api.dependencies import (
    get_principal,
    provide_audit_store,
    provide_pending_approval_store,
)
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.models import AuditQueryFilters, is_known_resource_type, resolve_details_model
from ps_service.auth import Principal
from ps_service.authz.service import list_audit_events
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.audit_actions import PasskeyEnrollDetails
from ps_service.passkey_signing.executors import _EXECUTORS, register_approval_executor
from ps_service.passkey_signing.router import provide_signing_credential_store

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow, SigningCredentialRow

_ACTOR_SUBJECT = enrol_support._ACTOR_SUBJECT
_ACTOR_ISSUER = enrol_support._ACTOR_ISSUER
_RP_ID = enrol_support._RP_ID
_ORIGIN = enrol_support._ORIGIN
_TOOL_NAME = sign_support._TOOL_NAME
_OWNER = ("owner", "https://issuer.example.com/")


@dataclass
class _OrderRecordingCredentialStore(FakeSigningCredentialStore):
    """A credential store that appends each insert to a shared ordered events list."""

    events: list[str] = field(default_factory=list[str])

    def create_signing_credential(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        rp_id: str | None,
    ) -> SigningCredentialRow:
        """Record the insert on the shared events list, then insert as the base store does."""
        self.events.append("credential:insert")
        return super().create_signing_credential(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            rp_id=rp_id,
        )


class _Rig:
    """An app with fake approval, credential and audit stores, plus a recording executor."""

    def __init__(
        self,
        *,
        with_principal: bool = False,
        audit: InMemoryAuditStore | None = None,
        credentials: FakeSigningCredentialStore | None = None,
    ) -> None:
        self.pending = FakePendingApprovalStore()
        self.credentials = credentials or FakeSigningCredentialStore()
        self.audit = audit or InMemoryAuditStore()
        self.executed: list[str] = []
        app = create_app(enrol_support._app_config())
        app.dependency_overrides[provide_pending_approval_store] = lambda: self.pending
        app.dependency_overrides[provide_signing_credential_store] = lambda: self.credentials
        app.dependency_overrides[provide_audit_store] = lambda: self.audit
        if with_principal:
            app.dependency_overrides[get_principal] = lambda: Principal(
                sub=_ACTOR_SUBJECT, iss=_ACTOR_ISSUER
            )
        _EXECUTORS.pop(_TOOL_NAME, None)  # the conftest restores the registry after the test
        register_approval_executor(_TOOL_NAME, self._execute)
        self.client = TestClient(app)
        self.row, self.code = enrol_support._seed_pending_approval(self.pending)

    def _execute(self, row: PendingApprovalRow, _config: object) -> dict[str, object]:
        self.executed.append(row.id)
        return {"merged": True}

    def credential(self, row: PendingApprovalRow | None = None) -> dict[str, object]:
        target = row or self.row
        return enrol_support._build_registration_credential(
            rp_id=_RP_ID, origin=_ORIGIN, challenge=enrol_support.enrollment_challenge(target)
        )

    def enroll(self, *, code: str | None = None, credential: dict[str, object] | None = None):
        return self.client.post(
            f"/approvals/{self.row.id}/enroll/verify",
            json={"code": code or self.code, "credential": credential or self.credential()},
        )


def _capture_logs(monkeypatch: pytest.MonkeyPatch, target: str) -> list[dict[str, object]]:
    """Replace the log call at `target` with a recorder and return the recorded entries."""
    logged: list[dict[str, object]] = []

    def _record(**kwargs: object) -> None:
        logged.append(kwargs)

    # detroit-exception: capturing the log call's args IS the spec here
    monkeypatch.setattr(target, _record)
    return logged


def _passkey_rows(store: InMemoryAuditStore) -> list[object]:
    return [row for row in store.rows if row.action.startswith("passkey.")]


def test_successful_enrolment_writes_one_passkey_enroll_row_attributed_to_the_bound_actor() -> None:
    rig = _Rig()
    response = rig.enroll()
    assert response.status_code == 200
    assert response.json() == {"status": "enrolled"}
    (row,) = rig.audit.rows
    assert (row.action, row.outcome) == ("passkey.enroll", "applied")
    assert (row.actor_subject, row.actor_issuer) == (_ACTOR_SUBJECT, _ACTOR_ISSUER)


def test_enrol_row_resource_is_the_pending_approval_id_and_proof_is_approval_code() -> None:
    rig = _Rig()
    rig.enroll()
    (row,) = rig.audit.rows
    assert row.resource_type == "pending_approval"
    assert row.resource_id == rig.row.id
    assert row.details == {"proof": "approval_code"}


def test_enrol_row_is_written_after_the_credential_insert() -> None:
    audit = InMemoryAuditStore()
    rig = _Rig(audit=audit, credentials=_OrderRecordingCredentialStore(events=audit.events))
    rig.enroll()
    assert rig.audit.events == ["credential:insert", "audit:passkey.enroll:applied"]


def test_enrolment_succeeds_and_logs_the_class_name_when_the_audit_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged = _capture_logs(monkeypatch, "ps_service.audit.emit.emit_log_entry")
    rig = _Rig()
    rig.audit.fail_on_action["passkey.enroll"] = AuditPostgresUnavailableError("db host down")
    response = rig.enroll()
    assert response.status_code == 200
    assert response.json() == {"status": "enrolled"}
    assert rig.credentials.has_any_for_actor(
        actor_subject=_ACTOR_SUBJECT, actor_issuer=_ACTOR_ISSUER, rp_id=_RP_ID
    )
    assert [e["extra"] for e in logged] == [
        {"audit_action": "passkey.enroll", "reason": "AuditPostgresUnavailableError"}
    ]
    assert "db host" not in repr(logged)


def test_enrolment_with_a_leaking_audit_store_logs_and_records_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel text in the audit store's error reaches no log entry and no audit row."""
    sentinels = ("SECRET-PK", "Bearer abc", "https://u:p@host/", "/Users/x/y.py", "itoken=SECRET")
    logged = _capture_logs(monkeypatch, "ps_service.audit.emit.emit_log_entry")
    rig = _Rig()
    rig.audit.fail_on_action["passkey.enroll"] = AuditPostgresUnavailableError(
        " | ".join(sentinels)
    )
    response = rig.enroll()
    assert response.status_code == 200
    assert logged, "the failed audit write must be logged"
    haystack = repr(logged) + repr(rig.audit.rows) + response.text
    for sentinel in sentinels:
        assert sentinel not in haystack, sentinel


@pytest.mark.parametrize(
    "case", ["wrong_code", "unknown_approval", "expired_approval", "invalid_registration"]
)
def test_rejected_enrolment_writes_no_audit_row(case: str) -> None:
    configure()
    rig = _Rig()
    if case == "wrong_code":
        response = rig.enroll(code="not-the-code")
    elif case == "unknown_approval":
        response = rig.client.post(
            f"/approvals/{uuid.uuid4()}/enroll/verify",
            json={"code": rig.code, "credential": rig.credential()},
        )
    elif case == "expired_approval":
        rig.pending._rows_by_id[rig.row.id] = replace(
            rig.row, expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        response = rig.enroll()
    else:
        response = rig.enroll(credential={"bogus": True})
    assert response.status_code == 404
    assert rig.audit.rows == []
    assert rig.audit.events == []


def test_rejected_enrolment_is_logged_in_the_operator_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged = _capture_logs(monkeypatch, "ps_service.passkey_signing.error_handlers.emit_log_entry")
    rig = _Rig()
    rig.enroll(code="not-the-code")
    (entry,) = logged
    assert (entry["component"], entry["action"], entry["outcome"]) == (
        "passkey_signing",
        "enroll_verify",
        "failed",
    )
    assert _ACTOR_SUBJECT not in repr(entry)
    assert _ACTOR_ISSUER not in repr(entry)


def _sign_rig() -> tuple[_Rig, sign_support._Authenticator]:
    rig = _Rig(with_principal=True)
    authenticator = sign_support._Authenticator()
    sign_support._enroll(rig.credentials, authenticator)
    return rig, authenticator


def test_a_signed_action_writes_no_passkey_sign_row() -> None:
    rig, authenticator = _sign_rig()
    options = rig.client.post(f"/approvals/{rig.row.id}/sign/options", json={"code": rig.code})
    challenge = base64url_to_bytes(options.json()["challenge"])
    assertion = authenticator.build_assertion(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge, sign_count=1
    )
    response = rig.client.post(
        f"/approvals/{rig.row.id}/sign/verify", json={"code": rig.code, "credential": assertion}
    )
    assert response.status_code == 200
    assert rig.executed == [rig.row.id]
    assert _passkey_rows(rig.audit) == []
    assert not any(row.action == "passkey.sign" for row in rig.audit.rows)


def test_passkey_sign_is_not_a_registered_audit_action() -> None:
    assert resolve_details_model("passkey.sign") is None


@pytest.mark.parametrize(
    "case",
    [
        "options_wrong_code",
        "options_expired_approval",
        "verify_wrong_code",
        "verify_bad_assertion",
        "verify_unknown_approval",
    ],
)
def test_rejected_sign_step_writes_no_audit_row(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    logged = _capture_logs(monkeypatch, "ps_service.passkey_signing.error_handlers.emit_log_entry")
    rig, authenticator = _sign_rig()
    bogus = {
        "id": "AAAA",
        "rawId": "AAAA",
        "type": "public-key",
        "response": {"clientDataJSON": "", "authenticatorData": "", "signature": ""},
    }
    del authenticator
    base = f"/approvals/{rig.row.id}"
    if case == "options_wrong_code":
        response = rig.client.post(f"{base}/sign/options", json={"code": "not-the-code"})
    elif case == "options_expired_approval":
        rig.pending._rows_by_id[rig.row.id] = replace(
            rig.row, expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        response = rig.client.post(f"{base}/sign/options", json={"code": rig.code})
    elif case == "verify_wrong_code":
        response = rig.client.post(
            f"{base}/sign/verify", json={"code": "not-the-code", "credential": bogus}
        )
    elif case == "verify_bad_assertion":
        response = rig.client.post(
            f"{base}/sign/verify", json={"code": rig.code, "credential": bogus}
        )
    else:
        response = rig.client.post(
            f"/approvals/{uuid.uuid4()}/sign/verify", json={"code": rig.code, "credential": bogus}
        )
    assert response.status_code == 404
    assert rig.audit.rows == []
    assert rig.executed == []
    assert logged, "the rejection is logged in the operator log"
    assert all(_ACTOR_SUBJECT not in repr(entry) for entry in logged)


def test_passkey_enroll_details_reject_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        PasskeyEnrollDetails.model_validate({"proof": "approval_code", "code": "x"})
    with pytest.raises(ValidationError):
        InMemoryAuditStore().record_standalone(
            actor_subject="a",
            actor_issuer="b",
            action="passkey.enroll",
            resource_type="pending_approval",
            resource_id="x",
            outcome="applied",
            details={"proof": "approval_code", "extra": "x"},
        )


def test_passkey_enroll_is_registered_with_the_pending_approval_resource_type() -> None:
    assert resolve_details_model("passkey.enroll") is PasskeyEnrollDetails
    assert is_known_resource_type("pending_approval")


def test_enrol_row_is_returned_by_list_audit_events_for_actor_and_action() -> None:
    rig = _Rig()
    rig.enroll()
    roles = FakeAccessRoleStore(expected_owner=_OWNER)
    roles.bootstrap_first_owner(_OWNER)
    page = list_audit_events(
        _OWNER,
        filters=AuditQueryFilters(actor_subject=_ACTOR_SUBJECT, action="passkey.enroll"),
        cursor=None,
        page_size=10,
        access_role_store=roles,
        audit_store=rig.audit,
    )
    assert [(e.actor_subject, e.action, e.resource_type, e.resource_id) for e in page.events] == [
        (_ACTOR_SUBJECT, "passkey.enroll", "pending_approval", rig.row.id)
    ]
