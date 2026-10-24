"""`merge-near-misses` needs an explicit `ComplianceOfficer` grant on a real session.

No admin override and no local-test bypass satisfy the gate, and a refused call creates no
approval and leaves the graph untouched.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore
from graph_cleanup._fakes import approval_row_count
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._merge_runs import OWNER
from graph_gateway._fakes import close_all_rigs

from mcp_interface._near_miss_batch_env import DENIED, REVIEW, UNAUTHENTICATED, BatchEnv
from mcp_interface.test_access_role_tools import (
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.authz.models import AccessRole
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def cleanup_rig() -> Iterator[CleanupRig]:
    rig = CleanupRig()
    yield rig
    close_all_rigs()


@pytest.mark.parametrize("role", [None, AccessRole.SYSTEM_ADMIN, AccessRole.SYSTEM_OWNER])
def test_a_caller_without_an_explicit_officer_grant_gets_the_access_error_and_no_approval(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig, role: AccessRole | None
) -> None:
    configure()
    access_roles = FakeAccessRoleStore(expected_owner=OWNER)
    access_roles.bootstrap_first_owner(OWNER)
    caller = ("plain-user", OWNER[1])
    if role is AccessRole.SYSTEM_OWNER:
        caller = OWNER
    elif role is AccessRole.SYSTEM_ADMIN:
        access_roles.grant(actor=OWNER, target=caller, access_role=role)
    environment = BatchEnv(monkeypatch, cleanup_rig, access_roles=access_roles)
    before = environment.snapshot()

    text = environment.merge(who=caller)

    assert text == DENIED
    assert approval_row_count(environment.store) == 0
    assert environment.snapshot() == before


@pytest.mark.parametrize("bypass", [False, True])
def test_no_verified_bearer_token_gets_an_error_and_creates_no_approval(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig, *, bypass: bool
) -> None:
    if bypass:
        monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    environment = BatchEnv(monkeypatch, cleanup_rig, access_roles=RaisingAccessRoleStore())
    before = environment.snapshot()

    text = asyncio.run(_server().call_tool("merge-near-misses", {"review_ids": [REVIEW]}))

    assert UNAUTHENTICATED in str(text)
    assert approval_row_count(environment.store) == 0
    assert environment.snapshot() == before


def test_the_local_test_bypass_does_not_satisfy_the_gate(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    environment = BatchEnv(monkeypatch, cleanup_rig)

    text = asyncio.run(_server().call_tool("merge-near-misses", {"review_ids": [REVIEW]}))

    assert UNAUTHENTICATED in str(text)
    assert approval_row_count(environment.store) == 0
