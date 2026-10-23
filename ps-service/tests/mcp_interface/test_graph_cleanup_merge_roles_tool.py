"""`merge-roles` (preview + approval) through the MCP tool.

Covers the preview (no writes, approval bound to the pair and the previewed state) and the
rejections that create no approval. The graph is the Graph Cleanup gateway rig's `policy_system`
graph, read through its read-only handle. Gate behaviour is in `test_graph_cleanup_authz_gate.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from authz._fakes import FakeAccessRoleStore
from graph_cleanup._fakes import (
    INSTRUMENT_ID,
    OBL_MOVED_1,
    OBL_MOVED_2,
    OBL_OF_SURVIVOR,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    FakeApprovalStore,
    approval_row_count,
)
from graph_cleanup._gateway_fakes import CleanupReadGraph, CleanupRig
from graph_gateway._fakes import close_all_rigs
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server, tool_core
from ps_service.passkey_signing.errors import PendingApprovalPersistenceError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

_OWNER = "owner"
_OFFICER = "officer"
_ADMIN = "admin"
_ISSUER = "https://issuer.example.com/"

_MERGE_ARGS: dict[str, object] = {"survivor_id": ROLE_SURVIVOR, "absorbed_id": ROLE_ABSORBED}


@contextlib.contextmanager
def _actor(sub: str) -> Generator[None]:
    token = AccessToken(token="t", client_id="c", scopes=[], subject=sub, claims={"iss": _ISSUER})
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _factory(value: object) -> Callable[..., object]:
    def _make(*_args: object, **_kwargs: object) -> object:
        return value

    return _make


def _officers() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=(_OWNER, _ISSUER))
    store.bootstrap_first_owner((_OWNER, _ISSUER))
    store.grant(
        actor=(_OWNER, _ISSUER),
        target=(_OFFICER, _ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    store.grant(
        actor=(_OWNER, _ISSUER), target=(_ADMIN, _ISSUER), access_role=AccessRole.SYSTEM_ADMIN
    )
    return store


class _Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rig = CleanupRig()
        self.rig.seed_role_merge_world()
        self.pending = FakeApprovalStore()
        monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _factory(_officers()))
        monkeypatch.setattr(tool_core, "PsycopgPendingApprovalStore", _factory(self.pending))
        monkeypatch.setattr(
            "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
            _factory(self._open_graph),
        )

    def _open_graph(self, _config: object) -> CleanupReadGraph:
        return self.rig.read

    def graph_state(self) -> tuple[object, object]:
        """The rig graph's nodes and edges, to compare before and after a call."""
        graph = self.rig.graph
        return dict(graph.nodes), dict(graph.edges)

    def call(self, name: str, args: dict[str, object], *, sub: str = _OFFICER) -> str:
        with _actor(sub):
            result = asyncio.run(mcp_server.server.call_tool(name, args))
        assert isinstance(result, CallToolResult)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return block.text


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    configure()
    built = _Env(monkeypatch)
    yield built
    close_all_rigs()


def test_merge_returns_the_preview_and_a_pending_approval_and_writes_nothing(env: _Env) -> None:
    before = env.graph_state()

    body = json.loads(env.call("merge-roles", _MERGE_ARGS))

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    preview = body["preview"]
    assert preview["survivor_id"] == ROLE_SURVIVOR
    assert preview["survivor_name"] == "Market Surveillance Authority"
    assert preview["absorbed_id"] == ROLE_ABSORBED
    assert preview["absorbed_name"] == "market surveillance authorities"
    assert preview["instrument_id"] == INSTRUMENT_ID
    assert preview["moved_obligation_ids"] == [OBL_MOVED_1, OBL_MOVED_2]
    assert body["approval_url"].startswith(
        f"http://unknown/approvals/{body['pending_approval_id']}#"
    )
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert (row.actor_subject, row.actor_issuer) == (_OFFICER, _ISSUER)
    assert row.tool_name == "merge-roles"
    assert row.normalized_args == {
        "survivor_id": ROLE_SURVIVOR,
        "absorbed_id": ROLE_ABSORBED,
        "state_digest": preview["state_digest"],
    }
    assert approval_row_count(env.pending) == 1
    assert env.graph_state() == before
    assert env.rig.logged_groups == []


