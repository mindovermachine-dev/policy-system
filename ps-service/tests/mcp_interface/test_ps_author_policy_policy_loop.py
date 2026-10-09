"""Tests for `ps-author-policy`'s Policy field-by-field authoring loop (issue #137, Slice 2).

Two independent proofs, per PLAN.md Section 5 "Slice 2":

1. `test_scaffold_then_field_loop_persists_every_field_in_documented_order`
   -- a scripted `create-policy-draft` -> `update-policy-draft` x6 sequence
   against a module-local `_HybridGraph` fake, proving the tool-call
   SEQUENCE `ps-skills/ps-plugin/skills/ps-author-policy/SKILL.md`'s
   new "Policy authoring loop" sub-flow documents: the scaffold (Title,
   derived and never asked for, D-3; then `scope_in`/`scope_out` persisted
   together as the "1-2 fields" of AC-BI-002) followed by one
   `update-policy-draft` call per remaining P-002..P-006 criterion, in
   `policy-rubric.md`'s listed order, each field landing immediately
   (AC-BI-005) -- and that `title`/`status` never appear in any `fields`
   payload sent to `update-policy-draft` (D-3's immutability guard,
   checked at the call-construction level, not just documentation).
2. `test_overall_score_formula_matches_hand_arithmetic_for_passing_and_failing_vectors`
   -- a pure-function check of `scoring-model.md` Section 4's aggregation
   formula against `policy-rubric.md`'s real 7 criterion weights and
   `pass_threshold: 80`, decoupled from any tool call: feeds a fixed
   (criterion, weight, score) vector through the formula and asserts the
   computed `overall_score` and the `>= pass_threshold` comparison match
   hand arithmetic, for both a passing and a failing vector
   (AC-BI-006/AC-BI-012).

P-007 Lifecycle Honesty is scored against the Policy's own `status`
property (`policy-template.md`'s "Ownership and Status" section), which
`update-policy-draft` can NEVER patch (`status` is not in
`service._POLICY_PATCHABLE_FIELDS` -- confirmed by reading
`service.py:112-122` directly) and which this skill never changes (it only
ever authors content, never calls a status-transition tool). A freshly
scaffolded Policy stays honestly `"draft"` for the whole of this loop, and
`scoring-model.md` Section 2 is explicit that "being honestly in
draft/planned is not [a Fail]" -- so P-007 is never itself the target of a
Socratic question or an `update-policy-draft` call in this loop; only
P-001 (`scope_in`/`scope_out`, scaffolded then refined) through P-006
(`capability_grouping_rationale`) are persisted via `fields`. This is why
test 1 below exercises exactly 6 `update-policy-draft` calls (the scaffold
plus 5 loop fields) covering the 7 real patchable content properties, not
7 tool calls for "P-001..P-007" read literally -- PLAN.md Section 5 Slice
2 step 3's own instruction text ("for each of P-002..P-007") cannot be
followed literally given `status`'s immutability; this file follows
Section 0.5 D-5's own 7-*property* content-read query instead (which
likewise excludes `status`), the more specific and mechanically-checkable
source. Flagged as a deviation in IMPL_SLICE_2.md, not silently resolved.

Fake graph: CHANGES.md Appendix A's `_HybridGraph` (F-1's resolution),
as corrected by `test_ps_author_policy_branch_detection.py` (Slice 1) --
trimmed here to only the dispatch branches this slice's tests actually
exercise (seed-check, `backfill_governance_status`'s no-op, `read_policy_
tree`'s `"s.id, s.title, s.status, c.id"` branch -- needed because
`update-policy-draft` runs the same `_read_transition_target` preamble as
the four #134 transition functions, per `service.py:1377`'s own
docstring -- `find_existing_policy`'s `"RETURN p.id, p.title"` branch, and
the plain write fallback). Slice 1's `read_policy_tree_for_fork` branch and
freehand-`cypher`-queue branch are intentionally omitted: this slice's
scenarios are all fresh-branch, content-CRUD-only, and never call the
`cypher` tool. Module-local, no cross-file import -- matches this
directory's established convention (every test file's fakes are its own).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

_TITLE = "Data Protection Policy"

_SCOPE_IN = (
    "In-scope: all data processing activities performed by systems governed by "
    "the Data Protection capability, including storage, transmission, and "
    "third-party sharing of personal data."
)
_SCOPE_OUT = (
    "Out-of-scope: anonymized data with no re-identification risk, and internal "
    "test datasets containing only synthetic records."
)
_NORMATIVE_COMMITMENTS = (
    "All personal data must be encrypted at rest and in transit. Data retention "
    "periods shall not exceed the documented limit for each data category."
)
_REVIEW_CADENCE = (
    "Reviewed annually, and immediately upon any material change to the "
    "applicable data protection regulation."
)
_EXCEPTION_PATHWAY = (
    "Exceptions require written risk acceptance from the Data Protection "
    "Officer, logged in the exception register with an expiry date."
)
_MEASURABLE_OUTCOMES = "100% of production data stores pass the quarterly encryption-at-rest audit."
_CAPABILITY_GROUPING_RATIONALE = (
    "Both governed Capabilities share the same accountable owner (the Data "
    "Protection Officer), the same annual review cadence, and the same "
    "encryption-based control model; grouping reflects shared governance, not "
    "a single regulation article."
)

# The Process order this file proves: scaffold (P-001, both properties in one
# call) then P-002..P-006 in `policy-rubric.md`'s own listed order.
_LOOP_FIELDS: tuple[tuple[str, str], ...] = (
    ("normative_commitments", _NORMATIVE_COMMITMENTS),
    ("review_cadence", _REVIEW_CADENCE),
    ("exception_pathway", _EXCEPTION_PATHWAY),
    ("measurable_outcomes", _MEASURABLE_OUTCOMES),
    ("capability_grouping_rationale", _CAPABILITY_GROUPING_RATIONALE),
)

_DISALLOWED_FIELD_KEYS = frozenset({"title", "status"})


# --- fakes (CHANGES.md Appendix A, trimmed per this file's own docstring) --


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

    Zero Standards/Controls throughout -- this slice's scenarios never need
    tree children, only the Policy's own id/status/owner (so
    `update-policy-draft`'s `_read_transition_target` preamble and
    `_authorize_draft_edit`'s owner check both succeed).
    """

    id: str
    title: str
    status: str
    version: str = "1"
    owner_subject: str = _ACTOR_SUBJECT
    owner_issuer: str = _ACTOR_ISSUER


