"""The gateway-owned FalkorDB connection factory and `/ready` probe.

`ps_service.graph_gateway.connection` is the one module that builds a FalkorDB client and probes
connectivity; the gateway package no longer depends on Ingestion or Export for either.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

import ps_service
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.connection import (
    GraphConnectivityError,
    check_connectivity,
    connect,
)

if TYPE_CHECKING:
    from falkordb import FalkorDB

_GATEWAY_ROOT = Path(ps_service.__file__).parent / "graph_gateway"
_PACKAGES_THE_GATEWAY_MUST_NOT_IMPORT = ("ps_service.ingestion", "ps_service.export")


class _RecordingClientFactory:
    """The driver seam: records the host and port the factory hands to the client constructor."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, *, host: str, port: int) -> FalkorDB:
        self.calls.append((host, port))
        return cast("FalkorDB", object())


class _Probe:
    def __init__(self, error: Exception | None) -> None:
        self._error = error

    def list_graphs(self) -> list[str]:
        if self._error is not None:
            raise self._error
        return ["cra_native"]


def test_connect_builds_the_client_with_host_and_port() -> None:
    factory = _RecordingClientFactory()

    client = connect("10.1.2.3", 7001, client_factory=factory)

    assert factory.calls == [("10.1.2.3", 7001)]
    assert client is not None


def test_check_connectivity_marks_falkordb_healthy_on_success() -> None:
    check_connectivity(_Probe(None), host="127.0.0.1", port=6379)

    assert is_healthy(FALKORDB) is True


def test_check_connectivity_marks_unhealthy_and_raises_a_sanitised_error_when_unreachable() -> None:
    with pytest.raises(GraphConnectivityError) as raised:
        check_connectivity(_Probe(ConnectionRefusedError("refused")), host="127.0.0.1", port=6379)

    assert str(raised.value) == (
        "FalkorDB connection failed at 127.0.0.1:6379. Is FalkorDB running? Error: refused"
    )
    assert isinstance(raised.value.__cause__, ConnectionRefusedError)
    assert is_healthy(FALKORDB) is False


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_the_gateway_package_imports_nothing_from_ingestion_or_export() -> None:
    offenders = sorted(
        f"{path.name}: {module}"
        for path in _GATEWAY_ROOT.rglob("*.py")
        for module in _imported_modules(path)
        if module.startswith(_PACKAGES_THE_GATEWAY_MUST_NOT_IMPORT)
    )

    assert offenders == []


@pytest.mark.parametrize("module", ["ps_service.main", "ps_service.graph_gateway"])
def test_each_front_door_imports_in_a_fresh_interpreter(module: str) -> None:
    done = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter
        [sys.executable, "-I", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
