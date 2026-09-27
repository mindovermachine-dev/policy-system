"""Tests for the registered `near_misses_list`/`near_misses_resolve` MCP tools
(issue #126, Group 3 -- `ps-near-miss-review` skill). This file also hosts
Slices 3.3/3.4's tests as those branches land; Slice 3.1 (`near_misses_list`'s
happy path) and Slice 3.2 (`near_misses_resolve`'s `keep-separate` happy
path) are implemented here.

Slice 3.1: `near_misses_list` (zero parameters -- no D-PREFLIGHT check, since
neither `handle_near_misses_list` nor `handle_near_misses_resolve` call
`_assert_llm_interface_available` either, confirmed by reading
`ps-cli/src/ps_cli/modules/handlers.py:77-92`) delegates in-process to
`run_list_near_misses` (D-DELEGATE) via the shared `_run_mcp_action` audit
wrapper (D-AUDIT-WRAPPER), returning `run_list_near_misses(...).model_dump()`
-- `run_list_near_misses` itself already builds the
`PendingReviewListResponse` via `near_miss_review_orchestration`'s own
already-public `_to_pending_review_entry` (D-RESPONSE-SHAPE), so no separate
reconstruction is needed in `mcp_server.py`.

Slice 3.2: `near_misses_resolve(review_id, decision)` -- `decision`'s
`Literal["keep-separate", "merge"]` type is itself the MCP-schema-level enum
enforcement (AC-BI-005); only `decision="keep-separate"` is exercised here.
Delegates in-process to `run_resolve_near_miss` (D-DELEGATE) via the same
`_run_mcp_action` wrapper, returning
`run_resolve_near_miss(...).model_dump()` -- like `run_list_near_misses`,
`run_resolve_near_miss` already builds the `ResolveReviewResponse` via
`near_miss_review_orchestration`'s own already-public `_to_resolve_response`
(D-RESPONSE-SHAPE), so no separate reconstruction is needed here either.
`winner_id`/`loser_id` are asserted `None` for this decision (AC-BI-004's
documented shape). The `merge` decision and its own failure states are
Slices 3.3/3.4's scope, not this one.

Slice 3.3 (issue #35): `near_misses_resolve`'s `merge` path originally gated
`decision="merge"` behind a typed-confirm MCP elicitation round trip
(`MergeConfirmation`/`_confirm_merge`, CHANGES.md H2). Issue #131 removes
that mechanism entirely (AC-BI-009) and replaces it with a signed-passkey
pending-approval flow: `decision="merge"` now returns a pending approval
(`pending_approval_id`/`approval_url`/`expires_at`) immediately, with no
graph write and no elicitation round trip at all -- see the "issue #131"
section below for that flow's own tests.
`test_docstring_states_merge_is_irreversible` is kept, updated for the new
flow's own docstring text (the underlying merge write is still IRREVERSIBLE
once it eventually executes, which is exactly why it is now gated behind a
signed approval rather than a typed-confirm round trip).
`test_domain_concepts_tool.py::test_tool_listed_alongside_cypher` is this
suite's only other precedent for asserting on a tool's own listed metadata
(there `input_schema`, here `description`).

Issue #131 (signed passkey approval before near-miss merges): PLAN.md's
`_resolve_signing_actor()`/`create_merge_pending_approval`/
`check_pending_approval` design. `_verified_actor()` (this file, new) binds
a real `AccessToken` directly onto the MCP SDK's own
`mcp.server.auth.middleware.auth_context.auth_context_var` contextvar --
the same one `get_access_token()` (`_resolve_principal`/
`_resolve_signing_actor`) reads, normally populated by `AuthContextMiddleware`
from a real HTTP request (`test_mcp_auth.py`'s own end-to-end proof of that
wiring). Binding it directly here, rather than driving a full
`initialize`/`notifications/initialized`/`tools/call` JSON-RPC handshake
through a `TestClient`, is sufficient because `near_misses_resolve`'s merge
branch and `near_misses_check_approval` never touch `ctx.session` at all
(no elicitation, no server-initiated request) -- only `get_access_token()`
and, for the merge branch, `ctx.request_context.request` for the approval
link's `{base_url}` (falls back to a fixed placeholder when no context is
bound at all, exercised by this file's own bare `server.call_tool(name,
args)` calls with no context argument, which is every call in this file
including the new ones below).

`test_merge_pauses_for_elicitation_then_executes_once_confirmed` (the old
elicitation-round-trip test) and `test_keep_separate_never_elicits_single_round_trip`
(its third-proof companion) are both removed outright, along with the
`_elicitation_context()`/`_FakeSession` helpers and the `mcp.types`
elicitation imports they used -- the mechanism they exercised
(`MergeConfirmation`/`_confirm_merge`/`Elicit`/`Resolve`) no longer exists in
`mcp_server.py` at all, so keeping either test around (even passing, by
accident of dead code) would assert on a mechanism that is gone; deleting
them, not weakening them, is this file's explicit proof that the old gate
is really gone (AC-BI-009), alongside the new
`test_merge_returns_pending_approval_without_executing_the_merge` below,
which is this issue's own defining test (PLAN.md §4 Slice 1).
`test_confirmed_gate_is_not_part_of_the_client_visible_schema` is renamed
`test_ctx_parameter_is_not_part_of_the_client_visible_schema` and now
proves the *new* auto-injected `ctx: Context` parameter (needed for the
approval link's `{base_url}`) is excluded from the client-visible schema,
the same way the old resolver-filled `confirmed` parameter was.

Slice 3.4 (Group 3, issue #35/#126): failure-state completeness for both
tools' `keep-separate` paths, per PLAN.md's D-SANITIZE-UNEXPECTED table and
`near_miss_review_orchestration.py`'s own module docstring. Historical note:
this slice originally also covered a merge-only "stale reference" case
(`dependencies.resolve_review` raising `StalePendingReviewError`, re-raised
as `PendingReviewNotFoundError` with a dedicated "stale" message) -- that
test (`test_resolve_merge_stale_reference_returns_named_error`) is removed
by issue #131, since `near_misses_resolve`'s merge branch no longer calls
`resolve_review` at all (see the "issue #131" section above); the analogous
early-existence check for merge now lives in
`test_resolve_merge_unresolved_review_not_found_returns_named_error`
above, and the exact stale-reference scenario becomes a later slice's
execution-time concern. Graph-unavailable reuses the existing generic
`_sanitize_graph_open` helper via `_sanitize_near_miss_review_graph_opens`
(mirrors `_sanitize_pipeline_graph_opens`/`_sanitize_change_check_graph_opens`
exactly). The residual unexpected-exception safety net is `_run_mcp_action`'s
own existing catch-all (D-AUDIT-WRAPPER point 4) -- no new production code,
proven per tool via a fake delegate raising something unclassified, mirroring
`test_check_regulations_tool.py`'s identically-shaped tests for this same
branch.

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_ingest_regulation_tool.py`'s/`test_check_regulations_tool.py`'s own
convention. The fake `NearMissReviewDependencies` bundle
(`_fake_dependencies`/`_record`) is the existing REST-side fixture already
exercised by `tests/api/test_routes_near_misses.py` -- reused here via a
cross-module import rather than reinvented (PLAN.md's explicit Slice 3.1
instruction, extended here to Slice 3.2's own `resolve=` parameter that same
fixture already supports), mirroring this codebase's own established
cross-module-private-import convention for production code (e.g.
`mcp_server.py`'s own imports of `routes._to_accepted_response`).
`tests/api/` is an importable package (`tests/api/_fakes.py`'s own module
docstring), so this cross-package import works the same way
`test_check_regulations_tool.py`'s `from api._fakes import
build_fake_change_check_dependencies` already does.

`pytest-asyncio` is not installed; this file drives the tool with a bare
`asyncio.run(server.call_tool(...))`, exactly like `test_cypher_tool.py`/
`test_ingest_regulation_tool.py`/`test_check_regulations_tool.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from api.test_routes_near_misses import (
    _fake_dependencies,  # pyright: ignore[reportPrivateUsage]  -- reuse the existing REST-side fixture verbatim, not reinvented (PLAN.md Slice 3.1 instruction)
    _record,  # pyright: ignore[reportPrivateUsage]  -- same reuse, mirrors `_fake_dependencies`' own justification immediately above
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.company_merge.models import ResolveOutcome
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path
    from typing import Literal

    import pytest

    from ps_service.company_merge.falkordb_client import GraphHandle

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_ACTOR_SUBJECT = "actor-sub-1"
_ACTOR_ISSUER = "https://issuer.example.com/"


@contextlib.contextmanager
def _verified_actor(*, sub: str = _ACTOR_SUBJECT, iss: str = _ACTOR_ISSUER) -> Generator[None]:
    """Bind a real, verified `AccessToken` onto the MCP SDK's own auth contextvar.

    `_resolve_signing_actor`/`_resolve_principal` both read `get_access_token()`
    off this exact contextvar (`mcp.server.auth.middleware.auth_context`),
    normally populated by `AuthContextMiddleware` from a real HTTP request
    (`test_mcp_auth.py`'s own end-to-end proof of that wiring). Binding it
    directly here -- rather than driving a full JSON-RPC handshake through a
    `TestClient` -- is sufficient for every test in this file that uses it,
    since none of them ever touch `ctx.session` (no elicitation, no
    server-initiated request).
    """
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _fake_store_factory(store: object) -> Callable[[object], object]:
    """A `PsycopgPendingApprovalStore`-shaped factory returning the same fake store every call.

    Monkeypatched onto `mcp_server.PsycopgPendingApprovalStore` -- the tool
    bodies call it as `PsycopgPendingApprovalStore(config)`, so this must
    accept (and ignore) one positional argument. Typed loosely (`object`,
    not `PendingApprovalStore`) so it also accepts this file's own
    deliberately-narrower-than-the-Protocol `_RaisingStore` fake below,
    which only needs `create_pending_approval` to raise.
    """
    return lambda _config: store


_CODE_TOKEN_BYTES = 32
_NONCE_BYTES = 32
_EXPIRY_WINDOW = timedelta(minutes=15)


@dataclasses.dataclass
class _FakePendingApprovalStore:
    """In-memory `PendingApprovalStore` (structural `Protocol` match, no real Postgres).

    A local, private copy of `tests/passkey_signing/_fakes.py`'s own
    `FakePendingApprovalStore` (same code/nonce/expiry generation), not a
    cross-package import of it: `ps-service/tests/passkey_signing/` sorts
    alphabetically *after* `ps-service/tests/mcp_interface/`, and pytest's
    `--import-mode=importlib` (`pyproject.toml`) only makes a package's
    submodules resolvable as real imports once pytest has itself collected
    something from that package first (confirmed empirically: a bare
    `pytest ps-service/tests/mcp_interface/test_near_miss_tools.py` run,
    with no other test file collected first, cannot resolve
    `from passkey_signing._fakes import ...` at all) -- every existing
    cross-package import in this test suite already only ever points at an
    alphabetically-*earlier* package (`mcp_interface` -> `api`,
    `ingestion` -> `change_monitor`) for exactly this reason. Duplicating
    this ~20-line fake here keeps this file's collection independent of
    collection order/scope.
    """

    _rows_by_id: dict[str, PendingApprovalRow] = dataclasses.field(default_factory=dict)

    def create_pending_approval(
        self,
        *,
        tool_name: str,
        normalized_args: dict[str, object],
        actor_subject: str,
        actor_issuer: str,
        display_summary: dict[str, object],
    ) -> tuple[PendingApprovalRow, str]:
        code = secrets.token_urlsafe(_CODE_TOKEN_BYTES)
        code_hash = hashlib.sha256(code.encode()).digest()
        nonce = secrets.token_bytes(_NONCE_BYTES)
        created_at = datetime.now(UTC)
        row = PendingApprovalRow(
            id=str(uuid.uuid4()),
            code_hash=code_hash,
            tool_name=tool_name,
            normalized_args=normalized_args,
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            nonce=nonce,
            display_summary=display_summary,
            status="pending",
            outcome=None,
            created_at=created_at,
            expires_at=created_at + _EXPIRY_WINDOW,
        )
        self._rows_by_id[row.id] = row
        return row, code

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        return self._rows_by_id.get(pending_approval_id)

    def get_by_code_hash(self, code_hash: bytes) -> PendingApprovalRow | None:
        for row in self._rows_by_id.values():
            if row.code_hash == code_hash:
                return row
        return None


def _call_near_misses_list() -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("near_misses_list", {}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_happy_path_lists_every_unresolved_review_with_full_field_set(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-004 (list half): several scripted `PendingReviewRecord`s come
    back verbatim, field for field (`id`/`kind`/`incoming_text`/
    `nearest_existing_text`/`similarity` -- AC-BI-004's explicit requirement
    list), plus the `mcp_interface` started/succeeded log pair carrying the
    resolved principal.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    first = _record("review_aaa", kind="Capability", similarity=0.62)
    second = _record("review_bbb", kind="Policy", similarity=0.81)
    dependencies, _ = _fake_dependencies((first, second))
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )

    result = _call_near_misses_list()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert [entry["id"] for entry in body["reviews"]] == ["review_aaa", "review_bbb"]
    for record, entry in zip((first, second), body["reviews"], strict=True):
        assert entry["id"] == record.id
        assert entry["kind"] == record.kind
        assert entry["incoming_text"] == record.incoming_text
        assert entry["nearest_existing_text"] == record.nearest_existing_text
        assert entry["similarity"] == record.similarity
    # AC-BI-003/004 precedent (`test_routes_near_misses.py`): the underlying
    # node ids are deliberately not part of this wire response.
    assert "incoming_id" not in body["reviews"][0]
    assert "nearest_existing_id" not in body["reviews"][0]

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_list"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


def _call_near_misses_resolve(review_id: str, decision: str) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool(
            "near_misses_resolve", {"review_id": review_id, "decision": decision}
        )
    )
    assert isinstance(result, CallToolResult)
    return result


def test_keep_separate_resolves_with_winner_and_loser_left_none(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-004 (resolve half, keep-separate): the resolved review's
    `winner_id`/`loser_id` stay `None` in the response for this decision,
    plus the `mcp_interface` started/succeeded log pair carrying the
    resolved principal.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    recorded_calls: list[tuple[str, str]] = []

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph
        recorded_calls.append((review_id, decision))
        return ResolveOutcome(review_id=review_id, decision=decision)

    dependencies, _ = _fake_dependencies((), resolve=_resolve)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )

    result = _call_near_misses_resolve("review_aaa", "keep-separate")

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "review_id": "review_aaa",
        "decision": "keep-separate",
        "winner_id": None,
        "loser_id": None,
        "pending_approval_id": None,
        "approval_url": None,
        "expires_at": None,
    }
    assert recorded_calls == [("review_aaa", "keep-separate")]

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_resolve"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


# --- Slice 3.3: `merge` path + CHANGES.md H2's elicitation gate ---


def test_docstring_states_merge_is_irreversible() -> None:
    """The underlying merge write is still IRREVERSIBLE once it eventually
    executes -- this is exactly why issue #131 gates it behind a signed
    passkey approval rather than the old typed-confirm elicitation round
    trip. Mirrors `test_domain_concepts_tool.py::test_tool_listed_alongside_cypher`'s
    own `list_tools()` precedent for asserting on a tool's listed metadata.
    """
    tools = asyncio.run(mcp_server.server.list_tools())
    [tool] = [t for t in tools if t.name == "near_misses_resolve"]
    assert tool.description is not None
    assert "IRREVERSIBLE" in tool.description


def test_ctx_parameter_is_not_part_of_the_client_visible_schema() -> None:
    """The SDK's auto-injected `ctx: Context` parameter (needed for the
    approval link's `{base_url}`, PLAN.md §2.2 step 4) must never leak into
    the tool's client-visible JSON argument schema -- mirrors the old,
    now-removed `confirmed` resolver-parameter's own exclusion proof (the
    mechanism differs -- Context injection, not `Resolve(...)` -- but the
    client-visible contract this protects is the same).
    """
    tools = asyncio.run(mcp_server.server.list_tools())
    [tool] = [t for t in tools if t.name == "near_misses_resolve"]
    assert set(tool.input_schema.get("properties", {})) == {"review_id", "decision"}
    assert set(tool.input_schema.get("required", [])) == {"review_id", "decision"}


# --- issue #131: signed passkey approval before near-miss merges -------------


def test_merge_returns_pending_approval_without_executing_the_merge(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """PLAN.md §4 Slice 1's own defining test: calling `near_misses_resolve`
    with `decision="merge"` for a real, authenticated caller returns a
    pending approval immediately -- no elicitation, no `confirmed` argument
    -- and never calls the fake `resolve_review` delegate at all, proving
    the graph is left completely unchanged (reusing the same
    `_fake_dependencies`/`_record` merge-fixture setup the old, now-removed
    elicitation test used).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    recorded_calls: list[tuple[str, str]] = []

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph
        recorded_calls.append((review_id, decision))
        return ResolveOutcome(
            review_id=review_id,
            decision=decision,
            winner_id="capability_existing_a",
            loser_id="capability_incoming_a",
        )

    record = _record("review_aaa")
    dependencies, _ = _fake_dependencies((record,), resolve=_resolve)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )
    monkeypatch.setattr(
        mcp_server, "PsycopgPendingApprovalStore", _fake_store_factory(_FakePendingApprovalStore())
    )

    with _verified_actor():
        result = _call_near_misses_resolve("review_aaa", "merge")

    assert result.is_error is False
    body = json.loads(_text(result))
    assert set(body) == {"pending_approval_id", "approval_url", "expires_at"}
    assert body["pending_approval_id"]
    assert body["approval_url"]
    assert body["expires_at"]
    # The loser node is never touched and no edge is re-pointed: the fake
    # `resolve_review` delegate above -- the only code path that would ever
    # perform that write -- is never called.
    assert recorded_calls == []

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_resolve"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert len({line["run_id"] for line in mcp_lines}) == 1


