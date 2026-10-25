"""Fast tests for the `graph_gateway` provisioning CLI's environment handling.

The database side (it really provisions, and never prints the admin password) is covered by
`test_provision_live.py`.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import psycopg
import pytest

from ps_service.graph_gateway import provision
from ps_service.graph_gateway.provision import ProvisioningTarget, main
from ps_service.persistence import StatePostgresProvisioningError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_ENVIRON = {
    "PS_STATE_POSTGRES_HOST": "db.internal.example",
    "PS_STATE_POSTGRES_PORT": "6543",
    "PS_STATE_POSTGRES_DATABASE": "ps_state",
    "PS_STATE_POSTGRES_USER": "ps_state",
    "PS_STATE_ADMIN_POSTGRES_USER": "admin_user",
    "PS_STATE_ADMIN_POSTGRES_PASSWORD": "s3cret-admin-password",
    "PS_STATE_GRAPH_OWNER_ROLE": "ps_state_graph_owner",
}


def test_provisioning_target_reads_every_value_from_the_environment() -> None:
    target = ProvisioningTarget.from_environ(_ENVIRON)

    assert (target.host, target.port, target.database, target.app_user) == (
        "db.internal.example",
        6543,
        "ps_state",
        "ps_state",
    )
    assert (target.admin_user, target.owner_role) == ("admin_user", "ps_state_graph_owner")


def test_provisioning_target_defaults_port_and_owner_role() -> None:
    environ = {
        key: value
        for key, value in _ENVIRON.items()
        if key not in {"PS_STATE_POSTGRES_PORT", "PS_STATE_GRAPH_OWNER_ROLE"}
    }

    target = ProvisioningTarget.from_environ(environ)

    assert target.port == 5432
    assert target.owner_role == "ps_state_graph_owner"


def test_provisioning_target_reports_missing_env_var_by_name_without_values() -> None:
    environ = {k: v for k, v in _ENVIRON.items() if k != "PS_STATE_ADMIN_POSTGRES_PASSWORD"}

    with pytest.raises(StatePostgresProvisioningError) as raised:
        ProvisioningTarget.from_environ(environ)

    message = str(raised.value)
    assert "PS_STATE_ADMIN_POSTGRES_PASSWORD" in message
    assert "db.internal.example" not in message
    assert "admin_user" not in message


def test_provisioning_target_repr_never_contains_the_admin_password() -> None:
    assert "s3cret-admin-password" not in repr(ProvisioningTarget.from_environ(_ENVIRON))


def test_main_returns_one_and_names_the_missing_variable_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {k: v for k, v in _ENVIRON.items() if k != "PS_STATE_POSTGRES_HOST"}

    exit_code = main([], environ)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "PS_STATE_POSTGRES_HOST" in captured.err
    assert "s3cret-admin-password" not in captured.out + captured.err


def test_main_help_exits_zero_without_connecting(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"], {})

    assert raised.value.code == 0
    assert "PS_STATE_ADMIN_POSTGRES_USER" in capsys.readouterr().out


def _unused_local_port() -> int:
    """Return a loopback port with nothing listening (bind, read the port, close)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_provisioning_target_reads_the_connect_timeout_with_a_two_minute_default() -> None:
    explicit = ProvisioningTarget.from_environ(
        {**_ENVIRON, "PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS": "7"}
    )

    assert explicit.connect_timeout_seconds == 7
    assert ProvisioningTarget.from_environ(_ENVIRON).connect_timeout_seconds == 120


def test_provisioning_target_rejects_a_non_integer_connect_timeout_by_variable_name() -> None:
    environ = {**_ENVIRON, "PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS": "soon"}

    with pytest.raises(StatePostgresProvisioningError, match="PS_STATE_PROVISION_CONNECT_TIMEOUT"):
        ProvisioningTarget.from_environ(environ)


def test_main_keeps_retrying_an_unreachable_server_until_the_deadline_then_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {
        **_ENVIRON,
        "PS_STATE_POSTGRES_HOST": "127.0.0.1",
        "PS_STATE_POSTGRES_PORT": str(_unused_local_port()),
        "PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS": "2",
    }

    started = time.monotonic()
    exit_code = main([], environ)
    elapsed = time.monotonic() - started

    captured = capsys.readouterr()
    assert exit_code == 1
    assert elapsed >= 2
    assert "did not accept connections within 2 seconds" in captured.err
    assert "s3cret-admin-password" not in captured.out + captured.err
    assert "127.0.0.1" not in captured.out + captured.err


