# pyright: reportPrivateUsage=false
"""`list-capability-resolutions`: one entry per automatic Capability resolution, read only.

The MCP tool, the service, the single-tenant reads and the baseline reads run for real over the
gateway rig's in-memory graphs. Only the graph openers (the approved boundary), the role store and
the audit store are fakes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)

from graph_cleanup._fakes import RecordingAuditStore
from graph_cleanup._gateway_fakes import CleanupRig
from graph_cleanup._mcp_calls import call_tool, factory
from ps_service.authz.models import AccessRole
from ps_service.company_merge import falkordb_client
from ps_service.graph_cleanup import baseline_reader, dependencies, graph_reader
from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.service import list_capability_resolutions
from ps_service.llm_interface import client as llm_client

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

    from ps_service.config import ServiceConfig

_ISSUER = "https://issuer.example.com/"
_OWNER = ("owner", _ISSUER)
_OFFICER = ("officer", _ISSUER)
_TOOL = "list-capability-resolutions"

_X = "cap_x_vuln_patch"
_A = "cap_a_vuln_remediation"
_Y = "cap_y_asset_register"
_D = "cap_d_asset_list"
_B = "cap_b_asset_inventory"
_POLICY = "pol_patching"


def _roles() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _seed_world(rig: CleanupRig) -> None:
    """Two resolutions: X onto governed A directly, Y onto D, which a cleanup merged into B.

    X is held by two baselines (only the second carries its embedding); Y by one; a third
    baseline holds neither.
    """
    rig.seed_capability(_A, name="Vulnerability remediation", status="active")
    rig.seed_capability(_B, name="Asset inventory")
    rig.seed_policy(_POLICY, title="Patch Management Policy", status="approved")
    rig.seed_governs(_A, _POLICY)
    rig.graph.seed_node(("ResolvedCapability",), _X, {"resolves_to": _A})
    rig.graph.seed_node(("ResolvedCapability",), _Y, {"resolves_to": _D})
    rig.graph.seed_node(("MergedCapability",), _D, {"merged_into": _B})
    rig.seed_baseline(
        "dora_baseline",
        instrument_id="32022R2554",
        capabilities={_X: {"name": "vulnerability patching", "confidence": 0.8}},
    )
    rig.seed_baseline(
        "nis2_baseline",
        instrument_id="32022L2555",
        capabilities={
            _X: {"name": "Vulnerability patching", "confidence": 0.9, "embedding": [1.0, 0.0]},
            _Y: {"name": "Asset register", "confidence": 0.7, "embedding": [0.0, 1.0]},
        },
    )
    rig.seed_baseline(
        "cra_baseline",
        instrument_id="32024R2847",
        capabilities={"cap_unrelated": {"name": "Secure boot"}},
    )


@dataclass
class _Snapshot:
    log_length: int
    nodes: dict[str, object] = field(default_factory=dict)
    edges: dict[str, object] = field(default_factory=dict)


def _snapshot(rig: CleanupRig) -> _Snapshot:
    return _Snapshot(
        log_length=len(rig.store.groups),
        nodes={repr(key): dict(value) for key, value in rig.graph.nodes.items()},
        edges={repr(key): dict(value) for key, value in rig.graph.edges.items()},
    )


def _install(monkeypatch: pytest.MonkeyPatch, rig: CleanupRig) -> RecordingAuditStore:
    audit = RecordingAuditStore()
    _override(
        monkeypatch,
        role_store_factory=factory(_roles()),
        audit_store_factory=factory(audit),
        graph_factory=factory(rig.read),
    )

    def _opener() -> object:
        def _open(_config: ServiceConfig) -> object:
            return rig.baseline_graphs()

        return _open

    monkeypatch.setattr(dependencies, "build_default_graph_cleanup_baseline_opener", _opener)
    return audit


def test_one_entry_per_marker_with_terminal_policy_and_instruments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = CleanupRig()
    _seed_world(rig)
    _install(monkeypatch, rig)
    before = _snapshot(rig)

    body = json.loads(call_tool(_TOOL, {}, _OFFICER))

    entries = [
        {
            key: entry[key]
            for key in (
                "x_id",
                "x_name",
                "a_id",
                "a_name",
                "marker_resolves_to",
                "governing_policy",
                "instruments",
            )
        }
        for entry in body["resolutions"]
    ]
    assert entries == [
        {
            "x_id": _X,
            "x_name": "Vulnerability patching",
            "a_id": _A,
            "a_name": "Vulnerability remediation",
            "marker_resolves_to": _A,
            "governing_policy": {
                "id": _POLICY,
                "title": "Patch Management Policy",
                "status": "approved",
            },
            "instruments": ["32022L2555", "32022R2554"],
        },
        {
            "x_id": _Y,
            "x_name": "Asset register",
            "a_id": _B,
            "a_name": "Asset inventory",
            "marker_resolves_to": _D,
            "governing_policy": None,
            "instruments": ["32022L2555"],
        },
    ]
    after = _snapshot(rig)
    assert after == before
    assert rig.store.groups == []


def test_the_list_is_logged_with_counts_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    rig = CleanupRig()
    _seed_world(rig)
    emitter, log_path = make_emitter()

    list_capability_resolutions(rig.read, rig.baseline_graphs(), emitter=emitter)
    emitter.flush()

    [entry] = [
        line for line in read_lines(log_path) if line["action"] == "list_capability_resolutions"
    ]
    assert entry["component"] == "graph_cleanup"
    assert entry["outcome"] == "succeeded"
    assert (entry["resolution_count"], entry["baseline_count"]) == (2, 3)
    assert "x_name" not in entry
    assert not any("patching" in str(value).lower() for value in entry.values())


def test_a_resolution_no_baseline_holds_is_listed_without_text_or_instruments() -> None:
    rig = CleanupRig()
    rig.seed_capability(_A, name="Vulnerability remediation")
    rig.graph.seed_node(("ResolvedCapability",), _X, {"resolves_to": _A})

    result = list_capability_resolutions(rig.read, ())

    [entry] = result.resolutions
    assert (entry.x_id, entry.x_name, entry.instruments) == (_X, None, ())


def test_a_marker_cycle_is_an_error_not_a_dropped_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = CleanupRig()
    rig.graph.seed_node(("ResolvedCapability",), _X, {"resolves_to": _D})
    rig.graph.seed_node(("MergedCapability",), _D, {"merged_into": _X})
    _install(monkeypatch, rig)

    text = call_tool(_TOOL, {}, _OFFICER)

    assert text.startswith("error: ")
    assert "cycle" in text


def test_a_terminal_that_is_not_a_capability_is_an_error() -> None:
    rig = CleanupRig()
    rig.graph.seed_node(("ResolvedCapability",), _X, {"resolves_to": "cap_gone"})

    with pytest.raises(GraphCleanupValidationError, match="cap_gone"):
        list_capability_resolutions(rig.read, ())


def test_an_id_held_by_both_marker_labels_is_an_error() -> None:
    rig = CleanupRig()
    rig.seed_capability(_A, name="Vulnerability remediation")
    rig.graph.seed_node(("ResolvedCapability",), _X, {"resolves_to": _A})
    rig.graph.seed_node(("MergedCapability",), _X, {"merged_into": _A})

    with pytest.raises(GraphCleanupValidationError, match="more than one marker"):
        list_capability_resolutions(rig.read, ())


def test_an_unreachable_baseline_store_is_the_generic_graph_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = CleanupRig()
    _seed_world(rig)
    _install(monkeypatch, rig)

    def _opener() -> object:
        def _open(_config: ServiceConfig) -> object:
            message = "connection refused to 10.0.0.1:6379"
            raise ConnectionError(message)

        return _open

    monkeypatch.setattr(dependencies, "build_default_graph_cleanup_baseline_opener", _opener)

    text = call_tool(_TOOL, {}, _OFFICER)

    assert text == "error: the policy graph database is not reachable"


def _seed_relying_obligations(rig: CleanupRig) -> None:
    """Cached embeddings on A and X, and baseline `REQUIRES` edges into X, Y and A.

    `obl_merged` was absorbed into `obl_kept` by a cleanup merge; `obl_gone` no longer exists in
    the single-tenant graph; `obl_on_a` requires A itself, not X. B has no cached embedding.
    """
    rig.graph.nodes[("Capability", _A)]["embedding"] = [0.6, 0.8]
    for obligation in ("obl_direct", "obl_kept", "obl_on_a", "obl_y"):
        rig.seed_endpoint("Obligation", obligation, text=obligation)
    rig.graph.seed_node(("MergedObligation",), "obl_merged", {"merged_into": "obl_kept"})
    rig.seed_baseline_requires(
        "nis2_baseline",
        (("obl_direct", _X), ("obl_merged", _X), ("obl_gone", _X), ("obl_on_a", _A)),
    )
    rig.seed_baseline_requires("nis2_baseline", (("obl_y", _Y),))
    rig.seed_baseline_requires("dora_baseline", (("obl_direct", _X),))


def _no_embedding_call(*_args: object, **_kwargs: object) -> object:
    message = "listing resolutions must not call an embedding model"
    raise AssertionError(message)


def test_similarity_from_cached_embeddings_and_count_of_relying_obligations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = CleanupRig()
    _seed_world(rig)
    _seed_relying_obligations(rig)
    _install(monkeypatch, rig)
    monkeypatch.setattr(llm_client, "default_embedding_caller", _no_embedding_call)
    monkeypatch.setattr(llm_client.litellm, "embedding", _no_embedding_call)
    before = _snapshot(rig)

    body = json.loads(call_tool(_TOOL, {}, _OFFICER))

    measures = {
        entry["x_id"]: (entry["similarity"], entry["obligation_count"])
        for entry in body["resolutions"]
    }
    assert measures == {_X: (pytest.approx(0.6), 2), _Y: (None, 1)}
    assert _snapshot(rig) == before
    assert rig.store.groups == []


def test_an_unscorable_embedding_pair_has_no_similarity() -> None:
    rig = CleanupRig()
    _seed_world(rig)
    rig.graph.nodes[("Capability", _A)]["embedding"] = [0.6, 0.8, 0.0]

    result = list_capability_resolutions(rig.read, rig.baseline_graphs())

    [x_entry] = [entry for entry in result.resolutions if entry.x_id == _X]
    assert x_entry.similarity is None


def test_an_obligation_marker_cycle_is_an_error() -> None:
    rig = CleanupRig()
    _seed_world(rig)
    rig.graph.seed_node(("MergedObligation",), "obl_p", {"merged_into": "obl_q"})
    rig.graph.seed_node(("MergedObligation",), "obl_q", {"merged_into": "obl_p"})
    rig.seed_baseline_requires("nis2_baseline", (("obl_p", _X),))

    with pytest.raises(GraphCleanupValidationError, match="cycle"):
        list_capability_resolutions(rig.read, rig.baseline_graphs())


def test_the_terminal_embedding_read_names_its_label_and_parameters() -> None:
    query = graph_reader._CAPABILITY_EMBEDDINGS_QUERY

    assert query.startswith("MATCH (c:Capability) WHERE c.id IN $ids ")
    assert "c.embedding" in query


@pytest.mark.parametrize(
    ("query", "fragments"),
    [
        (
            baseline_reader._CAPABILITY_QUERY,
            ("MATCH (c:Capability)", "WHERE c.id IN $ids", "c.embedding"),
        ),
        (baseline_reader._INSTRUMENT_QUERY, ("MATCH (r:RegulatoryInstrument)", "RETURN r.id")),
        (
            baseline_reader._REQUIRES_QUERY,
            (
                "MATCH (s:Obligation)-[:REQUIRES]->(c:Capability)",
                "WHERE c.id IN $ids",
                "RETURN s.id, c.id",
            ),
        ),
        (
            baseline_reader._COVERS_QUERY,
            (
                "MATCH (s:PracticeArea)-[:COVERS]->(c:Capability)",
                "WHERE c.id IN $ids",
                "RETURN s.id, c.id",
            ),
        ),
        (
            baseline_reader._MITIGATED_BY_QUERY,
            (
                "MATCH (s:RiskPath)-[:MITIGATED_BY]->(c:Capability)",
                "WHERE c.id IN $ids",
                "RETURN s.id, c.id",
            ),
        ),
    ],
)
def test_each_baseline_read_names_its_label_and_parameters(
    query: str, fragments: tuple[str, ...]
) -> None:
    for fragment in fragments:
        assert fragment in query


class _Database:
    """A read-only database stand-in that lists some graphs and records each one opened."""

    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.opened: list[str] = []

    def list_graphs(self) -> list[str]:
        return list(self._names)

    def select_graph(self, name: str) -> object:
        self.opened.append(name)
        return name


def test_the_default_baseline_opener_opens_only_listed_baseline_graphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _Database(["policy_system", "nis2_baseline", "dora_native", "dora_baseline"])
    monkeypatch.setattr(falkordb_client, "connect_from_config", factory(database))

    opened = dependencies.build_default_graph_cleanup_baseline_opener()(
        object()  # pyright: ignore[reportArgumentType] -- the patched connection ignores config
    )

    assert opened == (("dora_baseline", "dora_baseline"), ("nis2_baseline", "nis2_baseline"))
    assert database.opened == ["dora_baseline", "nis2_baseline"]
