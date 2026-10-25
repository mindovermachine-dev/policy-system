"""Tests for ps_cli.modules.handlers.

`handle_ingest_document`'s full happy-path wiring is
deliberately not unit-tested at this layer -- its coverage there is exactly two
`cli.run()`-level tests in `ps-cli/tests/test_cli.py`, which prove the full
wiring (parser -> dispatch -> handler -> client) end to end. Exactly one test at
this layer covers the local-validation-before-any-network-call behavior
(`validate_local_seed_file`) -- distinct from
happy-path wiring, and the one place a fake client can assert zero calls were
made.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli.credentials import TokenBundle
from ps_cli.errors import PsCliError
from ps_cli.http_client import PsServiceClient
from ps_cli.models import (
    ExportResult,
    IngestionResult,
    ServiceStatus,
    StageOutcome,
    StatusDetail,
)
from ps_cli.modules.handlers import (
    handle_get_health,
    handle_ingest_document,
)

if TYPE_CHECKING:
    from pathlib import Path

_PS_SERVICE_HOST = "127.0.0.1"
_SERVICE_URL = f"http://{_PS_SERVICE_HOST}:8000"
_FAKE_ISSUER = "https://issuer.example"
# The status and probe paths PS Service serves, plus the two probe paths it does not serve,
# so a test can assert which of them the client requests.
_STATUS_AND_PROBE_PATHS = frozenset(
    {"/status", "/livez", "/readyz", *(f"/{name}" for name in ("health", "ready"))}
)


class _UnusedPsServiceClientMethods:
    """Base for hand-written `PsServiceClientProtocol` fakes below.

    Every method raises unless a subclass overrides it -- a fake overrides
    only the method its own test actually exercises, so an unexpected call
    to any other method fails loudly and immediately, the same guarantee
    the old `cast()`-narrowed partial fakes gave for free, now that the
    type checker requires each fake to structurally satisfy all three
    `PsServiceClientProtocol` methods rather than just the one under test.
    """

    def get_status(self, *, detail: StatusDetail = StatusDetail.IF_LOGGED_IN) -> ServiceStatus:
        """Fail: this test's fake does not expect `get_status()` to be called."""
        msg = f"get_status must not be called in this test (detail={detail!r})"
        raise AssertionError(msg)

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """Fail: this test's fake does not expect `ingest_internal()` to be called."""
        msg = f"ingest_internal must not be called in this test (content={content!r})"
        raise AssertionError(msg)

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """Fail: this test's fake does not expect `export_instrument()` to be called."""
        msg = f"export_instrument must not be called in this test (instrument_id={instrument_id!r})"
        raise AssertionError(msg)


def test_handle_ingest_document_validates_locally_before_any_http_call(
    tmp_path: Path,
) -> None:
    """A schema-invalid local fixture is rejected before `client.ingest_internal()` is called.

    `client` here is a bare `_UnusedPsServiceClientMethods` instance --
    its `ingest_internal` raises `AssertionError` if ever called, so the
    absence of that failure (only `PsCliError` is raised, from local
    validation) is itself the proof of zero HTTP calls.
    """
    invalid_document: dict[str, object] = {
        "nodes": [],
        "edges": [],
        "graph_name": "policy_system",
    }
    document_path = tmp_path / "bad-seed.json"
    document_path.write_text(json.dumps(invalid_document), encoding="utf-8")
    client = _UnusedPsServiceClientMethods()

    with pytest.raises(PsCliError) as excinfo:
        handle_ingest_document(document_path, client)

    assert "graph_name" in excinfo.value.msg or "additional" in excinfo.value.msg.lower()


_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT: dict[str, object] = {
    "nodes": [
        {
            "label": "RegulatoryInstrument",
            "id": "ENGPRAC-3.0",
            "properties": {
                "title": "Engineering Practices Policy",
                "source_type": "internal",
                "effective_date": "2026-08-01",
                "version": "3.0",
                "status": "active",
            },
        }
    ],
    "edges": [],
}


class _FakeInternalIngestClient(_UnusedPsServiceClientMethods):
    """Hand-written fake implementing `ingest_internal()`'s signature.

    Scripted to return a fixed `IngestionResult`, used only for the pending_reviews
    stage-line tests, which need a specific
    `IngestionResult` to exercise `handle_ingest_document`'s conditional-append
    idiom for `pending_reviews`.
    """

    def __init__(self, *, result: IngestionResult) -> None:
        """Script this fake's ingest_internal() outcome."""
        self._result = result

    def get_status(self, *, detail: StatusDetail = StatusDetail.IF_LOGGED_IN) -> ServiceStatus:
        """Report a fully-healthy target -- the pre-flight check must let this through."""
        del detail
        return ServiceStatus(
            status="ready", version="1.4.0", unhealthy_dependencies=[], gated_graphs=[]
        )

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """Return the scripted result, ignoring `content`."""
        del content
        return self._result


