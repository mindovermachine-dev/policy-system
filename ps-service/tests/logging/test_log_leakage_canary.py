"""Fake secrets driven through representative flows never reach any log sink.

Each flow runs against the real `configure()`d Logging facade (file sink plus stderr
mirror). After a flush, the file, captured stderr/stdout and the `logging` root records
at DEBUG are searched for every canary value. The canaries are obviously fake and carry
the `canary-` prefix.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import TYPE_CHECKING

import pytest
from api._fakes import build_fake_change_check_dependencies, install_no_principal
from audit._fakes import InMemoryAuditStore, audit_store_factory
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from invitations.test_client import (
    _RecordingTransport,  # pyright: ignore[reportPrivateUsage]  -- reuse `create_invitation`'s own fake-transport double rather than re-declaring it
)
from litellm.types.utils import Choices, Message, ModelResponse
from mcp.types import CallToolResult, TextContent
from mcp_interface._environment import graph_factory_for
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from mcp_interface.test_catalog_source_authz_gate import (
    _ACCESS_DENIED_MESSAGE,  # pyright: ignore[reportPrivateUsage]  -- reuse the gate tests' own fixtures rather than re-declaring them
    _CALLER_ISSUER,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from pydantic import BaseModel

from ps_service.api.dependencies import (
    provide_audit_store,
    provide_change_check_dependencies,
    provide_run_id,
)
from ps_service.api.error_handlers import register_exception_handlers
from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.invitations.client import create_invitation
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage
from ps_service.logging import bind_run_context, facade
from ps_service.main import create_app
from ps_service.mcp_interface import catalog_tools
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # the seeded-graph probe the fake must answer
)
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ps_test_support.mock_oidc_provider import MockOidcProvider

CANARY_TOKEN = "canary-token-1"
CANARY_PROMPT = "canary-prompt-1"
CANARY_POLICY = "canary-policy-1"
CANARY_USER = "canary-user-1"
CANARY_EMAIL = "canary-invitee-1@example.com"
CANARIES = (CANARY_TOKEN, CANARY_PROMPT, CANARY_POLICY, CANARY_USER, CANARY_EMAIL)
assert all(canary.startswith("canary-") for canary in CANARIES)

_AUDIENCE = "ps-service"


class _PolicyBody(BaseModel):
    """A request body whose `count` field rejects the canary policy text."""

    count: int


@pytest.fixture
def log_file(tmp_path: Path) -> Iterator[Path]:
    """A real default emitter writing under this test's own directory, mirroring failures."""
    path = tmp_path / "canary.jsonl"
    saved = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    facade.configure(log_path=path, mirror_failures_to_stderr=True)
    try:
        yield path
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = saved  # pyright: ignore[reportPrivateUsage]


