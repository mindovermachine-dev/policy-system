"""Content lint: the operator and architecture docs describe the graph log provisioning (#205).

The immutable `graph_log` tables are created by a privileged, admin-credential step (the
`ps-state-provision` Job, or the same CLI by hand), never by ps-service itself. These tests pin
the facts an operator or reviewer needs: the upgrade step for existing deployments (the Postgres
init script runs only on an empty `PGDATA`), the failure message and its remedy, the restore
consequence, the residuals, the new values and env vars, and the architecture registration.
Anchors are headings and quoted text, never line numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_OPS = _REPO / "docs" / "artifacts" / "operations-guide.md"
_INSTALL = _REPO / "docs" / "artifacts" / "installation-guide.md"
_VALREF = _REPO / "docs" / "artifacts" / "helm-chart-values-reference.md"
_CA = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_SA = _REPO / "docs" / "architecture" / "ps-solution-architecture.md"
_PROVISION_COMMAND = "python -m ps_service.graph_gateway.provision"
_MISSING_MIGRATION = "graph_gateway/0001_graph_mutation_log.sql"


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


def test_operations_guide_names_the_missing_migration_remedy_command() -> None:
    section = _section(_read(_OPS), "### Upgrading to the graph mutation log")

    assert _MISSING_MIGRATION in section
    assert _PROVISION_COMMAND in section
    assert "not_ready" in section


def test_operations_guide_restore_needs_admin_credentials_and_states_the_residuals() -> None:
    ops = _read(_OPS)
    restore = _section(ops, "## Restore")

    assert "admin" in restore
    assert "ps_state_graph_owner" in restore
    assert "#216" in ops
    # Residual: ps_state owns the database, so "cannot DROP" must not be over-claimed.
    upgrade = _section(ops, "### Upgrading to the graph mutation log")
    assert "DROP DATABASE" in upgrade
    assert "#151" in upgrade
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


def test_ca_states_the_command_link_is_optional_at_the_store_and_required_at_the_gateway() -> None:
    gateway = _section(_read(_CA), "### Graph Write Gateway")

    assert "optional at the store" in gateway
    assert "required at the gateway" in gateway


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


# --- issue #206: the gateway proper (submit, apply, catch-up, recovery) --------------------------

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
        "_recover_graph_gateway_at_startup",
    ):
        assert f"`{entry_point}" in registration, entry_point


def test_architecture_actions_table_lists_every_gateway_action() -> None:
    actions = _gateway_section("#### Actions")

    for action in ("SubmitGroup", "SubmitGroupInTransaction", "CaughtUp", "IsCaughtUp", "Recover"):
        assert f"| {action} |" in actions, action


def test_architecture_gateway_intro_says_what_is_built_and_what_is_not() -> None:
    gateway = _section(_read(_CA), "### Graph Write Gateway")
    intro = gateway.split("#### Domain Concepts")[0]

    assert "#205" in intro
    assert "#206" in intro
    assert "no writer" in intro.lower()
    assert "#207" in intro
    assert "#208" in intro
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
        "#208",
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

    assert re.search(r'ProcessHarness -->\|"[^"]*recover[^"]*"\| GraphWriteGateway', ca)


def test_architecture_component_row_and_sequence_state_the_implemented_behaviour() -> None:
    ca = _read(_CA)
    row = next(line for line in ca.splitlines() if line.startswith("| Graph Write Gateway | `ps."))
    sequence = _section(
        ca, "### Graph Write Gateway: SubmitGroup, commit-then-apply, apply pending (C-0)"
    )

    assert "#206" in row
    assert "#206" in sequence
    for needle in ("stale", "blocked"):
        assert needle in sequence, needle
