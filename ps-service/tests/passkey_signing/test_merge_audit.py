"""The signing router audits nothing itself and does not pre-check the graph write gateway.

A signed near-miss merge is a `merge-capabilities` approval. The executor registered for that tool
owns the write and its `capability.merge` audit rows (covered in
`graph_cleanup/test_execute_capability_merge.py` and `test_graph_cleanup_signing_to_merge.py`). The
router only verifies the signature, consumes the approval and stores the executor's outcome, so a
missing gateway is the executor's sanitized error on a consumed approval, not a 503 on a pending
one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from webauthn.helpers import base64url_to_bytes

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from passkey_signing.test_signing_ceremony import (
    _ACTOR_ISSUER,  # pyright: ignore[reportPrivateUsage]
    _ACTOR_SUBJECT,  # pyright: ignore[reportPrivateUsage]
    _ORIGIN,  # pyright: ignore[reportPrivateUsage]
    _RP_ID,  # pyright: ignore[reportPrivateUsage]
    _TOOL_NAME,  # pyright: ignore[reportPrivateUsage]
    _Authenticator,  # pyright: ignore[reportPrivateUsage]
    _client,  # pyright: ignore[reportPrivateUsage]
    _enroll,  # pyright: ignore[reportPrivateUsage]
    _seed_pending_approval,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.auth import Principal
from ps_service.logging import configure
from ps_service.passkey_signing import executors

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig
    from ps_service.passkey_signing.models import PendingApprovalRow

_EXECUTOR_ERROR = "the graph write gateway is not available"


@pytest.fixture(autouse=True)
def _configured_logging() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    """The failure paths emit semantic log entries, which need a configured facade."""
    configure()


def test_signing_with_no_gateway_installed_stores_the_executors_error_on_a_consumed_approval() -> (
    None
):
    install_graph_write_gateway(None)
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator, sign_count=0)
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        principal=Principal(sub=_ACTOR_SUBJECT, iss=_ACTOR_ISSUER),
    )

    def _gateway_missing(_row: PendingApprovalRow, _config: ServiceConfig) -> dict[str, object]:
        return {"error": _EXECUTOR_ERROR}

    # `_client` registered the recorder; swap in an executor that reports the missing gateway.
    executors._EXECUTORS[_TOOL_NAME] = _gateway_missing  # pyright: ignore[reportPrivateUsage]
    options = client.post(f"/approvals/{row.id}/sign/options", json={"code": code}).json()
    assertion = authenticator.build_assertion(
        rp_id=_RP_ID,
        origin=_ORIGIN,
        challenge=base64url_to_bytes(options["challenge"]),
        sign_count=1,
    )

    response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "signed", "error": _EXECUTOR_ERROR}
    stored = pending_store.get_by_id(row.id)
    assert stored is not None
    assert (stored.status, stored.outcome) == ("signed", {"error": _EXECUTOR_ERROR})
