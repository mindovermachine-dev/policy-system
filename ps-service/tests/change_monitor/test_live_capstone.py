"""Live end-to-end capstone for issue #19 (PLAN_REVIEWED.md §3 test 21,
§1.4; PLAN_A.md §1.5) — run for real against the live CELLAR/ELI service
(`publications.europa.eu`) and a real, reachable FalkorDB instance.

`@pytest.mark.cellar_live @pytest.mark.falkordb_live` on every test in this
module (via `pytestmark`): these tests make real network calls and write
real data to FalkorDB — they are excluded from the fast regression suite
(`-m "not cellar_live and not falkordb_live"`) and must be run explicitly:

    uv run pytest ps-service/tests/change_monitor/test_live_capstone.py \
        -m "cellar_live and falkordb_live" -q

What the capstone proves (PLAN_A.md §1.5 — full-fidelity consolidated
re-ingestion, Follow-on A folded into #19):

- **AC-001**: `fetch_consolidated_versions` genuinely reaches the CELLAR
  SPARQL endpoint and returns the real consolidated-expression state.
- **Consolidated re-ingestion end-to-end**: a real `ingest_regulatory_instrument`
  seed of GDPR's *base act* (`32016R0679`, 372 PARAGRAPH / 173 RECITAL),
  followed by a real `trigger_reingestion` of its *consolidated expression*
  (`02016R0679-20160504`), lands a fully-populated new-version structural
  subtree (372 PARAGRAPH, 0 RECITAL — the preamble is legitimately absent
  from a consolidation), writes the single `SUPERSEDED_BY` edge and the
  prior's `superseded` status, and leaves the prior version's subtree
  intact (both versions coexist in `{short}_native`, subtrees scoped by
  their `{SHORT}-{VERSION}#` node-id prefix). A repeat call is a clean
  no-op (`already_processed`).

Module-level constants (not conditionals) identify the base act to seed
with. `_CAPSTONE_SHORT` is a deliberately unique throwaway short name so
the `{short}_native` graph this test seeds and cleans is its own, never
colliding with a real seeded graph (`cra_native` / `gdpr_native` / ...).
The AC-011 "no regulation literal in a conditional" scan covers
`change_monitor/*` source only, not this tests directory, but nothing
here branches on these values regardless.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ps_service.change_monitor.cellar_consolidated import fetch_consolidated_versions
from ps_service.change_monitor.falkordb_client import (
    check_connectivity,
    connect_from_config,
    native_graph_name,
    select_graph,
)
from ps_service.change_monitor.trigger import trigger_reingestion
from ps_service.config import load_config
from ps_service.ingestion.adapters.cellar_eli import CellarEliAdapter
from ps_service.ingestion.pipeline import ingest_regulatory_instrument
from ps_service.logging import EmitterConfig, LogEmitter

if TYPE_CHECKING:
    from ps_service.change_monitor.falkordb_client import GraphHandle

pytestmark = [pytest.mark.cellar_live, pytest.mark.falkordb_live]

# GDPR's base-act CELEX: one consolidated expression, `02016R0679-20160504`,
# stable since 2016 — a fixed, well-understood live target for AC-001.
_BASE_CELEX = "32016R0679"
_LATEST_CONSOLIDATED_CELEX = "02016R0679-20160504"

# A throwaway short name unique to this test, so `cmcap_native` belongs to
# this capstone alone.
_CAPSTONE_SHORT = "CMCAP"
_SEED_VERSION = "1.0"
_NEW_VERSION = "2.0"
_SEED_ID = f"{_CAPSTONE_SHORT}-{_SEED_VERSION}"
_NEW_ID = f"{_CAPSTONE_SHORT}-{_NEW_VERSION}"


def _scalar(graph: GraphHandle, query: str, params: dict[str, object] | None = None) -> object:
    """Return the single cell of a one-row / one-column query, or `None`."""
    rows = cast("list[list[object]]", graph.query(query, params=params).result_set)
    if not rows:
        return None
    return rows[0][0]


def _status_of(graph: GraphHandle, instrument_id: str) -> object:
    """The `status` property of the `RegulatoryInstrument` node, or `None`."""
    return _scalar(
        graph,
        "MATCH (n:RegulatoryInstrument {id: $id}) RETURN n.status",
        {"id": instrument_id},
    )


def _node_and_edge_totals(graph: GraphHandle) -> tuple[int, int]:
    """The graph's total node count and total relationship count."""
    node_count = cast("int", _scalar(graph, "MATCH (n) RETURN count(n)"))
    edge_count = cast("int", _scalar(graph, "MATCH ()-[r]->() RETURN count(r)"))
    return node_count, edge_count


