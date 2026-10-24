"""Tests for the registered `list-near-misses`/`resolve-near-miss` MCP tools.

`list-near-misses` takes no parameters and so has no pre-flight check, since
neither `list-near-misses` nor `resolve-near-miss` call
`_assert_llm_interface_available`. It delegates in-process to
`run_list_near_misses` via the shared `_run_mcp_action` audit wrapper and
returns `run_list_near_misses(...).model_dump()`; that function already builds
the `PendingReviewListResponse` via `near_miss_review_orchestration`'s public
`_to_pending_review_entry`, so no reconstruction is needed in `mcp_server.py`.

`resolve-near-miss(review_id, decision)`: `decision`'s
`Literal["keep-separate", "merge"]` type is itself the MCP-schema-level enum
enforcement. It delegates in-process to `run_resolve_near_miss` via the same
`_run_mcp_action` wrapper and returns `run_resolve_near_miss(...).model_dump()`;
that function already builds the `ResolveReviewResponse` via
`near_miss_review_orchestration`'s public `_to_resolve_response`. For
`keep-separate`, `winner_id`/`loser_id` are `None`.

`decision="merge"` is the Graph Cleanup Capability merge behind a signed passkey approval and a
`ComplianceOfficer` gate; its tests are in `test_near_miss_merge_via_cleanup.py`. This file keeps
`keep-separate`, the listing and the ownership rules of `check-near-miss-approval`.

`_verified_actor()` (this file) binds a real `AccessToken` directly onto the
MCP SDK's `mcp.server.auth.middleware.auth_context.auth_context_var`
contextvar -- the same one `get_access_token()`
(`_resolve_signing_actor`) reads, normally populated by
`AuthContextMiddleware` from a real HTTP request (`test_mcp_auth.py` proves
that wiring end to end). Binding it directly, rather than driving a full
`initialize`/`notifications/initialized`/`tools/call` JSON-RPC handshake
through a `TestClient`, is sufficient because these tools never touch `ctx.session` (no
elicitation, no server-initiated request) -- only `get_access_token()`.

Failure-state completeness for both tools' `keep-separate` paths follows
`near_miss_review_orchestration.py`'s own module docstring.
Graph-unavailable reuses the generic `_sanitize_graph_open` helper via
`_sanitize_near_miss_review_graph_opens` (mirrors
`_sanitize_pipeline_graph_opens`/`_sanitize_change_check_graph_opens`
exactly). The residual unexpected-exception safety net is `_run_mcp_action`'s
own catch-all, proven per tool via a fake delegate raising something
unclassified, mirroring `test_check_regulations_tool.py`'s identically-shaped
tests for this same branch.

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_ingest_regulation_tool.py`'s/`test_check_regulations_tool.py`'s own
convention. `_record` (`tests/api/test_routes_near_misses.py`'s own fixture,
reused here via cross-module import rather than reinvented, mirroring this
codebase's established cross-module-private-import convention -- e.g.
`mcp_server.py`'s own imports of `routes._to_accepted_response`) builds the
`PendingReviewRecord` field values this file scripts into fake FalkorDB rows.
`tests/api/` is an importable package (`tests/api/_fakes.py`'s own module
docstring), so this cross-package import works the same way
`test_check_regulations_tool.py`'s `from api._fakes import
build_fake_change_check_dependencies` already does.

`build_default_near_miss_review_dependencies`'s DI seam is narrowed so only its
FalkorDB `open_single_tenant_graph` opener is substitutable
(`near_miss_review_orchestration.py`'s own module docstring);
`list_pending_reviews`/`resolve_review` are always the real, shipped
`ps_service.company_merge.pending_review` functions. `_ScriptedGraphHandle`
(below) is a structural `GraphHandle` stand-in that scripts per-call
`graph.query(...)` results/exceptions -- mirroring
`tests/company_merge/test_pending_review_{list,resolve}.py`'s own
`_FakeGraph`/`_FakeQueryResult` (a local, private copy here, not a
cross-package import, matching this codebase's "own copy per module"
convention) -- and the real `list_pending_reviews`/`resolve_review` run
against it, issuing their own real Cypher query strings and mapping the
scripted rows back exactly as they would against real FalkorDB. Tests assert
on `graph.calls` (which real queries the real functions issued) or on the
tool's own output, not on whether a fake was invoked.

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
from typing import TYPE_CHECKING, cast

import pytest
from api._gateway_fakes import installed_process_gateway
from api.test_routes_near_misses import (
    _record,  # pyright: ignore[reportPrivateUsage]  -- reuse the existing REST-side fixture, not reinvented
)
from audit._fakes import InMemoryAuditStore, audit_store_factory
from company_merge._kinds import UNREGISTERED_KIND
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from ps_service.company_merge.models import PendingReviewRecord
    from ps_service.company_merge.reviewable_kinds import ReviewableKind
    from ps_service.config import ServiceConfig

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_ACTOR_SUBJECT = "actor-sub-1"
_ACTOR_ISSUER = "https://issuer.example.com/"


@contextlib.contextmanager
def _verified_actor(*, sub: str = _ACTOR_SUBJECT, iss: str = _ACTOR_ISSUER) -> Generator[None]:
    """Bind a real, verified `AccessToken` onto the MCP SDK's own auth contextvar.

    `_resolve_signing_actor` reads `get_access_token()`
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

    Installed as the environment's `pending_approval_store_factory` -- the tool bodies call it
    as `factory(config)`, so this must accept (and ignore) one positional argument. Typed
    loosely (`object`, not `PendingApprovalStore`) so it also accepts this file's own
    deliberately-narrower-than-the-Protocol `_RaisingStore` fake below, which only needs
    `create_pending_approval` to raise.
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

    def mark_signed(self, pending_approval_id: str) -> bool:
        """Mirror of `_fakes.FakePendingApprovalStore.mark_signed` (see class docstring)."""
        row = self._rows_by_id.get(pending_approval_id)
        if row is None or row.status != "pending":
            return False
        self._rows_by_id[pending_approval_id] = dataclasses.replace(row, status="signed")
        return True

    def set_outcome(self, pending_approval_id: str, outcome: dict[str, object]) -> None:
        """Mirror of `_fakes.FakePendingApprovalStore.set_outcome` (see class docstring)."""
        row = self._rows_by_id.get(pending_approval_id)
        if row is not None:
            self._rows_by_id[pending_approval_id] = dataclasses.replace(row, outcome=outcome)


