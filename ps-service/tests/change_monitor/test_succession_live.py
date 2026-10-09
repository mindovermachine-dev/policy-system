"""Live FalkorDB smoke for the #201 succession statements (CHANGES F9).

Runs the REAL `read_reingestion_facts`, `mark_stage_complete`, `link_and_supersede` and
`clear_marker` against a throwaway graph so the Cypher the in-memory ledger double only
models is proven to parse and behave on FalkorDB (notably `MERGE ... SET ... WITH new MERGE`).

    uv run pytest ps-service/tests/change_monitor/test_succession_live.py -m falkordb_live -q
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ps_service.change_monitor.errors import ChangeMonitorStateError
from ps_service.change_monitor.falkordb_client import (
    check_connectivity,
    connect_from_config,
    native_graph_name,
    select_graph,
    single_tenant_graph_name,
)
from ps_service.change_monitor.graph_reader import read_tracked_instruments
from ps_service.change_monitor.succession import (
    clear_marker,
    link_and_supersede,
    mark_stage_complete,
    read_reingestion_facts,
    supersede_in_single_tenant,
)
from ps_service.config import load_config

if TYPE_CHECKING:
    from ps_service.change_monitor.falkordb_client import GraphHandle

pytestmark = pytest.mark.falkordb_live

_SHORT = "SUCCLIVE"
_PRIOR = f"{_SHORT}-1.0"
_NEW = f"{_SHORT}-2.0"
_PROPS = "status: 'active', instrument_type: 'regulation'"


def _rows(graph: GraphHandle, query: str, params: dict[str, object]) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def test_link_and_supersede_marker_and_facts_round_trip_on_real_falkordb() -> None:
    config = load_config()
    db = connect_from_config(config)
    check_connectivity(db, host=config.falkordb_host, port=config.falkordb_port)
    graph_name = native_graph_name(_SHORT)
    if graph_name in set(db.list_graphs()):
        db.select_graph(graph_name).delete()
    try:
        graph = select_graph(db, graph_name)
        graph.query(
            f"CREATE (:RegulatoryInstrument {{id: $p, {_PROPS}}}), "
            f"(:RegulatoryInstrument {{id: $n, {_PROPS}}})",
            params={"p": _PRIOR, "n": _NEW},
        )

        mark_stage_complete(graph, _NEW, "extraction")
        mark_stage_complete(graph, _NEW, "derivation")
        before = read_reingestion_facts(graph, _NEW)
        assert before.node_exists is True
        assert before.prior_id is None
        assert before.marker_stage == "derivation"

        link_and_supersede(graph, _PRIOR, _NEW)
        link_and_supersede(graph, _PRIOR, _NEW)

        linked = read_reingestion_facts(graph, _NEW)
        assert linked.prior_id == _PRIOR
        assert linked.prior_status == "superseded"
        assert linked.absorbed is True
        assert linked.marker_stage == "linked"
        edges = _rows(
            graph,
            "MATCH (:RegulatoryInstrument {id: $p})-[e:SUPERSEDED_BY]->"
            "(:RegulatoryInstrument {id: $n}) RETURN count(e)",
            {"p": _PRIOR, "n": _NEW},
        )
        assert edges == [[1]]

        clear_marker(graph, _NEW)
        clear_marker(graph, _NEW)
        done = read_reingestion_facts(graph, _NEW)
        assert done.marker_stage is None
        assert done.absorbed is True
    finally:
        if graph_name in set(db.list_graphs()):
            db.select_graph(graph_name).delete()


_LIST_INGESTED_QUERY = (
    "MATCH (ri:RegulatoryInstrument) OPTIONAL MATCH (ri)-[:SUPERSEDED_BY]->(succ) "
    "RETURN ri.id AS id, ri.status AS status, succ.id AS superseded_by ORDER BY ri.id"
)


def test_policy_system_succession_drops_the_prior_from_the_tracked_set_on_real_falkordb() -> None:
    """Step 2 of the 3-step succession: the merged graph shows the prior superseded."""
    config = load_config()
    db = connect_from_config(config)
    check_connectivity(db, host=config.falkordb_host, port=config.falkordb_port)
    graph_name = f"{single_tenant_graph_name()}_succlive"
    if graph_name in set(db.list_graphs()):
        db.select_graph(graph_name).delete()
    try:
        graph = select_graph(db, graph_name)
        graph.query(
            "CREATE (:RegulatoryInstrument {id: $p, status: 'active', source_type: 'external', "
            "instrument_type: 'regulation', celex: 'C1', effective_date: '2024-01-01'}), "
            "(:RegulatoryInstrument {id: $n, status: 'active', source_type: 'external', "
            "instrument_type: 'regulation', celex: 'C2', effective_date: '2025-01-01'})",
            params={"p": _PRIOR, "n": _NEW},
        )
        assert {node.regulatory_instrument_id for node in read_tracked_instruments(graph)} == {
            _PRIOR,
            _NEW,
        }

        supersede_in_single_tenant(graph, _PRIOR, _NEW)
        supersede_in_single_tenant(graph, _PRIOR, _NEW)

        assert {node.regulatory_instrument_id for node in read_tracked_instruments(graph)} == {_NEW}
        assert _rows(graph, _LIST_INGESTED_QUERY, {}) == [
            [_PRIOR, "superseded", _NEW],
            [_NEW, "active", None],
        ]
        with pytest.raises(ChangeMonitorStateError):
            supersede_in_single_tenant(graph, _PRIOR, "SUCCLIVE-missing")
    finally:
        if graph_name in set(db.list_graphs()):
            db.select_graph(graph_name).delete()
