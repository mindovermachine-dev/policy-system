"""Tests for `ps-author-policy`'s Control field-by-field authoring loop (issue #137, Slice 4).

Five proofs, per PLAN.md Section 5 "Slice 4":

1. `test_add_control_then_field_loop_persists_every_field_in_documented_order`
   -- a scripted `create-policy-draft` -> `add-standard-to-draft` ->
   `add-control-to-draft` -> `update-control-draft` x6 sequence against a
   module-local `_HybridGraph` (adapted from `test_ps_author_policy_
   standard_loop.py`'s own shape, Slice 3, one level deeper), proving the
   tool-call SEQUENCE `ps-skills/ps-plugin/skills/ps-author-policy/
   SKILL.md`'s new "Control authoring loop" sub-flow documents:
   `add-control-to-draft(standard_id, title=<derived>,
   control_type=<answer>)` with no content `fields` at creation time
   (D-3-style, mirrors Policy/Standard's own title-only creation), then one
   `update-control-draft` call per C-001..C-006 criterion in
   `control-rubric.md`'s listed order (C-004 Ownership Clarity's two
   properties, `executor_role`/`reviewer_role`, persisted together in one
   call -- the same one-criterion/compound-field pattern Slices 2/3 used for
   `scope_in`/`scope_out` and `implementer_role`/`reviewer_role`) -- and
   that `title`/`status` never appear in any `fields` payload sent to either
   tool (D-3's immutability guard, checked at the call-construction level,
   same as Slices 2/3). The write-params assertion on `add-control-to-
   draft`'s own MERGE additionally pins the one deliberate divergence from
   Standard: `implementation_status` server-defaults to `"planned"`, never
   `"draft"` -- this file's own comments and assertions describe it as
   `"planned"` throughout, never as `"draft"` (the governance `status`
   field, returned separately and always literally `"draft"` while the
   Control is in draft state, is a different property entirely).
2. `test_control_type_patchable_via_update_but_rejected_as_creation_field`
   -- proves the two ends of CHANGES.md finding #8 against the real tools:
   `add-control-to-draft`'s own `fields` rejects a `"type"` key outright
   (the top-level `control_type` param is the only creation-time path), but
   `update-control-draft`'s `fields` accepts `"type"` and persists it --
   the one property `_CONTROL_PATCHABLE_FIELDS` includes that
   `_STANDARD_PATCHABLE_FIELDS`'s Standard-loop analogue has no equivalent
   of (verified directly against `service.py:148-177`).
3. `test_add_another_control_under_same_standard_succeeds` -- after one
   Control's field loop completes, a second `add-control-to-draft` call
   under the SAME `standard_id` is a valid, accepted sequence against the
   real tool/fake (AC-BI-008's "add another Control" outcome).
4. `test_decline_to_add_a_control_is_a_valid_zero_call_outcome` -- AC-BI-008's
   own extra trigger clause, "or the user declines to add one": a Standard
   with zero `add-control-to-draft` calls against it is asserted
   structurally to be a valid, complete terminal state, not a missing step
   -- the fake's own write log for that Standard shows no Control-creation
   write at all, matching the schema's own cardinality (`scoring-model.md`
   Section 1: `IMPLEMENTED_BY` requires exactly one inbound edge *per
   Control*, never a minimum count *per Standard* -- verified directly
   against that file, not assumed by analogy to Policy/Standard).
5. `test_overall_score_formula_matches_hand_arithmetic_for_passing_and_failing_vectors`
   -- pure-function check of `scoring-model.md` Section 4's formula against
   `control-rubric.md`'s real 6 criterion weights (verified directly
   against the file: 0.20+0.15+0.15+0.15+0.20+0.15 == 1.00,
   `pass_threshold: 80`), same shape as Slices 2/3's own formula tests.

Two real-code findings, verified directly against `service.py`/
`graph_writer.py` (not trusted from PLAN.md's prose), both already flagged
by PLAN.md Section 5 Slice 4 itself but confirmed here rather than assumed:

- **`add_control_to_standard`'s own server-forced defaults**
  (`graph_writer.py:979-985`): `properties.setdefault("implementation_status",
  "planned")` -- NOT `"draft"` like `add_standard_to_policy`'s own default
  for Standard (`graph_writer.py`'s own comment: "the one deliberate
  divergence ... matching `ps-domain-concepts.md`'s own 'earliest state in
  status workflow' convention for Control"). `title`/`type`/`status` are
  always forced after `extra_properties`, never caller-controlled through
  `fields`.
- **`_CONTROL_PATCHABLE_FIELDS` (service.py:161-177) includes `"type"`**,
  unlike `_STANDARD_PATCHABLE_FIELDS`; but `add-control-to-draft`'s own MCP
  tool narrows this by one key at its own call site
  (`_ADD_CONTROL_TO_DRAFT_PATCHABLE_FIELDS = _CONTROL_PATCHABLE_FIELDS -
  {"type"}`, `mcp_server.py:2394`) -- so `"type"` is rejected in
  `add-control-to-draft`'s own `fields` (only `control_type` sets it at
  creation) but accepted in `update-control-draft`'s `fields` (the only
  post-creation path to change it).

Fake graph: adapted from `test_ps_author_policy_standard_loop.py`'s own
`_HybridGraph` (Slice 3), extended one level deeper with
`find_control_with_parent`'s own dispatch branch (needed because
`update-control-draft`'s preamble, `_read_control_with_parent_backfilled`,
reads it TWICE -- once before, once after the idempotent
`backfill_governance_status` no-op -- per `service.py:1744-1781`, the
genuinely TWO-hop case in this issue, `Policy -[:SUPPORTED_BY]-> Standard
-[:IMPLEMENTED_BY]-> Control`, distinct from `find_standard_with_parent`'s
own one-hop shape). `add-control-to-draft`'s OWN preamble,
`_read_standard_with_parent_backfilled` (service.py:1715), still reads the
one-hop Standard-with-parent shape Slice 3's fake already dispatches (the
caller supplies a `standard_id`, exactly like `update-standard-draft`
does -- PLAN.md Section 1.4's own correction, not a two-hop read). Every
write this slice's scenarios issue (`create-policy-draft`'s MERGE,
`add-standard-to-draft`'s MERGE, `add-control-to-draft`'s MERGE,
`update-control-draft`'s SET) is routed to one generic write bucket, with
`params` captured alongside each query's text (`write_params`), same as
Slice 3 -- needed because `update-control-draft`'s own MCP response never
echoes back `updated_fields` either (mirrors `update-standard-draft`'s own
finding, Slice 3's module docstring; `mcp_server.py:2543-2544` returns
exactly `{"control_id", "standard_id", "policy_id", "title", "status"}`).
Module-local, no cross-file import -- matches this directory's established
convention.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
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

_ACTOR_SUBJECT = "policy-author"
_ACTOR_ISSUER = "https://issuer.example.com/"

_POLICY_TITLE = "Data Protection Policy"
_STANDARD_TITLE = "Encryption at Rest Standard"
_CONTROL_TITLE = "Encryption at Rest Automated Check"
_CONTROL_TITLE_B = "Encryption at Rest Manual Spot-Check"

_CONTROL_TYPE = "automated"

_PASS_FAIL_CRITERIA = (
    "Query each in-scope production data store's encryption-at-rest flag via the "
    "cloud provider API; pass if 100% report 'enabled', fail if any report "
    "'disabled' or the API call returns no value -- no interpretation required."
)
_EXECUTION_METHOD = (
    "An automated script queries the cloud provider's encryption-status API for "
    "every in-scope data store; intended to run on a weekly schedule once "
    "execution_frequency is set, matching the parent Standard's own procedure "
    "cadence."
)
_EVIDENCE_PLAN = (
    "Each run's raw API response is written as a timestamped JSON artifact to the "
    "compliance evidence bucket, plus a summary row appended to the "
    "encryption-audit log; evidence_ref will point to that bucket path once "
    "execution begins."
)
_EXECUTOR_ROLE = "Data Protection Engineer"
_REVIEWER_ROLE = "Data Protection Officer"
_RISK_ALIGNMENT_RATIONALE = (
    "Directly verifies the encryption-at-rest requirement that mitigates the Data "
    "Exposure at Rest RiskPath's own exposure: an unencrypted store found by this "
    "Control is exactly the condition that RiskPath models as unacceptable."
)
# C-006 Lifecycle Honesty's honest answer for freshly authored, unexecuted
# content is "planned" -- Control's OWN workflow value, matching the server's
# own default (never "draft", which is the separate governance `status`
# property this skill never touches).
_IMPLEMENTATION_STATUS = "planned"

# The Process order this file proves: `control-rubric.md`'s own C-001..C-006
# listed order. C-004 Ownership Clarity's two properties persisted together in
# one call (one criterion, one Socratic question, one `update-control-draft`
# call -- the same compound-field pattern Slices 2/3 used for
# `scope_in`/`scope_out` and `implementer_role`/`reviewer_role`).
_LOOP_CALLS: tuple[dict[str, object], ...] = (
    {"pass_fail_criteria": _PASS_FAIL_CRITERIA},
    {"execution_method": _EXECUTION_METHOD},
    {"evidence_plan": _EVIDENCE_PLAN},
    {"executor_role": _EXECUTOR_ROLE, "reviewer_role": _REVIEWER_ROLE},
    {"risk_alignment_rationale": _RISK_ALIGNMENT_RATIONALE},
    {"implementation_status": _IMPLEMENTATION_STATUS},
)

_DISALLOWED_FIELD_KEYS = frozenset({"title", "status"})


# --- fakes (adapted from test_ps_author_policy_standard_loop.py, Slice 3) --


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
    this way -- carried over unchanged from Slice 3.
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

    Needed because `add-control-to-draft`'s own preamble,
    `_read_standard_with_parent_backfilled`, reads the SAME one-hop shape
    `update-standard-draft` uses (PLAN.md Section 1.4's own correction: the
    caller supplies a `standard_id`, so this is not the two-hop case) --
    carried over unchanged from Slice 3.
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

    Control has no ownership field of its own, nor does its parent Standard
    -- ownership is derived by walking all the way to the root Policy via
    the genuinely TWO-hop `Policy -[:SUPPORTED_BY]-> Standard
    -[:IMPLEMENTED_BY]-> Control` traversal (`graph_writer.py:1021-1073`),
    read TWICE by `update-control-draft`'s own preamble,
    `_read_control_with_parent_backfilled` (service.py:1744-1781) -- once
    before, once after the idempotent `backfill_governance_status` no-op.
    This fixture answers both calls identically.
    """

    standard_id: str
    policy_id: str
    policy_owner_subject: str = _ACTOR_SUBJECT
    policy_owner_issuer: str = _ACTOR_ISSUER
    policy_status: str = "draft"
    control_status: str = "draft"
    control_title: str = ""


