"""Real-`openssl`, real-TLS-handshake tests for `scripts/lib/local-tls.sh`.

Marked `integration`: they spawn real `openssl` and bind a local TLS server socket (L2 Testing
Patterns). They prove what the fake-`openssl` logic tests cannot: the CA/leaf extensions that
strict verifiers require (Python 3.13+/browsers need SKI and AKI), the CA's critical name
constraint and short validity, a strict-verify handshake on the running interpreter, strict leaf
names, CA replacement, idempotence, near-expiry renewal and file modes.
"""

from __future__ import annotations

import shutil
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
HOST = "ps.test"
OTHER_HOST = "other.test"
SECONDS_PER_DAY = 86400

_LEGACY_CA_CNF = """[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = Policy System local CA
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
"""


def _ensure(harness: LibHarness, host: str = HOST, **env: str) -> None:
    proc = harness.run(PRELUDE + f'ensure_local_leaf "{host}"', **env)
    assert proc.returncode == 0, proc.stderr


def _openssl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, test-owned files
        ["openssl", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=_OPENSSL_TIMEOUT_SECONDS,
    )


def _text(path: Path) -> str:
    out = _openssl("x509", "-in", str(path), "-noout", "-text")
    assert out.returncode == 0, out.stderr
    return out.stdout


def _squashed_text(path: Path) -> str:
    return " ".join(_text(path).split())


def _state(harness: LibHarness) -> Path:
    return harness.root / "state"


def _expires_after(path: Path, days: int) -> bool:
    """True when the certificate is still valid `days` days from now."""
    done = _openssl("x509", "-in", str(path), "-noout", "-checkend", str(days * SECONDS_PER_DAY))
    return done.returncode == 0


def _fingerprint(path: Path, algorithm: str) -> str:
    out = _openssl("x509", "-in", str(path), "-noout", "-fingerprint", f"-{algorithm}")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().split("=", 1)[1].replace(":", "").upper()


def _seed_ca(harness: LibHarness, cnf_text: str, days: int) -> None:
    """Write a CA built from `cnf_text` (not the shape the library creates) to the state dir."""
    state = _state(harness)
    state.mkdir(exist_ok=True)
    cnf = harness.root / "seed-ca.cnf"
    cnf.write_text(cnf_text, encoding="utf-8")
    made = _openssl(
        "req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", str(days),
        "-config", str(cnf), "-keyout", str(state / "ca.key"), "-out", str(state / "ca.pem"),
    )  # fmt: skip
    assert made.returncode == 0, made.stderr


def _constrained_ca_cnf(host: str) -> str:
    return _LEGACY_CA_CNF.replace(
        "[v3_ca]\n", f"[v3_ca]\nnameConstraints = critical, permitted;DNS:{host}\n"
    )


def _issue_leaf(
    harness: LibHarness, name: str, sans: tuple[str, ...] | None = None
) -> tuple[Path, Path]:
    """Sign a leaf for `name` with the state dir's CA; `sans` defaults to one DNS entry."""
    state = _state(harness)
    entries = (f"DNS:{name}",) if sans is None else sans
    san_line = f"subjectAltName = {','.join(entries)}\n" if entries else ""
    cnf = harness.root / f"leaf-{name}.cnf"
    cnf.write_text(
        f"[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = {name}\n"
        "[v3_leaf]\nbasicConstraints = critical, CA:FALSE\n"
        "keyUsage = critical, digitalSignature, keyEncipherment\nextendedKeyUsage = serverAuth\n"
        f"{san_line}subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid, issuer\n",
        encoding="utf-8",
    )
    key, csr, pem = (harness.root / f"{name}.{ext}" for ext in ("key", "csr", "pem"))
    made = _openssl(
        "req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", str(cnf),
        "-keyout", str(key), "-out", str(csr),
    )  # fmt: skip
    assert made.returncode == 0, made.stderr
    signed = _openssl(
        "x509", "-req", "-sha256", "-days", "397", "-in", str(csr),
        "-CA", str(state / "ca.pem"), "-CAkey", str(state / "ca.key"),
        "-CAserial", str(harness.root / "extra.srl"), "-CAcreateserial",
        "-extfile", str(cnf), "-extensions", "v3_leaf", "-out", str(pem),
    )  # fmt: skip
    assert signed.returncode == 0, signed.stderr
    return pem, key


def _install_leaf(harness: LibHarness, pem: Path, key: Path) -> None:
    state = _state(harness)
    shutil.copy(pem, state / "leaf.pem")
    shutil.copy(key, state / "leaf.key")


def test_ca_is_a_ca_with_subject_key_identifier(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)

    text = _text(_state(lib_harness) / "ca.pem")
    assert "CA:TRUE" in text
    assert "Subject Key Identifier" in text
    assert "Certificate Sign" in text


def test_ca_has_a_critical_name_constraint_permitting_only_the_evaluator_name(
    lib_harness: LibHarness,
) -> None:
    _ensure(lib_harness)

    text = _squashed_text(_state(lib_harness) / "ca.pem")
    assert f"X509v3 Name Constraints: critical Permitted: DNS:{HOST} " in text
    assert "Excluded:" not in text
    assert text.count("Permitted:") == 1


