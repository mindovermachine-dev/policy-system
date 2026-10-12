"""Truth table for `classify_reingestion` (#201 S1b, PLAN 3.4 + CHANGES A4 `finalize`).

Each row seeds a `{short}_native` state on the gateway rig, runs the REAL `classify_reingestion` and
`succession` reads over `SuccessionReadGraph`, and asserts the state, the stages still to run
and where the prior id came from. Classification is read-only: the rig log stays empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest

from ps_service.change_monitor.errors import ChangeMonitorStateError
from ps_service.change_monitor.trigger import classify_reingestion

if TYPE_CHECKING:
    from change_monitor._fakes import MakeEmitter, ReadLines
    from change_monitor._gateway_fakes import SuccessionReadGraph, SuccessionRig

_PRIOR = "CRA-1.0"
_NEW = "CRA-2.0"
_ALL = ("ingestion", "extraction", "derivation", "merge")
_AFTER_INGEST = _ALL[1:]
_AFTER_EXTRACT = _ALL[2:]
_AFTER_DERIVE = _ALL[3:]


def _graph(
    rig: SuccessionRig,
    *,
    node: bool = True,
    edge: Literal["none"] | bool | None = "none",
    marker: str | None = None,
    prior_status: str | None = None,
    prior_type: str = "regulation",
) -> SuccessionReadGraph:
    """Seed the rig's native graph and return its read handle.

    `edge` is "none" (no edge), or the edge's `absorbed` value (an edge as the pre-gateway code
    wrote it, without an `identity`).
    """
    if prior_status is None:
        prior_status = "active" if edge == "none" else "superseded"
    rig.seed_instrument(rig.native, _PRIOR, status=prior_status, instrument_type=prior_type)
    if node:
        rig.seed_instrument(rig.native, _NEW)
    if edge != "none":
        rig.seed_edge(rig.native, _PRIOR, _NEW, absorbed=edge)
    if marker is not None:
        rig.seed_marker(rig.native, _NEW, marker)
    return rig.read_native()


@pytest.mark.parametrize(
    ("world", "state", "stages"),
    [
        pytest.param({"node": False}, "fresh", _ALL, id="fresh-node-absent"),
        pytest.param({}, "resume", _ALL, id="resume-no-marker-reruns-all-four"),
        pytest.param({"marker": "ingestion"}, "resume", _AFTER_INGEST, id="resume-after-ingest"),
        pytest.param({"marker": "extraction"}, "resume", _AFTER_EXTRACT, id="resume-after-extract"),
        pytest.param({"marker": "derivation"}, "resume", _AFTER_DERIVE, id="resume-after-derive"),
        pytest.param({"marker": "merge"}, "resume", (), id="resume-link-only"),
        pytest.param(
            {"marker": "linked"}, "resume", (), id="resume-linked-marker-no-edge-links-only"
        ),
        pytest.param({"edge": True}, "already_processed", (), id="already-processed"),
        pytest.param(
            {"edge": True, "marker": "linked"}, "finalize", (), id="finalize-after-native-link"
        ),
        pytest.param(
            {"edge": None, "prior_status": "superseded"},
            "repair",
            _AFTER_INGEST,
            id="repair-legacy-no-marker",
        ),
        pytest.param(
            {"edge": None, "prior_status": "superseded", "marker": "extraction"},
            "repair",
            _AFTER_EXTRACT,
            id="repair-after-extract",
        ),
        pytest.param(
            {"edge": None, "prior_status": "superseded", "marker": "derivation"},
            "repair",
            _AFTER_DERIVE,
            id="repair-after-derive",
        ),
        pytest.param(
            {"edge": None, "prior_status": "superseded", "marker": "merge"},
            "repair",
            (),
            id="repair-link-only",
        ),
    ],
)
def test_classify_reingestion_truth_table(
    world: dict[str, object],
    state: str,
    stages: tuple[str, ...],
    succession_rig: SuccessionRig,
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    graph = _graph(succession_rig, **world)  # pyright: ignore[reportArgumentType]

    pre = classify_reingestion(graph, _NEW, emitter=emitter)

    assert pre.state == state
    assert pre.stages_to_run == stages
    assert pre.prior_id == _PRIOR
    assert pre.prior_instrument_type == "regulation"
    assert succession_rig.rig.store.groups == []
    assert graph.legacy_statements == []


def test_classify_reingestion_carries_the_prior_instrument_type_for_the_guard(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    pre = classify_reingestion(
        _graph(succession_rig, prior_type="national_transposition"), _NEW, emitter=emitter
    )

    assert pre.prior_instrument_type == "national_transposition"


@pytest.mark.parametrize(
    "world",
    [
        pytest.param({"edge": True, "prior_status": "active"}, id="edge-but-prior-not-superseded"),
        pytest.param({"marker": "bogus"}, id="unknown-marker-stage"),
    ],
)
def test_classify_reingestion_rejects_an_inconsistent_graph(
    world: dict[str, object], succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    graph = _graph(succession_rig, **world)  # pyright: ignore[reportArgumentType]

    with pytest.raises(ChangeMonitorStateError):
        classify_reingestion(graph, _NEW, emitter=emitter)


def test_classify_reingestion_with_no_active_prior_raises_for_fresh(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    with pytest.raises(ChangeMonitorStateError):
        classify_reingestion(succession_rig.read_native(), _NEW, emitter=emitter)


def test_classify_reingestion_logs_state_stages_and_prior(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, path = make_emitter()

    classify_reingestion(_graph(succession_rig, marker="extraction"), _NEW, emitter=emitter)
    emitter.flush()

    [entry] = [e for e in read_lines(path) if e["action"] == "classify_reingestion"]
    assert entry["component"] == "change_monitor"
    assert entry["entity_id"] == _NEW
    assert entry["outcome"] == "resume"
    assert entry["stages_to_run"] == list(_AFTER_EXTRACT)
    assert entry["prior_id"] == _PRIOR
