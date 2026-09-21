"""AC-BI-014/015/016: the auth audit-logging contract (issue #58, Slice 10).

`PsTokenVerifier.verify_token` is THE single logging choke point for every
authentication outcome (PLAN.md §1.2): on success it emits
``component="auth", action="authenticate_request", outcome="success"``
with ``extra={"sub", "iss"}`` (AC-BI-014); on any rejection it emits
``outcome="unauthenticated"`` with ``extra={"reason": <category>}`` drawn
from the closed six-category set (AC-BI-015). Across every one of those
entries, the raw token string (and the literal ``"Bearer "`` prefix) must
never appear anywhere in the serialized log line (AC-BI-016) -- proven once,
generically, across all seven verification outcomes (six failure categories
plus success), not hoped for per call site.

Exercises `PsTokenVerifier` directly against the real
`tests.auth.mock_oidc_provider.MockOidcProvider` (AC-BI-017), using the
repo's real `make_emitter`/`read_lines` fixtures (`tests/conftest.py`) --
a real `LogEmitter` writing JSON lines to a temp file, read back and parsed
-- exactly like `tests/query_engine/test_execute_cypher_query_logging.py`'s
own convention for this component's logging tests.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import jwt

from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_test_support.mock_oidc_provider import (
    MockOidcProvider,
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]
    type ReadLines = Callable[[Path], list[dict[str, object]]]

_AUDIENCE = "ps-service"
_ALLOWED_ALGORITHMS = frozenset({"RS256"})

# A deliberately distinctive marker embedded in every minted token's `sub`,
# so the "never in logs" assertion has something recognizable to search for
# beyond the raw JWT string itself.
_DISTINCTIVE_SUB = "audit-log-canary-subject-4f9c2b"


def _verifier(
    provider: MockOidcProvider, *, emitter: LogEmitter, audience: str = _AUDIENCE
) -> PsTokenVerifier:
    auth_context = AuthContext(
        issuer=provider.issuer,
        audience=audience,
        cli_client_id=None,
        scopes=(),
        jwks_uri=provider.jwks_uri,
        allowed_algorithms=_ALLOWED_ALGORITHMS,
    )
    return PsTokenVerifier(auth_context, emitter=emitter)


def _assert_token_never_logged(lines: list[dict[str, object]], token: str) -> None:
    """AC-BI-016: the raw token string, and the literal `"Bearer "` prefix,
    must never appear anywhere in any captured log entry's serialized form.
    """
    for entry in lines:
        serialized = json.dumps(entry)
        assert token not in serialized
        assert "Bearer " not in serialized
        for value in entry.values():
            assert value != token


def test_successful_verification_logs_sub_and_iss(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = mock_oidc_provider.mint_token(sub=_DISTINCTIVE_SUB, aud=_AUDIENCE)

    result = asyncio.run(verifier.verify_token(token))
    emitter.flush()

    assert result is not None
    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["component"] == "auth"
    assert entry["action"] == "authenticate_request"
    assert entry["outcome"] == "success"
    assert entry["sub"] == _DISTINCTIVE_SUB
    assert entry["iss"] == mock_oidc_provider.issuer
    _assert_token_never_logged(lines, token)


def test_missing_token_logs_reason_missing(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)

    result = asyncio.run(verifier.verify_token(""))
    emitter.flush()

    assert result is None
    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["outcome"] == "unauthenticated"
    assert entry["reason"] == "missing"


def test_expired_token_logs_reason_expired(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = mock_oidc_provider.mint_token(sub=_DISTINCTIVE_SUB, aud=_AUDIENCE, exp_delta=-3600.0)

    result = asyncio.run(verifier.verify_token(token))
    emitter.flush()

    assert result is None
    lines = read_lines(log_path)
    entry = lines[-1]
    assert entry["outcome"] == "unauthenticated"
    assert entry["reason"] == "expired"
    _assert_token_never_logged(lines, token)


def test_immature_token_also_logs_reason_expired(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-015 enumerates no separate `nbf` bucket -- both temporal-validity
    failures (expired, not-yet-valid) share the `expired` category
    (PLAN.md §1.2's mapping table).
    """
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    now = int(time.time())
    token = mock_oidc_provider.mint_token(
        sub=_DISTINCTIVE_SUB, aud=_AUDIENCE, extra_claims={"nbf": now + 3600}
    )

    result = asyncio.run(verifier.verify_token(token))
    emitter.flush()

    assert result is None
    lines = read_lines(log_path)
    entry = lines[-1]
    assert entry["outcome"] == "unauthenticated"
    assert entry["reason"] == "expired"
    _assert_token_never_logged(lines, token)


