r"""Tests for `ps-author-policy`'s citation-persistence contract (issue #137, Slice 5).

Three proofs, per PLAN.md Section 5 "Slice 5":

1. `test_update_standard_draft_persists_procedure_with_sources_suffix` -- a
   scripted `create-policy-draft` -> `add-standard-to-draft` ->
   `update-standard-draft(standard_id, fields={"procedure": "<drafted
   text>\n\nSources: https://example.org/guidance"})` call against a
   module-local `_HybridGraph` fake (adapted from
   `test_ps_author_policy_standard_loop.py`'s own shape, Slice 3), asserting
   the persisted value's exact `Sources:` suffix via the fake's recorded
   `write_params` -- a direct, mechanical check of AC-BI-013's persistence
   contract, independent of how the citation was decided.
2. `test_update_control_draft_persists_execution_method_with_sources_suffix`
   -- the same shape one level deeper (`add-control-to-draft` ->
   `update-control-draft(control_id, fields={"execution_method": "<drafted
   text>\n\nSources: https://example.org/guidance"})`), adapted from
   `test_ps_author_policy_control_loop.py`'s own shape (Slice 4).
3. `test_update_standard_draft_without_sources_suffix_round_trips` -- a
   negative case: a plain `update-standard-draft` call on `procedure` with
   NO `Sources:` suffix (the Slice 3 default path) still round-trips
   correctly -- confirming the tool itself neither requires nor rejects the
   suffix either way. The discipline that a `Sources:` line is appended
   only when research was actually used, and only for actually-retrieved
   URLs, is the SKILL's own discipline (SKILL.md's "Web research and
   citations" note), never something `update-standard-draft`/
   `update-control-draft` enforce -- this file documents that boundary,
   which is exactly why PLAN.md Section 1(a)'s later content-lint test also
   checks the Guardrails text states the rule explicitly.

Fake graph: adapted from `test_ps_author_policy_control_loop.py`'s own
`_HybridGraph` (Slice 4), trimmed to only the dispatch branches this
slice's three scenarios actually exercise: seed-check,
`backfill_governance_status`'s idempotent no-op,
`find_standard_with_parent` (`update-standard-draft`'s own preamble, and
`add-control-to-draft`'s own preamble), `find_control_with_parent`
(`update-control-draft`'s own preamble), `read_policy_tree`
(`add-standard-to-draft`'s own preamble), and `find_existing_policy`
(`create-policy-draft`'s own new-id uniqueness check). Every write is
routed to one generic bucket with `params` captured alongside each query's
text (`write_params`), since neither `update-standard-draft`'s nor
`update-control-draft`'s own MCP response echoes back `updated_fields`
(Slices 3/4's own findings) -- the persisted VALUE, including any
`Sources:` suffix, is only observable via the fake's recorded params, the
same technique Slices 3/4 already established. Module-local, no
cross-file import -- matches this directory's established convention.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors test_policy_lifecycle_tools.py's own convention
    FakeAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  -- pins the exact seed-check query text, mirrors test_cypher_tool.py
)

if TYPE_CHECKING:
    from collections.abc import Generator

    import pytest

_ACTOR_SUBJECT = "policy-author"
_ACTOR_ISSUER = "https://issuer.example.com/"

_POLICY_TITLE = "Data Protection Policy"
_STANDARD_TITLE = "Encryption at Rest Standard"
_CONTROL_TITLE = "Encryption at Rest Automated Check"
_CONTROL_TYPE = "automated"

_SOURCE_URL = "https://example.org/guidance"

_PROCEDURE_WITH_SOURCE = (
    "1. Query each production data store's encryption-at-rest flag via the cloud "
    "provider API, weekly. 2. For any store returned as not encrypted, file a "
    "remediation ticket due within 5 business days. 3. Re-check on ticket closure."
    f"\n\nSources: {_SOURCE_URL}"
)
_PROCEDURE_WITHOUT_SOURCE = (
    "1. Query each production data store's encryption-at-rest flag via the cloud "
    "provider API, weekly. 2. For any store returned as not encrypted, file a "
    "remediation ticket due within 5 business days. 3. Re-check on ticket closure."
)
_EXECUTION_METHOD_WITH_SOURCE = (
    "An automated script queries the cloud provider's encryption-status API for "
    "every in-scope data store, on a weekly schedule."
    f"\n\nSources: {_SOURCE_URL}"
)


# --- fakes (adapted from test_ps_author_policy_control_loop.py, Slice 4) ---


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _ACTOR_ISSUER) -> Generator[None]:
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted values."""

    def __init__(
        self,
        *,
        header: list[object] | None = None,
        result_set: list[object] | None = None,
    ) -> None:
        self.header = header or []
        self.result_set = result_set or []


