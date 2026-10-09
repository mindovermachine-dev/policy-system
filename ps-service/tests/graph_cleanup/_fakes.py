# pyright: reportPrivateUsage=false
"""Scripted single-tenant graph and in-memory stores for the graph-cleanup merge tests.

The real reader, planner, writer and executor run; only the rows the graph returns (and
the failures it raises) are canned. Queries are dispatched by their text.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from ps_service.graph_cleanup.graph_writer import MERGE_CAPABILITIES_QUERY, RELEASE_GOVERNANCE_QUERY
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.audit.models import AuditEventRow, AuditQueryFilters, AuditQueryPage

SURVIVOR = "cap_survivor"
ABSORBED = "cap_absorbed"


@dataclass
class QueryResult:
    result_set: list[object]


@dataclass
class ScriptedMergeGraph:
    """Case-1 graph: both Capabilities active, a few edges on the absorbed one."""

    nodes: list[list[object]] = field(
        default_factory=lambda: [
            [SURVIVOR, "Survivor", "active", "desc", None, 0.9],
            [ABSORBED, "Absorbed", "active", None, "technical", 0.8],
        ]
    )
    requires: list[list[object]] = field(
        default_factory=lambda: [["obl_1", ABSORBED], ["obl_2", ABSORBED], ["obl_2", SURVIVOR]]
    )
    covers: list[list[object]] = field(default_factory=lambda: [["pa_1", ABSORBED]])
    mitigated: list[list[object]] = field(default_factory=list)
    governors: list[list[object]] = field(default_factory=list)
    # `[policy_id, capability_id]` rows; `None` derives them from `governors` plus an
    # unrelated `cap_other` per policy, so the policy's governed set is never just the pair.
    governed_sets: list[list[object]] | None = None
    tombstone_count: int = 0
    write_rows: list[list[object]] = field(default_factory=lambda: [[SURVIVOR, ABSORBED]])
    write_error: Exception | None = None
    read_error: Exception | None = None
    queries: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    @property
    def write_calls(self) -> list[tuple[str, dict[str, object] | None]]:
        return [call for call in self.queries if call[0] == MERGE_CAPABILITIES_QUERY]

    def query(self, q: str, params: Mapping[str, object] | None = None) -> QueryResult:
        self.queries.append((q, dict(params) if params is not None else None))
        if q == MERGE_CAPABILITIES_QUERY:
            if self.write_error is not None:
                raise self.write_error
            return QueryResult(list(self.write_rows))
        if self.read_error is not None:
            raise self.read_error
        if "RETURN count(a)" in q:
            return QueryResult([[self.tombstone_count]])
        if "RETURN c.id, c.name" in q:
            return QueryResult(list(self.nodes))
        if "p.id IN $policy_ids" in q:
            if self.governed_sets is not None:
                return QueryResult(list(self.governed_sets))
            derived: list[object] = []
            for governor in self.governors:
                derived.extend([[governor[1], governor[0]], [governor[1], "cap_other"]])
            return QueryResult(derived)
        if "GOVERNED_BY" in q:
            return QueryResult(list(self.governors))
        if "[:REQUIRES]" in q:
            return QueryResult(list(self.requires))
        if "[:COVERS]" in q:
            return QueryResult(list(self.covers))
        if "[:MITIGATED_BY]" in q:
            return QueryResult(list(self.mitigated))
        message = f"unscripted query: {q}"
        raise AssertionError(message)


@dataclass
class RecordedAudit:
    actor_subject: str
    actor_issuer: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    details: dict[str, object]


@dataclass
class RecordingAuditStore:
    """In-memory `AuditStore` whose `record_standalone` validates against the registry.

    `events` is a shared, ordered log (graph writes append "graph_write" too when the
    same list is passed to `OrderedGraph`), so a test can assert audit-before-write.
    """

    rows: list[RecordedAudit] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    raise_on_outcome: dict[str, Exception] = field(default_factory=dict)
    history: list[AuditEventRow] = field(default_factory=list)
    query_error: Exception | None = None
    query_calls: int = 0

    def record(self, cur: object, **kwargs: object) -> str:
        del cur, kwargs
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
        from ps_service.audit.models import resolve_details_model

        error = self.raise_on_outcome.get(outcome)
        if error is not None:
            raise error
        model = resolve_details_model(action)
        assert model is not None, f"unregistered audit action {action!r}"
        validated = model.model_validate(dict(details))
        self.events.append(f"audit:{outcome}")
        self.rows.append(
            RecordedAudit(
                actor_subject=actor_subject,
                actor_issuer=actor_issuer,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                details=validated.model_dump(mode="json", exclude_none=True),
            )
        )

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Newest-first page of `history` (seeded newest first), filtered like the real store."""
        from ps_service.audit.models import AuditQueryPage

        if self.query_error is not None:
            raise self.query_error
        self.query_calls += 1
        matching = [
            event
            for event in self.history
            if (filters.resource_id is None or event.resource_id == filters.resource_id)
            and (filters.action is None or event.action == filters.action)
        ]
        start = int(cursor) if cursor is not None else 0
        page = matching[start : start + page_size]
        more = start + page_size < len(matching)
        return AuditQueryPage(
            events=tuple(page), next_cursor=str(start + page_size) if more else None
        )


