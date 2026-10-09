"""Truth table for `classify_reingestion` (#201 S1b, PLAN 3.4 + CHANGES A4 `finalize`).

Each row builds a `{short}_native` ledger state, runs the REAL `classify_reingestion` and
`succession` over the `LedgerNativeGraph` boundary double, and asserts the state, the
stages still to run and where the prior id came from. Classification is read-only.
"""

from __future__ import annotations

from typing import Literal

import pytest

from change_monitor._fakes import LedgerNativeGraph, MakeEmitter, ReadLines
from ps_service.change_monitor.errors import ChangeMonitorStateError
from ps_service.change_monitor.trigger import classify_reingestion

_PRIOR = "CRA-1.0"
_NEW = "CRA-2.0"
_ALL = ("ingestion", "extraction", "derivation", "merge")
_AFTER_INGEST = _ALL[1:]
_AFTER_EXTRACT = _ALL[2:]
_AFTER_DERIVE = _ALL[3:]


def _graph(
    *,
    node: bool = True,
    edge: Literal["none"] | bool | None = "none",
    marker: str | None = None,
    prior_status: str | None = None,
    prior_type: str = "regulation",
) -> LedgerNativeGraph:
    """Build the ledger state; `edge` is "none" (no edge), or the edge's `absorbed` value."""
    if prior_status is None:
        prior_status = "active" if edge == "none" else "superseded"
    graph = LedgerNativeGraph()
    graph.seed_instrument(_PRIOR, status=prior_status, instrument_type=prior_type)
    if node:
        graph.seed_instrument(_NEW)
    if edge != "none":
        graph.seed_edge(_PRIOR, _NEW, absorbed=edge)
    if marker is not None:
        graph.seed_marker(_NEW, marker)
    return graph


@pytest.mark.parametrize(
    ("graph", "state", "stages"),
    [
        pytest.param(_graph(node=False), "fresh", _ALL, id="fresh-node-absent"),
        pytest.param(_graph(), "resume", _ALL, id="resume-no-marker-reruns-all-four"),
        pytest.param(_graph(marker="ingestion"), "resume", _AFTER_INGEST, id="resume-after-ingest"),
        pytest.param(
            _graph(marker="extraction"), "resume", _AFTER_EXTRACT, id="resume-after-extract"
        ),
        pytest.param(
            _graph(marker="derivation"), "resume", _AFTER_DERIVE, id="resume-after-derive"
        ),
        pytest.param(_graph(marker="merge"), "resume", (), id="resume-link-only"),
        pytest.param(
            _graph(marker="linked"), "resume", (), id="resume-linked-marker-no-edge-links-only"
        ),
        pytest.param(_graph(edge=True), "already_processed", (), id="already-processed"),
        pytest.param(
            _graph(edge=True, marker="linked"), "finalize", (), id="finalize-after-native-link"
        ),
        pytest.param(
            _graph(edge=None, prior_status="superseded"),
            "repair",
            _AFTER_INGEST,
            id="repair-legacy-no-marker",
        ),
        pytest.param(
            _graph(edge=None, prior_status="superseded", marker="extraction"),
            "repair",
            _AFTER_EXTRACT,
            id="repair-after-extract",
        ),
        pytest.param(
            _graph(edge=None, prior_status="superseded", marker="derivation"),
            "repair",
            _AFTER_DERIVE,
            id="repair-after-derive",
        ),
        pytest.param(
            _graph(edge=None, prior_status="superseded", marker="merge"),
            "repair",
            (),
            id="repair-link-only",
        ),
    ],
)
def test_classify_reingestion_truth_table(
    graph: LedgerNativeGraph, state: str, stages: tuple[str, ...], make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    pre = classify_reingestion(graph, _NEW, emitter=emitter)

    assert pre.state == state
    assert pre.stages_to_run == stages
    assert pre.prior_id == _PRIOR
    assert pre.prior_instrument_type == "regulation"
    assert graph.writes == []


def test_classify_reingestion_carries_the_prior_instrument_type_for_the_guard(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()

    pre = classify_reingestion(_graph(prior_type="national_transposition"), _NEW, emitter=emitter)

    assert pre.prior_instrument_type == "national_transposition"


@pytest.mark.parametrize(
    "graph",
    [
        pytest.param(_graph(edge=True, prior_status="active"), id="edge-but-prior-not-superseded"),
        pytest.param(_graph(marker="bogus"), id="unknown-marker-stage"),
    ],
)
def test_classify_reingestion_rejects_an_inconsistent_graph(
    graph: LedgerNativeGraph, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    with pytest.raises(ChangeMonitorStateError):
        classify_reingestion(graph, _NEW, emitter=emitter)


def test_classify_reingestion_with_no_active_prior_raises_for_fresh(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()

    with pytest.raises(ChangeMonitorStateError):
        classify_reingestion(LedgerNativeGraph(), _NEW, emitter=emitter)


def test_classify_reingestion_logs_state_stages_and_prior(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, path = make_emitter()

    classify_reingestion(_graph(marker="extraction"), _NEW, emitter=emitter)
    emitter.flush()

    [entry] = [e for e in read_lines(path) if e["action"] == "classify_reingestion"]
    assert entry["component"] == "change_monitor"
    assert entry["entity_id"] == _NEW
    assert entry["outcome"] == "resume"
    assert entry["stages_to_run"] == list(_AFTER_EXTRACT)
    assert entry["prior_id"] == _PRIOR