class _HybridGraph:
    """CHANGES.md Appendix A's fake, trimmed to this slice's own dispatch needs.

    `policy_tree` is deliberately settable after construction: `create-
    policy-draft` computes its own new `policy_id` (title-slug + hash) that
    this test does not predict in advance, so the fixture is populated from
    the real tool's own return value, right before the first `update-
    policy-draft` call that needs it.
    """

    def __init__(self, *, existing: tuple[str, str] | None = None) -> None:
        self._existing = existing
        self.policy_tree: _PolicyTreeFixture | None = None
        self.write_queries: list[str] = []

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del params, timeout
        if q == _SEED_CHECK_QUERY:
            return _FakeQueryResult(header=[[0, "c"]], result_set=[[1]])
        if "coalesce(p.version" in q or "IS NULL" in q:
            # `graph_writer.backfill_governance_status`'s three idempotent,
            # already-backfilled-safe `SET` statements -- issued ahead of
            # `read_policy_tree` by `_read_transition_target`. Structural
            # no-op, exactly as `test_ps_author_policy_branch_detection.py`
            # (Slice 1) and `test_policy_lifecycle_tools.py` already treat it.
            return _FakeQueryResult()
        if "s.id, s.title, s.status, c.id" in q:
            # `graph_writer.read_policy_tree` -- `update-policy-draft`'s own
            # `_read_transition_target` preamble (service.py:1377-1382).
            # Checked BEFORE the shorter `find_existing_policy` substring
            # below (its own `RETURN` clause contains that substring) --
            # same "more specific match wins" ordering Slice 1 discovered.
            if self.policy_tree is None:
                return _FakeQueryResult(result_set=[])
            p = self.policy_tree
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
            # `graph_writer.find_existing_policy` -- `create-policy-draft`'s
            # own new-id uniqueness check.
            rows: list[object] = [[self._existing[0], self._existing[1]]] if self._existing else []
            return _FakeQueryResult(result_set=rows)
        self.write_queries.append(q)
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

    Unused by this slice's own assertions, but `create-policy-draft` always
    calls it (service.py:428-440) and `update-policy-draft` always
    constructs one for its access-role store (mcp_server.py:2180) -- so it
    must exist and not raise.
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


