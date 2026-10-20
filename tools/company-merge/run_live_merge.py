#!/usr/bin/env python3
r"""Run the live Company Merge of the three external baselines into `policy_system`.

A re-runnable, approval-gated operator CLI. It merges the `CRA-1.0`, `NIS2-1.0` and
`GDPR-1.0` baseline graphs into the shared `policy_system` graph by calling
`ps_service.company_merge.merge.merge_baseline_graph` once per regulation, then writes
a JSON before/after report.

**This script is the single approved place a live write into the real, shared
`policy_system` graph is issued from by the Company Merge tooling.** It refuses to
proceed past two gates before any write:

1. **Approval gate** (`ps_service.company_merge.live_merge_approval.
   load_and_validate_approval`): a human-authored approval file, never written by this
   script, must already exist at `--approval-file` and validate for the given
   `--run-kind` (`initial` or `rerun`). The run kind selects one of two hardcoded
   confirmation phrases; it is never a free-form value. Checked first, before any
   graph is selected.
2. **Precondition check**
   (`ps_service.company_merge.preconditions.check_obligation_has_edge_cardinality`):
   re-run live, immediately before the merge, against the `cra_baseline`,
   `gdpr_baseline` and `nis2_baseline` graphs. Any violation on any graph blocks the
   merge entirely; no `merge_baseline_graph` call is issued.

Only once both gates pass does it merge CRA-1.0, then NIS2-1.0, then GDPR-1.0, in that
fixed order (CRA and NIS2 refresh already-merged data, GDPR is the genuinely new
merge, so a problem surfaces on the lower-risk case first). Each regulation runs under
its own `run_id` (a fixed live-merge prefix, the lower-cased short name and the UTC
start timestamp) and the run stops at the first regulation that raises.

A before/after snapshot of the graph is written into the report: node counts, edge
counts, node id sets, node properties (with `embedding` stripped from Capability
nodes), DEFINES/EXPRESSES edge properties, and HAS/SATISFIED_BY/REQUIRES edge
endpoint pairs. On full success the approval file is renamed with a
`.consumed-<timestamp>` suffix so it cannot be reused; on any failure it is left
untouched, so a retry against the same still-valid approval remains possible.

    uv run tools/company-merge/run_live_merge.py \\
        --approval-file ./APPROVAL_LIVE_MERGE_INITIAL.md \\
        --run-kind initial

Options:
    --approval-file  Path to the human-authored approval record (required).
    --run-kind       `initial` or `rerun` (required); selects the confirmation phrase
                     the approval file must carry.
    --report-path    Where the JSON before/after report is written (default: a
                     UTC-timestamped file in the tool's built-in report directory).
    --host, --port   FalkorDB location (default: env PS_FALKORDB_HOST /
                     PS_FALKORDB_PORT, else localhost:6379).

Exit codes:
    0   Full success; the report path and consumed approval path are printed.
    1   FalkorDB failed during the precondition check, the merge threshold or
        embedding model is not configured, or the Graph Write Gateway could not be
        built (for example the PS state database is not configured).
    2   The approval gate failed.
    3   At least one baseline graph violates the precondition.
    4   `merge_baseline_graph` or the per-regulation audit row failed partway through;
        the report lists the error.

**Scope.** This CLI merges the three external regulations only. It passes no
`governance=` to `merge_baseline_graph`, which imports an internal-sourced baseline's
Policy/Standard/Control tree as caller-owned drafts and so needs an owner: pointed at
an internal baseline that carries a Policy, the merge fails closed with
`GovernanceOwnerRequiredError` and the run is reported as a failure (exit code 4).
Internal documents are ingested through `POST /ingestions` (or restored), not through
this tool.

**Writes go through the Graph Write Gateway.** `merge_baseline_graph` does not write
`policy_system` directly: every write is a logged group submitted to the gateway,
which anchors it to an audit event. This CLI builds the gateway from the service
configuration (`build_default_graph_write_gateway`) and, for each regulation, records
one system-actor `ingestion_run.submit` audit row whose `resource_id` is that run's id
(`build_system_audit_recorder`), then hands the gateway, the two graph names and the
row's id to the merge. It fails closed: if the PS state database is not configured no
gateway can be built and it exits 1 before any graph is selected; if the audit row
cannot be written the run stops as a merge failure (exit 4) before that regulation's
merge. The CLI therefore needs the PS state database (`PS_STATE_POSTGRES_*`) in
addition to FalkorDB.

Requires a running FalkorDB with `cra_baseline`, `gdpr_baseline`, `nis2_baseline` and
`policy_system` reachable, the PS state Postgres, and
`PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD`, `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD`,
`PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD` and
`PS_LLMINTERFACE_EMBED_MODEL` resolvable through `ps_service.config.load_config()`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ps_service.company_merge.falkordb_client import (
    connect,
    select_graph,
    single_tenant_graph_name,
)
from ps_service.company_merge.live_merge_approval import (
    LiveMergeApprovalError,
    load_and_validate_approval,
)
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.preconditions import check_obligation_has_edge_cardinality
from ps_service.config import ServiceConfig, load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.ingestion_runs.audit_actions import (
    INGESTION_RUN_RESOURCE_TYPE,
    INGESTION_RUN_SUBMIT_ACTION,
)
from ps_service.logging import bind_run_context
from ps_service.logging.facade import configure as configure_logging
from ps_service.logging.facade import resolve_default_log_path

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Literal

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import MergeResult
    from ps_service.graph_gateway.system_audit import SystemAuditRecorder
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

# Connection defaults, env-driven -- same PS_FALKORDB_HOST/PS_FALKORDB_PORT convention every
# other `tools/` script and ps-service itself reads (see .env.example).
DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))

# Deliberately hardcoded, not discovered via `db.list_graphs()` -- same reasoning as
# `check_baseline_preconditions.py`'s own `_CURRENTLY_MAPPED_SHORT_NAMES`.
_PRECONDITION_CHECK_SHORT_NAMES = ("CRA", "GDPR", "NIS2")

# Fixed run order: CRA and NIS2 first (refreshing already-merged data), GDPR last as the
# genuinely new merge, so a problem surfaces on the lower-risk idempotent case first.
_RUN_ORDER: tuple[tuple[str, str], ...] = (
    ("CRA", "CRA-1.0"),
    ("NIS2", "NIS2-1.0"),
    ("GDPR", "GDPR-1.0"),
)

_EXPECTED_SCOPE = ("CRA-1.0", "GDPR-1.0", "NIS2-1.0")
_EXPECTED_TARGET_GRAPH = "policy_system"

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

_MERGE_FAILURE_EXIT_CODE = 4

# The five node labels / relationship types the live merge touches. Own copy of
# `tests/company_merge/_live_merge_assertions.py`'s query shapes: this is production code,
# which cannot import a test-only module, so the query text is deliberately duplicated
# (the repo's "own copy of an established shape, not a shared import" convention, as in
# `company_merge/falkordb_client.py` re: `domain_mapper.falkordb_client`).
_NODE_LABELS = ("RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability")
_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES")
_PROPERTY_BEARING_EDGE_TYPES = ("DEFINES", "EXPRESSES")
_PROPERTYLESS_EDGE_TYPES = ("HAS", "SATISFIED_BY", "REQUIRES")

if TYPE_CHECKING:
    MergeFn = Callable[..., MergeResult]
    ClockFn = Callable[[], datetime]
    GraphProvider = Callable[[str], GraphHandle]
    WriteAccessBuilder = Callable[[ServiceConfig], tuple[GroupSubmitter, SystemAuditRecorder]]

_AUDIT_TRIGGER = "operator_cli"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--approval-file",
        type=Path,
        required=True,
        help="Path to a human-authored approval record (never written by this script)",
    )
    parser.add_argument(
        "--run-kind",
        choices=["initial", "rerun"],
        required=True,
        help="Selects which hardcoded confirmation phrase the approval file must carry "
        "(ps_service.company_merge.live_merge_approval) -- never a free-form phrase argument",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=None,
        help="Where the JSON before/after report is written (default: a UTC-timestamped "
        "file in the tool's built-in report directory)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="FalkorDB host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="FalkorDB port")
    return parser.parse_args(argv)


def _default_report_path(run_timestamp: str) -> Path:
    """Return the built-in report location for a run started at `run_timestamp`."""
    repo_root = Path(__file__).resolve().parents[2]
    return (
        repo_root
        / ".orchestrator"
        / "tracker"
        / "issue-28-live-merge-verification"
        / f"live_merge_report_{run_timestamp}.json"
    )


def _default_graph_provider(host: str, port: int) -> GraphProvider:
    """Real connection: one FalkorDB client, one `select_graph` per graph name.

    Lazy -- `connect()` does not itself round-trip to FalkorDB (see its own
    docstring); the first real network call this script makes is the
    precondition check's own read, deliberately AFTER the approval gate.
    """
    db = connect(host, port)

    def provider(graph_name: str) -> GraphHandle:
        return select_graph(db, graph_name)

    return provider


def _default_write_access(config: ServiceConfig) -> tuple[GroupSubmitter, SystemAuditRecorder]:
    """Build the Graph Write Gateway and the system audit recorder from `config`.

    Contacts neither store. Raises if the PS state database is not configured: there is no
    direct-write fallback.
    """
    if config.state_postgres_host is None:
        message = "the PS state database is not configured (PS_STATE_POSTGRES_HOST is unset)"
        raise RuntimeError(message)
    gateway = cast("GroupSubmitter", build_default_graph_write_gateway(config))
    return gateway, build_system_audit_recorder(config)


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _count(graph: GraphHandle, query: str) -> int:
    """A count query's value; a graph key that does not exist yet answers with no rows: zero."""
    rows = _query_rows(graph, query)
    return cast("int", rows[0][0]) if rows else 0