@dataclass
class _PolicyTreeFixture:
    """Minimal stand-in for `graph_writer.read_policy_tree`'s own `PolicyRecord`.

    Needed because `add-standard-to-draft`'s own preamble,
    `_read_transition_target` (service.py:1498), still reads the Policy tree
    this way -- carried over unchanged from Slices 3/4.
    """

    id: str
    title: str
    status: str
    version: str = "1"
    owner_subject: str = _ACTOR_SUBJECT
    owner_issuer: str = _ACTOR_ISSUER


@dataclass
class _StandardParentFixture:
    """Stand-in for `graph_writer.find_standard_with_parent`'s own `StandardWithParent`.

    Read by both `update-standard-draft`'s own preamble and
    `add-control-to-draft`'s own preamble -- carried over unchanged from
    Slices 3/4.
    """

    policy_id: str
    policy_owner_subject: str = _ACTOR_SUBJECT
    policy_owner_issuer: str = _ACTOR_ISSUER
    policy_status: str = "draft"
    standard_status: str = "draft"
    standard_title: str = ""


@dataclass
class _ControlParentFixture:
    """Stand-in for `graph_writer.find_control_with_parent`'s own `ControlWithParent`.

    Read (twice) by `update-control-draft`'s own preamble -- carried over
    unchanged from Slice 4.
    """

    standard_id: str
    policy_id: str
    policy_owner_subject: str = _ACTOR_SUBJECT
    policy_owner_issuer: str = _ACTOR_ISSUER
    policy_status: str = "draft"
    control_status: str = "draft"
    control_title: str = ""


class _HybridGraph:
    """Trimmed from `test_ps_author_policy_control_loop.py`'s own `_HybridGraph` (Slice 4).

    Only the dispatch branches this slice's three scenarios actually
    exercise: seed-check, `backfill_governance_status`'s idempotent no-op,
    `find_standard_with_parent`, `find_control_with_parent`,
    `read_policy_tree`, `find_existing_policy`. Every write is routed to
    one generic bucket, with `params` captured alongside each query's text
    (`write_params`) -- same convention as Slices 3/4, needed because
    neither `update-standard-draft`'s nor `update-control-draft`'s own MCP
    response echoes back `updated_fields`.
    """

    def __init__(self) -> None:
        self.policy_tree: _PolicyTreeFixture | None = None
        self.standards: dict[str, _StandardParentFixture] = {}
        self.controls: dict[str, _ControlParentFixture] = {}
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object]] = []

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        p = params or {}
        if q == _SEED_CHECK_QUERY:
            return _FakeQueryResult(header=[[0, "c"]], result_set=[[1]])
        if "coalesce(p.version" in q or "IS NULL" in q:
            # `graph_writer.backfill_governance_status`'s idempotent no-op --
            # issued ahead of every read branch below. Structural no-op,
            # same as Slices 1-4.
            return _FakeQueryResult()
        if "RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status, c.status, c.title" in q:
            # `graph_writer.find_control_with_parent` -- `update-control-
            # draft`'s own preamble (the two-hop case).
            control_id = cast("str", p["control_id"])
            fixture = self.controls.get(control_id)
            if fixture is None:
                return _FakeQueryResult(result_set=[])
            row = [
                fixture.standard_id,
                fixture.policy_id,
                fixture.policy_owner_subject,
                fixture.policy_owner_issuer,
                fixture.policy_status,
                fixture.control_status,
                fixture.control_title,
            ]
            return _FakeQueryResult(result_set=[row])
        if "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title" in q:
            # `graph_writer.find_standard_with_parent` -- `update-standard-
            # draft`'s and `add-control-to-draft`'s own preambles (one-hop).
            standard_id = cast("str", p["standard_id"])
            fixture = self.standards.get(standard_id)
            if fixture is None:
                return _FakeQueryResult(result_set=[])
            row = [
                fixture.policy_id,
                fixture.policy_owner_subject,
                fixture.policy_owner_issuer,
                fixture.policy_status,
                fixture.standard_status,
                fixture.standard_title,
            ]
            return _FakeQueryResult(result_set=[row])
        if "s.id, s.title, s.status, c.id" in q:
            # `graph_writer.read_policy_tree` -- `add-standard-to-draft`'s
            # own preamble. Checked BEFORE `find_existing_policy`'s shorter
            # substring below (its own `RETURN` clause contains it) -- same
            # ordering discipline Slices 1-4 already established.
            if self.policy_tree is None:
                return _FakeQueryResult(result_set=[])
            pt = self.policy_tree
            row = [
                pt.id,
                pt.title,
                pt.status,
                pt.version,
                pt.owner_subject,
                pt.owner_issuer,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ]
            return _FakeQueryResult(result_set=[row])
        if "RETURN p.id, p.title" in q:
            # `graph_writer.find_existing_policy` -- `create-policy-draft`'s
            # own new-id uniqueness check.
            return _FakeQueryResult(result_set=[])
        self.write_queries.append(q)
        self.write_params.append(p)
        return _FakeQueryResult()


