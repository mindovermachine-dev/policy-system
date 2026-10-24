"""Tests for `ps_service.company_merge.merge.merge_baseline_graph`
the top-level `MergeBaselineGraph`
orchestration wiring `graph_reader`/`dedup`/`graph_writer` together.

Fakes here are hand-written, satisfying `GraphHandle`/`EmbeddingCaller`
structurally -- mirroring `test_graph_reader.py`'s `_ScriptedFakeGraph`
dispatch-by-substring style for the baseline-graph side, and
`test_dedup_combined_resolution.py`'s `_ScriptedCallEmbedding` for the
embedding side. `_FakeSingleTenantGraph` additionally records every call
(read AND write) for the call-order proof (test (b)) and the edge-level
dedup-target reachability proof (test (c), Q3's fix).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from graph_gateway._fakes import InMemoryGraph

    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import MergeRig

import re
from dataclasses import dataclass
from typing import cast

import httpx
import openai
import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.errors import (
    CompanyMergeConfigurationError,
    CompanyMergePersistenceError,
)
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.graph_writer import ANY_LABEL_EXISTENCE_READ
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import NearMissPair
from ps_service.company_merge.pending_review import persist_pending_reviews
from ps_service.company_merge.reviewable_kinds import CAPABILITY
from ps_service.domain_mapper.identity import (
    capability_id,
    obligation_id,
    practice_area_id,
    risk_path_id,
)
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.logging import bind_run_context

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85
_GOVERNANCE = GovernanceImport(owner=("sub-1", "https://issuer.example"))


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


# The one literal read query dedup.read_existing_canonical_index issues
# (Capability only) -- used to distinguish a "read" call from a
# "write" call in a fake single-tenant graph's call log (test (b)'s
# call-order proof).
_EXISTENCE_READ = re.compile(
    r"^MATCH \((?:n|o):(\w+)\) WHERE (?:n|o)\.id IN \$ids RETURN (?:n|o)\.id$"
)

_EMBEDDING_STATE_READ = re.compile(
    r"^MATCH \(n:(Capability|Policy)\) WHERE n\.id IN \$ids RETURN n\.id, n\.embedding IS NULL$"
)

_READ_MARKERS = (
    "RETURN n.id, n.name, n.embedding",
    "RETURN n.id, n.embedding IS NULL",
    "RETURN n.id, n.title, n.embedding",
    "RETURN a.id, b.id",
    "RETURN m.id, m.merged_into",
    "RETURN m.id, m.resolves_to",
)


def _minted_capability_ids(rig: MergeRig) -> list[str]:
    """The Capability nodes the merge created through the gateway, in log order."""
    return [e.identity for g in rig.groups for e in g.entries if e.name == "Capability"]


def _review_nodes(rig: MergeRig) -> list[dict[str, object]]:
    """The `PendingReview` nodes the merge created through the gateway."""
    return [
        props for (label, _), props in rig.policy_system.nodes.items() if label == "PendingReview"
    ]


_UNRESOLVED_PAIRS_READ = re.compile(r"^UNWIND \$pairs AS p MATCH \(r:PendingReview \{kind: \$kind,")
"""The one statement that asks which near-miss pairs already have a review."""

_DEDUP_READ_EVENT = "dedup_read"
"""What the fake single-tenant graph appends to the rig's shared event list per merge read."""


def _is_read_call(call: _RecordedCall) -> bool:
    return any(marker in call.query for marker in _READ_MARKERS)


class _FakeBaselineGraph:
    """Answers every one of `read_baseline_graph`'s ten queries with its own
    scripted row set, dispatched by a distinctive substring -- mirrors
    `test_graph_reader.py`'s `_ScriptedFakeGraph` exactly, plus recording
    every call received (needed for test (d)'s zero-calls proof).
    """

    def __init__(
        self,
        *,
        regulatory_instrument_properties: dict[str, object],
        role_rows: list[object],
        requirement_rows: list[object],
        obligation_rows: list[object],
        capability_rows: list[object],
        defines_rows: list[object],
        expresses_rows: list[object],
        has_rows: list[object],
        satisfied_by_rows: list[object],
        requires_rows: list[object],
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
        self._role_rows = role_rows
        self._requirement_rows = requirement_rows
        self._obligation_rows = obligation_rows
        # A mutable table keyed by id, not a plain row list:
        # `MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL SET
        # n.embedding` (backfill_incoming_capability_embeddings) needs
        # somewhere to actually write a freshly-computed incoming embedding
        # back onto its own row, mirroring `_FakeSingleTenantGraph`'s own
        # `_backfill` mechanism. A 4-element row (the shape most of this
        # file's fixtures use) is padded with a `None`
        # embedding -- callers that care about the embedding pass a 5th
        # element explicitly instead.
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows:
            row_list = list(cast("list[object]", row))
            if len(row_list) == 4:
                row_list.append(None)
            self._capabilities[cast("str", row_list[0])] = row_list
        self._defines_rows = defines_rows
        self._expresses_rows = expresses_rows
        self._has_rows = has_rows
        self._satisfied_by_rows = satisfied_by_rows
        self._requires_rows = requires_rows
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
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "[e:DEFINES]" in q:
            return _FakeQueryResult(self._defines_rows)
        if "[e:EXPRESSES]" in q:
            return _FakeQueryResult(self._expresses_rows)
        if "[:HAS]" in q:
            return _FakeQueryResult(self._has_rows)
        if "[:SATISFIED_BY]" in q:
            return _FakeQueryResult(self._satisfied_by_rows)
        if "[:REQUIRES]" in q:
            return _FakeQueryResult(self._requires_rows)
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
        if "(n:Policy) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(self._policy_rows, ("id", "title", "status", "confidence"))
            )
        if "(n:Standard) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(
                    self._standard_rows,
                    ("id", "title", "implementation_status", "confidence", "description"),
                )
            )
        if "(n:Control) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(
                    self._control_rows,
                    ("id", "type", "title", "implementation_status", "confidence", "description"),
                )
            )
        if "(n:PracticeArea) RETURN" in q:
            return _FakeQueryResult(self._practice_area_rows)
        if "(n:RiskPath) RETURN" in q:
            return _FakeQueryResult(self._risk_path_rows)
        if "n.role_id" in q:
            return _FakeQueryResult(self._requirement_rows)
        if "n.description" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "n.name, n.confidence" in q:
            return _FakeQueryResult(self._role_rows)
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult(self._obligation_rows)
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
            )
        if "WHERE n.id IN $ids RETURN n.id, n.embedding IS NULL" in q:
            # backfill_incoming_capability_embeddings' read of which of its ids exist
            # and still have no embedding (the write is a gateway group on the baseline graph).
            assert params is not None
            return _FakeQueryResult(
                [
                    [i, self._capabilities[i][4] is None]
                    for i in cast("list[str]", params["ids"])
                    if i in self._capabilities
                ]
            )
        raise AssertionError(f"unexpected query issued: {q!r}")

    @staticmethod
    def _node_rows(rows: list[object], keys: tuple[str, ...]) -> list[object]:
        """Governance nodes are read whole (`RETURN n`): one single-node row per scripted row.

        The scripted rows keep their column order; a `None` column is an absent property.
        """
        return [
            [
                _FakeRegulatoryInstrumentNode(
                    {
                        key: value
                        for key, value in zip(keys, cast("list[object]", row), strict=True)
                        if value is not None
                    }
                )
            ]
            for row in rows
        ]

    def capability_embedding(self, node_id: str) -> tuple[float, ...] | None:
        """Test accessor: this baseline graph's own current cached
        `embedding` for Capability `node_id`, or `None` if never backfilled.
        """
        row = self._capabilities.get(node_id)
        if row is None:
            return None
        raw = cast("list[float] | None", row[4])
        return tuple(raw) if raw is not None else None