def test_ca_validity_is_at_most_730_days(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)

    ca = _state(lib_harness) / "ca.pem"
    assert _expires_after(ca, 729)
    assert not _expires_after(ca, 731)


def test_ca_is_restricted_to_tls_server_authentication(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)

    text = _squashed_text(_state(lib_harness) / "ca.pem")
    assert "X509v3 Extended Key Usage: TLS Web Server Authentication" in text
    assert "Code Signing" not in text
    assert "E-mail Protection" not in text


def test_ca_subject_carries_the_evaluator_name(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)

    assert f"Policy System local CA ({HOST})" in _text(_state(lib_harness) / "ca.pem")


def test_openssl_verify_rejects_a_leaf_for_any_other_name(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)
    other_leaf, _ = _issue_leaf(lib_harness, OTHER_HOST)

    verdict = _openssl("verify", "-CAfile", str(_state(lib_harness) / "ca.pem"), str(other_leaf))

    assert verdict.returncode != 0
    assert "permitted subtree violation" in verdict.stdout + verdict.stderr


def test_leaf_has_ski_aki_san_and_server_auth_and_verifies_against_the_ca(
    lib_harness: LibHarness,
) -> None:
    _ensure(lib_harness)

    state = _state(lib_harness)
    text = _text(state / "leaf.pem")
    assert "Subject Key Identifier" in text
    assert "Authority Key Identifier" in text
    assert f"DNS:{HOST}" in text
    assert "TLS Web Server Authentication" in text
    assert "CA:FALSE" in text
    verify = _openssl("verify", "-CAfile", str(state / "ca.pem"), str(state / "leaf.pem"))
    assert verify.returncode == 0, verify.stderr


def test_leaf_san_is_exactly_the_one_name(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)

    text = _squashed_text(_state(lib_harness) / "leaf.pem")
    assert f"X509v3 Subject Alternative Name: DNS:{HOST} " in text
    assert text.count("DNS:") == 1


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


def _handshake_error(harness: LibHarness, cert: Path, key: Path, server_name: str) -> str:
    """Connect a strict default client trusting only the local CA; "" when it verifies."""
    server = _TlsServer(cert, key)
    try:
        context = ssl.create_default_context(cafile=str(_state(harness) / "ca.pem"))
        try:
            with (
                socket.create_connection(("127.0.0.1", server.port), timeout=5) as raw,
                context.wrap_socket(raw, server_hostname=server_name),
            ):
                return ""
        except ssl.SSLError as error:
            return str(error)
    finally:
        server.close()


def test_strict_default_client_verifies_the_leaf_with_only_the_local_ca(
    lib_harness: LibHarness,
) -> None:
    """Python's default context is strict (X509_STRICT): passes only with SKI/AKI present."""
    _ensure(lib_harness)
    state = _state(lib_harness)

    assert _handshake_error(lib_harness, state / "leaf.pem", state / "leaf.key", HOST) == ""


def test_python_tls_client_rejects_a_leaf_for_another_name(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)
    other_leaf, other_key = _issue_leaf(lib_harness, OTHER_HOST)

    rejection = _handshake_error(lib_harness, other_leaf, other_key, OTHER_HOST)

    assert "permitted subtree violation" in rejection


def test_second_run_is_a_no_op(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)
    state = _state(lib_harness)
    before = ((state / "ca.pem").read_bytes(), (state / "leaf.pem").read_bytes())

    _ensure(lib_harness)

    assert ((state / "ca.pem").read_bytes(), (state / "leaf.pem").read_bytes()) == before


def test_a_leaf_with_an_extra_san_is_reissued_and_the_ca_is_kept(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)
    state = _state(lib_harness)
    ca = (state / "ca.pem").read_bytes()
    _install_leaf(lib_harness, *_issue_leaf(lib_harness, HOST, (f"DNS:{HOST}", f"DNS:www.{HOST}")))
    assert f"DNS:www.{HOST}" in _text(state / "leaf.pem")

    _ensure(lib_harness)

    text = _squashed_text(state / "leaf.pem")
    assert text.count("DNS:") == 1
    assert f"DNS:{HOST} " in text
    assert (state / "ca.pem").read_bytes() == ca


def test_a_leaf_without_a_san_is_reissued_and_the_ca_is_kept(lib_harness: LibHarness) -> None:
    _ensure(lib_harness)
    state = _state(lib_harness)
    ca = (state / "ca.pem").read_bytes()
    _install_leaf(lib_harness, *_issue_leaf(lib_harness, HOST, ()))
    assert "Subject Alternative Name" not in _text(state / "leaf.pem")

    _ensure(lib_harness)

    assert f"DNS:{HOST}" in _text(state / "leaf.pem")
    assert (state / "ca.pem").read_bytes() == ca