# --- Script the real `pending_review.list_pending_reviews`/
# `resolve_review` functions against a structural `GraphHandle` fake, rather than
# faking those functions themselves (see module docstring). --------------------


@dataclasses.dataclass
class _RecordedGraphCall:
    """One `graph.query(...)` call the real `pending_review` functions issued."""

    query: str
    params: dict[str, object] | None


class _ScriptedQueryResult:
    """Satisfies `GraphQueryResult` structurally (own copy, mirrors
    `tests/company_merge/test_pending_review_list.py`'s own `_FakeQueryResult`).
    """

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _ScriptedGraphHandle:
    """Structural `GraphHandle` stand-in scripting one result/exception per call, in order.

    Own local copy of `tests/company_merge/test_pending_review_resolve.py`'s
    own `_FakeGraph` (not a cross-package import -- mirrors this codebase's
    "own copy per module" convention), extended so a step may be an
    `Exception` instance to raise instead of a rows list -- this file's own
    D-SANITIZE-UNEXPECTED/residual-safety-net tests need `graph.query(...)`
    itself to raise something unclassified, exactly as it would for a real,
    misbehaving FalkorDB driver.
    """

    def __init__(self, steps: list[list[list[object]] | Exception]) -> None:
        self.calls: list[_RecordedGraphCall] = []
        self._steps = list(steps)

    def query(self, q: str, params: dict[str, object] | None = None) -> _ScriptedQueryResult:
        self.calls.append(_RecordedGraphCall(q, params))
        step = self._steps.pop(0) if self._steps else []
        if isinstance(step, Exception):
            raise step
        return _ScriptedQueryResult(cast("list[object]", step))


