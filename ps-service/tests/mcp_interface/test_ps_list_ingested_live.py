"""Live FalkorDB proof of the `ps-list-ingested` fixed query (issue #77).

The Cypher is extracted from SKILL.md so the skill text and the tested query cannot
drift, and is run through the real `handle_mcp_tool_call`. UNVERIFIED where no FalkorDB
is available (collected, deselected by default).

Run with:
`uv run pytest ps-service/tests/mcp_interface/test_ps_list_ingested_live.py \
    -m falkordb_live -q`
Uses a throwaway graph, deleted before and after each test.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

import pytest

from ps_service.ingestion.falkordb_client import FalkorDB, connect, select_graph
from ps_service.ingestion.graph_writer import register_regulatory_instrument_version
from ps_service.ingestion.models import RegulatoryInstrumentMetadata
from ps_service.mcp_interface.mcp_server import handle_mcp_tool_call
from ps_service.query_engine import falkordb_client as query_engine_client
from ps_service.query_engine.cypher_query import (
    _GRAPH_UNSEEDED_DETAIL,  # pyright: ignore[reportPrivateUsage]  # test pins the exact error text the skill must name
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.ingestion.falkordb_client import GraphHandle

pytestmark = pytest.mark.falkordb_live

_GRAPH = "ps_list_ingested_live_test"
_SKILL = (
    Path(__file__).resolve().parents[3]
    / "ps-skills"
    / "ps-plugin"
    / "skills"
    / "ps-list-ingested"
    / "SKILL.md"
)
_COLUMNS = [
    "id",
    "celex",
    "title",
    "source_type",
    "instrument_type",
    "jurisdiction",
    "effective_date",
    "version",
    "status",
    "superseded_by",
]


def _skill_query() -> str:
    match = re.search(r"```cypher\n(.*?)```", _SKILL.read_text(encoding="utf-8"), re.DOTALL)
    assert match is not None, "SKILL.md has no fenced cypher block"
    return match.group(1).strip()


def _delete(db: FalkorDB) -> None:
    if _GRAPH in db.list_graphs():
        db.select_graph(_GRAPH).delete()


class _Graphs(NamedTuple):
    """The one throwaway graph, as each component's own handle type sees it."""

    seed: GraphHandle
    read: query_engine_client.GraphHandle


@pytest.fixture
def graph() -> Iterator[_Graphs]:
    db = connect(host="127.0.0.1", port=6379)
    _delete(db)
    yield _Graphs(
        seed=select_graph(db, _GRAPH),
        read=query_engine_client.select_graph(
            query_engine_client.connect(host="127.0.0.1", port=6379), _GRAPH
        ),
    )
    _delete(db)


def _seed(
    graph: GraphHandle,
    ident: str,
    *,
    version: str,
    status: str,
    source_type: str = "external",
    instrument_type: str | None = None,
    jurisdiction: str = "EU",
    celex: str | None = None,
) -> None:
    register_regulatory_instrument_version(
        graph,
        ident,
        RegulatoryInstrumentMetadata.model_validate(
            {
                "title": f"Title of {ident}",
                "jurisdiction": jurisdiction,
                "effective_date": date(2024, 1, 1),
                "version": version,
                "status": status,
                "source_type": source_type,
                "instrument_type": instrument_type,
                "celex": celex,
            }
        ),
    )


def _run(graph: query_engine_client.GraphHandle) -> list[dict[str, object]]:
    result = handle_mcp_tool_call(_skill_query(), graph=graph, timeout_ms=5000, row_cap=1000)
    assert isinstance(result, dict)
    columns = cast("list[str]", result["columns"])
    return [
        dict(zip(columns, row, strict=True)) for row in cast("list[list[object]]", result["rows"])
    ]


def _counts(graph: GraphHandle) -> tuple[int, int]:
    nodes = cast("list[list[int]]", graph.query("MATCH (n) RETURN count(n)").result_set)
    edges = cast("list[list[int]]", graph.query("MATCH ()-[r]->() RETURN count(r)").result_set)
    return nodes[0][0], edges[0][0]


