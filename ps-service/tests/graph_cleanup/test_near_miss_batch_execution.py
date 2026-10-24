"""Executing a signed near-miss batch: every merge is tried, each outcome is reported by name.

Over a `CleanupRig` (real reader, planner, per-kind executors and gateway rig; only the stores and
the audit trail are doubles) a batch is created, marked signed and executed. A merge that cannot
run is reported as failed with the executor's safe text and the other merges still run; a failed
merge is not retried, and the batch modules carry no merge Cypher of their own.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from graph_cleanup._fakes import FakeApprovalStore, RecordingAuditStore
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from graph_cleanup._near_miss_batch_worlds import (
    SeededPair,
    seed_capability_pair_with_review,
    seed_role_pair_with_review,
)
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.graph_cleanup import (
    near_miss_batch,
    near_miss_batch_execution,
    near_miss_batch_status,
)
from ps_service.graph_cleanup.near_miss_batch import create_near_miss_merge_batch
from ps_service.graph_cleanup.near_miss_batch_execution import execute_near_miss_batch
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.passkey_signing.models import PendingApprovalRow

_GENERIC_ITEM_ERROR = "this merge could not be carried out because of an unexpected error"
_NOT_AUDITED = "the merge was not performed because it could not be audited; nothing was changed"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _signed(rig: CleanupRig, review_ids: list[str]) -> PendingApprovalRow:
    store = FakeApprovalStore()
    approval = create_near_miss_merge_batch(
        rig.read,
        review_ids=review_ids,
        acknowledged_review_ids=[],
        actor=OFFICER,
        base_url="https://ps.example.com",
        store=store,
    )
    assert store.mark_signed(approval.pending_approval_id)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    return row


def _entries(outcome: Mapping[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", outcome["merges"])


def _states(outcome: Mapping[str, object]) -> list[tuple[object, object]]:
    return [(entry["review_id"], entry["state"]) for entry in _entries(outcome)]


def _make_stale(rig: CleanupRig, pair: SeededPair) -> None:
    """Give the absorbed node a requirer after the preview, so its state digest no longer holds."""
    rig.seed_endpoint("Obligation", f"obl_late_{pair.absorbed_id}")
    rig.seed_edge(
        ("REQUIRES", "Obligation", f"obl_late_{pair.absorbed_id}", "Capability", pair.absorbed_id),
        legacy=False,
    )


def _three(rig: CleanupRig) -> tuple[SeededPair, SeededPair, SeededPair]:
    return (
        seed_capability_pair_with_review(rig, 1),
        seed_capability_pair_with_review(rig, 2),
        seed_capability_pair_with_review(rig, 3),
    )


def test_one_stale_item_fails_by_name_while_the_other_items_still_merge(
    cleanup_rig: CleanupRig,
) -> None:
    first, second, third = _three(cleanup_rig)
    row = _signed(cleanup_rig, [first.review_id, second.review_id, third.review_id])
    _make_stale(cleanup_rig, second)

    outcome = execute_near_miss_batch(row, CONFIG, cleanup_rig.dependencies(roles()))

    assert _states(outcome) == [
        (first.review_id, "merged"),
        (second.review_id, "failed"),
        (third.review_id, "merged"),
    ]
    failed = _entries(outcome)[1]
    assert failed["error"]
    assert (failed["winner_id"], failed["loser_id"]) == (None, None)
    assert (outcome["merged_count"], outcome["failed_count"]) == (2, 1)
    assert ("Capability", second.absorbed_id) in cleanup_rig.graph.nodes
    assert ("PendingReview", second.review_id) in cleanup_rig.graph.nodes
    assert ("Capability", first.absorbed_id) not in cleanup_rig.graph.nodes
    assert ("Capability", third.absorbed_id) not in cleanup_rig.graph.nodes


def test_a_failed_item_writes_a_failed_merge_row_and_the_merged_items_write_applied_rows(
    cleanup_rig: CleanupRig,
) -> None:
    first, second, third = _three(cleanup_rig)
    row = _signed(cleanup_rig, [first.review_id, second.review_id, third.review_id])
    _make_stale(cleanup_rig, second)

    execute_near_miss_batch(row, CONFIG, cleanup_rig.dependencies(roles()))

    assert [(r.action, r.outcome, r.resource_id) for r in cleanup_rig.audit.rows] == [
        ("capability.merge", "applied", first.absorbed_id),
        ("capability.merge", "failed", second.absorbed_id),
        ("capability.merge", "applied", third.absorbed_id),
    ]
    assert {r.details["approval_id"] for r in cleanup_rig.audit.rows} == {row.id}
    assert [r.details["review_id"] for r in cleanup_rig.audit.rows] == [
        first.review_id,
        second.review_id,
        third.review_id,
    ]


def test_a_failed_item_is_not_retried(cleanup_rig: CleanupRig) -> None:
    first, second = (seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2))
    row = _signed(cleanup_rig, [first.review_id, second.review_id])
    _make_stale(cleanup_rig, first)

    execute_near_miss_batch(row, CONFIG, cleanup_rig.dependencies(roles()))

    assert [r.resource_id for r in cleanup_rig.audit.rows].count(first.absorbed_id) == 1
    assert len(cleanup_rig.logged_groups) == 1


def test_the_outcome_has_one_entry_per_review_id_with_winner_loser_or_the_executors_error_text(
    cleanup_rig: CleanupRig,
) -> None:
    capability = seed_capability_pair_with_review(cleanup_rig, 1)
    role = seed_role_pair_with_review(cleanup_rig, 1)
    stale = seed_capability_pair_with_review(cleanup_rig, 2)
    row = _signed(cleanup_rig, [capability.review_id, stale.review_id, role.review_id])
    _make_stale(cleanup_rig, stale)

    outcome = execute_near_miss_batch(row, CONFIG, cleanup_rig.dependencies(roles()))

    entries = _entries(outcome)
    assert [e["review_id"] for e in entries] == [
        capability.review_id,
        stale.review_id,
        role.review_id,
    ]
    assert entries[0] == {
        "review_id": capability.review_id,
        "kind": "Capability",
        "state": "merged",
        "winner_id": capability.survivor_id,
        "loser_id": capability.absorbed_id,
        "error": None,
    }
    assert entries[2]["winner_id"] == role.survivor_id
    assert entries[2]["loser_id"] == role.absorbed_id
    assert entries[1]["state"] == "failed"
    assert isinstance(entries[1]["error"], str)
    assert "changed" in entries[1]["error"]


def test_a_guard_miss_in_the_middle_of_the_batch_does_not_stop_the_later_items(
    cleanup_rig: CleanupRig,
) -> None:
    first, second, third = _three(cleanup_rig)
    row = _signed(cleanup_rig, [first.review_id, second.review_id, third.review_id])
    cleanup_rig.read.flip_before("RETURN c.id, c.name", 4, lambda: _make_stale(cleanup_rig, second))

    outcome = execute_near_miss_batch(row, CONFIG, cleanup_rig.dependencies(roles()))

    assert [state for _, state in _states(outcome)] == ["merged", "failed", "merged"]
    assert [
        (r.outcome, r.resource_id) for r in cleanup_rig.audit.rows if r.outcome == "failed"
    ] == [("failed", second.absorbed_id)]


def test_an_item_whose_review_was_resolved_since_the_preview_fails_by_name(
    cleanup_rig: CleanupRig,
) -> None:
    first, second = (seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2))
    row = _signed(cleanup_rig, [first.review_id, second.review_id])
    del cleanup_rig.graph.nodes[("PendingReview", first.review_id)]

    outcome = execute_near_miss_batch(row, CONFIG, cleanup_rig.dependencies(roles()))

    assert _states(outcome) == [(first.review_id, "failed"), (second.review_id, "merged")]
    assert ("Capability", first.absorbed_id) in cleanup_rig.graph.nodes


def test_when_the_audit_trail_cannot_record_a_merge_that_merge_is_not_performed_and_is_reported(
    cleanup_rig: CleanupRig,
) -> None:
    first, second = (seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2))
    row = _signed(cleanup_rig, [first.review_id, second.review_id])
    cleanup_rig.audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError(
        "db host 10.1.2.3"
    )
    before = copy.deepcopy(dict(cleanup_rig.graph.nodes)), set(cleanup_rig.graph.edges)

    outcome = execute_near_miss_batch(row, CONFIG, cleanup_rig.dependencies(roles()))

    assert _states(outcome) == [(first.review_id, "failed"), (second.review_id, "failed")]
    assert [e["error"] for e in _entries(outcome)] == [_NOT_AUDITED, _NOT_AUDITED]
    assert "10.1.2.3" not in str(outcome)
    assert cleanup_rig.logged_groups == []
    assert (copy.deepcopy(dict(cleanup_rig.graph.nodes)), set(cleanup_rig.graph.edges)) == before


class _FlakyAudit(RecordingAuditStore):
    """An audit store that fails unexpectedly writing the `applied` row of one resource.

    With `poison_failed_rows` its `failed` row fails too, as when the whole trail is down.
    """

    poisoned: str = ""
    poison_failed_rows: bool = False

    def record_standalone_with_event_id(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        details: Mapping[str, object],
    ) -> str:
        if resource_id == self.poisoned and (outcome == "applied" or self.poison_failed_rows):
            message = "unexpected audit failure"
            raise RuntimeError(message)
        return super().record_standalone_with_event_id(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details,
        )


def _flaky(rig: CleanupRig, *, poisoned: str, poison_failed_rows: bool) -> GraphCleanupDependencies:
    audit = _FlakyAudit(
        rows=rig.audit.rows,
        events=rig.audit.events,
        event_ids=rig.audit.event_ids,
        event_id_sink=rig.audit.event_id_sink,
    )
    audit.poisoned = poisoned
    audit.poison_failed_rows = poison_failed_rows

    def _audit_store(_config: ServiceConfig) -> RecordingAuditStore:
        return audit

    return dataclasses.replace(rig.dependencies(roles()), audit_store=_audit_store)


def test_an_unexpected_exception_in_one_item_becomes_a_failed_entry_and_the_outcome_is_stored(
    cleanup_rig: CleanupRig,
) -> None:
    first, second, third = _three(cleanup_rig)
    row = _signed(cleanup_rig, [first.review_id, second.review_id, third.review_id])
    dependencies = _flaky(cleanup_rig, poisoned=second.absorbed_id, poison_failed_rows=False)

    outcome = execute_near_miss_batch(row, CONFIG, dependencies)

    assert _states(outcome) == [
        (first.review_id, "merged"),
        (second.review_id, "failed"),
        (third.review_id, "merged"),
    ]
    assert _entries(outcome)[1]["error"] == _GENERIC_ITEM_ERROR
    assert "unexpected audit failure" not in str(outcome)
    assert [(r.outcome, r.resource_id) for r in cleanup_rig.audit.rows] == [
        ("applied", first.absorbed_id),
        ("failed", second.absorbed_id),
        ("applied", third.absorbed_id),
    ]
    assert cleanup_rig.audit.rows[1].details["review_id"] == second.review_id
    assert cleanup_rig.audit.rows[1].details["approval_id"] == row.id


def test_an_unexpected_exception_with_the_audit_trail_down_still_stores_the_batch_outcome(
    cleanup_rig: CleanupRig,
) -> None:
    first, second = (seed_capability_pair_with_review(cleanup_rig, n) for n in (1, 2))
    row = _signed(cleanup_rig, [first.review_id, second.review_id])
    dependencies = _flaky(cleanup_rig, poisoned=first.absorbed_id, poison_failed_rows=True)

    outcome = execute_near_miss_batch(row, CONFIG, dependencies)

    assert _states(outcome) == [(first.review_id, "failed"), (second.review_id, "merged")]
    assert _entries(outcome)[0]["error"] == _GENERIC_ITEM_ERROR
    assert [r.resource_id for r in cleanup_rig.audit.rows] == [second.absorbed_id]


_CYPHER_WORDS = re.compile(r"\b(MATCH|MERGE|DELETE|DETACH|CREATE|SET|RETURN)\b")
_FORBIDDEN_NAMES = frozenset(
    {
        "graph_" + "write_target",
        "Mutation" + "Group",
        "write_" + "capability_merge",
        "write_" + "role_merge",
    }
)
_GRAPH_QUERY_CALLS = frozenset({"query", "ro_query"})
_AUDIT_RECEIVERS = frozenset({"audit_store", "store"})


def _violations(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _CYPHER_WORDS.search(node.value):
                found.append(f"cypher keyword in a string at line {node.lineno}")
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in _FORBIDDEN_NAMES:
                found.append(f"{name} at line {node.lineno}")
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in _FORBIDDEN_NAMES:
            found.append(f"import of {node.name}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _GRAPH_QUERY_CALLS
        ):
            receiver = node.func.value
            receiver_name = (
                receiver.id
                if isinstance(receiver, ast.Name)
                else receiver.attr
                if isinstance(receiver, ast.Attribute)
                else ""
            )
            if receiver_name not in _AUDIT_RECEIVERS:
                found.append(f"{node.func.attr}() call at line {node.lineno}")
    return found


@pytest.mark.parametrize(
    "module", [near_miss_batch, near_miss_batch_execution, near_miss_batch_status]
)
def test_the_batch_modules_contain_no_cypher_and_build_no_write_group(module: ModuleType) -> None:
    assert module.__file__ is not None
    path = Path(module.__file__)

    assert _violations(path) == []


def test_the_scan_catches_a_cypher_string_a_write_group_name_and_a_graph_query(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from x import " + "Mutation" + 'Group\ntext = "MATCH (n) RETURN n"\ngraph.query(text)\n',
        encoding="utf-8",
    )

    assert len(_violations(sample)) == 3