def _row_for(record: PendingReviewRecord) -> list[object]:
    """The `_LIST_PENDING_REVIEWS_QUERY` row shape `list_pending_reviews` expects.

    Column order mirrors `pending_review._LIST_PENDING_REVIEWS_QUERY`/
    `list_pending_reviews`'s own unpacking exactly.
    """
    return [
        record.id,
        record.kind,
        record.incoming_id,
        record.incoming_text,
        record.nearest_existing_id,
        record.nearest_existing_text,
        record.similarity,
        record.created_at,
    ]


def _use_fake_graph_opener_only(
    monkeypatch: pytest.MonkeyPatch, opener: Callable[[ServiceConfig], object]
) -> None:
    """Hand the tools `opener` as their graph factory; the review functions stay real.

    The tools wire the real `list_pending_reviews`/`resolve_review` over the environment's graph
    factory, so only the graph this opener hands them is substituted.
    """
    _override(monkeypatch, graph_factory=opener)


def _use_scripted_graph(monkeypatch: pytest.MonkeyPatch, graph: _ScriptedGraphHandle) -> None:
    """`_use_fake_graph_opener_only`, specialised to hand every call the same `graph`."""
    _use_fake_graph_opener_only(monkeypatch, lambda _config: graph)


def _raising_open(config: object) -> object:
    _ = config
    message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
    raise ConnectionError(message)


