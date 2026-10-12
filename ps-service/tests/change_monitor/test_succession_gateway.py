"""The re-ingest succession is written only as Graph Write Gateway groups (issue #213).

Each test enters at `trigger_reingestion` with a runner double that raises `_Stop` after the stage
under test, over the process gateway rig. The assertions are on the rig graph and the rig's log:
which groups were logged, on which graph, in which order, anchored to which audit row. The
`SuccessionReadGraph` handle serves the classification reads and accepts, without applying them,
only the frozen legacy statements named in `allowed_legacy`: the shapes not migrated yet.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from change_monitor._fakes import FakeAdapter, ReadLines
from change_monitor._gateway_fakes import RecordingSubmitter
from change_monitor._legacy_reference import FROZEN_STATEMENTS
from ps_service import dependency_health
from ps_service.change_monitor.errors import ChangeMonitorStateError, SuccessionPersistenceError
from ps_service.change_monitor.graph_submission import (
    SuccessionTarget,
    edge_identity,
    ensure_caught_up,
)
from ps_service.change_monitor.models import PipelineRunResult, StageSummary
from ps_service.change_monitor.succession import (
    clear_marker,
    link_and_supersede,
    mark_stage_complete,
)
from ps_service.change_monitor.trigger import trigger_reingestion
from ps_service.graph_cleanup.graph_submission import edge_identity as cleanup_edge_identity
from ps_service.graph_gateway.models import GroupOutcome
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from change_monitor._fakes import MakeEmitter
    from change_monitor._gateway_fakes import SuccessionRig
    from ps_service.change_monitor.models import ReingestionOutcome
    from ps_service.graph_gateway.models import MutationGroup
    from ps_service.ingestion.graph_submission import GroupSubmitter

_IDENTIFIER = "32024R2847"
_PRIOR = "CRA-1.0"
_NEW = "CRA-2.0"
_ALL = ("ingestion", "extraction", "derivation", "merge")
_NOT_MIGRATED: set[str] = set()
_AFTER_LINK: set[str] = set()


class _Stop(Exception):  # noqa: N818 -- a test sentinel, not an error condition
    """Raised by the runner double after the stage under test."""


class _StopAfterRunner:
    """A `PipelineRunner` double: ingestion creates the new node, then it stops after a stage."""

    def __init__(
        self, rig: SuccessionRig, *, stop_after: str | None, creates_node: bool = True
    ) -> None:
        self._rig = rig
        self._stop_after = stop_after
        self._creates_node = creates_node
        self.stages_requested: list[tuple[str, ...]] = []

    def __call__(
        self, *, stages: tuple[str, ...], run_id: str, on_stage_complete: Callable[[str], None]
    ) -> PipelineRunResult:
        del run_id
        self.stages_requested.append(stages)
        done: list[StageSummary] = []
        for stage in stages:
            if stage == "ingestion" and self._creates_node:
                self._rig.seed_instrument(self._rig.native, _NEW)  # as Ingestion registers it
            done.append(StageSummary(stage, {}))
            on_stage_complete(stage)
            if stage == self._stop_after:
                raise _Stop
        return PipelineRunResult(stages=tuple(done))


def _structure() -> FetchedRegulatoryInstrumentStructure:
    metadata = RegulatoryInstrumentMetadata(
        title="Fixture",
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external",
        instrument_type="regulation",
    )
    return FetchedRegulatoryInstrumentStructure(metadata=metadata, nodes=(), edges=())


def _fresh_rig(rig: SuccessionRig) -> SuccessionRig:
    """The state before a re-ingest: the prior is active in both graphs."""
    rig.seed_instrument(rig.native, _PRIOR)
    rig.seed_instrument(rig.single_tenant, _PRIOR)
    rig.seed_instrument(rig.single_tenant, _NEW)
    return rig


def _trigger(
    rig: SuccessionRig,
    runner: _StopAfterRunner | None,
    *,
    allowed_legacy: set[str],
    audit_event_id: str,
    emitter: object,
    submitter: GroupSubmitter | None = None,
) -> ReingestionOutcome:
    return trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=FakeAdapter({_IDENTIFIER: _structure()}),
        graph=rig.read_native(allowed_legacy=allowed_legacy),
        submitter=submitter or rig.rig.gateway,
        audit_anchor=lambda: audit_event_id,
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        run_pipeline=runner,
    )


# --- shape 1: set_new_version_property (AC-BI-002, AC-BI-010) ---------------------------------


def test_the_version_is_one_native_group_anchored_to_the_audit_row(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _fresh_rig(succession_rig)
    emitter, _ = make_emitter()
    audit_event_id = rig.new_audit_event_id()

    with pytest.raises(_Stop):
        _trigger(
            rig,
            _StopAfterRunner(rig, stop_after="ingestion"),
            allowed_legacy=_NOT_MIGRATED,
            audit_event_id=audit_event_id,
            emitter=emitter,
        )

    assert rig.version_of(rig.native, _NEW) == "2.0"
    assert rig.ops("cra_native")[0] == [("merge_property", "RegulatoryInstrument", _NEW)]
    assert rig.audit_links("cra_native")[0] == audit_event_id


def test_a_missing_new_node_is_a_state_error_and_nothing_is_written(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _fresh_rig(succession_rig)
    emitter, _ = make_emitter()

    with pytest.raises(ChangeMonitorStateError):
        _trigger(
            rig,
            _StopAfterRunner(rig, stop_after="ingestion", creates_node=False),
            allowed_legacy=_NOT_MIGRATED,
            audit_event_id=rig.new_audit_event_id(),
            emitter=emitter,
        )

    assert rig.groups("cra_native") == []
    assert ("RegulatoryInstrument", _NEW) not in rig.native.nodes


def test_a_graph_failure_on_the_version_write_is_a_persistence_error_and_marks_falkordb_down(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _fresh_rig(succession_rig)
    emitter, _ = make_emitter()
    rig.native.fail_on_write(redis.exceptions.ConnectionError("Error 111 connecting to FalkorDB"))

    with pytest.raises(SuccessionPersistenceError, match="FalkorDB write failed"):
        _trigger(
            rig,
            _StopAfterRunner(rig, stop_after="ingestion"),
            allowed_legacy=_NOT_MIGRATED,
            audit_event_id=rig.new_audit_event_id(),
            emitter=emitter,
        )

    assert dependency_health.is_healthy(dependency_health.FALKORDB) is False
    assert rig.version_of(rig.native, _NEW) == "1.0"


def test_a_successful_group_marks_falkordb_healthy_again(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _fresh_rig(succession_rig)
    emitter, _ = make_emitter()
    dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=RuntimeError("earlier"))

    with pytest.raises(_Stop):
        _trigger(
            rig,
            _StopAfterRunner(rig, stop_after="ingestion"),
            allowed_legacy=_NOT_MIGRATED,
            audit_event_id=rig.new_audit_event_id(),
            emitter=emitter,
        )

    assert dependency_health.is_healthy(dependency_health.FALKORDB) is True


def test_the_submitted_group_is_logged_as_a_semantic_entry(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    rig = _fresh_rig(succession_rig)
    emitter, log_path = make_emitter()

    with pytest.raises(_Stop):
        _trigger(
            rig,
            _StopAfterRunner(rig, stop_after="ingestion"),
            allowed_legacy=_NOT_MIGRATED,
            audit_event_id=rig.new_audit_event_id(),
            emitter=emitter,
        )

    emitter.flush()
    entries = [
        line for line in read_lines(log_path) if line.get("action") == "submit_succession_group"
    ]
    assert [(e["component"], e["entity_id"], e["outcome"], e["step"]) for e in entries] == [
        ("change_monitor", _NEW, "applied", "version"),
        ("change_monitor", _NEW, "applied", "stage_marker"),
    ]
    assert (entries[0]["graph"], entries[0]["primitives"]) == ("cra_native", 1)


# --- shape 2: mark_stage_complete, one group per stage (AC-BI-002, AC-BI-009) ------------------


@pytest.mark.parametrize("stop_after", _ALL)
def test_each_completed_stage_is_one_marker_group_on_the_native_graph(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, stop_after: str
) -> None:
    rig = _fresh_rig(succession_rig)
    emitter, _ = make_emitter()
    audit_event_id = rig.new_audit_event_id()

    with pytest.raises(_Stop):
        _trigger(
            rig,
            _StopAfterRunner(rig, stop_after=stop_after),
            allowed_legacy=_NOT_MIGRATED,
            audit_event_id=audit_event_id,
            emitter=emitter,
        )

    done = _ALL[: _ALL.index(stop_after) + 1]
    marker_groups = [
        group for group in rig.groups("cra_native") if group.entries[0].name == "ReingestProgress"
    ]
    assert [group.entries[0].content["properties"] for group in marker_groups] == [
        {"stage": stage} for stage in done
    ]
    assert [len(group.entries) for group in marker_groups] == [1] * len(done)
    assert set(rig.audit_links("cra_native")) == {audit_event_id}
    assert rig.marker_stage(rig.native, _NEW) == stop_after


def test_marking_the_same_stage_again_logs_nothing(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _fresh_rig(succession_rig)
    rig.seed_instrument(rig.native, _NEW)
    emitter, _ = make_emitter()
    target = SuccessionTarget(
        rig.rig.gateway, "cra_native", "policy_system", rig.new_audit_event_id, emitter
    )

    mark_stage_complete(target, _NEW, "extraction")
    mark_stage_complete(target, _NEW, "extraction")

    assert len(rig.groups("cra_native")) == 1
    assert rig.marker_stage(rig.native, _NEW) == "extraction"


def test_a_pending_marker_group_is_a_persistence_error_and_the_next_run_resumes_from_it(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _fresh_rig(succession_rig)
    emitter, _ = make_emitter()
    rig.native.fail_after_n_writes(1, redis.exceptions.ConnectionError("FalkorDB went away"))

    with pytest.raises(SuccessionPersistenceError, match="could not be applied"):
        _trigger(
            rig,
            _StopAfterRunner(rig, stop_after="ingestion"),
            allowed_legacy=_NOT_MIGRATED,
            audit_event_id=rig.new_audit_event_id(),
            emitter=emitter,
        )
    assert rig.marker_stage(rig.native, _NEW) is None  # committed to the log, not applied yet
    rig.native.heal()
    rig.rig.gateway.catch_up("cra_native")
    assert rig.marker_stage(rig.native, _NEW) == "ingestion"
    resumed = _StopAfterRunner(rig, stop_after="extraction", creates_node=False)

    with pytest.raises(_Stop):
        _trigger(
            rig,
            resumed,
            allowed_legacy=_NOT_MIGRATED,
            audit_event_id=rig.new_audit_event_id(),
            emitter=emitter,
        )

    assert resumed.stages_requested == [("extraction", "derivation", "merge")]


# --- shape 3: link_and_supersede, one native group (AC-BI-002, AC-BI-009, AC-BI-010) ----------


def _linkable_rig(rig: SuccessionRig, *, legacy_edge: bool = False) -> SuccessionRig:
    """Every stage done and marked: only the succession is outstanding (a link-only call)."""
    _fresh_rig(rig)
    rig.seed_instrument(rig.native, _NEW)
    rig.seed_marker(rig.native, _NEW, "merge")
    if legacy_edge:  # an earlier ingestion-only link: edge without `absorbed` or identity
        rig.seed_edge(rig.native, _PRIOR, _NEW, absorbed=None)
        rig.seed_instrument(rig.native, _PRIOR, status="superseded")
    return rig


def _link(rig: SuccessionRig, emitter: object, submitter: GroupSubmitter) -> tuple[str, object]:
    audit_event_id = rig.new_audit_event_id()
    outcome = _trigger(
        rig,
        None,
        allowed_legacy=_AFTER_LINK,
        audit_event_id=audit_event_id,
        emitter=emitter,
        submitter=submitter,
    )
    return audit_event_id, outcome


def test_the_link_is_one_group_that_applies_edge_absorbed_status_and_marker_together(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _linkable_rig(succession_rig)
    emitter, _ = make_emitter()
    submitter = RecordingSubmitter(rig.rig.gateway)

    audit_event_id, _ = _link(rig, emitter, submitter)

    assert submitter.ops("cra_native") == [
        ["stamp_edge_identity", "upsert_edge", "merge_property", "upsert_node"],
        ["delete_node"],
    ]
    assert rig.edge_properties(rig.native, _PRIOR, _NEW) == [
        {"identity": f"SUPERSEDED_BY:{_PRIOR}->{_NEW}", "absorbed": True}
    ]
    assert rig.status_of(rig.native, _PRIOR) == "superseded"
    # the stamp has nothing to stamp on a graph without a legacy edge, so it is not logged
    assert rig.ops("cra_native")[0] == [
        ("upsert_edge", "SUPERSEDED_BY", f"SUPERSEDED_BY:{_PRIOR}->{_NEW}"),
        ("merge_property", "RegulatoryInstrument", _PRIOR),
        ("upsert_node", "ReingestProgress", _NEW),
    ]
    assert rig.groups("cra_native")[0].entries[2].content["properties"] == {"stage": "linked"}
    assert rig.audit_links("cra_native")[0] == audit_event_id


def test_a_legacy_identityless_edge_is_stamped_in_the_same_group_not_duplicated(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _linkable_rig(succession_rig, legacy_edge=True)
    emitter, _ = make_emitter()

    _link(rig, emitter, rig.rig.gateway)

    assert rig.edge_properties(rig.native, _PRIOR, _NEW) == [
        {"identity": f"SUPERSEDED_BY:{_PRIOR}->{_NEW}", "absorbed": True}
    ]
    link_group = rig.ops("cra_native")[0]
    assert [op for op, _, _ in link_group][:2] == ["stamp_edge_identity", "upsert_edge"]


def test_linking_again_is_unchanged_and_logs_nothing(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _linkable_rig(succession_rig)
    emitter, _ = make_emitter()
    target = SuccessionTarget(
        rig.rig.gateway, "cra_native", "policy_system", rig.new_audit_event_id, emitter
    )
    link_and_supersede(target, _PRIOR, _NEW)
    logged = len(rig.groups("cra_native"))

    link_and_supersede(target, _PRIOR, _NEW)

    assert len(rig.groups("cra_native")) == logged == 1


@pytest.mark.parametrize(("prior", "new"), [("CRA-0.9", _NEW), (_PRIOR, "CRA-9.9")])
def test_linking_a_missing_node_is_a_state_error_and_nothing_is_written(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, prior: str, new: str
) -> None:
    rig = _linkable_rig(succession_rig)
    emitter, _ = make_emitter()
    target = SuccessionTarget(
        rig.rig.gateway, "cra_native", "policy_system", rig.new_audit_event_id, emitter
    )

    with pytest.raises(ChangeMonitorStateError):
        link_and_supersede(target, prior, new)

    assert rig.groups("cra_native") == []
    assert rig.status_of(rig.native, _PRIOR) == "active"
    assert rig.marker_stage(rig.native, _NEW) == "merge"


def test_the_local_edge_identity_formula_equals_graph_cleanups() -> None:
    assert edge_identity("SUPERSEDED_BY", "a", "b") == cleanup_edge_identity(
        "SUPERSEDED_BY", "a", "b"
    )


# --- shape 4: supersede_in_single_tenant, one policy_system group (AC-BI-002, AC-BI-006) -------


def test_the_policy_system_supersede_is_one_group_with_edge_and_status(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _linkable_rig(succession_rig)
    emitter, _ = make_emitter()
    submitter = RecordingSubmitter(rig.rig.gateway)

    audit_event_id, _ = _link(rig, emitter, submitter)

    assert submitter.ops("policy_system") == [
        ["stamp_edge_identity", "upsert_edge", "merge_property"]
    ]
    assert rig.edge_properties(rig.single_tenant, _PRIOR, _NEW) == [
        {"identity": f"SUPERSEDED_BY:{_PRIOR}->{_NEW}"}
    ]
    assert rig.status_of(rig.single_tenant, _PRIOR) == "superseded"
    assert rig.ops("policy_system") == [
        [
            ("upsert_edge", "SUPERSEDED_BY", f"SUPERSEDED_BY:{_PRIOR}->{_NEW}"),
            ("merge_property", "RegulatoryInstrument", _PRIOR),
        ]
    ]
    assert rig.audit_links("policy_system") == [audit_event_id]


def test_a_legacy_policy_system_edge_is_stamped_not_duplicated(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _linkable_rig(succession_rig)
    rig.seed_edge(rig.single_tenant, _PRIOR, _NEW, absorbed=None)  # written by the pre-gateway code
    emitter, _ = make_emitter()

    _link(rig, emitter, rig.rig.gateway)

    assert rig.edge_properties(rig.single_tenant, _PRIOR, _NEW) == [
        {"identity": f"SUPERSEDED_BY:{_PRIOR}->{_NEW}"}
    ]


@pytest.mark.parametrize("missing", [_PRIOR, _NEW])
def test_a_node_missing_from_policy_system_is_a_state_error_and_the_link_marker_stays(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, missing: str
) -> None:
    rig = _linkable_rig(succession_rig)
    del rig.single_tenant.nodes[("RegulatoryInstrument", missing)]
    emitter, _ = make_emitter()

    with pytest.raises(ChangeMonitorStateError, match="policy_system has no"):
        _link(rig, emitter, rig.rig.gateway)

    assert rig.groups("policy_system") == []
    assert rig.marker_stage(rig.native, _NEW) == "linked"  # the next sweep finalizes
    assert rig.status_of(rig.native, _PRIOR) == "superseded"


def test_a_stop_after_the_link_group_is_completed_by_the_next_run(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _linkable_rig(succession_rig)
    emitter, _ = make_emitter()
    rig.single_tenant.fail_on_write(redis.exceptions.ConnectionError("FalkorDB went away"))

    with pytest.raises(SuccessionPersistenceError):
        _link(rig, emitter, rig.rig.gateway)
    assert rig.marker_stage(rig.native, _NEW) == "linked"
    rig.single_tenant.heal()
    rig.rig.gateway.catch_up("policy_system")
    native_groups = len(rig.groups("cra_native"))

    outcome = _trigger(
        rig,
        None,
        allowed_legacy=_AFTER_LINK,
        audit_event_id=rig.new_audit_event_id(),
        emitter=emitter,
    )

    assert outcome.state == "finalize"
    assert rig.ops("cra_native")[native_groups:] == [  # no native link group again
        [("delete_node", "ReingestProgress", _NEW)]
    ]
    assert rig.status_of(rig.single_tenant, _PRIOR) == "superseded"


# --- shape 5: clear_marker (AC-BI-002) and the end of the direct writers (AC-BI-001) ----------


def test_a_completed_succession_ends_with_the_marker_delete_as_its_last_native_group(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _linkable_rig(succession_rig)
    emitter, _ = make_emitter()

    audit_event_id, outcome = _link(rig, emitter, rig.rig.gateway)

    assert outcome.outcome == "superseded"  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
    assert rig.marker_stage(rig.native, _NEW) is None
    assert rig.ops("cra_native")[-1] == [("delete_node", "ReingestProgress", _NEW)]
    assert rig.audit_links("cra_native")[-1] == audit_event_id
    assert len(rig.groups("cra_native")) == 2  # the link, then the marker delete


def test_clearing_a_marker_that_is_not_there_logs_nothing(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _fresh_rig(succession_rig)
    emitter, _ = make_emitter()
    target = SuccessionTarget(
        rig.rig.gateway, "cra_native", "policy_system", rig.new_audit_event_id, emitter
    )

    clear_marker(target, _NEW)

    assert rig.groups("cra_native") == []


@pytest.mark.parametrize("name", sorted(FROZEN_STATEMENTS))
def test_the_read_handle_refuses_every_frozen_legacy_write(
    succession_rig: SuccessionRig, name: str
) -> None:
    handle = succession_rig.read_native()

    with pytest.raises(AssertionError, match="must go through the gateway"):
        handle.query(
            FROZEN_STATEMENTS[name],
            {"new_id": _NEW, "prior_id": _PRIOR, "new_version": "2.0", "stage": "merge"},
        )


class _BehindSubmitter:
    """A submitter that answers `unchanged` while its graph is behind its log (only the check)."""

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        return GroupOutcome(
            graph=group.graph, first_position=None, last_position=None, status="unchanged"
        )

    def is_caught_up(self, graph: str) -> bool:
        del graph
        return False

    def stamp_legacy_edge_identity(self, graph: str, audit_event_id: str | None) -> object:
        raise NotImplementedError

    def read_persisted_elements(self, graph: str) -> object:
        raise NotImplementedError


def test_a_group_that_changes_nothing_still_fails_while_the_graph_is_behind_its_log(
    make_emitter: MakeEmitter,
) -> None:
    """After every group the graph must hold what the log holds: an unchanged group is no proof."""
    emitter, _ = make_emitter()
    target = SuccessionTarget(
        _BehindSubmitter(),  # pyright: ignore[reportArgumentType]
        "cra_native",
        "policy_system",
        lambda: str(uuid.uuid4()),
        emitter,
    )

    with pytest.raises(SuccessionPersistenceError, match="could not be applied"):
        clear_marker(target, _NEW)


def test_ensure_caught_up_applies_a_pending_group_and_needs_a_catch_up_capable_gateway(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = _fresh_rig(succession_rig)
    rig.seed_instrument(rig.native, _NEW)
    emitter, _ = make_emitter()
    target = SuccessionTarget(
        rig.rig.gateway, "cra_native", "policy_system", rig.new_audit_event_id, emitter
    )
    rig.native.fail_on_write(redis.exceptions.ConnectionError("FalkorDB went away"))
    with pytest.raises(SuccessionPersistenceError):
        mark_stage_complete(target, _NEW, "extraction")
    rig.native.heal()

    ensure_caught_up(rig.rig.gateway, "cra_native", emitter=emitter)

    assert rig.marker_stage(rig.native, _NEW) == "extraction"
    with pytest.raises(TypeError, match="cannot catch a graph up"):
        ensure_caught_up(object(), "cra_native")  # pyright: ignore[reportArgumentType]