def test_handle_ingest_document_surfaces_nonzero_pending_reviews(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`handle_ingest_document`'s own
    stage-print loop appends `" (pending_reviews: {n})"` when the merge
    stage's summary reports a nonzero count.
    """
    document_path = tmp_path / "seed.json"
    document_path.write_text(json.dumps(_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT), encoding="utf-8")
    result = IngestionResult(
        run_id="run-internal-004",
        regulatory_instrument_id="ri-engprac",
        source="internal",
        stages=[
            StageOutcome(
                stage="merge",
                status="succeeded",
                summary={
                    "obligations": 1,
                    "canonical_capabilities": 1,
                    "near_misses": 1,
                    "pending_reviews": 1,
                },
            ),
        ],
    )
    client = _FakeInternalIngestClient(result=result)

    handle_ingest_document(document_path, client)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert "merge: succeeded (pending_reviews: 1)" in lines


def test_handle_ingest_document_stage_line_byte_identical_when_no_pending_reviews(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """For the internal-seed pipeline: pending_reviews ==
    0 (or absent) prints exactly the pre-existing line, unchanged.
    """
    document_path = tmp_path / "seed.json"
    document_path.write_text(json.dumps(_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT), encoding="utf-8")
    result = IngestionResult(
        run_id="run-internal-005",
        regulatory_instrument_id="ri-engprac",
        source="internal",
        stages=[
            StageOutcome(stage="internal_ingestion", status="succeeded", summary={"roles": 1}),
            StageOutcome(
                stage="merge",
                status="succeeded",
                summary={
                    "obligations": 1,
                    "canonical_capabilities": 1,
                    "near_misses": 0,
                    "pending_reviews": 0,
                },
            ),
        ],
    )
    client = _FakeInternalIngestClient(result=result)

    handle_ingest_document(document_path, client)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert "internal_ingestion: succeeded" in lines
    assert "merge: succeeded" in lines


class _FakeHealthClient(_UnusedPsServiceClientMethods):
    """Hand-written fake implementing `get_status()`'s signature with a scripted reply."""

    def __init__(self, *, status: str, unhealthy_dependencies: list[str] | None) -> None:
        """Script the status and dependency names this fake's `get_status()` returns."""
        self._status = ServiceStatus(
            status=status,
            version="1.4.0",
            unhealthy_dependencies=unhealthy_dependencies,
            gated_graphs=None if unhealthy_dependencies is None else [],
        )
        self.detail_seen: list[StatusDetail] = []

    def get_status(self, *, detail: StatusDetail = StatusDetail.IF_LOGGED_IN) -> ServiceStatus:
        """Record the requested detail mode, then return the scripted status."""
        self.detail_seen.append(detail)
        return self._status


def test_handle_get_health_prints_reachable_alive_ready_on_happy_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fully healthy, fully ready target prints exactly the three summary lines."""
    fake = _FakeHealthClient(
        status="ready",
        unhealthy_dependencies=[],
    )

    handle_get_health(fake)

    captured = capsys.readouterr()
    assert captured.out == "reachable: yes\nhealth: alive\nready: ready\n"
    assert captured.err == ""
    assert fake.detail_seen == [StatusDetail.IF_LOGGED_IN]


def test_handle_get_health_prints_the_login_hint_without_detail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A status-only reply still succeeds and points to logging in for the detail."""
    fake = _FakeHealthClient(status="ready", unhealthy_dependencies=None)

    handle_get_health(fake)

    captured = capsys.readouterr()
    assert captured.out == (
        "reachable: yes\nhealth: alive\nready: ready\n"
        "\N{ELECTRIC LIGHT BULB} log in to see dependency detail\n"
    )


def test_handle_get_health_sends_one_get_status_and_no_probe_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real client on a recording transport: `get health` asks PS Service once, at `/status`."""
    recorded: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={"status": "ready", "version": "1.4.0"})

    client = PsServiceClient(_SERVICE_URL, transport=httpx.MockTransport(_handler))

    handle_get_health(client)

    assert [(r.method, r.url.path) for r in recorded] == [("GET", "/status")]
    assert _status_requests(recorded) == [("GET", "/status")]
    assert "ready: ready" in capsys.readouterr().out


def test_handle_get_health_raises_ps_cli_error_naming_unhealthy_dependencies_when_not_ready() -> (
    None
):
    """A not-ready target with a named unhealthy dependency raises PsCliError with that name
    in the hint.
    """
    fake = _FakeHealthClient(
        status="not_ready",
        unhealthy_dependencies=["falkordb"],
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_get_health(fake)

    assert "not ready" in excinfo.value.msg
    assert excinfo.value.hint is not None
    assert "falkordb" in excinfo.value.hint


def test_handle_get_health_raises_ps_cli_error_with_no_hint_when_no_dependency_named() -> None:
    """A not-ready target with no unhealthy dependency named still
    raises, but with no hint -- proving the hint is genuinely conditional, not always-present.
    """
    fake = _FakeHealthClient(
        status="not_ready",
        unhealthy_dependencies=[],
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_get_health(fake)

    assert excinfo.value.hint is None


def test_handle_get_health_not_ready_message_never_says_could_not_reach() -> None:
    """The not-ready error's rendered text is textually distinct from the unreachable-target
    error's wording, proven by an executed assertion, not just inspection.
    """
    fake = _FakeHealthClient(
        status="not_ready",
        unhealthy_dependencies=["falkordb"],
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_get_health(fake)

    assert "Could not reach" not in str(excinfo.value)


def _status_requests(recorded: list[httpx.Request]) -> list[tuple[str, str]]:
    """The `(method, path)` of every recorded request to a status or probe path, in order."""
    return [(r.method, r.url.path) for r in recorded if r.url.path in _STATUS_AND_PROBE_PATHS]


class _FakeCredentialStore:
    """A dict-backed `CredentialStore` double holding one seeded login."""

    def __init__(self) -> None:
        """Start with no tokens stored for any context."""
        self._tokens: dict[str, TokenBundle] = {}

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return the stored `TokenBundle` for `context`, or `None` if none is stored."""
        return self._tokens.get(context)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context`, overwriting any existing value."""
        self._tokens[context] = tokens

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle; a no-op if none exists."""
        self._tokens.pop(context, None)


def _logged_in_recording_transport(recorded: list[httpx.Request]) -> httpx.MockTransport:
    """A transport answering discovery, the token refresh, `GET /status` and `POST /ingestions`.

    Every request it sees is appended to `recorded`, authentication calls included.
    """

    def _handle(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": _SERVICE_URL,
                    "authorization_servers": [_FAKE_ISSUER],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": "ps-cli-test-client",
                },
            )
        if str(request.url) == f"{_FAKE_ISSUER}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": _FAKE_ISSUER,
                    "device_authorization_endpoint": f"{_FAKE_ISSUER}/device_authorization",
                    "token_endpoint": f"{_FAKE_ISSUER}/token",
                },
            )
        if str(request.url) == f"{_FAKE_ISSUER}/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "refreshed-token-1",
                    "refresh_token": "rt-rotated",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "version": "1.4.0",
                    "unhealthy_dependencies": [],
                    "gated_graphs": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "run_id": "run-preflight-1",
                "regulatory_instrument_id": "ri-engprac",
                "source": "internal",
                "stages": [],
            },
        )

    return httpx.MockTransport(_handle)


def test_ingest_document_preflight_sends_one_get_status_then_the_ingestion(
    tmp_path: Path,
) -> None:
    """The pre-flight is one authenticated `GET /status`, followed by the ingestion request."""
    document_path = tmp_path / "seed.json"
    document_path.write_text(json.dumps(_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT), encoding="utf-8")
    recorded: list[httpx.Request] = []
    store = _FakeCredentialStore()
    store.set_tokens("dev", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
    client = PsServiceClient(
        _SERVICE_URL,
        transport=_logged_in_recording_transport(recorded),
        credential_store=store,
        context="dev",
    )

    handle_ingest_document(document_path, client)

    assert _status_requests(recorded) == [("GET", "/status")]
    assert [
        (r.method, r.url.path)
        for r in recorded
        if r.url.host == _PS_SERVICE_HOST and not r.url.path.startswith("/.well-known/")
    ] == [("GET", "/status"), ("POST", "/ingestions")]
    status_request = next(r for r in recorded if r.url.path == "/status")
    assert status_request.headers.get("authorization") == "Bearer refreshed-token-1"