def _link(graph: GraphHandle, prior: str, successor: str) -> None:
    graph.query(
        "MATCH (a:RegulatoryInstrument {id: $a}), (b:RegulatoryInstrument {id: $b}) "
        "CREATE (a)-[:SUPERSEDED_BY]->(b)",
        params={"a": prior, "b": successor},
    )


def test_skill_query_lists_every_version_ordered_with_blanks_and_successor_link(
    graph: _Graphs,
) -> None:
    _seed(
        graph.seed,
        "ENGPRAC-1.0",
        version="1.0",
        status="active",
        source_type="internal",
        jurisdiction="",
    )
    _seed(graph.seed, "CMCAP-2.0", version="2.0", status="active", instrument_type="regulation")
    _seed(graph.seed, "CMCAP-1.0", version="1.0", status="superseded", instrument_type="regulation")
    _seed(graph.seed, "CRA-1.0", version="1.0", status="active", celex="32024R2847")
    _link(graph.seed, "CMCAP-1.0", "CMCAP-2.0")
    before = _counts(graph.seed)

    rows = _run(graph.read)

    assert [r["id"] for r in rows] == ["CMCAP-1.0", "CMCAP-2.0", "CRA-1.0", "ENGPRAC-1.0"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["CMCAP-1.0"]["status"] == "superseded"
    assert by_id["CMCAP-1.0"]["superseded_by"] == "CMCAP-2.0"
    assert by_id["CMCAP-2.0"]["status"] == "active"
    assert by_id["CMCAP-2.0"]["superseded_by"] is None
    assert by_id["ENGPRAC-1.0"]["celex"] is None
    assert by_id["ENGPRAC-1.0"]["instrument_type"] is None
    assert by_id["CRA-1.0"]["celex"] == "32024R2847"
    assert _counts(graph.seed) == before


def test_skill_query_returns_one_row_per_node_when_no_superseded_by_edge_exists(
    graph: _Graphs,
) -> None:
    for ident in ("A-1.0", "B-1.0", "C-1.0"):
        _seed(graph.seed, ident, version="1.0", status="active")

    rows = _run(graph.read)

    assert [r["id"] for r in rows] == ["A-1.0", "B-1.0", "C-1.0"]
    assert all(r["superseded_by"] is None for r in rows)


def test_skill_query_returns_one_ten_column_row_for_a_seeded_instrument(
    graph: _Graphs,
) -> None:
    register_regulatory_instrument_version(
        graph.seed,
        "CRA-1.0",
        RegulatoryInstrumentMetadata(
            title="Cyber Resilience Act",
            jurisdiction="EU",
            effective_date=date(2024, 10, 23),
            version="1.0",
            status="active",
            source_type="external",
            instrument_type="regulation",
            celex="32024R2847",
        ),
    )

    result = handle_mcp_tool_call(_skill_query(), graph=graph.read, timeout_ms=5000, row_cap=1000)

    assert isinstance(result, dict)
    assert result["columns"] == _COLUMNS
    rows = cast("list[list[object]]", result["rows"])
    assert len(rows) == 1
    assert rows[0][0] == "CRA-1.0"


def test_empty_graph_returns_the_unseeded_error_the_skill_names(graph: _Graphs) -> None:
    result = handle_mcp_tool_call(_skill_query(), graph=graph.read, timeout_ms=5000, row_cap=1000)

    assert result == f"error: {_GRAPH_UNSEEDED_DETAIL}"
    assert _GRAPH_UNSEEDED_DETAIL in _SKILL.read_text(encoding="utf-8")


def test_seeded_graph_without_instruments_is_an_empty_success_not_an_error(
    graph: _Graphs,
) -> None:
    graph.seed.query("CREATE (:Capability {id: 'cap-1'})")

    result = handle_mcp_tool_call(_skill_query(), graph=graph.read, timeout_ms=5000, row_cap=1000)

    assert isinstance(result, dict)
    assert result["rows"] == []