@dataclass
class OrderedGraph(ScriptedMergeGraph):
    """`ScriptedMergeGraph` that appends "graph_write" to a shared event log on each write."""

    events: list[str] = field(default_factory=list)

    def query(self, q: str, params: Mapping[str, object] | None = None) -> QueryResult:
        if q == MERGE_CAPABILITIES_QUERY:
            self.events.append("graph_write")
        return super().query(q, params)


def approval_row_count(store: FakeApprovalStore) -> int:
    """How many `pending_approvals` rows the in-memory store holds."""
    return len(store._rows_by_id)


def expire_approval(store: FakeApprovalStore, approval_id: str, *, expired_for: timedelta) -> None:
    """Move a stored row's `expires_at` to `expired_for` in the past."""
    row = store._rows_by_id[approval_id]
    store._rows_by_id[approval_id] = replace(row, expires_at=datetime.now(UTC) - expired_for)


def tamper_approval_args(
    store: FakeApprovalStore, approval_id: str, normalized_args: dict[str, object]
) -> None:
    """Overwrite a stored row's `normalized_args` (simulates a presented-for-another-pair row)."""
    row = store._rows_by_id[approval_id]
    store._rows_by_id[approval_id] = replace(row, normalized_args=normalized_args)


@dataclass
class FakeApprovalStore:
    """In-memory `PendingApprovalStore` (structural `Protocol` match; no Postgres).

    Own copy of the shape `passkey_signing._fakes.FakePendingApprovalStore` has: that package
    sorts after this one, so it cannot be imported from here reliably.
    """

    _rows_by_id: dict[str, PendingApprovalRow] = field(default_factory=dict)

    def create_pending_approval(
        self,
        *,
        tool_name: str,
        normalized_args: dict[str, object],
        actor_subject: str,
        actor_issuer: str,
        display_summary: dict[str, object],
    ) -> tuple[PendingApprovalRow, str]:
        code = secrets.token_urlsafe(32)
        created_at = datetime.now(UTC)
        row = PendingApprovalRow(
            id=str(uuid.uuid4()),
            code_hash=hashlib.sha256(code.encode()).digest(),
            tool_name=tool_name,
            normalized_args=normalized_args,
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            nonce=secrets.token_bytes(32),
            display_summary=display_summary,
            status="pending",
            outcome=None,
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=15),
        )
        self._rows_by_id[row.id] = row
        return row, code

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        return self._rows_by_id.get(pending_approval_id)

    def get_by_code_hash(self, code_hash: bytes) -> PendingApprovalRow | None:
        return next((r for r in self._rows_by_id.values() if r.code_hash == code_hash), None)

    def mark_signed(self, pending_approval_id: str) -> bool:
        row = self._rows_by_id.get(pending_approval_id)
        if row is None or row.status != "pending":
            return False
        self._rows_by_id[pending_approval_id] = replace(row, status="signed")
        return True

    def set_outcome(self, pending_approval_id: str, outcome: dict[str, object]) -> None:
        row = self._rows_by_id.get(pending_approval_id)
        if row is not None:
            self._rows_by_id[pending_approval_id] = replace(row, outcome=outcome)


