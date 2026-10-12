"""Live twin of `test_restore_equivalence.py`: real FalkorDB and Postgres (issue #214, slice 9).

Two proofs, neither run in the implementation sandbox (no FalkorDB, no Postgres):

1. AC-BI-007 and AC-BI-001. Every curated artifact is restored by the REAL `restore_instrument`
   through the real gateway (Postgres graph log, FalkorDB, applied by swap) into scratch graphs.
   Each leg's canonical digest equals the old path's (the frozen reference over the in-memory
   interpreter, with the production `cosine_similarity`), and a replay of each leg from the log
   rebuilds the same digest. `created_at` is stamped by `datetime.now` on the new path, so it is
   set to a constant on the scratch `policy_system` before its digest, as `masked_copy` does for
   the reference.
2. The hold-time budget (CHANGES F-8, C-0 exception 2). CRA is restored last into a
   `policy_system` already holding the other nine curated instruments. The swap logs the seconds
   the legs were held (`swap_set` / `hold_seconds`); the budget is `_HOLD_BUDGET_SECONDS`. The
   measured value is printed (`pytest -s`) and goes into `LIVE_GATE.md`.

Run with `uv run pytest -s -m "falkordb_live and postgres_live" <this file>`.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._seed_merge_graphs import SIMILARITY_THRESHOLD
from graph_gateway.equivalence_harness import run_equivalence

from ps_service.graph_gateway.digest import canonical_digest
from ps_service.ingestion.falkordb_client import select_graph
from ps_service.restore.restore_instrument import restore_instrument
from restore._curated import CURATED_IDS, load_curated
from restore._equivalence_support import (
    curated_artifact,
    harness_artifact,
    owner_of,
    run_old_path,
)
from restore._old_path import populate_old_path
from restore.live_gateway import live_restore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB
    from graph_gateway._fakes import InMemoryGraph
    from persistence.provisioned_postgres import Provisioned

    from ps_service.logging.emitter import LogEmitter
    from restore.live_gateway import LiveRestore

pytestmark = [pytest.mark.falkordb_live, pytest.mark.postgres_live]

_ACTOR = "test-actor"
_HOLD_BUDGET_SECONDS = 60.0
"""The target in CA Restore C-0 exception 2: the seconds a swap may hold the three legs."""
_MASK_CREATED_AT = "MATCH (n) WHERE n.created_at IS NOT NULL SET n.created_at = 'masked'"


def _restore(
    db: FalkorDB,
    live: LiveRestore,
    instrument_id: str,
    *,
    token: str,
    policy_system: str,
    emitter: LogEmitter,
) -> tuple[str, str]:
    """Restore one curated instrument under tokened leg names; return those two names."""
    artifact = curated_artifact(instrument_id)
    manifest = replace(artifact.manifest, short_name=f"{artifact.manifest.short_name}_{token}")
    restore_instrument(
        replace(artifact, manifest=manifest),
        db=db,
        single_tenant_graph_name=policy_system,
        similarity_threshold=SIMILARITY_THRESHOLD,
        actor=_ACTOR,
        open_gateway=live.open_gateway,
        audit_event_id=live.audit_event_id,
        emitter=emitter,
        owner=owner_of(instrument_id),
    )
    short = manifest.short_name.lower()
    return f"{short}_native", f"{short}_baseline"


def _equal(db: FalkorDB, instrument_id: str, leg: str, name: str, reference: InMemoryGraph) -> None:
    result = run_equivalence(
        harness_artifact(instrument_id, leg),
        lambda _artifact: reference,
        lambda _artifact: select_graph(db, name),
    )
    assert result.passed, (name, result)


@pytest.mark.parametrize("instrument_id", CURATED_IDS)
def test_a_live_restore_equals_the_old_path_and_replays_to_the_same_graphs(
    instrument_id: str,
    live_falkordb: FalkorDB,
    provisioned_graph_log: Provisioned,
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    policy_system = f"__gh214_equiv_{token}__"
    curated = load_curated(instrument_id)
    old = run_old_path(
        instrument_id, [(curated.native, curated.baseline)], emitter, owner=owner_of(instrument_id)
    )
    names: tuple[str, ...] = ()
    try:
        with live_restore(provisioned_graph_log, live_falkordb, emitter) as live:
            native_name, baseline_name = _restore(
                live_falkordb,
                live,
                instrument_id,
                token=token,
                policy_system=policy_system,
                emitter=emitter,
            )
            names = (native_name, baseline_name, policy_system)

            _equal(live_falkordb, instrument_id, "native", native_name, old.native)
            _equal(
                live_falkordb,
                instrument_id,
                "baseline",
                baseline_name,
                populate_old_path(curated.baseline),
            )
            live_digests = {
                name: canonical_digest(select_graph(live_falkordb, name)) for name in names
            }
            for name in names:
                live.gateway.replay_graph(name)
            assert {
                name: canonical_digest(select_graph(live_falkordb, name)) for name in names
            } == live_digests

            select_graph(live_falkordb, policy_system).query(_MASK_CREATED_AT)
            _equal(live_falkordb, instrument_id, "baseline", policy_system, old.policy_system)
    finally:
        live_falkordb.connection.delete(*names, policy_system)


def test_restoring_cra_into_a_populated_policy_system_holds_the_graphs_within_budget(
    live_falkordb: FalkorDB,
    provisioned_graph_log: Provisioned,
    make_emitter: MakeEmitter,
    read_lines: Callable[[Path], list[dict[str, object]]],
) -> None:
    """CHANGES F-8: the hold scales with the whole `policy_system`, so CRA goes in last."""
    emitter, log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    policy_system = f"__gh214_hold_{token}__"
    order = (*(i for i in CURATED_IDS if i != "CRA-1.0"), "CRA-1.0")
    names: list[str] = []
    try:
        with live_restore(provisioned_graph_log, live_falkordb, emitter) as live:
            for instrument_id in order:
                names.extend(
                    _restore(
                        live_falkordb,
                        live,
                        instrument_id,
                        token=token,
                        policy_system=policy_system,
                        emitter=emitter,
                    )
                )
            emitter.flush()
    finally:
        live_falkordb.connection.delete(*names, policy_system)

    swaps = [line for line in read_lines(log_path) if line.get("action") == "swap_set"]
    hold = cast("float", swaps[-1]["hold_seconds"])
    print(f"\nCRA swap hold_seconds={hold:.2f} (budget {_HOLD_BUDGET_SECONDS})")  # noqa: T201
    assert swaps[-1]["outcome"] == "success"
    assert hold <= _HOLD_BUDGET_SECONDS
