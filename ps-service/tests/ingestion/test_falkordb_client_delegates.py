"""`ps_service.ingestion.falkordb_client` delegates to the gateway factory."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ps_service.config import load_config
from ps_service.ingestion import falkordb_client
from ps_service.ingestion.errors import IngestionConfigurationError
from ps_service.ingestion.falkordb_client import (
    FalkorDBConnectionError,
    check_connectivity_from_config,
)

if TYPE_CHECKING:
    from falkordb import FalkorDB


def test_the_connectivity_probe_still_raises_the_ingestion_error_with_the_same_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreachable(
        host: str, port: int, *, socket_connect_timeout: float, socket_timeout: float
    ) -> object:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(falkordb_client, "connect", _unreachable)
    config = load_config()

    with pytest.raises(FalkorDBConnectionError) as raised:
        check_connectivity_from_config(config)

    assert isinstance(raised.value, IngestionConfigurationError)
    assert str(raised.value) == (
        f"FalkorDB connection failed at {config.falkordb_host}:{config.falkordb_port}. "
        "Is FalkorDB running? Error: refused"
    )
    assert isinstance(raised.value.__cause__, ConnectionRefusedError)


class _FakeDatabase:
    def select_graph(self, name: str) -> str:
        return f"graph:{name}"


def test_select_graph_hands_back_the_graph_the_database_selects() -> None:
    database = _FakeDatabase()

    assert (
        falkordb_client.select_graph(cast("FalkorDB", database), "cra_native") == "graph:cra_native"
    )