OBL_SURVIVOR = "obl_survivor"
OBL_ABSORBED = "obl_absorbed"
ROLE_ID = "role_1"


@dataclass
class ScriptedObligationGraph:
    """Obligation-merge graph: two Obligations under one Role, a few edges on the absorbed one.

    Queries are dispatched by their text; `write_calls` are the guarded merge statements
    (`MergedObligation` in the text).
    """

    nodes: list[list[object]] = field(
        default_factory=lambda: [
            [OBL_SURVIVOR, "Report incidents", 0.9],
            [OBL_ABSORBED, "Report  incidents.", 0.8],
        ]
    )
    roles: list[list[object]] = field(
        default_factory=lambda: [
            [OBL_SURVIVOR, ROLE_ID, "Manufacturer"],
            [OBL_ABSORBED, ROLE_ID, "Manufacturer"],
        ]
    )
    satisfied: list[list[object]] = field(
        default_factory=lambda: [
            ["req_1", OBL_ABSORBED],
            ["req_2", OBL_ABSORBED],
            ["req_2", OBL_SURVIVOR],
        ]
    )
    requires: list[list[object]] = field(
        default_factory=lambda: [[OBL_ABSORBED, "cap_1"], [OBL_SURVIVOR, "cap_1"]]
    )
    refs: list[list[object]] = field(
        default_factory=lambda: [["req_1", "Art. 6(1)"], ["req_2", "Art. 6(2)"]]
    )
    marker_count: int = 0
    absorbed_count: int = 1
    write_rows: list[list[object]] = field(default_factory=lambda: [[OBL_SURVIVOR, OBL_ABSORBED]])
    write_error: Exception | None = None
    read_error: Exception | None = None
    queries: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    @property
    def write_calls(self) -> list[tuple[str, dict[str, object] | None]]:
        return [call for call in self.queries if "MergedObligation" in call[0]]

    def query(self, q: str, params: Mapping[str, object] | None = None) -> QueryResult:
        self.queries.append((q, dict(params) if params is not None else None))
        if "MergedObligation" in q and "DETACH DELETE" in q:
            self.events.append("graph_write")
            if self.write_error is not None:
                raise self.write_error
            return QueryResult(list(self.write_rows))
        if self.read_error is not None:
            raise self.read_error
        if "RETURN count(m)" in q:
            return QueryResult([[self.marker_count]])
        if "RETURN count(o)" in q:
            return QueryResult([[self.absorbed_count]])
        ids = set(cast("list[str]", (params or {}).get("ids", [])))
        if "RETURN o.id, o.text" in q:
            return QueryResult([r for r in self.nodes if r[0] in ids])
        if "RETURN o.id, r.id" in q:
            return QueryResult([r for r in self.roles if r[0] in ids])
        if "EXPRESSES" in q:
            return QueryResult(list(self.refs))
        if "[:SATISFIED_BY]" in q:
            return QueryResult([r for r in self.satisfied if r[1] in ids])
        if "[:REQUIRES]" in q:
            return QueryResult([r for r in self.requires if r[0] in ids])
        message = f"unscripted query: {q}"
        raise AssertionError(message)


@dataclass
class ScriptedReleaseGraph(ScriptedMergeGraph):
    """Graph for `release-capability-governance`: `SURVIVOR` is governed by a draft policy.

    The release statement returns `release_rows`; the verifier's edge count is `edge_count`.
    Every release write appends "graph_write" to the shared `events` log.
    """

    release_rows: list[list[object]] = field(default_factory=lambda: [[SURVIVOR]])
    release_error: Exception | None = None
    edge_count: int = 1
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.governors:
            self.governors = [[SURVIVOR, "pol_1", "Incident Policy", "draft"]]

    @property
    def release_calls(self) -> list[tuple[str, dict[str, object] | None]]:
        return [call for call in self.queries if call[0] == RELEASE_GOVERNANCE_QUERY]

    def query(self, q: str, params: Mapping[str, object] | None = None) -> QueryResult:
        if q == RELEASE_GOVERNANCE_QUERY:
            self.queries.append((q, dict(params) if params is not None else None))
            self.events.append("graph_write")
            if self.release_error is not None:
                raise self.release_error
            return QueryResult(list(self.release_rows))
        if "RETURN count(r)" in q:
            self.queries.append((q, dict(params) if params is not None else None))
            if self.read_error is not None:
                raise self.read_error
            return QueryResult([[self.edge_count]])
        return super().query(q, params)


