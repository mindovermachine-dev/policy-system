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

Slice 3.3: `near_misses_resolve`'s `merge` path, plus AC-BI-006's
irreversibility guarantee. CHANGES.md H2 reverses D-MERGE-WARN's earlier
rejection of the MCP SDK's `Resolve`/`Elicit` mechanism: `near_misses_resolve`
gains a `confirmed: Annotated[bool | MergeConfirmation, Resolve(_confirm_merge)]`
parameter (`mcp_server._confirm_merge`) that resolves instantly (`True`, no
round trip) for `decision="keep-separate"` -- Slice 3.2's own test above is
unmodified and still passes, proving this -- and returns an `Elicit(...)`
marker for `decision="merge"`, forcing a real elicitation round trip before
the tool body ever runs. `test_merge_pauses_for_elicitation_then_executes_once_confirmed`
is this issue's primary AC-BI-006 proof (CHANGES.md H2's own "supersedes the
docstring test as sole evidence -- keep both"): round one (no prior answer)
returns an `InputRequiredResult` and never calls the fake `resolve_review`;
round two (an accepted `{"confirm": "merge"}` answer for the same wire key,
replaying round one's own `request_state`) executes the merge exactly once
with `winner_id`/`loser_id` populated. `test_keep_separate_never_elicits_single_round_trip`
is H2's third, explicit proof that `keep-separate` never enters this
transport at all. `test_docstring_states_merge_is_irreversible` is the
still-valid, now-secondary docstring-content proof (PLAN.md's own Slice 3.3
text) -- `test_domain_concepts_tool.py::test_tool_listed_alongside_cypher` is
this suite's only existing precedent for asserting on a tool's own listed
metadata (there `input_schema`, here `description`); no test in this suite
asserted on a tool's description text before this one.

`_elicitation_context()` (this file, new) builds a `Context` that negotiates
the `>= 2026-07-28` protocol revision with the elicitation form capability
declared -- the shape `resolve_arguments`/`_fulfil`
(`mcp/server/mcpserver/resolve.py`, read in full before writing this) needs
to actually batch the merge's question into an `InputRequiredResult` on round
one and resume from `request_state`/`input_responses` on round two, instead
of trying (and failing, with no real session) to send a synchronous
`elicitation/create` request the way a bare, context-less
`server.call_tool(...)` call would for a resolver that returns a marker.
Every other tool/decision in this suite keeps calling
`server.call_tool(name, args)` with no context at all, exactly as before --
that default bare `Context` has `protocol_version is None`, so
`_confirm_merge`'s `True` (non-marker) return for `keep-separate` is accepted
without ever touching `context.session`, confirmed by reading `_resolve`'s
own `_is_marker(result)` branch.

Slice 3.4 (this issue's last slice for Group 3): failure-state completeness
for both tools, per PLAN.md's D-SANITIZE-UNEXPECTED table and
`near_miss_review_orchestration.py`'s own module docstring (lines 25-46).
Read directly against `run_resolve_near_miss`'s own body (same file,
lines 172-182): `dependencies.resolve_review` raising
`StalePendingReviewError` is caught *inside* `run_resolve_near_miss` itself
and re-raised as `PendingReviewNotFoundError` with a dedicated "stale"
message -- `StalePendingReviewError` never escapes to this module's own
`near_misses_resolve` tool body. So, unlike PLAN.md's slice text (written
before this confirmation), the tool body needs exactly **one** except
clause (`except PendingReviewNotFoundError`), not two -- it already covers
both the not-found/already-resolved case (`resolve_review` returning `None`)
and the merge-only stale-reference case (`resolve_review` raising
`StalePendingReviewError`), distinguished only by `str(exc)`'s message text,
never by exception type. Two tests are still written for this one except
clause (`test_not_found_or_already_resolved_...`/
`test_merge_stale_reference_...`) because they exercise two genuinely
different fake-dependency behaviours (a `None` return vs. a raised
`StalePendingReviewError`, the latter needing a full `merge`-decision
elicitation round trip) and because the skill's own error-state table names
them as two distinct user-facing conditions (ps-qna's "never collapse
distinct error states" guardrail) -- not because the production code
branches on them separately. Graph-unavailable reuses the existing generic
`_sanitize_graph_open` helper via a new `_sanitize_near_miss_review_graph_opens`
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
import dataclasses
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from api.test_routes_near_misses import (
    _fake_dependencies,  # pyright: ignore[reportPrivateUsage]  -- reuse the existing REST-side fixture verbatim, not reinvented (PLAN.md Slice 3.1 instruction)
    _record,  # pyright: ignore[reportPrivateUsage]  -- same reuse, mirrors `_fake_dependencies`' own justification immediately above
)
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver.context import Context
from mcp.types import (
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    InputResponseRequestParams,
    TextContent,
)

from ps_service.company_merge.errors import StalePendingReviewError
from ps_service.company_merge.models import ResolveOutcome
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Literal

    import pytest
    from mcp.server.session import ServerSession
    from mcp.types import InputResponses

    from ps_service.company_merge.falkordb_client import GraphHandle

    type ReadLines = Callable[[Path], list[dict[str, object]]]


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
    """AC-BI-006, docstring-content half (PLAN.md's own Slice 3.3 text, kept
    as a secondary signal per CHANGES.md H2 -- the elicitation round-trip
    test below is the primary proof now). Mirrors
    `test_domain_concepts_tool.py::test_tool_listed_alongside_cypher`'s own
    `list_tools()` precedent for asserting on a tool's listed metadata --
    that test checks `input_schema`; no existing test in this suite asserts
    on a tool's `description` text, so this is the first.
    """
    tools = asyncio.run(mcp_server.server.list_tools())
    [tool] = [t for t in tools if t.name == "near_misses_resolve"]
    assert tool.description is not None
    assert "IRREVERSIBLE" in tool.description


def test_confirmed_gate_is_not_part_of_the_client_visible_schema() -> None:
    """CHANGES.md H2's own claim, verified directly (not just cited): a
    resolver-filled parameter (`confirmed`) is excluded from the tool's
    client-visible JSON argument schema -- confirmed by reading
    `Tool.from_function` (`mcp/server/mcpserver/tools/base.py`): resolved
    parameter names are added to `skip_names` before `func_metadata(...)`
    builds `arg_model`, so a calling model never has to (and cannot) supply
    `confirmed` directly.
    """
    tools = asyncio.run(mcp_server.server.list_tools())
    [tool] = [t for t in tools if t.name == "near_misses_resolve"]
    assert set(tool.input_schema.get("properties", {})) == {"review_id", "decision"}
    assert set(tool.input_schema.get("required", [])) == {"review_id", "decision"}


@dataclass
class _FakeSession:
    """Minimal duck-typed stand-in for `ServerSession`.

    `_confirm_merge`'s elicitation gate, on the `>= 2026-07-28` protocol path
    `_elicitation_context()` always negotiates, only ever reads
    `context.client_capabilities` (which routes through
    `session.client_capabilities`) -- confirmed by reading
    `mcp/server/mcpserver/resolve.py`'s `_fulfil`/`_require_capability` in
    full before writing this fake. Nothing else on `ServerSession` is ever
    touched by this code path, so nothing else needs faking here (mirrors
    `test_routes_near_misses.py`'s own `_FakeGraphHandle` -- a fake as narrow
    as the code path under test actually needs).
    """

    client_capabilities: ClientCapabilities


def _elicitation_context(
    *, input_responses: InputResponses | None = None, request_state: str | None = None
) -> Context[dict[str, object], object]:
    """Build a `Context` that negotiates the `>= 2026-07-28` `InputRequiredResult`
    transport, with the elicitation form capability declared.

    This is the shape `resolve_arguments`/`_fulfil` need to actually batch
    `_confirm_merge`'s question into an `InputRequiredResult` on round one and
    resume it from `request_state`/`input_responses` on round two -- a bare,
    context-less `server.call_tool(...)` call (every other test in this
    suite) has `protocol_version is None`, which routes a resolver's `Elicit`
    marker into the *synchronous* `ctx.elicit()` path instead
    (`resolve.py::_fulfil`'s `if not res.input_required` branch) and fails
    outright with no real session -- confirmed directly by running this
    scenario against a bare `server.call_tool(...)` call before writing this
    helper (it raises `ToolError` wrapping a `Context is not available
    outside of a request` `ValueError`, exactly as `resolve.py`'s own code
    predicts).
    """
    request_context: ServerRequestContext[dict[str, object], object] = ServerRequestContext(
        session=cast(
            "ServerSession",
            _FakeSession(
                client_capabilities=ClientCapabilities(
                    elicitation=ElicitationCapability(form=FormElicitationCapability())
                )
            ),
        ),
        lifespan_context={},
        protocol_version="2026-07-28",
        method="tools/call",
    )
    input_params = (
        InputResponseRequestParams(input_responses=input_responses, request_state=request_state)
        if input_responses is not None or request_state is not None
        else None
    )
    return Context(
        request_context=request_context, mcp_server=mcp_server.server, input_params=input_params
    )


def test_merge_pauses_for_elicitation_then_executes_once_confirmed(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """CHANGES.md H2's primary AC-BI-006 proof: a real elicitation round trip
    gates `decision="merge"`, not just docstring text.

    1. `decision="merge"` with no prior answer returns an `InputRequiredResult`
       and never calls the fake `resolve_review` delegate.
    2. Re-invoking with an accepted `{"confirm": "merge"}` answer for the
       same wire key (replaying round one's own `request_state`) executes the
       merge exactly once, with `winner_id`/`loser_id` populated (Slice 3.3's
       own "merge happy path" requirement, PLAN.md).
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

    dependencies, _ = _fake_dependencies((), resolve=_resolve)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )

    round_one = asyncio.run(
        mcp_server.server.call_tool(
            "near_misses_resolve",
            {"review_id": "review_aaa", "decision": "merge"},
            _elicitation_context(),
        )
    )

    assert isinstance(round_one, InputRequiredResult)
    assert recorded_calls == []

    requests = round_one.input_requests or {}
    [wire_key] = list(requests)
    elicit_request = requests[wire_key]
    assert isinstance(elicit_request, ElicitRequest)
    assert elicit_request.params.message == mcp_server._MERGE_WARNING  # pyright: ignore[reportPrivateUsage]  -- asserting the exact client-visible elicitation text, same convention as the docstring test above

    input_responses: InputResponses = {
        wire_key: ElicitResult(action="accept", content={"confirm": "merge"})
    }
    round_two = asyncio.run(
        mcp_server.server.call_tool(
            "near_misses_resolve",
            {"review_id": "review_aaa", "decision": "merge"},
            _elicitation_context(
                input_responses=input_responses, request_state=round_one.request_state
            ),
        )
    )

    assert isinstance(round_two, CallToolResult)
    assert round_two.is_error is False
    body = json.loads(_text(round_two))
    assert body == {
        "review_id": "review_aaa",
        "decision": "merge",
        "winner_id": "capability_existing_a",
        "loser_id": "capability_incoming_a",
    }
    assert recorded_calls == [("review_aaa", "merge")]

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "near_misses_resolve"
    ]
    # Round one never reaches the tool body at all (the resolver alone
    # answers `Tool.run`), so only round two's own started/succeeded pair is
    # logged here -- one call, one audited action, matching the one real
    # `resolve_review` invocation asserted above.
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


def test_keep_separate_never_elicits_single_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES.md H2's third proof: `decision="keep-separate"` never enters
    the elicitation transport at all -- one round trip, unchanged from Slice
    3.2's own behavior, proven here explicitly (not just implied by the
    unmodified passing of `test_keep_separate_resolves_with_winner_and_loser_left_none`
    above). Uses the plain, context-less `server.call_tool(...)` call every
    other tool in this suite uses -- no `>= 2026-07-28` transport negotiated
    at all -- since `_confirm_merge` never returns a marker for this
    decision, so `context.session` is never touched (confirmed by reading
    `resolve.py::_resolve`'s `_is_marker(result)` branch).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()

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

    result = asyncio.run(
        mcp_server.server.call_tool(
            "near_misses_resolve", {"review_id": "review_ccc", "decision": "keep-separate"}
        )
    )

    assert not isinstance(result, InputRequiredResult)
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert recorded_calls == [("review_ccc", "keep-separate")]


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


def test_resolve_merge_stale_reference_returns_named_error(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-SANITIZE-UNEXPECTED (merge-only row): `dependencies.resolve_review`
    raising `StalePendingReviewError` (the review's incoming/existing node
    was already deleted by a prior merge) is caught *inside*
    `run_resolve_near_miss` itself and re-raised as
    `PendingReviewNotFoundError` with a dedicated "stale" message (confirmed
    by reading `near_miss_review_orchestration.py`'s own body, lines
    172-182) -- `near_misses_resolve`'s own body catches that same
    `PendingReviewNotFoundError` type (one except clause covers both this
    and the plain not-found case above) and returns `error: <str(exc)>`
    verbatim. Exercises the full elicitation round trip since this is the
    `merge` decision: round one pauses, round two (confirmed) runs the body
    and hits the stale exception before any write.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph, decision
        raise StalePendingReviewError(review_id)

    dependencies, _ = _fake_dependencies((), resolve=_resolve)
    monkeypatch.setattr(
        mcp_server, "build_default_near_miss_review_dependencies", lambda: dependencies
    )

    round_one = asyncio.run(
        mcp_server.server.call_tool(
            "near_misses_resolve",
            {"review_id": "review_stale", "decision": "merge"},
            _elicitation_context(),
        )
    )
    assert isinstance(round_one, InputRequiredResult)

    requests = round_one.input_requests or {}
    [wire_key] = list(requests)
    input_responses: InputResponses = {
        wire_key: ElicitResult(action="accept", content={"confirm": "merge"})
    }
    round_two = asyncio.run(
        mcp_server.server.call_tool(
            "near_misses_resolve",
            {"review_id": "review_stale", "decision": "merge"},
            _elicitation_context(
                input_responses=input_responses, request_state=round_one.request_state
            ),
        )
    )

    assert isinstance(round_two, CallToolResult)
    assert round_two.is_error is False
    text = _text(round_two)
    assert text.startswith("error: ")
    assert "no longer exists" in text
    assert "stale" in text
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