def _snapshot_graph(graph: GraphHandle) -> dict[str, object]:
    """Capture node/edge counts, id-sets, properties, and edge existence-pairs.

    Covers every node label and relationship type the live merge touches.

    `embedding` is stripped from a Capability node's captured properties at capture time
    (not comparison time): a semantic match's embedding can legitimately be backfilled
    between the before and after snapshots without that being a genuine before/after
    integrity violation.
    """
    node_counts = {
        label: _count(graph, f"MATCH (n:{label}) RETURN count(n)") for label in _NODE_LABELS
    }
    edge_counts = {
        rel: _count(graph, f"MATCH ()-[r:{rel}]->() RETURN count(r)") for rel in _EDGE_TYPES
    }

    node_properties: dict[str, dict[str, dict[str, object]]] = {}
    node_ids: dict[str, list[str]] = {}
    for label in _NODE_LABELS:
        by_id: dict[str, dict[str, object]] = {}
        for node_id, props in _query_rows(
            graph, f"MATCH (n:{label}) RETURN n.id AS id, properties(n) AS props"
        ):
            properties = dict(cast("dict[str, object]", props))
            if label == "Capability":
                properties.pop("embedding", None)
            by_id[cast("str", node_id)] = properties
        node_properties[label] = by_id
        node_ids[label] = sorted(by_id)

    edge_properties: dict[str, dict[str, dict[str, object]]] = {}
    for relationship_type in _PROPERTY_BEARING_EDGE_TYPES:
        by_endpoint_pair: dict[str, dict[str, object]] = {}
        for source_id, target_id, props in _query_rows(
            graph, f"MATCH (a)-[e:{relationship_type}]->(b) RETURN a.id, b.id, properties(e)"
        ):
            key = f"{cast('str', source_id)}|{cast('str', target_id)}"
            by_endpoint_pair[key] = dict(cast("dict[str, object]", props))
        edge_properties[relationship_type] = by_endpoint_pair

    edge_ids: dict[str, list[list[str]]] = {}
    for relationship_type in _PROPERTYLESS_EDGE_TYPES:
        pairs: list[list[str]] = []
        for source_id, target_id in _query_rows(
            graph, f"MATCH (a)-[:{relationship_type}]->(b) RETURN a.id, b.id"
        ):
            pairs.append([cast("str", source_id), cast("str", target_id)])
        edge_ids[relationship_type] = pairs

    return {
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "node_ids": node_ids,
        "node_properties": node_properties,
        "edge_properties": edge_properties,
        "edge_ids": edge_ids,
    }