@dataclass
class ScriptedUnmergeGraph:
    """Graph for capability `unmerge`: `ABSORBED` is a tombstone of `SURVIVOR`.

    Queries are dispatched by their text. The unmerge statement (`UNMERGE_CAPABILITY_QUERY`)
    returns `write_rows` and appends "graph_write" to the shared `events` log.
    """

    nodes: list[list[object]] = field(
        default_factory=lambda: [
            [SURVIVOR, "Survivor", "active", "desc", None, 0.9],
            [ABSORBED, "Absorbed", "merged", None, "technical", 0.8],
        ]
    )
    redirects: list[list[object]] = field(default_factory=lambda: [[ABSORBED, SURVIVOR]])
    requires: list[list[object]] = field(
        default_factory=lambda: [["obl_1", SURVIVOR], ["obl_2", SURVIVOR]]
    )
    covers: list[list[object]] = field(default_factory=lambda: [["pa_1", SURVIVOR]])
    mitigated: list[list[object]] = field(default_factory=lambda: [["rp_1", SURVIVOR]])
    governors: list[list[object]] = field(default_factory=list)
    existing: dict[str, list[str]] = field(
        default_factory=lambda: {
            "Obligation": ["obl_1", "obl_2"],
            "PracticeArea": ["pa_1"],
            "RiskPath": ["rp_1"],
        }
    )
    policy_rows: list[list[object]] = field(default_factory=lambda: [["pol_1"]])
    unmerged_row: list[object] = field(default_factory=lambda: [1, 0])
    write_rows: list[list[object]] = field(default_factory=lambda: [[ABSORBED]])
    write_error: Exception | None = None
    read_error: Exception | None = None
    queries: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    @property
    def write_calls(self) -> list[tuple[str, dict[str, object] | None]]:
        from ps_service.graph_cleanup.graph_writer import UNMERGE_CAPABILITY_QUERY

        return [call for call in self.queries if call[0] == UNMERGE_CAPABILITY_QUERY]

    def query(self, q: str, params: Mapping[str, object] | None = None) -> QueryResult:
        from ps_service.graph_cleanup.graph_writer import UNMERGE_CAPABILITY_QUERY

        self.queries.append((q, dict(params) if params is not None else None))
        if q == UNMERGE_CAPABILITY_QUERY:
            self.events.append("graph_write")
            if self.write_error is not None:
                raise self.write_error
            return QueryResult(list(self.write_rows))
        if self.read_error is not None:
            raise self.read_error
        ids = cast("list[str]", (params or {}).get("ids", []))
        if "count(m)" in q:
            return QueryResult([list(self.unmerged_row)])
        if "MERGED_INTO" in q:
            return QueryResult([r for r in self.redirects if r[0] in ids])
        if "RETURN c.id, c.name" in q:
            return QueryResult([r for r in self.nodes if r[0] in ids])
        if "RETURN c.id, p.id, p.title, p.status" in q:
            return QueryResult([r for r in self.governors if r[0] in ids])
        for rel, rows in (
            ("[:REQUIRES]", self.requires),
            ("[:COVERS]", self.covers),
            ("[:MITIGATED_BY]", self.mitigated),
        ):
            if rel in q:
                return QueryResult([r for r in rows if r[1] in ids])
        for label, present in self.existing.items():
            if f"(x:{label}) WHERE x.id IN $ids" in q:
                return QueryResult([[x] for x in present if x in ids])
        if "MATCH (p:Policy {id: $policy_id})" in q:
            return QueryResult(list(self.policy_rows))
        message = f"unscripted query: {q}"
        raise AssertionError(message)