class _FakeFalkorDB:
    """Stands in for the eager `falkordb.FalkorDB` client."""

    def __init__(self, handle: _HybridGraph) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> _HybridGraph:
        del name
        return self._handle


def _install_graph(monkeypatch: pytest.MonkeyPatch, handle: _HybridGraph) -> None:
    def _connect_from_config(_config: object) -> _FakeFalkorDB:
        return _FakeFalkorDB(handle)

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect_from_config)


class _FakeAuditStore:
    """Records every `record_standalone` call.

    Unused by this slice's own assertions, but every one of these tools
    always constructs one for its access-role store -- so it must exist and
    not raise.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    def record_standalone(self, **kwargs: object) -> None:
        self.calls.append(kwargs)

    def query(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError


def _install_audit_store(monkeypatch: pytest.MonkeyPatch, store: _FakeAuditStore) -> None:
    def _factory(_config: object) -> _FakeAuditStore:
        return store

    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", _factory)


def _install_access_role_store(monkeypatch: pytest.MonkeyPatch, store: object) -> None:
    def _factory(_config: object, **_kwargs: object) -> object:
        return store

    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory)


# --- tool-call helpers -------------------------------------------------


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _call_create_policy_draft(*, title: str) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("create-policy-draft", {"title": title}))
    assert isinstance(result, CallToolResult)
    return result


def _call_add_standard_to_draft(*, policy_id: str, title: str) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool(
            "add-standard-to-draft", {"policy_id": policy_id, "title": title}
        )
    )
    assert isinstance(result, CallToolResult)
    return result


def _call_add_control_to_draft(
    *, standard_id: str, title: str, control_type: str
) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool(
            "add-control-to-draft",
            {"standard_id": standard_id, "title": title, "control_type": control_type},
        )
    )
    assert isinstance(result, CallToolResult)
    return result


def _call_update_standard_draft(standard_id: str, fields: dict[str, object]) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool(
            "update-standard-draft", {"standard_id": standard_id, "fields": fields}
        )
    )
    assert isinstance(result, CallToolResult)
    return result


def _call_update_control_draft(control_id: str, fields: dict[str, object]) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool(
            "update-control-draft", {"control_id": control_id, "fields": fields}
        )
    )
    assert isinstance(result, CallToolResult)
    return result


# --- shared sequence-building helpers -----------------------------------


def _new_policy(monkeypatch: pytest.MonkeyPatch, handle: _HybridGraph) -> str:
    _install_graph(monkeypatch, handle)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())
    create_result = _call_create_policy_draft(title=_POLICY_TITLE)
    assert create_result.is_error is False
    created = json.loads(_text(create_result))
    policy_id = cast("str", created["policy_id"])
    handle.policy_tree = _PolicyTreeFixture(id=policy_id, title=_POLICY_TITLE, status="draft")
    return policy_id


def _new_standard(handle: _HybridGraph, *, policy_id: str, title: str) -> str:
    add_result = _call_add_standard_to_draft(policy_id=policy_id, title=title)
    assert add_result.is_error is False
    added = json.loads(_text(add_result))
    standard_id = cast("str", added["standard_id"])
    handle.standards[standard_id] = _StandardParentFixture(
        policy_id=policy_id, standard_title=title
    )
    return standard_id


def _new_control(
    handle: _HybridGraph, *, standard_id: str, policy_id: str, title: str, control_type: str
) -> str:
    add_result = _call_add_control_to_draft(
        standard_id=standard_id, title=title, control_type=control_type
    )
    assert add_result.is_error is False
    added = json.loads(_text(add_result))
    control_id = cast("str", added["control_id"])
    handle.controls[control_id] = _ControlParentFixture(
        standard_id=standard_id, policy_id=policy_id, control_title=title
    )
    return control_id


# --- (1) update-standard-draft persists a Sources: suffix ------------------


def test_update_standard_draft_persists_procedure_with_sources_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-013: a `procedure` value that used web research persists its
    literal `Sources: <url>` suffix, verbatim, via the same
    `update-standard-draft` call Slice 3's field loop already uses.
    """
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        standard_id = _new_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)

        result = _call_update_standard_draft(standard_id, {"procedure": _PROCEDURE_WITH_SOURCE})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["standard_id"] == standard_id
    assert body["policy_id"] == policy_id

    # write_queries[0] = create-policy-draft's MERGE; [1] = add-standard-to-
    # draft's MERGE; [2] = this update-standard-draft's SET.
    assert len(handle.write_queries) == 3
    write_query = handle.write_queries[-1]
    assert "MATCH (s:Standard {id: $standard_id}) SET s += $set_properties" in write_query

    persisted_procedure = cast("str", handle.write_params[-1]["set_properties"]["procedure"])  # type: ignore[index]
    assert persisted_procedure == _PROCEDURE_WITH_SOURCE
    assert persisted_procedure.endswith(f"Sources: {_SOURCE_URL}")


