"""`postgres_live` tests: every audit writer can run at once without deadlock or a gap.

The chain trigger holds the head row lock until the writer's transaction ends. That is safe only
because every `AuditStore.record` call is the last statement before its commit, so the head is
the last lock any transaction takes. These tests hammer that claim with the real callers: six
in-transaction paths (grant, revoke of an ordinary role, revoke of `SystemOwner` with a second
owner present, runtime-config set and reset, ingestion run create and complete) plus the two
standalone writers, all at the same time on one database. A separate test races the two
bootstrap paths on an empty authorisation table. No call may fail at all (a `40P01` deadlock or
a `55P03` lock timeout would surface as one), and afterwards the independent oracle must find
one intact chain whose length equals the row count.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from typing import TYPE_CHECKING

import pytest
from persistence.provisioned_postgres import (
    Provisioned,
    migrate_state_database,
    provision_graph_log,
)

import ps_service.authz.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import, registers access_role.* actions
from audit._integrity_support import ISSUER, assert_chain_is_intact, register_test_key
from ps_service.audit import PsycopgAuditStore
from ps_service.authz.models import AccessRole
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.ingestion_runs import PsycopgIngestionRunStore
from ps_service.runtime_config import PsycopgRuntimeConfigStore

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.postgres_live

_RUN_SECONDS = 4.0
_JOIN_TIMEOUT_SECONDS = 30.0
_ACTOR = ("concurrency-actor", ISSUER)
_RESULT: dict[str, object] = {"summary": "ok"}


@pytest.fixture
def prov(fresh_provisioned: Provisioned) -> Provisioned:
    """A fully provisioned cluster with an empty audit chain."""
    migrate_state_database(fresh_provisioned)
    provision_graph_log(fresh_provisioned)
    return fresh_provisioned


def _principal(label: str) -> tuple[str, str]:
    return (f"{label}-{uuid.uuid4().hex[:10]}", ISSUER)


def _run_workers(
    workers: dict[str, Callable[[], None]], *, copies: int = 2
) -> tuple[dict[str, int], list[BaseException]]:
    """Run `copies` threads per worker for `_RUN_SECONDS`, return calls per worker and errors."""
    stop = threading.Event()
    errors: list[BaseException] = []
    counts: dict[str, int] = dict.fromkeys(workers, 0)
    lock = threading.Lock()

    def loop(name: str, body: Callable[[], None]) -> None:
        while not stop.is_set():
            try:
                body()
            except BaseException as exc:  # noqa: BLE001  # collect every failure for the assertion
                with lock:
                    errors.append(exc)
                return
            with lock:
                counts[name] += 1

    threads = [
        threading.Thread(target=loop, args=(name, body), daemon=True)
        for name, body in workers.items()
        for _ in range(copies)
    ]
    for thread in threads:
        thread.start()
    time.sleep(_RUN_SECONDS)
    stop.set()
    deadline = time.monotonic() + _JOIN_TIMEOUT_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads), "a writer thread did not finish"
    return counts, errors


def test_all_in_transaction_paths_and_both_standalone_writers_run_at_once(
    prov: Provisioned,
) -> None:
    config = prov.state_config()
    audit_store = PsycopgAuditStore(config)
    roles = PsycopgAccessRoleStore(config, audit_store=audit_store)
    runtime_config = PsycopgRuntimeConfigStore(config, audit_store=audit_store)
    runs = PsycopgIngestionRunStore(config, audit_store=audit_store)
    system_audit = build_system_audit_recorder(config)
    key = register_test_key()
    keeper = _principal("keeper")
    roles.grant(actor=_ACTOR, target=keeper, access_role=AccessRole.SYSTEM_OWNER)

    def grant() -> None:
        roles.grant(actor=_ACTOR, target=_principal("g"), access_role=AccessRole.SYSTEM_ADMIN)

    def revoke_ordinary() -> None:
        target = _principal("r")
        roles.grant(actor=_ACTOR, target=target, access_role=AccessRole.POLICY_MANAGER)
        roles.revoke(actor=_ACTOR, target=target, access_role=AccessRole.POLICY_MANAGER)

    def revoke_system_owner() -> None:
        target = _principal("o")
        roles.grant(actor=_ACTOR, target=target, access_role=AccessRole.SYSTEM_OWNER)
        roles.revoke(actor=_ACTOR, target=target, access_role=AccessRole.SYSTEM_OWNER)

    def config_set_and_reset() -> None:
        runtime_config.set(key, uuid.uuid4().hex, actor=_ACTOR)
        runtime_config.reset(key, actor=_ACTOR)

    def run_create() -> None:
        runs.create_run(
            run_id=str(uuid.uuid4()), celex="32024R2847", short_name="cra", actor=_ACTOR
        )

    def run_create_and_complete() -> None:
        run_id = str(uuid.uuid4())
        runs.create_run(run_id=run_id, celex="32024R2847", short_name="cra", actor=_ACTOR)
        runs.complete_run(run_id, status="succeeded", result=_RESULT, error=None)

    def standalone() -> None:
        audit_store.record_standalone(
            actor_subject=_ACTOR[0],
            actor_issuer=_ACTOR[1],
            action="access_role.grant",
            resource_type="principal",
            resource_id=uuid.uuid4().hex,
            outcome="applied",
            details={"access_role": "SystemAdmin"},
        )

    def system() -> None:
        system_audit(action="ingestion.system", resource_type="run", resource_id="s", details={})

    counts, errors = _run_workers(
        {
            "grant": grant,
            "revoke_ordinary": revoke_ordinary,
            "revoke_system_owner": revoke_system_owner,
            "config": config_set_and_reset,
            "run_create": run_create,
            "run_create_and_complete": run_create_and_complete,
            "standalone": standalone,
            "system": system,
        }
    )

    assert errors == []
    assert all(count > 0 for count in counts.values()), counts
    with prov.superuser_connect(prov.state_db) as conn:
        total = conn.execute("SELECT count(*) FROM audit_log.audit_events").fetchone()
    assert total is not None
    assert assert_chain_is_intact(prov) == total[0]
    assert int(str(total[0])) > 8


def test_concurrent_bootstrap_attempts_on_an_empty_store_never_deadlock(prov: Provisioned) -> None:
    owner = _principal("first-owner")
    config = dataclasses.replace(
        prov.state_config(),
        authz_bootstrap_owner_subject=owner[0],
        authz_bootstrap_owner_issuer=owner[1],
    )
    roles = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    start = threading.Barrier(8)
    errors: list[BaseException] = []

    def attempt(principal: tuple[str, str]) -> None:
        try:
            start.wait(timeout=10)
            roles.bootstrap_first_owner(principal)
        except BaseException as exc:  # noqa: BLE001  # collect every failure for the assertion
            errors.append(exc)

    principals = [owner] + [_principal(f"stranger{n}") for n in range(7)]
    threads = [threading.Thread(target=attempt, args=(p,), daemon=True) for p in principals]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_JOIN_TIMEOUT_SECONDS)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    with prov.superuser_connect(prov.state_db) as conn:
        actions = [
            str(row[0])
            for row in conn.execute(
                "SELECT action FROM audit_log.audit_events ORDER BY chain_seq"
            ).fetchall()
        ]
    assert actions.count("access_role.bootstrap") == 1
    assert set(actions) <= {"access_role.bootstrap", "access_role.bootstrap_rejected"}
    assert assert_chain_is_intact(prov) == len(actions)
