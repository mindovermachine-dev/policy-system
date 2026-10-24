"""Content lint: the operator and architecture docs describe the graph log provisioning.

The immutable `graph_log` tables are created by a privileged, admin-credential step (the
`ps-state-provision` Job, or the same CLI by hand), never by ps-service itself. These tests pin
the facts an operator or reviewer needs: the upgrade step for existing deployments (the Postgres
init script runs only on an empty `PGDATA`), the failure message and its remedy, the restore
consequence, the residuals, the new values and env vars, and the architecture registration.
Anchors are headings and quoted text, never line numbers.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_OPS = _REPO / "docs" / "artifacts" / "operations-guide.md"
_INSTALL = _REPO / "docs" / "artifacts" / "installation-guide.md"
_EVAL_INSTALL = _REPO / "docs" / "artifacts" / "evaluator-installation-guide.md"
_VALREF = _REPO / "docs" / "artifacts" / "helm-chart-values-reference.md"
_CA = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_SA = _REPO / "docs" / "architecture" / "ps-solution-architecture.md"
_PROVISION_COMMAND = "python -m ps_service.graph_gateway.provision"
_MISSING_MIGRATION = "graph_gateway/0001_graph_mutation_log.sql"
_BACKUP_COMMAND = (
    "kubectl exec deploy/policy-system-ps-postgres -- "
    "pg_basebackup -U postgres_admin -D - -Ft -X fetch -c fast"
)
_AUTHENTIK_BACKUP_LINK = "(#authentik-backup)"
_AUTHENTIK_RESTORE_LINK = "(#authentik-restore)"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of the Markdown section whose heading line is `heading`."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    level = len(heading) - len(heading.lstrip("#"))
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(rf"^#{{1,{level}}} ", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_operations_guide_documents_the_graph_log_upgrade_step() -> None:
    section = _section(_read(_OPS), "### Upgrading to the graph mutation log")

    for needle in (
        "ps-state-provision",
        "--wait-for-jobs",
        "ps_state_graph_owner",
        "graph_log",
        "PGDATA",
        "kubectl get job",
        "kubectl logs job/",
        _PROVISION_COMMAND,
        "PS_STATE_ADMIN_POSTGRES_PASSWORD",
        "psPostgres.provisioning.enabled",
    ):
        assert needle in section, needle
    # An existing cluster is covered by the Job, not by the init script.
    assert "only on an empty" in section
    # The upgrade fails (not hangs) when provisioning fails.
    assert "fails" in section


def test_operations_guide_says_the_job_also_applies_the_swap_set_migration() -> None:
    section = _section(_read(_OPS), "### Upgrading to the graph mutation log")

    assert "0002_group_swap_set.sql" in section
    assert "swap_set_id" in section
    assert "image" in section.split("0002_group_swap_set.sql", 1)[1]  # rollback leaves the column


def test_architecture_registration_lists_the_swap_set_migration_and_the_swap_modules() -> None:
    rows = _registration_rows()
    base = "ps-service/src/ps_service/graph_gateway/"

    migration = rows[f"{base}migrations/0002_group_swap_set.sql"]
    assert "swap_set_id" in migration
    assert "GraphKeyPort" in rows[f"{base}swap_port.py"]
    assert "staged" in rows[f"{base}swap.py"]


def test_operations_guide_names_the_missing_migration_remedy_command() -> None:
    section = _section(_read(_OPS), "### Upgrading to the graph mutation log")

    assert _MISSING_MIGRATION in section
    assert _PROVISION_COMMAND in section
    assert "not_ready" in section


def test_operations_guide_restore_carries_the_roles_and_states_the_residuals() -> None:
    ops = _read(_OPS)
    restore = _section(ops, "## Restore")

    assert "admin" in restore
    assert "ps_state_graph_owner" in restore
    # Residual: ps_state owns the database, so "cannot DROP" must not be over-claimed.
    upgrade = _section(ops, "### Upgrading to the graph mutation log")
    assert "DROP DATABASE" in upgrade
    assert "graph_log" in _section(ops, "### PS Postgres (`ps_state` and `ps_signing`)")


def test_installation_guide_points_at_the_operations_upgrade_subsection() -> None:
    section = _section(_read(_INSTALL), "## Upgrading an existing install")

    assert "operations-guide.md#upgrading-to-the-graph-mutation-log" in section


def test_values_reference_documents_graph_owner_role_and_provisioning_values() -> None:
    text = _read(_VALREF)

    for needle in (
        "psPostgres.state.graphOwnerRole",
        "psPostgres.provisioning.enabled",
        "psPostgres.provisioning.backoffLimit",
        "psPostgres.provisioning.connectTimeoutSeconds",
        "psPostgres.provisioning.activeDeadlineSeconds",
        "PS_STATE_ADMIN_POSTGRES_USER",
        "PS_STATE_ADMIN_POSTGRES_PASSWORD",
        "PS_STATE_GRAPH_OWNER_ROLE",
    ):
        assert needle in text, needle


def _registered_paths(ca: str) -> list[str]:
    gateway = _section(ca, "### Graph Write Gateway")
    registration = _section(gateway, "#### Implementation Registration")
    return re.findall(r"^\| `([^`]+)` \|", registration, flags=re.MULTILINE)


def test_architecture_registration_lists_graph_gateway_store_files() -> None:
    paths = _registered_paths(_read(_CA))

    assert paths, "the Graph Write Gateway registration table lists no files"
    for path in paths:
        assert (_REPO / path).exists(), path
    registered = {Path(path).name for path in paths}
    assert {"__init__.py", "store.py", "models.py", "errors.py", "payloads.py", "provision.py"} <= (
        registered
    )
    assert "migrations" in {Path(path).name for path in paths} | {
        Path(path).parent.name for path in paths
    }


def test_ca_persistence_actions_list_privileged_migration_actions() -> None:
    persistence = _section(_read(_CA), "### Persistence")

    assert "| ApplyPrivilegedMigrations |" in persistence
    assert "| VerifyPrivilegedMigrations |" in persistence
    assert "ps_state_graph_owner" in persistence or "owner role" in persistence


def test_ca_container_diagram_lists_startup_verification_edge() -> None:
    ca = _read(_CA)

    assert re.search(
        r'ProcessHarness -->\|"verify_privileged_migrations \(graph_gateway, read-only\)"\|'
        r" Persistence",
        ca,
    )


def test_ca_states_the_command_link_is_optional_at_the_store_and_the_gateway() -> None:
    """The gateway does not require the link either."""
    gateway = _section(_read(_CA), "### Graph Write Gateway")

    assert "optional at both the store and the gateway" in gateway
    assert "required at the gateway" not in gateway


@pytest.mark.parametrize("needle", ["owner role", "admin-credential"])
def test_solution_architecture_persistence_row_mentions_privileged_provisioning(
    needle: str,
) -> None:
    sa = _read(_SA)

    assert needle in sa


_RETIRED_PHRASES = (
    "may start before PS Service has created",
    "audit tables PS Service's own startup migrations create",
    "a missing prerequisite table exits non-zero",
    "`audit_events` exists (the log links to it)",
)


def test_no_doc_or_source_mentions_the_audit_events_missing_exit() -> None:
    searched = [
        _OPS,
        _INSTALL,
        _VALREF,
        _CA,
        _SA,
        _REPO / "CONTRIBUTING.md",
        _REPO / "charts" / "policy-system" / "values.yaml",
        _REPO / "charts" / "policy-system" / "templates" / "ps-state-provision-job.yaml",
        _REPO / "ps-service" / "src" / "ps_service" / "graph_gateway" / "provision.py",
    ]

    for path in searched:
        text = _read(path)
        for phrase in _RETIRED_PHRASES:
            assert phrase not in text, f"{path.name}: {phrase}"


def test_operations_guide_says_the_cli_applies_ordinary_migrations_first() -> None:
    section = _section(_read(_OPS), "### Upgrading to the graph mutation log")

    assert "ordinary" in section
    assert "SET ROLE" in section
    # Who creates the public tables, so the ps_state ownership is not a surprise.
    assert "executing as `ps_state`" in section
    # The Job succeeds from an empty database in one pass; no retry for a missing table.
    assert "empty database" in section


def test_operations_guide_documents_the_lock_timeout_failure_and_remedy() -> None:
    section = _section(_read(_OPS), "### Upgrading to the graph mutation log")

    assert "migration lock" in section
    assert "60 s" in section
    assert "PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS" in section
    assert "stuck" in section


def test_operations_guide_state_table_names_the_provisioning_job_as_creator() -> None:
    section = _section(_read(_OPS), "### PS Postgres (`ps_state` and `ps_signing`)")

    assert "provisioning Job" in section
    assert "executing as `ps_state`" in section


def test_values_reference_documents_connect_timeout_backoff_and_the_render_guard() -> None:
    text = _read(_VALREF)

    assert "psPostgres.provisioning.connectTimeoutSeconds" in text
    assert "`3` / `120` / `900`" in text
    assert "(backoffLimit + 1) * connectTimeoutSeconds" in text
    assert "PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS" in text


def test_ca_apply_privileged_migrations_precondition_has_no_audit_events_wait() -> None:
    persistence = _section(_read(_CA), "### Persistence")
    row = next(
        line
        for line in persistence.splitlines()
        if line.startswith("| ApplyPrivilegedMigrations |")
    )

    assert "audit_events" not in row.split("|")[5]
    assert "ordinary" in row
    assert "SET ROLE" in row
    assert "lock" in row


def test_ca_apply_pending_migrations_is_no_longer_startup_only() -> None:
    persistence = _section(_read(_CA), "### Persistence")
    row = next(
        line for line in persistence.splitlines() if line.startswith("| ApplyPendingMigrations |")
    )
    cells = row.split("|")

    assert "startup-only" not in cells[4]
    assert "run_as_role" in row
    assert "lock_timeout_seconds" in row
    assert "advisory lock" in row


def test_ca_fail_closed_invariant_names_the_two_fixed_sanitized_errors() -> None:
    text = _read(_CA)

    assert "the caller receives the existing sanitized error" not in text
    invariant = next(line for line in text.splitlines() if line.startswith("- **Fail-closed:**"))
    for needle in ("GraphLogUnavailableError", "GraphUnavailableError", "fixed sanitized error"):
        assert needle in invariant, needle


# --- the gateway proper (submit, apply, catch-up, recovery) --------------------------

_GATEWAY_PACKAGE = _REPO / "ps-service" / "src" / "ps_service" / "graph_gateway"
_MECHANISM_WORDS = re.compile(
    r"\b(sql|tables?|triggers?|locks?|locked|index(?:es)?|batch(?:es)?|cypher|unwind|advisory"
    r"|psycopg|threads?)\b",
    flags=re.IGNORECASE,
)


def _gateway_section(heading: str) -> str:
    return _section(_section(_read(_CA), "### Graph Write Gateway"), heading)


def _registration_rows() -> dict[str, str]:
    registration = _gateway_section("#### Implementation Registration")
    return {
        match.group(1): match.group(0)
        for match in re.finditer(r"^\| `([^`]+)` \|.*$", registration, flags=re.MULTILINE)
    }


def test_architecture_registration_covers_every_graph_gateway_module() -> None:
    registered = set(_registration_rows())
    modules = {
        f"ps-service/src/ps_service/graph_gateway/{path.name}"
        for path in _GATEWAY_PACKAGE.glob("*.py")
    }

    assert modules <= registered, sorted(modules - registered)
    assert "ps-service/src/ps_service/main.py" in registered


def test_architecture_registration_names_exactly_the_error_types_the_package_defines() -> None:
    row = _registration_rows()["ps-service/src/ps_service/graph_gateway/errors.py"]
    named = set(re.findall(r"`(\w+Error)`", row))
    defined = set(
        re.findall(
            r"^class (\w+Error)\(", _read(_GATEWAY_PACKAGE / "errors.py"), flags=re.MULTILINE
        )
    )

    assert named == defined, (sorted(named - defined), sorted(defined - named))
    assert "GraphLogInvalidGroupError" not in _read(_CA)


def test_architecture_registration_lists_the_gateway_entry_points() -> None:
    registration = _gateway_section("#### Implementation Registration")

    for entry_point in (
        "submit_group",
        "submit_group_in_transaction",
        "catch_up",
        "is_caught_up",
        "recover",
        "build_default_graph_write_gateway",
        "_start_graph_replay_at_startup",
    ):
        assert f"`{entry_point}" in registration, entry_point


def test_architecture_actions_table_lists_every_gateway_action() -> None:
    actions = _gateway_section("#### Actions")

    for action in ("SubmitGroup", "SubmitGroupInTransaction", "CaughtUp", "IsCaughtUp", "Recover"):
        assert f"| {action} |" in actions, action


def test_architecture_gateway_intro_says_what_is_built_and_what_is_not() -> None:
    gateway = _section(_read(_CA), "### Graph Write Gateway")
    intro = gateway.split("#### Domain Concepts")[0]
    assert "no writer" not in intro.lower()
    assert "ingestion is its first writer" in intro.lower()
    assert "the contracts below describe the intended behavior" not in intro


def test_architecture_states_the_outcome_of_a_submitted_group() -> None:
    outcomes = _gateway_section("#### Outcomes").lower()

    for needle in (
        "applied",
        "unchanged",
        "committed, apply pending",
        "rejected",
        "graph unavailable",
        "apply refused",
        "blocked",
        "first and last position",
    ):
        assert needle in outcomes, needle
    for error in ("GraphWriteRejectedError", "GraphUnavailableError", "GraphApplyBlockedError"):
        assert error.lower() in outcomes, error


def test_architecture_exceptions_state_the_refused_entry_and_restart_recovery() -> None:
    exceptions = _gateway_section("#### C-0 exceptions")

    assert "Apply refused" in exceptions
    assert "blocked" in exceptions
    assert "CaughtUp" in exceptions
    assert "restart" in exceptions
    assert "fails closed" in exceptions or "fail closed" in exceptions
    assert "the caller receives the sanitized error." not in exceptions


def test_architecture_invariants_state_serialisation_and_the_gate_on_unapplied_entries() -> None:
    invariants = _gateway_section("#### Invariants")

    for name in ("Serialised per graph", "Nothing accepted while behind", "Caller-supplied values"):
        assert f"**{name}:**" in invariants, name


def test_architecture_contract_sections_use_no_mechanism_words() -> None:
    gateway = _section(_read(_CA), "### Graph Write Gateway")
    actions = _section(gateway, "#### Actions")
    contract = "\n".join(
        [
            _section(gateway, "#### Invariants"),
            _section(gateway, "#### C-0 exceptions"),
            _section(gateway, "#### Outcomes"),
            "\n".join(line for line in actions.splitlines() if line.startswith("| ")),
        ]
    )

    assert _MECHANISM_WORDS.findall(contract) == []


def test_architecture_guidance_states_lock_order_and_the_single_replica_precondition() -> None:
    guidance = _gateway_section("#### Kind")

    order = next(line for line in guidance.splitlines() if line.startswith("- Lock order:"))
    assert order.index("in-process") < order.index("advisory lock")
    for needle in (
        "strategy: Recreate",
        "session advisory lock",
        "replicas",
        "committed_apply_pending",
        "reconciler",
        "GatewaySettings",
    ):
        assert needle in guidance, needle


def test_architecture_guidance_lists_exactly_the_permitted_log_fields() -> None:
    from ps_service.graph_gateway.gateway_log import ALLOWED_LOG_FIELDS

    guidance = _gateway_section("#### Kind")
    line = next(row for row in guidance.splitlines() if "Gateway log entries carry" in row)

    named = set(re.findall(r"`(\w+)`", line.split(";")[0]))
    assert named == set(ALLOWED_LOG_FIELDS)


def test_architecture_container_diagram_shows_startup_recovery_of_the_gateway() -> None:
    ca = _read(_CA)

    assert re.search(r'ProcessHarness -->\|"[^"]*replay[^"]*"\| GraphWriteGateway', ca)


def test_architecture_component_row_and_sequence_state_the_implemented_behaviour() -> None:
    ca = _read(_CA)
    sequence = _section(
        ca, "### Graph Write Gateway: SubmitGroup, commit-then-apply, apply pending (C-0)"
    )
    for needle in ("stale", "blocked"):
        assert needle in sequence, needle


# --- replay, digest, checkpoints, the startup gate, the harness -----------------------

_DOMAIN_CONCEPTS = _REPO / "docs" / "artifacts" / "ps-domain-concepts.md"
_PROCESS_HARNESS_HEADING = "### Process Harness"


def _ca_row(prefix: str) -> str:
    return next(line for line in _read(_CA).splitlines() if line.startswith(prefix))


def test_architecture_registers_replay_and_digest_as_implemented() -> None:
    ca = _read(_CA)
    gateway = _section(ca, "### Graph Write Gateway")
    intro = gateway.split("#### Domain Concepts")[0]
    registration = _gateway_section("#### Implementation Registration")
    actions = _gateway_section("#### Actions")

    for row in (
        _ca_row("| Graph Write Gateway | `ps."),
        _ca_row("| Digest checkpoint |"),
        _ca_row("| Canonical digest |"),
    ):
        assert "Target design" not in row, row[:60]
        assert "target design" not in row.split(":", 1)[0].lower(), row[:60]
    assert "contracts for those stay target design" not in intro
    assert "registered when #207 lands" not in registration
    for module in (
        "digest.py",
        "replay.py",
        "replay_gate.py",
        "replay_state.py",
        "startup_retry.py",
        "equivalence_harness.py",
    ):
        assert module in registration, module
    for action in ("Replay", "Verify"):
        row = next(r for r in actions.splitlines() if r.startswith(f"| {action} |"))
        assert "Target only" not in row, action
    assert "not yet wired" not in registration
    assert "are not drawn" not in ca
    assert "restore_exact_floats" not in ca
    assert "restore_properties" in registration


def test_architecture_describes_the_replay_gate_the_503_read_gate_and_ready() -> None:
    ca = _read(_CA)
    gateway = _section(ca, "### Graph Write Gateway")
    harness = _section(ca, _PROCESS_HARNESS_HEADING)
    kind = _gateway_section("#### Kind")

    for needle in (
        "graph_replay",
        "GraphReplayState",
        "startup_replay_max_backoff_seconds",
        "hiredis",
        "fail closed",
    ):
        assert needle in gateway, needle
    for needle in ("graph_replay", "gated_graphs", "503", "Retry-After", "/.well-known/"):
        assert needle in harness, needle
    for needle in ("pairs", "scalars", "float lists", "mixed lists"):
        assert needle in kind, needle


def test_architecture_describes_deterministic_replay_verification_and_the_equivalence_harness() -> (
    None
):
    gateway = _section(_read(_CA), "### Graph Write Gateway")
    kind = _gateway_section("#### Kind")
    invariants = _gateway_section("#### Invariants")

    for needle in (
        "verified_position",
        "unverified_entries",
        "equivalence_harness.py",
    ):
        assert needle in gateway, needle
    assert "digest" in kind and "checkpoint" in kind.lower()
    assert "sentinel" in invariants.lower() or "bookkeeping" in invariants.lower()
    assert "excluded from the digest" in gateway or "skipped by the digest" in gateway


def test_domain_concepts_define_the_canonical_digest_encoding() -> None:
    section = _section(_read(_DOMAIN_CONCEPTS), "### Canonical digest")

    for needle in ("labels", "embedding", "bit", "-0.0", "replay-progress", "sha256"):
        assert needle in section, needle
    assert "internal" in section.lower()


def test_operations_guide_documents_startup_replay_and_the_gated_graph_remedy() -> None:
    ops = _read(_OPS)
    section = _section(ops, "### Graph replay at startup and a gated graph")

    for needle in (
        "graph_replay",
        "gated_graphs",
        "503",
        "redis-cli",
        "DEL",
        "restart",
        "startup_replay",
    ):
        assert needle in section, needle
    assert "(#graph-replay-at-startup-and-a-gated-graph)" in ops


# --- ingestion writes through the gateway --------------------------------------------


def _ingestion_section() -> str:
    return _section(_read(_CA), "### Ingestion")


def test_architecture_ingestion_section_says_its_writes_go_through_the_gateway() -> None:
    ingestion = _ingestion_section()
    guidance = ingestion.split("#### Implementation Registration")[0]

    assert "Target design (not yet implemented" not in guidance
    for needle in (
        "ONE native group",
        "test_no_graph_write_path.py",
        "graph_submission.py",
        "system:ingestion",
        "ingestion_run.submit",
        "503",
        "never falls back to a direct FalkorDB write",
        "StampEdgeIdentity",
    ):
        assert needle in ingestion, needle
    assert "persist_native_structural_graph" not in ingestion


def test_architecture_diagram_and_rows_show_ingestion_as_a_gateway_writer() -> None:
    ca = _read(_CA)

    assert "edges of Ingestion, the Domain Mapper" in ca
    assert "Ingestion->>Gateway: SubmitGroup" in ca
    assert "Ingestion->>DB: PersistNativeStructuralGraph" not in ca
    assert "with no writer wired yet" not in ca
    assert "no writer is wired" not in ca


def test_architecture_gateway_section_documents_the_stamp_primitive_and_the_system_audit_row() -> (
    None
):
    gateway = _section(_read(_CA), "### Graph Write Gateway")

    for needle in (
        "StampEdgeIdentity",
        "stamp_legacy_edge_identity",
        "edge_identity_backfill.py",
        "system_audit.py",
        "system:ingestion",
        "AuditTrailUnavailableError",
    ):
        assert needle in gateway, needle


def test_domain_concepts_state_the_relationship_identity_and_the_system_audit_row() -> None:
    section = _section(_read(_DOMAIN_CONCEPTS), "## Graph Write Gateway and Mutation Log")

    for needle in (
        "Relationship identity",
        "modulo this property",
        "System audit row",
        "system:ingestion",
    ):
        assert needle in section, needle
    assert "Target design (not yet implemented" not in section


def test_the_mcp_copy_of_the_domain_concepts_is_the_documented_one() -> None:
    copy = _REPO / "ps-service" / "src" / "ps_service" / "mcp_interface" / "ps-domain-concepts.md"

    assert _read(copy) == _read(_DOMAIN_CONCEPTS)


def test_operations_guide_documents_the_503_when_the_gateway_is_not_running() -> None:
    ops = _read(_OPS)
    troubleshooting = _section(ops, "## Troubleshooting / FAQ")

    assert "graph writes are unavailable" in troubleshooting
    assert "Ingestion never writes to FalkorDB directly" in troubleshooting


# --- Company Merge writes through the gateway ----------------------------------------


def _company_merge_guidance() -> str:
    section = _section(_read(_CA), "### Company Merge")
    return section.split("#### Implementation Registration")[0]


def test_architecture_company_merge_section_says_its_writes_go_through_the_gateway() -> None:
    ca = _read(_CA)
    guidance = _company_merge_guidance()

    assert "Target design (not yet implemented" not in guidance
    for needle in (
        "test_no_graph_write_path.py",
        "create-only",
        "set-if-null",
        "`keep-separate` submits one group that deletes the `PendingReview`",
        "read_review_merge_sides",
        "choose_survivor",
        "Graph Cleanup `merge-capabilities` approval performs the merge",
        "StalePendingReviewError",
        "record_opening_row_with_event_id",
        "CompanyMergePersistenceError",
    ):
        assert needle in guidance, needle
    assert "read-resolve-write" not in guidance  # the near-miss merge path is gone
    assert "Company Merge target design" not in ca
    assert "Company Merge is its third" in ca


def test_architecture_company_merge_lists_the_observable_differences() -> None:
    guidance = _company_merge_guidance()
    differences = guidance.split("Write behaviour:")[1]

    for needle in (
        "`None`",  # a classification row with a None required property now raises
        "`status`",
        "node deleted between the merge's reads and its write",
        '(`stages_to_run == ("merge",)`)',  # merge-only resume needs the gateway
        "`ingestion_run.submit`",
        "BEFORE the opening audit row is written",  # keep-separate resolve pre-check
        "returns the executor's sanitized error",  # sign route, cleanup-kind approval
        "`GraphCleanupPersistenceError`",  # a near-miss merge's write refusal
        "the loser stays active with the `PendingReview` intact",
        "marks FalkorDB unhealthy only when a transient fault outlasts its retries",
        "UNIDENTIFIED_EDGE_SCAN",
        "PS state database",
    ):
        assert needle in differences, needle


def test_architecture_restore_registration_lists_no_deleted_interim_module() -> None:
    restore = _section(_read(_CA), "### Restore")
    registration = restore.split("#### Implementation Registration")[1].split("#### Actions")[0]

    for module in ("populate", "staging", "snapshot_merge_writer"):
        assert f"restore/{module}.py" not in registration, module
    for module in ("leg_primitives", "merge_primitives", "baseline_reader", "graph_submission"):
        assert f"restore/{module}.py" in registration, module
    assert "retired by #214" not in restore
    assert "Interim snapshot writer (#210)" not in restore
    assert "WATCH`/`MULTI`/`EXEC` optimistic-concurrency retry loop" not in restore


def test_no_target_design_marker_of_issue_214_is_left_in_the_architecture_documents() -> None:
    ca = _read(_CA)
    sa = _read(_SA)

    assert "(target design, #214)" not in ca
    assert "#214 target design" not in ca
    assert "Target design (#214)" not in ca
    assert "Target design delivered by #214" not in ca
    assert "Restore is target" not in sa
    assert "Restore's move onto it (#214) is target design" not in ca
    assert "Restore's move onto it (#214) is target design" not in sa
    assert "Restore's edge is target design" not in ca


def test_company_merge_registration_lists_the_gateway_modules_and_a_read_only_client() -> None:
    section = _section(_read(_CA), "### Company Merge")
    registration = section.split("#### Implementation Registration")[1].split("#### Actions")[0]
    paths = re.findall(r"^\| `([^`]+)` \|", registration, flags=re.MULTILINE)

    assert paths
    for path in paths:
        assert (_REPO / path).exists(), path
    assert {Path(path).name for path in paths} >= {
        "graph_submission.py",
        "generated_values.py",
        "pending_review.py",
    }
    assert "read-only client" in registration


def test_domain_concepts_and_operations_guide_name_company_merge_as_a_gateway_writer() -> None:
    section = _section(_read(_DOMAIN_CONCEPTS), "## Graph Write Gateway and Mutation Log")
    ops = _section(_read(_OPS), "## Troubleshooting / FAQ")

    assert "Company Merge (the merge stage and the near-miss resolution) since #210" in section
    assert "Company Merge" in ops
    assert "the approval stays pending" in ops


# --- Policy Lifecycle writes through the gateway -------------------------------------


def _policy_lifecycle_section() -> str:
    return _section(_read(_CA), "### Policy Lifecycle")


def _policy_lifecycle_guidance() -> str:
    return _policy_lifecycle_section().split("#### Implementation Registration")[0]


def test_architecture_policy_lifecycle_says_its_writes_go_through_the_gateway() -> None:
    guidance = _policy_lifecycle_guidance()

    assert "Target design (not yet implemented" not in guidance
    for needle in (
        "test_no_graph_write_path.py",
        "record_standalone_with_event_id",
        "one gateway group",
        "PolicyLifecycleGraphUnavailableError",
    ):
        assert needle in guidance, needle


def test_architecture_policy_lifecycle_cascade_is_one_gateway_group_not_one_cypher_statement() -> (
    None
):
    section = _policy_lifecycle_section()

    assert "one gateway group" in section.split("###### Constraints")[1].split("#### ")[0]
    assert "in one Cypher statement" not in section


def test_architecture_policy_lifecycle_keeps_both_halves_of_the_persistence_error_line() -> None:
    guidance = _policy_lifecycle_guidance()

    assert "`redis.exceptions.RedisError`" in guidance  # reads still wrap the driver error
    assert "`FalkorDB write failed: `" in guidance  # writes map the gateway failure


def test_architecture_policy_lifecycle_lists_the_observable_differences() -> None:
    guidance = _policy_lifecycle_guidance()
    differences = guidance.split("Write behaviour:")[1]
    differences = differences.split("\n- ")[0]

    for needle in (
        "`identity` property",
        "fails closed",
        "`get-policy`",
        "`FalkorDB write failed: `",
        "committed but not yet applied",
        "unrelated write",
        "full governed set",  # the repoint guard compares the prior's complete governed set
        "no audit link",
        "one atomic group",  # create is one group: no partial draft
        "does not create a second node",
        "full-edge scan",
        "`MutationGroup.audit_event_id` is optional",
    ):
        assert needle in differences, needle


def test_architecture_policy_lifecycle_actions_submit_groups_and_adds_are_idempotent() -> None:
    section = _policy_lifecycle_section()
    actions = section.split("#### Actions")[1].split("\n---")[0]
    rows = {
        line.split("|")[1].strip(): line for line in actions.splitlines() if line.startswith("| ")
    }

    assert "Writes FalkorDB" not in actions
    for name in ("CreatePolicyDraft", "ProposePolicy", "UpdatePolicyDraft", "AddControlToDraft"):
        assert "gateway group" in rows[name], name
    for name in ("AddStandardToDraft", "AddControlToDraft"):
        idempotent = rows[name].split("|")[-3]
        assert idempotent.strip().startswith("Yes"), name
        assert "one node" in idempotent, name
        assert "each call mints" not in rows[name]


def test_architecture_policy_lifecycle_registry_lists_the_submission_module() -> None:
    section = _policy_lifecycle_section()
    registration = section.split("#### Implementation Registration")[1].split("#### Actions")[0]
    paths = re.findall(r"^\| `([^`]+)` \|", registration, flags=re.MULTILINE)

    for path in paths:
        assert (_REPO / path).exists(), path
    assert "ps-service/src/ps_service/policy_lifecycle/graph_submission.py" in paths
    graph_writer = next(line for line in registration.splitlines() if "graph_writer.py" in line)
    assert "read layer and primitive builders" in graph_writer
    assert "read/write layer" not in graph_writer


def test_architecture_audit_event_id_is_optional_on_a_mutation_group() -> None:
    ca = _read(_CA)

    assert "`audit_event_id` is mandatory on a `MutationGroup`" not in ca
    assert "`audit_event_id` is optional on a `MutationGroup`" in ca
    assert "draft edits, governance backfills" in ca


def test_solution_architecture_and_domain_concepts_name_policy_lifecycle_as_a_gateway_writer() -> (
    None
):
    sa = _read(_SA)
    section = _section(_read(_DOMAIN_CONCEPTS), "## Graph Write Gateway and Mutation Log")

    writer_edge = sa.split("Cypher: read knowledge graph")[1].split("\n")[0]
    assert "Policy Lifecycle" in writer_edge
    assert "(target)" not in sa.split("Cypher: read knowledge graph")[1].split("\n")[0]
    assert (
        "Policy Lifecycle (creation, edits, transitions and governance backfills) since #211"
        in section
    )


def test_both_copies_of_the_domain_concepts_are_identical() -> None:
    copy = _REPO / "ps-service" / "src" / "ps_service" / "mcp_interface" / "ps-domain-concepts.md"

    assert _read(copy) == _read(_DOMAIN_CONCEPTS)
    assert "since #211" in _read(copy)


def test_operations_guide_says_draft_and_transition_tools_answer_an_error_without_a_gateway() -> (
    None
):
    ops = _section(_read(_OPS), "## Troubleshooting / FAQ")

    assert "Policy Lifecycle" in ops
    assert "answer `error: ...`" in ops
    assert "record no audit row" in ops


# --- Graph Cleanup writes through the gateway ----------------------------------------

_MCP_INTERFACE = _REPO / "ps-service" / "src" / "ps_service" / "mcp_interface"
_EXECUTORS = _REPO / "ps-service" / "src" / "ps_service" / "graph_cleanup" / "executors.py"
_CLEANUP_TOOL_FUNCTIONS = (
    "merge_capabilities_tool",
    "merge_obligations_tool",
    "release_capability_governance_tool",
    "unmerge_tool",
)


def _graph_cleanup_section() -> str:
    return _section(_read(_CA), "### Graph Cleanup")


def _graph_cleanup_guidance() -> str:
    return _graph_cleanup_section().split("#### Implementation Registration")[0]


def test_architecture_graph_cleanup_says_its_writes_go_through_the_gateway() -> None:
    ca = _read(_CA)
    guidance = _graph_cleanup_guidance()

    assert "Target design (not yet implemented" not in guidance
    for needle in (
        "test_no_graph_write_path.py",
        "record_standalone_with_event_id",
        "one gateway group",
        "committed_apply_pending",
        "is_caught_up",
    ):
        assert needle in guidance, needle
    assert "Graph Cleanup is its fifth" in ca


def test_architecture_graph_cleanup_lists_the_observable_differences() -> None:
    guidance = _graph_cleanup_guidance()
    differences = guidance.split("Write behaviour:")[1]
    differences = differences.split("\n- ")[0]

    for needle in (
        "`identity` property",
        "unrelated write",
        "stale",
        "committed but not yet applied",
        "success",
        "defers",
        "without a gateway",
        "`failed`",
        "identity-less",
        "collapse",
        "stamp",
        "logs nothing",
    ):
        assert needle in differences, needle


def test_architecture_graph_cleanup_constraints_describe_a_gateway_group() -> None:
    section = _graph_cleanup_section()
    constraints = section.split("###### Constraints")[1].split("#### Kind")[0]

    assert "one gateway group guarded by the graph's log position" in constraints
    assert "one all-or-nothing statement" not in section
    assert "once the graph has caught up to its log" in constraints


def test_architecture_graph_cleanup_registry_lists_the_gateway_modules_not_the_writer() -> None:
    section = _graph_cleanup_section()
    registration = section.split("#### Implementation Registration")[1].split("#### Actions")[0]
    paths = re.findall(r"^\| `([^`]+)`", registration, flags=re.MULTILINE)
    joined = " ".join(paths)

    assert "graph_cleanup/graph_submission.py" in joined
    assert "graph_cleanup/group_builders.py" in joined
    assert "graph_cleanup/graph_writer.py" not in joined
    for path in paths:
        for single in re.findall(r"ps-service/\S+?\.(?:py|md)", path):
            assert (_REPO / single).exists(), single
    dependencies = next(line for line in registration.splitlines() if "dependencies.py" in line)
    assert "Graph Write Gateway" in dependencies


def test_architecture_graph_cleanup_actions_submit_one_gateway_group() -> None:
    actions = _graph_cleanup_section().split("#### Actions")[1].split("\n---")[0]
    rows = {
        line.split("|")[1].strip(): line for line in actions.splitlines() if line.startswith("| ")
    }

    assert "same statement" not in actions
    for name in ("MergeCapabilities", "MergeObligations", "ReleaseCapabilityGovernance", "Unmerge"):
        assert "gateway group" in rows[name], name
    assert "defers" in rows["CheckCleanupApproval"]


def test_solution_architecture_and_domain_concepts_name_graph_cleanup_as_a_gateway_writer() -> None:
    sa = _read(_SA)
    section = _section(_read(_DOMAIN_CONCEPTS), "## Graph Write Gateway and Mutation Log")

    assert "Graph Cleanup" in sa.split("Cypher: read knowledge graph")[1].split("\n")[0]
    assert "Graph Cleanup (merge, release and unmerge) since #212" in section
    assert "since #212" in _read(
        _REPO / "ps-service" / "src" / "ps_service" / "mcp_interface" / "ps-domain-concepts.md"
    )


def test_operations_guide_says_graph_cleanup_without_a_gateway_records_a_failed_row() -> None:
    ops = _section(_read(_OPS), "## Troubleshooting / FAQ")

    assert "Graph Cleanup" in ops
    assert "check-cleanup-approval" in ops
    assert "`failed`" in ops


def _tool_docstrings(directory: Path = _MCP_INTERFACE) -> dict[str, str]:
    """Docstring of each cleanup tool function, in whichever module of `directory` defines it."""
    found: dict[str, str] = {}
    for path in sorted(directory.glob("*.py")):
        for node in ast.walk(ast.parse(_read(path))):
            if isinstance(node, ast.FunctionDef) and node.name in _CLEANUP_TOOL_FUNCTIONS:
                found[node.name] = ast.get_docstring(node) or ""
    return found


def test_the_four_cleanup_mcp_descriptions_say_one_all_or_nothing_graph_write() -> None:
    descriptions = _tool_docstrings()

    assert set(descriptions) == set(_CLEANUP_TOOL_FUNCTIONS)
    for name, text in descriptions.items():
        flat = " ".join(text.split())
        assert "one all-or-nothing graph write" in flat, name
        assert "gateway" not in flat, name
        assert "guarded statement" not in flat, name
        assert "one guarded write" not in flat, name


def test_the_executors_docstring_describes_audit_first_then_one_gateway_group() -> None:
    doc = " ".join((ast.get_docstring(ast.parse(_read(_EXECUTORS))) or "").split())

    assert "one gateway group" in doc
    assert "one guarded statement" not in doc
    assert "CHANGES.md" not in doc


def test_no_cleanup_source_or_doc_describes_a_direct_cleanup_write() -> None:
    cleanup = _REPO / "ps-service" / "src" / "ps_service" / "graph_cleanup"
    texts = [path.read_text(encoding="utf-8") for path in cleanup.glob("*.py")]
    texts.append(_graph_cleanup_section())

    for text in texts:
        assert "one guarded statement" not in text
        assert "one guarded, all-or-nothing statement" not in text


# --- Change Monitor and Export write through the gateway -----------------------------

_OPS_FAQ_HEADING = "## Troubleshooting / FAQ"
_CURATED_README = _REPO / "curated-content" / "README.md"
_MCP_COPY = _REPO / "ps-service" / "src" / "ps_service" / "mcp_interface" / "ps-domain-concepts.md"


def _export_guidance() -> str:
    return _section(_read(_CA), "### Export").split("#### Implementation Registration")[0]


def _change_monitor_guidance() -> str:
    section = _section(_read(_CA), "### Regulatory Change Monitor")
    return section.split("#### Implementation Registration")[0]


def _registry_paths(heading: str) -> list[str]:
    section = _section(_read(_CA), heading)
    registration = section.split("#### Implementation Registration")[1].split("#### Actions")[0]
    return re.findall(r"^\| `([^`]+)`", registration, flags=re.MULTILINE)


def test_architecture_export_says_its_backfill_goes_through_the_gateway() -> None:
    guidance = _export_guidance()

    assert "Target design (not yet implemented" not in guidance
    for needle in (
        "test_no_graph_write_path.py",
        "before any is submitted",
        "one gateway group",
        "system audit row",
        "SystemAuditRecorder",
        "ExportGraphWriteError",
        "GraphWriteUnavailableError",
    ):
        assert needle in guidance, needle


def test_architecture_change_monitor_says_its_succession_goes_through_the_gateway() -> None:
    guidance = _change_monitor_guidance()

    assert "Target design (not yet implemented" not in guidance
    for needle in (
        "test_no_graph_write_path.py",
        "GraphWriteUnavailableError",
        "audit_event_id",
        "ingestion_run.submit",
        "SuccessionPersistenceError",
        "`linked`",
    ):
        assert needle in guidance, needle


def test_architecture_change_monitor_lists_the_observable_differences() -> None:
    guidance = _change_monitor_guidance()
    differences = guidance.split("Write behaviour:")[1]
    differences = differences.split("\n- ")[0]

    for needle in (
        "identity",
        "SUPERSEDED_BY:<prior>-><new>",
        "ChangeMonitorStateError",
        "committed_apply_pending",
        "SuccessionPersistenceError",
        "system audit row",
        "mid-run",
        "link-only",
        "`finalize`",
        "submit",
        "gateway first",
        "FalkorDB write failed:",
        "StampEdgeIdentity",
        "`policy_system`",
    ):
        assert needle in differences, needle
    assert "null audit link" not in differences
    assert "audit_event_id=None" not in differences


@pytest.mark.parametrize("heading", ["### Export", "### Regulatory Change Monitor"])
def test_architecture_registry_paths_exist_and_name_no_removed_writer(heading: str) -> None:
    paths = _registry_paths(heading)

    assert paths
    for path in paths:
        for single in re.findall(r"(?:ps-service|tools)/\S+?\.(?:py|md)", path):
            assert (_REPO / single).exists(), single


def test_architecture_change_monitor_registry_lists_the_gateway_seam() -> None:
    section = _section(_read(_CA), "### Regulatory Change Monitor")
    registration = section.split("#### Implementation Registration")[1].split("#### Actions")[0]
    succession = next(line for line in registration.splitlines() if "succession.py" in line)

    assert "change_monitor/graph_submission.py" in registration
    assert "one fused Cypher statement" not in registration
    assert "Cypher" not in succession
    assert "gateway group" in succession


def test_architecture_export_registry_lists_the_audit_module_and_gateway_backfill() -> None:
    section = _section(_read(_CA), "### Export")
    registration = section.split("#### Implementation Registration")[1].split("#### Actions")[0]
    embeddings = next(line for line in registration.splitlines() if "export/embeddings.py" in line)

    assert "gateway group" in embeddings
    assert "export/errors.py" in registration
    assert "ExportGraphWriteError" in registration
    assert "api/export_audit_actions.py" in registration


def test_architecture_actions_describe_gateway_groups_not_cypher_statements() -> None:
    cm = _section(_read(_CA), "### Regulatory Change Monitor").split("#### Actions")[1]
    trigger = next(line for line in cm.splitlines() if line.startswith("| TriggerReingestion"))
    export = _section(_read(_CA), "### Export").split("#### Actions")[1]
    export_row = next(line for line in export.splitlines() if line.startswith("| ExportInstrument"))

    assert "gateway group" in trigger
    assert "GraphWriteUnavailableError" in trigger
    assert "gateway group" in export_row
    assert "system audit row" in export_row
    assert "Writes embeddings onto the source FalkorDB" not in export_row


def test_architecture_check_regulations_row_says_link_only_runs_write_the_audit_pair() -> None:
    row = next(
        line for line in _read(_CA).splitlines() if line.startswith("| CheckRegulations | Accept")
    )

    assert "a link-only completion that runs no stage, writes nothing" not in row
    assert "`already_processed` outcome writes nothing" in row
    assert "link-only" in row
    assert "`finalize`" in row


def test_architecture_names_change_monitor_and_export_as_gateway_writers() -> None:
    ca = _read(_CA)
    gateway = _section(ca, "### Graph Write Gateway")
    sa = _read(_SA)

    assert "Regulatory Change Monitor (re-ingest succession) is its sixth" in gateway
    assert "Export (embedding backfill) is its seventh" in gateway
    assert "Graph Cleanup is its fifth" in gateway
    assert "Restore" in gateway.split("seventh")[1].split("A test-side harness")[0]
    writer_edge = sa.split("Cypher: read knowledge graph")[1].split("\n")[0]
    assert "Regulatory Change Monitor" in writer_edge
    assert "Export" in writer_edge
    assert "(#213-#214)" not in _section(
        ca, "### Graph Write Gateway: SubmitGroup, commit-then-apply, apply pending (C-0)"
    )


def test_domain_concepts_name_restore_as_a_gateway_writer_since_214() -> None:
    section = _section(_read(_DOMAIN_CONCEPTS), "## Graph Write Gateway and Mutation Log")

    assert "Regulatory Change Monitor (re-ingest succession) and Export (embedding backfill)" in (
        section
    )
    assert "since #213" in section
    assert "Restore" in section.split("since #213")[1].split("\n")[0]
    assert "since #214" in section
    assert "until then its direct writes are not in the log" not in section
    assert _read(_MCP_COPY) == _read(_DOMAIN_CONCEPTS)


def test_operations_guide_503_row_names_the_amendment_check_and_exports() -> None:
    ops = _section(_read(_OPS), _OPS_FAQ_HEADING)
    row = next(line for line in ops.splitlines() if "graph writes are unavailable" in line)

    for needle in (
        "Regulatory Change Monitor",
        "`check-regulations`",
        "`POST /change-checks`",
        "`finalize`",
        "link-only",
        "Export",
        "`POST /exports`",
        "PS state database",
        "curated-export",
    ):
        assert needle in row, needle


def test_curated_content_readme_says_the_export_cli_needs_the_ps_state_database() -> None:
    text = " ".join(_read(_CURATED_README).split())

    assert "PS state Postgres" in text
    assert "Graph Write Gateway" in text
    assert "system audit row" in text


# --- Restore commits each restore as one gateway group (design record) -------------------

_RESTORE_DECISIONS = "#### Guarantees"
_RESTORE_C0 = "#### Observable behaviour"


def _restore_decisions() -> str:
    return _section(_section(_read(_CA), "### Restore"), _RESTORE_DECISIONS)


def test_architecture_restore_states_the_gateway_group_guarantees() -> None:
    decisions = _restore_decisions()

    for guarantee in (
        "**Baseline merge as primitives.**",
        "**A group may span graphs.**",
        "**Swap apply.**",
        "**Replace.**",
        "**Audit linkage.**",
    ):
        assert guarantee in decisions, guarantee
    assert "one log position" in decisions
    assert "Restore holds no write path to the graph store" in decisions


def test_architecture_restore_d5_maps_every_outcome_to_a_terminal_audit_row() -> None:
    decisions = _restore_decisions()

    for needle in (
        "RestoreConcurrencyConflictError",
        "RestoreGroupRejectedError",
        "RestoreApplyPendingError",
        "group_rejected",
        "committed_apply_pending",
        "opening row",
    ):
        assert needle in decisions, needle


def test_architecture_restore_decisions_state_contracts_without_mechanism_words() -> None:
    decisions = _restore_decisions()
    prose = "\n".join(line for line in decisions.splitlines() if not line.startswith("| "))

    assert _MECHANISM_WORDS.findall(prose) == []
    assert "(replaces" not in decisions
    assert "Stage-1 review" not in decisions
    assert "NOT done" not in decisions


def test_architecture_restore_states_the_seven_observable_behaviours() -> None:
    restore = _section(_read(_CA), "### Restore")
    exceptions = _section(restore, _RESTORE_C0)

    assert len(re.findall(r"^\d+\. \*\*", exceptions, flags=re.MULTILINE)) == 7
    for needle in (
        "committed, apply pending",
        "scales with the total size of the single-tenant graph",
        "mutation log",
        "carry an identity",
        "creates no graph",
        "ArtifactContentRejectedError",
        "503",
    ):
        assert needle in exceptions, needle
    assert _MECHANISM_WORDS.findall(exceptions) == []


def test_architecture_gateway_records_the_multi_graph_group_swap_and_replace_guarantees() -> None:
    gateway = _section(_read(_CA), "### Graph Write Gateway")
    concepts = _section(gateway, "#### Domain Concepts")
    invariants = _gateway_section("#### Invariants")
    outcomes = _gateway_section("#### Outcomes")
    exceptions = _gateway_section("#### C-0 exceptions")
    actions = _gateway_section("#### Actions")

    assert "more than one graph" in concepts
    for name in ("Set atomicity", "Swap none-or-all", "Replace exactness"):
        assert f"**{name}:**" in invariants, name
    assert "per-graph" in outcomes.lower()
    assert "waits" in exceptions
    assert "| SubmitMultiGraphGroup |" in actions
    assert "CaughtUp" in next(line for line in actions.splitlines() if "| CaughtUp |" in line)
    assert "whole set" in next(line for line in actions.splitlines() if "| CaughtUp |" in line)


def test_architecture_gateway_guidance_holds_the_multi_graph_mechanics() -> None:
    guidance = _gateway_section("#### Kind")
    line = next(
        row for row in guidance.splitlines() if row.startswith("- Multi-graph group mechanics")
    )

    for needle in ("ascending graph name", "swap_set_id", "in-process", "advisory"):
        assert needle in line, needle
    replace = next(row for row in guidance.splitlines() if row.startswith("- Replace expansion"))
    assert "DeleteNode" in replace
    assert "StampEdgeIdentity" in replace


def test_architecture_sequence_diagrams_the_multi_graph_path() -> None:
    text = _read(_CA)
    heading = "### Graph Write Gateway: SubmitMultiGraphGroup, one transaction, swap apply"
    section = _section(text, heading)

    assert "```mermaid" in section
    assert "SubmitMultiGraphGroup" in section


def test_domain_concepts_say_a_transaction_group_may_span_graphs() -> None:
    section = _section(_read(_DOMAIN_CONCEPTS), "### Transaction group")

    assert "more than one graph" in section
    assert _read(_MCP_COPY) == _read(_DOMAIN_CONCEPTS)


def test_architecture_restore_registration_lists_the_primitive_builders_and_they_exist() -> None:
    restore = _section(_read(_CA), "### Restore")
    registration = restore.split("#### Implementation Registration")[1].split("#### Actions")[0]

    for module in ("baseline_reader", "leg_primitives", "merge_primitives", "graph_submission"):
        path = f"ps-service/src/ps_service/restore/{module}.py"
        assert f"| `{path}` |" in registration, path
        assert (_REPO / path).is_file(), path


def test_operations_guide_503_row_names_restore_and_a_blocked_swap_has_its_own_row() -> None:
    ops = _section(_read(_OPS), _OPS_FAQ_HEADING)
    row = next(line for line in ops.splitlines() if "graph writes are unavailable" in line)
    swap = next(line for line in ops.splitlines() if "committed_apply_pending" in line)

    for needle in (
        "`POST /restorations`",
        "`POST /restorations/from-catalog`",
        "`restore-instrument`",
    ):
        assert needle in row, needle
    for needle in ("restart `ps-service`", "ascending graph-name order", "different states"):
        assert needle in swap, needle
    assert "no partial state is read" not in swap
    assert "no partial state is read" not in _read(_OPS)
    assert "hold_seconds" in _read(_OPS)


def test_architecture_registers_the_gateway_owned_connection_modules() -> None:
    text = _read(_CA)

    for module in ("connection.py", "key_port.py"):
        row = next(
            line
            for line in text.splitlines()
            if f"`ps-service/src/ps_service/graph_gateway/{module}`" in line
        )
        assert "FalkorDB" in row, module
    assert "the only module that imports the FalkorDB driver" in text
    probe_row = next(
        line
        for line in text.splitlines()
        if "`ps-service/src/ps_service/graph_gateway/connectivity.py`" in line
    )
    assert "driver-free" in probe_row


def test_architecture_describes_the_server_side_read_only_query_path() -> None:
    text = _read(_CA)
    engine_section = _section(text, "### Query Engine")

    assert "GRAPH.RO_QUERY" in engine_section
    assert "the boundary available today" in engine_section
    row = next(
        line
        for line in text.splitlines()
        if "`ps-service/src/ps_service/graph_gateway/read_only.py`" in line
    )
    assert "ReadOnlyGraph" in row
    query_client_row = next(
        line
        for line in text.splitlines()
        if "`ps-service/src/ps_service/query_engine/falkordb_client.py`" in line
    )
    assert "ReadOnlyGraph" in query_client_row


def test_architecture_says_every_reader_client_is_driver_free() -> None:
    text = _read(_CA)

    for package in ("ingestion", "domain_mapper", "company_merge", "change_monitor"):
        row = next(
            line
            for line in text.splitlines()
            if f"`ps-service/src/ps_service/{package}/falkordb_client.py`" in line
        )
        assert "ReadOnlyDatabase" in row, package
    export_row = next(
        line
        for line in text.splitlines()
        if "`ps-service/src/ps_service/export/falkordb_connection.py`" in line
    )
    assert "ReadOnlyDatabase" in export_row
    assert "key_port.py" in export_row


def test_operations_guide_documents_the_graph_drift_alert() -> None:
    section = _section(_read(_OPS), "### Graph drift alert")

    for needle in (
        "drift_check",
        "GraphDriftDetected",
        "checkpoint_digest",
        "live_digest",
        "no_baseline",
        "skipped",
        "PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS",
        "psService.graphGateway.driftCheckIntervalSeconds",
        "kubectl logs",
        "never repaired",
        "redis-cli DEL",
    ):
        assert needle in section, needle
    assert '"outcome": "failed"' in section


def test_values_reference_lists_the_drift_check_interval() -> None:
    row = next(
        line
        for line in _read(_VALREF).splitlines()
        if "`psService.graphGateway.driftCheckIntervalSeconds`" in line
    )

    assert "PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS" in row
    assert "`3600`" in row
    assert "`0`" in row


def test_architecture_describes_requested_checkpoints_and_the_drift_check() -> None:
    text = _read(_CA)
    gateway = _section(text, "### Graph Write Gateway")

    assert "**Drift is detected, never repaired:**" in gateway
    assert "GraphDriftDetected" in gateway
    assert "requests a digest checkpoint on its terminal group" in gateway
    for field in ("checkpoint_digest", "live_digest", "graph_count", "drift_count", "reason"):
        assert f"`{field}`" in gateway, field
    for action in ("drift_check", "drift_pass", "drift_monitor"):
        assert f"`{action}`" in gateway, action
    drift_row = next(
        line
        for line in text.splitlines()
        if "`ps-service/src/ps_service/graph_gateway/drift.py`" in line
    )
    assert "DriftChecker" in drift_row
    assert any(line.startswith("| CheckDrift |") for line in gateway.splitlines())


# --- gateway-only graph access and read-only reads ---------------------------------


def test_architecture_states_gateway_only_graph_access_and_read_only_reads() -> None:
    gateway = _section(_read(_CA), "### Graph Write Gateway")

    assert "**Gateway-only graph access:**" in gateway
    assert "**Read-only reads:**" in gateway
    assert "reads stay direct" not in gateway
    assert "test_gateway_only_graph_access.py" in gateway
    assert "no allow-list" in gateway
    assert "tools/curated-export/export_instrument.py" in gateway
    assert "Every writer requests a digest checkpoint" in gateway


def test_architecture_restore_describes_no_deleted_interim_module() -> None:
    restore = _section(_read(_CA), "### Restore")

    assert "unreferenced by Restore" not in restore
    assert "allows them by name" not in restore
    assert "deleted by #215" not in restore


def test_architecture_diagram_comment_names_the_gateway_only_rule() -> None:
    text = _read(_CA)

    assert "outside the gateway package reaches FalkorDB only through" in text


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def test_operations_guide_backup_is_one_pg_basebackup_operation() -> None:
    backup = _collapsed(_section(_read(_OPS), "## Backup"))

    assert _BACKUP_COMMAND in backup
    assert "tar tf" in backup
    assert "verify-backup-restore.sh backup" in backup
    for retired in ("redis-cli", "dump.rdb", "pg_dump", "pg_restore", "each is independent"):
        assert retired not in backup, retired


def test_operations_guide_backup_states_contents_and_encryption() -> None:
    backup = _collapsed(_section(_read(_OPS), "## Backup"))

    assert "password hashes" in backup
    assert "audit trail" in backup
    assert "encrypted" in backup
    assert "not encrypted by Postgres or by the command" in backup


def test_operations_guide_backup_excludes_authentik_and_links_the_procedure() -> None:
    backup = _collapsed(_section(_read(_OPS), "## Backup"))

    assert "Authentik" in backup
    assert "not covered" in backup
    assert _AUTHENTIK_BACKUP_LINK in backup
    assert _AUTHENTIK_RESTORE_LINK in backup
    assert "docs.goauthentik.io" not in backup


def test_operations_guide_authentik_backup_and_restore_sections() -> None:
    ops = _read(_OPS)
    backup = _collapsed(_section(ops, "## Authentik Backup"))
    restore = _collapsed(_section(ops, "## Authentik Restore"))

    for needle in ("pg_dump", "-Fc", "pg_restore -l", "encrypted", "AUTHENTIK_SECRET_KEY"):
        assert needle in backup, needle
    for needle in ("passkey", "pg_restore", "principal_subject"):
        assert needle in backup + restore, needle
    assert "user and their enrolled passkey" in backup
    assert "policy-system-authentik-api-token" in backup
    for needle in (
        "Restore PS Postgres first",
        "Start PS Service last",
        "different points in time",
    ):
        assert needle in restore, needle


def test_operations_guide_backup_has_no_change_history_wording() -> None:
    backup = _collapsed(_section(_read(_OPS), "## Backup")).lower()

    for phrase in ("replaces", "no longer", "previously", "used to"):
        assert phrase not in backup, phrase


def test_operations_guide_restore_runs_the_provisioning_job_with_retained_secrets() -> None:
    restore = _collapsed(_section(_read(_OPS), "## Restore"))

    assert "helm upgrade" in restore
    assert "--wait-for-jobs" in restore
    assert "--reset-then-reuse-values" in restore
    for role in ("admin", "state", "signing"):
        assert f"psPostgres.{role}.existingSecret" in restore
    assert "ps-postgres-init: created roles" in restore
    assert "ps-state-provision-r" in restore


def test_operations_guide_restore_stops_ps_postgres_before_retaining_secrets() -> None:
    restore = _collapsed(_section(_read(_OPS), "### Before you start"))

    retain = restore.index("To retain the current passwords of a running release")
    scale = restore.index("deploy/policy-system-ps-postgres --replicas=0", retain)
    upgrade = restore.index("helm upgrade", retain)
    assert scale < upgrade
    assert "Multi-Attach" in restore


def test_operations_guide_restore_stops_ps_service_and_empties_falkordb() -> None:
    restore = _collapsed(_section(_read(_OPS), "## Restore"))

    assert "kubectl scale deploy/policy-system-ps-service" in restore
    assert "--replicas=0" in restore
    assert "FalkorDB is emptied" in restore
    assert "/fdb" in restore
    assert "kubectl scale deploy/policy-system-ps-service --replicas=1" in restore


def test_operations_guide_restore_states_the_ready_outcome() -> None:
    restore = _collapsed(_section(_read(_OPS), "## Restore"))

    assert "503" in restore
    assert "graph_replay" in restore
    assert "empty `gated_graphs`" in restore
    assert "verify-backup-restore.sh restore" in restore
    assert "wait-ready" in restore
    assert "verify-provision" in restore


def test_operations_guide_restore_documents_the_alter_role_fallback_for_a_lost_secret() -> None:
    restore = _collapsed(_section(_read(_OPS), "### If a credentials Secret is lost"))

    assert "ALTER ROLE postgres_admin PASSWORD" in restore
    assert "ALTER ROLE ps_state PASSWORD" in restore
    assert "ALTER ROLE ps_signing PASSWORD" in restore
    assert "--set psPostgres.admin.existingSecret" in restore


def test_operations_guide_restore_has_no_retired_text_or_change_history() -> None:
    restore = _collapsed(_section(_read(_OPS), "## Restore"))

    for retired in (
        "redis-cli",
        "dump.rdb",
        "pg_dump",
        "pg_restore",
        "each is independent",
    ):
        assert retired not in restore, retired
    for phrase in ("replaces", "no longer", "previously", "used to"):
        assert phrase not in restore.lower(), phrase


def test_values_reference_existing_secret_row_gives_the_restore_guidance() -> None:
    row = _collapsed(
        next(
            line
            for line in _read(_VALREF).splitlines()
            if "psPostgres.admin.existingSecret" in line and line.startswith("|")
        )
    )

    assert "operations-guide.md#restore" in row
    assert "backup" in row
    assert "different name" in row


_README = _REPO / "README.md"
_COLLAPSE_SECTIONS = ("## Backup", "## Restore")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def test_no_old_backup_statements() -> None:
    """No old backup procedure, marker or stale persistence claim remains."""
    ops = _read(_OPS)
    backup_restore = _collapse(" ".join(_section(ops, heading) for heading in _COLLAPSE_SECTIONS))
    for absent in ("redis-cli", "dump.rdb", "pg_dump", "pg_restore", "each is independent", "#216"):
        assert absent not in backup_restore, absent
    for present in (
        "pg_basebackup",
        "-X fetch",
        "-c fast",
        "encrypted",
        "existingSecret",
        "--wait-for-jobs",
        _AUTHENTIK_BACKUP_LINK,
        _AUTHENTIK_RESTORE_LINK,
    ):
        assert present in backup_restore, present

    ops_all = _collapse(ops)
    for absent in (
        "survives a pod restart",
        "persists to a `PersistentVolumeClaim` (see [Backup]",
    ):
        assert absent not in ops_all, absent

    valref = _collapse(_read(_VALREF))
    for absent in ("survives a pod restart", "data lost on pod restart"):
        assert absent not in valref, absent
    assert "avoids a replay" in valref

    install = _collapse(_read(_INSTALL)) + _collapse(_read(_EVAL_INSTALL))
    assert "Reload the FalkorDB data" not in install
    assert "operations-guide.md#restore" in _collapse(_read(_EVAL_INSTALL))

    sa = _collapse(_read(_SA))
    for absent in ("(target)", "mutation log, target)"):
        assert absent not in sa, absent

    readme_rows = [
        _collapse(line) for line in _read(_README).splitlines() if "Backup & Restore" in line
    ]
    assert len(readme_rows) == 1
    row = readme_rows[0]
    assert "PS Postgres Backup & Restore" in row
    for absent in ("Azure Blob", "Scheduled backups of the compliance graph", "FalkorDB Backup"):
        assert absent not in row, absent

    history = _collapse(
        " ".join(
            _section(ops, heading)
            for heading in (*_COLLAPSE_SECTIONS, "### Authentik is not covered")
        )
    ).lower()
    for absent in ("replaces", "no longer", "previously", "used to"):
        assert absent not in history, absent


def test_operations_guide_states_what_an_unauthenticated_ready_reveals() -> None:
    ops = _read(_OPS)
    section = _collapsed(_section(ops, "### What `/ready` reveals"))

    for needle in (
        '{"status": "ready"}',
        '{"status": "not_ready"}',
        "unhealthy_dependencies",
        "gated_graphs",
        "Authorization: Bearer",
        "local-test bypass",
        "not an error",
    ):
        assert needle in section, needle
    assert "(#what-ready-reveals)" in ops


def test_operations_guide_restore_says_the_gated_check_needs_a_ready_token() -> None:
    restore = _collapsed(_section(_read(_OPS), "## Restore"))

    assert "PS_READY_TOKEN" in restore
    assert "gated_graphs=unchecked" in restore


def test_architecture_documents_state_unauthenticated_ready_is_status_only() -> None:
    ca = _collapsed(_read(_CA))
    sa = _collapsed(_read(_SA))

    assert 'the body is `{"status": "ready" | "not_ready"}` for every unauthenticated' in ca
    assert "never answered `401`" in ca
    assert "only to a caller with a valid bearer token" in sa
    assert "reported by name only;" not in sa


def test_helm_values_reference_shows_the_anonymous_and_the_authenticated_ready_answer() -> None:
    valref = _read(_VALREF)

    assert 'anonymous: {"status":"ready"}' in valref
    assert "Authorization: Bearer $ACCESS_TOKEN" in valref


def test_the_cleanup_docstring_scan_reaches_a_probe_in_any_tool_module(tmp_path: Path) -> None:
    (tmp_path / "probe_tools.py").write_text(
        'def merge_capabilities_tool() -> None:\n    """probe docstring"""\n', encoding="utf-8"
    )

    assert _tool_docstrings(tmp_path) == {"merge_capabilities_tool": "probe docstring"}


def test_each_cleanup_tool_function_is_defined_in_exactly_one_module() -> None:
    owners = [
        path.name
        for path in sorted(_MCP_INTERFACE.glob("*.py"))
        for node in ast.walk(ast.parse(_read(path)))
        if isinstance(node, ast.FunctionDef) and node.name in _CLEANUP_TOOL_FUNCTIONS
    ]

    assert len(owners) == len(_CLEANUP_TOOL_FUNCTIONS)
