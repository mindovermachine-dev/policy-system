"""The thin vertical path of the internal-seed ingestion through the gateway (#208, slice 1).

A seed holding only the RegulatoryInstrument: it is submitted as one mutation group on
`{short}_native` (and one on `{short}_baseline`), logged under the caller's audit event id,
applied by the gateway, and read back from the graph. Postgres and FalkorDB are the gateway
rig's in-memory fakes. Failure behavior of the submission is pinned here: a rejected, unlogged
or unapplied native group stops the run before the baseline group is submitted.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from graph_gateway._fakes import GatewayRig

from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed, SeedNode
from ps_service.ingestion.adapters.internal_seed.persist import (
    ingest_internal_regulatory_instrument,
)

if TYPE_CHECKING:
    from ps_service.graph_gateway.models import GraphLogEntry

_NATIVE_GRAPH = "testreg_native"
# `ps-graph-digest-v1` of a graph holding only the five-property instrument `_ri_only_seed` builds.
_RI_ONLY_DIGEST = "sha256:ffac18b7da459260b7c8850f5916c2fcd0f536b169d97a783c65b57b6a5272b0"


def _ri_only_seed(properties: dict[str, str | float] | None = None) -> InternalRegulationSeed:
    instrument = SeedNode(
        label="RegulatoryInstrument",
        id="TESTREG-1.0",
        properties=properties
        if properties is not None
        else {
            "title": "Test Regulation",
            "source_type": "internal",
            "effective_date": "2026-01-01",
            "version": "1.0",
            "status": "active",
        },
    )
    return InternalRegulationSeed(nodes=(instrument,), edges=())


class _Run:
    """One ingestion of a seed over a rig."""

    def __init__(self, rig: GatewayRig) -> None:
        self.rig = rig
        self.audit_event_id = str(uuid.uuid4())
        rig.store.audit_rows.add(self.audit_event_id)

    def ingest(self, seed: InternalRegulationSeed) -> None:
        ingest_internal_regulatory_instrument(
            seed,
            submitter=self.rig.gateway,
            native_graph_name=_NATIVE_GRAPH,
            baseline_graph_name="testreg_baseline",
            audit_event_id=self.audit_event_id,
        )


def _logged(rig: GatewayRig) -> tuple[GraphLogEntry, ...]:
    return tuple(rig.store.entries.get(_NATIVE_GRAPH, ()))


def test_regulatory_instrument_is_logged_applied_and_readable_from_the_native_graph(
    rig: GatewayRig,
) -> None:
    run = _Run(rig)

    run.ingest(_ri_only_seed())

    native = rig.graphs.open(_NATIVE_GRAPH)
    assert native.nodes[("RegulatoryInstrument", "TESTREG-1.0")] == {
        "title": "Test Regulation",
        "source_type": "internal",
        "effective_date": "2026-01-01",
        "version": "1.0",
        "status": "active",
    }
    assert len(_logged(rig)) == 1
    group = rig.store.groups[0]
    assert (group.graph, group.audit_event_id) == (_NATIVE_GRAPH, run.audit_event_id)
    assert canonical_digest(native) == _RI_ONLY_DIGEST


def test_read_back_digest_equals_that_of_the_same_node_written_directly(rig: GatewayRig) -> None:
    run = _Run(rig)
    reference = GatewayRig()
    reference_audit_id = str(uuid.uuid4())
    reference.store.audit_rows.add(reference_audit_id)
    reference.gateway.submit_group(
        MutationGroup(
            graph=_NATIVE_GRAPH,
            audit_event_id=reference_audit_id,
            primitives=(
                UpsertNode(
                    label="RegulatoryInstrument",
                    id="TESTREG-1.0",
                    properties={
                        "title": "Test Regulation",
                        "source_type": "internal",
                        "effective_date": "2026-01-01",
                        "version": "1.0",
                        "status": "active",
                    },
                ),
            ),
        )
    )

    run.ingest(_ri_only_seed())

    assert canonical_digest(rig.graphs.open(_NATIVE_GRAPH)) == canonical_digest(
        reference.graphs.open(_NATIVE_GRAPH)
    )


def test_the_baseline_instrument_is_written_through_the_gateway(
    rig: GatewayRig,
) -> None:
    run = _Run(rig)

    run.ingest(_ri_only_seed())

    assert ("RegulatoryInstrument", "TESTREG-1.0") in rig.graphs.open("testreg_baseline").nodes


def test_reingesting_the_same_seed_logs_nothing_more(rig: GatewayRig) -> None:
    run = _Run(rig)
    run.ingest(_ri_only_seed())

    run.ingest(_ri_only_seed())

    assert len(_logged(rig)) == 1


def test_a_reserved_property_key_is_rejected_before_anything_is_submitted(
    rig: GatewayRig,
) -> None:
    run = _Run(rig)

    with pytest.raises(InternalSeedError, match="RegulatoryInstrument"):
        run.ingest(_ri_only_seed({"title": "T", "id": "overwrites-the-node-id"}))

    assert _logged(rig) == ()


def test_an_unreachable_gateway_store_fails_the_ingestion_before_the_baseline_group(
    rig: GatewayRig,
) -> None:
    run = _Run(rig)
    rig.store.fail_appends()

    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        run.ingest(_ri_only_seed())

    assert rig.store.groups == []


def test_a_group_committed_but_not_applied_fails_the_ingestion_before_the_baseline_group(
    rig: GatewayRig,
) -> None:
    run = _Run(rig)
    rig.graphs.open(_NATIVE_GRAPH).fail_on_write(redis.exceptions.ConnectionError("down"))

    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        run.ingest(_ri_only_seed())

    assert len(_logged(rig)) == 1
    assert [group.graph for group in rig.store.groups] == [_NATIVE_GRAPH]  # no baseline group