def test_merge_without_a_real_authenticated_caller_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002: a caller with no real, verified actor identity -- here, the
    local-test bypass alone, with no `AccessToken` ever bound -- gets a
    clear error, never a pending approval. No dependencies/store faking is
    needed: this check runs before either is ever touched.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()

    result = _call_near_misses_resolve("review_aaa", "merge")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "authenticat" in text.lower()


def test_resolve_merge_unresolved_review_not_found_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAN.md §2.2 step 2's early existence check: a `review_id` absent from
    the unresolved-reviews listing is rejected with the same
    `PendingReviewNotFoundError` message `keep-separate` already uses,
    before any Postgres row is created. Supersedes the old, now-removed
    `test_resolve_merge_stale_reference_returns_named_error`: that test's
    scenario -- a review whose referenced node a PRIOR merge already deleted
    -- is a later slice's authoritative execution-time check now (this
    Slice 1 create step never calls `resolve_review` at all).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    dependencies, _ = _fake_dependencies(())  # no unresolved reviews at all
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )

    with _verified_actor():
        result = _call_near_misses_resolve("review_missing", "merge")

    assert result.is_error is False
    assert _text(result) == "error: no unresolved PendingReview with id 'review_missing'"


def test_resolve_merge_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED, merge branch: an unreachable graph during the
    early existence check sanitises to the same fixed message every other
    tool uses, via the same `_sanitize_near_miss_review_graph_opens` wrapper.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    dependencies, _ = _fake_dependencies(())

    def _raising_open(config: object) -> object:
        _ = config
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_dependencies = dataclasses.replace(dependencies, open_single_tenant_graph=_raising_open)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: broken_dependencies
    )

    with _verified_actor():
        result = _call_near_misses_resolve("review_aaa", "merge")

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_resolve_merge_residual_unexpected_exception_returns_generic_error(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4: an exception the merge branch does not itself
    sanitise (here, the store's `create_pending_approval` raising something
    unclassified) is caught by `_run_mcp_action`'s residual safety net --
    the fixed, generic message, never the raw exception text.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    record = _record("review_aaa")
    dependencies, _ = _fake_dependencies((record,))
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )

    class _RaisingStore:
        def create_pending_approval(self, **kwargs: object) -> object:
            _ = kwargs
            message = "boom -- must never reach the caller"
            raise RuntimeError(message)

    monkeypatch.setattr(
        mcp_server, "PsycopgPendingApprovalStore", _fake_store_factory(_RaisingStore())
    )

    with _verified_actor():
        result = _call_near_misses_resolve("review_aaa", "merge")

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_resolve"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert "boom -- must never reach the caller" in str(lines[-1].get("detail"))


