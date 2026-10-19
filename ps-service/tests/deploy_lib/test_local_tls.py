"""Logic tests for `scripts/lib/local-tls.sh` with a fake `openssl`.

Real-cryptography checks (CA/leaf extensions, strict TLS handshake, idempotence, permissions)
run real `openssl` and a local TLS server, so they live in `test_local_tls_integration.py`
under the `integration` marker (L2 Testing Patterns). Here: tool preflight, the
two-Secret apply idiom (certificate and key for the proxy, CA certificate for PS Service) and
hostname validation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import LibHarness

LIB = "$PS_TEST_LIB_DIR"
PRELUDE = f"""
set -uo pipefail
EXIT_FAILURE=1
print_error() {{ local f="$1"; shift; printf "$f" "$@" >&2; }}
source "{LIB}/local-tls.sh"
"""


def test_missing_openssl_is_reported_with_an_install_hint(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(
        f'source "{LIB}/local-tls.sh"; print_error() {{ printf "$@" >&2; }}; require_tls_tools',
        path_only=str(lib_harness.bin_dir),
    )

    assert proc.returncode != 0
    assert "openssl" in proc.stderr
    assert "install" in proc.stderr.lower()


def test_tools_present_passes_the_preflight(lib_harness: LibHarness) -> None:
    lib_harness.install_fake_openssl(["x"])

    proc = lib_harness.run(PRELUDE + "require_tls_tools")

    assert proc.returncode == 0, proc.stderr


def test_secrets_are_applied_with_the_dry_run_pipe_and_no_key_material_on_stdout(
    lib_harness: LibHarness,
) -> None:
    state = lib_harness.root / "state"
    state.mkdir()
    for name in ("leaf.pem", "leaf.key", "ca.pem"):
        (state / name).write_text("PRIVATE-MATERIAL", encoding="utf-8")

    proc = lib_harness.run(
        PRELUDE + "apply_local_tls_secret policy-system ps-local-tls ps-local-ca"
    )

    assert proc.returncode == 0, proc.stderr
    calls = lib_harness.kubectl_calls()
    tls = next(c for c in calls if "create secret generic ps-local-tls" in c)
    ca = next(c for c in calls if "create secret generic ps-local-ca" in c)
    for call in (tls, ca):
        assert "--dry-run=client" in call
    assert "tls.crt=" in tls
    assert "tls.key=" in tls
    assert "ca.crt" not in tls
    assert "ca.crt=" in ca
    assert "tls.key" not in ca
    assert sum(1 for c in calls if re.search(r"\bapply\b", c)) == 2
    assert "PRIVATE-MATERIAL" not in proc.stdout + proc.stderr


def test_a_dns_name_is_accepted_as_the_evaluator_hostname(lib_harness: LibHarness) -> None:
    for name in ("ps.test", "ps", "my-eval.example.test", "PS.Test"):
        proc = lib_harness.run(PRELUDE + f'local_tls_validate_hostname "{name}"')

        assert proc.returncode == 0, f"{name}: {proc.stderr}"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("192.168.1.5", "IP address"),
        ("10.0.0.1", "IP address"),
        ("::1", "letters, digits"),
        ("fe80::1", "letters, digits"),
        ("ps.test\nIP:1.2.3.4", "letters, digits"),
        ("ps.test;DNS:evil.test", "letters, digits"),
        ("ps test", "letters, digits"),
        ("ps_test", "letters, digits"),
        ("-ps.test", "letters, digits"),
        ("ps..test", "letters, digits"),
        ("ps.test.", "letters, digits"),
    ],
)
def test_an_unsafe_or_ip_literal_hostname_is_rejected_with_a_fix(
    lib_harness: LibHarness, name: str, reason: str
) -> None:
    proc = lib_harness.run(
        PRELUDE + 'local_tls_validate_hostname "$PS_TEST_NAME"', PS_TEST_NAME=name
    )

    assert proc.returncode != 0
    assert reason in proc.stderr
    assert "Fix:" in proc.stderr


def test_a_rejected_hostname_writes_nothing_to_the_state_dir(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(PRELUDE + 'ensure_local_leaf "ps.test;DNS:evil.test"')

    assert proc.returncode != 0
    assert not (lib_harness.root / "state" / "ca.key").exists()
    assert not (lib_harness.root / "state" / "ca.cnf").exists()


def test_lib_never_disables_certificate_verification() -> None:
    text = (Path(__file__).resolve().parents[3] / "scripts/lib/local-tls.sh").read_text()

    assert not re.search(r"(^|\s)-k(\s|$)|--insecure|verify=False", text)