# A stand-in Postgres server: it declines any SSL/GSS encryption request and answers every
# startup packet with one FATAL ErrorResponse carrying the SQLSTATE given as its argument, the
# way a real server reports "starting up" (57P03) or a refused login (28P01). It runs in its own
# process because libpq's blocking calls hold the GIL, which would starve an in-process thread.
_REFUSING_SERVER_SCRIPT = r"""
import socket, struct, sys

ssl_or_gss_request = {80877103, 80877104}
fields = [(b"S", b"FATAL"), (b"V", b"FATAL"), (b"C", sys.argv[1].encode()), (b"M", b"refused")]
payload = b"".join(key + value + b"\0" for key, value in fields) + b"\0"
error_response = b"E" + struct.pack("!i", len(payload) + 4) + payload
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(16)
print(listener.getsockname()[1], flush=True)
while True:
    client, _ = listener.accept()
    with client:
        while True:
            header = client.recv(4, socket.MSG_WAITALL)
            if len(header) < 4:
                break
            body = client.recv(struct.unpack("!i", header)[0] - 4, socket.MSG_WAITALL)
            if struct.unpack("!i", body[:4])[0] in ssl_or_gss_request:
                client.sendall(b"N")
                continue
            client.sendall(error_response)
            break
"""


@pytest.fixture
def refusing_server() -> Iterator[Callable[[str], int]]:
    """Start stand-in servers that refuse every login with a given SQLSTATE; yield a starter."""
    servers: list[subprocess.Popen[str]] = []

    def start(sqlstate: str) -> int:
        server = subprocess.Popen(  # noqa: S603 -- fixed interpreter and a literal script
            [sys.executable, "-c", _REFUSING_SERVER_SCRIPT, sqlstate],
            stdout=subprocess.PIPE,
            text=True,
        )
        servers.append(server)
        assert server.stdout is not None
        return int(server.stdout.readline())

    yield start
    for server in servers:
        server.kill()
        server.wait()


def test_main_exits_one_at_once_when_the_server_refuses_the_login(
    capsys: pytest.CaptureFixture[str], refusing_server: Callable[[str], int]
) -> None:
    environ = {
        **_ENVIRON,
        "PS_STATE_POSTGRES_HOST": "127.0.0.1",
        "PS_STATE_POSTGRES_PORT": str(refusing_server("28P01")),
        "PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS": "10",
    }

    started = time.monotonic()
    exit_code = main([], environ)
    elapsed = time.monotonic() - started

    captured = capsys.readouterr()
    assert exit_code == 1
    assert elapsed < 5
    assert "OperationalError" in captured.err
    assert "did not accept connections" not in captured.err
    assert "s3cret-admin-password" not in captured.out + captured.err


def test_provision_raises_the_refusal_without_retrying_when_the_server_refuses_the_login(
    refusing_server: Callable[[str], int],
) -> None:
    target = ProvisioningTarget.from_environ(
        {
            **_ENVIRON,
            "PS_STATE_POSTGRES_HOST": "127.0.0.1",
            "PS_STATE_POSTGRES_PORT": str(refusing_server("28P01")),
            "PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS": "10",
        }
    )

    started = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        provision.provision(target)

    assert time.monotonic() - started < 5


def test_main_keeps_retrying_a_server_that_is_starting_up_until_the_deadline_then_exits_one(
    capsys: pytest.CaptureFixture[str], refusing_server: Callable[[str], int]
) -> None:
    environ = {
        **_ENVIRON,
        "PS_STATE_POSTGRES_HOST": "127.0.0.1",
        "PS_STATE_POSTGRES_PORT": str(refusing_server("57P03")),
        "PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS": "2",
    }

    started = time.monotonic()
    exit_code = main([], environ)
    elapsed = time.monotonic() - started

    captured = capsys.readouterr()
    assert exit_code == 1
    assert elapsed >= 2
    assert "did not accept connections within 2 seconds" in captured.err
    assert "127.0.0.1" not in captured.out + captured.err


def test_provisioning_target_reads_the_lock_timeout_with_a_one_minute_default() -> None:
    explicit = ProvisioningTarget.from_environ(
        {**_ENVIRON, "PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS": "5"}
    )

    assert explicit.lock_timeout_seconds == 5
    assert ProvisioningTarget.from_environ(_ENVIRON).lock_timeout_seconds == 60


@pytest.mark.parametrize("value", ["soon", "0", "-3"])
def test_provisioning_target_rejects_a_non_positive_lock_timeout_by_variable_name(
    value: str,
) -> None:
    environ = {**_ENVIRON, "PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS": value}

    with pytest.raises(StatePostgresProvisioningError, match="PS_STATE_PROVISION_LOCK_TIMEOUT"):
        ProvisioningTarget.from_environ(environ)


def test_parser_description_is_explicit_and_provenance_free() -> None:
    description = provision._build_parser().description  # pyright: ignore[reportPrivateUsage]

    assert description == provision._PARSER_DESCRIPTION  # pyright: ignore[reportPrivateUsage]
    assert description is not None
    assert "ps_state" in description
    assert "PS_STATE_GRAPH_OWNER_ROLE" in description
    assert not re.search(r"#\d+|issue", description, re.IGNORECASE)
    assert description != (provision.__doc__ or "").split("\n\n", 1)[0]
