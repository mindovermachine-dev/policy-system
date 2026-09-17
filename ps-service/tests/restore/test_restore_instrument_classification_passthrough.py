"""Tests for `ps_service.restore.restore_instrument`'s offline restore-path
parity for issue #106's PracticeArea/RiskPath/classification-edge
passthrough (PLAN.md §5, IMPL_SLICE_5.md).

`_run_baseline_merge` mirrors `company_merge.merge.merge_baseline_graph`'s
live-path classification pass (Slices 1-4) exactly: after Capability/Policy
dedup+writes complete, it persists PracticeArea/RiskPath nodes via
`graph_writer.persist_practice_area_and_risk_path_passthrough` (no dedup, no
embedding call -- identity convergence is structural, content-hashed ids
minted upstream by `internal_seed`), validates every COVERS/OWNS/
MITIGATED_BY/VERIFIED_BY edge endpoint via
`graph_writer.validate_classification_edge_endpoints` before any
classification edge is written, then folds `classification_edges` into the
same `persist_rewired_edges` call used for the regulatory-spine/governance
edges.

Fakes here mirror `tests/company_merge/test_merge_baseline_graph.py`'s
`_FakeBaselineGraph`/`_FakeSingleTenantGraph` conventions (also already
duplicated once into `tests/restore/test_restore_instrument_policy_
convergence.py`) -- duplicated rather than imported cross-directory, per
this test suite's existing per-component fake convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

import ps_service.company_merge.dedup as dedup_module
import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.domain_mapper.identity import practice_area_id

if TYPE_CHECKING:
    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.logging.emitter import LogEmitter

_THRESHOLD = 0.85


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeRegulatoryInstrumentNode:
    """Satisfies `graph_reader._RegulatoryInstrumentNode` structurally -- only
    `.properties` is ever read.
    """

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


_READ_MARKERS = ("RETURN n.id, n.name, n.embedding", "RETURN n.id, n.title, n.embedding")


def _is_read_call(call: _RecordedCall) -> bool:
    return any(marker in call.query for marker in _READ_MARKERS)


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s queries with its own
    scripted row set, dispatched by a distinctive substring -- mirrors
    `test_merge_baseline_graph.py`'s own `_FakeBaselineGraph` exactly,
    including issue #106's PracticeArea/RiskPath/classification-edge
    branches (needed here, unlike `test_restore_instrument_policy_
    convergence.py`'s trimmed copy, since these tests exercise non-empty
    classification content).
    """

    def __init__(
        self,
        *,
        regulatory_instrument_properties: dict[str, object],
        capability_rows: list[object] | None = None,
        policy_rows: list[object] | None = None,
        standard_rows: list[object] | None = None,
        control_rows: list[object] | None = None,
        governed_by_rows: list[object] | None = None,
        supported_by_rows: list[object] | None = None,
        implemented_by_rows: list[object] | None = None,
        practice_area_rows: list[object] | None = None,
        risk_path_rows: list[object] | None = None,
        covers_rows: list[object] | None = None,
        owns_rows: list[object] | None = None,
        mitigated_by_rows: list[object] | None = None,
        verified_by_rows: list[object] | None = None,
    ) -> None:
        self._regulatory_instrument_properties = regulatory_instrument_properties
        self._capability_rows = capability_rows or []
        self._policy_rows = policy_rows or []
        self._standard_rows = standard_rows or []
        self._control_rows = control_rows or []
        self._governed_by_rows = governed_by_rows or []
        self._supported_by_rows = supported_by_rows or []
        self._implemented_by_rows = implemented_by_rows or []
        self._practice_area_rows = practice_area_rows or []
        self._risk_path_rows = risk_path_rows or []
        self._covers_rows = covers_rows or []
        self._owns_rows = owns_rows or []
        self._mitigated_by_rows = mitigated_by_rows or []
        self._verified_by_rows = verified_by_rows or []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "[e:DEFINES]" in q or "[e:EXPRESSES]" in q:
            return _FakeQueryResult([])
        if "[:HAS]" in q or "[:SATISFIED_BY]" in q or "[:REQUIRES]" in q:
            return _FakeQueryResult([])
        if "[:GOVERNED_BY]" in q:
            return _FakeQueryResult(self._governed_by_rows)
        if "[:SUPPORTED_BY]" in q:
            return _FakeQueryResult(self._supported_by_rows)
        if "[:IMPLEMENTED_BY]" in q:
            return _FakeQueryResult(self._implemented_by_rows)
        if "[:COVERS]" in q:
            return _FakeQueryResult(self._covers_rows)
        if "[:OWNS]" in q:
            return _FakeQueryResult(self._owns_rows)
        if "[:MITIGATED_BY]" in q:
            return _FakeQueryResult(self._mitigated_by_rows)
        if "[:VERIFIED_BY]" in q:
            return _FakeQueryResult(self._verified_by_rows)
        if "(n:Policy) RETURN" in q:
            return _FakeQueryResult(self._policy_rows)
        if "(n:Standard) RETURN" in q:
            return _FakeQueryResult(self._standard_rows)
        if "(n:Control) RETURN" in q:
            return _FakeQueryResult(self._control_rows)
        if "(n:PracticeArea) RETURN" in q:
            return _FakeQueryResult(self._practice_area_rows)
        if "(n:RiskPath) RETURN" in q:
            return _FakeQueryResult(self._risk_path_rows)
        if "n.role_id" in q:
            return _FakeQueryResult([])  # Requirement
        if "n.description" in q:
            return _FakeQueryResult(self._capability_rows)
        if "n.name, n.confidence" in q:
            return _FakeQueryResult([])  # Role
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult([])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
            )
        raise AssertionError(f"unexpected query issued: {q!r}")


