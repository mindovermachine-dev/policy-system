"""Tests for `ps-author-policy`'s branch-detection sequence (issue #137, Slice 1).

Proves the tool-call SEQUENCE that `ps-skills/ps-plugin/skills/
ps-author-policy/SKILL.md`'s Process section documents (branch detection:
not-found / fresh / resume / fork / disagreement) actually works end to end
against the real `cypher`/`create-policy-draft`/`get-policy` MCP tools --
"red" here means proving this sequence, not that the tools themselves don't
exist yet (they already do, issue #136). Every scenario drives the real
tool functions directly (never the SKILL.md prose, which pytest cannot
execute) and asserts on the fake graph's recorded `write_queries`/
`cypher_queries` and on each tool's return value/raised exception, mirroring
PLAN.md §5 Slice 1's own scenario descriptions and CHANGES.md Appendix B.2's
6th (disagreement) scenario.

Fake graph: CHANGES.md Appendix A's `_HybridGraph` (F-1's resolution),
combining `test_cypher_tool.py`'s `_FakeGraphHandle` per-call result/error
FIFO-queue scripting for the freehand `cypher` tool with
`test_policy_lifecycle_tools.py`'s `_FakeGraph` literal-query-substring
dispatch for the write-side content-CRUD tools -- extended here with one
additional literal-dispatch branch for the Policy-tree read
(`graph_writer.read_policy_tree`'s own `"s.id, s.title, s.status, c.id"`
column-list substring), reused by BOTH `get-policy` (the resume/owned and
resume/not-owned scenarios) and `create-policy-draft`'s own
`supersedes_policy_id` prior-status check (`_read_transition_target`, the
fork scenarios) -- not spelled out row-by-row in Appendix A's code sample,
but called for by its own note ("add the analogous fixed strings ... read
verbatim from graph_writer.py the same way test_policy_lifecycle_tools.py:
94-101 does -- do not invent them"); see IMPL_SLICE_1.md for the exact
citation. A second additional branch handles `read_policy_tree_for_fork`'s
own distinguishing `"RETURN s.id, properties(s), c.id, properties(c)"`
query (issued by a successful fork's `_build_forked_standard_drafts`,
BEFORE the graph write) -- routed to its own empty-result branch so it is
never miscounted as a write. A third additional branch treats
`backfill_governance_status`'s three idempotent `SET` statements (issued
ahead of every `read_policy_tree*` read, D-7) as a structural no-op, exactly
as `test_policy_lifecycle_tools.py`'s own `_TreeFakeGraph`/`_StatefulFakeGraph`
already do.

`_verified_actor`/`_install_graph`/`_install_audit_store`/
`_install_access_role_store` are local copies of
`test_policy_lifecycle_tools.py`'s own helpers of the same name, read
verbatim from that file per PLAN.md §0.6.
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
    from collections.abc import Generator, Mapping

    import pytest

_ACTOR_SUBJECT = "policy-author"
_ACTOR_ISSUER = "https://issuer.example.com/"
_OTHER_SUBJECT = "someone-else"

_CAPABILITY_NAME = "Data Protection"
_CAPABILITY_ID = "cap_data_protection"
_CAPABILITY_B_NAME = "Encryption Key Management"
_CAPABILITY_B_ID = "cap_encryption_key_management"

_DRAFT_POLICY_ID = "pol_data_protection_policy_aaaaaa"
_APPROVED_POLICY_ID = "pol_data_protection_policy_bbbbbb"
_PROPOSED_POLICY_ID = "pol_data_protection_policy_cccccc"
_DRAFT_POLICY_B_ID = "pol_encryption_key_mgmt_policy_dddddd"


# --- the exact Cypher query shapes PLAN.md §5 Slice 1 steps 2-3 specify ----


def _capability_existence_query(name: str) -> str:
    return (
        f"MATCH (c:Capability {{name: '{name}'}}) "
        "RETURN c.name AS capability_name, c.id AS capability_id"
    )


def _capability_governed_by_query(name: str) -> str:
    return (
        f"MATCH (c:Capability {{name: '{name}'}})-[:GOVERNED_BY]->(p:Policy) "
        "OPTIONAL MATCH (p)-[:SUPERSEDED_BY]->(f:Policy) "
        "RETURN p.id AS policy_id, p.status AS status, f.id AS fork_id, f.status AS fork_status"
    )


# --- fakes (CHANGES.md Appendix A, extended per this file's own docstring) -


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
    """Satisfies `GraphQueryResult` structurally with scripted values.

    Two-field shape (`header` + `result_set`), per Appendix A's note: the
    `cypher` tool path needs `header` to build its `columns` response; the
    write-tool path never reads it (`header=[]` is a harmless default).
    """

    def __init__(
        self,
        *,
        header: list[object] | None = None,
        result_set: list[object] | None = None,
    ) -> None:
        self.header = header or []
        self.result_set = result_set or []


_CAP_EXISTENCE_HEADER: list[object] = [[0, "capability_name"], [0, "capability_id"]]
_GOVERNED_BY_HEADER: list[object] = [
    [0, "policy_id"],
    [0, "status"],
    [0, "fork_id"],
    [0, "fork_status"],
]


def _existence_result(rows: list[object]) -> _FakeQueryResult:
    return _FakeQueryResult(header=_CAP_EXISTENCE_HEADER, result_set=rows)


def _governed_by_result(rows: list[object]) -> _FakeQueryResult:
    return _FakeQueryResult(header=_GOVERNED_BY_HEADER, result_set=rows)


@dataclass
class _PolicyTreeFixture:
    """Minimal stand-in for `graph_writer.read_policy_tree`'s own `PolicyRecord`.

    Zero Standards/Controls throughout -- Slice 1's branch-detection
    scenarios never need tree children, only the Policy's own id/status/
    owner.
    """

    id: str
    title: str
    status: str
    version: str = "1"
    owner_subject: str = _ACTOR_SUBJECT
    owner_issuer: str = _ACTOR_ISSUER


class _HybridGraph:
    """CHANGES.md Appendix A's fake, extended with a Policy-tree fixture.

    Combines `test_cypher_tool.py`'s `_FakeGraphHandle` per-call
    result/error scripting (generalized to a FIFO queue -- the disagreement
    scenario needs four distinct scripted `cypher` results in one test) with
    `test_policy_lifecycle_tools.py`'s `_FakeGraph` literal-query dispatch
    for the write-side content-CRUD tools. One instance serves both call
    shapes because both connect through the same single
    `connect_from_config` monkeypatch point within one test.
    """

    def __init__(
        self,
        *,
        existing: tuple[str, str] | None = None,
        policy_tree: _PolicyTreeFixture | None = None,
        cypher_results: list[_FakeQueryResult | Exception] | None = None,
        governors: dict[str, str | None] | None = None,
    ) -> None:
        self._governors = governors or {}
        self.claim_params: list[dict[str, object]] = []
        self._existing = existing
        self._policy_tree = policy_tree
        self._cypher_results = list(cypher_results or [])
        self.write_queries: list[str] = []
        self.cypher_queries: list[str] = []
        self.raise_on_write: Exception | None = None

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        if q == _SEED_CHECK_QUERY:
            return _FakeQueryResult(header=[[0, "c"]], result_set=[[1]])
        if "coalesce(p.version" in q or "IS NULL" in q:
            # `graph_writer.backfill_governance_status`'s three idempotent,
            # already-backfilled-safe `SET` statements -- issued ahead of
            # every `read_policy_tree*` read (D-7). A structural no-op here,
            # exactly as `test_policy_lifecycle_tools.py`'s own
            # `_TreeFakeGraph`/`_StatefulFakeGraph` treat them (never
            # recorded as a "write" this test cares about).
            return _FakeQueryResult()
        if "RETURN cap.id, g.id" in q:
            # `graph_writer.read_capability_governors` (issue #185): the fresh
            # create's existence/ungoverned pre-check.
            requested = cast("list[str]", (params or {})["capability_ids"])
            return _FakeQueryResult(
                result_set=[[c, self._governors[c]] for c in requested if c in self._governors]
            )
        if "FOREACH (c IN caps | MERGE (c)-[:GOVERNED_BY]->(p))" in q:
            # The guarded fresh-create statement: one write, returns its row.
            self.write_queries.append(q)
            self.claim_params.append(dict(params or {}))
            return _FakeQueryResult(result_set=[["pol"]])
        if "RETURN s.id, properties(s), c.id, properties(c)" in q:
            # `graph_writer.read_policy_tree_for_fork` -- only reached on a
            # successful fork, before the actual graph write; this fixture's
            # Policy always has zero Standards, so this is always empty.
            return _FakeQueryResult(result_set=[])
        if "s.id, s.title, s.status, c.id" in q:
            # `graph_writer.read_policy_tree` -- shared by `get-policy`'s own
            # read and by `create-policy-draft(supersedes_policy_id=...)`'s
            # `_read_transition_target` prior-status check. Checked BEFORE
            # the `RETURN p.id, p.title` existence-check branch below: this
            # query's own `RETURN` clause is `"RETURN p.id, p.title,
            # p.status, ..."`, which contains that branch's shorter
            # substring, so the more specific match must win here (mirrors
            # `test_policy_lifecycle_tools.py`'s own "checked BEFORE"
            # ordering discipline for its overlapping-substring branches).
            if self._policy_tree is None:
                return _FakeQueryResult(result_set=[])
            p = self._policy_tree
            row = [
                p.id,
                p.title,
                p.status,
                p.version,
                p.owner_subject,
                p.owner_issuer,
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
            # `graph_writer.find_existing_policy` -- create-policy-draft's
            # own new-id uniqueness check (both the ordinary and fork path).
            rows: list[object] = [[self._existing[0], self._existing[1]]] if self._existing else []
            return _FakeQueryResult(result_set=rows)
        if q.strip().upper().startswith(("MATCH", "RETURN")) and self._cypher_results:
            # The skill's own freehand `cypher`-tool call (branch-detection
            # existence/GOVERNED_BY reads) -- dequeue the next scripted
            # outcome.
            self.cypher_queries.append(q)
            outcome = self._cypher_results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self.write_queries.append(q)
        if self.raise_on_write is not None:
            raise self.raise_on_write
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


@dataclass
class _RecordedAuditCall:
    actor_subject: str
    actor_issuer: str
    action: str
    resource_id: str
    outcome: str
    details: Mapping[str, object]


class _FakeAuditStore:
    """Records every `record_standalone` call (copied from `test_policy_lifecycle_tools.py`)."""

    def __init__(self) -> None:
        self.calls: list[_RecordedAuditCall] = []

    def record(self, *args: object, **kwargs: object) -> str:
        raise NotImplementedError

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        details: Mapping[str, object],
    ) -> None:
        del resource_type
        self.calls.append(
            _RecordedAuditCall(
                actor_subject=actor_subject,
                actor_issuer=actor_issuer,
                action=action,
                resource_id=resource_id,
                outcome=outcome,
                details=details,
            )
        )

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


def _call_cypher(query: str) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("cypher", {"query": query}))
    assert isinstance(result, CallToolResult)
    return result


def _cypher_body(result: CallToolResult) -> dict[str, object]:
    text = _text(result)
    assert not text.startswith("error:"), text
    return cast("dict[str, object]", json.loads(text))


def _call_get_policy(policy_id: str) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("get-policy", {"policy_id": policy_id}))
    assert isinstance(result, CallToolResult)
    return result


def _call_create_policy_draft(
    *,
    title: str,
    supersedes_policy_id: str | None = None,
    capability_ids: list[str] | None = None,
) -> CallToolResult:
    args: dict[str, object] = {"title": title}
    if capability_ids is not None:
        args["capability_ids"] = capability_ids
    if supersedes_policy_id is not None:
        args["supersedes_policy_id"] = supersedes_policy_id
    result = asyncio.run(mcp_server.server.call_tool("create-policy-draft", args))
    assert isinstance(result, CallToolResult)
    return result


# --- (1) Capability not found -------------------------------------------


def test_scenario_1_capability_not_found_no_create_policy_draft_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph(cypher_results=[_existence_result([])])
    _install_graph(monkeypatch, handle)

    existence_result = _call_cypher(_capability_existence_query(_CAPABILITY_NAME))

    body = _cypher_body(existence_result)
    assert body["rows"] == []
    assert handle.cypher_queries == [_capability_existence_query(_CAPABILITY_NAME)]
    assert handle.write_queries == []


# --- (2) fresh: no governing Policy --------------------------------------


def test_scenario_2_no_governing_policy_is_the_fresh_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph(
        cypher_results=[
            _existence_result([[_CAPABILITY_NAME, _CAPABILITY_ID]]),
            _governed_by_result([]),
        ],
        governors={_CAPABILITY_ID: None},
    )
    _install_graph(monkeypatch, handle)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    existence_result = _call_cypher(_capability_existence_query(_CAPABILITY_NAME))
    existence_rows = cast("list[list[str]]", _cypher_body(existence_result)["rows"])
    assert existence_rows == [[_CAPABILITY_NAME, _CAPABILITY_ID]]

    governed_by_result = _call_cypher(_capability_governed_by_query(_CAPABILITY_NAME))
    assert _cypher_body(governed_by_result)["rows"] == []

    assert len(handle.cypher_queries) == 2
    assert handle.write_queries == []

    # Scaffold (issue #185): the skill passes the ids step 2 returned.
    found_ids = [row[1] for row in existence_rows]
    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = _call_create_policy_draft(
            title="Data Protection Policy", capability_ids=found_ids
        )

    assert create_result.is_error is False
    body = json.loads(_text(create_result))
    assert body["governed_capability_ids"] == [_CAPABILITY_ID]
    assert len(handle.write_queries) == 1  # the one guarded claim statement
    assert handle.claim_params[0]["capability_ids"] == [_CAPABILITY_ID]


def test_scenario_2b_fresh_create_for_already_governed_capability_names_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph(governors={_CAPABILITY_ID: "pol_someone_elses"})
    _install_graph(monkeypatch, handle)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft(title="X Policy", capability_ids=[_CAPABILITY_ID])

    assert "already governed by a Policy" in _text(result)
    assert handle.write_queries == []


def test_scenario_2c_fresh_create_for_unknown_capability_names_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph(governors={})
    _install_graph(monkeypatch, handle)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft(title="X Policy", capability_ids=["cap_missing"])

    assert "no Capability exists with id(s)" in _text(result)
    assert handle.write_queries == []


# --- (3) resume: caller-owned Draft --------------------------------------


def test_scenario_3_resume_caller_owned_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    tree = _PolicyTreeFixture(
        id=_DRAFT_POLICY_ID,
        title="Data Protection Policy",
        status="draft",
        owner_subject=_ACTOR_SUBJECT,
    )
    handle = _HybridGraph(
        policy_tree=tree,
        cypher_results=[
            _existence_result([[_CAPABILITY_NAME, _CAPABILITY_ID]]),
            _governed_by_result([[_DRAFT_POLICY_ID, "draft", None, None]]),
        ],
    )
    _install_graph(monkeypatch, handle)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    _call_cypher(_capability_existence_query(_CAPABILITY_NAME))
    governed_by_result = _call_cypher(_capability_governed_by_query(_CAPABILITY_NAME))
    assert _cypher_body(governed_by_result)["rows"] == [[_DRAFT_POLICY_ID, "draft", None, None]]

    with _verified_actor(sub=_ACTOR_SUBJECT):
        get_result = _call_get_policy(_DRAFT_POLICY_ID)

    assert get_result.is_error is False
    body = json.loads(_text(get_result))
    assert body["policy_id"] == _DRAFT_POLICY_ID
    assert body["status"] == "draft"
    assert body["owner_subject"] == _ACTOR_SUBJECT
    # A read-only resume detection -- Slice 2's field loop does the writing.
    assert handle.write_queries == []


# --- (4) governed_by_unowned_draft ---------------------------------------


def test_scenario_4_governing_draft_not_caller_owned_reports_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    tree = _PolicyTreeFixture(
        id=_DRAFT_POLICY_ID,
        title="Data Protection Policy",
        status="draft",
        owner_subject=_OTHER_SUBJECT,
    )
    handle = _HybridGraph(
        policy_tree=tree,
        cypher_results=[
            _existence_result([[_CAPABILITY_NAME, _CAPABILITY_ID]]),
            _governed_by_result([[_DRAFT_POLICY_ID, "draft", None, None]]),
        ],
    )
    _install_graph(monkeypatch, handle)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    _call_cypher(_capability_existence_query(_CAPABILITY_NAME))
    _call_cypher(_capability_governed_by_query(_CAPABILITY_NAME))

    with _verified_actor(sub=_ACTOR_SUBJECT):
        get_result = _call_get_policy(_DRAFT_POLICY_ID)

    assert get_result.is_error is False
    assert _text(get_result) == "error: you do not have access to this Policy"
    # D-1/D-2: not forkable either (only an approved prior can be forked) --
    # block and report, never a competing `create-policy-draft` call.
    assert handle.write_queries == []


# --- (5) fork: governing Approved Policy ---------------------------------


def test_scenario_5_fork_from_approved_prior_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    tree = _PolicyTreeFixture(
        id=_APPROVED_POLICY_ID,
        title="Data Protection Policy",
        status="approved",
        owner_subject=_OTHER_SUBJECT,
    )
    handle = _HybridGraph(
        policy_tree=tree,
        cypher_results=[
            _existence_result([[_CAPABILITY_NAME, _CAPABILITY_ID]]),
            _governed_by_result([[_APPROVED_POLICY_ID, "approved", None, None]]),
        ],
    )
    _install_graph(monkeypatch, handle)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    _call_cypher(_capability_existence_query(_CAPABILITY_NAME))
    governed_by_result = _call_cypher(_capability_governed_by_query(_CAPABILITY_NAME))
    assert _cypher_body(governed_by_result)["rows"] == [
        [_APPROVED_POLICY_ID, "approved", None, None]
    ]

    with _verified_actor(sub=_ACTOR_SUBJECT):
        fork_result = _call_create_policy_draft(
            title="Data Protection Policy v2", supersedes_policy_id=_APPROVED_POLICY_ID
        )

    assert fork_result.is_error is False
    body = json.loads(_text(fork_result))
    assert body["status"] == "draft"
    assert body["version"] == "2"
    assert body["superseded_policy_id"] == _APPROVED_POLICY_ID
    # The new forked draft's own persisted MERGE write, plus the
    # Policy-level SUPERSEDED_BY lineage edge (D-2's own fork write shape).
    assert len(handle.write_queries) == 2
    assert any("SET p += $properties" in q for q in handle.write_queries)
    assert any("SUPERSEDED_BY" in q for q in handle.write_queries)


# --- (5b) fork: governing Proposed Policy --------------------------------


def test_scenario_5b_fork_from_proposed_prior_reports_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    tree = _PolicyTreeFixture(
        id=_PROPOSED_POLICY_ID,
        title="Data Protection Policy",
        status="proposed",
        owner_subject=_OTHER_SUBJECT,
    )
    handle = _HybridGraph(
        policy_tree=tree,
        cypher_results=[
            _existence_result([[_CAPABILITY_NAME, _CAPABILITY_ID]]),
            _governed_by_result([[_PROPOSED_POLICY_ID, "proposed", None, None]]),
        ],
    )
    _install_graph(monkeypatch, handle)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    _call_cypher(_capability_existence_query(_CAPABILITY_NAME))
    governed_by_result = _call_cypher(_capability_governed_by_query(_CAPABILITY_NAME))
    assert _cypher_body(governed_by_result)["rows"] == [
        [_PROPOSED_POLICY_ID, "proposed", None, None]
    ]

    with _verified_actor(sub=_ACTOR_SUBJECT):
        # D-2: the fork is ALWAYS attempted for a proposed/approved prior --
        # never blocked pre-emptively -- and this exact attempt is rejected
        # by the tool itself with the named
        # `PolicySupersedePriorNotApprovedError`.
        fork_result = _call_create_policy_draft(
            title="Data Protection Policy v2", supersedes_policy_id=_PROPOSED_POLICY_ID
        )

    assert fork_result.is_error is False
    text = _text(fork_result)
    assert text == (
        f"error: Policy {_PROPOSED_POLICY_ID!r} cannot be superseded: "
        "current status is 'proposed', requires 'approved'"
    )
    # Rejected before `find_existing_policy`/the fork-content read/any write.
    assert handle.write_queries == []


# --- (6) multi-Capability GOVERNED_BY disagreement (D-6, CHANGES.md B.2) --


def test_scenario_6_multi_capability_governed_by_disagreement_blocks_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _HybridGraph(
        cypher_results=[
            _existence_result([[_CAPABILITY_NAME, _CAPABILITY_ID]]),
            _existence_result([[_CAPABILITY_B_NAME, _CAPABILITY_B_ID]]),
            _governed_by_result([]),  # A: ungoverned
            _governed_by_result([[_DRAFT_POLICY_B_ID, "draft", None, None]]),  # B: draft
        ]
    )
    configure()
    _install_graph(monkeypatch, handle)

    # PLAN.md §5 Slice 1 steps 2-3: the existence-check loop runs for every
    # named Capability first, then the GOVERNED_BY loop runs for every
    # Capability that exists (CHANGES.md Appendix A note: either ordering is
    # fine; this matches the SKILL.md Process text this slice writes).
    _call_cypher(_capability_existence_query(_CAPABILITY_NAME))
    _call_cypher(_capability_existence_query(_CAPABILITY_B_NAME))
    a_governed = _call_cypher(_capability_governed_by_query(_CAPABILITY_NAME))
    b_governed = _call_cypher(_capability_governed_by_query(_CAPABILITY_B_NAME))

    assert _cypher_body(a_governed)["rows"] == []
    assert _cypher_body(b_governed)["rows"] == [[_DRAFT_POLICY_B_ID, "draft", None, None]]
    assert len(handle.cypher_queries) == 4
    # Disagreement (D-6): report and ask the user how to proceed -- no
    # `create-policy-draft`/`get-policy` call for either Capability.
    assert handle.write_queries == []


# --- (7) issue #185: a SUPERSEDED_BY successor of the governing Policy ----

_FORK_POLICY_ID = "pol_data_protection_policy_ffffff"


def _fork_rows_result(rows: list[list[str | None]]) -> _FakeQueryResult:
    return _governed_by_result(cast("list[object]", rows))


def test_scenario_7_owned_draft_fork_is_resumed_and_never_re_forked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-007: governing Policy approved + owned draft fork => resume, no second fork."""
    configure()
    tree = _PolicyTreeFixture(id=_FORK_POLICY_ID, title="Data Protection Policy v2", status="draft")
    handle = _HybridGraph(
        policy_tree=tree,
        cypher_results=[
            _existence_result([[_CAPABILITY_NAME, _CAPABILITY_ID]]),
            _fork_rows_result([[_APPROVED_POLICY_ID, "approved", _FORK_POLICY_ID, "draft"]]),
        ],
    )
    _install_graph(monkeypatch, handle)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    _call_cypher(_capability_existence_query(_CAPABILITY_NAME))
    rows = cast(
        "list[list[str]]",
        _cypher_body(_call_cypher(_capability_governed_by_query(_CAPABILITY_NAME)))["rows"],
    )
    assert rows == [[_APPROVED_POLICY_ID, "approved", _FORK_POLICY_ID, "draft"]]

    with _verified_actor(sub=_ACTOR_SUBJECT):
        get_result = _call_get_policy(rows[0][2])

    body = json.loads(_text(get_result))
    assert body["policy_id"] == _FORK_POLICY_ID
    assert body["status"] == "draft"
    # Resume = read only: no create-policy-draft, no write of any kind.
    assert handle.write_queries == []


