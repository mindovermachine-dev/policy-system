"""Fast tests for the `graph_gateway` provisioning CLI's environment handling (issue #205 slice 1).

The database side (it really provisions, and never prints the admin password) is covered by
`test_provision_live.py`.
"""

from __future__ import annotations

import socket
import time

import pytest

from ps_service.graph_gateway.provision import ProvisioningTarget, main
from ps_service.persistence import StatePostgresProvisioningError

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
