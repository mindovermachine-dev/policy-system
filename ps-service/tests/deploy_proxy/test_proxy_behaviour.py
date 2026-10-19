"""Behaviour of the evaluator proxy's rendered nginx configuration in the pinned nginx image.

The rendered `nginx.conf` runs unchanged except that the two upstream servers point at stub
listeners added to the same container. The Authentik stub logs every hit it receives, so a
rejected request is proven not to have reached it. Needs a container runtime and the pinned image
(pulled when absent), hence the `container_image` marker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.container_image

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_TIMEOUT_SECONDS = 60.0
PULL_TIMEOUT_SECONDS = 300.0
CURL_TIMEOUT_SECONDS = 20.0

_ECHO_FIELDS = (
    "host=$http_host",
    "xfh=$http_x_forwarded_host",
    "xfp=$http_x_forwarded_proto",
    "xff=$http_x_forwarded_for",
    "xport=$http_x_forwarded_port",
)
_ECHO = "".join(f"{field}\\n" for field in _ECHO_FIELDS)
_ECHO_PS = _ECHO + "upgrade=$http_upgrade\\nconnection=$http_connection\\n"
_ECHO_AUTHENTIK = _ECHO + "connection=$http_connection\\n"

STUB_TEMPLATE = """
    server {
      listen 127.0.0.1:19000;
      client_max_body_size 10m;
      access_log /tmp/ps_service_hits;
      location = /cookies {
        add_header Set-Cookie "sid=1; Path=/";
        add_header Set-Cookie "already=3; Path=/; HttpOnly";
        return 200 "cookies\\n";
      }
      location = /echo {
        return 200 "@ECHO@";
      }
      location /mcp/ {
        if ($http_authorization = "") {
          add_header WWW-Authenticate 'Bearer realm="ps"' always;
          return 401 "unauthorized\\n";
        }
        return 200 "authorized\\n";
      }
      location / { return 200 "ps-service-stub\\n"; }
    }
    server {
      listen 127.0.0.1:19001;
      access_log /tmp/authentik_hits;
      location = /auth/static/cookies {
        add_header Set-Cookie "authentik_csrf=1; Path=/";
        add_header Set-Cookie "authentik_session=2; Path=/";
        add_header Set-Cookie "already=3; Path=/; HttpOnly";
        return 200 "cookies\\n";
      }
      location = /auth/static/echo {
        return 200 "@ECHO_NO_UPGRADE@";
      }
      location = /auth/application/o/ps-cli/.well-known/openid-configuration {
        default_type application/json;
        return 200 '{"issuer": "$http_x_forwarded_proto://$http_x_forwarded_host/auth/application/o/ps-cli/"}';
      }
      location / { return 200 "authentik-stub\\n"; }
    }