class _HybridGraph:
    """Adapted from `test_ps_author_policy_standard_loop.py`'s own `_HybridGraph` (Slice 3).

    Extended one level deeper with `find_control_with_parent`'s own dispatch
    branch (`RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status,
    c.status, c.title`) -- its own literal text shares no overlapping
    substring with any other dispatch branch here (verified by inspection:
    `find_standard_with_parent`'s own `RETURN p.id, p.owner_subject, ...,
    s.status, s.title` starts with `p.id`, not `s.id`; `read_policy_tree`'s
    own `s.id, s.title, s.status, c.id` differs at the third column;
    `find_existing_policy`'s own `RETURN p.id, p.title` is shorter but
    textually distinct), so no "more specific match wins" ordering is needed
    against them -- it is simply checked before the generic write bucket,
    same as every other read-shaped branch here. Every write this slice's
    scenarios issue (`create-policy-draft`'s MERGE, `add-standard-to-draft`'s
    MERGE, `add-control-to-draft`'s MERGE, `update-control-draft`'s SET) is
    routed to one generic bucket, with `params` captured alongside each
    query's text (`write_params`, parallel to Slice 3's own convention).
    """

    def __init__(self, *, existing: tuple[str, str] | None = None) -> None:
        self._existing = existing
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
            # `graph_writer.backfill_governance_status`'s three idempotent,
            # already-backfilled-safe `SET` statements -- issued ahead of
            # `read_policy_tree`, `find_standard_with_parent`, AND
            # `find_control_with_parent`. Structural no-op, same as
            # Slices 1-3.
            return _FakeQueryResult()
        if "RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status, c.status, c.title" in q:
            # `graph_writer.find_control_with_parent` -- `update-control-
            # draft`'s own `_read_control_with_parent_backfilled` preamble
            # (the genuinely two-hop case in this issue).
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
            # `graph_writer.find_standard_with_parent` -- `add-control-to-
            # draft`'s own `_read_standard_with_parent_backfilled` preamble
            # (one-hop, same shape `update-standard-draft` uses).
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
            # own `_read_transition_target` preamble. Checked BEFORE
            # `find_existing_policy`'s shorter substring below (its own
            # `RETURN` clause contains it) -- same ordering discipline
            # Slices 1-3 already established.
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
            rows: list[object] = [[self._existing[0], self._existing[1]]] if self._existing else []
            return _FakeQueryResult(result_set=rows)
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
    *, standard_id: str, title: str, control_type: str, fields: dict[str, object] | None = None
) -> CallToolResult:
    args: dict[str, object] = {
        "standard_id": standard_id,
        "title": title,
        "control_type": control_type,
    }
    if fields is not None:
        args["fields"] = fields
    result = asyncio.run(mcp_server.server.call_tool("add-control-to-draft", args))
    assert isinstance(result, CallToolResult)
    return result


