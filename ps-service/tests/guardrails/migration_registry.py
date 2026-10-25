"""The live tests that prove each SQL migration directory applies.

The migration coverage guard (`test_migration_coverage.py`) requires one entry here for every
directory of `NNNN_*.sql` files under `ps-service/src/ps_service`. Adding a migration file makes
the entry's `latest_migration` stale, so the guard fails until the entry is updated and names a
test that upgrades from the previous migration. Node ids are repo-root-relative, so each can be
pasted into `uv run pytest -m postgres_live <node id>`. There is no exemption field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class MigrationCoverage:
    """A migration directory and the live tests that prove it applies.

    Attributes:
        directory: Repo-root-relative directory holding the `NNNN_*.sql` files.
        database: The database the directory's migrations run in.
        latest_migration: The newest file name; a new migration must update the entry.
        empty_apply_test: Pytest node id of a postgres_live test applying the directory to an
            empty database.
        upgrade_test: Pytest node id of a postgres_live test applying the newest migration to
            the schema the previous release left. A single-file directory whose first migration
            applies to an empty schema may repeat `empty_apply_test`; any other directory names a
            test that starts from the earlier state.
    """

    directory: str
    database: Literal["ps_state", "ps_signing"]
    latest_migration: str
    empty_apply_test: str
    upgrade_test: str


_SRC = "ps-service/src/ps_service"
_TESTS = "ps-service/tests"
_PROVISION_EMPTY = (
    f"{_TESTS}/audit/test_integrity_provisioning_live.py"
    "::test_provisioning_an_empty_database_applies_ordinary_then_graph_gateway_then_audit_integrity"
)
_ACCESS_ROLE_SCHEMA = (
    f"{_TESTS}/authz/test_access_role_assignments_schema_live.py"
    "::test_table_has_exactly_the_expected_columns_with_types_and_nullability"
)
_INGESTION_RUNS_APPLY = (
    f"{_TESTS}/ingestion_runs/test_migrations.py"
    "::test_migration_applies_once_and_a_second_run_applies_nothing"
)
_RUNTIME_CONFIG_SCHEMA = (
    f"{_TESTS}/runtime_config/test_runtime_config_schema_live.py"
    "::test_table_has_exactly_the_key_value_and_updated_at_columns"
)

REGISTRY: tuple[MigrationCoverage, ...] = (
    MigrationCoverage(
        directory=f"{_SRC}/audit/migrations",
        database="ps_state",
        latest_migration="0002_audit_events_details_indexes.sql",
        empty_apply_test=_PROVISION_EMPTY,
        upgrade_test=(
            f"{_TESTS}/audit/test_audit_upgrade_live.py"
            "::test_upgrade_applies_only_the_later_migration"
        ),
    ),
    MigrationCoverage(
        directory=f"{_SRC}/audit/privileged_migrations",
        database="ps_state",
        latest_migration="0001_audit_log_insert_only_hash_chain.sql",
        empty_apply_test=_PROVISION_EMPTY,
        upgrade_test=(
            f"{_TESTS}/audit/test_integrity_backfill_live.py"
            "::test_history_is_chained_in_occurred_at_then_id_order_with_the_oracle_hashes"
        ),
    ),
    MigrationCoverage(
        directory=f"{_SRC}/authz/migrations",
        database="ps_state",
        latest_migration="0001_access_role_assignments.sql",
        empty_apply_test=_ACCESS_ROLE_SCHEMA,
        upgrade_test=_ACCESS_ROLE_SCHEMA,
    ),
    MigrationCoverage(
        directory=f"{_SRC}/graph_gateway/migrations",
        database="ps_state",
        latest_migration="0002_group_swap_set.sql",
        empty_apply_test=(
            f"{_TESTS}/graph_gateway/test_swap_set_postgres_live.py"
            "::test_a_database_provisioned_from_empty_logs_and_reads_back_a_swap_set"
        ),
        upgrade_test=(
            f"{_TESTS}/graph_gateway/test_swap_set_postgres_live.py"
            "::test_a_database_that_only_holds_the_first_migration_gains_the_column"
        ),
    ),
    MigrationCoverage(
        directory=f"{_SRC}/ingestion_runs/migrations",
        database="ps_state",
        latest_migration="0001_ingestion_runs.sql",
        empty_apply_test=_INGESTION_RUNS_APPLY,
        upgrade_test=_INGESTION_RUNS_APPLY,
    ),
    MigrationCoverage(
        directory=f"{_SRC}/passkey_signing/migrations",
        database="ps_signing",
        latest_migration="0003_signing_credentials_rp_id.sql",
        empty_apply_test=(
            f"{_TESTS}/passkey_signing/test_migration_runner.py"
            "::test_apply_pending_migrations_creates_the_pending_approvals_table"
        ),
        upgrade_test=(
            f"{_TESTS}/passkey_signing/test_signing_credentials_upgrade_live.py"
            "::test_upgrade_applies_only_the_later_migration"
        ),
    ),
    MigrationCoverage(
        directory=f"{_SRC}/runtime_config/migrations",
        database="ps_state",
        latest_migration="0001_runtime_config.sql",
        empty_apply_test=_RUNTIME_CONFIG_SCHEMA,
        upgrade_test=_RUNTIME_CONFIG_SCHEMA,
    ),
)