def test_check_approval_reports_pending_for_a_freshly_created_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`near_misses_check_approval` reports `status="pending"` right after
    `near_misses_resolve`'s merge branch creates the approval -- the same
    store instance backs both calls, exactly as it would in production
    (one Postgres instance, two tool calls).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    record = _record("review_aaa")
    dependencies, _ = _fake_dependencies((record,))
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )
    store = _FakePendingApprovalStore()
    monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", _fake_store_factory(store))

    with _verified_actor():
        resolve_result = _call_near_misses_resolve("review_aaa", "merge")
        pending_approval_id = json.loads(_text(resolve_result))["pending_approval_id"]

        check_result = asyncio.run(
            mcp_server.server.call_tool(
                "near_misses_check_approval", {"pending_approval_id": pending_approval_id}
            )
        )

    assert isinstance(check_result, CallToolResult)
    assert check_result.is_error is False
    body = json.loads(_text(check_result))
    assert body["pending_approval_id"] == pending_approval_id
    assert body["status"] == "pending"
    assert body["review_id"] == "review_aaa"
    assert body["decision"] == "merge"
    assert body["winner_id"] is None
    assert body["loser_id"] is None


def test_check_approval_unknown_id_gets_generic_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    monkeypatch.setattr(
        mcp_server, "PsycopgPendingApprovalStore", _fake_store_factory(_FakePendingApprovalStore())
    )

    with _verified_actor():
        result = asyncio.run(
            mcp_server.server.call_tool(
                "near_misses_check_approval", {"pending_approval_id": "does-not-exist"}
            )
        )

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no pending approval" in text


