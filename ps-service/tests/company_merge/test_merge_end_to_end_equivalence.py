"""`merge_baseline_graph` end to end against the frozen legacy reference (issue #210, slice 8c).

AC-BI-002, merge half. The per-family slices (2c-8a) compared one writer family at a time; here a
composite baseline carrying EVERY family at once (instrument, Roles, Requirements, DEFINES and
EXPRESSES provenance, Obligations, Capabilities with an exact, a semantic and a new match,
PracticeArea/RiskPath, all edge types, near-miss PendingReviews) is merged through the gateway and
through the frozen writer, with the same generated values and embeddings, and the `policy_system`
digests must be equal (modulo edge identity; live digest and live interpreter agreement are not
verifiable here, the `falkordb_live` tests stay as the live anchor).

The two cases the issue names are the CASE-winner rule's neighbours in the merge stage, where
there is no winner statement (that is the resolve route, slices 10a-10c):
- tie: two existing Capabilities at the SAME similarity to the incoming one. The dedup decision
  logic is read-only and unchanged (#88); what is proved is that the write for whichever one it
  picks is the one the frozen writer made.
- missing `created_at`: an existing canonical node that never had one. Create-only upserts must
  leave it without (a "backfill" would be a behaviour change); a node that has one keeps it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import FamilyABaseline, MergeRig
from company_merge._merge_equivalence import SeededNode, production_run, reference_graph
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.logging import LogEmitter

_ROLE = "role_manufacturer"
_ROLE_B = "role_operator"
_REQ = "req_a"
_OBL = "obl_notify_the_csirt"
_OBL_B = "obl_keep_records"
_CAP_A = "capability_encrypt_data"
_CAP_ALIAS = "capability_encrypt_data_alias"
_CAP_NEW = "capability_patch_vulnerabilities"
_PA = "pa_secure_sdlc_4a7c1d"
_RP = "rp_secure_build_release_d93f8a"
_PA_PROPS: dict[str, str | float] = {"name": "Secure SDLC", "status": "active", "version": "1.0"}
_RP_PROPS: dict[str, str | float] = {"name": "Secure Build", "status": "active", "version": "2.0"}

_COMPOSITE = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act", "celex_id": "32024R2847"},
    roles={_ROLE: "Manufacturer", _ROLE_B: "Operator"},
    requirements={_REQ: "Conduct a risk assessment"},
    defines=((_ROLE, "Art. 3(1)"), (_ROLE_B, "Art. 3(2)")),
    expresses=((_REQ, "Art. 13"),),
    obligations={_OBL: ("Notify the CSIRT.", 0.7), _OBL_B: ("Keep records.", 0.6)},
    capabilities={_CAP_A: ("Encrypt data", 0.8), _CAP_NEW: ("Patch vulnerabilities", 0.9)},
    practice_areas={_PA: _PA_PROPS},
    risk_paths={_RP: _RP_PROPS},
    edges=(
        ("HAS", "Role", _ROLE, "Obligation", _OBL),
        ("HAS", "Role", _ROLE_B, "Obligation", _OBL_B),
        ("SATISFIED_BY", "Requirement", _REQ, "Obligation", _OBL),
        ("REQUIRES", "Obligation", _OBL, "Capability", _CAP_A),
        ("REQUIRES", "Obligation", _OBL_B, "Capability", _CAP_NEW),
        ("COVERS", "PracticeArea", _PA, "Capability", _CAP_A),
        ("MITIGATED_BY", "RiskPath", _RP, "Capability", _CAP_NEW),
    ),
)
# A second regulation: the alias is matched semantically onto the first one's Capability, the
# Role / Obligation / PracticeArea ids recur (create-only keeps the first), one Capability is new.
_SECOND = FamilyABaseline(
    instrument_id="NIS2-1.0",
    instrument_properties={"title": "NIS 2"},
    roles={_ROLE: "Manufacturer (renamed)"},
    obligations={_OBL: ("Notify the CSIRT (drifted).", 0.1), "obl_new": ("Assess risk.", 0.5)},
    capabilities={_CAP_ALIAS: ("Encrypt data", 0.8), "capability_log_events": ("Log events", 0.4)},
    practice_areas={_PA: {"name": "Drifted name", "status": "draft"}},
    edges=(
        ("HAS", "Role", _ROLE, "Obligation", "obl_new"),
        ("REQUIRES", "Obligation", "obl_new", "Capability", _CAP_ALIAS),
        ("REQUIRES", "Obligation", _OBL, "Capability", "capability_log_events"),
    ),
)

_UNIT_VECTOR = [1.0] + [0.0] * 511  # what the one-hot caller returns for the first text it embeds
_INCOMING = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act"},
    obligations={_OBL: ("Notify the CSIRT.", 0.7)},
    capabilities={_CAP_ALIAS: ("Encrypt data", 0.8)},
    edges=(("REQUIRES", "Obligation", _OBL, "Capability", _CAP_ALIAS),),
)
_TIED: tuple[SeededNode, ...] = (
    ("Capability", "capability_tie_a", {"name": "Encrypt", "embedding": _UNIT_VECTOR}),
    ("Capability", "capability_tie_b", {"name": "Cipher", "embedding": _UNIT_VECTOR}),
)
_NO_CREATED_AT: tuple[SeededNode, ...] = (
    ("Capability", _CAP_A, {"name": "Encrypt data", "embedding": _UNIT_VECTOR}),
    (
        "Capability",
        "capability_has_created_at",
        {
            "name": "Other",
            "embedding": [0.0, 1.0] + [0.0] * 510,
            "created_at": "2020-01-01T00:00:00+00:00",
        },
    ),
)

_SCENARIOS: dict[str, tuple[tuple[FamilyABaseline, ...], tuple[SeededNode, ...]]] = {
    "E2E-fresh": ((_COMPOSITE,), ()),
    "E2E-rerun": ((_COMPOSITE, _COMPOSITE), ()),
    "E2E-two-regulations": ((_COMPOSITE, _SECOND), ()),
    "E2E-tie": ((_INCOMING,), _TIED),
    "E2E-missing-created-at": ((_COMPOSITE,), _NO_CREATED_AT),
}


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_merge_baseline_graph_and_the_frozen_reference_build_the_same_policy_system(
    scenario: str, merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """AC-BI-002: the `policy_system` digest equals the frozen reference's, per scenario."""
    steps, seeded = _SCENARIOS[scenario]

    reference = reference_graph(steps, emitter, seeded)
    production_run(steps, merge_rig, emitter, seeded)

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_the_composite_scenario_reaches_every_family(emitter: LogEmitter) -> None:
    """Guard of the guard: nodes of each family, ten-type edges and reviews are in the reference."""
    graph = reference_graph(_SCENARIOS["E2E-two-regulations"][0], emitter)

    labels = {label for label, _ in graph.nodes}
    assert labels >= {
        "RegulatoryInstrument",
        "Role",
        "Requirement",
        "Obligation",
        "Capability",
        "PracticeArea",
        "RiskPath",
        "PendingReview",
    }
    assert {key[0] for key in graph.edges} >= {
        "DEFINES",
        "EXPRESSES",
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
        "COVERS",
        "MITIGATED_BY",
    }
    assert ("Capability", _CAP_ALIAS) not in graph.nodes  # matched onto the first regulation's


def test_the_tie_scenario_merges_the_incoming_capability_onto_one_of_the_tied_nodes(
    emitter: LogEmitter, merge_rig: MergeRig
) -> None:
    """Guard of the guard: the incoming alias really met two equally similar nodes."""
    steps, seeded = _SCENARIOS["E2E-tie"]

    production_run(steps, merge_rig, emitter, seeded)

    capabilities = {i for (label, i) in merge_rig.policy_system.nodes if label == "Capability"}
    assert capabilities == {"capability_tie_a", "capability_tie_b"}  # no node minted for the alias


def test_a_node_without_created_at_stays_without_and_one_with_keeps_it(
    emitter: LogEmitter, merge_rig: MergeRig
) -> None:
    steps, seeded = _SCENARIOS["E2E-missing-created-at"]

    production_run(steps, merge_rig, emitter, seeded)

    nodes = merge_rig.policy_system.nodes
    assert "created_at" not in nodes[("Capability", _CAP_A)]
    assert nodes[("Capability", "capability_has_created_at")]["created_at"] == (
        "2020-01-01T00:00:00+00:00"
    )
