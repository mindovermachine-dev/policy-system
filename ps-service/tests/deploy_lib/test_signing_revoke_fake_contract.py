"""The fake signing revoke command prints the same JSON line shape as the real command.

The deploy-script harnesses answer `kubectl exec ... python -m ps_service.passkey_signing.revoke`
with `fixtures/fakes/ps_signing_revoke`. This runs that fake and the real command (on in-memory
stores) for each outcome and compares the keys and the status, so the fake cannot drift from the
contract the deploy library parses.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore
from passkey_signing._fakes import (
    FakePendingApprovalStore,
    FakeSigningCredentialStore,
    FakeSigningRevocationStore,
)

from ps_service.passkey_signing.revoke import RevokeCollaborators, main

if TYPE_CHECKING:
    from collections.abc import Callable

FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "fakes" / "ps_signing_revoke"
SUBJECT = "owner@example.com"
ISSUER = "https://auth.example.test/application/o/ps/"
COMMAND = ["--subject", SUBJECT, "--issuer", ISSUER]


def _real_store(mode: str) -> tuple[FakeSigningRevocationStore, str]:
    store = FakeSigningRevocationStore(
        credentials=FakeSigningCredentialStore(), pending=FakePendingApprovalStore()
    )
    approval, _code = store.pending.create_pending_approval(
        tool_name="merge-capabilities",
        normalized_args={},
        actor_subject=SUBJECT,
        actor_issuer=ISSUER,
        display_summary={},
    )
    store.credentials.create_signing_credential(
        actor_subject=SUBJECT,
        actor_issuer=ISSUER,
        credential_id=b"cred",
        public_key=b"key",
        sign_count=0,
        rp_id="ps.example.test",
    )
    store.fail_before_audit = mode == "failed"
    return store, approval.id


def _fake_state(mode: str, approval_id: str) -> dict[str, object]:
    state: dict[str, object] = {
        "actors": {f"{SUBJECT}\n{ISSUER}": {"credentials": 1, "pending": [approval_id]}}
    }
    if mode == "failed":
        state["fail"] = "revoke"
    return state


def _args(mode: str) -> list[str]:
    return [*COMMAND, "--count-only"] if mode == "count" else COMMAND


def _run_fake(mode: str, approval_id: str, tmp_path: Path) -> tuple[int, dict[str, object]]:
    state = tmp_path / "signing-state.json"
    state.write_text(json.dumps(_fake_state(mode, approval_id)), encoding="utf-8")
    argv = [
        "exec",
        "deploy/policy-system-ps-service",
        "-c",
        "ps-service",
        "--",
        "python",
        "-m",
        "ps_service.passkey_signing.revoke",
        *_args(mode),
    ]
    proc = subprocess.run(  # noqa: S603 - the repo's own fake, fixed argv
        [sys.executable, str(FAKE), *argv],
        capture_output=True,
        text=True,
        check=False,
        env={"PS_TEST_SIGNING_STATE": str(state)},
        timeout=30,
    )
    assert proc.stdout.count("\n") == 1, proc.stdout
    parsed: dict[str, object] = json.loads(proc.stdout)
    return proc.returncode, parsed


def _run_real(
    store: FakeSigningRevocationStore, mode: str, read_stdout: Callable[[], str]
) -> tuple[int, dict[str, object]]:
    code = main(_args(mode), collaborators=RevokeCollaborators(store, InMemoryAuditStore()))
    out = read_stdout()
    assert out.count("\n") == 1, out
    parsed: dict[str, object] = json.loads(out)
    return code, parsed


@pytest.mark.parametrize("mode", ["revoke", "count", "failed"])
def test_fake_and_real_entry_point_emit_the_same_keys(
    mode: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, approval_id = _real_store(mode)
    fake_code, fake = _run_fake(mode, approval_id, tmp_path)
    real_code, real = _run_real(store, mode, lambda: capsys.readouterr().out)

    assert fake_code == real_code
    assert set(fake) == set(real)
    assert fake["status"] == real["status"]
    assert fake == real
