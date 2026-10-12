"""D-1 spike (issue #214, stage 1): the offline merge as primitives, test-only, on fakes.

Two questions, answered on real curated artifacts (ENGPRAC-1.0 internal; WEBACCESSIBILITY-1.0
then NIS2-1.0 external, merged in that order into one `policy_system`):

1. Read classification. The pre-#214 merge (`restore_instrument._run_baseline_merge`, the real
   function, run against the interim snapshot writer's in-memory interpreter) is instrumented:
   every read and every endpoint match is recorded with whether it touched something the SAME
   run had written. Only node existence by id may.
2. Overlay sufficiency. The policy_system effect is rebuilt in memory from the live graph read
   at one log position (an overlay: ids at that position plus ids of the primitives built so
   far) and submitted as ONE group with `ExpectedPosition`. Its canonical digest must equal the
   old path's, modulo edge identity (the #210 convention) and `created_at` (the old writer
   stamps `datetime.now`, which is not injectable; the stamp is checked to be an ISO instant
   and then masked on both sides).

What this proves: on these artifacts, with in-memory fakes of the log and graph, the overlay
reproduces every own-write read and the result equals the old path's. What it does not prove:
agreement with a real FalkorDB interpreter or Postgres (none is reachable here), behaviour for
`merged` tombstones or `MergedObligation` markers (absent from the curated artifacts), or the
production builders (the helpers are test-local; slice 7 re-proves with production code).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from company_merge._gateway_fakes import MergeRig

from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import StaleGraphStateError
from restore._spike_builders import (
    EXPECTED_OWN_WRITE_KINDS,
    KIND_INDEX,
    KIND_MARKERS,
    ReferenceStore,
    artifact_baseline,
    masked_copy,
    run_new_path,
    run_reference,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter

_OWNER = ("sub-owner-1", "https://idp.example")
_INTERNAL = ("ENGPRAC-1.0",)
_EXTERNAL = ("WEBACCESSIBILITY-1.0", "NIS2-1.0")
_RERESTORE = ("WEBACCESSIBILITY-1.0", "WEBACCESSIBILITY-1.0")


@pytest.fixture
def merge_rig() -> Iterator[MergeRig]:
    rig = MergeRig()
    yield rig
    rig.close()


def _both_paths(
    instruments: tuple[str, ...], rig: MergeRig, make_emitter: MakeEmitter
) -> tuple[ReferenceStore, list[int]]:
    emitter, _log_path = make_emitter()
    reference = ReferenceStore()
    group_sizes: list[int] = []
    for instrument in instruments:
        baseline = artifact_baseline(instrument)
        governance = GovernanceImport(owner=_OWNER, policy_embeddings=baseline.policy_embeddings)
        run_reference(reference, baseline, emitter, governance)
        group_sizes.append(run_new_path(rig, baseline, emitter, governance))
    return reference, group_sizes


@pytest.mark.parametrize(
    "instruments",
    [_INTERNAL, _EXTERNAL, _RERESTORE],
    ids=["internal", "external", "re-restore"],
)
def test_overlay_digest_equals_the_old_path(
    instruments: tuple[str, ...], merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    reference, group_sizes = _both_paths(instruments, merge_rig, make_emitter)

    assert all(size > 0 for size in group_sizes)
    expected = masked_copy(reference.as_gateway_graph())
    actual = masked_copy(merge_rig.policy_system)
    assert actual.nodes.keys() == expected.nodes.keys()
    assert actual.edges.keys() == expected.edges.keys()
    assert canonical_digest(actual) == canonical_digest(expected)


@pytest.mark.parametrize("instruments", [_INTERNAL, _EXTERNAL], ids=["internal", "external"])
def test_each_restore_is_one_group_on_policy_system(
    instruments: tuple[str, ...], merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    _both_paths(instruments, merge_rig, make_emitter)

    assert len(merge_rig.groups) == len(instruments)
    assert {group.audit_event_id for group in merge_rig.groups} == {merge_rig.audit_event_id}


@pytest.mark.parametrize("instruments", [_INTERNAL, _EXTERNAL], ids=["internal", "external"])
def test_only_node_existence_is_read_back_from_the_runs_own_writes(
    instruments: tuple[str, ...], merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    reference, _sizes = _both_paths(instruments, merge_rig, make_emitter)

    own_write_kinds = {read.kind for read in reference.reads if read.own_write}
    assert own_write_kinds <= EXPECTED_OWN_WRITE_KINDS, own_write_kinds
    assert own_write_kinds  # the spike really exercised the overlay, not an empty case
    pre_existing_kinds = {read.kind for read in reference.reads if not read.own_write}
    assert {KIND_INDEX, KIND_MARKERS} <= pre_existing_kinds


def test_a_stale_position_refuses_the_whole_leg_and_logs_nothing(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    baseline = artifact_baseline(_EXTERNAL[0])
    governance = GovernanceImport(owner=_OWNER, policy_embeddings=baseline.policy_embeddings)

    with pytest.raises(StaleGraphStateError):
        run_new_path(merge_rig, baseline, emitter, governance, position_override=7)

    assert merge_rig.groups == []
    assert merge_rig.policy_system.nodes == {}