# --- (2) update-control-draft persists a Sources: suffix --------------------


def test_update_control_draft_persists_execution_method_with_sources_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract as (1), one level deeper: `execution_method` (C-002)
    persists its literal `Sources: <url>` suffix via the same
    `update-control-draft` call Slice 4's field loop already uses.
    """
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        standard_id = _new_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)
        control_id = _new_control(
            handle,
            standard_id=standard_id,
            policy_id=policy_id,
            title=_CONTROL_TITLE,
            control_type=_CONTROL_TYPE,
        )

        result = _call_update_control_draft(
            control_id, {"execution_method": _EXECUTION_METHOD_WITH_SOURCE}
        )

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["control_id"] == control_id
    assert body["standard_id"] == standard_id
    assert body["policy_id"] == policy_id

    # write_queries[0] = create-policy-draft's MERGE; [1] = add-standard-to-
    # draft's MERGE; [2] = add-control-to-draft's MERGE; [3] = this
    # update-control-draft's SET.
    assert len(handle.write_queries) == 4
    write_query = handle.write_queries[-1]
    assert "MATCH (c:Control {id: $control_id}) SET c += $set_properties" in write_query

    persisted_method = cast(
        "str",
        handle.write_params[-1]["set_properties"]["execution_method"],  # type: ignore[index]
    )
    assert persisted_method == _EXECUTION_METHOD_WITH_SOURCE
    assert persisted_method.endswith(f"Sources: {_SOURCE_URL}")


# --- (3) negative case: no Sources: suffix still round-trips ---------------


def test_update_standard_draft_without_sources_suffix_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Slice 3 default path (research declined/not used): a plain
    `procedure` value with NO `Sources:` suffix still round-trips correctly
    -- proving `update-standard-draft` neither requires nor rejects the
    suffix. The append-only-when-used, only-real-URLs discipline is the
    SKILL's own (SKILL.md), never the tool's.
    """
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        standard_id = _new_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)

        result = _call_update_standard_draft(standard_id, {"procedure": _PROCEDURE_WITHOUT_SOURCE})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["standard_id"] == standard_id
    assert body["policy_id"] == policy_id

    persisted_procedure = cast("str", handle.write_params[-1]["set_properties"]["procedure"])  # type: ignore[index]
    assert persisted_procedure == _PROCEDURE_WITHOUT_SOURCE
    assert "Sources:" not in persisted_procedure
