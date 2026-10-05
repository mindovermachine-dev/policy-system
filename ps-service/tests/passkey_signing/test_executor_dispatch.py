"""Approval-executor registry and `sign/verify` dispatch (issue #190, slice 10 sub-step c).

A signed approval runs the executor registered for its `tool_name`; the outcome the
executor returns is stored on the row. An unregistered `tool_name`, or an executor that
raises, stores a generic safe error and executes nothing (the signature stays consumed).
Near-miss rows keep their original path (`test_signing_ceremony.py`, unchanged).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from passkey_signing._harness import (
    ACTOR_ISSUER,
    ACTOR_SUBJECT,
    ORIGIN,
    RP_ID,
    Authenticator,
    enroll,
    sign_challenge,
)
from ps_service.api.dependencies import provide_pending_approval_store
from ps_service.config import ServiceConfig
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing import executors
from ps_service.passkey_signing.router import provide_signing_credential_store

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.passkey_signing.models import PendingApprovalRow

_TOOL = "test-dispatch-tool"
_GENERIC = (
    "this action could not be completed; if you still intend to proceed, ask for a new approval"
)


@pytest.fixture(autouse=True)
def isolated_registry() -> Iterator[None]:
    configure()
    snapshot = dict(executors._EXECUTORS)  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        executors._EXECUTORS.clear()  # pyright: ignore[reportPrivateUsage]
        executors._EXECUTORS.update(snapshot)  # pyright: ignore[reportPrivateUsage]


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
        authentik_api_token="t",
        authentik_base_url="https://authentik.example.com",
    )


def _sign(tool_name: str) -> tuple[dict[str, object], FakePendingApprovalStore, PendingApprovalRow]:
    pending = FakePendingApprovalStore()
    credentials = FakeSigningCredentialStore()
    row, code = pending.create_pending_approval(
        tool_name=tool_name,
        normalized_args={"x": 1},
        actor_subject=ACTOR_SUBJECT,
        actor_issuer=ACTOR_ISSUER,
        display_summary={},
    )
    authenticator = Authenticator()
    enroll(credentials, authenticator)
    app = create_app(_config())
    app.dependency_overrides[provide_pending_approval_store] = lambda: pending
    app.dependency_overrides[provide_signing_credential_store] = lambda: credentials
    client = TestClient(app)
    assertion = authenticator.build_assertion(
        rp_id=RP_ID, origin=ORIGIN, challenge=sign_challenge(row), sign_count=1
    )
    response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )
    assert response.status_code == 200
    return response.json(), pending, row


def test_a_registered_executor_runs_with_the_row_and_its_outcome_is_stored() -> None:
    seen: list[str] = []

    def _executor(row: PendingApprovalRow, config: ServiceConfig) -> dict[str, object]:
        seen.append(row.tool_name)
        assert config.port == 8000
        return {"done": True}

    executors.register_approval_executor(_TOOL, _executor)

    body, pending, row = _sign(_TOOL)

    assert seen == [_TOOL]
    assert body == {"status": "signed", "done": True}
    stored = pending.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "signed"
    assert stored.outcome == {"done": True}


def test_an_unregistered_tool_name_stores_a_generic_error_and_runs_nothing() -> None:
    body, pending, row = _sign("never-registered")

    assert body["status"] == "signed"
    assert body["error"] == _GENERIC
    stored = pending.get_by_id(row.id)
    assert stored is not None
    assert stored.outcome == {"error": _GENERIC}


def test_an_executor_that_raises_stores_a_generic_error_without_internal_detail() -> None:
    def _boom(row: PendingApprovalRow, config: ServiceConfig) -> dict[str, object]:
        del row, config
        message = "host=10.1.2.3 password=hunter2"
        raise RuntimeError(message)

    executors.register_approval_executor(_TOOL, _boom)

    body, pending, row = _sign(_TOOL)

    assert body == {"status": "signed", "error": _GENERIC}
    stored = pending.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "signed"
    assert "10.1.2.3" not in str(stored.outcome)


def test_registering_a_tool_name_twice_is_rejected() -> None:
    executors.register_approval_executor(_TOOL, lambda _row, _config: {})

    with pytest.raises(ValueError, match="already registered"):
        executors.register_approval_executor(_TOOL, lambda _row, _config: {})


def test_effect_verifier_registry_round_trips() -> None:
    def _verifier(row: PendingApprovalRow, graph: object) -> bool:
        del row, graph
        return True

    executors.register_effect_verifier(_TOOL, _verifier)

    assert executors.resolve_effect_verifier(_TOOL) is _verifier
    assert executors.resolve_effect_verifier("unknown-tool") is None
    executors._VERIFIERS.pop(_TOOL)  # pyright: ignore[reportPrivateUsage]