def test_wrong_audience_token_logs_reason_wrong_audience(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = mock_oidc_provider.mint_token(sub=_DISTINCTIVE_SUB, aud="someone-else")

    result = asyncio.run(verifier.verify_token(token))
    emitter.flush()

    assert result is None
    lines = read_lines(log_path)
    entry = lines[-1]
    assert entry["outcome"] == "unauthenticated"
    assert entry["reason"] == "wrong_audience"
    _assert_token_never_logged(lines, token)


def test_wrong_issuer_token_logs_reason_wrong_issuer(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = mock_oidc_provider.mint_token(
        sub=_DISTINCTIVE_SUB, aud=_AUDIENCE, iss="https://not-the-real-issuer.example"
    )

    result = asyncio.run(verifier.verify_token(token))
    emitter.flush()

    assert result is None
    lines = read_lines(log_path)
    entry = lines[-1]
    assert entry["outcome"] == "unauthenticated"
    assert entry["reason"] == "wrong_issuer"
    _assert_token_never_logged(lines, token)


def test_bad_alg_token_logs_reason_bad_alg(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = jwt.encode(
        {"sub": _DISTINCTIVE_SUB, "aud": _AUDIENCE, "iss": mock_oidc_provider.issuer},
        "",
        algorithm="none",
    )

    result = asyncio.run(verifier.verify_token(token))
    emitter.flush()

    assert result is None
    lines = read_lines(log_path)
    entry = lines[-1]
    assert entry["outcome"] == "unauthenticated"
    assert entry["reason"] == "bad_alg"
    _assert_token_never_logged(lines, token)


def test_untrusted_signature_token_logs_reason_invalid_signature(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """A token with the right `iss`/`aud` shape but signed by an independent
    provider's key -- rejected on signature, never on `kid` lookup failure.
    """
    emitter, log_path = make_emitter()
    other_provider = MockOidcProvider()
    try:
        verifier = _verifier(mock_oidc_provider, emitter=emitter)
        token = other_provider.mint_token(
            sub=_DISTINCTIVE_SUB, aud=_AUDIENCE, iss=mock_oidc_provider.issuer
        )

        result = asyncio.run(verifier.verify_token(token))
        emitter.flush()

        assert result is None
        lines = read_lines(log_path)
        entry = lines[-1]
        assert entry["outcome"] == "unauthenticated"
        assert entry["reason"] == "invalid_signature"
        _assert_token_never_logged(lines, token)
    finally:
        other_provider.shutdown()


def test_unknown_kid_after_refetch_logs_reason_invalid_signature(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = mock_oidc_provider.mint_token(
        sub=_DISTINCTIVE_SUB, aud=_AUDIENCE, kid="never-published-kid"
    )

    result = asyncio.run(verifier.verify_token(token))
    emitter.flush()

    assert result is None
    lines = read_lines(log_path)
    entry = lines[-1]
    assert entry["outcome"] == "unauthenticated"
    assert entry["reason"] == "invalid_signature"
    _assert_token_never_logged(lines, token)


def test_malformed_token_logs_reason_invalid_signature(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)
    token = "not-a-real-jwt-at-all"

    result = asyncio.run(verifier.verify_token(token))
    emitter.flush()

    assert result is None
    lines = read_lines(log_path)
    entry = lines[-1]
    assert entry["outcome"] == "unauthenticated"
    assert entry["reason"] == "invalid_signature"
    _assert_token_never_logged(lines, token)


def test_token_never_appears_across_every_captured_outcome(
    mock_oidc_provider: MockOidcProvider, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-016, proven once generically across success and all six failure
    categories in a single shared log file, rather than hoped-for per test.
    """
    emitter, log_path = make_emitter()
    verifier = _verifier(mock_oidc_provider, emitter=emitter)

    tokens = [
        mock_oidc_provider.mint_token(sub=_DISTINCTIVE_SUB, aud=_AUDIENCE),  # success
        "",  # missing
        mock_oidc_provider.mint_token(sub=_DISTINCTIVE_SUB, aud=_AUDIENCE, exp_delta=-3600.0),
        mock_oidc_provider.mint_token(sub=_DISTINCTIVE_SUB, aud="someone-else"),
        mock_oidc_provider.mint_token(
            sub=_DISTINCTIVE_SUB, aud=_AUDIENCE, iss="https://not-the-real-issuer.example"
        ),
        jwt.encode(
            {"sub": _DISTINCTIVE_SUB, "aud": _AUDIENCE, "iss": mock_oidc_provider.issuer},
            "",
            algorithm="none",
        ),
        mock_oidc_provider.mint_token(
            sub=_DISTINCTIVE_SUB, aud=_AUDIENCE, kid="never-published-kid"
        ),
    ]

    for token in tokens:
        asyncio.run(verifier.verify_token(token))
    emitter.flush()

    lines = read_lines(log_path)
    assert len(lines) == len(tokens)
    for token in tokens:
        if token:
            _assert_token_never_logged(lines, token)
