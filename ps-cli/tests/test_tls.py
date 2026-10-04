"""Issue #175: ps-cli verifies TLS against the OS trust store via `truststore`.

Drives the real `oidc_discovery` request path against a local HTTPS server whose
certificate is signed by a CA generated per test run. That CA is never added to the
OS trust store (a test must not mutate the developer's keychain), so on macOS and
Windows the "trusted CA" case is not reachable here and only the "untrusted fails,
loudly and without fallback" cases run. On Linux, `truststore` defers to OpenSSL's
default paths, so `SSL_CERT_FILE` naming the generated CA demonstrates the trusted
case (AC-BI-009).
"""

from __future__ import annotations

import datetime
import json
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ps_cli.errors import PsCliError
from ps_cli.oidc_discovery import fetch_protected_resource_metadata
from ps_cli.tls import build_ssl_context, is_certificate_verification_error

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_METADATA_BODY = json.dumps(
    {
        "resource": "https://localhost",
        "authorization_servers": ["https://localhost"],
        "scopes_supported": ["openid"],
    }
).encode()
_ON_LINUX = sys.platform.startswith("linux")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # http.server's required method name
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(_METADATA_BODY)

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request logging."""


class _QuietServer(HTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        """Clients that reject the certificate reset the connection; that is expected."""


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_ca(directory: Path) -> tuple[rsa.RSAPrivateKey, x509.Certificate, Path]:
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ps-cli test CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = directory / "ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert, path


def _write_server_cert(
    directory: Path,
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    *,
    hostname: str,
    expired: bool,
) -> tuple[Path, Path]:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    not_after = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(days=1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=2))
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _Server:
    def __init__(self, url: str, ca_path: Path) -> None:
        self.url = url
        self.ca_path = ca_path


def _serve(
    directory: Path, *, hostname: str = "localhost", expired: bool = False
) -> Iterator[_Server]:
    ca_key, ca_cert, ca_path = _write_ca(directory)
    cert_path, key_path = _write_server_cert(
        directory, ca_key, ca_cert, hostname=hostname, expired=expired
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    httpd = _QuietServer(("127.0.0.1", 0), _Handler)
    httpd.socket = server_context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Server(f"https://localhost:{httpd.server_port}", ca_path)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def good_server(tmp_path: Path) -> Iterator[_Server]:
    """HTTPS server for `localhost` with a valid cert from an OS-untrusted test CA."""
    yield from _serve(tmp_path)


@pytest.fixture
def wrong_host_server(tmp_path: Path) -> Iterator[_Server]:
    """HTTPS server whose cert is for another hostname."""
    yield from _serve(tmp_path, hostname="other.example")


@pytest.fixture
def expired_server(tmp_path: Path) -> Iterator[_Server]:
    """HTTPS server whose cert (for `localhost`) has expired."""
    yield from _serve(tmp_path, expired=True)


def _clean_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


def test_untrusted_ca_fails_naming_the_certificate_without_fallback(
    good_server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-003/005: an untrusted CA fails, and the message blames the certificate."""
    _clean_trust_env(monkeypatch)

    with pytest.raises(PsCliError) as excinfo:
        fetch_protected_resource_metadata(good_server.url)

    assert "TLS certificate" in excinfo.value.msg
    assert excinfo.value.hint is not None
    assert "certificate store" in excinfo.value.hint
    assert "server is running" not in str(excinfo.value)


@pytest.mark.skipif(not _ON_LINUX, reason="OpenSSL default paths only apply on Linux (AC-BI-009)")
def test_linux_ssl_cert_file_naming_the_ca_makes_it_trusted(
    good_server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-009: on Linux `SSL_CERT_FILE` is honoured, so the generated CA is trusted."""
    _clean_trust_env(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", str(good_server.ca_path))

    metadata = fetch_protected_resource_metadata(good_server.url)

    assert metadata.resource == "https://localhost"


@pytest.mark.skipif(_ON_LINUX, reason="macOS/Windows use the native verifier (AC-BI-009)")
def test_off_linux_ssl_cert_file_has_no_effect(
    good_server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-009: off Linux `SSL_CERT_FILE` is ignored; only the OS store is consulted."""
    _clean_trust_env(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", str(good_server.ca_path))

    with pytest.raises(PsCliError) as excinfo:
        fetch_protected_resource_metadata(good_server.url)

    assert "TLS certificate" in excinfo.value.msg


@pytest.mark.parametrize("scenario", ["wrong_host_server", "expired_server"])
def test_trusted_ca_but_bad_certificate_still_fails(
    scenario: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-003: a wrong-hostname or expired certificate fails even if its CA is trusted."""
    server: _Server = request.getfixturevalue(scenario)
    _clean_trust_env(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", str(server.ca_path))  # trusts the CA on Linux

    with pytest.raises(PsCliError) as excinfo:
        fetch_protected_resource_metadata(server.url)

    assert "TLS certificate" in excinfo.value.msg


def test_unreachable_host_keeps_the_could_not_reach_message() -> None:
    """AC-BI-006: a refused connection is not reported as a certificate problem."""
    with pytest.raises(PsCliError) as excinfo:
        fetch_protected_resource_metadata("https://127.0.0.1:1")

    assert excinfo.value.msg.startswith("Could not reach https://127.0.0.1:1")
    assert excinfo.value.hint == "check the URL and that the server is running"


def test_certificate_error_message_carries_no_exception_detail() -> None:
    """AC-BI-007: the reported text never echoes the underlying exception's own text."""
    cause = ssl.SSLCertVerificationError("Bearer secret-token certificate verify failed")
    connect_error = httpx.ConnectError("boom")
    connect_error.__cause__ = cause

    def _handler(request: httpx.Request) -> httpx.Response:
        raise connect_error

    with pytest.raises(PsCliError) as excinfo:
        fetch_protected_resource_metadata(
            "https://ps.example", transport=httpx.MockTransport(_handler)
        )

    assert "secret-token" not in str(excinfo.value)
    assert "TLS certificate" in excinfo.value.msg


def test_is_certificate_verification_error_walks_the_cause_chain() -> None:
    """The detector finds the SSL error through `__cause__` and `__context__` links."""
    inner = ssl.SSLCertVerificationError("bad cert")
    middle = httpx.ConnectError("mid")
    middle.__context__ = inner
    outer = httpx.ConnectError("outer")
    outer.__cause__ = middle

    assert is_certificate_verification_error(outer)
    assert not is_certificate_verification_error(httpx.ConnectError("refused"))


def test_build_ssl_context_verifies_certificates_and_hostnames() -> None:
    """AC-BI-003: the shared context never disables verification."""
    context = build_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
