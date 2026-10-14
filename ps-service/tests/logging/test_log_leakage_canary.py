"""Fake secrets driven through representative flows never reach any log sink.

Each flow runs against the real `configure()`d Logging facade (file sink plus stderr
mirror). After a flush, the file, captured stderr/stdout and the `logging` root records
at DEBUG are searched for every canary value. The canaries are obviously fake and carry
the `canary-` prefix.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from litellm.types.utils import Choices, Message, ModelResponse
from pydantic import BaseModel

from ps_service.api.dependencies import provide_run_id
from ps_service.api.error_handlers import register_exception_handlers
from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage
from ps_service.logging import bind_run_context, facade
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
CANARIES = (CANARY_TOKEN, CANARY_PROMPT, CANARY_POLICY, CANARY_USER)
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


def test_accepted_bearer_token_when_verified_then_token_not_in_any_sink(
    mock_oidc_provider: MockOidcProvider,
    log_file: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The raw token of a successful authentication is never logged."""
    caplog.set_level(logging.DEBUG)
    token = mock_oidc_provider.mint_token(sub="subject-1", aud=_AUDIENCE)

    assert asyncio.run(_verifier(mock_oidc_provider).verify_token(token)) is not None

    blob = _everything_logged(log_file, capfd, caplog)
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