def _reachable_count(graph: GraphHandle, instrument_id: str, label: str) -> int:
    """How many `label` nodes are reachable from the given instrument via
    `HAS` edges — i.e. belong to that version's structural subtree.
    """
    return cast(
        "int",
        _scalar(
            graph,
            f"MATCH (:RegulatoryInstrument {{id: $id}})-[:HAS*1..]->(n:{label}) RETURN count(n)",
            {"id": instrument_id},
        ),
    )


def _reachable_node_ids(graph: GraphHandle, instrument_id: str, label: str) -> list[str]:
    """The ids of every `label` node in the given instrument's subtree."""
    rows = cast(
        "list[list[object]]",
        graph.query(
            f"MATCH (:RegulatoryInstrument {{id: $id}})-[:HAS*1..]->(n:{label}) RETURN n.id",
            params={"id": instrument_id},
        ).result_set,
    )
    return [cast("str", row[0]) for row in rows]


def test_ac001_live_consolidated_version_detection() -> None:
    """AC-001: the real CELLAR SPARQL endpoint reports GDPR's one
    consolidated expression, `02016R0679-20160504`.

    Overlaps `test_cellar_consolidated.py::test_ac001_live_consolidated_detection`
    (Increment 4c) by design — kept here as the first beat of the capstone
    story so the end-to-end proof is readable in one place.
    """
    info = fetch_consolidated_versions(_BASE_CELEX)

    assert info.base_celex == _BASE_CELEX
    assert info.latest_celex == _LATEST_CONSOLIDATED_CELEX