def _call_update_control_draft(control_id: str, fields: dict[str, object]) -> CallToolResult:
    # D-3-style immutability guard, checked at the call-construction level --
    # no test scenario below is even ALLOWED to build a `fields` payload
    # naming either immutable key, regardless of what assertions follow.
    assert _DISALLOWED_FIELD_KEYS.isdisjoint(fields), (
        f"D-3 immutability guard violated: {sorted(_DISALLOWED_FIELD_KEYS & fields.keys())}"
    )
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


def _complete_one_control(
    handle: _HybridGraph, *, standard_id: str, policy_id: str, title: str, control_type: str
) -> str:
    add_result = _call_add_control_to_draft(
        standard_id=standard_id, title=title, control_type=control_type
    )
    assert add_result.is_error is False
    added = json.loads(_text(add_result))
    assert added["standard_id"] == standard_id
    assert added["policy_id"] == policy_id
    assert added["title"] == title
    # Governance `status`, always "draft" while in draft state -- distinct
    # from the content `implementation_status` property, which defaults to
    # "planned" (asserted separately via `write_params` below, since this
    # response shape never echoes content fields).
    assert added["status"] == "draft"
    control_id = cast("str", added["control_id"])

    handle.controls[control_id] = _ControlParentFixture(
        standard_id=standard_id, policy_id=policy_id, control_title=title
    )

    for fields in _LOOP_CALLS:
        result = _call_update_control_draft(control_id, fields)
        assert result.is_error is False
        body = json.loads(_text(result))
        assert body["control_id"] == control_id
        assert body["standard_id"] == standard_id
        assert body["policy_id"] == policy_id
        assert body["status"] == "draft"

    return control_id


