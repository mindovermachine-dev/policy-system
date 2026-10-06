"""A signing credential is only usable on the `rp_id` it was enrolled under (issue #196).

`signing_credentials` recorded no `rp_id`, and `needs_enrollment` was simply
`not has_any_for_actor(...)`. A WebAuthn credential only works for the RP id
that created it, so once an actor had enrolled on *any* host, every later
approval link on a *different* host reported "already enrolled", offered that
unusable credential in `allowCredentials`, and left the officer with a
browser-side failure and no route back to enrollment -- the shell only enrolls
when `needs_enrollment` is true.

Fixing the loopback-IP link host (`127.0.0.1` -> `localhost`) makes this live
rather than theoretical: an actor already enrolled under `127.0.0.1` is now
handed `localhost` links. The same trap fires on any real host change
(a `kind` NodePort to an Ingress domain).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from webauthn.helpers import bytes_to_base64url

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from passkey_signing.test_signing_ceremony import (
    _ACTOR_ISSUER,  # pyright: ignore[reportPrivateUsage]
    _ACTOR_SUBJECT,  # pyright: ignore[reportPrivateUsage]
    _RP_ID,  # pyright: ignore[reportPrivateUsage]
    _client,  # pyright: ignore[reportPrivateUsage]
    _seed_pending_approval,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.auth import Principal

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

_OTHER_RP_ID = "some-other-host"


def _authenticated_client(
    pending_store: FakePendingApprovalStore, credential_store: FakeSigningCredentialStore
) -> TestClient:
    return _client(
        pending_store=pending_store,
        credential_store=credential_store,
        principal=Principal(sub=_ACTOR_SUBJECT, iss=_ACTOR_ISSUER),
    )


def test_summary_requires_enrollment_when_the_only_credential_is_for_another_rp_id() -> None:
    """AC-BI-014: a credential enrolled on a different host cannot sign here, so the
    ceremony must offer enrollment rather than report the actor as already enrolled.
    """
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    credential_store.create_signing_credential(
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        credential_id=b"credential-enrolled-elsewhere",
        public_key=b"public-key",
        sign_count=0,
        rp_id=_OTHER_RP_ID,
    )
    client = _authenticated_client(pending_store, credential_store)

    body = client.post(f"/approvals/{row.id}/summary", json={"code": code}).json()

    assert body["needs_enrollment"] is True


def test_summary_skips_enrollment_when_a_credential_exists_for_this_rp_id() -> None:
    """AC-BI-014's other half: scoping must not force a re-enrollment on every visit --
    a credential enrolled under the current `rp_id` still suppresses it.
    """
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    credential_store.create_signing_credential(
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        credential_id=b"credential-enrolled-here",
        public_key=b"public-key",
        sign_count=0,
        rp_id=_RP_ID,
    )
    client = _authenticated_client(pending_store, credential_store)

    body = client.post(f"/approvals/{row.id}/summary", json={"code": code}).json()

    assert body["needs_enrollment"] is False


def test_summary_requires_enrollment_for_a_credential_predating_the_rp_id_column() -> None:
    """AC-BI-015: a row enrolled before `rp_id` was recorded (NULL) cannot be proven
    usable on this host, so it must never suppress enrollment.
    """
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    credential_store.create_signing_credential(
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        credential_id=b"credential-predating-the-column",
        public_key=b"public-key",
        sign_count=0,
        rp_id=None,
    )
    client = _authenticated_client(pending_store, credential_store)

    body = client.post(f"/approvals/{row.id}/summary", json={"code": code}).json()

    assert body["needs_enrollment"] is True


def test_sign_options_offers_only_credentials_enrolled_for_this_rp_id() -> None:
    """AC-BI-014/AC-BI-015: `allowCredentials` must not advertise a credential the
    browser cannot produce an assertion for -- the authenticator would simply find
    no match, surfacing as an unexplained browser-side failure.
    """
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    for credential_id, rp_id in (
        (b"credential-enrolled-here", _RP_ID),
        (b"credential-enrolled-elsewhere", _OTHER_RP_ID),
        (b"credential-predating-the-column", None),
    ):
        credential_store.create_signing_credential(
            actor_subject=_ACTOR_SUBJECT,
            actor_issuer=_ACTOR_ISSUER,
            credential_id=credential_id,
            public_key=b"public-key",
            sign_count=0,
            rp_id=rp_id,
        )
    client = _authenticated_client(pending_store, credential_store)

    options = client.post(f"/approvals/{row.id}/sign/options", json={"code": code}).json()

    offered = {entry["id"] for entry in options["allowCredentials"]}
    assert offered == {bytes_to_base64url(b"credential-enrolled-here")}
