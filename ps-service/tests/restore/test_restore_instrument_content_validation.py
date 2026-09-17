"""Tests for `restore_instrument`'s content-validation ordering (GH #104, AC-BI-006).

A label/relationship-type violation in EITHER leg is refused before any
FalkorDB call of any kind. Proven with a `db` spy that records, then refuses,
every attribute access, and a baseline-ONLY violation (the native leg is
valid) -- the exact case the pre-#104 order (stage native, then validate
baseline inside `stage_graph`) got wrong: `{short}_native__restoring__*` had
already been populated by the time the baseline leg was rejected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn, cast

import pytest

from ps_service.export.models import SerializedGraph, SerializedNode
from ps_service.restore.errors import ArtifactContentRejectedError
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from falkordb import FalkorDB

    from ps_service.restore.models import RestoreArtifact

_INSTRUMENT_ID = "GH104-1.0"
_SHORT_NAME = "GH104"
_ACTOR = "test-actor-104"
_NEVER_TOUCHED_SINGLE_TENANT = "__gh104_never_touched_single_tenant__"


class _DbTouchedError(Exception):
    """Raised by `_SpyDb` on any attribute access -- restore reached FalkorDB."""


class _SpyDb:
    """A `FalkorDB` stand-in that records, then refuses, every attribute access.

    `restore_instrument` reaches `db` only via `stage_graph -> graph_query_handle
    -> db.select_graph` and `raw_connection -> db.connection`; both are plain
    attribute lookups, so `__getattr__` sees every possible touch.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> NoReturn:
        self.touched.append(name)
        raise _DbTouchedError(f"restore touched db.{name} before content validation passed")


def _valid_native_graph() -> SerializedGraph:
    return SerializedGraph(
        nodes=(SerializedNode(label="RegulatoryInstrument", properties={"id": _INSTRUMENT_ID}),),
        edges=(),
    )


def _baseline_graph_with_a_disallowed_label() -> SerializedGraph:
    return SerializedGraph(
        nodes=(
            SerializedNode(label="RegulatoryInstrument", properties={"id": _INSTRUMENT_ID}),
            SerializedNode(label="EvilLabel", properties={"id": "evil-1"}),
        ),
        edges=(),
    )


def _artifact_with_a_baseline_only_violation() -> RestoreArtifact:
    return build_restore_artifact(
        instrument_id=_INSTRUMENT_ID,
        short_name=_SHORT_NAME,
        native_graph=_valid_native_graph(),
        baseline_graph=_baseline_graph_with_a_disallowed_label(),
    )


def test_baseline_only_content_violation_is_rejected_before_any_db_call(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    spy_db = _SpyDb()

    with pytest.raises(ArtifactContentRejectedError, match=r"node label 'EvilLabel'"):
        restore_instrument(
            _artifact_with_a_baseline_only_violation(),
            db=cast("FalkorDB", spy_db),
            single_tenant_graph_name=_NEVER_TOUCHED_SINGLE_TENANT,
            similarity_threshold=0.9,
            actor=_ACTOR,
            emitter=emitter,
        )

    assert spy_db.touched == []


def test_content_violation_emits_started_then_failed_audit_entries(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    with pytest.raises(ArtifactContentRejectedError):
        restore_instrument(
            _artifact_with_a_baseline_only_violation(),
            db=cast("FalkorDB", _SpyDb()),
            single_tenant_graph_name=_NEVER_TOUCHED_SINGLE_TENANT,
            similarity_threshold=0.9,
            actor=_ACTOR,
            emitter=emitter,
        )
    emitter.flush()

    outcomes = [
        row["outcome"] for row in read_lines(log_path) if row["action"] == "restore_instrument"
    ]
    assert outcomes == ["started", "failed"]