def test_check_approval_reports_a_role_merge_approval_to_its_creator(env: _Env) -> None:
    created = json.loads(env.call("merge-roles", _MERGE_ARGS))
    approval_id = created["pending_approval_id"]

    status = json.loads(env.call("check-cleanup-approval", {"pending_approval_id": approval_id}))

    assert status == {
        "pending_approval_id": approval_id,
        "status": "pending",
        "tool_name": "merge-roles",
        "outcome": None,
    }


def _rig_with_second_instrument(env: _Env) -> None:
    env.rig.seed_instrument("reg_gdpr")
    env.rig.seed_role("role_gdpr_controller", name="Controller", confidence=0.9)
    env.rig.seed_defines("reg_gdpr", "role_gdpr_controller", source_ref="Art. 4(7)")


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"survivor_id": ROLE_SURVIVOR, "absorbed_id": ROLE_SURVIVOR}, "into itself"),
        ({"survivor_id": ROLE_SURVIVOR, "absorbed_id": "role_missing"}, "does not exist"),
        ({"survivor_id": "role_missing", "absorbed_id": ROLE_ABSORBED}, "does not exist"),
        ({"survivor_id": ROLE_SURVIVOR, "absorbed_id": OBL_MOVED_1}, "does not exist"),
        ({"survivor_id": OBL_MOVED_1, "absorbed_id": ROLE_ABSORBED}, "does not exist"),
    ],
    ids=["same-id", "absorbed-missing", "survivor-missing", "absorbed-obligation", "survivor-obl"],
)
def test_self_missing_and_non_role_ids_are_rejected_with_no_approval(
    env: _Env, args: dict[str, object], fragment: str
) -> None:
    before = env.graph_state()

    text = env.call("merge-roles", args)

    assert text.startswith("error: ")
    assert fragment in text
    assert approval_row_count(env.pending) == 0
    assert env.graph_state() == before


def test_a_merged_role_marker_id_is_rejected_as_a_marker_with_no_approval(env: _Env) -> None:
    env.rig.seed_endpoint("MergedRole", "role_gone", merged_into=ROLE_SURVIVOR)

    text = env.call("merge-roles", {"survivor_id": ROLE_SURVIVOR, "absorbed_id": "role_gone"})

    assert text.startswith("error: ")
    assert "MergedRole marker" in text
    assert approval_row_count(env.pending) == 0


def test_roles_of_different_instruments_are_rejected_with_no_approval(env: _Env) -> None:
    _rig_with_second_instrument(env)

    text = env.call(
        "merge-roles", {"survivor_id": ROLE_SURVIVOR, "absorbed_id": "role_gdpr_controller"}
    )

    assert text.startswith("error: ")
    assert "different instruments" in text
    assert approval_row_count(env.pending) == 0


def test_a_role_without_a_defines_edge_is_rejected_with_no_approval(env: _Env) -> None:
    env.rig.seed_role("role_orphan", name="Orphan", confidence=0.5)

    text = env.call("merge-roles", {"survivor_id": ROLE_SURVIVOR, "absorbed_id": "role_orphan"})

    assert text.startswith("error: ")
    assert "exactly one instrument" in text
    assert approval_row_count(env.pending) == 0


def test_a_role_with_a_second_defines_edge_is_rejected_with_no_approval(env: _Env) -> None:
    _rig_with_second_instrument(env)
    env.rig.seed_defines("reg_gdpr", ROLE_ABSORBED, source_ref="Art. 9")

    text = env.call("merge-roles", _MERGE_ARGS)

    assert text.startswith("error: ")
    assert "exactly one instrument" in text
    assert approval_row_count(env.pending) == 0


