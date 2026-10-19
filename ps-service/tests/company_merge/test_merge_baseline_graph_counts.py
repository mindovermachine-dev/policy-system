"""`merge_baseline_graph` reports net-new / matched Capability counts and the new-Obligation count.

`MergeResult.new_capability_count`, `matched_capability_count` and
`new_obligation_count` are facts derived from the dedup resolutions and one pre-write existence
probe (never driver statistics: `GraphQueryResult` exposes only `result_set`). They default to 0 so
every pre-existing `MergeResult(...)` construction stays valid.

Fakes are self-contained and stateful (this package's convention): the single-tenant fake records
Obligation/Capability mints so a second merge of the same baseline sees what the first wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from company_merge._thresholds import REVIEW_THRESHOLD
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import MergeResult
from ps_service.domain_mapper.identity import obligation_id
from ps_service.logging import configure

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import MergeRig

_THRESHOLD = 0.85
_ROLE = "role_ops"
_VEC_A = [1.0, 0.0]
_VEC_B = [0.0, 1.0]
_VEC_MIX = [0.5, 0.5]

_OBL_NEW_1 = obligation_id(_ROLE, "Report incidents.")
_OBL_NEW_2 = obligation_id(_ROLE, "Keep records.")
_OBL_EXISTING = obligation_id(_ROLE, "Assess risk.")
_OBL_ABSORBED = obligation_id(_ROLE, "Report incidents promptly.")
_OBL_SURVIVOR = obligation_id(_ROLE, "Report incidents")

_CAP_EXACT = "cap_exact"
_CAP_SEMANTIC_EXISTING = "cap_semantic_existing"
_CAP_SEMANTIC_INCOMING = "cap_semantic_incoming"
_CAP_NEW = "cap_new"
_CAP_TOMBSTONE = "cap_tombstone"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


class _Result:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


class _InstrumentNode:
    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _Baseline:
    """A baseline graph with one Role, the given Obligations and the given Capabilities."""

    def __init__(
        self,
        *,
        obligations: tuple[tuple[str, str], ...],
        capabilities: tuple[tuple[str, str, list[float]], ...] = (),
    ) -> None:
        self._obligations = obligations
        self._capabilities = capabilities

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        if "[:HAS]" in q:
            return _Result([[_ROLE, oid] for oid, _ in self._obligations])
        if any(
            f"(n:{label})" in q
            for label in ("Policy", "Standard", "Control", "PracticeArea", "RiskPath")
        ):
            return _Result([])
        if "n.description" in q:
            return _Result([[cid, name, 0.8, None, vec] for cid, name, vec in self._capabilities])
        if "(n:Obligation) RETURN" in q:
            return _Result([[oid, text, 0.9] for oid, text in self._obligations])
        if "n.name, n.confidence" in q:
            return _Result([[_ROLE, "Operator", 0.9]])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _Result([[_InstrumentNode({"id": "REG-C", "title": "Counts"})]])
        return _Result([])


class _SingleTenant:
    """Stateful single-tenant fake: records Obligation / Capability mints and every call."""

    def __init__(
        self,
        *,
        obligations: frozenset[str] = frozenset(),
        capability_rows: tuple[tuple[str, str, list[float]], ...] = (),
        tombstones: dict[str, str] | None = None,
        markers: dict[str, str] | None = None,
        gateway_graph: InMemoryGraph | None = None,
    ) -> None:
        # Obligations a merge submits through the gateway land in the rig's
        # `policy_system` graph; the existence reads see them there.
        self._gateway_graph = gateway_graph
        self.gateway_obligations_at_probe: list[set[str]] = []
        self.obligations: set[str] = set(obligations)
        self.capabilities: dict[str, list[object]] = {
            cid: [cid, name, vec] for cid, name, vec in capability_rows
        }
        self._tombstones = tombstones or {}
        self._markers = markers or {}
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.calls.append((q, params))
        if "MERGED_INTO" in q:
            return _Result([[k, v] for k, v in self._tombstones.items()])
        if "(m:Merged" in q:
            return _Result([[k, v] for k, v in self._markers.items()])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _Result(self._capability_index_rows())
        if "Obligation) WHERE" in q and "$ids" in q:
            assert params is not None
            ids = cast("list[str]", params["ids"])
            held = self.obligations | self._gateway_obligations()
            if "(o:Obligation)" in q:
                self.gateway_obligations_at_probe.append(self._gateway_obligations())
            return _Result([[i] for i in ids if i in held])
        return _Result([])

    def _capability_index_rows(self) -> list[object]:
        """`[id, name, embedding]` of the seeded Capabilities plus those the gateway has minted."""
        rows: dict[str, list[object]] = {key: list(row) for key, row in self.capabilities.items()}
        if self._gateway_graph is not None:
            for (label, node_id), props in self._gateway_graph.nodes.items():
                if label == "Capability" and node_id not in rows:
                    rows[node_id] = [node_id, props.get("name"), props.get("embedding")]
        return list(rows.values())

    def _gateway_obligations(self) -> set[str]:
        if self._gateway_graph is None:
            return set()
        return {node_id for label, node_id in self._gateway_graph.nodes if label == "Obligation"}

    def matching(self, fragment: str) -> list[tuple[str, dict[str, object] | None]]:
        return [c for c in self.calls if fragment in c[0]]


def _merge(
    baseline: _Baseline,
    single_tenant: _SingleTenant,
    emitter: object | None,
    merge_rig: MergeRig,
) -> MergeResult:
    return merge_baseline_graph(
        "REG-C",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model="fake-embed-model",
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        **merge_rig.merge_keywords(),
    )


def _mixed_capabilities() -> tuple[tuple[str, str, list[float]], ...]:
    """Incoming: one exact, one semantic, one redirected (tombstone), one new."""
    return (
        (_CAP_EXACT, "Exact", _VEC_MIX),
        (_CAP_SEMANTIC_INCOMING, "Semantic twin", _VEC_A),
        (_CAP_TOMBSTONE, "Old name", _VEC_MIX),
        (_CAP_NEW, "Brand new", _VEC_B),
    )


def _mixed_single_tenant() -> _SingleTenant:
    return _SingleTenant(
        capability_rows=(
            (_CAP_EXACT, "Exact", _VEC_MIX),
            (_CAP_SEMANTIC_EXISTING, "Semantic original", _VEC_A),
            (_CAP_TOMBSTONE, "Old name", _VEC_MIX),
        ),
        tombstones={_CAP_TOMBSTONE: _CAP_EXACT},
    )


def test_merge_result_counts_default_to_zero() -> None:
    result = MergeResult(
        regulatory_instrument_id="REG-C",
        obligation_ids=(),
        capability_canonical_ids=(),
        near_misses=(),
    )

    assert (
        result.new_capability_count,
        result.matched_capability_count,
        result.new_obligation_count,
    ) == (0, 0, 0)


def test_new_capability_count_counts_only_new_resolutions(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    emitter, _ = make_emitter()
    baseline = _Baseline(obligations=(), capabilities=_mixed_capabilities())

    result = _merge(baseline, _mixed_single_tenant(), emitter, merge_rig)

    assert result.new_capability_count == 1


def test_matched_capability_count_counts_exact_semantic_and_redirected(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    emitter, _ = make_emitter()
    baseline = _Baseline(obligations=(), capabilities=_mixed_capabilities())

    result = _merge(baseline, _mixed_single_tenant(), emitter, merge_rig)

    assert result.matched_capability_count == 3
    assert result.new_capability_count + result.matched_capability_count == 4


def test_new_obligation_count_excludes_obligations_already_in_the_single_tenant_graph(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    emitter, _ = make_emitter()
    baseline = _Baseline(
        obligations=(
            (_OBL_NEW_1, "Report incidents."),
            (_OBL_NEW_2, "Keep records."),
            (_OBL_EXISTING, "Assess risk."),
        )
    )
    single_tenant = _SingleTenant(obligations=frozenset({_OBL_EXISTING}))

    result = _merge(baseline, single_tenant, emitter, merge_rig)

    assert result.new_obligation_count == 2
    assert len(result.obligation_ids) == 3


def test_new_obligation_count_excludes_obligations_dropped_by_cleanup_redirects(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    emitter, _ = make_emitter()
    baseline = _Baseline(
        obligations=((_OBL_ABSORBED, "Report incidents promptly."), (_OBL_NEW_2, "Keep records."))
    )
    single_tenant = _SingleTenant(
        obligations=frozenset({_OBL_SURVIVOR}), markers={_OBL_ABSORBED: _OBL_SURVIVOR}
    )

    result = _merge(baseline, single_tenant, emitter, merge_rig)

    assert result.obligation_ids == (_OBL_NEW_2,)
    assert result.new_obligation_count == 1


def test_second_merge_of_the_same_baseline_reports_zero_new_obligations_and_zero_new_capabilities(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    emitter, _ = make_emitter()
    baseline = _Baseline(
        obligations=((_OBL_NEW_1, "Report incidents."), (_OBL_NEW_2, "Keep records.")),
        capabilities=((_CAP_NEW, "Brand new", _VEC_B), (_CAP_EXACT, "Exact", _VEC_MIX)),
    )
    single_tenant = _SingleTenant(
        capability_rows=((_CAP_EXACT, "Exact", _VEC_MIX),), gateway_graph=merge_rig.policy_system
    )

    first = _merge(baseline, single_tenant, emitter, merge_rig)
    second = _merge(baseline, single_tenant, emitter, merge_rig)

    assert (first.new_obligation_count, first.new_capability_count) == (2, 1)
    assert (second.new_obligation_count, second.new_capability_count) == (0, 0)
    assert second.matched_capability_count == 2


def test_obligation_existence_probe_is_one_parameterized_query(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """L2 Query Safety: ids flow in as a parameter, and the probe precedes the first write."""
    emitter, _ = make_emitter()
    baseline = _Baseline(
        obligations=((_OBL_NEW_1, "Report incidents."), (_OBL_NEW_2, "Keep records."))
    )
    single_tenant = _SingleTenant(gateway_graph=merge_rig.policy_system)

    _merge(baseline, single_tenant, emitter, merge_rig)

    probes = single_tenant.matching("MATCH (o:Obligation) WHERE o.id IN $ids")
    assert len(probes) == 1
    query, params = probes[0]
    assert _OBL_NEW_1 not in query
    assert params == {"ids": sorted([_OBL_NEW_1, _OBL_NEW_2])}
    # the probe ran before the Obligation group was submitted ...
    assert single_tenant.gateway_obligations_at_probe == [set()]
    # ... which then created exactly the net-new nodes
    assert {
        node_id for label, node_id in merge_rig.policy_system.nodes if label == "Obligation"
    } == {
        _OBL_NEW_1,
        _OBL_NEW_2,
    }


def test_obligation_probe_is_skipped_when_the_baseline_has_no_obligations(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    emitter, _ = make_emitter()
    single_tenant = _SingleTenant()

    result = _merge(_Baseline(obligations=()), single_tenant, emitter, merge_rig)

    assert result.new_obligation_count == 0
    assert single_tenant.matching("MATCH (o:Obligation) WHERE o.id IN $ids") == []


def test_merge_succeeded_log_entry_carries_the_three_counts(
    make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    emitter, log_path = make_emitter()
    baseline = _Baseline(
        obligations=((_OBL_NEW_1, "Report incidents."),), capabilities=_mixed_capabilities()
    )

    _merge(baseline, _mixed_single_tenant(), emitter, merge_rig)
    emitter.flush()  # pyright: ignore[reportAttributeAccessIssue]

    [entry] = [
        e
        for e in read_lines(log_path)
        if e.get("component") == "company_merge"
        and e.get("action") == "merge_baseline_graph"
        and e.get("outcome") == "succeeded"
    ]
    assert entry["new_obligations"] == 1
    assert entry["new_capabilities"] == 1
    assert entry["matched_capabilities"] == 3