def test_consolidated_reingestion_end_to_end(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Seed a throwaway `{short}_native` graph with a real base-act ingest,
    then run a real `trigger_reingestion` of the *consolidated expression*
    and assert the full re-ingestion outcome (PLAN_A.md §1.5).

    Asserts:

    - `ReingestionOutcome.outcome == "superseded"`, non-empty `run_id`,
      prior / new ids correct.
    - `CMCAP-2.0` exists `status='active' version='2.0'`.
    - `CMCAP-2.0`'s subtree holds the consolidated structure: 372 PARAGRAPH
      reachable, every id prefixed `02016R0679-20160504#` (the consolidated
      CELEX), and 0 RECITAL (the preamble is absent from a consolidation).
    - `CMCAP-1.0`'s subtree is untouched: 173 RECITAL and 372 PARAGRAPH
      still reachable, every id prefixed `32016R0679#` (the base-act CELEX)
      — the two versions coexist.
    - Exactly one `SUPERSEDED_BY` edge `CMCAP-1.0 -> CMCAP-2.0`;
      `CMCAP-1.0.status == 'superseded'`.
    - Re-running with identical args is a clean no-op (`already_processed`,
      `run_id is None`, node + edge totals unchanged).
    """
    config = load_config()
    db = connect_from_config(config)
    check_connectivity(db, host=config.falkordb_host, port=config.falkordb_port)

    graph_name = native_graph_name(_CAPSTONE_SHORT)
    log_path = tmp_path_factory.mktemp("capstone_live") / "capstone_live.jsonl"
    emitter = LogEmitter(EmitterConfig(log_path=log_path))
    adapter = CellarEliAdapter()

    if graph_name in set(db.list_graphs()):
        db.select_graph(graph_name).delete()
    try:
        graph = select_graph(db, graph_name)

        # --- seed: real live ingest of the base act as version 1.0 --------
        ingest_regulatory_instrument(
            _BASE_CELEX,
            _CAPSTONE_SHORT,
            version=_SEED_VERSION,
            adapter=adapter,
            graph=graph,
            emitter=emitter,
        )
        assert _status_of(graph, _SEED_ID) == "active"
        seed_recitals = _reachable_count(graph, _SEED_ID, "RECITAL")
        seed_paragraphs = _reachable_count(graph, _SEED_ID, "PARAGRAPH")
        assert seed_recitals == 173
        assert seed_paragraphs == 372

        # --- trigger: real live re-ingest of the CONSOLIDATED text as 2.0 -
        outcome = trigger_reingestion(
            _LATEST_CONSOLIDATED_CELEX,
            _CAPSTONE_SHORT,
            _NEW_VERSION,
            adapter=adapter,
            graph=graph,
            emitter=emitter,
        )

        assert outcome.outcome == "superseded"
        assert isinstance(outcome.run_id, str)
        assert outcome.run_id
        assert outcome.prior_regulatory_instrument_id == _SEED_ID
        assert outcome.new_regulatory_instrument_id == _NEW_ID

        new_status, new_version = cast(
            "list[list[object]]",
            graph.query(
                "MATCH (n:RegulatoryInstrument {id: $id}) RETURN n.status, n.version",
                params={"id": _NEW_ID},
            ).result_set,
        )[0]
        assert new_status == "active"
        assert new_version == _NEW_VERSION

        # --- new version carries the consolidated structural subtree ------
        # Structural node ids are prefixed with the raw CELEX the adapter
        # was called with (never the `{SHORT}-{VERSION}` node id) — see
        # graph_writer's "structural node id prefixing" docstring — so the
        # base-act and consolidated subtrees never collide.
        new_paragraph_ids = _reachable_node_ids(graph, _NEW_ID, "PARAGRAPH")
        assert len(new_paragraph_ids) == 372
        assert all(pid.startswith(f"{_LATEST_CONSOLIDATED_CELEX}#") for pid in new_paragraph_ids)
        assert _reachable_count(graph, _NEW_ID, "RECITAL") == 0

        # --- prior version's subtree is untouched: the two coexist -------
        prior_paragraph_ids = _reachable_node_ids(graph, _SEED_ID, "PARAGRAPH")
        assert len(prior_paragraph_ids) == seed_paragraphs
        assert all(pid.startswith(f"{_BASE_CELEX}#") for pid in prior_paragraph_ids)
        assert _reachable_count(graph, _SEED_ID, "RECITAL") == seed_recitals

        assert _status_of(graph, _SEED_ID) == "superseded"

        edge_count = _scalar(
            graph,
            "MATCH (:RegulatoryInstrument {id: $prior})-[r:SUPERSEDED_BY]->"
            "(:RegulatoryInstrument {id: $new}) RETURN count(r)",
            {"prior": _SEED_ID, "new": _NEW_ID},
        )
        assert edge_count == 1

        totals_after_trigger = _node_and_edge_totals(graph)

        # --- re-run: identical args -> clean no-op ------------------------
        rerun = trigger_reingestion(
            _LATEST_CONSOLIDATED_CELEX,
            _CAPSTONE_SHORT,
            _NEW_VERSION,
            adapter=adapter,
            graph=graph,
            emitter=emitter,
        )

        assert rerun.outcome == "already_processed"
        assert rerun.run_id is None

        rerun_edge_count = _scalar(
            graph,
            "MATCH (:RegulatoryInstrument {id: $prior})-[r:SUPERSEDED_BY]->"
            "(:RegulatoryInstrument {id: $new}) RETURN count(r)",
            {"prior": _SEED_ID, "new": _NEW_ID},
        )
        assert rerun_edge_count == 1
        assert _node_and_edge_totals(graph) == totals_after_trigger
    finally:
        emitter.flush()
        if graph_name in set(db.list_graphs()):
            db.select_graph(graph_name).delete()