class _FakeSingleTenantGraph:
    """Answers `read_existing_canonical_index`'s two possible read queries
    with pre-seeded rows and records every call (read AND write) it
    receives, in order -- the call log a caller inspects for the call-order
    proof (test (b)) and the edge-level dedup-target reachability proof
    (test (c)).

    The cross-regulation tests additionally require
    this fake to ACCUMULATE state ACROSS two separate `merge_baseline_graph`
    calls against the SAME instance -- simulating two regulations merged one
    after another into the same single-tenant graph, the way a real
    FalkorDB graph would. Every write is a gateway group applied to the rig's
    `policy_system` graph, and this fake answers its reads from that graph plus its seeded rows,
    so a SECOND call's `read_existing_canonical_index` read sees everything the FIRST call
    wrote. It refuses a write statement.
    """

    def __init__(
        self,
        *,
        obligation_rows: list[object] | None = None,
        capability_rows: list[object] | None = None,
        policy_rows: list[object] | None = None,
        practice_area_rows: list[object] | None = None,
        risk_path_rows: list[object] | None = None,
        gateway_graph: InMemoryGraph | None = None,
    ) -> None:
        # The Obligation nodes a merge submits through the gateway live in the rig's
        # `policy_system` graph; the existence reads below are answered from it too.
        self._gateway_graph = gateway_graph
        self._obligations: dict[str, list[object]] = {}
        for row in obligation_rows or []:
            row_list = list(cast("list[object]", row))
            self._obligations[cast("str", row_list[0])] = row_list
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows or []:
            row_list = list(cast("list[object]", row))
            self._capabilities[cast("str", row_list[0])] = row_list
        self._policies: dict[str, list[object]] = {}
        for row in policy_rows or []:
            row_list = list(cast("list[object]", row))
            self._policies[cast("str", row_list[0])] = row_list
        # `practice_area_rows`/`risk_path_rows` mirror
        # `capability_rows`/`policy_rows`'s "seed pre-existing rows" role,
        # but store the FULL properties dict per row (`[id, properties]`)
        # rather than a fixed `[id, text, embedding]` shape -- PracticeArea/
        # RiskPath have no embedding and richer properties than a single
        # text field, and tests assert individual property
        # values (e.g. `description`) are unchanged after a merge.
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
        # A seeded row is a node the graph already holds, so an edge to it is valid (the gateway
        # rejects a group edge whose endpoint is missing from the graph).
        for label, ids in (
            ("Obligation", self._obligations),
            ("Capability", self._capabilities),
            ("Policy", self._policies),
            ("PracticeArea", self._practice_areas),
            ("RiskPath", self._risk_paths),
        ):
            for node_id in ids:
                if gateway_graph is not None and (label, node_id) not in gateway_graph.nodes:
                    gateway_graph.seed_node((label,), node_id, {})

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        call = _RecordedCall(q, params)
        self.calls.append(call)
        if self._gateway_graph is not None and _is_read_call(call):
            self._gateway_graph.events.append(_DEDUP_READ_EVENT)
        if "(m:Merged" in q or "(m:Resolved" in q:  # no redirect markers
            return _FakeQueryResult([])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult(self._capability_index_rows())
        if "-[:DEFINES]->(n:Role) RETURN n.id, n.name, n.embedding" in q:
            assert params is not None
            return _FakeQueryResult(self._defined_role_rows(cast("str", params["instrument_id"])))
        if _UNRESOLVED_PAIRS_READ.match(q) is not None:
            assert params is not None
            return _FakeQueryResult(self._unresolved_review_rows(params))
        if "(n:Policy) RETURN n.id, n.title, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._policies.values()])
        existence = _EXISTENCE_READ.search(q)
        if existence is not None and "$ids" in q:
            # the existing-node reads (`_read_existing_ids`, `count_new_obligations`) of the labels
            # whose writes are gateway groups
            assert params is not None
            held = self._held_ids(existence.group(1))
            return _FakeQueryResult([[i] for i in cast("list[str]", params["ids"]) if i in held])
        state_read = _EMBEDDING_STATE_READ.search(q)
        if state_read is not None:
            # The backfills' read of which of their ids exist and have no embedding
            # (the write is a gateway group on `policy_system`)
            assert params is not None
            table = {
                "Capability": self._capabilities,
                "Policy": self._policies,
            }[state_read.group(1)]
            return _FakeQueryResult(
                [
                    [i, self._embedding_of(state_read.group(1), i, table) is None]
                    for i in cast("list[str]", params["ids"])
                    if i in table or (state_read.group(1), i) in self._gateway_nodes()
                ]
            )
        if q == ANY_LABEL_EXISTENCE_READ:
            # `validate_classification_edge_endpoints`'s
            # batched existence check -- answer with whichever requested ids
            # are actually present in ANY of this fake's tracked node tables
            # (mirrors a real `MATCH (n {id: id})` with no label filter).
            assert params is not None
            requested_ids = cast("list[str]", params["ids"])
            known_ids = (
                set(self.obligation_ids())
                | set(self._capabilities)
                | set(self._policies)
                | set(self._practice_areas)
                | set(self._risk_paths)
                | {node_id for _label, node_id in self._gateway_nodes()}
            )
            return _FakeQueryResult([[rid] for rid in requested_ids if rid in known_ids])
        return _FakeQueryResult([[0]])

    def _defined_role_rows(self, instrument_id: str) -> list[object]:
        """`[id, name, embedding]` of the Roles `instrument_id` `DEFINES` in the gateway graph."""
        if self._gateway_graph is None:
            return []
        defined = {
            key[4]
            for key in self._gateway_graph.edges
            if key[0] == "DEFINES" and key[2] == instrument_id and key[3] == "Role"
        }
        return [
            [node_id, props.get("name"), props.get("embedding")]
            for (label, node_id), props in self._gateway_graph.nodes.items()
            if label == "Role" and node_id in defined
        ]

    def _unresolved_review_rows(self, params: dict[str, object]) -> list[object]:
        """The `$pairs` the gateway-written `PendingReview` nodes of `$kind` already name."""
        wanted = {
            (pair["incoming_id"], pair["nearest_existing_id"])
            for pair in cast("list[dict[str, str]]", params["pairs"])
        }
        return [
            [props["incoming_id"], props["nearest_existing_id"]]
            for (label, _), props in self._gateway_nodes().items()
            if label == "PendingReview"
            and props.get("kind") == params["kind"]
            and (props["incoming_id"], props["nearest_existing_id"]) in wanted
        ]

    def calls_matching(self, substring: str) -> list[_RecordedCall]:
        return [call for call in self.calls if substring in call.query]

    def edges_of(self, relationship: str) -> list[dict[str, str]]:
        """The `relationship` edges the gateway has written, as `{"source_id", "target_id"}`.

        An edge is a gateway primitive, not a statement this fake receives.
        """
        if self._gateway_graph is None:
            return []
        return [
            {"source_id": key[2], "target_id": key[4]}
            for key in self._gateway_graph.edges
            if key[0] == relationship
        ]

    def policy_ids(self) -> set[str]:
        """The ids of every Policy node currently held (seeded or gateway-written)."""
        return set(self._policies) | {i for lbl, i in self._gateway_nodes() if lbl == "Policy"}

    def _capability_index_rows(self) -> list[object]:
        """`[id, name, embedding]` of the seeded Capabilities plus those the gateway has minted.

        A seeded row without an embedding shows the one a gateway backfill has set since.
        """
        rows: dict[str, list[object]] = {key: list(row) for key, row in self._capabilities.items()}
        for key, row in rows.items():
            row[2] = self._embedding_of("Capability", key, self._capabilities)
        for (label, node_id), props in self._gateway_nodes().items():
            if label == "Capability" and node_id not in rows:
                rows[node_id] = [node_id, props.get("name"), props.get("embedding")]
        return list(rows.values())

    def _embedding_of(
        self, label: str, node_id: str, table: dict[str, list[object]]
    ) -> object | None:
        """The embedding a node holds: the seeded row's, else what the gateway has written."""
        row = table.get(node_id)
        if row is not None and row[2] is not None:
            return row[2]
        node = self._gateway_nodes().get((label, node_id))
        return node.get("embedding") if node is not None else None

    def _gateway_nodes(self) -> dict[tuple[str, str], dict[str, object]]:
        """The nodes the merge's gateway groups have written (empty without a rig graph)."""
        return self._gateway_graph.nodes if self._gateway_graph is not None else {}

    def _held_ids(self, label: str) -> set[str]:
        """Ids of the `label` nodes held: the seeded rows plus what the gateway has written."""
        seeded: Mapping[str, object] = {
            "Obligation": self._obligations,
            "Capability": self._capabilities,
            "Policy": self._policies,
            "PracticeArea": self._practice_areas,
            "RiskPath": self._risk_paths,
        }.get(label, {})
        return set(seeded) | {i for lbl, i in self._gateway_nodes() if lbl == label}

    def obligation_ids(self) -> frozenset[str]:
        """Increments 15/16: the current set of distinct canonical
        Obligation node ids accumulated so far across however many
        `merge_baseline_graph` calls have run against this instance -- the
        direct proof that a cross-regulation exact/semantic match never
        mints a second node for the same canonical concept.
        """
        return frozenset(self._held_ids("Obligation"))

    def practice_area_properties(self, node_id: str) -> dict[str, object] | None:
        """The current stored properties for a PracticeArea `node_id`.

        `None` if no PracticeArea with this id was ever seeded or written through the
        gateway -- a public read-back accessor,
        mirroring `obligation_ids()`'s role, so tests never reach into
        `_practice_areas` directly.
        """
        return self._practice_areas.get(node_id) or self._gateway_nodes().get(
            ("PracticeArea", node_id)
        )

    def risk_path_properties(self, node_id: str) -> dict[str, object] | None:
        """The current stored properties for a RiskPath `node_id`.

        `None` if no RiskPath with this id was ever seeded or written through the
        gateway -- see `practice_area_properties`.
        """
        return self._risk_paths.get(node_id) or self._gateway_nodes().get(("RiskPath", node_id))


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text`."""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        vector = self._vectors_by_text.get(text)
        if vector is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


class _ScriptedCallEmbeddingWithFailure:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text` --
    a scripted `Exception` value is raised instead of returning a response,
    mirroring `test_dedup_abort_on_embedding_failure.py`'s
    `_ScriptedCallEmbedding` (the established idiom for this
    scenario). A separate class from `_ScriptedCallEmbedding` above, which
    every OTHER test in this file scripts with plain vectors only.
    """

    def __init__(self, vectors_by_text: dict[str, list[float] | Exception]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        scripted = self._vectors_by_text.get(text)
        if scripted is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        if isinstance(scripted, Exception):
            raise scripted
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=scripted, index=0, object="embedding")]
        )


def _everything_new_baseline_graph() -> _FakeBaselineGraph:
    """The "everything is new" fixture: one Role, one Requirement, one
    Obligation, one Capability, fully wired edges. No pre-existing canonical
    node exists anywhere, so nothing has anything to converge onto.
    """
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-1.0_req_art_1.1"
    obligation_text = "Report the incident to the competent authority."
    obligation_node_id = obligation_id(role_node_id, obligation_text)
    capability_name = "Incident Reporting Capability"
    capability_node_id = capability_id(capability_name)

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-1.0", "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9, None]],
        requirement_rows=[
            [
                requirement_node_id,
                "Must report incidents.",
                "requirement",
                0.9,
                role_node_id,
            ]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[capability_node_id, capability_name, 0.8, None]],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, capability_node_id]],
    )


def test_everything_new_writes_every_node_type_directly(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """(a) "everything is new": an empty existing single-tenant
    graph -> every node type present afterward in the fake single-tenant
    graph's write log, directly (Obligation via create-only passthrough;
    Capability via match_kind="new", nothing to
    converge onto).
    """
    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    result = merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert len(result.obligation_ids) == 1
    assert len(result.capability_canonical_ids) == 1
    assert result.near_misses == ()

    # RegulatoryInstrument/Role/Requirement leave as one gateway group
    assert {entry.name for group in merge_rig.groups for entry in group.entries} >= {
        "RegulatoryInstrument",
        "Role",
        "Requirement",
    }
    assert {entry.name for group in merge_rig.groups for entry in group.entries} >= {"Obligation"}
    assert any(key[0] == "Capability" for key in merge_rig.policy_system.nodes)
    assert {key[0] for key in merge_rig.policy_system.edges} >= {"HAS", "SATISFIED_BY", "REQUIRES"}


def test_both_dedup_reads_complete_before_any_write_call(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """(b) call-order proof: the Capability dedup pass completes (its
    read_existing_canonical_index read happens) before any write call
    appears in the fake single_tenant_graph's call log.
    """
    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    read_positions = [i for i, call in enumerate(single_tenant.calls) if _is_read_call(call)]
    # Capability index + Role index + one marker read per marker label (Merged<Kind> for each
    # kind, plus ResolvedCapability)
    assert len(read_positions) == 6
    # A write is a gateway group: its first log append must come after the last read
    events = merge_rig.events
    assert events.count(_DEDUP_READ_EVENT) == 6
    assert max(i for i, event in enumerate(events) if event == _DEDUP_READ_EVENT) < events.index(
        "log_append"
    )


def test_capability_dedup_target_reused_obligation_passed_through_at_edge_level(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """(c) Q3's fix, "the Capability dedup target is reused," proven
    end-to-end within ONE call: pre-seed the fake single_tenant_graph with
    one existing canonical Capability node BEFORE calling
    merge_baseline_graph. The Capability resolves via exact-key match (same
    name -> same capability_id hash) -> no new Capability node write, and the
    `REQUIRES` edge targets the pre-existing canonical id.

    The Obligation is a passthrough node: it is written with
    create-only under its own baseline-local
    (Role-scoped) id, and the Role's `HAS` / Requirement's `SATISFIED_BY`
    edges target that same id -- never a "canonical" id, because there is no
    Obligation dedup.
    """
    emitter, _log_path = make_emitter()
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-1.0_req_art_1.1"

    obligation_text = "Report the security incident to the competent authority."
    obligation_node_id = obligation_id(role_node_id, obligation_text)

    shared_capability_name = "Encrypt Data At Rest"
    shared_capability_id = capability_id(shared_capability_name)

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-1.0", "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9, None]],
        requirement_rows=[
            [
                requirement_node_id,
                "Must report incidents.",
                "requirement",
                0.9,
                role_node_id,
            ]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[shared_capability_id, shared_capability_name, 0.8, None]],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, shared_capability_id]],
    )
    single_tenant = _FakeSingleTenantGraph(
        gateway_graph=merge_rig.policy_system,
        capability_rows=[[shared_capability_id, shared_capability_name, None]],
    )

    result = merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert result.obligation_ids == (obligation_node_id,)
    assert result.capability_canonical_ids == (shared_capability_id,)

    assert _minted_capability_ids(merge_rig) == [], (
        "no new Capability node should have been minted -- the dedup target was reused"
    )
    assert ("Obligation", obligation_node_id) in merge_rig.policy_system.nodes, (
        "the Obligation is written straight through as a passthrough node"
    )

    has_writes = single_tenant.edges_of("HAS")
    assert len(has_writes) == 1
    assert has_writes[0] == {
        "source_id": role_node_id,
        "target_id": obligation_node_id,
    }

    satisfied_by_writes = single_tenant.edges_of("SATISFIED_BY")
    assert len(satisfied_by_writes) == 1
    assert satisfied_by_writes[0] == {
        "source_id": requirement_node_id,
        "target_id": obligation_node_id,
    }

    requires_writes = single_tenant.edges_of("REQUIRES")
    assert len(requires_writes) == 1
    assert requires_writes[0] == {
        "source_id": obligation_node_id,
        "target_id": shared_capability_id,
    }


def test_missing_merge_threshold_raises_before_any_call(merge_rig: MergeRig) -> None:
    """(d) B1's fix, runtime enforcement: merge_threshold=None raises
    CompanyMergeConfigurationError; both the fake baseline_graph and fake
    single_tenant_graph receive ZERO calls of any kind (the check fires
    before even the Regulation read).
    """
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    with pytest.raises(
        CompanyMergeConfigurationError, match="PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD"
    ):
        merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            merge_threshold=None,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            **merge_rig.merge_keywords(),
        )

    assert baseline.calls == []
    assert single_tenant.calls == []


def test_missing_review_threshold_raises_before_any_call(merge_rig: MergeRig) -> None:
    """`review_threshold=None` raises `CompanyMergeConfigurationError` naming the review variable;
    neither graph receives a call of any kind.
    """
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    with pytest.raises(
        CompanyMergeConfigurationError, match="PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD"
    ):
        merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            merge_threshold=_THRESHOLD,
            review_threshold=None,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            **merge_rig.merge_keywords(),
        )

    assert baseline.calls == []
    assert single_tenant.calls == []


def test_review_threshold_above_merge_threshold_raises_before_any_call(
    merge_rig: MergeRig,
) -> None:
    """A review floor above the merge threshold is rejected before either graph is read."""
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    with pytest.raises(CompanyMergeConfigurationError, match="PS_COMPANYMERGE_CAPABILITY_"):
        merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            merge_threshold=0.6,
            review_threshold=0.8,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            **merge_rig.merge_keywords(),
        )

    assert baseline.calls == []
    assert single_tenant.calls == []


def test_succeeded_log_entry_carries_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    """With bind_run_context("run-x"), the
    outcome="succeeded" entry for action="merge_baseline_graph" carries
    run_id="run-x".
    """
    emitter, log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    with bind_run_context("run-x"):
        merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            merge_threshold=_THRESHOLD,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            emitter=emitter,
            **merge_rig.merge_keywords(),
        )
    emitter.flush()

    entries = read_lines(log_path)
    succeeded = [
        e
        for e in entries
        if e.get("action") == "merge_baseline_graph" and e.get("outcome") == "succeeded"
    ]
    assert len(succeeded) == 1
    assert succeeded[0]["run_id"] == "run-x"


def test_dedup_decision_log_entries_carry_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    """At least one per-decision entry for
    action="dedupe_canonical_nodes" also carries run_id="run-x", with
    entity_id equal to one of the resolved nodes' incoming_id and outcome
    equal to its match_kind.
    """
    emitter, log_path = make_emitter()
    baseline = _everything_new_baseline_graph()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    with bind_run_context("run-x"):
        result = merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            merge_threshold=_THRESHOLD,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            emitter=emitter,
            **merge_rig.merge_keywords(),
        )
    emitter.flush()

    entries = read_lines(log_path)
    dedup_entries = [e for e in entries if e.get("action") == "dedupe_canonical_nodes"]
    assert dedup_entries
    for entry in dedup_entries:
        assert entry["run_id"] == "run-x"

    resolved_capability_id = result.capability_canonical_ids[0]
    matching = [e for e in dedup_entries if e.get("entity_id") == resolved_capability_id]
    assert len(matching) == 1
    assert matching[0]["outcome"] == "new"


# ---------------------------------------------------------------------------
# Across two separate merge_baseline_graph calls against the SAME
# accumulating _FakeSingleTenantGraph instance (two regulations merged one
# after another): Obligation is Role-scoped and passed through, never
# deduped, so two sources' duties are always distinct nodes; only Capability
# converges. The Capability REQUIRES-rewiring is proven
# cross-regulation at the end of this section.
# ---------------------------------------------------------------------------


def _single_obligation_baseline_graph(
    *,
    regulatory_instrument_id: str,
    role_id_value: str,
    requirement_id_value: str,
    obligation_text: str,
) -> _FakeBaselineGraph:
    """A minimal baseline graph fixture: one Role, one Requirement, one
    Obligation, `HAS`/`SATISFIED_BY` wired, no Capability.
    """
    obligation_node_id = obligation_id(role_id_value, obligation_text)
    return _FakeBaselineGraph(
        regulatory_instrument_properties={
            "id": regulatory_instrument_id,
            "title": f"Test Regulation {regulatory_instrument_id}",
        },
        role_rows=[[role_id_value, "Manufacturer", 0.9, None]],
        requirement_rows=[
            [
                requirement_id_value,
                "Must report incidents.",
                "requirement",
                0.9,
                role_id_value,
            ]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[],
        defines_rows=[[role_id_value, "Article 1(1)"]],
        expresses_rows=[[requirement_id_value, "Article 1(1)"]],
        has_rows=[[role_id_value, obligation_node_id]],
        satisfied_by_rows=[[requirement_id_value, obligation_node_id]],
        requires_rows=[],
    )


def test_cross_regulation_obligations_are_passed_through_never_deduped(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """Two regulations whose duty text is IDENTICAL but whose Roles
    differ (Roles are regulation-scoped) produce two DISTINCT Obligation
    nodes -- each written straight through under its own Role-scoped id, with
    its own single `HAS` edge. Company Merge runs no exact-key or semantic
    dedup for Obligation, and makes zero embedding calls for it.
    """
    emitter, _log_path = make_emitter()
    shared_text = "Report the incident to the competent authority without undue delay."
    id_a = obligation_id("role_a_manufacturer", shared_text)
    id_b = obligation_id("role_b_provider", shared_text)
    assert id_a != id_b

    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    merge_baseline_graph(
        "REG-A",
        baseline_graph=_single_obligation_baseline_graph(
            regulatory_instrument_id="REG-A",
            role_id_value="role_a_manufacturer",
            requirement_id_value="REG-A_req_art_1.1",
            obligation_text=shared_text,
        ),
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )
    assert single_tenant.obligation_ids() == {id_a}

    call_embedding = _ScriptedCallEmbedding({})  # must never be called for Obligation
    result_b = merge_baseline_graph(
        "REG-B",
        baseline_graph=_single_obligation_baseline_graph(
            regulatory_instrument_id="REG-B",
            role_id_value="role_b_provider",
            requirement_id_value="REG-B_req_art_2.1",
            obligation_text=shared_text,
        ),
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert result_b.obligation_ids == (id_b,)
    assert result_b.near_misses == ()
    # Both distinct nodes now exist -- never merged.
    assert single_tenant.obligation_ids() == {id_a, id_b}
    # No embedding call was ever made for an Obligation.
    assert call_embedding.calls == []
    # No dedup read query for Obligation was ever issued.
    assert not single_tenant.calls_matching("(n:Obligation) RETURN n.id, n.text, n.embedding")

    has_writes = single_tenant.edges_of("HAS")
    assert any(c == {"source_id": "role_b_provider", "target_id": id_b} for c in has_writes)


def test_cross_regulation_capability_requires_edge_converges_via_exact_key_match(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """Capability-side cross-regulation
    convergence via a `REQUIRES` edge. A `persist_rewired_edges`
    that only rewrote an edge's Obligation-typed endpoint would leave a
    `REQUIRES` edge's Capability-typed TARGET pointing at its baseline-local
    id whenever that Capability resolved onto an existing canonical node --
    a silent dangling reference, since a matched Capability is never minted
    as its own node. The rewiring is proven WITHIN one
    call elsewhere; this test proves it holds ACROSS two separate
    `merge_baseline_graph` calls too, applied to Capability instead of Obligation.
    """
    emitter, _log_path = make_emitter()
    shared_capability_name = "Multi-Factor Authentication Capability"
    canonical_capability_id = capability_id(shared_capability_name)

    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    first_obligation_text = "Implement strong authentication for remote access."
    first_obligation_id = obligation_id("role_g_operator", first_obligation_text)
    first_regulatory_instrument = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-G", "title": "Test Regulation REG-G"},
        role_rows=[["role_g_operator", "Operator", 0.9, None]],
        requirement_rows=[
            [
                "REG-G_req_art_9.1",
                "Must secure remote access.",
                "requirement",
                0.9,
                "role_g_operator",
            ]
        ],
        obligation_rows=[[first_obligation_id, first_obligation_text, 0.9]],
        capability_rows=[[canonical_capability_id, shared_capability_name, 0.8, None]],
        defines_rows=[["role_g_operator", "Article 9(1)"]],
        expresses_rows=[["REG-G_req_art_9.1", "Article 9(1)"]],
        has_rows=[["role_g_operator", first_obligation_id]],
        satisfied_by_rows=[["REG-G_req_art_9.1", first_obligation_id]],
        requires_rows=[[first_obligation_id, canonical_capability_id]],
    )
    merge_baseline_graph(
        "REG-G",
        baseline_graph=first_regulatory_instrument,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )
    assert len(_minted_capability_ids(merge_rig)) == 1

    # The Obligation side needs no special handling: Obligation is
    # a passthrough node (no dedup pass, no call_embedding), so REG-H's
    # Obligation lands under its own Role-scoped id regardless of text.
    second_obligation_text = "Enforce multi-factor authentication for all users."
    second_obligation_id = obligation_id("role_h_operator", second_obligation_text)
    second_regulatory_instrument = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-H", "title": "Test Regulation REG-H"},
        role_rows=[["role_h_operator", "Operator", 0.9, None]],
        requirement_rows=[
            [
                "REG-H_req_art_10.1",
                "Must enforce MFA.",
                "requirement",
                0.9,
                "role_h_operator",
            ]
        ],
        obligation_rows=[[second_obligation_id, second_obligation_text, 0.9]],
        capability_rows=[[canonical_capability_id, shared_capability_name, 0.8, None]],
        defines_rows=[["role_h_operator", "Article 10(1)"]],
        expresses_rows=[["REG-H_req_art_10.1", "Article 10(1)"]],
        has_rows=[["role_h_operator", second_obligation_id]],
        satisfied_by_rows=[["REG-H_req_art_10.1", second_obligation_id]],
        requires_rows=[[second_obligation_id, canonical_capability_id]],
    )
    result_h = merge_baseline_graph(
        "REG-H",
        baseline_graph=second_regulatory_instrument,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert result_h.capability_canonical_ids == (canonical_capability_id,)
    # No second Capability node was ever minted -- the dedup target was
    # reused via exact-key match (same capability name -> same
    # capability_id hash across both regulations).
    assert len(_minted_capability_ids(merge_rig)) == 1

    requires_writes = single_tenant.edges_of("REQUIRES")
    assert any(
        c == {"source_id": second_obligation_id, "target_id": canonical_capability_id}
        for c in requires_writes
    ), (
        "REQUIRES edge's Capability TARGET must be rewritten to the canonical id "
        "across the two calls -- the exact Increment 12 bug scenario, proven "
        "cross-regulation"
    )


# ---------------------------------------------------------------------------
# Policy import + Standard/Control passthrough +
# governance-edge rewiring, guarded by `if graph.policy_nodes:`.
# ---------------------------------------------------------------------------


def _internal_baseline_with_governance() -> _FakeBaselineGraph:
    """One Capability, one Policy, one Standard, one Control, fully wired
    with `GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY` -- an internal-sourced
    baseline (`DeriveGovernanceArtifacts` ran). No Role/Requirement/
    Obligation content is needed for these tests -- only the governance
    layer's own merge behavior is under test.
    """
    capability_id_value = "cap_engineering_review_abc"
    policy_id_value = "pol_engineering_practices_xyz"
    standard_id_value = "std_pol_engineering_practices_xyz_v1"
    control_id_value = "ctrl_std_pol_engineering_practices_xyz_v1_manual"

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "ENGPRAC-3.0", "title": "Engineering Practices"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[capability_id_value, "Engineering Review Capability", 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        policy_rows=[
            [policy_id_value, "Engineering Practices Policy", "draft", 0.9],
        ],
        standard_rows=[
            [standard_id_value, "Code Review Standard", "draft", 0.85, None],
        ],
        control_rows=[
            [control_id_value, "manual", "Peer Review Control", "planned", 0.8, None],
        ],
        governed_by_rows=[[capability_id_value, policy_id_value]],
        supported_by_rows=[[policy_id_value, standard_id_value]],
        implemented_by_rows=[[standard_id_value, control_id_value]],
    )


def test_internal_baseline_merges_policy_standard_control_into_single_tenant(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """An internal-sourced baseline (`graph.policy_nodes` non-empty)
    is imported by the shared draft-governance pass -- Policy/Standard/Control
    written create-only -- and `GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY`
    are rewritten, closing the loop to a real `Capability-[:GOVERNED_BY]->
    Policy-[:SUPPORTED_BY]->Standard-[:IMPLEMENTED_BY]->Control` traversal in
    the single-tenant graph.
    """
    emitter, _log_path = make_emitter()
    baseline = _internal_baseline_with_governance()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    result = merge_baseline_graph(
        "ENGPRAC-3.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        governance=_GOVERNANCE,
        **merge_rig.merge_keywords(),
    )

    assert (result.governance_policies, result.governance_standards) == (1, 1)
    assert result.governance_controls == 1

    created = {entry.name for group in merge_rig.groups for entry in group.entries}
    assert {"Policy", "Standard", "Control"} <= created
    for label in ("Policy", "Standard", "Control"):
        assert any(key[0] == label for key in merge_rig.policy_system.nodes)

    governed_by_writes = single_tenant.edges_of("GOVERNED_BY")
    assert len(governed_by_writes) == 1
    assert governed_by_writes[0] == {
        "source_id": "cap_engineering_review_abc",
        "target_id": "pol_engineering_practices_xyz",
    }

    supported_by_writes = single_tenant.edges_of("SUPPORTED_BY")
    assert len(supported_by_writes) == 1
    assert supported_by_writes[0] == {
        "source_id": "pol_engineering_practices_xyz",
        "target_id": "std_pol_engineering_practices_xyz_v1",
    }

    implemented_by_writes = single_tenant.edges_of("IMPLEMENTED_BY")
    assert len(implemented_by_writes) == 1
    assert implemented_by_writes[0] == {
        "source_id": "std_pol_engineering_practices_xyz_v1",
        "target_id": "ctrl_std_pol_engineering_practices_xyz_v1_manual",
    }


def _baseline_graph_with_classification_nodes(
    *,
    practice_area_rows: list[object],
    risk_path_rows: list[object],
    extra_capability_rows: list[object] | None = None,
    covers_rows: list[object] | None = None,
    owns_rows: list[object] | None = None,
    mitigated_by_rows: list[object] | None = None,
    verified_by_rows: list[object] | None = None,
    policy_rows: list[object] | None = None,
    standard_rows: list[object] | None = None,
    control_rows: list[object] | None = None,
) -> _FakeBaselineGraph:
    """`_everything_new_baseline_graph()`'s regulatory spine, plus
    PracticeArea/RiskPath rows -- used by the classification tests below, which only care about the
    classification-layer behavior, not the regulatory spine itself. `extra_capability_rows`/
    `covers_rows`/etc. let a test add classification-edge content without
    every caller having to restate the whole regulatory spine.
    """
    role_node_id = "role_manufacturer_abc123"
    requirement_node_id = "REG-1.0_req_art_1.1"
    obligation_text = "Report the incident to the competent authority."
    obligation_node_id = obligation_id(role_node_id, obligation_text)
    capability_name = "Incident Reporting Capability"
    capability_node_id = capability_id(capability_name)

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-1.0", "title": "Test Regulation"},
        role_rows=[[role_node_id, "Manufacturer", 0.9, None]],
        requirement_rows=[
            [
                requirement_node_id,
                "Must report incidents.",
                "requirement",
                0.9,
                role_node_id,
            ]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[
            [capability_node_id, capability_name, 0.8, None],
            *(extra_capability_rows or []),
        ],
        defines_rows=[[role_node_id, "Article 1(1)"]],
        expresses_rows=[[requirement_node_id, "Article 1(1)"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, capability_node_id]],
        practice_area_rows=practice_area_rows,
        risk_path_rows=risk_path_rows,
        covers_rows=covers_rows,
        owns_rows=owns_rows,
        mitigated_by_rows=mitigated_by_rows,
        verified_by_rows=verified_by_rows,
        policy_rows=policy_rows,
        standard_rows=standard_rows,
        control_rows=control_rows,
    )


def test_practice_area_and_risk_path_nodes_pass_through_to_single_tenant(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """(PracticeArea+RiskPath halves): a baseline carrying
    PracticeArea/RiskPath nodes (with name/description/status/version/
    owner_id or risk_type) merges and both land in the single-tenant graph
    with every property intact -- the exact-identity passthrough
    (`graph_writer.persist_practice_area_and_risk_path_passthrough`).
    """
    emitter, _log_path = make_emitter()
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    risk_path_id_value = "rp_secure_build_release_d93f8a"

    baseline = _baseline_graph_with_classification_nodes(
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
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert single_tenant.practice_area_properties(practice_area_id_value) == {
        "name": "Secure SDLC",
        "status": "active",
        "description": "Secure development lifecycle practices",
        "version": "1.0",
        "owner_id": "role_ciso",
    }
    assert single_tenant.risk_path_properties(risk_path_id_value) == {
        "name": "Secure Build & Release",
        "status": "active",
        "description": "Risks in the build/release pipeline",
        "risk_type": "operational",
        "version": "2.0",
    }
    names = {entry.name for group in merge_rig.groups for entry in group.entries}
    assert {"PracticeArea", "RiskPath"} <= names


def test_practice_area_and_risk_path_properties_unchanged_when_node_already_exists(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """(Both halves) a PracticeArea/
    RiskPath node that already exists in the single-tenant graph keeps its
    existing properties after the merge -- create-only
    semantics, not an overwrite -- even though the incoming baseline node
    shares the same `id` but carries different property values. The writer
    reads the existing ids first and leaves them out of its gateway group,
    so nothing is logged for either node.
    """
    emitter, _log_path = make_emitter()
    practice_area_id_value = "pa_existing"
    risk_path_id_value = "rp_existing"

    baseline = _baseline_graph_with_classification_nodes(
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
    single_tenant = _FakeSingleTenantGraph(
        gateway_graph=merge_rig.policy_system,
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

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert single_tenant.practice_area_properties(practice_area_id_value) == {
        "name": "Secure SDLC",
        "status": "active",
        "description": "original",
        "version": "1.0",
        "owner_id": "role_ciso",
    }
    assert single_tenant.risk_path_properties(risk_path_id_value) == {
        "name": "Secure Build & Release",
        "status": "active",
        "description": "original",
        "risk_type": "operational",
        "version": "1.0",
    }
    # nothing was logged for either node: the existing ones were left out of the group
    names = {entry.name for group in merge_rig.groups for entry in group.entries}
    assert not {"PracticeArea", "RiskPath"} & names


def test_capability_near_miss_persists_pending_review_node(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """A below-threshold near-miss
    surfaced during the Capability dedup pass is persisted as a
    `PendingReview` node -- wired at the same call site
    `_log_dedup_decisions(capability_dedup, ...)` already logs it from.
    """
    emitter, _log_path = make_emitter()
    existing_capability_id = "capability_existing_conduct_risk_assessment"
    existing_capability_name = "Conduct Risk Assessment Capability"
    incoming_capability_id = "capability_incoming_report_incident"
    incoming_capability_name = "Report Incident Capability"
    existing_vector = [1.0, 0.0]
    incoming_vector = [0.6, 0.8]  # cosine similarity with existing_vector is well below _THRESHOLD

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-NM", "title": "Test Regulation"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[incoming_capability_id, incoming_capability_name, 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
    )
    single_tenant = _FakeSingleTenantGraph(
        gateway_graph=merge_rig.policy_system,
        capability_rows=[[existing_capability_id, existing_capability_name, existing_vector]],
    )
    call_embedding = _ScriptedCallEmbedding({incoming_capability_name: incoming_vector})

    merge_baseline_graph(
        "REG-NM",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    pending_reviews = _review_nodes(merge_rig)
    assert len(pending_reviews) == 1
    params = pending_reviews[0]
    assert params["kind"] == "Capability"
    assert params["incoming_id"] == incoming_capability_id
    assert params["incoming_text"] == incoming_capability_name
    assert params["nearest_existing_id"] == existing_capability_id
    assert params["nearest_existing_text"] == existing_capability_name


def test_rerun_over_unchanged_input_creates_no_second_review_for_a_pair(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """A pair that already has an unresolved review is surfaced again but not recorded again.

    One incoming Capability sits near an existing one; a review for exactly that pair is already
    in the graph. `merge_baseline_graph` reads it back, keeps the pair in `near_misses` (the
    decision happened) and persists nothing, so `pending_review_count` is 0.
    """
    emitter, _log_path = make_emitter()
    existing_id, existing_name = "capability_existing_cra", "Conduct Risk Assessment Capability"
    incoming_id, incoming_name = "capability_incoming_ri", "Report Incident Capability"
    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-NM", "title": "Test Regulation"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[incoming_id, incoming_name, 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
    )
    single_tenant = _FakeSingleTenantGraph(
        gateway_graph=merge_rig.policy_system,
        capability_rows=[[existing_id, existing_name, [1.0, 0.0]]],
    )
    persist_pending_reviews(
        GraphWriteTarget(merge_rig.gateway, "policy_system", merge_rig.audit_event_id),
        (
            NearMissPair(
                incoming_id=incoming_id,
                incoming_text=incoming_name,
                nearest_existing_id=existing_id,
                nearest_existing_text=existing_name,
                similarity=0.6,
            ),
        ),
        kind=CAPABILITY,
    )
    assert len(_review_nodes(merge_rig)) == 1

    result = merge_baseline_graph(
        "REG-NM",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=_ScriptedCallEmbedding({incoming_name: [0.6, 0.8]}),
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert len(_review_nodes(merge_rig)) == 1
    assert [(p.incoming_id, p.nearest_existing_id) for p in result.near_misses] == [
        (incoming_id, existing_id)
    ]
    assert result.pending_review_count == 0


def test_near_title_policy_is_a_separate_draft_with_no_embedding_call_and_no_review(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """A Policy whose title is only NEAR an existing Policy's is
    not converged onto it. It gets its own draft node, no Policy embedding is computed
    (the scripted embedder would fail the test on any call) and no Policy
    `PendingReview` is raised -- convergence is gone from the live path.
    """
    emitter, _log_path = make_emitter()
    existing_policy_id = "pol_existing_engineering_practices"
    incoming_policy_id = "pol_incoming_secure_development"

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "ENGPRAC-NM", "title": "Engineering Practices"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        policy_rows=[[incoming_policy_id, "Secure Development Policy", "draft", 0.9]],
    )
    single_tenant = _FakeSingleTenantGraph(
        gateway_graph=merge_rig.policy_system,
        policy_rows=[[existing_policy_id, "Engineering Practices Policy", [1.0, 0.0]]],
    )
    call_embedding = _ScriptedCallEmbedding({})

    result = merge_baseline_graph(
        "ENGPRAC-NM",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        governance=_GOVERNANCE,
        **merge_rig.merge_keywords(),
    )

    assert result.governance_policies == 1
    assert single_tenant.policy_ids() == {existing_policy_id, incoming_policy_id}
    assert _review_nodes(merge_rig) == []
    assert call_embedding.calls == []


def test_pending_review_count_covers_capability_near_misses_only(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """`MergeResult.pending_review_count` is the
    run-scoped count of `PendingReview` nodes THIS call persisted. Only Capability
    near-misses exist now: a below-threshold Capability pair and a near-title Policy
    both appear in the run, yet the count is 1, matching the one `CREATE (r:PendingReview
    ...)` write actually issued.
    """
    emitter, _log_path = make_emitter()
    existing_capability_id = "capability_existing_conduct_risk_assessment"
    existing_capability_name = "Conduct Risk Assessment Capability"
    incoming_capability_id = "capability_incoming_report_incident"
    incoming_capability_name = "Report Incident Capability"
    existing_vector = [1.0, 0.0]
    incoming_vector = [0.6, 0.8]  # cosine similarity with existing_vector is well below _THRESHOLD

    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-NM-2", "title": "Test Regulation"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[incoming_capability_id, incoming_capability_name, 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        policy_rows=[
            ["pol_incoming_secure_development", "Secure Development Policy", "draft", 0.9]
        ],
    )
    single_tenant = _FakeSingleTenantGraph(
        gateway_graph=merge_rig.policy_system,
        capability_rows=[[existing_capability_id, existing_capability_name, existing_vector]],
        policy_rows=[
            ["pol_existing_engineering_practices", "Engineering Practices Policy", existing_vector]
        ],
    )
    call_embedding = _ScriptedCallEmbedding({incoming_capability_name: incoming_vector})

    result = merge_baseline_graph(
        "REG-NM-2",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        governance=_GOVERNANCE,
        **merge_rig.merge_keywords(),
    )

    assert result.pending_review_count == 1
    assert len(_review_nodes(merge_rig)) == 1


def test_external_baseline_unaffected_by_policy_pass(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """S4: an external-sourced baseline (`graph.policy_nodes == ()`) is a
    structural no-op for the whole governance import -- all four
    governance counts are zero, no Policy dedup read ever issued, no
    Policy/Standard/Control write ever issued.
    """
    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()  # Capability-only, no governance rows
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    result = merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert result.governance_policies == 0
    assert not single_tenant.calls_matching("(n:Policy) RETURN n.id, n.title, n.embedding")
    assert not any(
        label in {"Policy", "Standard", "Control"} for label, _ in merge_rig.policy_system.nodes
    )
    assert not any(
        entry.name in {"Policy", "Standard", "Control"}
        for group in merge_rig.groups
        for entry in group.entries
    )


# ---------------------------------------------------------------------------
# COVERS/OWNS/MITIGATED_BY/VERIFIED_BY edge rewiring,
# the structural no-dedup guarantee, the pre-write
# validation, the structural no-op, the abort-with-no-
# partial-write, and the semantic log counts.
# ---------------------------------------------------------------------------


def test_external_baseline_unaffected_by_classification_pass(
    make_emitter: MakeEmitter, merge_rig: MergeRig
) -> None:
    """An external-sourced baseline (no `internal_seed` content
    at all) issues NO query matching any of the six classification-layer
    markers -- checked against `single_tenant.calls` (every call, read AND
    write), the stronger claim `validate_classification_edge_endpoints`'s
    early-return (before issuing its own `UNWIND` read) makes true.
    """
    emitter, _log_path = make_emitter()
    baseline = _everything_new_baseline_graph()  # Capability-only, no classification rows
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert not any(
        label in c.query
        for c in single_tenant.calls
        for label in ("PracticeArea", "RiskPath", "COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY")
    )


def test_two_baselines_authoring_same_practice_area_name_converge_on_one_node(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """Two independent baselines
    (simulating two separate `internal_seed` mints of the same PracticeArea
    `name`) arrive with the SAME content-hashed id, computed via the REAL
    `practice_area_id` -- not hand-assigned -- because that is how real
    convergence happens upstream of Company Merge. A single
    `merge_baseline_graph` call never recomputes this id; two SEPARATE calls
    against the SAME `_FakeSingleTenantGraph` instance prove real
    cross-baseline convergence, using the fake's own documented cross-call
    accumulation support.
    """
    shared_id = practice_area_id("Secure SDLC")
    cap_a_name = "Encrypt Data At Rest Capability"
    cap_a_id = capability_id(cap_a_name)
    cap_b_name = "Rotate Encryption Keys Capability"
    cap_b_id = capability_id(cap_b_name)

    emitter, _log_path = make_emitter()
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    baseline_a = _classification_only_baseline_graph(
        regulatory_instrument_id="REG-A",
        capability_rows=[[cap_a_id, cap_a_name, 0.8, None]],
        practice_area_rows=[[shared_id, "Secure SDLC", "draft", None, None, None]],
        covers_rows=[[shared_id, cap_a_id]],
    )
    baseline_b = _classification_only_baseline_graph(
        regulatory_instrument_id="REG-B",
        capability_rows=[[cap_b_id, cap_b_name, 0.8, None]],
        practice_area_rows=[[shared_id, "Secure SDLC", "draft", None, None, None]],
        covers_rows=[[shared_id, cap_b_id]],
    )
    # Orthogonal vectors -- cosine similarity 0, safely below _THRESHOLD --
    # so CAP_A/CAP_B never spuriously converge onto each other; only the
    # PracticeArea id (computed structurally, never via embedding) converges.
    call_embedding = _ScriptedCallEmbedding({cap_a_name: [1.0, 0.0], cap_b_name: [0.0, 1.0]})

    merge_baseline_graph(
        "REG-A",
        baseline_graph=baseline_a,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )
    merge_baseline_graph(
        "REG-B",
        baseline_graph=baseline_b,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    # Exactly one PracticeArea MERGE target across both calls.
    practice_area_entries = [
        e.identity for g in merge_rig.groups for e in g.entries if e.name == "PracticeArea"
    ]
    assert practice_area_entries == [shared_id]  # the second merge found it and logged nothing

    # Both baselines' COVERS edges resolve onto the same source id.
    covers_writes = single_tenant.edges_of("COVERS")
    assert {c["source_id"] for c in covers_writes} == {shared_id}
    assert {c["target_id"] for c in covers_writes} == {
        cap_a_id,
        cap_b_id,
    }

    # No RouteEmbedding-reachable call was ever made with the PracticeArea's
    # own name -- only the two Capability names, proving the PracticeArea
    # convergence above was never routed through any embedding comparison.
    assert set(call_embedding.calls) == {cap_a_name, cap_b_name}
    assert "Secure SDLC" not in call_embedding.calls


def test_merge_baseline_graph_never_dedupes_practice_area_or_risk_path(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """Within-a-single-call structural proof: a
    baseline carrying two PracticeArea rows AND two RiskPath rows, each pair
    minting to the SAME content-hashed id (simulating two separate incoming
    entries for the same `name`, converging structurally),
    plus two COVERS edges from the shared PracticeArea id to two different
    Capabilities.

    The proof uses the real resulting graph/embedding-fake state, not a patched
    collaborator: `merge.py`'s own `_persist_classification_
    passthrough` never calls `dedup.dedupe_canonical_nodes` for PracticeArea/
    RiskPath at all -- it goes straight to `graph_writer.persist_practice_
    area_and_risk_path_passthrough` -- so the real, unmodified
    `merge_baseline_graph` run below proves the claim two ways: (1) each
    passthrough kind still converges to exactly ONE node from its two
    same-id incoming rows, purely structurally, with zero embedding calls
    for either name, and (2) the embedding fake's own recorded call set is
    scripted with (and asserted to contain) ONLY the two Capability names --
    an unscripted PracticeArea/RiskPath embedding attempt would itself raise
    inside `_ScriptedCallEmbedding`, so this equality check is a real,
    failure-loud proof, not an omission.
    """
    shared_pa_id = practice_area_id("Secure SDLC")
    shared_rp_id = risk_path_id("Unpatched Internet-Facing Service")
    cap_a_id = "cap_encrypt_data_at_rest_abc"
    cap_b_id = "cap_rotate_encryption_keys_def"

    emitter, _log_path = make_emitter()
    baseline = _classification_only_baseline_graph(
        regulatory_instrument_id="REG-1.0",
        capability_rows=[
            [cap_a_id, "Encrypt Data At Rest Capability", 0.8, None],
            [cap_b_id, "Rotate Encryption Keys Capability", 0.8, None],
        ],
        # Two rows, same id -- simulating two separate incoming PracticeArea
        # entries that both minted the identical practice_area_id("Secure
        # SDLC") hash before Company Merge ever saw them.
        practice_area_rows=[
            [shared_pa_id, "Secure SDLC", "draft", None, None, None],
            [shared_pa_id, "Secure SDLC", "draft", None, None, None],
        ],
        # Same convergence proof, RiskPath side: two rows, same content-
        # hashed id, no MITIGATED_BY/VERIFIED_BY edges needed (classification
        # edges stay empty; `_persist_classification_passthrough`'s edge
        # validation no-ops on an empty `classification_edges` tuple).
        risk_path_rows=[
            [shared_rp_id, "Unpatched Internet-Facing Service", "draft", None, None, None],
            [shared_rp_id, "Unpatched Internet-Facing Service", "draft", None, None, None],
        ],
        covers_rows=[[shared_pa_id, cap_a_id], [shared_pa_id, cap_b_id]],
    )
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)
    # The single-tenant graph starts empty, so the FIRST capability minted
    # this run needs no embedding call at all (an empty working index
    # short-circuits `find_best_semantic_match`) -- but the SECOND
    # capability's own semantic-match scan is scored against the growing
    # same-run working index (which now contains the first mint), so it
    # still needs its own embedding call even though same-run mints are
    # never eligible merge targets. Both names are scripted so
    # neither call falls through to a real, unconfigured LLM provider --
    # and so that any (incorrect) PracticeArea/RiskPath embedding attempt
    # would raise `AssertionError` inside the fake instead of silently
    # succeeding.
    call_embedding = _ScriptedCallEmbedding(
        {
            "Encrypt Data At Rest Capability": [1.0, 0.0],
            "Rotate Encryption Keys Capability": [0.0, 1.0],
        }
    )

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    # No RouteEmbedding-reachable call was ever made with either passthrough
    # kind's own name -- only the two Capability names -- proving neither
    # convergence above was ever routed through any embedding comparison
    # (i.e. `dedupe_canonical_nodes` was never reached for PracticeArea or
    # RiskPath; it is only ever invoked with `kind="Capability"`/`"Policy"`,
    # per its own `Literal` signature).
    assert set(call_embedding.calls) == {
        "Encrypt Data At Rest Capability",
        "Rotate Encryption Keys Capability",
    }
    assert "Secure SDLC" not in call_embedding.calls
    assert "Unpatched Internet-Facing Service" not in call_embedding.calls

    # Exactly one PracticeArea node: both rows target the SAME id (the second row is dropped by
    # the create-only fold). The gateway group carries one create-only primitive per id, the
    # first row's.
    pa_entries = [
        e.identity for g in merge_rig.groups for e in g.entries if e.name == "PracticeArea"
    ]
    assert pa_entries == [shared_pa_id]

    # Same structural-convergence proof, RiskPath side.
    rp_entries = [e.identity for g in merge_rig.groups for e in g.entries if e.name == "RiskPath"]
    assert rp_entries == [shared_rp_id]

    # That one PracticeArea node carries both incoming rows' edges.
    covers_writes = single_tenant.edges_of("COVERS")
    assert {c["source_id"] for c in covers_writes} == {shared_pa_id}
    assert {c["target_id"] for c in covers_writes} == {
        cap_a_id,
        cap_b_id,
    }


def test_missing_classification_edge_endpoint_raises_before_any_classification_edge_write(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """A `COVERS` edge referencing an endpoint
    id absent from BOTH the baseline and the single-tenant graph raises
    `CompanyMergePersistenceError` before any of the four classification
    EDGE types is written -- checked via `calls_matching` on each of
    `[:COVERS]`/`[:OWNS]`/`[:MITIGATED_BY]`/`[:VERIFIED_BY]`, NOT a blanket
    "zero writes" claim, since the PracticeArea NODE write is accepted
    precedent to already have landed by this point (mirrors
    `persist_role_and_requirement_passthrough`'s own "nodes before edges"
    ordering).
    """
    emitter, _log_path = make_emitter()
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    baseline = _baseline_graph_with_classification_nodes(
        practice_area_rows=[[practice_area_id_value, "Secure SDLC", "active", None, None, None]],
        risk_path_rows=[],
        covers_rows=[[practice_area_id_value, "cap_never_persisted_anywhere"]],
    )
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)

    with pytest.raises(CompanyMergePersistenceError, match="cap_never_persisted_anywhere"):
        merge_baseline_graph(
            "REG-1.0",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            merge_threshold=_THRESHOLD,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            emitter=emitter,
            **merge_rig.merge_keywords(),
        )

    for relationship in ("COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY"):
        assert single_tenant.edges_of(relationship) == []
    # Accepted precedent: the PracticeArea node write already landed before
    # the edge-endpoint validation ran.
    assert ("PracticeArea", practice_area_id_value) in merge_rig.policy_system.nodes


def _baseline_graph_with_classification_content() -> _FakeBaselineGraph:
    """Fixture: one NEW Capability (no
    existing match in the single-tenant graph, forcing an embedding call),
    one PracticeArea, one RiskPath, one COVERS edge, one MITIGATED_BY edge
    -- so a false "no classification writes happened" pass can't hide a bug
    where classification writes occur BEFORE the Capability dedup call.
    """
    incoming_capability_id = "cap_incoming_report_incident"
    practice_area_id_value = "pa_secure_sdlc_4a7c1d"
    risk_path_id_value = "rp_secure_build_release_d93f8a"

    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": "REG-BI-010", "title": "Test Regulation"},
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[[incoming_capability_id, "Report Incident Capability", 0.8, None]],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        practice_area_rows=[[practice_area_id_value, "Secure SDLC", "active", None, None, None]],
        risk_path_rows=[[risk_path_id_value, "Secure Build & Release", "active", None, None, None]],
        covers_rows=[[practice_area_id_value, incoming_capability_id]],
        mitigated_by_rows=[[risk_path_id_value, incoming_capability_id]],
    )


def test_no_classification_writes_when_capability_embedding_fails(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """If Capability dedup's embedding
    call raises `LlmProviderError`, `merge_baseline_graph` never reaches
    `_persist_classification_passthrough` -- zero PracticeArea/RiskPath node
    writes and zero COVERS/OWNS/MITIGATED_BY/VERIFIED_BY edge writes have
    occurred by the time the exception propagates.
    """
    emitter, _log_path = make_emitter()
    baseline = _baseline_graph_with_classification_content()
    existing_capability_id = "capability_existing_conduct_risk_assessment"
    existing_capability_name = "Conduct Risk Assessment Capability"
    incoming_capability_name = "Report Incident Capability"
    single_tenant = _FakeSingleTenantGraph(
        gateway_graph=merge_rig.policy_system,
        # A non-empty existing index forces `find_best_semantic_match` to
        # actually attempt an embedding call for the incoming Capability
        # (an empty index would short-circuit with zero calls, per
        # `find_best_semantic_match`'s own docstring).
        capability_rows=[[existing_capability_id, existing_capability_name, [1.0, 0.0]]],
    )
    call_embedding = _ScriptedCallEmbeddingWithFailure(
        {
            incoming_capability_name: openai.APIConnectionError(
                request=httpx.Request("POST", "https://example.invalid")
            )
        }
    )

    with pytest.raises(LlmProviderError):
        merge_baseline_graph(
            "REG-BI-010",
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            merge_threshold=_THRESHOLD,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            call_embedding=call_embedding,
            emitter=emitter,
            **merge_rig.merge_keywords(),
        )

    for marker in ("PracticeArea", "RiskPath"):
        assert not any(label == marker for label, _ in merge_rig.policy_system.nodes)
    for relationship in ("COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY"):
        assert single_tenant.edges_of(relationship) == []
    assert merge_rig.groups == []  # the node writes are gateway groups


def test_succeeded_log_entry_carries_classification_write_counts(
    make_emitter: MakeEmitter, read_lines: ReadLines, merge_rig: MergeRig
) -> None:
    """The `outcome="succeeded"` `merge_baseline_graph` log entry
    carries the six PracticeArea/RiskPath/classification-edge write counts
    via `extra=`, computed from a baseline fixture with a KNOWN, DIFFERENT
    count of each (2 PracticeArea, 1 RiskPath, 3 COVERS, 1 OWNS, 1
    MITIGATED_BY, 2 VERIFIED_BY) -- so a bug that swaps two counts cannot
    pass by coincidence.
    """
    emitter, log_path = make_emitter()
    pa_1, pa_2 = "pa_secure_sdlc_4a7c1d", "pa_secure_build_release_d93f8a"
    rp_1 = "rp_secure_build_release_d93f8a"
    cap_1, cap_2, cap_3 = "cap_one_abc", "cap_two_def", "cap_three_ghi"
    pol_1 = "pol_engineering_practices_xyz"
    ctrl_1, ctrl_2 = "ctrl_one_abc", "ctrl_two_def"

    baseline = _baseline_graph_with_classification_nodes(
        practice_area_rows=[
            [pa_1, "Secure SDLC", "active", None, None, None],
            [pa_2, "Secure Build & Release", "active", None, None, None],
        ],
        risk_path_rows=[[rp_1, "Ransomware Exposure", "active", None, None, None]],
        extra_capability_rows=[
            [cap_1, "Encrypt Data At Rest Capability", 0.8, None],
            [cap_2, "Rotate Encryption Keys Capability", 0.8, None],
            [cap_3, "Patch Management Capability", 0.8, None],
        ],
        policy_rows=[[pol_1, "Engineering Practices Policy", "draft", 0.9]],
        control_rows=[
            [ctrl_1, "manual", "Peer Review Control", "planned", 0.8, None],
            [ctrl_2, "automated", "Static Analysis Control", "planned", 0.8, None],
        ],
        covers_rows=[[pa_1, cap_1], [pa_1, cap_2], [pa_2, cap_3]],
        owns_rows=[[pa_1, pol_1]],
        mitigated_by_rows=[[rp_1, cap_1]],
        verified_by_rows=[[rp_1, ctrl_1], [rp_1, ctrl_2]],
    )
    single_tenant = _FakeSingleTenantGraph(gateway_graph=merge_rig.policy_system)
    # Four Capabilities land in this one baseline (the fixture's own spine
    # Capability plus cap_1/cap_2/cap_3): the single-tenant graph starts
    # empty, so the FIRST one minted needs no embedding call, but every
    # SUBSEQUENT one is scored against the growing same-run working index
    # (a same-run mint is never an eligible merge target, but
    # scoring against it still requires an embedding call for both sides).
    # All four names are scripted so none falls through to a real,
    # unconfigured LLM provider.
    call_embedding = _ScriptedCallEmbedding(
        {
            "Incident Reporting Capability": [1.0, 0.0, 0.0, 0.0],
            "Encrypt Data At Rest Capability": [0.0, 1.0, 0.0, 0.0],
            "Rotate Encryption Keys Capability": [0.0, 0.0, 1.0, 0.0],
            "Patch Management Capability": [0.0, 0.0, 0.0, 1.0],
        }
    )

    merge_baseline_graph(
        "REG-1.0",
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
        governance=_GOVERNANCE,
        **merge_rig.merge_keywords(),
    )
    emitter.flush()

    entries = read_lines(log_path)
    succeeded = [
        e
        for e in entries
        if e.get("action") == "merge_baseline_graph" and e.get("outcome") == "succeeded"
    ]
    assert len(succeeded) == 1
    entry = succeeded[0]
    assert (entry["governance_policies"], entry["governance_controls"]) == (1, 2)
    # `extra=` fields flatten onto the top-level JSON payload (`LogEntry.
    # to_json_line`), not nested under an "extra" key.
    assert entry["practice_area_count"] == 2
    assert entry["risk_path_count"] == 1
    assert entry["covers_count"] == 3
    assert entry["owns_count"] == 1
    assert entry["mitigated_by_count"] == 1
    assert entry["verified_by_count"] == 2


def _classification_only_baseline_graph(
    *,
    regulatory_instrument_id: str,
    capability_rows: list[object],
    practice_area_rows: list[object],
    covers_rows: list[object],
    risk_path_rows: list[object] | None = None,
) -> _FakeBaselineGraph:
    """A minimal fixture carrying only Capability + PracticeArea/COVERS
    (optionally RiskPath) content -- no Role/Requirement/Obligation spine
    needed, mirroring `_internal_baseline_with_governance`'s own
    "governance-layer-only" shape. Used by the convergence tests above, which
    only care about PracticeArea/RiskPath/Capability convergence.
    """
    return _FakeBaselineGraph(
        regulatory_instrument_properties={
            "id": regulatory_instrument_id,
            "title": f"Test Regulation {regulatory_instrument_id}",
        },
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=capability_rows,
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
        practice_area_rows=practice_area_rows,
        risk_path_rows=risk_path_rows or [],
        covers_rows=covers_rows,
    )