def _everything_logged(
    log_path: Path, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> str:
    """The log file, stderr, stdout and every `logging` record, as one searchable text."""
    facade.reset_for_tests()  # drains the writer before the file is read
    captured = capfd.readouterr()
    written = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return f"{written}\n{captured.err}\n{captured.out}\n{caplog.text}"


def _assert_no_canary(blob: str) -> None:
    """Fail naming the first canary found in `blob`."""
    leaked = [canary for canary in CANARIES if canary in blob]
    assert not leaked, f"canary values reached a log sink: {leaked}"


def _verifier(provider: MockOidcProvider) -> PsTokenVerifier:
    """A verifier trusting `provider`, logging through the default facade emitter."""
    return PsTokenVerifier(
        AuthContext(
            issuer=provider.issuer,
            audience=_AUDIENCE,
            cli_client_id=None,
            scopes=(),
            jwks_uri=provider.jwks_uri,
            allowed_algorithms=frozenset({"RS256"}),
        )
    )


def test_rejected_bearer_token_when_verified_then_no_canary_in_any_sink(
    mock_oidc_provider: MockOidcProvider,
    log_file: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected token carrying a canary subject, and a garbage canary token, are never logged."""
    caplog.set_level(logging.DEBUG)
    expired = mock_oidc_provider.mint_token(sub=CANARY_USER, aud=_AUDIENCE, exp_delta=-3600)
    verifier = _verifier(mock_oidc_provider)

    assert asyncio.run(verifier.verify_token(expired)) is None
    assert asyncio.run(verifier.verify_token(CANARY_TOKEN)) is None

    _assert_no_canary(_everything_logged(log_file, capfd, caplog))


def test_accepted_bearer_token_with_canary_subject_then_neither_token_nor_subject_in_any_sink(
    mock_oidc_provider: MockOidcProvider,
    log_file: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither the raw token nor the subject of a successful authentication is logged."""
    caplog.set_level(logging.DEBUG)
    token = mock_oidc_provider.mint_token(sub=CANARY_USER, aud=_AUDIENCE)

    verified = asyncio.run(_verifier(mock_oidc_provider).verify_token(token))

    assert verified is not None
    assert verified.subject == CANARY_USER
    blob = _everything_logged(log_file, capfd, caplog)
    _assert_no_canary(blob)
    assert token not in blob
    assert "Bearer " not in blob


def test_unhandled_error_with_scrubbable_secret_shapes_then_no_canary_in_any_sink(
    log_file: Path, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Key=value, bearer and path shapes inside an exception message are redacted."""
    caplog.set_level(logging.DEBUG)
    message = (
        f"upstream rejected api_key={CANARY_TOKEN} with Authorization: Bearer {CANARY_TOKEN} "
        f"at https://host.example/{CANARY_TOKEN}?password={CANARY_TOKEN}"
    )
    app = FastAPI()
    register_exception_handlers(app)

    async def _boom(_run_id: str = Depends(provide_run_id)) -> None:
        raise RuntimeError(message)

    app.add_api_route("/boom", _boom, methods=["GET"])

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    _assert_no_canary(_everything_logged(log_file, capfd, caplog) + response.text)


def test_invalid_request_body_with_canary_policy_text_then_not_in_any_sink(
    log_file: Path, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A request rejected by validation does not echo the submitted text into the logs."""
    caplog.set_level(logging.DEBUG)
    app = FastAPI()
    register_exception_handlers(app)

    async def _accept(body: _PolicyBody) -> dict[str, int]:
        return {"count": body.count}

    app.add_api_route("/policy", _accept, methods=["POST"])

    response = TestClient(app, raise_server_exceptions=False).post(
        "/policy", json={"count": CANARY_POLICY}
    )

    assert response.status_code == 422
    _assert_no_canary(_everything_logged(log_file, capfd, caplog))


def test_llm_completion_with_canary_prompt_then_prompt_not_in_any_sink(
    log_file: Path, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The prompt and the reply of a successful completion are never logged."""
    caplog.set_level(logging.DEBUG)
    reply = ModelResponse(
        id="x",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop",
                index=0,
                message=Message(content=f"{CANARY_POLICY} reply", role="assistant"),
            )
        ],
    )

    def _fake_call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        return reply

    with bind_run_context("run-canary-1"):
        route_completion(
            [ChatMessage(role="user", content=CANARY_PROMPT)],
            model="fake-model",
            call_completion=_fake_call,
        )

    _assert_no_canary(_everything_logged(log_file, capfd, caplog))


def test_llm_completion_failing_with_canary_prompt_in_error_then_not_in_any_sink(
    log_file: Path, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A provider error that echoes the prompt does not put the prompt in the logs."""
    caplog.set_level(logging.DEBUG)

    def _failing_call(
        *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        raise RuntimeError(f"provider echoed prompt {CANARY_PROMPT} api_key={CANARY_TOKEN}")

    with bind_run_context("run-canary-2"), pytest.raises(Exception, match="canary"):
        route_completion(
            [ChatMessage(role="user", content=CANARY_PROMPT)],
            model="fake-model",
            call_completion=_failing_call,
        )

    _assert_no_canary(_everything_logged(log_file, capfd, caplog))


def test_denied_unverified_rest_request_with_canary_in_body_then_no_canary_in_any_sink(
    monkeypatch: pytest.MonkeyPatch,
    log_file: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unverified, denied request logs no part of its body, and writes no audit row."""
    caplog.set_level(logging.DEBUG)
    install_no_principal(monkeypatch)
    store = InMemoryAuditStore()
    fake = build_fake_change_check_dependencies(tracked=())
    app = create_app(
        ServiceConfig(
            host="127.0.0.1",
            port=8000,
            graceful_shutdown_seconds=10,
            logging_dir=None,
            is_local_test_bypass_active=True,
            authentik_api_token="test-authentik-token",
            authentik_base_url="https://authentik.example.com",
        )
    )
    app.dependency_overrides[provide_change_check_dependencies] = lambda: fake.dependencies
    app.dependency_overrides[provide_audit_store] = lambda: store

    response = TestClient(app, raise_server_exceptions=False).post(
        "/change-checks", json={"note": CANARY_POLICY, "token": CANARY_TOKEN, "who": CANARY_USER}
    )

    assert response.status_code == 403
    assert store.rows == []
    _assert_no_canary(_everything_logged(log_file, capfd, caplog) + response.text)


class _SeededResult:
    header: list[list[object]] = [[0, "c"]]  # noqa: RUF012
    result_set: list[object] = [[1]]  # noqa: RUF012


class _FailingGraph:
    """A seeded graph whose user query fails with text that echoes the query and a secret."""

    def query(self, q: str, params: object = None, timeout: int | None = None) -> object:
        del params, timeout
        if q == _SEED_CHECK_QUERY:
            return _SeededResult()
        raise RuntimeError(f"syntax error in {q} api_key={CANARY_TOKEN}")


class _FailingDatabase:
    def select_graph(self, name: str) -> _FailingGraph:
        del name
        return _FailingGraph()


def test_cypher_with_canary_query_then_no_canary_in_any_sink(
    monkeypatch: pytest.MonkeyPatch,
    log_file: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The `run-cypher` tool's query text and database error text reach no log sink or audit row."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    store = InMemoryAuditStore()
    _override(monkeypatch, audit_store_factory=audit_store_factory(store))

    _override(monkeypatch, graph_factory=graph_factory_for(_FailingDatabase()))

    result = asyncio.run(
        _server().call_tool(
            "run-cypher", {"query": f"MATCH (n) WHERE n.name = '{CANARY_POLICY}' RETURN n"}
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert [(row.action, row.outcome) for row in store.rows] == [("query.cypher", "failed")]
    _assert_no_canary(_everything_logged(log_file, capfd, caplog) + repr(store.rows))


class _SeededGraph:
    """A seeded graph that answers every query with one row."""

    def query(self, q: str, params: object = None, timeout: int | None = None) -> object:
        del q, params, timeout
        return _SeededResult()


class _SeededDatabase:
    def select_graph(self, name: str) -> _SeededGraph:
        del name
        return _SeededGraph()


def _tool_text(result: object) -> str:
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_mcp_tool_calls_by_a_canary_principal_then_no_canary_in_any_sink(
    monkeypatch: pytest.MonkeyPatch,
    log_file: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A verified canary caller's admitted `run-cypher` call and refused invite are never logged."""
    caplog.set_level(logging.DEBUG)
    store = InMemoryAuditStore()
    _override(monkeypatch, audit_store_factory=audit_store_factory(store))
    _override(monkeypatch, role_store_factory=_fake_store_factory(_seeded_store()))

    _override(monkeypatch, graph_factory=graph_factory_for(_SeededDatabase()))

    with _verified_actor(sub=CANARY_USER):
        admitted = asyncio.run(
            _server().call_tool("run-cypher", {"query": "MATCH (n) RETURN count(n) AS c"})
        )
        refused = asyncio.run(_server().call_tool("invite-user", {"email": CANARY_EMAIL}))

    assert isinstance(admitted, CallToolResult)
    assert admitted.is_error is False
    assert _tool_text(refused) == _ACCESS_DENIED_MESSAGE
    [cypher_row] = [row for row in store.rows if row.action == "query.cypher"]
    assert (cypher_row.actor_subject, cypher_row.outcome) == (CANARY_USER, "applied")
    [gate_row] = [row for row in store.rows if row.action == "access.gate"]
    assert gate_row.actor_subject == CANARY_USER
    _assert_no_canary(_everything_logged(log_file, capfd, caplog))


def test_invite_user_with_canary_email_then_no_canary_in_any_sink(
    monkeypatch: pytest.MonkeyPatch,
    log_file: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A canary SystemAdmin inviting a canary email puts neither value in any log sink."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", "test-authentik-token")
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", "https://authentik.example.com")
    store = InMemoryAuditStore()
    _override(monkeypatch, audit_store_factory=audit_store_factory(store))
    roles = _seeded_store()
    roles.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(CANARY_USER, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    _override(monkeypatch, role_store_factory=_fake_store_factory(roles))
    transport = _RecordingTransport(json.dumps({"pk": "tok-canary"}).encode())
    monkeypatch.setattr(
        catalog_tools,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )

    with _verified_actor(sub=CANARY_USER):
        result = asyncio.run(_server().call_tool("invite-user", {"email": CANARY_EMAIL}))

    assert json.loads(_tool_text(result))["itoken"] == "tok-canary"
    [invite_row] = [row for row in store.rows if row.action == "user.invite"]
    assert (invite_row.actor_subject, invite_row.resource_id) == (CANARY_USER, CANARY_EMAIL)
    _assert_no_canary(_everything_logged(log_file, capfd, caplog))
