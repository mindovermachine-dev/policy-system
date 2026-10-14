"""`postgres_live` twin of the swap-set link (migration `0002`).

Against a scratch PS Postgres it proves
that `0002_group_swap_set.sql` applies from an empty database and on top of a database that only
holds `0001`, that the application role can then log a swap set and read it back (every leg
carries the one id the store minted, the pending group and the set are found), and that groups
logged without a set keep a null id.

Deselected by default -- run with `uv run pytest -m postgres_live ps-service/tests/graph_gateway`;
needs `PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import shutil
import uuid
from typing import TYPE_CHECKING

import pytest
from persistence.provisioned_postgres import migrate_state_database, provision_graph_log

from ps_service.graph_gateway import MIGRATIONS_DIR
from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.provision import (
    _connect_when_server_is_ready,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.persistence.migration_runner import MigrationSource
from ps_service.persistence.privileged_migration_runner import apply_privileged_migrations

if TYPE_CHECKING:
    from pathlib import Path

    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live


def _graphs() -> tuple[str, str]:
    stem = uuid.uuid4().hex[:8]
    return (f"a-{stem}", f"b-{stem}")


def _group(graph: str) -> GraphLogGroupDraft:
    return GraphLogGroupDraft(
        graph=graph,
        entries=(GraphLogEntryDraft(name="Capability", identity=f"{graph}-0", content={"n": 0}),),
    )


def _assert_swap_set_round_trip(prov: Provisioned) -> None:
    store = PsycopgGraphLogStore(prov.state_config())
    first, second = _graphs()

    plain = store.append_group_standalone(_group(first))
    swapped = store.append_groups_standalone(
        (_group(first), _group(second)) if first < second else (_group(second), _group(first)),
        as_swap_set=True,
    )

    assert plain.swap_set_id is None
    (set_id,) = {group.swap_set_id for group in swapped}
    assert set_id is not None
    legs = store.read_swap_set(set_id)
    assert [leg.graph for leg in legs] == sorted([first, second])
    pending = store.read_pending_swap_group(first, plain.last_position)
    assert pending is not None
    assert (pending.swap_set_id, pending.first_position) == (set_id, 2)
    assert store.read_pending_swap_group(first, 2) is None
    assert store.read_pending_swap_group(second, 0) is not None


def test_a_database_provisioned_from_empty_logs_and_reads_back_a_swap_set(
    fresh_provisioned: Provisioned,
) -> None:
    migrate_state_database(fresh_provisioned)

    applied = provision_graph_log(fresh_provisioned)

    assert applied.graph_gateway_applied == [
        "0001_graph_mutation_log.sql",
        "0002_group_swap_set.sql",
    ]
    _assert_swap_set_round_trip(fresh_provisioned)


def test_a_database_that_only_holds_the_first_migration_gains_the_column(
    fresh_provisioned: Provisioned, tmp_path: Path
) -> None:
    migrate_state_database(fresh_provisioned)
    first_only = tmp_path / "first_only"
    first_only.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_graph_mutation_log.sql", first_only)
    target = fresh_provisioned.provisioning_target()
    with _connect_when_server_is_ready(target) as admin:
        apply_privileged_migrations(
            admin,
            sources=[MigrationSource("graph_gateway", first_only)],
            owner_role=target.owner_role,
            app_role=target.app_user,
        )

    applied = provision_graph_log(fresh_provisioned)

    assert applied.graph_gateway_applied == ["0002_group_swap_set.sql"]
    _assert_swap_set_round_trip(fresh_provisioned)
