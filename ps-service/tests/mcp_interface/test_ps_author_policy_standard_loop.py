"""Tests for `ps-author-policy`'s Standard field-by-field authoring loop (issue #137, Slice 3).

Four proofs, per PLAN.md Section 5 "Slice 3":

1. `test_add_standard_then_field_loop_persists_every_field_in_documented_order`
   -- a scripted `create-policy-draft` -> `add-standard-to-draft` ->
   `update-standard-draft` x6 sequence against a module-local `_HybridGraph`
   (adapted from `test_ps_author_policy_policy_loop.py`'s own shape, Slice
   2), proving the tool-call SEQUENCE `ps-skills/ps-plugin/skills/
   ps-author-policy/SKILL.md`'s new "Standard authoring loop" sub-flow
   documents: `add-standard-to-draft(policy_id, title=<derived>)` with no
   `fields` (mirrors Policy's own title-only creation, D-3-style), then one
   `update-standard-draft` call per S-001..S-006 criterion in
   `standard-rubric.md`'s listed order (S-002 Role Clarity's two properties,
   `implementer_role`/`reviewer_role`, persisted together in one call, the
   same one-criterion/compound-field pattern Slice 2 used for Policy's own
   `scope_in`/`scope_out` scaffold pair) -- and that `title`/`status` never
   appear in any `fields` payload sent to either tool (D-3's immutability
   guard, checked at the call-construction level, same as Slice 2).
2. `test_add_another_standard_under_same_policy_succeeds` -- after one
   Standard's field loop completes, a second `add-standard-to-draft` call
   under the SAME `policy_id` is a valid, accepted sequence against the real
   tool/fake (AC-BI-007's "add another Standard" outcome) -- proving the
   sequence is executable, not which outcome an LLM would pick.
3. `test_finish_after_one_completed_standard_needs_no_further_call` -- the
   sequence started sequence in (1)/(2) is already complete and well-formed
   once one Standard's field loop finishes: no further tool call is needed,
   and (via the same pure-formula helper as test 4) an all-Pass vector for
   that Standard's six criteria genuinely clears `pass_threshold` -- proving
   "finish" (AC-BI-007's third outcome) is a genuinely valid terminal state.
4. `test_overall_score_formula_matches_hand_arithmetic_for_passing_and_failing_vectors`
   -- pure-function check of `scoring-model.md` Section 4's formula against
   `standard-rubric.md`'s real 6 criterion weights (verified directly
   against the file: 0.20+0.15+0.15+0.20+0.10+0.20 == 1.00,
   `pass_threshold: 80`), same shape as Slice 2's test 2.

Two real-code findings, verified directly against `service.py`/
`mcp_server.py` (not trusted from PLAN.md's prose), each the opposite of
what Slice 2 found for Policy -- flagged in IMPL_SLICE_3.md, not silently
resolved:

- **S-006 Lifecycle Honesty's own property, `implementation_status`, IS
  patchable** (`service._STANDARD_PATCHABLE_FIELDS`, service.py:134-145,
  includes it -- unlike Policy's `status`, which `_POLICY_PATCHABLE_FIELDS`
  excludes). So, unlike Policy's P-007 (skipped entirely, no field to
  persist), Standard's S-006 IS asked about and persisted via
  `update-standard-draft`, same as every other S-00x criterion -- this
  file's field loop covers all 6 criteria / 7 properties, not 5 of 6.
- **`update-standard-draft`'s own MCP response never echoes back
  `updated_fields`** (`mcp_server.py:2372-2377` returns exactly
  `{"standard_id", "policy_id", "title", "status"}` -- unlike
  `update-policy-draft`'s response, which does carry `updated_fields`,
  `mcp_server.py:2192-2195`). Slice 2's test could assert persisted-field
  identity straight off each call's own JSON response; this file cannot --
  it instead asserts the persisted VALUES via `_HybridGraph`'s own recorded
  `write_params` (the params dict each `query()` call received), captured
  at the fake-graph boundary, the only place this information is still
  observable.

Fake graph: adapted from `test_ps_author_policy_policy_loop.py`'s own
`_HybridGraph` (itself CHANGES.md Appendix A's fake as corrected by Slice 1),
extended with `find_standard_with_parent`'s own dispatch branch (needed
because `update-standard-draft`'s preamble, `_read_standard_with_parent_
backfilled`, reads it TWICE -- once before, once after the idempotent
`backfill_governance_status` no-op -- per `service.py:1522-1560`, not
`_read_transition_target`/`read_policy_tree` as `update-policy-draft` uses;
`add-standard-to-draft`'s OWN preamble, `_read_transition_target` at
service.py:1498, does still read the Policy tree via `read_policy_tree`, so
that branch is kept too). Every write (the `create-policy-draft` MERGE, the
`add-standard-to-draft` MERGE, and every `update-standard-draft` SET) is
routed to the same generic write bucket (this slice never needs per-write-
type branching to route correctly -- the read-shaped branches above are
checked first, same "more specific match wins" ordering discipline Slice 1
established), but now with the query's own `params` dict also captured
(`write_params`, parallel to `write_queries`) -- needed per the second
finding above. Module-local, no cross-file import -- matches this
directory's established convention.
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
_STANDARD_TITLE_B = "Key Rotation Standard"

_PROCEDURE = (
    "1. Query each production data store's encryption-at-rest flag via the cloud "
    "provider API, weekly. 2. For any store returned as not encrypted, file a "
    "remediation ticket due within 5 business days. 3. Re-check on ticket closure."
)
_IMPLEMENTER_ROLE = "Data Protection Engineer"
_REVIEWER_ROLE = "Data Protection Officer"
_APPLICABILITY_BOUNDARY = (
    "Applies to all production data stores and message queues handling personal "
    "data; does not apply to ephemeral CI test fixtures or anonymized analytics "
    "exports."
)
_VERIFICATION_NOTES = (
    "A Control can test conformance directly: query each in-scope store's "
    "encryption-at-rest flag via the cloud provider API and assert 'enabled' for "
    "100% of stores in scope, pass/fail, no interpretation required."
)
_CHANGE_RATIONALE = (
    "Newly introduced to operationalize the parent Policy's normative_commitments "
    "encryption requirement with a concrete, auditable procedure."
)

# The Process order this file proves: `standard-rubric.md`'s own S-001..S-006
# listed order. S-002 Role Clarity's two properties persisted together in one
# call (one criterion, one Socratic question, one `update-standard-draft`
# call -- the same compound-field pattern Slice 2 used for Policy's own
# `scope_in`/`scope_out`). S-006 IS in this loop (see module docstring finding).
_LOOP_CALLS: tuple[dict[str, object], ...] = (
    {"procedure": _PROCEDURE},
    {"implementer_role": _IMPLEMENTER_ROLE, "reviewer_role": _REVIEWER_ROLE},
    {"applicability_boundary": _APPLICABILITY_BOUNDARY},
    {"verification_notes": _VERIFICATION_NOTES},
    {"change_rationale": _CHANGE_RATIONALE},
    {"implementation_status": "draft"},
)

_DISALLOWED_FIELD_KEYS = frozenset({"title", "status"})


# --- fakes (adapted from test_ps_author_policy_policy_loop.py, Slice 2) ----


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
    the same way `update-policy-draft` does -- unlike `update-standard-draft`,
    which reads the Standard-with-parent shape instead (see
    `_StandardParentFixture` below).
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

    Standard has no ownership field of its own (TASK.md's Implementation-
    decisions paragraph) -- ownership is derived transitively from the
    parent Policy, read fresh by `update-standard-draft`'s own preamble,
    `_read_standard_with_parent_backfilled` (service.py:1522-1560), which
    calls this read TWICE (once before, once after the idempotent
    `backfill_governance_status` no-op) -- this fixture answers both calls
    identically, same as the real backfill-then-reread pattern would once
    backfilled.
    """

    policy_id: str
    policy_owner_subject: str = _ACTOR_SUBJECT
    policy_owner_issuer: str = _ACTOR_ISSUER
    policy_status: str = "draft"
    standard_status: str = "draft"
    standard_title: str = ""