def test_a_ca_without_the_constraint_is_replaced_together_with_the_leaf(
    lib_harness: LibHarness,
) -> None:
    _seed_ca(lib_harness, _LEGACY_CA_CNF, days=3650)
    state = _state(lib_harness)
    legacy = (state / "ca.pem").read_bytes()
    _install_leaf(lib_harness, *_issue_leaf(lib_harness, HOST))
    old_leaf = (state / "leaf.pem").read_bytes()

    _ensure(lib_harness)

    assert (state / "ca.pem").read_bytes() != legacy
    assert (state / "leaf.pem").read_bytes() != old_leaf
    assert "Name Constraints: critical" in _text(state / "ca.pem")
    verify = _openssl("verify", "-CAfile", str(state / "ca.pem"), str(state / "leaf.pem"))
    assert verify.returncode == 0, verify.stderr


def test_the_replaced_ca_is_kept_and_its_fingerprints_and_subject_are_recorded(
    lib_harness: LibHarness,
) -> None:
    _seed_ca(lib_harness, _LEGACY_CA_CNF, days=3650)
    state = _state(lib_harness)
    sha1 = _fingerprint(state / "ca.pem", "sha1")
    sha256 = _fingerprint(state / "ca.pem", "sha256")

    proc = lib_harness.run(
        PRELUDE
        + f'ensure_local_leaf "{HOST}" || exit 1\n'
        + 'printf "sha1=%s\\nsha256=%s\\nsubject=%s\\npem=%s\\n" '
        + '"$LOCAL_TLS_REPLACED_CA_SHA1" "$LOCAL_TLS_REPLACED_CA_SHA256" '
        + '"$LOCAL_TLS_REPLACED_CA_SUBJECT" "$LOCAL_TLS_REPLACED_CA_PEM"'
    )

    assert proc.returncode == 0, proc.stderr
    lines = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    assert lines["sha1"] == sha1
    assert lines["sha256"] == sha256
    assert "Policy System local CA" in lines["subject"]
    assert f"({HOST})" not in lines["subject"]
    kept = state / f"ca.replaced-{sha1[:8]}.pem"
    assert lines["pem"] == str(kept)
    assert _fingerprint(kept, "sha1") == sha1


def test_a_current_ca_replaces_nothing_and_records_no_replaced_ca(
    lib_harness: LibHarness,
) -> None:
    _ensure(lib_harness)

    proc = lib_harness.run(
        PRELUDE
        + f'ensure_local_leaf "{HOST}" || exit 1\n'
        + 'printf "[%s]" "$LOCAL_TLS_REPLACED_CA_SHA1"'
    )

    assert proc.stdout == "[]"
    assert not list(_state(lib_harness).glob("ca.replaced-*.pem"))


def test_a_ca_close_to_expiry_is_replaced(lib_harness: LibHarness) -> None:
    _seed_ca(lib_harness, _constrained_ca_cnf(HOST), days=10)
    state = _state(lib_harness)
    short_lived = (state / "ca.pem").read_bytes()

    _ensure(lib_harness)

    assert (state / "ca.pem").read_bytes() != short_lived
    assert _expires_after(state / "ca.pem", 729)


def test_a_ca_constrained_to_another_name_is_replaced_by_one_for_the_requested_name(
    lib_harness: LibHarness,
) -> None:
    _ensure(lib_harness, host=OTHER_HOST)
    state = _state(lib_harness)
    other_ca = (state / "ca.pem").read_bytes()

    _ensure(lib_harness, host=HOST)

    assert (state / "ca.pem").read_bytes() != other_ca
    assert f"Permitted: DNS:{HOST} " in _squashed_text(state / "ca.pem")
    assert f"DNS:{HOST}" in _text(state / "leaf.pem")


def test_local_tls_ca_is_current_accepts_a_fresh_ca_and_refuses_a_legacy_one(
    lib_harness: LibHarness,
) -> None:
    _ensure(lib_harness)
    assert lib_harness.run(PRELUDE + f'local_tls_ca_is_current "{HOST}"').returncode == 0
    assert lib_harness.run(PRELUDE + f'local_tls_ca_is_current "{OTHER_HOST}"').returncode != 0

    _seed_ca(lib_harness, _LEGACY_CA_CNF, days=3650)

    assert lib_harness.run(PRELUDE + f'local_tls_ca_is_current "{HOST}"').returncode != 0


def test_local_tls_ca_is_current_refuses_a_constrained_ca_that_lives_longer_than_730_days(
    lib_harness: LibHarness,
) -> None:
    _seed_ca(lib_harness, _constrained_ca_cnf(HOST), days=1000)

    assert lib_harness.run(PRELUDE + f'local_tls_ca_is_current "{HOST}"').returncode != 0


def test_leaf_close_to_expiry_is_regenerated(lib_harness: LibHarness) -> None:
    _ensure(lib_harness, PS_EVAL_LEAF_VALID_DAYS="10")
    state = _state(lib_harness)
    short_lived = (state / "leaf.pem").read_bytes()

    _ensure(lib_harness)

    assert (state / "leaf.pem").read_bytes() != short_lived


def test_private_keys_are_owner_only_and_never_printed(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(PRELUDE + f'ensure_local_leaf "{HOST}"')

    state = _state(lib_harness)
    assert proc.returncode == 0
    for key in ("ca.key", "leaf.key"):
        assert stat.S_IMODE((state / key).stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert "PRIVATE KEY" not in proc.stdout + proc.stderr
