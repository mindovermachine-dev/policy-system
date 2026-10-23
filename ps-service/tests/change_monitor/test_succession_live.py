"""Live FalkorDB anchor for the frozen succession statements.

The succession's writes are gateway groups; the pre-migration statements live in
`_legacy_reference` and are run raw here, against a throwaway graph, so the semantics the
reference's interpreter models (and the equivalence tests compare against) stay anchored on a real
FalkorDB (notably `MERGE ... SET ... WITH new MERGE`). The REAL `read_reingestion_facts` and
`read_tracked_instruments` read the result back.

    uv run pytest ps-service/tests/change_monitor/test_succession_live.py -m falkordb_live -q
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from change_monitor._legacy_reference import FROZEN_STATEMENTS
from ps_service.change_monitor.falkordb_client import (
    check_connectivity,
    native_graph_name,
)
from ps_service.change_monitor.graph_reader import read_tracked_instruments
from ps_service.change_monitor.succession import read_reingestion_facts
from ps_service.config import load_config
from ps_service.graph_gateway.connection import connect_from_config, select_graph

if TYPE_CHECKING:
    from ps_service.change_monitor.falkordb_client import GraphHandle

pytestmark = pytest.mark.falkordb_live

_SHORT = "SUCCLIVE"
_PRIOR = f"{_SHORT}-1.0"
_NEW = f"{_SHORT}-2.0"
_PROPS = "status: 'active', instrument_type: 'regulation'"


def _rows(graph: GraphHandle, query: str, params: dict[str, object]) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _frozen(graph: GraphHandle, name: str, **params: object) -> list[list[object]]:
    """Run one FROZEN pre-gateway succession statement raw on `graph`."""
    return _rows(graph, FROZEN_STATEMENTS[name], params)


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

        _frozen(graph, "mark_stage", new_id=_NEW, stage="extraction")
        _frozen(graph, "mark_stage", new_id=_NEW, stage="derivation")
        before = read_reingestion_facts(graph, _NEW)
        assert before.node_exists is True
        assert before.prior_id is None
        assert before.marker_stage == "derivation"

        _frozen(graph, "link_and_supersede", prior_id=_PRIOR, new_id=_NEW)
        _frozen(graph, "link_and_supersede", prior_id=_PRIOR, new_id=_NEW)

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

        _frozen(graph, "clear_marker", new_id=_NEW)
        _frozen(graph, "clear_marker", new_id=_NEW)
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
    """Step 2 of the 3-step succession (frozen): the merged graph shows the prior superseded."""
    config = load_config()
    db = connect_from_config(config)
    check_connectivity(db, host=config.falkordb_host, port=config.falkordb_port)
    graph_name = f"{config.falkordb_graph}_succlive"
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

        assert _frozen(graph, "supersede_in_single_tenant", prior_id=_PRIOR, new_id=_NEW) == [
            [_PRIOR]
        ]
        _frozen(graph, "supersede_in_single_tenant", prior_id=_PRIOR, new_id=_NEW)

        assert {node.regulatory_instrument_id for node in read_tracked_instruments(graph)} == {_NEW}
        assert _rows(graph, _LIST_INGESTED_QUERY, {}) == [
            [_PRIOR, "superseded", _NEW],
            [_NEW, "active", None],
        ]
        # a missing node matches no row: the old writer raised on exactly this
        missing = "SUCCLIVE-missing"
        assert _frozen(graph, "supersede_in_single_tenant", prior_id=_PRIOR, new_id=missing) == []
    finally:
        if graph_name in set(db.list_graphs()):
            db.select_graph(graph_name).delete()