def test_check_approval_unauthenticated_caller_gets_generic_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAN.md §2.3's fail-closed rule applied to the read side: a caller
    with no real actor identity can never poll any pending approval, not
    even to learn that a real one exists -- the same generic message an
    unknown id gets, never a distinct "unauthenticated" message here (that
    distinction is `near_misses_resolve`'s own, on the write side).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _FakePendingApprovalStore()
    row, _code = store.create_pending_approval(
        tool_name="near_misses_resolve",
        normalized_args={"review_id": "review_aaa", "decision": "merge"},
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        display_summary={},
    )
    monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", _fake_store_factory(store))

    result = asyncio.run(
        mcp_server.server.call_tool("near_misses_check_approval", {"pending_approval_id": row.id})
    )

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no pending approval" in text


def test_check_approval_wrong_actor_gets_generic_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAN.md §2.3's ownership check: a different, but still real,
    authenticated caller gets the identical generic not-found error -- never
    a distinct "not yours" message that would leak the row's existence.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _FakePendingApprovalStore()
    row, _code = store.create_pending_approval(
        tool_name="near_misses_resolve",
        normalized_args={"review_id": "review_aaa", "decision": "merge"},
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        display_summary={},
    )
    monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", _fake_store_factory(store))

    with _verified_actor(sub="someone-else", iss=_ACTOR_ISSUER):
        result = asyncio.run(
            mcp_server.server.call_tool(
                "near_misses_check_approval", {"pending_approval_id": row.id}
            )
        )

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no pending approval" in text


# --- Slice 3.4: failure-state completeness for both tools --------------------


def test_list_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `run_list_near_misses` calls
    `dependencies.open_single_tenant_graph` directly, with no try/except of
    its own -- a generic exception from that opener must sanitise to the
    same fixed message `_resolve_graph`/`ingest_regulation`/
    `check_regulations` already use, never leaking host/port/driver detail,
    via the new `_sanitize_near_miss_review_graph_opens` wrapper (reusing
    the existing generic `_sanitize_graph_open` per-opener helper, not
    reinventing it).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    dependencies, _ = _fake_dependencies(())

    def _raising_open(config: object) -> object:
        _ = config
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_dependencies = dataclasses.replace(dependencies, open_single_tenant_graph=_raising_open)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: broken_dependencies
    )

    result = _call_near_misses_list()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_list_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4 / D-SANITIZE-UNEXPECTED's last row: an
    exception `near_misses_list`'s own body does not itself sanitise (here,
    `dependencies.list_pending_reviews` raising something unclassified, with
    the single-tenant graph already successfully opened) is caught by
    `_run_mcp_action`'s residual safety net -- returned as the fixed,
    generic message (never the raw exception text), with the full `repr`
    logged server-side only.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    dependencies, _ = _fake_dependencies(())

    def _raising_list(graph: object) -> object:
        _ = graph
        raise ValueError("boom -- must never reach the caller")

    broken_dependencies = dataclasses.replace(dependencies, list_pending_reviews=_raising_list)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: broken_dependencies
    )

    result = _call_near_misses_list()

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_list"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert failed_line.get("principal") == LOCAL_TEST_PRINCIPAL_ID
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))


