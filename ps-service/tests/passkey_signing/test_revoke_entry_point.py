"""The revoke command: one JSON line on stdout, exit 0 or 1, usage errors exit 2.

The command runs in the PS Service pod and is read by the deploy scripts, so its stdout is a
contract: exactly one compact JSON line, never the subject, the issuer, a host or a driver text.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore

from passkey_signing._fakes import (
    FakePendingApprovalStore,
    FakeSigningCredentialStore,
    FakeSigningRevocationStore,
)
from ps_service.audit.errors import AuditPersistenceError
from ps_service.passkey_signing.revoke import RevokeCollaborators, main

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type ReadLines = Callable[[Path], list[dict[str, object]]]
    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]

SUBJECT = "owner@example.com"
ISSUER = "https://auth.example.test/application/o/ps/"
ARGS = ["--subject", SUBJECT, "--issuer", ISSUER]
SIGNING_ENV = (
    "PS_PASSKEYSIGNING_POSTGRES_HOST",
    "PS_PASSKEYSIGNING_POSTGRES_DATABASE",
    "PS_PASSKEYSIGNING_POSTGRES_USER",
    "PS_PASSKEYSIGNING_POSTGRES_PASSWORD",
)
DATABASE_ENV_PREFIXES = ("PS_PASSKEYSIGNING_POSTGRES_", "PS_STATE_POSTGRES_")


def _store(credentials: int = 2) -> FakeSigningRevocationStore:
    store = FakeSigningRevocationStore(credentials=FakeSigningCredentialStore())
    for index in range(credentials):
        store.credentials.create_signing_credential(
            actor_subject=SUBJECT,
            actor_issuer=ISSUER,
            credential_id=f"cred-{index}".encode(),
            public_key=b"cose-key",
            sign_count=0,
            rp_id="ps.example.test",
        )
    return store


def _stdout_line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert out.count("\n") == 1, out
    assert SUBJECT not in out
    assert ISSUER not in out
    parsed: dict[str, object] = json.loads(out)
    assert out == json.dumps(parsed, separators=(",", ":")) + "\n"
    return parsed


def test_revoke_prints_exactly_one_json_line_with_count_and_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(credentials=2)
    approval, _code = store.pending.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={},
        actor_subject=SUBJECT,
        actor_issuer=ISSUER,
        display_summary={},
    )
    audit = InMemoryAuditStore()

    code = main(ARGS, collaborators=RevokeCollaborators(store, audit))

    assert code == 0
    assert _stdout_line(capsys) == {
        "status": "revoked",
        "credentials_deleted": 2,
        "voided_approval_ids": [approval.id],
    }
    assert store.credentials.all_rows() == ()
    assert [row.action for row in audit.rows] == ["passkey.revoke"]


def test_count_only_reads_counts_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    store = _store(credentials=2)
    pending = FakePendingApprovalStore()
    pending.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={},
        actor_subject=SUBJECT,
        actor_issuer=ISSUER,
        display_summary={},
    )
    store.pending = pending
    audit = InMemoryAuditStore()

    code = main([*ARGS, "--count-only"], collaborators=RevokeCollaborators(store, audit))

    assert code == 0
    assert _stdout_line(capsys) == {"status": "counted", "credentials": 2, "pending_approvals": 1}
    assert len(store.credentials.all_rows()) == 2
    assert audit.rows == []


def _unconfigured(monkeypatch: pytest.MonkeyPatch) -> RevokeCollaborators | None:
    for name in SIGNING_ENV:
        monkeypatch.delenv(name, raising=False)
    return None


def _invalid_port(monkeypatch: pytest.MonkeyPatch) -> RevokeCollaborators | None:
    monkeypatch.setenv("PS_PASSKEYSIGNING_POSTGRES_PORT", "not-a-port")
    return None


def _store_down(_: pytest.MonkeyPatch) -> RevokeCollaborators | None:
    store = _store()
    store.fail_before_audit = True
    return RevokeCollaborators(store, InMemoryAuditStore())


def _commit_failed(_: pytest.MonkeyPatch) -> RevokeCollaborators | None:
    store = _store()
    store.fail_commit = True
    return RevokeCollaborators(store, InMemoryAuditStore())


def _audit_down(_: pytest.MonkeyPatch) -> RevokeCollaborators | None:
    audit = InMemoryAuditStore(fail_on_outcome={"applied": AuditPersistenceError("db.internal")})
    return RevokeCollaborators(_store(), audit)


@pytest.mark.parametrize(
    ("arrange", "reason_code"),
    [
        pytest.param(_unconfigured, "configuration_invalid", id="configuration_invalid"),
        pytest.param(_invalid_port, "configuration_invalid", id="configuration_malformed"),
        pytest.param(_store_down, "signing_store_unavailable", id="signing_store_unavailable"),
        pytest.param(_commit_failed, "signing_store_unavailable", id="commit_failed"),
        pytest.param(_audit_down, "audit_trail_unavailable", id="audit_trail_unavailable"),
    ],
)
def test_entry_point_failures_print_a_reason_code_and_never_the_subject(
    arrange: Callable[[pytest.MonkeyPatch], RevokeCollaborators | None],
    reason_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    collaborators = arrange(monkeypatch)

    code = main(ARGS, collaborators=collaborators)

    assert code == 1
    assert _stdout_line(capsys) == {"status": "failed", "reason_code": reason_code}


def test_a_failed_count_prints_the_store_reason(capsys: pytest.CaptureFixture[str]) -> None:
    store = _store()
    store.fail_read = True

    code = main(
        [*ARGS, "--count-only"], collaborators=RevokeCollaborators(store, InMemoryAuditStore())
    )

    assert code == 1
    assert _stdout_line(capsys) == {"status": "failed", "reason_code": "signing_store_unavailable"}


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--subject", "  ", "--issuer", ISSUER], id="blank-subject"),
        pytest.param(["--subject", SUBJECT, "--issuer", ""], id="blank-issuer"),
        pytest.param(["--subject", SUBJECT], id="missing-issuer"),
    ],
)
def test_blank_subject_or_issuer_is_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store()

    with pytest.raises(SystemExit) as exit_info:
        main(argv, collaborators=RevokeCollaborators(store, InMemoryAuditStore()))

    assert exit_info.value.code == 2
    assert capsys.readouterr().out == ""
    assert len(store.credentials.all_rows()) == 2


def test_the_log_entry_carries_counts_and_never_the_subject_or_issuer(
    make_emitter: MakeEmitter, read_lines: ReadLines, capsys: pytest.CaptureFixture[str]
) -> None:
    emitter, log_path = make_emitter()

    code = main(
        ARGS, collaborators=RevokeCollaborators(_store(2), InMemoryAuditStore(), emitter=emitter)
    )

    assert code == 0
    capsys.readouterr()
    emitter.flush()
    entries = [line for line in read_lines(log_path) if line.get("action") == "passkey_revoke"]
    assert len(entries) == 1
    (entry,) = entries
    assert entry["component"] == "passkey_signing"
    assert entry["outcome"] == "succeeded"
    assert entry["mode"] == "revoke"
    assert entry["credentials_deleted"] == 2
    assert entry["approvals_voided"] == 0
    assert SUBJECT not in json.dumps(entry)
    assert ISSUER not in json.dumps(entry)


def _closed_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _no_database_env() -> dict[str, str]:
    return {}


def _unreachable_database_env() -> dict[str, str]:
    port = str(_closed_local_port())
    env: dict[str, str] = {}
    for prefix in DATABASE_ENV_PREFIXES:
        env |= {
            f"{prefix}HOST": "127.0.0.1",
            f"{prefix}PORT": port,
            f"{prefix}DATABASE": "ps",
            f"{prefix}USER": "ps",
            f"{prefix}PASSWORD": "example-password",
            f"{prefix}CONNECT_TIMEOUT_SECONDS": "2",
        }
    return env


@pytest.mark.parametrize(
    ("database_env", "extra_args", "mode", "reason_code"),
    [
        pytest.param(_no_database_env, [], "revoke", "configuration_invalid", id="unconfigured"),
        pytest.param(
            _unreachable_database_env,
            ["--count-only"],
            "count",
            "signing_store_unavailable",
            id="store_unreachable",
        ),
    ],
)
def test_the_module_run_logs_to_the_service_log_and_stderr_and_keeps_stdout_one_line(
    database_env: Callable[[], dict[str, str]],
    extra_args: list[str],
    mode: str,
    reason_code: str,
    tmp_path: Path,
    read_lines: ReadLines,
) -> None:
    log_dir = tmp_path / "pod-logs"
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(DATABASE_ENV_PREFIXES)
    }
    environ |= {"PS_LOGGING_DIR": str(log_dir), **database_env()}

    completed = subprocess.run(  # noqa: S603 -- fixed interpreter and module, literal arguments
        [sys.executable, "-m", "ps_service.passkey_signing.revoke", *ARGS, *extra_args],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.count("\n") == 1, completed.stdout
    assert json.loads(completed.stdout) == {"status": "failed", "reason_code": reason_code}
    entries = [
        line
        for line in read_lines(log_dir / "ps-service.jsonl")
        if line.get("action") == "passkey_revoke"
    ]
    assert [(entry["component"], entry["outcome"], entry["mode"]) for entry in entries] == [
        ("passkey_signing", "failed", mode)
    ]
    mirrored = [
        json.loads(line) for line in completed.stderr.splitlines() if '"passkey_revoke"' in line
    ]
    assert [(entry["action"], entry["outcome"]) for entry in mirrored] == [
        ("passkey_revoke", "failed")
    ]
    logged = completed.stdout + completed.stderr + (log_dir / "ps-service.jsonl").read_text()
    assert SUBJECT not in logged
    assert ISSUER not in logged
