"""A rig world with one near-miss review and the batch tools wired to it."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from graph_cleanup._fakes import ABSORBED, SURVIVOR, FakeApprovalStore
from graph_cleanup._mcp_calls import call_tool, factory
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles

from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.graph_cleanup.near_miss_batch_execution import execute_near_miss_batch

if TYPE_CHECKING:
    import pytest
    from graph_cleanup._gateway_fakes import CleanupReadGraph, CleanupRig

    from ps_service.authz.store import AccessRoleStore

REVIEW = "review_aaa"
OLD = "2020-01-01T00:00:00+00:00"
NEW = "2024-01-01T00:00:00+00:00"
DENIED = "error: You do not have the required access role for this action."
UNAUTHENTICATED = "error: graph cleanup requires a real authenticated caller"


class BatchEnv:
    """A rig world with a Capability review of (ABSORBED incoming, SURVIVOR existing)."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rig: CleanupRig,
        *,
        access_roles: object | None = None,
    ) -> None:
        self.rig = rig
        self.store = FakeApprovalStore()
        rig.seed_capability_merge_world()
        rig.seed_near_miss_review(
            REVIEW,
            incoming_id=ABSORBED,
            existing_id=SURVIVOR,
            incoming_created_at=NEW,
            existing_created_at=OLD,
        )

        def _open(_config: object) -> CleanupReadGraph:
            return rig.read

        _override(
            monkeypatch,
            role_store_factory=factory(access_roles or roles()),
            pending_approval_store_factory=factory(self.store),
            graph_factory=_open,
        )

    def snapshot(self) -> object:
        return copy.deepcopy(dict(self.rig.graph.nodes)), set(self.rig.graph.edges)

    def merge(
        self,
        review_ids: list[str] | None = None,
        *,
        who: tuple[str, str] = OFFICER,
        acknowledged: list[str] | None = None,
    ) -> str:
        args: dict[str, object] = {"review_ids": review_ids if review_ids is not None else [REVIEW]}
        if acknowledged is not None:
            args["acknowledged_review_ids"] = acknowledged
        return call_tool("merge-near-misses", args, who)

    def check(self, approval_id: str, *, who: tuple[str, str] = OFFICER) -> str:
        return call_tool("check-near-miss-merges", {"pending_approval_id": approval_id}, who)

    def sign_and_execute(
        self, approval_id: str, *, access_roles: AccessRoleStore | None = None
    ) -> dict[str, object]:
        """Sign the way the router does: mark signed, run the executor, store the outcome."""
        assert self.store.mark_signed(approval_id)
        signed = self.store.get_by_id(approval_id)
        assert signed is not None
        outcome = execute_near_miss_batch(
            signed, CONFIG, self.rig.dependencies(access_roles or roles())
        )
        self.store.set_outcome(approval_id, outcome)
        return outcome