def _call_list_near_misses() -> CallToolResult:
    result = asyncio.run(_server().call_tool("list-near-misses", {}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_happy_path_lists_every_unresolved_review_with_full_field_set(
    monkeypatch: pytest.MonkeyPatch,
    read_lines: ReadLines,
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    """Several scripted `PendingReviewRecord`s come
    back field for field (`id`/`kind`/`incoming_text`/
    `nearest_existing_text`/`similarity`), plus the `mcp_interface`
    started/succeeded log pair, which carries no caller identity.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    first = _record("review_aaa", kind="Capability", similarity=0.62)
    second = _record("review_bbb", kind=test_only_reviewable_kind.label, similarity=0.81)
    graph = _ScriptedGraphHandle(steps=[[_row_for(first), _row_for(second)]])
    _use_scripted_graph(monkeypatch, graph)

    result = _call_list_near_misses()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert [entry["id"] for entry in body["reviews"]] == ["review_aaa", "review_bbb"]
    for record, entry in zip((first, second), body["reviews"], strict=True):
        assert entry["id"] == record.id
        assert entry["kind"] == record.kind
        assert entry["incoming_text"] == record.incoming_text
        assert entry["nearest_existing_text"] == record.nearest_existing_text
        assert entry["similarity"] == record.similarity
    # Matches `test_routes_near_misses.py`: the underlying
    # node ids are deliberately not part of this wire response.
    assert "incoming_id" not in body["reviews"][0]
    assert "nearest_existing_id" not in body["reviews"][0]

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "list-near-misses"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert "principal" not in line


@pytest.fixture(name="audit_store", autouse=True)
def _audit_store_fixture(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:  # pyright: ignore[reportUnusedFunction]  # autouse + injected by name
    """`resolve-near-miss` writes `near_miss.resolve` rows: keep off Postgres."""

    # The resolution's graph group is anchored to the opening row's id, which the
    # graph log accepts only for a committed audit row -- register each id with the process rig.
    def _register(event_id: str) -> None:
        installed_process_gateway().rig.store.audit_rows.add(event_id)

    store = InMemoryAuditStore(event_id_sink=_register)
    _override(monkeypatch, audit_store_factory=audit_store_factory(store))
    return store


def _call_resolve_near_miss(review_id: str, decision: str) -> CallToolResult:
    result = asyncio.run(
        _server().call_tool("resolve-near-miss", {"review_id": review_id, "decision": decision})
    )
    assert isinstance(result, CallToolResult)
    return result


def test_keep_separate_decision_needs_no_role_and_writes_no_access_gate_row(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """Only the merge decision is role-gated, so a keep-separate call never writes a gate row."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _ScriptedGraphHandle(steps=[[_row_for(_record("review_aaa"))], [["Capability"]]])
    _use_scripted_graph(monkeypatch, graph)
    installed_process_gateway().rig.graphs.open("policy_system").seed_node(
        ("PendingReview",), "review_aaa", {"kind": "Capability"}
    )

    result = _call_resolve_near_miss("review_aaa", "keep-separate")

    assert json.loads(_text(result))["decision"] == "keep-separate"
    assert "access.gate" not in {row.action for row in audit_store.rows}
    assert "near_miss.resolve" in {row.action for row in audit_store.rows}


def test_keep_separate_resolves_with_winner_and_loser_left_none(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines, audit_store: InMemoryAuditStore
) -> None:
    """For keep-separate, the resolved review's
    `winner_id`/`loser_id` stay `None` in the response for this decision,
    plus the `mcp_interface` started/succeeded log pair, which carries no
    caller identity.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    # Step 0 (`get_pending_review`): the audit lookup of the review's entity ids.
    # Step 1 (`_FIND_REVIEW_QUERY`): the review exists, kind "Capability".
    # Step 2: the review is deleted by a gateway group, not by a graph query, so
    # the scripted handle sees no third call.
    graph = _ScriptedGraphHandle(steps=[[_row_for(_record("review_aaa"))], [["Capability"]]])
    _use_scripted_graph(monkeypatch, graph)
    # The review node the gateway group deletes (a delete of an absent node logs nothing).
    installed_process_gateway().rig.graphs.open("policy_system").seed_node(
        ("PendingReview",), "review_aaa", {"kind": "Capability"}
    )

    result = _call_resolve_near_miss("review_aaa", "keep-separate")

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
    # Proof that the real `resolve_review` actually ran its documented
    # 2-query sequence for `review_aaa`/`keep-separate` -- asserted on the
    # real queries/params it issued against `graph`, not on a fake
    # collaborator's own recorded call.
    assert len(graph.calls) == 2
    assert graph.calls[0].params == {"review_id": "review_aaa"}
    assert "MATCH (r:PendingReview {id: $review_id}) RETURN" in graph.calls[1].query
    assert graph.calls[1].params == {"review_id": "review_aaa"}
    # The delete reached the gateway as one group anchored to the opening audit row.
    rig = installed_process_gateway().rig
    (opening_id,) = audit_store.event_ids
    assert [g.audit_event_id for g in rig.store.groups if g.graph == "policy_system"] == [
        opening_id
    ]

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "resolve-near-miss"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert "principal" not in line


# --- `check-near-miss-approval` visibility ---


def test_check_approval_unknown_id_gets_generic_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _override(
        monkeypatch,
        pending_approval_store_factory=_fake_store_factory(_FakePendingApprovalStore()),
    )

    with _verified_actor():
        result = asyncio.run(
            _server().call_tool(
                "check-near-miss-approval", {"pending_approval_id": "does-not-exist"}
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
    """Fail-closed on the read side: a caller
    with no real actor identity can never poll any pending approval, not
    even to learn that a real one exists -- the same generic message an
    unknown id gets, never a distinct "unauthenticated" message here (that
    distinction is `resolve-near-miss`'s own, on the write side).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _FakePendingApprovalStore()
    row, _code = store.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={
            "survivor_id": "cap_a",
            "absorbed_id": "cap_b",
            "acknowledge_governance_change": False,
            "state_digest": "d",
            "review_id": "review_aaa",
        },
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        display_summary={},
    )
    _override(monkeypatch, pending_approval_store_factory=_fake_store_factory(store))

    result = asyncio.run(
        _server().call_tool("check-near-miss-approval", {"pending_approval_id": row.id})
    )

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no pending approval" in text


def test_check_approval_wrong_actor_gets_generic_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership check: a different, but still real,
    authenticated caller gets the identical generic not-found error -- never
    a distinct "not yours" message that would leak the row's existence.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _FakePendingApprovalStore()
    row, _code = store.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={
            "survivor_id": "cap_a",
            "absorbed_id": "cap_b",
            "acknowledge_governance_change": False,
            "state_digest": "d",
            "review_id": "review_aaa",
        },
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        display_summary={},
    )
    _override(monkeypatch, pending_approval_store_factory=_fake_store_factory(store))

    with _verified_actor(sub="someone-else", iss=_ACTOR_ISSUER):
        result = asyncio.run(
            _server().call_tool("check-near-miss-approval", {"pending_approval_id": row.id})
        )

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no pending approval" in text


# --- Failure-state completeness for both tools --------------------


def test_list_with_a_review_of_an_unregistered_kind_returns_an_error_naming_the_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    stray = _record("review_stray", kind=UNREGISTERED_KIND)
    _use_scripted_graph(monkeypatch, _ScriptedGraphHandle(steps=[[_row_for(stray)]]))

    result = _call_list_near_misses()

    assert result.is_error is False
    assert _text(result).startswith("error: ")
    assert UNREGISTERED_KIND in _text(result)


def test_list_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `run_list_near_misses` calls
    `dependencies.open_single_tenant_graph` directly, with no try/except of
    its own -- a generic exception from that opener must sanitise to the
    same fixed message `_resolve_graph`/`ingest-regulation`/
    `check-regulations` already use, never leaking host/port/driver detail,
    via the new `_sanitize_near_miss_review_graph_opens` wrapper (reusing
    the existing generic `_sanitize_graph_open` per-opener helper, not
    reinventing it).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _use_fake_graph_opener_only(monkeypatch, _raising_open)

    result = _call_list_near_misses()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_list_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4 / D-SANITIZE-UNEXPECTED's last row: an
    exception `list-near-misses`'s own body does not itself sanitise (here,
    the real `list_pending_reviews`'s own `graph.query(...)` call raising
    something unclassified, with the single-tenant graph already
    successfully opened) is caught by `_run_mcp_action`'s residual safety
    net -- returned as the fixed, generic message (never the raw exception
    text), with the full `repr` logged server-side only.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    graph = _ScriptedGraphHandle(steps=[ValueError("boom -- must never reach the caller")])
    _use_scripted_graph(monkeypatch, graph)

    result = _call_list_near_misses()

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "list-near-misses"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert "principal" not in failed_line
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))