def capability_merge_history(
    *,
    approval_id: str = "merge-approval-1",
    absorbed_policy: tuple[str, str] | None = None,
) -> list[AuditEventRow]:
    """A one-row (newest first) audit history holding the REAL planner's snapshot of a merge.

    The merge is the one `ScriptedUnmergeGraph` shows the aftermath of: the absorbed node held
    REQUIRES `obl_1`/`obl_2`, COVERS `pa_1`, MITIGATED_BY `rp_1`; the survivor already had
    REQUIRES `obl_2`.
    """
    from ps_service.audit.models import AuditEventRow
    from ps_service.graph_cleanup.audit_actions import CapabilityMergeDetails
    from ps_service.graph_cleanup.merge_planner import plan_capability_merge
    from ps_service.graph_cleanup.models import (
        CapabilityNodeState,
        EdgeRecord,
        GoverningPolicy,
        MergeState,
    )

    def edge(rel: str, label: str, source: str, target: str) -> EdgeRecord:
        return EdgeRecord(
            rel_type=rel,
            source_label=label,
            source_id=source,
            target_label="Capability",
            target_id=target,
        )

    state = MergeState(
        survivor=CapabilityNodeState(
            id=SURVIVOR, name="Survivor", status="active", properties={"description": "desc"}
        ),
        absorbed=CapabilityNodeState(
            id=ABSORBED, name="Absorbed", status="active", properties={"type": "technical"}
        ),
        edges=(
            edge("REQUIRES", "Obligation", "obl_1", ABSORBED),
            edge("REQUIRES", "Obligation", "obl_2", ABSORBED),
            edge("REQUIRES", "Obligation", "obl_2", SURVIVOR),
            edge("COVERS", "PracticeArea", "pa_1", ABSORBED),
            edge("MITIGATED_BY", "RiskPath", "rp_1", ABSORBED),
        ),
        survivor_policies=(),
        absorbed_policies=(
            (GoverningPolicy(id=absorbed_policy[0], title=absorbed_policy[1], status="approved"),)
            if absorbed_policy
            else ()
        ),
        governed_sets={absorbed_policy[0]: (ABSORBED,)} if absorbed_policy else {},
    )
    plan = plan_capability_merge(state)
    details = CapabilityMergeDetails(
        survivor_id=SURVIVOR,
        absorbed_id=ABSORBED,
        policy_case=plan.preview.policy_case,
        acknowledged=absorbed_policy is not None,
        approval_id=approval_id,
        before=plan.before,
        after=plan.after,
    ).model_dump(mode="json")
    return [
        AuditEventRow(
            id="evt-merge",
            occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
            actor_subject="officer",
            actor_issuer="https://issuer.example.com/",
            action="capability.merge",
            resource_type="capability",
            resource_id=ABSORBED,
            outcome="applied",
            details=details,
        )
    ]


@dataclass
class ScriptedObligationUnmergeGraph:
    """Graph for obligation `unmerge`: `OBL_ABSORBED` is deleted and marked as merged.

    Queries are dispatched by their text. The unmerge statement (`UNMERGE_OBLIGATION_QUERY`)
    returns `write_rows` and appends "graph_write" to the shared `events` log.
    """

    nodes: list[list[object]] = field(
        default_factory=lambda: [[OBL_SURVIVOR, "Report incidents", 0.9]]
    )
    markers: list[list[object]] = field(default_factory=lambda: [[OBL_ABSORBED, OBL_SURVIVOR]])
    roles: list[list[object]] = field(default_factory=lambda: [[ROLE_ID]])
    requirements: list[list[object]] = field(default_factory=lambda: [["req_1"], ["req_2"]])
    capabilities: list[list[object]] = field(default_factory=lambda: [["cap_1", "active"]])
    satisfied: list[list[object]] = field(
        default_factory=lambda: [["req_1", OBL_SURVIVOR], ["req_2", OBL_SURVIVOR]]
    )
    requires: list[list[object]] = field(default_factory=lambda: [[OBL_SURVIVOR, "cap_1"]])
    marker_count: int = 0
    absorbed_count: int = 1
    write_rows: list[list[object]] = field(default_factory=lambda: [[OBL_ABSORBED]])
    write_error: Exception | None = None
    read_error: Exception | None = None
    queries: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    @property
    def write_calls(self) -> list[tuple[str, dict[str, object] | None]]:
        from ps_service.graph_cleanup.graph_writer import UNMERGE_OBLIGATION_QUERY

        return [call for call in self.queries if call[0] == UNMERGE_OBLIGATION_QUERY]

    def query(self, q: str, params: Mapping[str, object] | None = None) -> QueryResult:
        from ps_service.graph_cleanup.graph_writer import UNMERGE_OBLIGATION_QUERY

        self.queries.append((q, dict(params) if params is not None else None))
        if q == UNMERGE_OBLIGATION_QUERY:
            self.events.append("graph_write")
            if self.write_error is not None:
                raise self.write_error
            return QueryResult(list(self.write_rows))
        if self.read_error is not None:
            raise self.read_error
        ids = set(cast("list[str]", (params or {}).get("ids", [])))
        if "RETURN count(m)" in q:
            return QueryResult([[self.marker_count]])
        if "RETURN count(o)" in q:
            return QueryResult([[self.absorbed_count]])
        if "MATCH (m:MergedObligation) WHERE m.id IN $ids" in q:
            return QueryResult([r for r in self.markers if r[0] in ids])
        if "RETURN o.id, o.text" in q:
            return QueryResult([r for r in self.nodes if r[0] in ids])
        if "MATCH (r:Role {id: $role_id})" in q:
            return QueryResult(list(self.roles))
        if "MATCH (q:Requirement) WHERE q.id IN $ids" in q:
            return QueryResult([r for r in self.requirements if r[0] in ids])
        if "MATCH (c:Capability) WHERE c.id IN $ids RETURN c.id, coalesce" in q:
            return QueryResult([r for r in self.capabilities if r[0] in ids])
        if "[:SATISFIED_BY]" in q:
            return QueryResult([r for r in self.satisfied if r[1] in ids])
        if "[:REQUIRES]" in q:
            return QueryResult([r for r in self.requires if r[0] in ids])
        message = f"unscripted query: {q}"
        raise AssertionError(message)


