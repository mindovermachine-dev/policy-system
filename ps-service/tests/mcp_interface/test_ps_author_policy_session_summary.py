"""Tests for `ps-author-policy`'s session-end summary (issue #137, Slice 6).

One end-to-end scripted sequence tying Slices 2-5 together, per PLAN.md
Section 5 "Slice 6": fresh Policy (scaffold + P-002..P-006) -> 1 passing
Standard (S-001..S-006, `procedure` carrying a `Sources:` suffix per Slice
5's own contract) -> 1 passing Control (C-001..C-006) -> a final
`get-policy` call -> a final `cypher` content re-read -- against a single
`_HybridGraph` instance, combining `test_ps_author_policy_control_loop.py`'s
own write-side dispatch branches (Slice 4: `find_control_with_parent`,
`find_standard_with_parent`, `read_policy_tree`, `find_existing_policy`)
with `test_ps_author_policy_branch_detection.py`'s own freehand-`cypher`
FIFO-queue branch (Slice 1) -- the two halves of this skill's own tool
surface (content-CRUD writes plus read-only `cypher`) exercised together in
one session for the first time. `read_policy_tree`'s own dispatch is made
STATEFUL here (unlike Slices 2-4's always-empty-children fixture): the test
helper appends the created Standard/Control to the shared `_PolicyFixture`
right after each create call succeeds, so the final `get-policy` call's own
tree read reflects everything created earlier in the same session -- this
is the one new capability this slice's fake needs that no earlier slice's
fake required, because no earlier slice's test ever called `get-policy`
*after* creating content.

Three assertions, per PLAN.md Section 5 Slice 6's own test description:

(a) the final `get-policy` call's returned tree includes every created id
    (Policy, Standard, Control) in the right parent/child relationship;
(b) the content persisted across the whole sequence matches what was
    written -- checked two ways: directly, via the fake's own recorded
    `write_params` (the only place `update-standard-draft`/
    `update-control-draft`'s persisted VALUES are observable at all,
    Slices 3/4's own finding -- their MCP response never echoes
    `updated_fields`), and via a final `cypher` read scripted to return
    exactly those same persisted values, proving the Process's own
    "re-read via cypher" instruction is a real, executable step against
    this skill's tool surface, not merely documentation;
(c) a mechanical, grep-style assertion over this test's own call log (every
    tool name actually passed to `mcp_server.server.call_tool` across the
    whole sequence) confirms `propose-policy`/`approve-policy`/
    `reject-policy`/`revert-policy-to-draft` were never invoked -- direct
    enforcement of AC-BI-010's "never calling it itself," not merely a
    documentation check.

Fake graph: module-local, no cross-file import -- matches this directory's
established convention (every test file's fakes are its own).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
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

# --- Policy content: scaffold (P-001) + P-002..P-006 -----------------------

_SCOPE_IN = "In-scope: all production data stores handling personal data."
_SCOPE_OUT = "Out-of-scope: anonymized data with no re-identification risk."
_POLICY_LOOP_FIELDS: tuple[tuple[str, str], ...] = (
    ("normative_commitments", "All personal data must be encrypted at rest and in transit."),
    ("review_cadence", "Reviewed annually."),
    ("exception_pathway", "Exceptions require written risk acceptance, logged with an expiry."),
    ("measurable_outcomes", "100% of production data stores pass the quarterly audit."),
    ("capability_grouping_rationale", "Single Capability; grouping question does not arise."),
)

# --- Standard content: S-001..S-006 (S-001's `procedure` carries the
# session's one `Sources:` suffix, per Slice 5's own citation contract) -----

_PROCEDURE_WITH_SOURCE = (
    "Query each production data store's encryption-at-rest flag weekly; file a "
    f"remediation ticket for any store found not encrypted.\n\nSources: {_SOURCE_URL}"
)
_STANDARD_LOOP_CALLS: tuple[dict[str, object], ...] = (
    {"procedure": _PROCEDURE_WITH_SOURCE},
    {"implementer_role": "Data Protection Engineer", "reviewer_role": "Data Protection Officer"},
    {"applicability_boundary": "Applies to all production data stores."},
    {"verification_notes": "A Control can test conformance directly, pass/fail."},
    {"change_rationale": "Newly introduced to operationalize the parent Policy."},
    {"implementation_status": "draft"},
)

# --- Control content: C-001..C-006 -----------------------------------------

_CONTROL_LOOP_CALLS: tuple[dict[str, object], ...] = (
    {"pass_fail_criteria": "Pass if 100% of in-scope stores report 'enabled'."},
    {"execution_method": "An automated script queries the cloud provider's encryption API."},
    {"evidence_plan": "Each run's API response is written to the compliance evidence bucket."},
    {"executor_role": "Data Protection Engineer", "reviewer_role": "Data Protection Officer"},
    {"risk_alignment_rationale": "Directly verifies the encryption-at-rest requirement."},
    {"implementation_status": "planned"},
)

_FORBIDDEN_TOOLS = frozenset(
    {"propose-policy", "approve-policy", "reject-policy", "revert-policy-to-draft"}
)


# --- fakes (merges test_ps_author_policy_control_loop.py's write-side
# dispatch, Slice 4, with test_ps_author_policy_branch_detection.py's own
# freehand-cypher FIFO-queue branch, Slice 1) ------------------------------


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
class _ControlFixture:
    """One Control, as `read_policy_tree`'s own row shape needs it."""

    id: str
    title: str
    status: str = "draft"
    control_type: str = "automated"


