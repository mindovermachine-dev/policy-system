"""Policy Lifecycle ignores `merged` Capability tombstones.

A tombstone must behave exactly like a nonexistent Capability to the draft
claim: the read that feeds `PolicyCapabilityNotFoundError` omits it. The claim
itself is a gateway group, whose write-time guard re-uses that
same read, so a race cannot claim a tombstone either (`test_claim_create_gateway.py`).
Without a live engine, the observable of the read is the statement text (L1
carve-out, `level1-coding-principles.md:71-76`); the live twin is in
`test_governed_by_live.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ps_service.policy_lifecycle.graph_writer import read_capability_governors

_PREDICATE = "coalesce(cap.status,'active') <> 'merged'"


@dataclass
class _Result:
    result_set: list[object]


class _Graph:
    def __init__(self, rows: list[object]) -> None:
        self.queries: list[str] = []
        self._rows = rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        self.queries.append(q)
        return _Result(self._rows)


def test_governor_read_filters_out_merged_tombstones() -> None:
    graph = _Graph([["cap_live", None]])

    governors = read_capability_governors(graph, ("cap_live", "cap_tomb"))

    assert governors == {"cap_live": None}
    assert _PREDICATE in graph.queries[0]