def test_resolve_not_found_or_already_resolved_returns_named_error(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-SANITIZE-UNEXPECTED: `dependencies.resolve_review` returning `None`
    (`review_id` doesn't exist, or was already resolved -- both collapse to
    the same condition, per `PendingReviewNotFoundError`'s own docstring)
    is translated by `run_resolve_near_miss` into `PendingReviewNotFoundError`,
    which `resolve-near-miss`'s own body catches and returns as
    `error: <str(exc)>` verbatim -- no merge write, no elicitation round
    trip for this `keep-separate` decision.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    # `_FIND_REVIEW_QUERY` returns zero rows: the real `resolve_review`
    # returns `None` immediately, before any write query is ever issued.
    graph = _ScriptedGraphHandle(steps=[[]])
    _use_scripted_graph(monkeypatch, graph)

    result = _call_resolve_near_miss("review_missing", "keep-separate")

    assert result.is_error is False
    assert _text(result) == "error: no unresolved PendingReview with id 'review_missing'"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "resolve-near-miss"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert all("principal" not in line for line in lines)


def test_resolve_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `run_resolve_near_miss` calls
    `dependencies.open_single_tenant_graph` directly, with no try/except of
    its own -- a generic exception from that opener must sanitise to the
    same fixed message, via `_sanitize_near_miss_review_graph_opens` (same
    wrapper `list-near-misses` reuses above, not a second implementation).
    Uses `keep-separate` so no elicitation round trip is needed to reach
    this branch.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _use_fake_graph_opener_only(monkeypatch, _raising_open)

    result = _call_resolve_near_miss("review_aaa", "keep-separate")

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_resolve_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4 / D-SANITIZE-UNEXPECTED's last row: an
    exception `resolve-near-miss`'s own body does not itself sanitise
    (here, the real `resolve_review`'s own write-query call raising
    something unclassified, with the single-tenant graph already
    successfully opened and the review found) is caught by
    `_run_mcp_action`'s residual safety net -- returned as the fixed,
    generic message (never the raw exception text), with the full `repr`
    logged server-side only. Uses `keep-separate` so no elicitation round
    trip is needed to reach this branch.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    # Step 1 (`_FIND_REVIEW_QUERY`) succeeds; the delete is a gateway group whose log
    # append raises something the gateway and `submit_group` do not classify.
    graph = _ScriptedGraphHandle(steps=[[_row_for(_record("review_aaa"))], [["Capability"]]])
    _use_scripted_graph(monkeypatch, graph)
    rig = installed_process_gateway().rig
    rig.graphs.open("policy_system").seed_node(("PendingReview",), "review_aaa", {"kind": "x"})
    rig.store.fail_appends(error=RuntimeError("boom -- must never reach the caller"))

    result = _call_resolve_near_miss("review_aaa", "keep-separate")

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "resolve-near-miss"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert "principal" not in failed_line
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))