# --- tool-call helpers -------------------------------------------------


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _call_create_policy_draft(*, title: str) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("create-policy-draft", {"title": title}))
    assert isinstance(result, CallToolResult)
    return result


def _call_update_policy_draft(policy_id: str, fields: dict[str, object]) -> CallToolResult:
    # D-3 immutability guard, checked at the call-construction level -- no
    # test scenario below is even ALLOWED to build a `fields` payload naming
    # either immutable key, regardless of what assertions follow.
    assert _DISALLOWED_FIELD_KEYS.isdisjoint(fields), (
        f"D-3 immutability guard violated: {sorted(_DISALLOWED_FIELD_KEYS & fields.keys())}"
    )
    result = asyncio.run(
        mcp_server.server.call_tool(
            "update-policy-draft", {"policy_id": policy_id, "fields": fields}
        )
    )
    assert isinstance(result, CallToolResult)
    return result


# --- (1) scripted scaffold + field-loop sequence -------------------------


def test_scaffold_then_field_loop_persists_every_field_in_documented_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _HybridGraph()
    _install_graph(monkeypatch, handle)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    persisted_order: list[str] = []

    with _verified_actor(sub=_ACTOR_SUBJECT):
        # Step 1 (D-3): title derived from the Capability name, never asked
        # for -- `create-policy-draft` has no content-field parameter at all.
        create_result = _call_create_policy_draft(title=_TITLE)
        assert create_result.is_error is False
        created = json.loads(_text(create_result))
        policy_id = created["policy_id"]
        assert created["title"] == _TITLE
        assert created["status"] == "draft"

        handle.policy_tree = _PolicyTreeFixture(
            id=policy_id,
            title=_TITLE,
            status="draft",
            owner_subject=_ACTOR_SUBJECT,
            owner_issuer=_ACTOR_ISSUER,
        )

        # Step 2 (AC-BI-002): the scaffold's "1-2 fields" -- P-001's two
        # properties persisted together, displayed before any question.
        scaffold_result = _call_update_policy_draft(
            policy_id, {"scope_in": _SCOPE_IN, "scope_out": _SCOPE_OUT}
        )
        assert scaffold_result.is_error is False
        scaffold_body = json.loads(_text(scaffold_result))
        assert scaffold_body["policy_id"] == policy_id
        assert sorted(scaffold_body["updated_fields"]) == ["scope_in", "scope_out"]
        persisted_order.extend(sorted(scaffold_body["updated_fields"]))

        # Step 3 (AC-BI-005): P-002..P-006, one Socratic field per call, in
        # policy-rubric.md's listed order, each landing immediately.
        for key, value in _LOOP_FIELDS:
            result = _call_update_policy_draft(policy_id, {key: value})
            assert result.is_error is False
            body = json.loads(_text(result))
            assert body["policy_id"] == policy_id
            assert body["updated_fields"] == [key]
            persisted_order.append(key)

    assert persisted_order == [
        "scope_in",
        "scope_out",
        "normative_commitments",
        "review_cadence",
        "exception_pathway",
        "measurable_outcomes",
        "capability_grouping_rationale",
    ]

    # Exactly 7 writes: the create-policy-draft MERGE, plus 6 update-policy-
    # draft SET calls (the combined scaffold call + 5 individual loop
    # fields). Every read-only find_existing_policy/backfill/read_policy_tree
    # call is routed to its own literal-dispatch branch above, never here.
    assert len(handle.write_queries) == 7
    assert "MERGE (p:Policy {id: $policy_id}) SET p += $properties" in handle.write_queries[0]
    for query in handle.write_queries[1:]:
        assert "SET p += $set_properties" in query