class _HybridGraph:
    """Adapted from `test_ps_author_policy_policy_loop.py`'s own `_HybridGraph` (Slice 2).

    Extended with `find_standard_with_parent`'s own dispatch branch (`RETURN
    p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title`) --
    checked BEFORE the generic write bucket below, since its own query text
    also contains that bucket's `SUPPORTED_BY]->(s:Standard {id:
    $standard_id})` matching substring (the same "more specific match wins"
    ordering `test_policy_lifecycle_tools.py`'s own `_StatefulFakeGraph`
    documents for this exact pair of queries). Every write this slice's
    scenarios issue (`create-policy-draft`'s MERGE, `add-standard-to-draft`'s
    MERGE, `update-standard-draft`'s SET) is routed to one generic bucket --
    this slice never needs to reject or vary a write by type, only observe
    it -- but now with `params` captured alongside each query's text
    (`write_params`), since `update-standard-draft`'s own MCP response never
    echoes back which fields it persisted (module docstring's second
    finding), unlike `update-policy-draft`'s.
    """

    def __init__(self, *, existing: tuple[str, str] | None = None) -> None:
        self._existing = existing
        self.policy_tree: _PolicyTreeFixture | None = None
        self.standards: dict[str, _StandardParentFixture] = {}
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
            # both `read_policy_tree` (via `_read_transition_target`, for
            # add-standard-to-draft) and `find_standard_with_parent` (via
            # `_read_standard_with_parent_backfilled`, for
            # update-standard-draft). Structural no-op, same as Slice 1/2.
            return _FakeQueryResult()
        if "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title" in q:
            # `graph_writer.find_standard_with_parent` -- checked BEFORE the
            # generic write bucket below (its own `RETURN` clause contains
            # that bucket's `SUPPORTED_BY]->(s:Standard {id: $standard_id})`
            # matching substring).
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
            # own `_read_transition_target` preamble (service.py:1498).
            # Checked BEFORE `find_existing_policy`'s shorter substring
            # below (its own `RETURN` clause contains it) -- same ordering
            # discipline Slice 1/2 already established.
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
    always constructs one for its access-role store (`PsycopgAuditStore(
    config)`, e.g. mcp_server.py:2181/2272/2361) -- so it must exist and
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


