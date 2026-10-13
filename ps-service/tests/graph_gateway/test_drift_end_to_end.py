"""A real writer, a head checkpoint, an out-of-band write, then the alert (issue #215, S5d).

The whole chain of AC-BI-009 and AC-BI-005 with nothing stubbed between them: the internal-seed
ingestion writer submits its groups through the gateway (and so requests its checkpoints), the
drift check finds the graph as the gateway left it, a write that bypasses the gateway makes it
differ, and the next check raises the alert naming graph, position and both digests. A later
gateway write moves the head past the checkpoint, so the check then reports `no_baseline`.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from ingestion.adapters.internal_seed._seeds import (
    CAPABILITY,
    OBLIGATION,
    RI,
    RI_REF,
    ROLE,
    Run,
    edge,
    node,
)

from graph_gateway._fakes import GatewayRig, close_all_rigs
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
from ps_service.logging import EmitterConfig, LogEmitter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _rigs_closed() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    close_all_rigs()


def _seed() -> InternalRegulationSeed:
    return InternalRegulationSeed(
        nodes=(
            RI,
            node("Role", "role-a", name="Role A"),
            node("Obligation", "obl-a", text="Do A"),
            node("Capability", "cap-1", name="Cap One"),
        ),
        edges=(
            edge("DEFINES", RI_REF, ROLE, source_ref="Art. 1"),
            edge("HAS", ROLE, OBLIGATION),
            edge("REQUIRES", OBLIGATION, CAPABILITY),
        ),
    )


def test_ingest_then_out_of_band_write_then_alert_then_no_baseline(tmp_path: Path) -> None:
    log_path, stderr = tmp_path / "e2e.jsonl", StringIO()
    emitter = LogEmitter(
        EmitterConfig(log_path=log_path, fallback=stderr, mirror_failures_to_stderr=True)
    )
    rig = GatewayRig(emitter)
    run = Run(rig)
    run.ingest(_seed())
    graph = run.native_name

    def drift_entries() -> list[dict[str, object]]:
        emitter.flush()
        lines = log_path.read_text(encoding="utf-8").splitlines()
        return [e for e in (json.loads(line) for line in lines) if e["action"] == "drift_check"]

    # 1. the writer left a checkpoint at the head and the graph still matches it
    head = rig.store.last_position(graph)
    checkpoint = rig.store.read_digest_checkpoint(graph, head)
    assert checkpoint is not None
    assert rig.gateway.check_drift(graph).status == "ok"

    # 2. a write that never went through the gateway: the next check raises the alert
    rig.graphs.open(graph).seed_node(("Capability",), "sneaked-in", {"name": "x"})
    result = rig.gateway.check_drift(graph)
    assert result.status == "drift"
    [ok, alert] = drift_entries()
    assert ok["outcome"] == "ok"
    assert alert["outcome"] == "failed"
    assert alert["error_class"] == "GraphDriftDetected"
    assert (alert["graph"], alert["last_position"]) == (graph, head)
    assert alert["checkpoint_digest"] == checkpoint.canonical_digest
    assert alert["live_digest"] == canonical_digest(rig.graphs.open(graph))
    assert alert["live_digest"] != alert["checkpoint_digest"]
    assert json.loads(stderr.getvalue().strip())["error_class"] == "GraphDriftDetected"

    # 3. one more gateway write moves the head past the checkpoint: nothing to compare with
    rig.gateway.submit_group(
        MutationGroup(
            graph=graph,
            audit_event_id=run.audit_event_id,
            primitives=(UpsertNode(label="Capability", id="cap-2", properties={"name": "Two"}),),
        )
    )
    assert rig.gateway.check_drift(graph).status == "no_baseline"
    last = drift_entries()[-1]
    assert (last["outcome"], last["graph"], last["last_position"]) == (
        "no_baseline",
        graph,
        head + 1,
    )
