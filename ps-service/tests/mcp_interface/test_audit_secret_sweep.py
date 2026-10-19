"""No new audit row or log line carries a secret, free-text error, trace or path.

Capstone: every new emitter (sync ingest, restore, near-miss resolve,
`check_regulations` re-ingest, invite, and the async run's completion builder) is driven once
through a FAILING path whose exception message and inputs are full of sentinel secrets. None of
the sentinels may appear in any recorded audit row or in any emitted log line.

The role-gate denial (`access.gate`) and `query.cypher` are swept here too, each with an audit
store that raises sentinel text and, for `cypher`, a database error full of sentinels. The
`passkey.enroll` sweep lives in `passkey_signing/test_enrollment_audit.py` because it needs that
directory's ceremony rig (the importlib collection order forbids importing it from here).

Sibling suites already pin each operation's row shape; this file only sweeps for leaks. It reuses
their private fixtures (the cross-package idiom of `test_ingest_regulation_audit.py`).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from api._audit_fakes import InMemoryAuditStore
from api._fakes import build_fake_pipeline_dependencies
from api.test_change_check_orchestration_audit import (
    PipelineStageError as SweepPipelineStageError,
)
from api.test_change_check_orchestration_audit import (
    _fake as _fake_sweep,  # pyright: ignore[reportPrivateUsage]  -- reuse the sweep fixtures verbatim
)
from api.test_change_check_orchestration_audit import (
    _node as _sweep_node,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from api.test_ingestion_orchestration_audit import (
    _run as _run_ingest,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from api.test_near_miss_review_orchestration import (
    _deps as _near_miss_deps,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from api.test_near_miss_review_orchestration import (
    _run as _run_near_miss,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from api.test_restore_orchestration_audit import (
    _Delegate,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _run_catalog,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from audit._fakes import InMemoryAuditStore as GateAuditStore
from audit._fakes import audit_store_factory
from authz._fakes import FakeAccessRoleStore

from mcp_interface.test_cypher_tool import (
    _FakeGraphHandle,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _install_graph,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.api.change_check_orchestration import run_change_check_sweep
from ps_service.api.errors import AccessDeniedError, RestoreStageFailedError
from ps_service.audit import AuditContext, AuditPersistenceError
from ps_service.authz.models import AccessRole
from ps_service.authz.request_gate import RoleGate, enforce_role_gate
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.ingestion_runs.audit_actions import completion_audit_entry
from ps_service.invitations.errors import AuthentikInvitationError
from ps_service.invitations.service import invite_user_audited
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from api._fakes import MakeEmitter, ReadLines


_SENTINELS = (
    "SECRET-PK",
    "Bearer abc",
    "https://u:p@host/",
    "/Users/x/y.py",
    "Traceback",
    "itoken=SECRET",
)
_LEAK = " | ".join(_SENTINELS)
_ACTOR = ("actor-sub", "https://issuer.example.com/")


_NEW_LOG_ACTIONS = frozenset({"resolve_near_miss", "change_check_sweep", "change_check_instrument"})


def _assert_clean(store: InMemoryAuditStore, lines: list[dict[str, object]]) -> None:
    """No sentinel in any audit row or in a log line emitted by this issue's code.

    Pre-existing operational logs (e.g. the restore stage failure entry, which logs the stage
    exception for operators) are out of scope: this sweep concerns the audit rows and what the
    new audit emission logs.
    """
    new_lines = [
        line
        for line in lines
        if str(line.get("action", "")).startswith("audit_")
        or line.get("action") in _NEW_LOG_ACTIONS
    ]
    haystack = json.dumps(
        [(r.action, r.outcome, r.resource_id, r.details) for r in store.rows] + new_lines,
        default=str,
    )
    assert store.rows, "the scenario must have written at least the opening row"
    for sentinel in _SENTINELS:
        assert sentinel not in haystack, sentinel


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
        authentik_api_token="Bearer abc",
        authentik_base_url="https://u:p@host/",
    )


def test_sync_ingest_failure_rows_and_logs_contain_no_secret(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore(fail_on_outcome={"failed": AuditPersistenceError(_LEAK)})
    deps = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError(_LEAK)
    ).dependencies

    with pytest.raises(Exception, match=r".+"):
        _run_ingest(store, deps, emitter=emitter)
    emitter.flush()

    _assert_clean(store, read_lines(log_path))


def test_restore_failure_rows_and_logs_contain_no_secret(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore(fail_on_outcome={"failed": AuditPersistenceError(_LEAK)})
    delegate = _Delegate(store, error=RestoreStageFailedError(stage="staging", reason=_LEAK))

    with pytest.raises(RestoreStageFailedError):
        _run_catalog(store, delegate, emitter=emitter)
    emitter.flush()

    _assert_clean(store, read_lines(log_path))


def test_near_miss_failure_rows_contain_no_secret() -> None:
    store = InMemoryAuditStore()

    def _boom(_review_id: str, _decision: str) -> None:
        raise CompanyMergePersistenceError(_LEAK)

    with pytest.raises(CompanyMergePersistenceError):
        _run_near_miss(store, _near_miss_deps(store, resolve=_boom))  # pyright: ignore[reportArgumentType]

    _assert_clean(store, [])


def test_sweep_reingest_failure_rows_and_logs_contain_no_secret(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore()
    fake = _fake_sweep(results=[SweepPipelineStageError(_LEAK)], tracked=(_sweep_node("CRA-1"),))

    result = run_change_check_sweep(
        config=_config(),
        run_id="sweep-run",
        dependencies=fake.dependencies,
        audit=AuditContext(_ACTOR, store),
        emitter=emitter,
    )
    emitter.flush()

    # `reingest_failed` detail is the sweep's own scrubbed REST/MCP response, not an audit row.
    assert [o.outcome for o in result.instruments] == ["reingest_failed"]
    _assert_clean(store, read_lines(log_path))


def test_invite_failure_rows_and_logs_contain_no_secret(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore()

    def _send(_config: ServiceConfig, _email: str) -> object:
        raise AuthentikInvitationError(_LEAK, reason_code="upstream_unreachable")

    with pytest.raises(AuthentikInvitationError):
        invite_user_audited(
            _config(),
            "target@example.com",
            audit=AuditContext(_ACTOR, store),
            send_invitation=_send,  # pyright: ignore[reportArgumentType]
            emitter=emitter,
        )
    emitter.flush()

    _assert_clean(store, read_lines(log_path))


def test_async_completion_entry_cannot_carry_text_from_a_result_or_failure() -> None:
    """The async run's builder has no field able to hold the stage error text."""
    result: dict[str, object] = {
        "error": _LEAK,
        "stages": [{"stage": "extract", "summary": {"error": _LEAK}, "detail": _LEAK}],
    }

    failed = completion_audit_entry(
        status="failed",
        celex="32024R2847",
        trigger="async_ingest",
        result=result,
        reason_code="pipeline_stage_failed",
    )

    for sentinel in _SENTINELS:
        assert sentinel not in json.dumps(failed.details)


