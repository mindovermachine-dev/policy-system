"""The restore legs as ONE group, and the second-restore cases into a populated `policy_system`.

The per-artifact digest equality of every leg is `test_restore_equivalence.py` (slice 9, through the
real `restore_instrument`); what stays here submits the builders' group directly.

AC-BI-007 and CHANGES.md F-4: the D-1 spike showed overlay sufficiency with test-local builders;
this re-proves it with the production builders (`restore.leg_primitives`,
`restore.merge_primitives`). The old path is the real code: `populate_graph` for the native and
baseline legs (staged graphs), and `restore_instrument._run_baseline_merge` over the interim
snapshot interpreter for `policy_system`. The new path submits ONE group of three legs to the
gateway rig, `policy_system` under `ExpectedPosition` and the other two replacing. Digests are
compared modulo edge identity (the old path stamps nothing; #210 convention) and `created_at`
(the old writer stamps `datetime.now`, which cannot be injected).

What this does not show: agreement with a real FalkorDB or Postgres (none is reachable from the
build host); the live twins are written for the user's environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from company_merge._gateway_fakes import POLICY_SYSTEM, MergeRig, stepped_values

from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import ExpectedPosition, GraphLeg, MultiGraphGroup
from ps_service.restore.leg_primitives import embeddings_by_label, leg_primitives
from ps_service.restore.merge_primitives import build_policy_system_leg
from restore._curated import CURATED_IDS, INTERNAL_IDS, load_curated
from restore._old_path import populate_old_path
from restore._spike_builders import (
    ArtifactBaseline,
    ReferenceStore,
    masked_copy,
    run_reference,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter

_OWNER = ("sub-owner-1", "https://idp.example")
_THRESHOLD = 0.85


@pytest.fixture
def merge_rig() -> Iterator[MergeRig]:
    rig = MergeRig()
    yield rig
    rig.close()


def _submit_new_path(rig: MergeRig, instrument_id: str, make_emitter: MakeEmitter) -> None:
    artifact = load_curated(instrument_id)
    emitter, _log_path = make_emitter()
    position = rig.gateway.read_log_position(POLICY_SYSTEM)
    leg = build_policy_system_leg(
        artifact.baseline,
        instrument_id=instrument_id,
        live=rig.legacy,
        governance=GovernanceImport(
            owner=_OWNER, policy_embeddings=embeddings_by_label(artifact.baseline, "Policy")
        ),
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
        values=stepped_values(),
    )
    rig.gateway.submit_multi_graph_group(
        MultiGraphGroup(
            audit_event_id=rig.audit_event_id,
            legs=(
                GraphLeg(
                    graph=f"{artifact.short_name}_native",
                    primitives=leg_primitives(artifact.native),
                    replace=True,
                ),
                GraphLeg(
                    graph=f"{artifact.short_name}_baseline",
                    primitives=leg_primitives(artifact.baseline),
                    replace=True,
                ),
                GraphLeg(
                    graph=POLICY_SYSTEM,
                    primitives=leg.primitives,
                    preconditions=(ExpectedPosition(position=position),),
                ),
            ),
        )
    )


@pytest.mark.parametrize("instrument_id", ["ENGPRAC-1.0", "WEBACCESSIBILITY-1.0"])
def test_a_restore_is_one_group_of_three_legs_under_one_event(
    instrument_id: str, merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    _submit_new_path(merge_rig, instrument_id, make_emitter)

    short = load_curated(instrument_id).short_name
    assert {g.graph for g in merge_rig.store.groups} == {
        f"{short}_native",
        f"{short}_baseline",
        POLICY_SYSTEM,
    }
    assert {g.audit_event_id for g in merge_rig.store.groups} == {merge_rig.audit_event_id}


def test_only_the_internal_instrument_carries_a_governance_tree() -> None:
    carrying = {
        instrument_id
        for instrument_id in CURATED_IDS
        if any(n.label == "Policy" for n in load_curated(instrument_id).baseline.nodes)
    }

    assert carrying == INTERNAL_IDS


@pytest.mark.parametrize(
    "instruments",
    [("WEBACCESSIBILITY-1.0", "WEBACCESSIBILITY-1.0"), ("WEBACCESSIBILITY-1.0", "NIS2-1.0")],
    ids=["re-restore", "second-instrument"],
)
def test_a_second_restore_into_a_populated_policy_system_equals_the_old_path(
    instruments: tuple[str, ...], merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    store = ReferenceStore()
    for instrument_id in instruments:
        artifact = load_curated(instrument_id)
        staged = populate_old_path(artifact.baseline)
        emitter, _log_path = make_emitter()
        baseline = ArtifactBaseline(
            instrument_id=instrument_id,
            graph=staged,
            capability_embeddings=embeddings_by_label(artifact.baseline, "Capability"),
            policy_embeddings={},
        )
        run_reference(store, baseline, emitter, GovernanceImport(owner=_OWNER))
        _submit_new_path(merge_rig, instrument_id, make_emitter)

    expected = masked_copy(store.as_gateway_graph())
    assert canonical_digest(masked_copy(merge_rig.policy_system)) == canonical_digest(expected)