"""


STUBS = STUB_TEMPLATE.replace("@ECHO@", _ECHO_PS).replace("@ECHO_NO_UPGRADE@", _ECHO_AUTHENTIK)


def _cli() -> str:
    override = os.environ.get("PS_CONTAINER_CLI")
    found = shutil.which(override) if override else shutil.which("podman") or shutil.which("docker")
    if found is None:
        pytest.skip("no container runtime on PATH")
    return found


def _run(cli: str, *args: str, timeout: float = RUN_TIMEOUT_SECONDS) -> str:
    result = subprocess.run(  # noqa: S603 - cli is a resolved absolute path; args are built here
        [cli, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    assert result.returncode == 0, f"{cli} {' '.join(args)} failed:\n{result.stderr}"
    return result.stdout


def _image(chart_dir: Path) -> str:
    values = yaml.safe_load((chart_dir / "values.yaml").read_text())["proxy"]["image"]
    return f"{values['repository']}:{values['tag']}"


@pytest.fixture(scope="module")
def proxy(
    tmp_path_factory: pytest.TempPathFactory, nginx_conf: str, chart_dir: Path
) -> Iterator[tuple[str, str, str]]:
    """Run the proxy container and yield (cli, container name, base url)."""
    openssl = shutil.which("openssl")
    assert openssl is not None, "openssl must be on PATH"
    cli = _cli()
    workdir = tmp_path_factory.mktemp("proxy")
    tls = workdir / "tls"
    tls.mkdir()
    subprocess.run(  # noqa: S603 - openssl is a resolved absolute path; args are literals
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=ps.test",
            "-keyout",
            str(tls / "tls.key"),
            "-out",
            str(tls / "tls.crt"),
        ],
        capture_output=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=True,
    )
    (tls / "tls.key").chmod(0o644)
    conf = nginx_conf
    conf = re.sub(r"server [^;\s]*-ps-service:8000;", "server 127.0.0.1:19000;", conf)
    conf = re.sub(r"server [^;\s]*-authentik-server:80;", "server 127.0.0.1:19001;", conf)
    conf = conf.rstrip()
    assert conf.endswith("}")
    conf = conf[:-1] + STUBS + "}\n"
    (workdir / "nginx.conf").write_text(conf)
    image = _image(chart_dir)
    _run(cli, "pull", "--quiet", image, timeout=PULL_TIMEOUT_SECONDS)
    name = f"ps-proxy-behaviour-{uuid.uuid4().hex[:8]}"
    _run(
        cli, "run", "-d", "--rm", "--name", name, "--read-only", "--tmpfs", "/tmp",  # noqa: S108
        "-p", "127.0.0.1::8443",
        "-v", f"{workdir / 'nginx.conf'}:/etc/nginx/nginx.conf:ro",
        "-v", f"{tls}:/etc/nginx/tls:ro",
        image,
    )  # fmt: skip
    try:
        port = _run(cli, "port", name, "8443/tcp").strip().splitlines()[0].rsplit(":", 1)[1]
        yield cli, name, f"https://127.0.0.1:{port}"
    finally:
        subprocess.run(  # noqa: S603 - cli is a resolved absolute path
            [cli, "rm", "-f", name], capture_output=True, timeout=RUN_TIMEOUT_SECONDS, check=False
        )


def _by_name(base: str, name: str = "ps.test") -> tuple[str, list[str]]:
    """Return the URL base and curl arguments that reach the proxy by `name`, so SNI is sent."""
    port = base.rsplit(":", 1)[1]
    return f"https://{name}:{port}", ["--resolve", f"{name}:{port}:127.0.0.1"]


def _get(
    base: str,
    target: str,
    *headers: str,
    name: str = "ps.test",
    host: str | None = "ps.test",
) -> tuple[int, str]:
    curl = shutil.which("curl")
    assert curl is not None, "curl must be on PATH"
    url, resolve = _by_name(base, name)
    host_args = ["-H", f"Host: {host}"] if host is not None else []
    header_args = [arg for header in headers for arg in ("-H", header)]
    result = subprocess.run(  # noqa: S603 - curl is a resolved absolute path; target is test data
        [
            curl, "-sk", "--path-as-is", *resolve, *host_args, *header_args, "-o", "-",
            "-w", "\n%{http_code}", f"{url}{target}",
        ],
        capture_output=True,
        text=True,
        timeout=CURL_TIMEOUT_SECONDS,
        check=False,
    )  # fmt: skip
    body, _, status = result.stdout.rpartition("\n")
    return int(status), body


def test_a_request_without_a_bearer_token_gets_the_upstream_401_through_the_proxy(
    proxy: tuple[str, str, str],
) -> None:
    _, _, base = proxy
    curl = shutil.which("curl")
    assert curl is not None, "curl must be on PATH"
    url, resolve = _by_name(base)

    result = subprocess.run(  # noqa: S603 - curl is a resolved absolute path; url is test data
        [curl, "-sk", *resolve, "-D", "-", "-o", "-", f"{url}/mcp/"],
        capture_output=True,
        text=True,
        timeout=CURL_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.stdout.startswith("HTTP/"), result.stdout
    assert result.stdout.split(None, 2)[1] == "401"
    assert 'www-authenticate: bearer realm="ps"' in result.stdout.lower()
    assert _get(base, "/mcp/", "Authorization: Bearer x") == (200, "authorized\n")


def _hits(cli: str, name: str, which: str) -> int:
    out = _run(cli, "exec", name, "sh", "-c", f"cat /tmp/{which}_hits 2>/dev/null | wc -l")
    return int(out.strip())


REJECTED = [
    "/auth/if/admin/",
    "/auth/api/v3/core/users/",
    "/auth//if/admin/",
    "/auth/static/../if/admin/",
    "/auth/static/%2e%2e/if/admin/",
    "/auth/static/..;/if/admin/",
    "/auth/static/%5c..%5cif/admin/",
    "/auth/static/%252e%252e/if/admin/",
    "/auth/devicex",
    "/auth",
    "/auth/",
]


def test_non_allowlisted_auth_paths_return_403_without_reaching_authentik(
    proxy: tuple[str, str, str],
) -> None:
    cli, name, base = proxy
    before = _hits(cli, name, "authentik")

    for target in REJECTED:
        status, _ = _get(base, target)
        assert status == 403, target

    assert _hits(cli, name, "authentik") == before


def test_allowlisted_paths_reach_authentik(proxy: tuple[str, str, str]) -> None:
    cli, name, base = proxy
    before = _hits(cli, name, "authentik")

    for target in (
        "/auth/application/o/ps/.well-known/openid-configuration",
        "/auth/device",
        "/auth/static/dist/flow.js",
        "/auth/api/v3/flows/executor/x/?query=next%3D%2Fauth%2F",
    ):
        status, body = _get(base, target)
        assert (status, body.strip()) == (200, "authentik-stub"), target

    assert _hits(cli, name, "authentik") == before + 4


def test_the_bare_auth_path_returns_the_static_body(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy

    status, body = _get(base, "/auth/")

    assert status == 403
    assert body.strip() == "Signed in. Open the Policy System from your client."


def test_other_paths_reach_ps_service(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy

    assert _get(base, "/health")[1].strip() == "ps-service-stub"
    assert _get(base, "/AUTH/if/admin/")[1].strip() == "ps-service-stub"


def test_the_access_log_omits_the_query_string(proxy: tuple[str, str, str]) -> None:
    cli, name, base = proxy

    _get(base, "/health?token=secret-value")

    logs = subprocess.run(  # noqa: S603 - cli is a resolved absolute path
        [cli, "logs", name],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )
    combined = logs.stdout + logs.stderr
    assert '"GET /health"' in combined
    assert "secret-value" not in combined


def _set_cookies(base: str, target: str) -> dict[str, str]:
    curl = shutil.which("curl")
    assert curl is not None, "curl must be on PATH"
    url, resolve = _by_name(base)
    result = subprocess.run(  # noqa: S603 - curl is a resolved absolute path; target is test data
        [
            curl,
            "-sk",
            "-D",
            "-",
            "-o",
            "/dev/null",
            *resolve,
            "-H",
            "Host: ps.test",
            f"{url}{target}",
        ],
        capture_output=True,
        text=True,
        timeout=CURL_TIMEOUT_SECONDS,
        check=False,
    )
    cookies: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, _, value = line.partition(":")
        if name.lower() == "set-cookie":
            cookie_name, _, attributes = value.strip().partition("=")
            cookies[cookie_name] = attributes
    return cookies


def _flags(attributes: str) -> set[str]:
    return {part.strip().lower() for part in attributes.split(";")[1:]}


def test_ps_service_cookies_get_all_three_flags(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy

    cookies = _set_cookies(base, "/cookies")

    assert {"secure", "httponly", "samesite=lax"} <= _flags(cookies["sid"])
    assert _flags(cookies["already"]).issuperset({"secure", "httponly", "samesite=lax"})


def test_authentik_cookies_keep_the_script_read_one_readable(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy

    cookies = _set_cookies(base, "/auth/static/cookies")

    csrf = _flags(cookies["authentik_csrf"])
    assert {"secure", "samesite=lax"} <= csrf
    assert "httponly" not in csrf
    assert {"secure", "httponly", "samesite=lax"} <= _flags(cookies["authentik_session"])


def test_an_upstream_cookie_that_already_has_a_flag_is_not_given_a_second_one(
    proxy: tuple[str, str, str],
) -> None:
    _, _, base = proxy

    cookies = _set_cookies(base, "/auth/static/cookies")

    attributes = [part.strip().lower() for part in cookies["already"].split(";")[1:]]
    assert attributes.count("httponly") == 1
    assert attributes.count("secure") == 1


def _context(version: ssl.TLSVersion) -> ssl.SSLContext | None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        context.minimum_version = version
        context.maximum_version = version
    except ValueError:
        return None
    return context


def _handshake(base: str, context: ssl.SSLContext) -> str | None:
    host, port = base.removeprefix("https://").rsplit(":", 1)
    with (
        socket.create_connection((host, int(port)), timeout=CURL_TIMEOUT_SECONDS) as raw,
        context.wrap_socket(raw, server_hostname="ps.test") as tls,
    ):
        return tls.version()


def test_tls_12_and_13_succeed(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy
    tls12 = _context(ssl.TLSVersion.TLSv1_2)
    tls13 = _context(ssl.TLSVersion.TLSv1_3)
    assert tls12 is not None
    assert tls13 is not None

    assert _handshake(base, tls12) == "TLSv1.2"
    assert _handshake(base, tls13) == "TLSv1.3"


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_tls_11_is_refused(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy
    context = _context(ssl.TLSVersion.TLSv1_1)
    if context is None:
        pytest.skip("this client cannot offer TLS 1.1")

    with pytest.raises((ssl.SSLError, OSError)):
        _handshake(base, context)


def test_plain_http_to_the_tls_port_returns_no_api_response(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy
    curl = shutil.which("curl")
    assert curl is not None, "curl must be on PATH"

    result = subprocess.run(  # noqa: S603 - curl is a resolved absolute path
        [
            curl,
            "-s",
            "-H",
            "Host: ps.test",
            "-w",
            "\n%{http_code}",
            base.replace("https://", "http://") + "/health",
        ],
        capture_output=True,
        text=True,
        timeout=CURL_TIMEOUT_SECONDS,
        check=False,
    )

    body, _, status = result.stdout.rpartition("\n")
    assert status == "400"
    assert "ps-service-stub" not in body


def _echo(body: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in body.strip().splitlines())


SPOOFED = (
    "X-Forwarded-For: 203.0.113.9",
    "X-Forwarded-Host: evil.example",
    "X-Forwarded-Proto: http",
    "X-Forwarded-Port: 1",
)


@pytest.mark.parametrize("target", ["/echo", "/auth/static/echo"])
def test_spoofed_x_forwarded_headers_are_replaced(proxy: tuple[str, str, str], target: str) -> None:
    _, _, base = proxy

    status, body = _get(base, target, *SPOOFED, host="ps.test:30443")

    assert status == 200
    seen = _echo(body)
    assert seen["xff"] != "" and "203.0.113.9" not in seen["xff"]
    assert "," not in seen["xff"]
    assert seen["xfh"] == "ps.test:30443"
    assert seen["xfp"] == "https"
    assert seen["xport"] == "30443"
    assert seen["host"] == "ps.test:30443"


def test_an_upgrade_request_reaches_ps_service_as_an_upgrade(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy

    _, upgraded = _get(base, "/echo", "Upgrade: websocket", "Connection: Upgrade")
    _, plain = _get(base, "/echo")

    assert _echo(upgraded)["upgrade"] == "websocket"
    assert _echo(upgraded)["connection"].lower() == "upgrade"
    assert _echo(plain)["connection"] == "close"


def test_authentik_never_receives_an_upgrade_connection(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy

    _, body = _get(base, "/auth/static/echo", "Upgrade: websocket", "Connection: Upgrade")

    assert _echo(body)["connection"] == ""


def test_a_host_header_for_another_name_reaches_no_backend(proxy: tuple[str, str, str]) -> None:
    cli, name, base = proxy
    before = (_hits(cli, name, "authentik"), _hits(cli, name, "ps_service"))

    for host in ("evil.example", "evil.example:30443", "ps.test.evil.example"):
        for target in ("/health", "/auth/static/echo"):
            assert _get(base, target, host=host)[0] == 421, (host, target)

    assert (_hits(cli, name, "authentik"), _hits(cli, name, "ps_service")) == before


def test_an_unknown_server_name_gets_no_certificate_and_no_backend(
    proxy: tuple[str, str, str],
) -> None:
    cli, name, base = proxy
    before = (_hits(cli, name, "authentik"), _hits(cli, name, "ps_service"))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    port = int(base.rsplit(":", 1)[1])

    for server_name in ("evil.example", None):
        with (
            pytest.raises((ssl.SSLError, OSError)),
            socket.create_connection(("127.0.0.1", port), timeout=CURL_TIMEOUT_SECONDS) as raw,
            context.wrap_socket(raw, server_hostname=server_name),
        ):
            pass

    assert (_hits(cli, name, "authentik"), _hits(cli, name, "ps_service")) == before


def test_a_request_body_above_the_default_limit_is_accepted(proxy: tuple[str, str, str]) -> None:
    _, _, base = proxy
    curl = shutil.which("curl")
    assert curl is not None, "curl must be on PATH"
    url, resolve = _by_name(base)

    result = subprocess.run(  # noqa: S603 - curl is a resolved absolute path
        [
            curl, "-sk", *resolve, "-H", "Host: ps.test", "-X", "POST",
            "--data-binary", "@-", "-o", "/dev/null", "-w", "%{http_code}", f"{url}/health",
        ],
        input="x" * (3 * 1024 * 1024),
        capture_output=True,
        text=True,
        timeout=CURL_TIMEOUT_SECONDS,
        check=False,
    )  # fmt: skip

    assert result.stdout != "413"


def _script_issuer() -> tuple[str, str]:
    """Return the issuer host:port and the issuer string the evaluator script configures."""
    script = (REPO_ROOT / "scripts" / "deploy-ps-eval.sh").read_text()
    port = re.search(r"^readonly PROXY_PORT=(\d+)$", script, re.MULTILINE)
    path = re.search(r'^readonly AUTHENTIK_WEB_PATH="([^"]+)"$', script, re.MULTILINE)
    slug = re.search(r'^readonly AUTHENTIK_APP_SLUG="([^"]+)"$', script, re.MULTILINE)
    assert port and path and slug
    authority = f"ps.test:{port.group(1)}"
    return authority, f"https://{authority}{path.group(1)}/application/o/{slug.group(1)}/"


def test_issuer_string_through_proxy_equals_the_issuer_the_script_configures(
    proxy: tuple[str, str, str],
) -> None:
    _, _, base = proxy
    authority, issuer = _script_issuer()

    status, body = _get(
        base,
        "/auth/application/o/ps-cli/.well-known/openid-configuration",
        host=authority,
    )

    assert status == 200
    assert json.loads(body)["issuer"] == issuer