class _FakeSingleTenantGraph:
    """Answers Capability/Policy existing-canonical-index reads with
    pre-seeded rows and records every call (read AND write) it receives --
    mirrors `test_merge_baseline_graph.py`'s own `_FakeSingleTenantGraph`,
    trimmed to Capability/Policy/Standard/Control/PracticeArea/RiskPath
    (issue #106).
    """

    def __init__(
        self,
        *,
        capability_rows: list[object] | None = None,
        policy_rows: list[object] | None = None,
        practice_area_rows: list[object] | None = None,
        risk_path_rows: list[object] | None = None,
    ) -> None:
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows or []:
            row_list = list(cast("list[object]", row))
            self._capabilities[cast("str", row_list[0])] = row_list
        self._policies: dict[str, list[object]] = {}
        for row in policy_rows or []:
            row_list = list(cast("list[object]", row))
            self._policies[cast("str", row_list[0])] = row_list
        self._standards: dict[str, list[object]] = {}
        self._controls: dict[str, list[object]] = {}
        self._practice_areas: dict[str, dict[str, object]] = {}
        for row in practice_area_rows or []:
            row_list = list(cast("list[object]", row))
            self._practice_areas[cast("str", row_list[0])] = dict(
                cast("dict[str, object]", row_list[1])
            )
        self._risk_paths: dict[str, dict[str, object]] = {}
        for row in risk_path_rows or []:
            row_list = list(cast("list[object]", row))
            self._risk_paths[cast("str", row_list[0])] = dict(
                cast("dict[str, object]", row_list[1])
            )
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "(n:Policy) RETURN n.id, n.title, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._policies.values()])
        if "MERGE (n:Standard {id: $id}) SET n += $properties" in q:
            self._set(self._standards, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:Control {id: $id}) SET n += $properties" in q:
            self._set(self._controls, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:Capability {id: $id}) ON CREATE SET" in q:
            self._mint(self._capabilities, params, "name")
            return _FakeQueryResult([])
        if "MERGE (n:Policy {id: $id}) ON CREATE SET" in q:
            self._mint(self._policies, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:PracticeArea {id: $id}) ON CREATE SET" in q:
            self._mint_properties(self._practice_areas, params)
            return _FakeQueryResult([])
        if "MERGE (n:RiskPath {id: $id}) ON CREATE SET" in q:
            self._mint_properties(self._risk_paths, params)
            return _FakeQueryResult([])
        if "MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._capabilities, params)
            return _FakeQueryResult([])
        if "MATCH (n:Policy {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._policies, params)
            return _FakeQueryResult([])
        if q == "UNWIND $ids AS id MATCH (n {id: id}) RETURN id":
            # issue #106, AC-BI-008: `validate_classification_edge_endpoints`'s
            # batched existence check -- answer with whichever requested ids
            # are actually present in ANY of this fake's tracked node tables.
            assert params is not None
            requested_ids = cast("list[str]", params["ids"])
            known_ids = (
                set(self._capabilities)
                | set(self._policies)
                | set(self._standards)
                | set(self._controls)
                | set(self._practice_areas)
                | set(self._risk_paths)
            )
            return _FakeQueryResult([[rid] for rid in requested_ids if rid in known_ids])
        return _FakeQueryResult([[0]])  # any other write (RegulatoryInstrument, edges, ...)

    def _set(
        self, table: dict[str, list[object]], params: dict[str, object] | None, text_key: str
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [node_id, properties.get(text_key), properties.get("embedding")]

    def _mint(
        self, table: dict[str, list[object]], params: dict[str, object] | None, text_key: str
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [node_id, properties.get(text_key), properties.get("embedding")]

    def _mint_properties(
        self, table: dict[str, dict[str, object]], params: dict[str, object] | None
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = dict(properties)

    def _backfill(self, table: dict[str, list[object]], params: dict[str, object] | None) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        row = table.get(node_id)
        if row is None or row[2] is not None:
            return
        row[2] = params["embedding"]

    def writes(self) -> list[_RecordedCall]:
        return [call for call in self.calls if not _is_read_call(call)]

    def calls_matching(self, substring: str) -> list[_RecordedCall]:
        return [call for call in self.calls if substring in call.query]

    def practice_area_properties(self, node_id: str) -> dict[str, object] | None:
        return self._practice_areas.get(node_id)

    def risk_path_properties(self, node_id: str) -> dict[str, object] | None:
        return self._risk_paths.get(node_id)


class _FakeDb:
    """Satisfies `_run_baseline_merge`'s own `db.select_graph(name)` call
    shape -- mirrors `test_restore_instrument_policy_convergence.py`'s own
    `_FakeDb` exactly.
    """

    def __init__(self, graphs: dict[str, GraphHandle]) -> None:
        self._graphs = graphs

    def select_graph(self, name: str) -> GraphHandle:
        return self._graphs[name]


def _classification_baseline(
    *,
    regulatory_instrument_id: str = "REG-1.0",
    capability_rows: list[object] | None = None,
    practice_area_rows: list[object] | None = None,
    risk_path_rows: list[object] | None = None,
    covers_rows: list[object] | None = None,
    owns_rows: list[object] | None = None,
    mitigated_by_rows: list[object] | None = None,
    verified_by_rows: list[object] | None = None,
) -> _FakeBaselineGraph:
    """A minimal internal-sourced baseline carrying only classification-layer
    content (plus whatever Capability rows a test needs) -- these tests
    exercise `_run_baseline_merge`'s issue #106 pass directly, not the
    regulatory-spine machinery already proven elsewhere.
    """
    return _FakeBaselineGraph(
        regulatory_instrument_properties={
            "id": regulatory_instrument_id,
            "title": "Test Regulation",
        },
        capability_rows=capability_rows,
        practice_area_rows=practice_area_rows,
        risk_path_rows=risk_path_rows,
        covers_rows=covers_rows,
        owns_rows=owns_rows,
        mitigated_by_rows=mitigated_by_rows,
        verified_by_rows=verified_by_rows,
    )


def _run_merge(
    baseline: _FakeBaselineGraph,
    snapshot: _FakeSingleTenantGraph,
    *,
    regulatory_instrument_id: str = "REG-1.0",
    incoming_embeddings: dict[str, tuple[float, ...]] | None = None,
    emitter: LogEmitter | None = None,
) -> dict[str, int]:
    """Run `_run_baseline_merge` against a fresh `_FakeDb` wiring `baseline`
    as the staged baseline graph and `snapshot` as the snapshot graph --
    the shape every test below needs, factored out once.
    """
    baseline_staged_name = "reg_baseline__restoring__token"
    snapshot_name = "policy_system__restoring__token"
    fake_db = cast("FalkorDB", _FakeDb({baseline_staged_name: baseline, snapshot_name: snapshot}))
    return restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        fake_db,
        baseline_staged_name,
        regulatory_instrument_id,
        incoming_embeddings or {},
        _THRESHOLD,
        snapshot_name,
        emitter,
    )


def test_practice_area_and_risk_path_nodes_pass_through_to_snapshot_graph() -> None:
    """AC-BI-003 (restore path): a baseline carrying PracticeArea/RiskPath
    nodes restores and both land in the snapshot graph with every property
    intact -- the restore-path analog of `test_merge_baseline_graph.py::
    test_practice_area_and_risk_path_nodes_pass_through_to_single_tenant`.
    """
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    risk_path_id_value = "rp_secure_build_release_d93f8a"
    baseline = _classification_baseline(
        practice_area_rows=[
            [
                practice_area_id_value,
                "Secure SDLC",
                "active",
                "Secure development lifecycle practices",
                "1.0",
                "role_ciso",
            ]
        ],
        risk_path_rows=[
            [
                risk_path_id_value,
                "Secure Build & Release",
                "active",
                "Risks in the build/release pipeline",
                "operational",
                "2.0",
            ]
        ],
    )
    snapshot = _FakeSingleTenantGraph()

    _run_merge(baseline, snapshot)

    assert snapshot.practice_area_properties(practice_area_id_value) == {
        "name": "Secure SDLC",
        "status": "active",
        "description": "Secure development lifecycle practices",
        "version": "1.0",
        "owner_id": "role_ciso",
    }
    assert snapshot.risk_path_properties(risk_path_id_value) == {
        "name": "Secure Build & Release",
        "status": "active",
        "description": "Risks in the build/release pipeline",
        "risk_type": "operational",
        "version": "2.0",
    }
    writes = snapshot.writes()
    assert any("MERGE (n:PracticeArea {id: $id}) ON CREATE SET" in c.query for c in writes)
    assert any("MERGE (n:RiskPath {id: $id}) ON CREATE SET" in c.query for c in writes)


def test_practice_area_and_risk_path_properties_unchanged_when_node_already_exists() -> None:
    """AC-BI-007 (restore path): a PracticeArea/RiskPath node that already
    exists in the snapshot graph keeps its existing properties after the
    restore -- `MERGE ... ON CREATE SET` semantics, not an overwrite --
    even though the incoming baseline node shares the same `id` but carries
    different property values. The restore-path analog of
    `test_merge_baseline_graph.py::test_practice_area_and_risk_path_
    properties_unchanged_when_node_already_exists`.
    """
    practice_area_id_value = "pa_existing"
    risk_path_id_value = "rp_existing"
    baseline = _classification_baseline(
        practice_area_rows=[
            [
                practice_area_id_value,
                "Secure SDLC",
                "active",
                "incoming-should-not-apply",
                "2.0",
                "role_incoming",
            ]
        ],
        risk_path_rows=[
            [
                risk_path_id_value,
                "Secure Build & Release",
                "active",
                "incoming-should-not-apply",
                "operational",
                "2.0",
            ]
        ],
    )
    snapshot = _FakeSingleTenantGraph(
        practice_area_rows=[
            [
                practice_area_id_value,
                {
                    "name": "Secure SDLC",
                    "status": "active",
                    "description": "original",
                    "version": "1.0",
                    "owner_id": "role_ciso",
                },
            ]
        ],
        risk_path_rows=[
            [
                risk_path_id_value,
                {
                    "name": "Secure Build & Release",
                    "status": "active",
                    "description": "original",
                    "risk_type": "operational",
                    "version": "1.0",
                },
            ]
        ],
    )

    _run_merge(baseline, snapshot)

    assert snapshot.practice_area_properties(practice_area_id_value) == {
        "name": "Secure SDLC",
        "status": "active",
        "description": "original",
        "version": "1.0",
        "owner_id": "role_ciso",
    }
    assert snapshot.risk_path_properties(risk_path_id_value) == {
        "name": "Secure Build & Release",
        "status": "active",
        "description": "original",
        "risk_type": "operational",
        "version": "1.0",
    }
    assert snapshot.calls_matching("MERGE (n:PracticeArea {id: $id}) ON CREATE SET")
    assert snapshot.calls_matching("MERGE (n:RiskPath {id: $id}) ON CREATE SET")


def test_covers_edge_target_rewritten_to_canonical_capability_id_in_restore() -> None:
    """AC-BI-005 (restore path): a COVERS edge's Capability target,
    resolved by the OFFLINE dedup pass (`resolve_capability_convergence_
    offline`) to an already-existing canonical Capability via an
    artifact-supplied embedding match, is rewritten onto that canonical id
    -- not left pointing at the incoming baseline-local id. Mirrors
    `test_restore_instrument_policy_convergence.py`'s own AC-BI-021 proof
    shape (a Policy/GOVERNED_BY semantic match), applied to a Capability/
    COVERS edge instead.

    Also: no `RouteEmbedding`/`EmbeddingCaller` call of any kind is
    possible for the PracticeArea source of this edge -- offline dedup
    (`resolve_capability_convergence_offline`) never calls `route_embedding`
    at all (`dedup.py`'s own module docstring: "every embedding is either
    artifact-supplied or already cached"), and
    `persist_practice_area_and_risk_path_passthrough`/`validate_
    classification_edge_endpoints` take no `EmbeddingCaller` parameter to
    begin with -- there is no argument through which such a call could even
    be threaded.
    """
    existing_capability_id = "cap_existing_encrypt_data_at_rest"
    incoming_capability_id = "cap_incoming_protect_data_at_rest"
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    shared_vector: list[float] = [1.0, 0.0]

    baseline = _classification_baseline(
        capability_rows=[[incoming_capability_id, "Protect Data At Rest Capability", 0.8, None]],
        practice_area_rows=[[practice_area_id_value, "Secure SDLC", "active", None, None, None]],
        covers_rows=[[practice_area_id_value, incoming_capability_id]],
    )
    snapshot = _FakeSingleTenantGraph(
        capability_rows=[
            [existing_capability_id, "Encrypt Data At Rest Capability", list(shared_vector)]
        ],
    )

    _run_merge(
        baseline,
        snapshot,
        incoming_embeddings={incoming_capability_id: tuple(shared_vector)},
    )

    covers_writes = snapshot.calls_matching("[:COVERS]")
    assert len(covers_writes) == 1
    params = covers_writes[0].params
    assert params is not None
    assert params["source_id"] == practice_area_id_value
    assert params["target_id"] == existing_capability_id
    # No new Capability node was minted -- the semantic match reused the
    # existing canonical node, exactly as the live path would.
    assert not snapshot.calls_matching("MERGE (n:Capability {id: $id}) ON CREATE SET")


def test_offline_dedup_never_called_for_practice_area_or_risk_path_and_names_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-006 (restore path): two SEPARATE restores of baselines that
    each author a PracticeArea with the SAME `name` (simulating two
    separate `internal_seed` mints of that name, which is where real
    convergence happens, per PLAN.md §1.1) converge onto the SAME content-
    hashed id across both `_run_baseline_merge` calls -- proven via the
    fake's own cross-call accumulation, mirroring `test_restore_instrument_
    policy_convergence.py`'s own two-separately-seeded-but-accumulating
    pattern.

    Also proves `resolve_capability_convergence_offline` (the one offline
    function that could reach `route_embedding`, if it called it at all --
    see `test_covers_edge_target_rewritten_to_canonical_capability_id_in_
    restore`'s docstring for why it structurally cannot) is NEVER invoked
    with `kind="PracticeArea"`/`kind="RiskPath"` across either call --
    monkeypatched here the same way `test_merge_baseline_graph.py::test_
    merge_baseline_graph_never_dedupes_practice_area_or_risk_path` proves it
    for the live path.
    """
    recorded_kinds: list[str] = []
    real_resolve_offline = dedup_module.resolve_capability_convergence_offline

    def _recording_wrapper(*args: object, **kwargs: object) -> object:
        recorded_kinds.append(cast("str", kwargs.get("kind", "Capability")))
        return real_resolve_offline(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        restore_instrument_module, "resolve_capability_convergence_offline", _recording_wrapper
    )
    shared_pa_id = practice_area_id("Secure SDLC")
    snapshot = _FakeSingleTenantGraph()

    baseline_a = _classification_baseline(
        regulatory_instrument_id="REG-A",
        practice_area_rows=[[shared_pa_id, "Secure SDLC", "active", None, None, None]],
    )
    baseline_b = _classification_baseline(
        regulatory_instrument_id="REG-B",
        practice_area_rows=[[shared_pa_id, "Secure SDLC", "active", None, None, None]],
    )

    _run_merge(baseline_a, snapshot, regulatory_instrument_id="REG-A")
    _run_merge(baseline_b, snapshot, regulatory_instrument_id="REG-B")

    assert set(recorded_kinds) == {"Capability"}

    merges = snapshot.calls_matching("MERGE (n:PracticeArea {id: $id}) ON CREATE SET")
    assert len(merges) == 2
    assert {c.params["id"] for c in merges if c.params is not None} == {shared_pa_id}


def test_missing_classification_edge_endpoint_raises_before_any_classification_edge_write() -> None:
    """AC-BI-008 (restore path): a COVERS edge referencing an endpoint id
    absent from BOTH the baseline and the snapshot graph raises
    `CompanyMergePersistenceError` before any of the four classification
    EDGE types is written -- checked via `calls_matching`, not a blanket
    "zero writes" claim, since the PracticeArea NODE write is accepted
    precedent to already have landed by this point (mirrors the live path's
    own `test_missing_classification_edge_endpoint_raises_before_any_
    classification_edge_write`).

    Propagation through `stage_and_finalize_policy_system_leg`'s discard-
    and-re-raise contract: confirmed by reading `ps_service.restore.
    staging.stage_and_finalize_policy_system_leg`'s `except Exception:
    discard_staged_keys(...); raise` block -- generic to ANY exception type
    raised from within `run_offline_merge`, with no special-casing --
    and cross-checked against the already-existing LIVE proof of this exact
    contract, `test_restore_instrument_all_or_nothing_live.py::test_merge_
    step_failure_leaves_single_tenant_and_native_untouched`, which injects a
    different exception (`_ForcedMergeFailureError`) from the same code
    depth inside `_run_baseline_merge` (`persist_canonical_nodes`, called
    immediately before where `validate_classification_edge_endpoints` now
    sits) and asserts the live single-tenant/native graphs are left
    byte-for-byte untouched. Since `CompanyMergePersistenceError(Exception)`
    is caught by that same unconditional `except Exception:` clause, this
    generic live proof already covers AC-BI-008's discard-on-error
    guarantee for the restore path without a redundant new live test.
    """
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    baseline = _classification_baseline(
        practice_area_rows=[[practice_area_id_value, "Secure SDLC", "active", None, None, None]],
        covers_rows=[[practice_area_id_value, "cap_never_persisted_anywhere"]],
    )
    snapshot = _FakeSingleTenantGraph()

    with pytest.raises(CompanyMergePersistenceError, match="cap_never_persisted_anywhere"):
        _run_merge(baseline, snapshot)

    for marker in ("[:COVERS]", "[:OWNS]", "[:MITIGATED_BY]", "[:VERIFIED_BY]"):
        assert snapshot.calls_matching(marker) == []
    assert snapshot.calls_matching("MERGE (n:PracticeArea {id: $id}) ON CREATE SET")


def test_external_baseline_restore_unaffected_by_classification_pass() -> None:
    """AC-BI-009 (restore path): an external-sourced restore (no
    `internal_seed` content at all) issues NO query matching any of the six
    classification-layer markers -- checked against `snapshot.calls` (every
    call, read AND write), mirroring the live path's own `test_external_
    baseline_unaffected_by_classification_pass`.
    """
    baseline = _classification_baseline(
        capability_rows=[["cap_no_governance", "Some Capability", 0.8, None]],
    )
    snapshot = _FakeSingleTenantGraph()

    _run_merge(baseline, snapshot)

    assert not any(
        label in c.query
        for c in snapshot.calls
        for label in ("PracticeArea", "RiskPath", "COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY")
    )