def test_resolve_not_found_or_already_resolved_returns_named_error(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-SANITIZE-UNEXPECTED: `dependencies.resolve_review` returning `None`
    (`review_id` doesn't exist, or was already resolved -- both collapse to
    the same condition, per `PendingReviewNotFoundError`'s own docstring)
    is translated by `run_resolve_near_miss` into `PendingReviewNotFoundError`,
    which `near_misses_resolve`'s own body catches and returns as
    `error: <str(exc)>` verbatim -- no merge write, no elicitation round
    trip for this `keep-separate` decision.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph, review_id, decision
        return None

    dependencies, _ = _fake_dependencies((), resolve=_resolve)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )

    result = _call_near_misses_resolve("review_missing", "keep-separate")

    assert result.is_error is False
    assert _text(result) == "error: no unresolved PendingReview with id 'review_missing'"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_resolve"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in lines)


def test_resolve_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `run_resolve_near_miss` calls
    `dependencies.open_single_tenant_graph` directly, with no try/except of
    its own -- a generic exception from that opener must sanitise to the
    same fixed message, via `_sanitize_near_miss_review_graph_opens` (same
    wrapper `near_misses_list` reuses above, not a second implementation).
    Uses `keep-separate` so no elicitation round trip is needed to reach
    this branch.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    dependencies, _ = _fake_dependencies(())

    def _raising_open(config: object) -> object:
        _ = config
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_dependencies = dataclasses.replace(dependencies, open_single_tenant_graph=_raising_open)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: broken_dependencies
    )

    result = _call_near_misses_resolve("review_aaa", "keep-separate")

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_resolve_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4 / D-SANITIZE-UNEXPECTED's last row: an
    exception `near_misses_resolve`'s own body does not itself sanitise
    (here, `dependencies.resolve_review` raising something unclassified,
    with the single-tenant graph already successfully opened) is caught by
    `_run_mcp_action`'s residual safety net -- returned as the fixed,
    generic message (never the raw exception text), with the full `repr`
    logged server-side only. Uses `keep-separate` so no elicitation round
    trip is needed to reach this branch.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    def _raising_resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph, review_id, decision
        raise RuntimeError("boom -- must never reach the caller")

    dependencies, _ = _fake_dependencies((), resolve=_raising_resolve)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )

    result = _call_near_misses_resolve("review_aaa", "keep-separate")

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_resolve"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert failed_line.get("principal") == LOCAL_TEST_PRINCIPAL_ID
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))
