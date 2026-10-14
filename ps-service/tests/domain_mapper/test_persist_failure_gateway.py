"""Persistence failures of the Domain Mapper stages through the gateway.

The real extraction and derivation stages run over the gateway rig (in-memory log and graph
fakes). A group that cannot be logged, that FalkorDB refuses, or that is committed but not
applied reaches the caller as the same sanitized `DomainMapperPersistenceError` the direct path
raised (`FalkorDB write failed: ` plus text carrying no host, port, query or content). A run that
failed after earlier groups committed leaves them intact, and a re-run converges to the digest of
an uninterrupted run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from domain_mapper._pipeline import run_derivation, run_extraction
from ps_service.domain_mapper.errors import DomainMapperPersistenceError
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import (
    GRAPH_LOG_UNAVAILABLE_MESSAGE,
    GraphApplyError,
    GraphLogUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from domain_mapper._fakes import MakeEmitter
    from domain_mapper._gateway_fakes import BaselineRig
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.graph_reader import PersistedElements
    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

_PREFIX = "FalkorDB write failed: "
_NOT_APPLIED = (
    "FalkorDB write failed: the group was committed to the graph log but could not be applied "
    "to the graph yet"
)
_LEAKY_REFUSAL = redis.exceptions.ResponseError(
    "Invalid input near 'MERGE (n:Role {name: \"Manufacturer\"})' at 10.0.0.5:6379 "
    "while writing 'Conduct a cybersecurity risk assessment.'"
)
_LEAKS = ("10.0.0.5", "6379", "MERGE", "Manufacturer", "Conduct", "Invalid input")
_DOWN = redis.exceptions.ConnectionError("Error 111 connecting to 10.0.0.5:6379")
_BASELINE = "cra_baseline"

type Stage = Callable[[BaselineRig, LogEmitter], object]


def _prepare_extraction(rig: BaselineRig, emitter: LogEmitter) -> None:
    del rig, emitter


def _prepare_derivation(rig: BaselineRig, emitter: LogEmitter) -> None:
    """Derivation reads what extraction left: run it healthy first."""
    run_extraction(rig, emitter)


_STAGES = pytest.mark.parametrize(
    ("prepare", "stage"),
    [
        pytest.param(_prepare_extraction, run_extraction, id="extraction"),
        pytest.param(_prepare_derivation, run_derivation, id="derivation"),
    ],
)


@_STAGES
def test_a_group_the_log_cannot_take_raises_the_sanitized_persistence_error(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig, prepare: Stage, stage: Stage
) -> None:
    """The graph log is unreachable: nothing is committed, the error is the fixed gateway text."""
    emitter, _ = make_emitter()
    prepare(baseline_rig, emitter)
    groups_before = len(baseline_rig.groups)
    baseline_rig.store.fail_appends()

    with pytest.raises(DomainMapperPersistenceError) as exc_info:
        stage(baseline_rig, emitter)

    assert str(exc_info.value) == f"{_PREFIX}{GRAPH_LOG_UNAVAILABLE_MESSAGE}"
    assert isinstance(exc_info.value.__cause__, GraphLogUnavailableError)
    assert len(baseline_rig.groups) == groups_before


@_STAGES
def test_a_group_falkordb_refuses_raises_the_sanitized_persistence_error(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig, prepare: Stage, stage: Stage
) -> None:
    """FalkorDB refuses a query retrying cannot fix: no query, host or content leaks."""
    emitter, _ = make_emitter()
    prepare(baseline_rig, emitter)
    baseline_rig.baseline.fail_on_write(_LEAKY_REFUSAL)

    with pytest.raises(DomainMapperPersistenceError) as exc_info:
        stage(baseline_rig, emitter)

    message = str(exc_info.value)
    assert message.startswith(_PREFIX)
    assert [leak for leak in _LEAKS if leak in message] == []
    assert isinstance(exc_info.value.__cause__, GraphApplyError)
    assert exc_info.value.__cause__.__cause__ is _LEAKY_REFUSAL


@_STAGES
def test_a_group_committed_but_not_applied_raises_the_persistence_error(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig, prepare: Stage, stage: Stage
) -> None:
    """FalkorDB stays down: the group is safe in the log, yet the stage must not go on."""
    emitter, _ = make_emitter()
    prepare(baseline_rig, emitter)
    applied_before = dict(baseline_rig.baseline.nodes)
    baseline_rig.baseline.fail_on_write(_DOWN)

    with pytest.raises(DomainMapperPersistenceError) as exc_info:
        stage(baseline_rig, emitter)

    assert str(exc_info.value) == _NOT_APPLIED
    assert "10.0.0.5" not in str(exc_info.value)
    assert baseline_rig.baseline.nodes == applied_before
    assert not baseline_rig.gateway.is_caught_up(_BASELINE)


@pytest.mark.parametrize("variant", ["log_down", "committed_not_applied"])
def test_failure_in_derivation_after_extraction_committed_leaves_the_extraction_group_and_a_rerun_converges_to_the_uninterrupted_digest(  # noqa: E501 -- name fixed by the plan
    make_emitter: MakeEmitter,
    baseline_rig: BaselineRig,
    other_baseline_rig: BaselineRig,
    variant: str,
) -> None:
    """Extraction's content survives; the re-run ends where an uninterrupted run does.

    The reference is a second, fresh rig running extraction and derivation with the same scripted
    LLM answers. After the fault is healed (and, when FalkorDB was the one down, the committed
    group is applied the way the gateway's reconciler does) the re-run of derivation alone
    reproduces the reference digest.
    """
    emitter, _ = make_emitter()
    run_extraction(other_baseline_rig, emitter)
    run_derivation(other_baseline_rig, emitter)
    uninterrupted = canonical_digest(other_baseline_rig.baseline)
    run_extraction(baseline_rig, emitter)
    after_extraction = canonical_digest(baseline_rig.baseline)
    extraction_group = baseline_rig.groups[0]
    if variant == "log_down":
        baseline_rig.store.fail_appends()
    else:
        baseline_rig.baseline.fail_on_write(_DOWN)

    with pytest.raises(DomainMapperPersistenceError, match=_PREFIX):
        run_derivation(baseline_rig, emitter)

    assert canonical_digest(baseline_rig.baseline) == after_extraction != uninterrupted
    assert baseline_rig.groups[0] == extraction_group
    baseline_rig.store.heal()
    baseline_rig.baseline.heal()
    baseline_rig.gateway.catch_up(_BASELINE)

    run_derivation(baseline_rig, emitter)

    assert canonical_digest(baseline_rig.baseline) == uninterrupted


class _OutcomeRecorder:
    """A pass-through `GroupSubmitter` that remembers each group outcome the gateway returned."""

    def __init__(self, inner: GroupSubmitter) -> None:
        self._inner = inner
        self.statuses: list[str] = []

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        outcome = self._inner.submit_group(group)
        self.statuses.append(outcome.status)
        return outcome

    def is_caught_up(self, graph: str) -> bool:
        return self._inner.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        return self._inner.stamp_legacy_edge_identity(graph, audit_event_id)

    def read_persisted_elements(self, graph: str) -> PersistedElements:
        return self._inner.read_persisted_elements(graph)


def test_rerunning_extraction_changes_nothing_and_logs_nothing(
    make_emitter: MakeEmitter, baseline_rig: BaselineRig
) -> None:
    """The second identical group is `unchanged`: no new entry, no new group, same digest."""
    emitter, _ = make_emitter()
    recorder = _OutcomeRecorder(baseline_rig.gateway)
    run_extraction(baseline_rig, emitter, submitter=recorder)
    digest = canonical_digest(baseline_rig.baseline)
    entries = baseline_rig.logged()
    groups = list(baseline_rig.groups)

    run_extraction(baseline_rig, emitter, submitter=recorder)

    assert recorder.statuses == ["applied", "unchanged"]
    assert canonical_digest(baseline_rig.baseline) == digest
    assert baseline_rig.logged() == entries
    assert baseline_rig.groups == groups