def _call_update_standard_draft(standard_id: str, fields: dict[str, object]) -> CallToolResult:
    # D-3-style immutability guard, checked at the call-construction level --
    # no test scenario below is even ALLOWED to build a `fields` payload
    # naming either immutable key, regardless of what assertions follow.
    assert _DISALLOWED_FIELD_KEYS.isdisjoint(fields), (
        f"D-3 immutability guard violated: {sorted(_DISALLOWED_FIELD_KEYS & fields.keys())}"
    )
    result = asyncio.run(
        mcp_server.server.call_tool(
            "update-standard-draft", {"standard_id": standard_id, "fields": fields}
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


def _complete_one_standard(handle: _HybridGraph, *, policy_id: str, title: str) -> str:
    add_result = _call_add_standard_to_draft(policy_id=policy_id, title=title)
    assert add_result.is_error is False
    added = json.loads(_text(add_result))
    assert added["policy_id"] == policy_id
    assert added["title"] == title
    assert added["status"] == "draft"
    standard_id = cast("str", added["standard_id"])

    handle.standards[standard_id] = _StandardParentFixture(
        policy_id=policy_id, standard_title=title
    )

    for fields in _LOOP_CALLS:
        result = _call_update_standard_draft(standard_id, fields)
        assert result.is_error is False
        body = json.loads(_text(result))
        assert body["standard_id"] == standard_id
        assert body["policy_id"] == policy_id
        assert body["status"] == "draft"

    return standard_id


# --- (1) scripted add-standard + field-loop sequence ----------------------


def test_add_standard_then_field_loop_persists_every_field_in_documented_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        standard_id = _complete_one_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)

    del standard_id  # only needed to drive the sequence above

    # write_queries[0] = create-policy-draft's MERGE; [1] = add-standard-to-
    # draft's MERGE; [2:] = the 6 update-standard-draft SETs, one per
    # `_LOOP_CALLS` entry, in order.
    assert len(handle.write_queries) == 2 + len(_LOOP_CALLS)
    assert "MERGE (p:Policy {id: $policy_id}) SET p += $properties" in handle.write_queries[0]

    assert "SUPPORTED_BY]->(s:Standard {id: $standard_id})" in handle.write_queries[1]
    assert "SET s += $properties" in handle.write_queries[1]
    # `graph_writer.add_standard_to_policy`'s own server-forced defaults
    # (graph_writer.py:799-801): `implementation_status` defaults to
    # `"draft"` (fields=None was sent -- add-standard-to-draft's own call
    # never includes content fields, D-3-style, mirrors Policy's
    # title-only creation), `title`/`status` are always forced, never
    # caller-controlled (the tool doesn't even expose them as settable).
    assert handle.write_params[1]["properties"] == {
        "implementation_status": "draft",
        "title": _STANDARD_TITLE,
        "status": "draft",
    }

    for query in handle.write_queries[2:]:
        assert "MATCH (s:Standard {id: $standard_id}) SET s += $set_properties" in query

    # Persisted VALUES, verified via the fake's recorded params -- unlike
    # Slice 2's Policy-loop test, `update-standard-draft`'s own JSON response
    # never echoes back `updated_fields` (module docstring's second finding),
    # so params captured at the fake-graph boundary are the only place this
    # is still observable, in the exact order each call sent them.
    persisted = [wp["set_properties"] for wp in handle.write_params[2:]]
    assert persisted == [dict(fields) for fields in _LOOP_CALLS]


# --- (2) "add another Standard" -------------------------------------------


def test_add_another_standard_under_same_policy_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        first_id = _complete_one_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)

        # AC-BI-007's "add another Standard" outcome: a second
        # `add-standard-to-draft` call under the SAME `policy_id`, after the
        # first Standard's own rubric passes -- always attempted, never
        # blocked by the tool/fake for "already has a Standard".
        second_add = _call_add_standard_to_draft(policy_id=policy_id, title=_STANDARD_TITLE_B)

    assert second_add.is_error is False
    added = json.loads(_text(second_add))
    assert added["policy_id"] == policy_id
    assert added["title"] == _STANDARD_TITLE_B
    assert added["status"] == "draft"
    assert added["standard_id"] != first_id