# --- (1) scripted add-control + field-loop sequence ------------------------


def test_add_control_then_field_loop_persists_every_field_in_documented_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        standard_id = _new_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)
        control_id = _complete_one_control(
            handle,
            standard_id=standard_id,
            policy_id=policy_id,
            title=_CONTROL_TITLE,
            control_type=_CONTROL_TYPE,
        )

    del control_id  # only needed to drive the sequence above

    # write_queries[0] = create-policy-draft's MERGE; [1] = add-standard-to-
    # draft's MERGE; [2] = add-control-to-draft's MERGE; [3:] = the 6
    # update-control-draft SETs, one per `_LOOP_CALLS` entry, in order.
    assert len(handle.write_queries) == 3 + len(_LOOP_CALLS)
    assert "MERGE (p:Policy {id: $policy_id}) SET p += $properties" in handle.write_queries[0]
    assert "SUPPORTED_BY]->(s:Standard {id: $standard_id})" in handle.write_queries[1]

    assert "IMPLEMENTED_BY]->(c:Control {id: $control_id})" in handle.write_queries[2]
    assert "SET c += $properties" in handle.write_queries[2]
    # `graph_writer.add_control_to_standard`'s own server-forced defaults
    # (graph_writer.py:979-985): `implementation_status` defaults to
    # `"planned"` -- NOT `"draft"` (Control's own workflow starts one step
    # later than Standard's, ps-domain-concepts.md's "earliest state"
    # convention) -- since `add-control-to-draft`'s own call never includes
    # content fields (D-3-style). `title`/`type`/`status` are always forced,
    # never caller-controlled through `fields`.
    assert handle.write_params[2]["properties"] == {
        "implementation_status": "planned",
        "title": _CONTROL_TITLE,
        "type": _CONTROL_TYPE,
        "status": "draft",
    }

    for query in handle.write_queries[3:]:
        assert "MATCH (c:Control {id: $control_id}) SET c += $set_properties" in query

    # Persisted VALUES, verified via the fake's recorded params -- same as
    # Slice 3's own `update-standard-draft` finding: `update-control-draft`'s
    # JSON response never echoes back `updated_fields`.
    persisted = [wp["set_properties"] for wp in handle.write_params[3:]]
    assert persisted == [dict(fields) for fields in _LOOP_CALLS]
    # The C-006 Lifecycle Honesty answer persisted is "planned", never
    # "draft" -- the last loop call's own set_properties.
    assert persisted[-1] == {"implementation_status": "planned"}