def _run_precondition_check(provider: GraphProvider) -> dict[str, tuple[object, ...]]:
    """Return every currently-mapped baseline graph's HAS-edge violations, by graph name.

    Empty dict means the invariant holds everywhere.
    """
    violations_by_graph: dict[str, tuple[object, ...]] = {}
    for short_name in _PRECONDITION_CHECK_SHORT_NAMES:
        graph_name = baseline_graph_name(short_name)
        baseline_graph = provider(graph_name)
        violations = check_obligation_has_edge_cardinality(baseline_graph)
        if violations:
            violations_by_graph[graph_name] = violations
    return violations_by_graph


def _check_approval_gate(args: argparse.Namespace) -> int | None:
    """Gate 1 (approval), checked first -- nothing else has run yet. `None` means proceed."""
    try:
        load_and_validate_approval(
            args.approval_file,
            expected_scope=_EXPECTED_SCOPE,
            expected_target_graph=_EXPECTED_TARGET_GRAPH,
            run_kind=cast('Literal["initial", "rerun"]', args.run_kind),
        )
    except LiveMergeApprovalError as exc:
        print(f"Approval gate failed -- live merge will not proceed: {exc}", file=sys.stderr)
        return 2
    return None


def _check_precondition_gate(provider: GraphProvider, *, host: str, port: int) -> int | None:
    """Gate 2 (precondition) -- the first real graph read this script makes.

    Deliberately after the approval gate. `None` means proceed.
    """
    try:
        violations_by_graph = _run_precondition_check(provider)
    except Exception as exc:  # noqa: BLE001 -- connectivity/query guard: friendly message, no traceback
        print(
            f"FalkorDB connection/query failed at {host}:{port} while checking preconditions. "
            f"Is FalkorDB running? Error: {exc}",
            file=sys.stderr,
        )
        return 1

    if violations_by_graph:
        print(
            "AC-BI-003: precondition violation found -- the live merge will NOT proceed:",
            file=sys.stderr,
        )
        for graph_name, violations in violations_by_graph.items():
            print(f"  {graph_name}: {len(violations)} violation(s)", file=sys.stderr)
            for violation in violations:
                print(f"    {violation!r}", file=sys.stderr)
        return 3
    return None