@dataclass
class _StandardFixture:
    """One Standard plus its own Controls, as `read_policy_tree`'s own row shape needs it."""

    id: str
    title: str
    status: str = "draft"
    controls: list[_ControlFixture] = field(default_factory=list)


@dataclass
class _PolicyFixture:
    """The whole tree `read_policy_tree` walks -- STATEFUL, unlike Slices 2-4's
    always-empty-children fixture: the test helper appends each created
    Standard/Control here right after its own create call succeeds, so a
    LATER `read_policy_tree`-shaped read (this slice's own final
    `get-policy` call) reflects everything created earlier in the same
    session.
    """

    id: str
    title: str
    status: str = "draft"
    version: str = "1"
    owner_subject: str = _ACTOR_SUBJECT
    owner_issuer: str = _ACTOR_ISSUER
    standards: list[_StandardFixture] = field(default_factory=list)


@dataclass
class _StandardParentFixture:
    """Stand-in for `graph_writer.find_standard_with_parent`'s own `StandardWithParent`.

    Read by `update-standard-draft`'s and `add-control-to-draft`'s own
    preambles -- carried over unchanged from Slices 3/4.
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
    """Slice 4's own write-side `_HybridGraph`, extended with a STATEFUL
    `read_policy_tree` dispatch (`_PolicyFixture` above) and Slice 1's own
    freehand-`cypher` FIFO-queue branch, checked after every literal-dispatch
    branch (same "more specific match wins" ordering discipline every
    earlier slice already established) -- this slice's own final `cypher`
    content-re-read call shares no literal substring with any dispatch
    branch below, so it always falls through to the queue.
    """

    def __init__(self, *, cypher_results: list[_FakeQueryResult | Exception] | None = None) -> None:
        self.policy: _PolicyFixture | None = None
        self.standards: dict[str, _StandardParentFixture] = {}
        self.controls: dict[str, _ControlParentFixture] = {}
        self._cypher_results = list(cypher_results or [])
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object]] = []
        self.cypher_queries: list[str] = []

    def _policy_tree_rows(self) -> list[object]:
        p = self.policy
        if p is None:
            return []
        base = [p.id, p.title, p.status, p.version, p.owner_subject, p.owner_issuer]
        if not p.standards:
            row: list[object] = [*base, None, None, None, None, None, None, None]
            return [row]
        rows: list[object] = []
        for s in p.standards:
            if not s.controls:
                rows.append([*base, s.id, s.title, s.status, None, None, None, None])
            else:
                for c in s.controls:
                    rows.append(
                        [*base, s.id, s.title, s.status, c.id, c.title, c.status, c.control_type]
                    )
        return rows

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        p = params or {}
        if q == _SEED_CHECK_QUERY:
            return _FakeQueryResult(header=[[0, "c"]], result_set=[[1]])
        if "coalesce(p.version" in q or "IS NULL" in q:
            # `graph_writer.backfill_governance_status`'s idempotent no-op --
            # issued ahead of every `find_*_with_parent`/`read_policy_tree`
            # read. Structural no-op, same as every earlier slice.
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
            # own preamble AND this slice's own final `get-policy` call.
            # Checked BEFORE `find_existing_policy`'s shorter substring below
            # (its own `RETURN` clause contains it) -- same ordering
            # discipline every earlier slice already established. STATEFUL:
            # reflects `self.policy.standards`/`.controls` as they stand at
            # the moment of THIS call.
            rows = self._policy_tree_rows()
            return _FakeQueryResult(result_set=rows)
        if "RETURN p.id, p.title" in q:
            # `graph_writer.find_existing_policy` -- `create-policy-draft`'s
            # own new-id uniqueness check.
            return _FakeQueryResult(result_set=[])
        if (
            q.strip().upper().startswith(("MATCH", "RETURN"))
            and " SET " not in q
            and "MERGE (" not in q
            and self._cypher_results
        ):
            # The skill's own freehand `cypher`-tool call (session-end
            # content re-read) -- dequeue the next scripted outcome. The
            # " SET "/"MERGE (" exclusion matters here in a way it didn't in
            # Slice 1's own fake: this slice is the first to combine the
            # freehand-cypher queue with the write-side content-CRUD tools
            # in one graph, and `update-*-draft`'s own SET statements (e.g.
            # `"MATCH (p:Policy {id: $policy_id}) SET p += $set_properties"`)
            # also start with `MATCH` -- without this guard they would be
            # wrongly swallowed into the queue (found by a red run: the
            # scaffold's own SET call vanished from `write_queries` the
            # moment `cypher_results` held an unconsumed entry).
            self.cypher_queries.append(q)
            outcome = self._cypher_results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
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

    def record(self, *args: object, **kwargs: object) -> str:
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


# --- tool-call helpers, all routed through one call log (requirement (c)) --


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _call_tool(name: str, args: dict[str, object], call_log: list[str]) -> CallToolResult:
    """Every tool call this test makes goes through here -- `call_log` is
    the mechanical record requirement (c) greps for the four forbidden
    status-transition tool names, none of which this test ever names.
    """
    call_log.append(name)
    result = asyncio.run(mcp_server.server.call_tool(name, args))
    assert isinstance(result, CallToolResult)
    return result


# --- the single end-to-end scripted sequence --------------------------------


def test_full_session_creates_full_tree_never_calls_a_status_transition_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()

    # Scripted for the final `cypher` content re-read (requirement (b)):
    # one row per node, matching exactly what the field loops below persist.
    policy_content_row = [
        _SCOPE_IN,
        _SCOPE_OUT,
        _POLICY_LOOP_FIELDS[0][1],  # normative_commitments
        _POLICY_LOOP_FIELDS[1][1],  # review_cadence
        _POLICY_LOOP_FIELDS[2][1],  # exception_pathway
        _POLICY_LOOP_FIELDS[3][1],  # measurable_outcomes
        _POLICY_LOOP_FIELDS[4][1],  # capability_grouping_rationale
    ]
    cypher_results: list[_FakeQueryResult | Exception] = [
        _FakeQueryResult(
            header=[[0, c] for c in ("scope_in", "scope_out", "normative_commitments")],
            result_set=[policy_content_row],
        )
    ]
    handle = _HybridGraph(cypher_results=cypher_results)
    _install_graph(monkeypatch, handle)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    call_log: list[str] = []

    with _verified_actor(sub=_ACTOR_SUBJECT):
        # --- Policy: scaffold, then P-002..P-006 ----------------------------
        create_result = _call_tool("create-policy-draft", {"title": _POLICY_TITLE}, call_log)
        assert create_result.is_error is False
        created = json.loads(_text(create_result))
        policy_id = cast("str", created["policy_id"])
        handle.policy = _PolicyFixture(id=policy_id, title=_POLICY_TITLE)

        scaffold_result = _call_tool(
            "update-policy-draft",
            {"policy_id": policy_id, "fields": {"scope_in": _SCOPE_IN, "scope_out": _SCOPE_OUT}},
            call_log,
        )
        assert scaffold_result.is_error is False

        for key, value in _POLICY_LOOP_FIELDS:
            result = _call_tool(
                "update-policy-draft", {"policy_id": policy_id, "fields": {key: value}}, call_log
            )
            assert result.is_error is False

        # --- Standard: add, then S-001..S-006 -------------------------------
        add_standard_result = _call_tool(
            "add-standard-to-draft",
            {"policy_id": policy_id, "title": _STANDARD_TITLE},
            call_log,
        )
        assert add_standard_result.is_error is False
        added_standard = json.loads(_text(add_standard_result))
        standard_id = cast("str", added_standard["standard_id"])

        standard_fixture = _StandardFixture(id=standard_id, title=_STANDARD_TITLE)
        handle.policy.standards.append(standard_fixture)
        handle.standards[standard_id] = _StandardParentFixture(
            policy_id=policy_id, standard_title=_STANDARD_TITLE
        )

        for fields in _STANDARD_LOOP_CALLS:
            result = _call_tool(
                "update-standard-draft", {"standard_id": standard_id, "fields": fields}, call_log
            )
            assert result.is_error is False

        # --- Control: add, then C-001..C-006 --------------------------------
        add_control_result = _call_tool(
            "add-control-to-draft",
            {"standard_id": standard_id, "title": _CONTROL_TITLE, "control_type": _CONTROL_TYPE},
            call_log,
        )
        assert add_control_result.is_error is False
        added_control = json.loads(_text(add_control_result))
        control_id = cast("str", added_control["control_id"])

        control_fixture = _ControlFixture(
            id=control_id, title=_CONTROL_TITLE, control_type=_CONTROL_TYPE
        )
        standard_fixture.controls.append(control_fixture)
        handle.controls[control_id] = _ControlParentFixture(
            standard_id=standard_id, policy_id=policy_id, control_title=_CONTROL_TITLE
        )

        for fields in _CONTROL_LOOP_CALLS:
            result = _call_tool(
                "update-control-draft", {"control_id": control_id, "fields": fields}, call_log
            )
            assert result.is_error is False

        # --- Session end: final get-policy + cypher content re-read --------
        get_result = _call_tool("get-policy", {"policy_id": policy_id}, call_log)
        assert get_result.is_error is False

        content_query = (
            f"MATCH (p:Policy {{id: '{policy_id}'}}) "
            "RETURN p.scope_in AS scope_in, p.scope_out AS scope_out, "
            "p.normative_commitments AS normative_commitments"
        )
        cypher_result = _call_tool("cypher", {"query": content_query}, call_log)
        assert cypher_result.is_error is False

    # --- (a) the final get-policy tree includes every created id in the
    # right parent/child relationship -------------------------------------
    get_body = json.loads(_text(get_result))
    assert get_body["policy_id"] == policy_id
    assert get_body["title"] == _POLICY_TITLE
    assert len(get_body["standards"]) == 1
    standard_body = get_body["standards"][0]
    assert standard_body["standard_id"] == standard_id
    assert standard_body["title"] == _STANDARD_TITLE
    assert len(standard_body["controls"]) == 1
    control_body = standard_body["controls"][0]
    assert control_body["control_id"] == control_id
    assert control_body["title"] == _CONTROL_TITLE
    assert control_body["control_type"] == _CONTROL_TYPE

    # --- (b) persisted content matches what was written, verified two ways:
    # directly via the fake's own recorded `write_params` (the only place
    # update-standard-draft's/update-control-draft's persisted VALUES are
    # observable, Slices 3/4's own finding)...
    scaffold_params = handle.write_params[1]["set_properties"]
    assert scaffold_params == {"scope_in": _SCOPE_IN, "scope_out": _SCOPE_OUT}
    policy_loop_params = [wp["set_properties"] for wp in handle.write_params[2:7]]
    assert policy_loop_params == [{key: value} for key, value in _POLICY_LOOP_FIELDS]

    standard_loop_params = [wp["set_properties"] for wp in handle.write_params[8:14]]
    assert standard_loop_params == [dict(fields) for fields in _STANDARD_LOOP_CALLS]
    first_standard_params = cast("dict[str, object]", standard_loop_params[0])
    persisted_procedure = cast("str", first_standard_params["procedure"])
    assert persisted_procedure.endswith(f"Sources: {_SOURCE_URL}")

    control_loop_params = [wp["set_properties"] for wp in handle.write_params[15:21]]
    assert control_loop_params == [dict(fields) for fields in _CONTROL_LOOP_CALLS]

    # ...and via the final `cypher` call's own scripted-but-consistent
    # response, proving the Process's "re-read via cypher" step is a real,
    # executable call against this skill's tool surface.
    cypher_body = json.loads(_text(cypher_result))
    assert cypher_body["rows"] == [policy_content_row]
    assert handle.cypher_queries == [content_query]

    # --- (c) mechanical call-log check: none of the four status-transition
    # tools were ever invoked anywhere in this session (AC-BI-010) ----------
    assert _FORBIDDEN_TOOLS.isdisjoint(call_log)
    assert call_log.count("cypher") == 1
    assert call_log.count("get-policy") == 1
