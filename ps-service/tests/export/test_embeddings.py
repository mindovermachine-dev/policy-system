"""Tests for `ps_service.export.embeddings.backfill_capability_embeddings`
written through the Graph Write Gateway.

The baseline graph is the gateway rig's in-memory FalkorDB read through `ExportReadGraph`; the
backfill's one group is submitted to the rig's gateway, so a test asserts on what the rig's graph
and log hold, never on a Cypher string. Gateway-level behaviour at `run_export` (audit anchoring,
failure mapping, re-run) is in `test_export_gateway.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import GatewayRig
from litellm.types.utils import Embedding, EmbeddingResponse

from export._gateway_fakes import ExportRig
from ps_service.export.embeddings import backfill_capability_embeddings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter


class _FakeEmbeddingCaller:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        self.calls.append(inputs[0])
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=self._vector, index=0, object="embedding")]
        )


@pytest.fixture
def rig() -> Iterator[ExportRig]:
    """An empty `cra_baseline` graph on the gateway rig."""
    gateway_rig = GatewayRig()
    yield ExportRig(gateway_rig, short_name="CRA")
    gateway_rig.close()


def _backfill(
    rig: ExportRig,
    caller: _FakeEmbeddingCaller,
    emitter: object,
    source_type: str = "external",
) -> int:
    return backfill_capability_embeddings(
        rig.baseline_reader(),  # pyright: ignore[reportArgumentType]
        source_type=source_type,  # pyright: ignore[reportArgumentType]
        model="fake-embed-model",
        call_embedding=caller,
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        **rig.write_target(),  # pyright: ignore[reportArgumentType]
    )


def test_backfill_writes_one_embedding_per_capability_node_missing_one(
    make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    rig.seed_node(rig.baseline, "Capability", "cap_1", {"name": "Data Encryption"})
    rig.seed_node(rig.baseline, "Capability", "cap_2", {"name": "Access Control"})
    caller = _FakeEmbeddingCaller([0.1, 0.2, 0.3])

    written = _backfill(rig, caller, emitter)

    assert written == 2
    assert sorted(caller.calls) == ["Access Control", "Data Encryption"]
    assert len(rig.baseline_groups()) == 1
    assert rig.embedding_of("Capability", "cap_1") == [0.1, 0.2, 0.3]
    assert rig.embedding_of("Capability", "cap_2") == [0.1, 0.2, 0.3]


def test_backfill_skips_capability_nodes_that_already_have_an_embedding(
    make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    rig.seed_node(
        rig.baseline, "Capability", "cap_1", {"name": "Data Encryption", "embedding": [0.9]}
    )
    rig.seed_node(rig.baseline, "Capability", "cap_2", {"name": "Access Control"})
    caller = _FakeEmbeddingCaller([0.4, 0.5])

    written = _backfill(rig, caller, emitter)

    assert written == 1
    assert caller.calls == ["Access Control"]
    assert rig.embedding_of("Capability", "cap_1") == [0.9]  # untouched
    assert rig.embedding_of("Capability", "cap_2") == [0.4, 0.5]
    assert [e.identity for g in rig.baseline_groups() for e in g.entries] == ["cap_2"]


def test_backfill_external_source_never_reads_or_writes_policy_nodes(
    make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    rig.seed_node(rig.baseline, "Capability", "cap_1", {"name": "Data Encryption"})
    rig.seed_node(rig.baseline, "Policy", "pol_1", {"title": "Data Protection Policy"})
    caller = _FakeEmbeddingCaller([0.1])

    written = _backfill(rig, caller, emitter)

    assert written == 1
    assert caller.calls == ["Data Encryption"]
    assert rig.embedding_of("Policy", "pol_1") is None
    assert not any("n:Policy" in q for q in rig.baseline_reader().statements)


def test_backfill_internal_source_also_covers_policy_nodes(
    make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    rig.seed_node(rig.baseline, "Capability", "cap_1", {"name": "Data Encryption"})
    rig.seed_node(rig.baseline, "Policy", "pol_1", {"title": "Data Protection Policy"})
    caller = _FakeEmbeddingCaller([0.7, 0.8])

    written = _backfill(rig, caller, emitter, source_type="internal")

    assert written == 2
    assert sorted(caller.calls) == ["Data Encryption", "Data Protection Policy"]
    assert rig.embedding_of("Policy", "pol_1") == [0.7, 0.8]
    assert len(rig.baseline_groups()) == 1  # one group for both labels


def test_backfill_with_no_missing_embeddings_writes_nothing(
    make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    caller = _FakeEmbeddingCaller([0.1])

    written = _backfill(rig, caller, emitter)

    assert written == 0
    assert caller.calls == []
    assert rig.rig.store.groups == []