def test_an_absorbed_role_with_an_unexpected_edge_is_rejected_with_no_approval(env: _Env) -> None:
    env.rig.seed_endpoint("Requirement", "req_odd")
    env.rig.seed_edge(("OWNS", "Role", ROLE_ABSORBED, "Requirement", "req_odd"), legacy=False)

    text = env.call("merge-roles", _MERGE_ARGS)

    assert text.startswith("error: ")
    assert "other than DEFINES and HAS" in text
    assert approval_row_count(env.pending) == 0


def test_a_moved_obligation_also_borne_by_a_third_role_is_rejected_with_no_approval(
    env: _Env,
) -> None:
    env.rig.seed_role("role_third", name="Third", confidence=0.5)
    env.rig.seed_defines(INSTRUMENT_ID, "role_third", source_ref="Art. 5")
    env.rig.seed_has("role_third", OBL_MOVED_1)

    text = env.call("merge-roles", _MERGE_ARGS)

    assert text.startswith("error: ")
    assert "borne by the absorbed role alone" in text
    assert approval_row_count(env.pending) == 0


def test_a_moved_obligation_the_survivor_also_bears_is_rejected_with_no_approval(
    env: _Env,
) -> None:
    env.rig.seed_has(ROLE_SURVIVOR, OBL_MOVED_2)

    text = env.call("merge-roles", _MERGE_ARGS)

    assert text.startswith("error: ")
    assert "borne by the absorbed role alone" in text
    assert approval_row_count(env.pending) == 0


@pytest.mark.parametrize("sub", [_OWNER, _ADMIN, "plain_user"])
def test_a_caller_without_the_compliance_officer_grant_gets_no_preview_and_no_approval(
    env: _Env, sub: str
) -> None:
    before = env.graph_state()

    text = env.call("merge-roles", _MERGE_ARGS, sub=sub)

    assert text == "error: You do not have the required access role for this action."
    assert approval_row_count(env.pending) == 0
    assert env.graph_state() == before


# -- a Role near-miss review resolved by the merge -------------------------------------

_REVIEW = "review_role_1"


def _seed_review(env: _Env, *, kind: str = "Role", incoming: str = ROLE_ABSORBED) -> None:
    env.rig.seed_near_miss_review(
        _REVIEW, incoming_id=incoming, existing_id=ROLE_SURVIVOR, kind=kind
    )


def test_a_review_naming_the_pair_gives_the_usual_preview_and_signs_the_review_id(
    env: _Env,
) -> None:
    _seed_review(env)
    before = env.graph_state()

    body = json.loads(env.call("merge-roles", {**_MERGE_ARGS, "review_id": _REVIEW}))

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.normalized_args == {
        "survivor_id": ROLE_SURVIVOR,
        "absorbed_id": ROLE_ABSORBED,
        "state_digest": body["preview"]["state_digest"],
        "review_id": _REVIEW,
    }
    assert approval_row_count(env.pending) == 1
    assert env.graph_state() == before
    assert env.rig.logged_groups == []


def _no_review(_env: _Env) -> None:
    """Seed nothing: the review id names no review."""


def _capability_review(env: _Env) -> None:
    _seed_review(env, kind="Capability")


def _review_of_another_pair(env: _Env) -> None:
    _seed_review(env, incoming="role_other")


@pytest.mark.parametrize(
    ("setup", "fragment"),
    [
        (_no_review, "does not exist or was already resolved"),
        (_capability_review, "not a Role review"),
        (_review_of_another_pair, "different pair"),
    ],
    ids=["no-review", "capability-review", "other-pair"],
)
def test_a_review_that_does_not_fit_the_pair_is_rejected_with_the_reason_and_no_approval(
    env: _Env, setup: Callable[[_Env], None], fragment: str
) -> None:
    setup(env)
    before = env.graph_state()

    text = env.call("merge-roles", {**_MERGE_ARGS, "review_id": _REVIEW})

    assert text.startswith("error: ")
    assert fragment in text
    assert approval_row_count(env.pending) == 0
    assert env.graph_state() == before


# -- duplicate Obligation pairs in the preview -----------------------------------------


