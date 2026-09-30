"""Real-`openssl`, real-TLS-handshake tests for `scripts/lib/local-tls.sh` (issue #165, 3C).

Marked `integration`: they spawn real `openssl` and bind a local TLS server socket (L2 Testing
Patterns). They prove what the fake-`openssl` logic tests cannot: the CA/leaf extensions that
strict verifiers require (Python 3.13+/browsers need SKI and AKI: AC-BI-007), a strict-verify
handshake on the running interpreter, idempotence, host change, near-expiry renewal and file
modes.
"""

from __future__ import annotations

import socket
import ssl
import stat
import subprocess
import threading
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import LibHarness

pytestmark = pytest.mark.integration

LIB = "$PS_TEST_LIB_DIR"
PRELUDE = f"""
set -uo pipefail
print_error() {{ local f="$1"; shift; printf "$f" "$@" >&2; }}
source "{LIB}/local-tls.sh"
"""
_OPENSSL_TIMEOUT_SECONDS = 30


def _ensure(harness: LibHarness, host: str = "authentik.local", **env: str) -> None:
    proc = harness.run(PRELUDE + f'ensure_local_leaf "{host}"', **env)
    assert proc.returncode == 0, proc.stderr


def _text(path: Path) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv, test-owned file
        ["openssl", "x509", "-in", str(path), "-noout", "-text"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=_OPENSSL_TIMEOUT_SECONDS,
    )
    return out.stdout


def _state(harness: LibHarness) -> Path:
    return harness.root / "state"


def test_ca_is_a_ca_with_subject_key_identifier(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)

    text = _text(_state(lib_harness) / "ca.pem")
    assert "CA:TRUE" in text
    assert "Subject Key Identifier" in text
    assert "Certificate Sign" in text


def test_leaf_has_ski_aki_san_and_server_auth_and_verifies_against_the_ca(
    lib_harness: LibHarness,
) -> None:
    _ensure(lib_harness)

    state = _state(lib_harness)
    text = _text(state / "leaf.pem")
    assert "Subject Key Identifier" in text
    assert "Authority Key Identifier" in text
    assert "DNS:authentik.local" in text
    assert "TLS Web Server Authentication" in text
    assert "CA:FALSE" in text
    verify = subprocess.run(  # noqa: S603 - fixed argv, test-owned files
        ["openssl", "verify", "-CAfile", str(state / "ca.pem"), str(state / "leaf.pem")],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=_OPENSSL_TIMEOUT_SECONDS,
    )
    assert verify.returncode == 0, verify.stderr


class _TlsServer:
    """One-shot local TLS server presenting the leaf; accepts connections until closed."""

    def __init__(self, cert: Path, key: Path) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port: int = self._sock.getsockname()[1]
        self._context = context
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                with self._context.wrap_socket(conn, server_side=True):
                    pass
            except ssl.SSLError, OSError:
                continue

    def close(self) -> None:
        self._stop = True
        self._sock.close()
        self._thread.join(timeout=5)


def test_strict_default_client_verifies_the_leaf_with_only_the_local_ca(
    lib_harness: LibHarness,
) -> None:
    """Python's default context is strict (X509_STRICT): passes only with SKI/AKI present."""
    _ensure(lib_harness)
    state = _state(lib_harness)
    server = _TlsServer(state / "leaf.pem", state / "leaf.key")
    try:
        context = ssl.create_default_context(cafile=str(state / "ca.pem"))
        with (
            socket.create_connection(("127.0.0.1", server.port), timeout=5) as raw,
            context.wrap_socket(raw, server_hostname="authentik.local") as tls,
        ):
            assert tls.version() is not None
    finally:
        server.close()


def test_served_fingerprint_equals_the_leaf_fingerprint(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)
    state = _state(lib_harness)
    server = _TlsServer(state / "leaf.pem", state / "leaf.key")
    try:
        proc = lib_harness.run(
            PRELUDE
            + 'want="$(leaf_fingerprint)"; '
            + f'have="$(served_fingerprint authentik.local {server.port} '
            + f'"{state}/ca.pem" 127.0.0.1)"; '
            + '[[ -n "$want" && "$want" == "$have" ]]'
        )
    finally:
        server.close()

    assert proc.returncode == 0, proc.stderr


def test_second_run_is_a_no_op(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)
    state = _state(lib_harness)
    before = ((state / "ca.pem").read_bytes(), (state / "leaf.pem").read_bytes())

    _ensure(lib_harness)

    assert ((state / "ca.pem").read_bytes(), (state / "leaf.pem").read_bytes()) == before


def test_hostname_change_regenerates_the_leaf_but_keeps_the_ca(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)
    state = _state(lib_harness)
    ca, leaf = (state / "ca.pem").read_bytes(), (state / "leaf.pem").read_bytes()

    _ensure(lib_harness, host="other.local")

    assert (state / "ca.pem").read_bytes() == ca
    assert (state / "leaf.pem").read_bytes() != leaf
    assert "DNS:other.local" in _text(state / "leaf.pem")


def test_leaf_close_to_expiry_is_regenerated(lib_harness: LibHarness) -> None:
    _ensure(lib_harness, PS_EVAL_LEAF_VALID_DAYS="10")
    state = _state(lib_harness)
    short_lived = (state / "leaf.pem").read_bytes()

    _ensure(lib_harness)

    assert (state / "leaf.pem").read_bytes() != short_lived


def test_private_keys_are_owner_only_and_never_printed(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(PRELUDE + 'ensure_local_leaf "authentik.local"')

    state = _state(lib_harness)
    assert proc.returncode == 0
    for key in ("ca.key", "leaf.key"):
        assert stat.S_IMODE((state / key).stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert "PRIVATE KEY" not in proc.stdout + proc.stderr