def obligation_merge_history(*, approval_id: str = "merge-approval-1") -> list[AuditEventRow]:
    """A one-row (newest first) audit history with the REAL planner's obligation-merge snapshot.

    The absorbed Obligation held SATISFIED_BY `req_1`/`req_2` and REQUIRES `cap_1`; the survivor
    already had SATISFIED_BY `req_2` and REQUIRES `cap_1`.
    """
    from ps_service.audit.models import AuditEventRow
    from ps_service.graph_cleanup.audit_actions import ObligationMergeDetails
    from ps_service.graph_cleanup.models import (
        EdgeRecord,
        ObligationMergeState,
        ObligationNodeState,
        RoleRef,
    )
    from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge

    role = RoleRef(id=ROLE_ID, name="Manufacturer")

    def sat(requirement: str, obligation: str) -> EdgeRecord:
        return EdgeRecord(
            rel_type="SATISFIED_BY",
            source_label="Requirement",
            source_id=requirement,
            target_label="Obligation",
            target_id=obligation,
        )

    def req(obligation: str, capability: str) -> EdgeRecord:
        return EdgeRecord(
            rel_type="REQUIRES",
            source_label="Obligation",
            source_id=obligation,
            target_label="Capability",
            target_id=capability,
        )

    plan = plan_obligation_merge(
        ObligationMergeState(
            survivor=ObligationNodeState(
                id=OBL_SURVIVOR, text="Report incidents", properties={"confidence": 0.9}
            ),
            absorbed=ObligationNodeState(
                id=OBL_ABSORBED, text="Report  incidents.", properties={"confidence": 0.8}
            ),
            survivor_roles=(role,),
            absorbed_roles=(role,),
            edges=(
                sat("req_1", OBL_ABSORBED),
                sat("req_2", OBL_ABSORBED),
                sat("req_2", OBL_SURVIVOR),
                req(OBL_ABSORBED, "cap_1"),
                req(OBL_SURVIVOR, "cap_1"),
            ),
        )
    )
    details = ObligationMergeDetails(
        survivor_id=OBL_SURVIVOR,
        absorbed_id=OBL_ABSORBED,
        role_id=ROLE_ID,
        approval_id=approval_id,
        before=plan.before,
        after=plan.after,
    ).model_dump(mode="json")
    return [
        AuditEventRow(
            id="evt-obl-merge",
            occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
            actor_subject="officer",
            actor_issuer="https://issuer.example.com/",
            action="obligation.merge",
            resource_type="obligation",
            resource_id=OBL_ABSORBED,
            outcome="applied",
            details=details,
        )
    ]