# --- (3) "finish" -----------------------------------------------------


def test_finish_after_one_completed_standard_needs_no_further_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph()

    with _verified_actor(sub=_ACTOR_SUBJECT):
        policy_id = _new_policy(monkeypatch, handle)
        _complete_one_standard(handle, policy_id=policy_id, title=_STANDARD_TITLE)

    # "Finish" (AC-BI-007's third outcome) is a user-facing choice, never a
    # tool call -- the sequence built above is already complete and
    # well-formed exactly as it stands: no further `add-standard-to-draft`/
    # `update-standard-draft` call happened or is needed.
    assert len(handle.write_queries) == 2 + len(_LOOP_CALLS)

    # And it is a genuinely valid terminal state, not merely an unfinished
    # one the skill gave up on early: an all-Pass vector for this Standard's
    # six criteria (matching the Pass-level content `_complete_one_standard`
    # actually persisted -- explicit procedure, both roles stated, a scoped
    # boundary, directly-testable verification notes, a stated change
    # rationale, and an honestly-`"draft"` `implementation_status` for
    # genuinely freshly authored content) clears `pass_threshold`.
    scores = {"S-001": 2, "S-002": 2, "S-003": 2, "S-004": 2, "S-005": 2, "S-006": 2}
    assert _overall_score(scores) >= _PASS_THRESHOLD


# --- (4) pure-function scoring-formula check ------------------------------

# standard-rubric.md's real 6 criteria, in listed order, with their real
# weights (verified against the file directly: 0.20+0.15+0.15+0.20+0.10+
# 0.20 == 1.00). `scoring-model.md` §5: pass_threshold: 80 (same threshold
# constant every rubric file uses).
_STANDARD_RUBRIC_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("S-001", 0.20),  # Procedure Specificity
    ("S-002", 0.15),  # Role Clarity
    ("S-003", 0.15),  # Boundary Clarity
    ("S-004", 0.20),  # Verification Readiness
    ("S-005", 0.10),  # Change Traceability
    ("S-006", 0.20),  # Lifecycle Honesty
)
_PASS_THRESHOLD = 80

# No existing production function computes this formula anywhere in
# ps_service (Slice 2's own grep already confirmed zero hits outside
# `company_merge/dedup.py`'s unrelated `best_overall_score`). Per the task's
# own scope: a test-local pure-function helper verifying documented
# arithmetic, not promoted to a shipped module -- same as Slice 2.


def _overall_score(scores: dict[str, int]) -> float:
    """`scoring-model.md` §4: `100 * Σ(weight_i * score_i) / 2` (every `max_score_i == 2`)."""
    return (
        100
        * sum(weight * scores[criterion_id] for criterion_id, weight in _STANDARD_RUBRIC_WEIGHTS)
        / 2
    )


def test_overall_score_formula_matches_hand_arithmetic_for_passing_and_failing_vectors() -> None:
    assert sum(weight for _id, weight in _STANDARD_RUBRIC_WEIGHTS) == pytest.approx(1.0)

    # Passing vector: two Partial criteria (S-002, S-003), rest Pass -- by hand:
    # 0.20*2 + 0.15*1 + 0.15*1 + 0.20*2 + 0.10*2 + 0.20*2
    #   = 0.40 + 0.15 + 0.15 + 0.40 + 0.20 + 0.40 = 1.70 -> 100*1.70/2 = 85.0
    passing_scores = {
        "S-001": 2,
        "S-002": 1,
        "S-003": 1,
        "S-004": 2,
        "S-005": 2,
        "S-006": 2,
    }
    # Failing vector: S-004 (one of the two highest-weight criteria) is a
    # Fail, the rest are Partial -- by hand:
    # 0.20*1 + 0.15*1 + 0.15*1 + 0.20*0 + 0.10*1 + 0.20*1
    #   = 0.20 + 0.15 + 0.15 + 0 + 0.10 + 0.20 = 0.80 -> 100*0.80/2 = 40.0
    failing_scores = {
        "S-001": 1,
        "S-002": 1,
        "S-003": 1,
        "S-004": 0,
        "S-005": 1,
        "S-006": 1,
    }

    passing_overall = _overall_score(passing_scores)
    failing_overall = _overall_score(failing_scores)

    assert passing_overall == pytest.approx(85.0)
    assert failing_overall == pytest.approx(40.0)
    assert passing_overall >= _PASS_THRESHOLD
    assert failing_overall < _PASS_THRESHOLD