def _failure_log_text(logged: list[dict[str, object]]) -> str:
    return json.dumps(logged, default=str)


def _capture_audit_failure_logs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record every entry the audit emit helpers log (their failure entries)."""
    logged: list[dict[str, object]] = []

    def _record(**kwargs: object) -> None:
        logged.append(kwargs)

    # detroit-exception: capturing the log call's args IS the spec here
    monkeypatch.setattr("ps_service.audit.emit.emit_log_entry", _record)
    return logged


def test_access_gate_denial_with_a_leaking_audit_store_logs_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged = _capture_audit_failure_logs(monkeypatch)
    store = GateAuditStore(fail_on_action={"access.gate": AuditPersistenceError(_LEAK)})
    roles = FakeAccessRoleStore(expected_owner=_ACTOR)
    roles.bootstrap_first_owner(_ACTOR)

    with pytest.raises(AccessDeniedError) as denied:
        enforce_role_gate(
            RoleGate(operation="set-catalog-source", minimum=AccessRole.COMPLIANCE_OFFICER),
            principal=("someone", _ACTOR[1]),
            audit_actor=("someone", _ACTOR[1]),
            role_store=roles,
            audit_store=store,
        )

    assert logged, "the failed audit write must be logged"
    assert store.rows == []
    for sentinel in _SENTINELS:
        assert sentinel not in _failure_log_text(logged), sentinel
        assert sentinel not in str(denied.value), sentinel


def test_access_gate_row_for_a_denial_carries_no_secret_beyond_the_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_audit_failure_logs(monkeypatch)
    store = GateAuditStore()
    roles = FakeAccessRoleStore(expected_owner=_ACTOR)
    roles.bootstrap_first_owner(_ACTOR)

    with pytest.raises(AccessDeniedError):
        enforce_role_gate(
            RoleGate(operation="set-catalog-source", minimum=AccessRole.SYSTEM_ADMIN),
            principal=("someone", _ACTOR[1]),
            audit_actor=("someone", _ACTOR[1]),
            role_store=roles,
            audit_store=store,
        )

    (row,) = store.rows
    assert row.details == {"required_role": "SystemAdmin"}


@pytest.mark.parametrize("failing", ["database", "audit_store"])
def test_cypher_failure_rows_and_logs_contain_no_secret(
    failing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    logged = _capture_audit_failure_logs(monkeypatch)
    store = GateAuditStore(
        fail_on_action={"query.cypher": AuditPersistenceError(_LEAK)}
        if failing == "audit_store"
        else {}
    )
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    _install_graph(monkeypatch, _FakeGraphHandle(error=RuntimeError(_LEAK)))

    asyncio.run(mcp_server.server.call_tool("cypher", {"query": f"MATCH (n) RETURN n // {_LEAK}"}))

    if failing == "database":
        (row,) = store.rows
        assert (row.outcome, row.details) == ("failed", {"reason_code": "query_failed"})
    else:
        assert logged, "the failed audit write must be logged"
    haystack = json.dumps([(r.action, r.details) for r in store.rows], default=str)
    haystack += _failure_log_text(logged)
    for sentinel in _SENTINELS:
        assert sentinel not in haystack, sentinel
