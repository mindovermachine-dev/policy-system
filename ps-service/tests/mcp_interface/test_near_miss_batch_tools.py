"""`merge-near-misses` and `check-near-miss-merges` as an officer drives them.

Over a `CleanupRig` (real reader, planner, executors and gateway rig; only the stores and the
signing ceremony are doubles) these tests call the MCP tools: the preview and the approval link,
the signed outcome the status tool reports, and the refusals that create no approval.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from graph_cleanup._fakes import ABSORBED, SURVIVOR, approval_row_count, expire_approval
from graph_cleanup._mcp_calls import call_tool, factory
from graph_cleanup._merge_runs import OFFICER
from graph_cleanup._near_miss_batch_worlds import (
    seed_capability_pair_with_review,
    seed_policy_governing,
    seed_role_pair_with_review,
)
from graph_gateway._fakes import close_all_rigs

from mcp_interface._near_miss_batch_env import REVIEW, BatchEnv
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.tool_core import CLEANUP_GATE, OWN_APPROVAL_READ_GATE
from ps_service.passkey_signing.errors import PendingApprovalPersistenceError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from graph_cleanup._gateway_fakes import CleanupRig

_STRANGER = ("stranger", "https://issuer.example.com/")


@pytest.fixture
def cleanup_rig() -> Iterator[CleanupRig]:
    from graph_cleanup._gateway_fakes import CleanupRig

    rig = CleanupRig()
    yield rig
    close_all_rigs()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig) -> BatchEnv:
    configure()
    return BatchEnv(monkeypatch, cleanup_rig)


def test_merge_near_misses_returns_a_preview_entry_and_an_approval_link_for_one_review(
    env: BatchEnv,
) -> None:
    before = env.snapshot()

    body = json.loads(env.merge())

    assert set(body) == {"merges", "pending_approval_id", "approval_url", "expires_at"}
    [entry] = body["merges"]
    assert entry["review_id"] == REVIEW
    assert entry["kind"] == "Capability"
    assert entry["survivor_id"] == SURVIVOR
    assert entry["absorbed_id"] == ABSORBED
    assert entry["policy_case"] == 1
    assert entry["edges_to_move"]["covers"] == ["pa_1"]
    assert entry["state_digest"]
    assert entry["acknowledged"] is False
    assert body["approval_url"].startswith(
        f"http://unknown/approvals/{body['pending_approval_id']}#"
    )
    row = env.store.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.tool_name == "merge-near-misses"
    assert (row.actor_subject, row.actor_issuer) == OFFICER
    assert env.snapshot() == before


def test_check_near_miss_merges_reports_winner_and_loser_once_signed(env: BatchEnv) -> None:
    approval_id = json.loads(env.merge())["pending_approval_id"]

    pending = json.loads(env.check(approval_id))
    env.sign_and_execute(approval_id)
    signed = json.loads(env.check(approval_id))

    assert pending == {"pending_approval_id": approval_id, "status": "pending", "merges": []}
    assert signed == {
        "pending_approval_id": approval_id,
        "status": "signed",
        "merges": [
            {
                "review_id": REVIEW,
                "kind": "Capability",
                "state": "merged",
                "winner_id": SURVIVOR,
                "loser_id": ABSORBED,
                "error": None,
            }
        ],
    }


def test_the_two_new_tools_are_declared_with_the_cleanup_gate_and_the_own_approval_gate() -> None:
    assert mcp_server.TOOL_GATES["merge-near-misses"] is CLEANUP_GATE
    assert mcp_server.TOOL_GATES["check-near-miss-merges"] is OWN_APPROVAL_READ_GATE


def test_an_unknown_review_id_gives_an_error_and_no_approval(env: BatchEnv) -> None:
    before = env.snapshot()

    text = env.merge(["missing"])

    assert text.startswith("error: ")
    assert "missing" in text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_another_principal_checking_the_batch_gets_the_same_error_as_an_unknown_id(
    env: BatchEnv,
) -> None:
    approval_id = json.loads(env.merge())["pending_approval_id"]

    foreign = env.check(approval_id, who=_STRANGER)
    unknown = env.check("no-such-id", who=_STRANGER)

    assert foreign == f"error: no pending approval with id {approval_id!r}"
    assert unknown == "error: no pending approval with id 'no-such-id'"


def test_a_mixed_batch_returns_one_approval_and_a_preview_entry_per_review_id_in_selection_order(
    env: BatchEnv,
) -> None:
    role = seed_role_pair_with_review(env.rig, 1)
    capability = seed_capability_pair_with_review(env.rig, 1)
    before = env.snapshot()

    body = json.loads(env.merge([role.review_id, REVIEW, capability.review_id]))

    assert [(m["review_id"], m["kind"]) for m in body["merges"]] == [
        (role.review_id, "Role"),
        (REVIEW, "Capability"),
        (capability.review_id, "Capability"),
    ]
    assert approval_row_count(env.store) == 1
    assert body["merges"][0]["moved_obligation_ids"] == ["obl_role_ex1"]
    assert env.snapshot() == before


def test_a_refused_request_is_an_error_string_listing_each_review_id_and_its_state(
    env: BatchEnv,
) -> None:
    stale = seed_capability_pair_with_review(env.rig, 1)
    del env.rig.graph.nodes[("Capability", stale.absorbed_id)]
    before = env.snapshot()

    text = env.merge([REVIEW, "missing", stale.review_id])

    assert text.startswith("error: ")
    assert "'missing': no unresolved near-miss review has this id" in text
    assert f"{stale.review_id!r}: " in text
    assert "stale" in text
    assert REVIEW not in text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_an_empty_or_duplicated_selection_is_an_error_naming_the_problem(env: BatchEnv) -> None:
    assert "no review ids" in env.merge([])
    duplicated = env.merge([REVIEW, REVIEW])
    assert duplicated.startswith("error: ")
    assert REVIEW in duplicated
    assert approval_row_count(env.store) == 0


def test_an_unreachable_graph_gives_the_fixed_message_and_no_approval(
    monkeypatch: pytest.MonkeyPatch, env: BatchEnv
) -> None:
    def _unreachable(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"
        raise ConnectionError(message)

    _override(monkeypatch, graph_factory=_unreachable)
    before = env.snapshot()

    text = env.merge()

    assert text == "error: the policy graph database is not reachable"
    assert "10.0.0.1" not in text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_an_unexpected_failure_gives_the_fixed_message_and_no_approval(
    monkeypatch: pytest.MonkeyPatch, env: BatchEnv
) -> None:
    class _RaisingStore:
        def create_pending_approval(self, **kwargs: object) -> object:
            _ = kwargs
            message = "insert failed on host 10.1.2.3:5432 password=hunter2"
            raise PendingApprovalPersistenceError(message)

    _override(monkeypatch, pending_approval_store_factory=factory(_RaisingStore()))
    before = env.snapshot()

    text = env.merge()

    assert text == "error: an unexpected error occurred"
    for secret in ("hunter2", "10.1.2.3"):
        assert secret not in text
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_the_overlap_and_policy_refusals_create_no_approval_row(env: BatchEnv) -> None:
    other = seed_capability_pair_with_review(env.rig, 1)
    env.rig.seed_capability("cap_x", name="Other", status="active")
    env.rig.seed_near_miss_review("review_share", incoming_id=SURVIVOR, existing_id="cap_x")
    seed_policy_governing(env.rig, "pol_a", other.survivor_id)
    seed_policy_governing(env.rig, "pol_b", other.absorbed_id)
    before = env.snapshot()

    overlap = env.merge([REVIEW, "review_share"])
    policies = env.merge([other.review_id])

    assert overlap.startswith("error: ")
    assert REVIEW in overlap
    assert "review_share" in overlap
    assert policies.startswith("error: ")
    assert "release-capability-governance" in policies
    assert other.review_id in policies
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_a_case_two_review_returns_acknowledgment_required_with_the_previews_and_no_approval(
    env: BatchEnv,
) -> None:
    seed_policy_governing(env.rig, "pol_1", ABSORBED)
    before = env.snapshot()

    body = json.loads(env.merge())

    assert body["acknowledgment_required"] is True
    assert body["reviews"] == [
        {
            "review_id": REVIEW,
            "policy": {"id": "pol_1", "title": "Policy pol_1", "status": "approved"},
        }
    ]
    assert "pol_1" in body["message"]
    assert "acknowledged_review_ids" in body["message"]
    assert [m["review_id"] for m in body["merges"]] == [REVIEW]
    assert "pending_approval_id" not in body
    assert approval_row_count(env.store) == 0
    assert env.snapshot() == before


def test_repeating_the_call_with_the_acknowledgment_returns_the_approval(env: BatchEnv) -> None:
    seed_policy_governing(env.rig, "pol_1", ABSORBED)

    body = json.loads(env.merge(acknowledged=[REVIEW]))

    assert body["merges"][0]["acknowledged"] is True
    assert body["pending_approval_id"]
    assert approval_row_count(env.store) == 1


def test_an_acknowledgment_that_does_not_apply_is_an_error_and_creates_no_approval(
    env: BatchEnv,
) -> None:
    text = env.merge(acknowledged=[REVIEW])

    assert text.startswith("error: ")
    assert REVIEW in text
    assert approval_row_count(env.store) == 0


def test_a_batch_approval_is_not_visible_through_the_cleanup_approval_check(env: BatchEnv) -> None:
    approval_id = json.loads(env.merge())["pending_approval_id"]

    text = call_tool("check-cleanup-approval", {"pending_approval_id": approval_id}, OFFICER)

    assert text == f"error: no pending approval with id {approval_id!r}"


def test_check_reports_failed_and_merged_entries_side_by_side_for_a_partially_failed_batch(
    env: BatchEnv,
) -> None:
    stale = seed_capability_pair_with_review(env.rig, 1)
    approval_id = json.loads(env.merge([REVIEW, stale.review_id]))["pending_approval_id"]
    env.rig.seed_endpoint("Obligation", "obl_late")
    env.rig.seed_edge(
        ("REQUIRES", "Obligation", "obl_late", "Capability", stale.absorbed_id), legacy=False
    )

    env.sign_and_execute(approval_id)
    body = json.loads(env.check(approval_id))

    assert body["status"] == "signed"
    merged, failed = body["merges"]
    assert (merged["review_id"], merged["state"], merged["winner_id"]) == (
        REVIEW,
        "merged",
        SURVIVOR,
    )
    assert merged["error"] is None
    assert (failed["review_id"], failed["state"]) == (stale.review_id, "failed")
    assert (failed["winner_id"], failed["loser_id"]) == (None, None)
    assert failed["error"]


def test_an_unsettled_entry_is_rendered_as_unsettled_and_never_as_merged_or_failed(
    env: BatchEnv,
) -> None:
    approval_id = json.loads(env.merge())["pending_approval_id"]
    assert env.store.mark_signed(approval_id)

    body = json.loads(env.check(approval_id))

    assert body["status"] == "signed"
    [entry] = body["merges"]
    assert (entry["review_id"], entry["state"]) == (REVIEW, "unsettled")
    assert (entry["winner_id"], entry["loser_id"], entry["error"]) == (None, None, None)
    assert "merged" not in json.dumps(body["merges"]).replace('"state": "unsettled"', "")


def test_an_unreadable_audit_trail_while_settling_gives_the_fixed_check_again_message(
    monkeypatch: pytest.MonkeyPatch, env: BatchEnv
) -> None:
    approval_id = json.loads(env.merge())["pending_approval_id"]
    assert env.store.mark_signed(approval_id)
    expire_approval(env.store, approval_id, expired_for=timedelta(minutes=10))
    _override(monkeypatch, audit_store_factory=factory(env.rig.audit))
    env.rig.audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("down at 10.1.2.3")

    text = env.check(approval_id)

    assert text.startswith("error: ")
    assert "10.1.2.3" not in text
    assert "try again" in text
    stored = env.store.get_by_id(approval_id)
    assert stored is not None
    assert stored.outcome is None
