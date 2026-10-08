"""ComplianceOfficer gate on `start_ingestion` and `get_ingestion_status` (issue #194, S2).

AC-BI-001: a caller below `ComplianceOfficer` is denied by `start_ingestion` before any
`ingestion_runs` row, run slot or pipeline stage exists. AC-BI-002: the same denial from
`get_ingestion_status`, whether or not the run id exists, before any store read. Each denial
text is compared against what `ingest_regulation` returns for the same caller.

Local copies of `test_ingest_regulation_authz_gate.py`'s `_verified_actor` /
`_fake_store_factory` / `_seeded_store` helpers (that file is a deliberately self-contained
precedent); every test binds a real, non-bypass `AccessToken` so the gate genuinely runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_ingest_regulation_authz_gate.py`
    FakeAccessRoleStore,
    RaisingAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from mcp_interface._ingestion_run_harness import (
    body,
    install_ingestion_run_store,
    isolate_ingestion_runs,
    wait_for_run,
)
from mcp_interface.test_ingest_regulation_tool import (
    _CELEX,  # pyright: ignore[reportPrivateUsage]  -- same reuse as `test_ingest_regulation_authz_gate.py`
    _SHORT_NAME,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _configure_complete_llm_env,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _use_real_pipeline_stages,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.authz.models import AccessRole
from ps_service.ingestion_runs import dispatch
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from ingestion_runs._fakes import InMemoryIngestionRunStore

_SYSTEM_OWNER_SUBJECT = "existing-system-owner"
_NON_ADMIN_SUBJECT = "authenticated-user-only-caller"
_SYSTEM_ADMIN_SUBJECT = "system-admin-without-grant"
_COMPLIANCE_OFFICER_SUBJECT = "granted-compliance-officer"
_CALLER_ISSUER = "https://issuer.example.com/"
_NEW_TOOLS = ("start_ingestion", "get_ingestion_status")
_ACCESS_DENIED_MESSAGE = "error: You do not have the required access role for this action."
_AUTHENTICATED_CALLER_MESSAGE = "error: this action requires a real authenticated caller"
_SEEDED_RUN_ID = str(uuid.uuid4())


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _CALLER_ISSUER) -> Generator[None]:
    """Bind a real, verified `AccessToken`; never sets the local-test bypass."""
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _fake_store_factory(store: object) -> object:
    def _factory(_config: object, **_kwargs: object) -> object:
        return store

    return _factory


def _seeded_store() -> FakeAccessRoleStore:
    """One bootstrapped `SystemOwner` under a different subject, so later callers default to
    `AuthenticatedUser` alone.
    """
    store = FakeAccessRoleStore(expected_owner=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER))
    store.bootstrap_first_owner((_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER))
    return store


def _arguments(tool: str) -> dict[str, str]:
    if tool == "get_ingestion_status":
        return {"run_id": _SEEDED_RUN_ID}
    return {"celex": _CELEX, "short_name": _SHORT_NAME}


def _call(tool: str) -> str:
    result = asyncio.run(mcp_server.server.call_tool(tool, _arguments(tool)))
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture(autouse=True)
def _isolate(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture
) -> Iterator[None]:
    isolate_ingestion_runs()
    yield
    isolate_ingestion_runs()


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> InMemoryIngestionRunStore:
    """The faked run store, seeded with one `running` row (for the existing-run-id denial)."""
    _configure_complete_llm_env(monkeypatch)
    configure()
    store = install_ingestion_run_store(monkeypatch)
    store.create_run(
        run_id=_SEEDED_RUN_ID, celex=_CELEX, short_name=_SHORT_NAME, actor=("someone", "else")
    )
    return store


def _assert_no_run_was_started(runs: InMemoryIngestionRunStore) -> None:
    assert list(runs.rows) == [_SEEDED_RUN_ID]
    assert dispatch.in_flight_run_count() == 0


@pytest.mark.parametrize("tool", [*_NEW_TOOLS, "ingest_regulation"])
@pytest.mark.parametrize("role", [None, AccessRole.SYSTEM_ADMIN])
def test_callers_below_compliance_officer_get_the_same_denial_as_ingest_regulation(
    tool: str,
    role: AccessRole | None,
    runs: InMemoryIngestionRunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-001 / AC-BI-002: AuthenticatedUser-only and SystemAdmin-without-grant are denied."""
    access = _seeded_store()
    subject = _NON_ADMIN_SUBJECT
    if role is not None:
        subject = _SYSTEM_ADMIN_SUBJECT
        access.grant(
            actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
            target=(subject, _CALLER_ISSUER),
            access_role=role,
        )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(access))

    with _verified_actor(sub=subject):
        assert _call(tool) == _ACCESS_DENIED_MESSAGE

    _assert_no_run_was_started(runs)
    assert runs.get_run_calls == 0


@pytest.mark.parametrize("tool", [*_NEW_TOOLS, "ingest_regulation"])
def test_an_access_store_outage_fails_closed_for_every_tool(
    tool: str, runs: InMemoryIngestionRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(RaisingAccessRoleStore())
    )

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        assert _call(tool) == "error: The authorization store is temporarily unavailable."

    _assert_no_run_was_started(runs)
    assert runs.get_run_calls == 0


@pytest.mark.parametrize("tool", [*_NEW_TOOLS, "ingest_regulation"])
def test_no_token_with_the_bypass_off_requires_a_real_authenticated_caller(
    tool: str, runs: InMemoryIngestionRunStore
) -> None:
    assert _call(tool) == _AUTHENTICATED_CALLER_MESSAGE

    _assert_no_run_was_started(runs)
    assert runs.get_run_calls == 0


def test_status_denial_is_identical_for_an_unseen_and_an_existing_run_id(
    runs: InMemoryIngestionRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-002: the denial never reveals whether a run id exists, and no read happens first."""
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(_seeded_store()))
    unseen = str(uuid.uuid4())

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        existing_denial = _call("get_ingestion_status")
        unseen_denial = asyncio.run(
            mcp_server.server.call_tool("get_ingestion_status", {"run_id": unseen})
        )

    assert isinstance(unseen_denial, CallToolResult)
    block = unseen_denial.content[0]
    assert isinstance(block, TextContent)
    assert existing_denial == block.text == _ACCESS_DENIED_MESSAGE
    assert runs.get_run_calls == 0


@pytest.mark.parametrize("tool", _NEW_TOOLS)
def test_a_granted_compliance_officer_reaches_the_tool_body(
    tool: str, runs: InMemoryIngestionRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    access = _seeded_store()
    access.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_COMPLIANCE_OFFICER_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(access))
    _use_real_pipeline_stages(monkeypatch)

    with _verified_actor(sub=_COMPLIANCE_OFFICER_SUBJECT):
        result = asyncio.run(mcp_server.server.call_tool(tool, _arguments(tool)))

    assert isinstance(result, CallToolResult)
    payload = body(result)
    if tool == "start_ingestion":
        assert payload["status"] == "running"
        run_id = str(payload["run_id"])
        wait_for_run(run_id)
        row = runs.rows[run_id]
        assert row.status == "succeeded"
        assert (row.actor_subject, row.actor_issuer) == (
            _COMPLIANCE_OFFICER_SUBJECT,
            _CALLER_ISSUER,
        )
    else:
        assert payload["run_id"] == _SEEDED_RUN_ID
        # The seeded `running` row has no worker in this process, so the poll reconciles it
        # (issue #194 S5) -- proof the granted caller reached the body and read the row.
        assert payload["status"] == "failed"