def test_scenario_7b_unowned_draft_fork_is_access_denied_and_not_re_forked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    tree = _PolicyTreeFixture(
        id=_FORK_POLICY_ID,
        title="Data Protection Policy v2",
        status="draft",
        owner_subject=_OTHER_SUBJECT,
    )
    handle = _HybridGraph(
        policy_tree=tree,
        cypher_results=[
            _fork_rows_result([[_APPROVED_POLICY_ID, "approved", _FORK_POLICY_ID, "draft"]]),
        ],
    )
    _install_graph(monkeypatch, handle)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    _call_cypher(_capability_governed_by_query(_CAPABILITY_NAME))
    with _verified_actor(sub=_ACTOR_SUBJECT):
        get_result = _call_get_policy(_FORK_POLICY_ID)

    # Skill state `superseded_by_unowned_draft`: stop, no create-policy-draft.
    assert _text(get_result) == "error: you do not have access to this Policy"
    assert handle.write_queries == []


def test_scenario_7c_proposed_fork_row_carries_status_for_fork_awaiting_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph(
        cypher_results=[
            _fork_rows_result([[_APPROVED_POLICY_ID, "approved", _FORK_POLICY_ID, "proposed"]]),
        ]
    )
    _install_graph(monkeypatch, handle)

    rows = _cypher_body(_call_cypher(_capability_governed_by_query(_CAPABILITY_NAME)))["rows"]

    # Skill state `fork_awaiting_approval`: decided from the row alone -- the
    # skill neither calls get-policy nor create-policy-draft.
    assert rows == [[_APPROVED_POLICY_ID, "approved", _FORK_POLICY_ID, "proposed"]]
    assert handle.write_queries == []


def test_scenario_7d_multiple_successor_rows_are_returned_and_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph(
        cypher_results=[
            _fork_rows_result(
                [
                    [_APPROVED_POLICY_ID, "approved", "pol_fork_one", "proposed"],
                    [_APPROVED_POLICY_ID, "approved", _FORK_POLICY_ID, "draft"],
                ]
            ),
        ]
    )
    _install_graph(monkeypatch, handle)

    rows = cast(
        "list[list[str]]",
        _cypher_body(_call_cypher(_capability_governed_by_query(_CAPABILITY_NAME)))["rows"],
    )

    assert [row[2] for row in rows] == ["pol_fork_one", _FORK_POLICY_ID]
    assert handle.write_queries == []