@dataclass(frozen=True, slots=True)
class _MergeSettings:
    """The resolved service config and the merge values drawn from it."""

    config: ServiceConfig
    merge_threshold: float
    review_threshold: float
    role_review_threshold: float
    embed_model: str


def _resolve_merge_config() -> _MergeSettings | int:
    """Fail-closed config resolution, before any write. An `int` return is the exit code."""
    config = load_config()
    if config.company_merge_capability_merge_threshold is None:
        print(
            "PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD is not set -- resolve it via ServiceConfig "
            "before running a live merge.",
            file=sys.stderr,
        )
        return 1
    if config.company_merge_capability_review_threshold is None:
        print(
            "PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD is not set -- resolve it via "
            "ServiceConfig before running a live merge.",
            file=sys.stderr,
        )
        return 1
    if config.company_merge_role_review_threshold is None:
        print(
            "PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD is not set -- resolve it via "
            "ServiceConfig before running a live merge.",
            file=sys.stderr,
        )
        return 1
    if config.llm_interface_embed_model is None:
        print(
            "PS_LLMINTERFACE_EMBED_MODEL is not set -- resolve it via ServiceConfig before "
            "running a live merge.",
            file=sys.stderr,
        )
        return 1
    return _MergeSettings(
        config=config,
        merge_threshold=config.company_merge_capability_merge_threshold,
        review_threshold=config.company_merge_capability_review_threshold,
        role_review_threshold=config.company_merge_role_review_threshold,
        embed_model=config.llm_interface_embed_model,
    )


