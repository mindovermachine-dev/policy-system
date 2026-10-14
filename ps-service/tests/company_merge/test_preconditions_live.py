"""Live regression test for `check_obligation_has_edge_cardinality`.

`@pytest.mark.falkordb_live`, strictly read-only -- issues only the one
`MATCH (o:Obligation) OPTIONAL MATCH (:Role)-[h:HAS]->(o) RETURN o.id,
count(h)` query per graph, no write of any kind. Runs the real function
against the three real, currently-mapped `{short}_baseline` graphs
(`cra_baseline`, `gdpr_baseline`, `nis2_baseline`, selected via
`ps_service.domain_mapper.falkordb_client.baseline_graph_name`), asserting
zero violations on each as a re-runnable automated regression check. Safe to
run at any time, with no gating.
"""

from __future__ import annotations

import pytest

from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.company_merge.preconditions import check_obligation_has_edge_cardinality
from ps_service.config import load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name

_CURRENTLY_MAPPED_SHORT_NAMES = ("CRA", "GDPR", "NIS2")


@pytest.mark.falkordb_live
@pytest.mark.parametrize("short_name", _CURRENTLY_MAPPED_SHORT_NAMES)
def test_currently_mapped_baseline_graph_has_zero_obligation_has_edge_violations(
    short_name: str,
) -> None:
    config = load_config()
    db = connect_from_config(config)
    baseline_graph = select_graph(db, baseline_graph_name(short_name))

    violations = check_obligation_has_edge_cardinality(baseline_graph)

    assert violations == (), (
        f"{short_name}_baseline: {len(violations)} Obligation(s) with HAS-edge "
        f"count != 1: {violations}"
    )