# --- (2) `type` patchable at update time, rejected at creation time --------


def test_control_type_patchable_via_update_but_rejected_as_creation_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        standard_id = _new_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)

        # `add-control-to-draft`'s own `fields` allow-list is
        # `_CONTROL_PATCHABLE_FIELDS - {"type"}` (mcp_server.py:2394) --
        # `"type"` in `fields` at creation time is rejected outright, before
        # any graph read; `control_type` is the only creation-time path.
        rejected = _call_add_control_to_draft(
            standard_id=standard_id,
            title=_CONTROL_TITLE,
            control_type="manual",
            fields={"type": "automated"},
        )
        assert rejected.is_error is False
        assert _text(rejected) == "error: fields.type is not a patchable field"
        # Rejected before any graph write -- only the create-policy-draft/
        # add-standard-to-draft writes from the setup above exist so far.
        assert len(handle.write_queries) == 2

        # The real creation call, `type="manual"` via `control_type` only.
        add_result = _call_add_control_to_draft(
            standard_id=standard_id, title=_CONTROL_TITLE, control_type="manual"
        )
        assert add_result.is_error is False
        added = json.loads(_text(add_result))
        control_id = cast("str", added["control_id"])
        handle.controls[control_id] = _ControlParentFixture(
            standard_id=standard_id, policy_id=policy_id, control_title=_CONTROL_TITLE
        )

        # `update-control-draft`'s own `fields` allow-list is the FULL
        # `_CONTROL_PATCHABLE_FIELDS`, including `"type"` -- the only
        # post-creation path to change it (CHANGES.md finding #8).
        update_result = _call_update_control_draft(control_id, {"type": "automated"})
        assert update_result.is_error is False
        body = json.loads(_text(update_result))
        assert body["control_id"] == control_id

    write_query = handle.write_queries[-1]
    assert "MATCH (c:Control {id: $control_id}) SET c += $set_properties" in write_query
    assert handle.write_params[-1]["set_properties"] == {"type": "automated"}


# --- (3) "add another Control" ----------------------------------------------


def test_add_another_control_under_same_standard_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        standard_id = _new_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)
        first_id = _complete_one_control(
            handle,
            standard_id=standard_id,
            policy_id=policy_id,
            title=_CONTROL_TITLE,
            control_type=_CONTROL_TYPE,
        )

        # AC-BI-008's "add another Control" outcome: a second
        # `add-control-to-draft` call under the SAME `standard_id`, after the
        # first Control's own rubric passes -- always attempted, never
        # blocked by the tool/fake for "already has a Control".
        second_add = _call_add_control_to_draft(
            standard_id=standard_id, title=_CONTROL_TITLE_B, control_type="manual"
        )

    assert second_add.is_error is False
    added = json.loads(_text(second_add))
    assert added["standard_id"] == standard_id
    assert added["title"] == _CONTROL_TITLE_B
    assert added["status"] == "draft"
    assert added["control_id"] != first_id