def _run_all_regulations(
    *,
    single_tenant_graph: GraphHandle,
    baseline_graphs: dict[str, GraphHandle],
    settings: _MergeSettings,
    emitter: LogEmitter,
    merge_fn: MergeFn,
    run_timestamp: str,
    submitter: GroupSubmitter,
    system_audit: SystemAuditRecorder,
) -> tuple[list[str], list[str], list[dict[str, object]]]:
    """Call `merge_fn` for CRA-1.0, NIS2-1.0, GDPR-1.0 in order, stopping on the first raise.

    Each regulation first records its own system audit row (resource = its run id); the row's id
    anchors every group the merge submits. If the row cannot be written the regulation is
    recorded as failed and nothing is merged.

    Returns `(run_ids, regulations_attempted, errors)`.
    """
    run_ids: list[str] = []
    regulations_attempted: list[str] = []
    errors: list[dict[str, object]] = []

    for short_name, regulatory_instrument_id in _RUN_ORDER:
        run_id = f"issue28-live-merge-{short_name.lower()}-{run_timestamp}"
        run_ids.append(run_id)
        regulations_attempted.append(regulatory_instrument_id)
        try:
            audit_event_id = system_audit(
                action=INGESTION_RUN_SUBMIT_ACTION,
                resource_type=INGESTION_RUN_RESOURCE_TYPE,
                resource_id=run_id,
                details={
                    "status": "started",
                    "trigger": _AUDIT_TRIGGER,
                    "short_name": short_name,
                },
            )
            with bind_run_context(run_id):
                merge_fn(
                    regulatory_instrument_id,
                    baseline_graph=baseline_graphs[short_name],
                    single_tenant_graph=single_tenant_graph,
                    submitter=submitter,
                    single_tenant_graph_name=single_tenant_graph_name(),
                    baseline_graph_name=baseline_graph_name(short_name),
                    audit_event_id=audit_event_id,
                    embed_model=settings.embed_model,
                    merge_threshold=settings.merge_threshold,
                    review_threshold=settings.review_threshold,
                    role_review_threshold=settings.role_review_threshold,
                    emitter=emitter,
                )
        except Exception as exc:  # noqa: BLE001 -- record and stop, never continue past a failed regulation
            errors.append(
                {
                    "regulatory_instrument_id": regulatory_instrument_id,
                    "run_id": run_id,
                    "error": str(exc),
                }
            )
            break

    return run_ids, regulations_attempted, errors


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """Bundles `_run_all_regulations`'s output with the before/after snapshots.

    Kept as one object so `_write_report` stays within L1's argument-count budget.
    """

    run_ids: list[str]
    regulations_attempted: list[str]
    errors: list[dict[str, object]]
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]
    started_at: str
    finished_at: str