def test_the_preview_carries_an_empty_pair_list_when_no_texts_look_alike(env: _Env) -> None:
    body = json.loads(env.call("merge-roles", _MERGE_ARGS))

    assert body["preview"]["duplicate_obligation_pairs"] == []


def test_the_preview_lists_the_obligation_pairs_the_merge_would_put_under_one_role(
    env: _Env,
) -> None:
    text = "Notify the authority of any incident"
    for obligation_id in (OBL_OF_SURVIVOR, OBL_MOVED_2):
        env.rig.graph.nodes[("Obligation", obligation_id)]["text"] = text
    before = env.graph_state()

    body = json.loads(env.call("merge-roles", _MERGE_ARGS))

    assert body["preview"]["duplicate_obligation_pairs"] == [
        {
            "survivor_obligation_id": OBL_OF_SURVIVOR,
            "survivor_text": text,
            "absorbed_obligation_id": OBL_MOVED_2,
            "absorbed_text": text,
            "basis": "identical_text",
        }
    ]
    assert env.graph_state() == before


# -- failures return a fixed message that leaks nothing ---------------------------------

_LEAKS = ("10.0.0.1", "6379", "refused", "relation", "Traceback", "hunter2", "/etc/")
_GRAPH_UNAVAILABLE = "error: the policy graph database is not reachable"
_UNEXPECTED = "error: an unexpected error occurred"


class _Unreachable:
    """A graph opener that fails the way a dead database does."""

    def __call__(self, _config: object) -> object:
        message = "connection refused to 10.0.0.1:6379 relation x password=hunter2"
        raise ConnectionError(message)


class _RedisDown:
    """A read handle whose every query hits a dead FalkorDB."""

    def query(self, *_args: object, **_kwargs: object) -> object:
        message = "Connection refused 10.0.0.1:6379 relation x"
        raise redis.exceptions.ConnectionError(message)


class _Garbage:
    """A read handle that answers with rows of the wrong shape (an unexpected failure)."""

    def query(self, *_args: object, **_kwargs: object) -> object:
        return type("Result", (), {"result_set": [["only one column"]]})()


class _DownApprovalStore(FakeApprovalStore):
    """An approval store that cannot reach Postgres."""

    def create_pending_approval(self, **_kwargs: object) -> object:  # type: ignore[override]
        message = "connection to 10.0.0.1:6379 refused: relation pending_approvals /etc/secret"
        raise PendingApprovalPersistenceError(message)


def _open_dead_redis(_config: object) -> object:
    return _RedisDown()


def _open_garbage(_config: object) -> object:
    return _Garbage()


@pytest.mark.parametrize(
    ("opener", "expected"),
    [
        (_Unreachable(), _GRAPH_UNAVAILABLE),
        (_open_dead_redis, _GRAPH_UNAVAILABLE),
        (_open_garbage, _UNEXPECTED),
    ],
    ids=["graph_unreachable", "graph_dies_on_read", "unexpected_failure"],
)
def test_a_graph_or_unexpected_failure_returns_a_fixed_message_and_creates_no_approval(
    env: _Env, monkeypatch: pytest.MonkeyPatch, opener: Callable[..., object], expected: str
) -> None:
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(opener),
    )
    before = env.graph_state()

    text = env.call("merge-roles", _MERGE_ARGS)

    assert text == expected
    assert not any(leak in text for leak in _LEAKS)
    assert approval_row_count(env.pending) == 0
    assert env.graph_state() == before
    assert env.rig.logged_groups == []


def test_an_unreachable_approval_store_returns_a_fixed_message_and_leaves_no_trace(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = _DownApprovalStore()
    monkeypatch.setattr(tool_core, "PsycopgPendingApprovalStore", _factory(down))
    before = env.graph_state()

    text = env.call("merge-roles", _MERGE_ARGS)

    assert text == _UNEXPECTED
    assert approval_row_count(down) == 0
    assert env.graph_state() == before
    assert env.rig.logged_groups == []
