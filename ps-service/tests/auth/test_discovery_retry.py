"""OIDC discovery retry at startup: bounded budget, backoff, fail closed.

`resolve_auth_context` is driven with a scripted transport plus an injected `sleep` and
`monotonic`, so no test waits in real time.
"""

from __future__ import annotations

import inspect
import json
import time
import urllib.error
import urllib.request
from typing import Self

import pytest

from ps_service.auth.errors import AuthConfigurationError, AuthDiscoveryError
from ps_service.auth.models import AuthContext
from ps_service.auth.startup import resolve_auth_context
from ps_service.config import ServiceConfig

_ISSUER = "https://issuer.example.com"
_AUDIENCE = "https://api.example.com"
_VALID_DOCUMENT: dict[str, object] = {
    "jwks_uri": f"{_ISSUER}/jwks.json",
    "id_token_signing_alg_values_supported": ["RS256"],
}


def _config(**overrides: object) -> ServiceConfig:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "is_local_test_bypass_active": False,
        "auth_issuer": _ISSUER,
        "auth_audience": _AUDIENCE,
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


class _FakeClock:
    """A clock that only moves when the injected `sleep` is called."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _FlakyTransport:
    """Fails with `URLError` for the first `failures` calls (or forever), then serves `document`."""

    def __init__(self, document: dict[str, object], *, failures: int | None) -> None:
        self._body = json.dumps(document).encode("utf-8")
        self._failures = failures
        self.calls = 0
        self.last_error: urllib.error.URLError | None = None

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _Response:
        self.calls += 1
        if self._failures is None or self.calls <= self._failures:
            self.last_error = urllib.error.URLError(f"connection refused ({self.calls})")
            raise self.last_error
        return _Response(self._body)


def test_retries_transient_discovery_failure_then_starts() -> None:
    clock = _FakeClock()
    transport = _FlakyTransport(_VALID_DOCUMENT, failures=2)

    context = resolve_auth_context(
        _config(auth_discovery_retry_budget_seconds=60),
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert isinstance(context, AuthContext)
    assert transport.calls == 3
    assert clock.sleeps == [1, 2]


def test_raises_auth_discovery_error_naming_issuer_when_budget_is_exhausted() -> None:
    clock = _FakeClock()
    transport = _FlakyTransport(_VALID_DOCUMENT, failures=None)

    with pytest.raises(AuthDiscoveryError) as excinfo:
        resolve_auth_context(
            _config(auth_discovery_retry_budget_seconds=10),
            transport=transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert _ISSUER in str(excinfo.value)
    assert excinfo.value.__cause__ is transport.last_error
    assert sum(clock.sleeps) <= 10
    assert clock.now - 1000.0 == sum(clock.sleeps)
    assert transport.calls == len(clock.sleeps) + 1


def test_backoff_doubles_and_is_capped_at_thirty_seconds() -> None:
    clock = _FakeClock()
    transport = _FlakyTransport(_VALID_DOCUMENT, failures=None)

    with pytest.raises(AuthDiscoveryError):
        resolve_auth_context(
            _config(auth_discovery_retry_budget_seconds=300),
            transport=transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.sleeps[:7] == [1, 2, 4, 8, 16, 30, 30]
    assert max(clock.sleeps) <= 30
    assert sum(clock.sleeps) == 300


def test_budget_zero_makes_a_single_attempt() -> None:
    clock = _FakeClock()
    transport = _FlakyTransport(_VALID_DOCUMENT, failures=None)

    with pytest.raises(AuthDiscoveryError) as excinfo:
        resolve_auth_context(
            _config(auth_discovery_retry_budget_seconds=0),
            transport=transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert _ISSUER in str(excinfo.value)
    assert transport.calls == 1
    assert clock.sleeps == []


def test_does_not_retry_when_issuer_is_unset() -> None:
    clock = _FakeClock()
    transport = _FlakyTransport(_VALID_DOCUMENT, failures=None)

    with pytest.raises(AuthConfigurationError):
        resolve_auth_context(
            _config(auth_issuer=None, auth_discovery_retry_budget_seconds=60),
            transport=transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert transport.calls == 0
    assert clock.sleeps == []


def test_does_not_retry_a_document_without_jwks_uri() -> None:
    clock = _FakeClock()
    transport = _FlakyTransport({"id_token_signing_alg_values_supported": ["RS256"]}, failures=0)

    with pytest.raises(AuthDiscoveryError) as excinfo:
        resolve_auth_context(
            _config(auth_discovery_retry_budget_seconds=60),
            transport=transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert "jwks_uri" in str(excinfo.value)
    assert transport.calls == 1
    assert clock.sleeps == []


def test_does_not_retry_a_document_without_supported_algorithms() -> None:
    clock = _FakeClock()
    transport = _FlakyTransport(
        {"jwks_uri": f"{_ISSUER}/jwks.json", "id_token_signing_alg_values_supported": ["HS256"]},
        failures=0,
    )

    with pytest.raises(AuthDiscoveryError):
        resolve_auth_context(
            _config(auth_discovery_retry_budget_seconds=60),
            transport=transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert transport.calls == 1
    assert clock.sleeps == []


def test_each_failed_attempt_is_reported_on_stderr_without_raw_error_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _FakeClock()
    transport = _FlakyTransport(_VALID_DOCUMENT, failures=1)

    resolve_auth_context(
        _config(auth_discovery_retry_budget_seconds=60),
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    captured = capsys.readouterr().err
    assert "oidc_discovery" in captured
    assert "retrying" in captured
    assert "URLError" in captured
    assert "connection refused" not in captured


def test_exhausted_budget_sleeps_exactly_the_budget_on_the_injected_clock() -> None:
    clock = _FakeClock()

    def sleep_must_be_injected(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now - 1000.0 > _config().auth_discovery_retry_budget_seconds + 1:
            message = "real sleep reached"
            raise AssertionError(message)

    transport = _FlakyTransport(_VALID_DOCUMENT, failures=None)

    with pytest.raises(AuthDiscoveryError):
        resolve_auth_context(
            _config(),
            transport=transport,
            sleep=sleep_must_be_injected,
            monotonic=clock.monotonic,
        )

    assert sum(clock.sleeps) == _config().auth_discovery_retry_budget_seconds
    parameters = inspect.signature(resolve_auth_context).parameters
    assert parameters["sleep"].default is time.sleep
    assert parameters["monotonic"].default is time.monotonic