def _write_report(args: argparse.Namespace, run_timestamp: str, outcome: _RunOutcome) -> Path:
    """Write the JSON before/after report and return its path (`--report-path` or the default)."""
    report = {
        "approval_file": str(args.approval_file),
        "run_kind": args.run_kind,
        "regulations_attempted": outcome.regulations_attempted,
        "run_ids": outcome.run_ids,
        "before": outcome.before_snapshot,
        "after": outcome.after_snapshot,
        "log_file_path": str(resolve_default_log_path()),
        "errors": outcome.errors,
        "started_at": outcome.started_at,
        "finished_at": outcome.finished_at,
    }
    report_path = (
        args.report_path if args.report_path is not None else _default_report_path(run_timestamp)
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report_path


def main(
    argv: Sequence[str] | None = None,
    *,
    graph_provider: GraphProvider | None = None,
    merge_fn: MergeFn = merge_baseline_graph,
    clock: ClockFn | None = None,
    write_access: WriteAccessBuilder = _default_write_access,
) -> int:
    """Run the approval-gated, precondition-gated live merge for CRA, NIS2 and GDPR.

    Args:
        argv: Command-line arguments (`--approval-file` and `--run-kind` are required;
            see `--help`); `None` reads `sys.argv`.
        graph_provider: Maps a graph name to a FalkorDB graph handle. Test-only seam (no
            CLI flag exposes it); `None` connects to FalkorDB at `--host`/`--port`.
        merge_fn: The merge to run per regulation; the default is the real
            `merge_baseline_graph`. Test-only seam.
        clock: Returns the current time; the default is the UTC wall clock. Test-only
            seam.
        write_access: Builds the `(submitter, audit recorder)` pair from the service
            configuration; the default builds the real Graph Write Gateway and
            recorder. Test-only seam.

    Returns:
        Process exit code: `0` on full success; `1` on a FalkorDB or query failure during
        the precondition check, an unset `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD`,
        `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD` or `PS_LLMINTERFACE_EMBED_MODEL`, or a
        gateway that cannot be built; `2` on an approval gate failure; `3` on a precondition
        violation; `4` if `merge_fn` or the per-regulation audit row raises for any regulation
        partway through the run.

    Raises:
        SystemExit: Raised by argparse on invalid arguments or `--help`.
        OSError: If the approval file cannot be renamed after a successful run, or the
            report cannot be written.

    Side Effects:
        Runs `merge_baseline_graph` against the live graph through the gateway, records
        one audit row per regulation, writes the JSON before/after report (creating its
        parent directories), and on full success renames the approval file with a
        `.consumed-<timestamp>` suffix. Prints the outcome to stdout, or failures to
        stderr.
    """
    args = _parse_args(argv)
    clock_fn: ClockFn = clock if clock is not None else lambda: datetime.now(UTC)
    run_timestamp = clock_fn().strftime(_TIMESTAMP_FORMAT)

    provider = (
        graph_provider
        if graph_provider is not None
        else _default_graph_provider(args.host, args.port)
    )

    approval_exit_code = _check_approval_gate(args)
    if approval_exit_code is not None:
        return approval_exit_code

    precondition_exit_code = _check_precondition_gate(provider, host=args.host, port=args.port)
    if precondition_exit_code is not None:
        return precondition_exit_code

    resolved_config = _resolve_merge_config()
    if isinstance(resolved_config, int):
        return resolved_config

    try:
        submitter, system_audit = write_access(resolved_config.config)
    except Exception as exc:  # noqa: BLE001 -- fail closed: no gateway, no write; friendly message, no traceback
        print(
            f"The Graph Write Gateway could not be built -- live merge will not proceed: {exc}",
            file=sys.stderr,
        )
        return 1

    single_tenant_graph = provider(single_tenant_graph_name())
    baseline_graphs = {
        short_name: provider(baseline_graph_name(short_name))
        for short_name, _regulatory_instrument_id in _RUN_ORDER
    }

    before_snapshot = _snapshot_graph(single_tenant_graph)
    emitter: LogEmitter = configure_logging()
    started_at = clock_fn().isoformat()

    run_ids, regulations_attempted, errors = _run_all_regulations(
        single_tenant_graph=single_tenant_graph,
        baseline_graphs=baseline_graphs,
        settings=resolved_config,
        emitter=emitter,
        merge_fn=merge_fn,
        run_timestamp=run_timestamp,
        submitter=submitter,
        system_audit=system_audit,
    )

    after_snapshot = _snapshot_graph(single_tenant_graph)
    finished_at = clock_fn().isoformat()

    outcome = _RunOutcome(
        run_ids=run_ids,
        regulations_attempted=regulations_attempted,
        errors=errors,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        started_at=started_at,
        finished_at=finished_at,
    )
    report_path = _write_report(args, run_timestamp, outcome)

    if errors:
        print(
            f"Live merge FAILED partway through -- see {report_path} for details.",
            file=sys.stderr,
        )
        return _MERGE_FAILURE_EXIT_CODE

    consumed_path = args.approval_file.with_name(
        f"{args.approval_file.name}.consumed-{run_timestamp}"
    )
    args.approval_file.rename(consumed_path)

    print("Live merge succeeded.")
    print(f"  regulations: {', '.join(regulations_attempted)}")
    print(f"  run_ids:     {', '.join(run_ids)}")
    print(f"  report:      {report_path}")
    print(f"  approval file consumed as: {consumed_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