# --- (4) "decline to add a Control" -----------------------------------------


def test_decline_to_add_a_control_is_a_valid_zero_call_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        standard_id = _new_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)

    del policy_id, standard_id  # only needed to drive the sequence above

    # AC-BI-008's own extra trigger clause: "or the user declines to add
    # one". Zero `add-control-to-draft` calls for this Standard is itself
    # the correct outcome, never a missing step -- this is asserted
    # structurally: exactly the 2 writes from `create-policy-draft`/
    # `add-standard-to-draft`, no Control-creation write of any kind.
    assert len(handle.write_queries) == 2
    assert not any("Control" in q for q in handle.write_queries)

    # And it is a genuinely valid terminal state per the schema's own
    # cardinality (`scoring-model.md` Section 1: `IMPLEMENTED_BY` requires
    # exactly one inbound edge *per Control* that exists, never a minimum
    # count *per Standard* -- a Standard with zero Controls violates no
    # cardinality rule; only an existing Control that lacked the edge
    # would).
    assert handle.controls == {}


# --- (5) pure-function scoring-formula check --------------------------------

# control-rubric.md's real 6 criteria, in listed order, with their real
# weights (verified against the file directly: 0.20+0.15+0.15+0.15+0.20+
# 0.15 == 1.00). `scoring-model.md` Section 5: pass_threshold: 80 (same
# threshold constant every rubric file uses).
_CONTROL_RUBRIC_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("C-001", 0.20),  # Pass/Fail Objectivity
    ("C-002", 0.15),  # Execution Clarity
    ("C-003", 0.15),  # Evidence Path Defined
    ("C-004", 0.15),  # Ownership Clarity
    ("C-005", 0.20),  # Risk Alignment
    ("C-006", 0.15),  # Lifecycle Honesty
)
_PASS_THRESHOLD = 80

# No existing production function computes this formula anywhere in
# ps_service (Slices 2/3's own grep already confirmed zero hits outside
# `company_merge/dedup.py`'s unrelated `best_overall_score`). Per the task's
# own scope: a test-local pure-function helper verifying documented
# arithmetic, not promoted to a shipped module -- same as Slices 2/3.


def _overall_score(scores: dict[str, int]) -> float:
    """`scoring-model.md` Section 4: `100 * Σ(weight_i * score_i) / 2` (`max_score_i == 2`)."""
    return (
        100
        * sum(weight * scores[criterion_id] for criterion_id, weight in _CONTROL_RUBRIC_WEIGHTS)
        / 2
    )


def test_overall_score_formula_matches_hand_arithmetic_for_passing_and_failing_vectors() -> None:
    assert sum(weight for _id, weight in _CONTROL_RUBRIC_WEIGHTS) == pytest.approx(1.0)

    # Passing vector: two Partial criteria (C-002, C-003), rest Pass -- by hand:
    # 0.20*2 + 0.15*1 + 0.15*1 + 0.15*2 + 0.20*2 + 0.15*2
    #   = 0.40 + 0.15 + 0.15 + 0.30 + 0.40 + 0.30 = 1.70 -> 100*1.70/2 = 85.0
    passing_scores = {
        "C-001": 2,
        "C-002": 1,
        "C-003": 1,
        "C-004": 2,
        "C-005": 2,
        "C-006": 2,
    }
    # Failing vector: C-005 (one of the two highest-weight criteria) is a
    # Fail, the rest are Partial -- by hand:
    # 0.20*1 + 0.15*1 + 0.15*1 + 0.15*1 + 0.20*0 + 0.15*1
    #   = 0.20 + 0.15 + 0.15 + 0.15 + 0 + 0.15 = 0.80 -> 100*0.80/2 = 40.0
    failing_scores = {
        "C-001": 1,
        "C-002": 1,
        "C-003": 1,
        "C-004": 1,
        "C-005": 0,
        "C-006": 1,
    }

    passing_overall = _overall_score(passing_scores)
    failing_overall = _overall_score(failing_scores)

    assert passing_overall == pytest.approx(85.0)
    assert failing_overall == pytest.approx(40.0)
    assert passing_overall >= _PASS_THRESHOLD
    assert failing_overall < _PASS_THRESHOLD