def test_resolve_keep_separate_of_a_review_of_an_unregistered_kind_returns_an_error_naming_the_kind(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    stray = _record("review_aaa", kind=UNREGISTERED_KIND)
    _use_scripted_graph(monkeypatch, _ScriptedGraphHandle(steps=[[_row_for(stray)]]))

    result = _call_resolve_near_miss("review_aaa", "keep-separate")

    assert result.is_error is False
    assert _text(result).startswith("error: ")
    assert UNREGISTERED_KIND in _text(result)
    assert audit_store.rows == []


# --- keep-separate audits `near_miss.resolve` ---


def _script_keep_separate(monkeypatch: pytest.MonkeyPatch) -> _ScriptedGraphHandle:
    graph = _ScriptedGraphHandle(steps=[[_row_for(_record("review_aaa"))], [["Capability"]], []])
    _use_scripted_graph(monkeypatch, graph)
    return graph


def test_resolve_near_miss_keep_separate_audits_the_verified_actor(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    configure()
    _script_keep_separate(monkeypatch)

    with _verified_actor():
        result = _call_resolve_near_miss("review_aaa", "keep-separate")

    assert result.is_error is False
    (row,) = audit_store.rows
    assert (row.actor_subject, row.actor_issuer) == (_ACTOR_SUBJECT, _ACTOR_ISSUER)
    assert (row.action, row.resource_id, row.outcome) == (
        "near_miss.resolve",
        "review_aaa",
        "applied",
    )
    assert row.details["decision"] == "keep_separate"


def test_resolve_near_miss_keep_separate_audits_system_local_test_bypass_under_bypass(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _script_keep_separate(monkeypatch)

    _call_resolve_near_miss("review_aaa", "keep-separate")

    (row,) = audit_store.rows
    assert (row.actor_subject, row.actor_issuer) == ("system:local-test-bypass",) * 2


def test_resolve_near_miss_returns_error_prefix_when_audit_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    configure()
    audit_store.fail_on_outcome["applied"] = AuditPostgresUnavailableError("down")
    graph = _script_keep_separate(monkeypatch)

    with _verified_actor():
        result = _call_resolve_near_miss("review_aaa", "keep-separate")

    assert _text(result).startswith("error: ")
    assert "audit trail" in _text(result)
    assert len(graph.calls) == 1  # only the lookup; no delete was issued
