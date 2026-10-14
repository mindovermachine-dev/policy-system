"""Export's artifacts through the gateway equal the legacy export's.

`_golden_export.json` was captured from the legacy path: the real legacy
`export_instrument` driven over the ENGPRAC-1.0 baseline (embeddings stripped from every Capability
and Policy node) and native artifacts, with a deterministic embedder and a stateful FalkorDB fake.
These tests run the migrated path, `run_export` over the gateway rig with the same input and the
same embedder, and compare artifact bytes and checksums. The manifest's `exported_at` is excluded.

The live tier (`-m falkordb_live`, `-m postgres_live`) is the anchor for the real FalkorDB reply
and the Postgres log; what a fast test can prove is that nothing in Export's own path (the vector
handed to the gateway, the log entry, the apply) changes a double.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import struct
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from api._gateway_fakes import ProcessGateway, process_gateway_installed
from litellm.types.utils import Embedding, EmbeddingResponse

from export._gateway_fakes import ArtifactDoc, ExportRig
from ps_service.api.export_orchestration import ExportDependencies, run_export
from ps_service.api.graph_write_access import graph_write_access
from ps_service.api.models import ExportRequest
from ps_service.config import ServiceConfig
from ps_service.export.export_instrument import export_instrument
from ps_service.graph_gateway.entry_codec import decode_entry, encode_primitive
from ps_service.graph_gateway.models import GraphLogEntry, MutationGroup, UpsertNode
from ps_service.graph_gateway.payloads import decode_embedding, embedding_payload

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

_REPO = Path(__file__).resolve().parents[3]
_CURATED = _REPO / "curated-content" / "ENGPRAC-1.0"
_GOLDEN = json.loads((Path(__file__).parent / "_golden_export.json").read_text())
_INSTRUMENT_ID = "ENGPRAC-1.0"


def _golden_embedder(*, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
    """The embedder `_golden_export.json` was generated with (see its `embedder` field)."""
    del timeout
    h = int(hashlib.sha256(inputs[0].encode()).hexdigest()[:8], 16)
    vector = [float(i) / 7.0 + (h % 997) / 1000.0 for i in range(8)]
    return EmbeddingResponse(
        model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
    )


class _FakeDb:
    def __init__(self, graphs: list[str]) -> None:
        self.graphs = graphs

    def list_graphs(self) -> list[str]:
        return self.graphs


def _load(name: str) -> ArtifactDoc:
    doc = json.loads((_CURATED / name).read_text())
    return ArtifactDoc(nodes=doc["nodes"], edges=doc["edges"])


@pytest.fixture
def gateway() -> Iterator[ProcessGateway]:
    with process_gateway_installed() as installed:
        yield installed


def test_run_export_matches_the_pre_migration_golden_export(
    gateway: ProcessGateway, make_emitter: MakeEmitter
) -> None:
    """Baseline.json, native.json and both sha256 equal the legacy export's."""
    rig = ExportRig(gateway.rig, short_name="ENGPRAC")
    rig.seed_artifact(
        rig.baseline, _load("baseline.json"), strip_embeddings_of=("Capability", "Policy")
    )
    rig.seed_artifact(rig.native, _load("native.json"))
    deps = ExportDependencies(
        open_db=lambda _config: cast("FalkorDB", _FakeDb([rig.baseline_name])),
        open_baseline_graph=lambda _db, _short: rig.baseline_reader(),  # pyright: ignore[reportArgumentType]
        open_native_graph=lambda _db, _short: rig.native_reader(),  # pyright: ignore[reportArgumentType]
        export=export_instrument,
        call_embedding=_golden_embedder,
        graph_write_access=graph_write_access,
    )

    response = run_export(
        ExportRequest.model_validate({"instrument_id": _INSTRUMENT_ID}),
        config=ServiceConfig(
            host="127.0.0.1",
            port=8000,
            graceful_shutdown_seconds=10,
            logging_dir=None,
            llm_interface_embed_model="fake/embed",
        ),
        actor="127.0.0.1",
        dependencies=deps,
        emitter=make_emitter()[0],
    )

    baseline_bytes = base64.b64decode(response.baseline_blob_base64)
    native_bytes = base64.b64decode(response.native_blob_base64)
    assert response.manifest.baseline_sha256 == _GOLDEN["baseline_sha256"]
    assert response.manifest.native_sha256 == _GOLDEN["native_sha256"]
    assert hashlib.sha256(baseline_bytes).hexdigest() == _GOLDEN["baseline_bytes_sha256"]
    assert hashlib.sha256(native_bytes).hexdigest() == _GOLDEN["native_bytes_sha256"]
    embedded = [key for key, props in rig.baseline.nodes.items() if "embedding" in props]
    assert len(embedded) == _GOLDEN["embedded_node_count"]
    assert len(gateway.rig.store.groups) == 1  # the one baseline group; native is never written


def _doubles(count: int, seed: int) -> list[float]:
    """Seeded doubles over the whole range, subnormals and extremes included."""
    rng = random.Random(seed)  # noqa: S311 -- seeded test data, not a secret
    values = [5e-324, -5e-324, 2.2250738585072014e-308, 1.7976931348623157e308, -0.0, 0.1, 1 / 3]
    while len(values) < count:
        raw = rng.getrandbits(64)
        value = struct.unpack("<d", struct.pack("<Q", raw))[0]
        if value == value and abs(value) != float("inf"):  # noqa: PLR0124 -- NaN check
            values.append(value)
    return values


def _bits(values: tuple[float, ...] | list[float]) -> list[bytes]:
    return [struct.pack("<d", v) for v in values]


def test_log_payload_float_roundtrip() -> None:
    """R-3: 3072 doubles survive primitive -> log entry -> payload -> primitive, bit for bit."""
    vector = _doubles(3072, seed=213)
    primitive = UpsertNode(label="Capability", id="cap_1", embedding=tuple(vector))
    group = MutationGroup(
        graph="cra_baseline",
        audit_event_id="3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10",
        primitives=(primitive,),
    )
    draft = encode_primitive(group.primitives[0])
    assert draft.embedding is not None

    stored = decode_embedding(embedding_payload(draft.embedding).body)
    entry = GraphLogEntry(
        graph="cra_baseline",
        position=1,
        group_id=uuid.uuid4(),
        name=draft.name,
        identity=draft.identity,
        content=json.loads(json.dumps(draft.content)),
        embedding=stored,
    )
    decoded = decode_entry(entry)

    assert isinstance(decoded, UpsertNode)
    assert decoded.embedding is not None
    assert _bits(decoded.embedding) == _bits(vector)
    # and the JSON an artifact carries them in reads back identically
    assert _bits(json.loads(json.dumps(list(decoded.embedding)))) == _bits(vector)