# --- (2) pure-function scoring-formula check ------------------------------

# policy-rubric.md's real 7 criteria, in listed order, with their real
# weights (verified against the file directly: 0.15+0.15+0.10+0.10+0.15+
# 0.20+0.15 == 1.00). `scoring-model.md` §5: pass_threshold: 80.
_POLICY_RUBRIC_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("P-001", 0.15),  # Scope Clarity
    ("P-002", 0.15),  # Normative Language
    ("P-003", 0.10),  # Review Cadence
    ("P-004", 0.10),  # Exception Pathway
    ("P-005", 0.15),  # Measurable Intent
    ("P-006", 0.20),  # Capability Grouping Coherence
    ("P-007", 0.15),  # Lifecycle Honesty
)
_PASS_THRESHOLD = 80

# No existing production function computes this formula anywhere in
# ps_service (grepped for "overall_score"/"pass_threshold" -- zero hits
# outside `company_merge/dedup.py`'s unrelated `best_overall_score`). Per
# the task's own scope: a test-local pure-function helper verifying
# documented arithmetic, not promoted to a shipped module.


def _overall_score(scores: dict[str, int]) -> float:
    """`scoring-model.md` §4: `100 * Σ(weight_i * score_i) / 2` (every `max_score_i == 2`)."""
    return (
        100
        * sum(weight * scores[criterion_id] for criterion_id, weight in _POLICY_RUBRIC_WEIGHTS)
        / 2
    )


def test_overall_score_formula_matches_hand_arithmetic_for_passing_and_failing_vectors() -> None:
    assert sum(weight for _id, weight in _POLICY_RUBRIC_WEIGHTS) == pytest.approx(1.0)

    # Passing vector: two Partial criteria (P-003, P-004), rest Pass --
    # by hand: 0.15*2+0.15*2+0.10*1+0.10*1+0.15*2+0.20*2+0.15*2
    #        = 0.30+0.30+0.10+0.10+0.30+0.40+0.30 = 1.80 -> 100*1.80/2 = 90.0
    passing_scores = {
        "P-001": 2,
        "P-002": 2,
        "P-003": 1,
        "P-004": 1,
        "P-005": 2,
        "P-006": 2,
        "P-007": 2,
    }
    # Failing vector: P-006 (the highest-weight criterion) is a Fail, the
    # rest are Partial except the honestly-draft P-007 -- by hand:
    # 0.15*1+0.15*1+0.10*1+0.10*1+0.15*1+0.20*0+0.15*2
    #   = 0.15+0.15+0.10+0.10+0.15+0+0.30 = 0.95 -> 100*0.95/2 = 47.5
    failing_scores = {
        "P-001": 1,
        "P-002": 1,
        "P-003": 1,
        "P-004": 1,
        "P-005": 1,
        "P-006": 0,
        "P-007": 2,
    }

    passing_overall = _overall_score(passing_scores)
    failing_overall = _overall_score(failing_scores)

    assert passing_overall == pytest.approx(90.0)
    assert failing_overall == pytest.approx(47.5)
    assert passing_overall >= _PASS_THRESHOLD
    assert failing_overall < _PASS_THRESHOLD
