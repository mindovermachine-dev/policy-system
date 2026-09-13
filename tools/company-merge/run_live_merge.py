#!/usr/bin/env python3
r"""Maintainer CLI: execute the live `MergeBaselineGraph` run into `policy_system` (issue #28).

**Slice 5 of issue #28** (PLAN.md §4, amended by CHANGES.md #1/#2/#3): a
re-runnable CLI, not ad-hoc commands. Every collaborator this script needs
beyond argv is dependency-injected on `main()` -- `graph_provider`,
`merge_fn` (defaults to the real `ps_service.company_merge.merge.merge_baseline_graph`),
`clock` -- so the whole orchestration is fully unit-testable with hand-written
fakes, with zero live FalkorDB/LLM dependency
(`ps-service/tests/company_merge/test_run_live_merge_cli.py`).

**This script is the single, approved place a live write into the real,
shared `policy_system` graph is ever issued from in this issue's own
tooling.** It refuses to proceed past two gates before any write:

1. **Approval gate** (AC-BI-001, `ps_service.company_merge.live_merge_approval.
   load_and_validate_approval`): a human-authored approval file, never written
   by this script or any test, must already exist at `--approval-file` and
   validate for the given `--run-kind` (`initial` or `rerun` -- CHANGES.md
   #3: this selects one of two hardcoded confirmation phrases, never a
   free-form argv value). Checked FIRST, before any graph is even selected.
2. **Precondition check** (AC-BI-002/AC-BI-003,
   `ps_service.company_merge.preconditions.check_obligation_has_edge_cardinality`):
   re-run live, immediately before the merge, against all three
   currently-mapped `{short}_baseline` graphs. Any violation on any graph
   blocks the merge entirely -- zero `merge_baseline_graph` calls issued.

Only once both gates pass does this script call `merge_baseline_graph` for
CRA-1.0, then NIS2-1.0, then GDPR-1.0 (in that fixed order -- CRA/NIS2 are
refreshing already-merged data per PLAN.md §0.3's finding, GDPR is the
genuinely new merge), each under its own `run_id`
(`issue28-live-merge-{short}-{timestamp}`), stopping immediately if any call
raises. A before/after snapshot of `policy_system`'s touched labels/
relationship-types (node counts, edge counts, node id-sets, node properties
with `embedding` stripped for Capability, DEFINES/EXPRESSES edge properties,
and HAS/SATISFIED_BY/REQUIRES edge existence-pairs -- CHANGES.md #1) is
written into a JSON report. On full success the approval file is renamed
with a `.consumed-<timestamp>` suffix so it can never be silently reused for
a later invocation; on any failure it is left untouched (a retry against the
same still-relevant approval remains possible).

    uv run tools/company-merge/run_live_merge.py \\
        --approval-file .orchestrator/tracker/issue-28-live-merge-verification/\\
APPROVAL_LIVE_MERGE_INITIAL.md \\
        --run-kind initial

Requires a running FalkorDB instance with `cra_baseline`/`gdpr_baseline`/
`nis2_baseline` and `policy_system` reachable, and
`PS_COMPANYMERGE_SIMILARITY_THRESHOLD`/`PS_LLMINTERFACE_EMBED_MODEL` resolvable
via `ps_service.config.load_config()`.
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
from ps_service.config import load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.logging import bind_run_context
from ps_service.logging.facade import configure as configure_logging
from ps_service.logging.facade import resolve_default_log_path

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Literal

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import MergeResult
    from ps_service.logging import LogEmitter

# Connection defaults, env-driven -- same PS_FALKORDB_HOST/PS_FALKORDB_PORT convention every
# other `tools/` script and ps-service itself reads (see .env.example).
DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))

# Deliberately hardcoded, not discovered via `db.list_graphs()` -- same reasoning as
# `check_baseline_preconditions.py`'s own `_CURRENTLY_MAPPED_SHORT_NAMES`.
_PRECONDITION_CHECK_SHORT_NAMES = ("CRA", "GDPR", "NIS2")

# Fixed run order (PLAN.md §4.1 step 8, as directed for this slice): CRA and NIS2 first --
# refreshing already-merged data per PLAN.md §0.3's finding -- GDPR last as the genuinely new
# merge, so a problem surfaces on the lower-risk idempotent case first.
_RUN_ORDER: tuple[tuple[str, str], ...] = (
    ("CRA", "CRA-1.0"),
    ("NIS2", "NIS2-1.0"),
    ("GDPR", "GDPR-1.0"),
)

_EXPECTED_SCOPE = ("CRA-1.0", "GDPR-1.0", "NIS2-1.0")
_EXPECTED_TARGET_GRAPH = "policy_system"

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

_MERGE_FAILURE_EXIT_CODE = 4

# The five node labels / relationship types this issue's live merge touches (PLAN.md §4.1 steps
# 7/9, CHANGES.md #1). Own copy of `tests/company_merge/_live_merge_assertions.py`'s query
# shapes -- this is PRODUCTION code, which cannot import a test-only module, so the identical
# query text is deliberately duplicated here (mirrors this repo's existing "own copy of an
# established shape, not a shared import" convention, e.g. `company_merge/falkordb_client.py`'s
# own docstring re: `domain_mapper.falkordb_client`).
_NODE_LABELS = ("RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability")
_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES")
_PROPERTY_BEARING_EDGE_TYPES = ("DEFINES", "EXPRESSES")
_PROPERTYLESS_EDGE_TYPES = ("HAS", "SATISFIED_BY", "REQUIRES")

if TYPE_CHECKING:
    MergeFn = Callable[..., MergeResult]
    ClockFn = Callable[[], datetime]
    GraphProvider = Callable[[str], GraphHandle]


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
        help="Where the JSON before/after report is written (default: under this issue's "
        "own tracker directory, named with a UTC timestamp)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="FalkorDB host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="FalkorDB port")
    return parser.parse_args(argv)


def _default_report_path(run_timestamp: str) -> Path:
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


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _count(graph: GraphHandle, query: str) -> int:
    return cast("int", _query_rows(graph, query)[0][0])


def _snapshot_graph(graph: GraphHandle) -> dict[str, object]:
    """Capture node/edge counts, id-sets, properties, and edge existence-pairs.

    For every label/relationship-type this issue's live merge touches (PLAN.md §4.1 steps
    7/9, CHANGES.md #1's extension).

    `embedding` is stripped from a Capability node's captured properties at CAPTURE time
    (not comparison time) -- a semantic match's embedding can legitimately be backfilled
    between the before and after snapshots (AC-BI-005's own already-proven backfill
    mechanism) without that being a genuine before/after integrity violation.
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
    """Return every currently-mapped baseline graph's AC-BI-002 violations, by graph name.

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
    """Gate 1 (AC-BI-001), checked FIRST -- nothing else has run yet. `None` means proceed."""
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
    """Gate 2 (AC-BI-002/AC-BI-003) -- the first real graph read this script makes.

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


def _resolve_merge_config() -> tuple[float, str] | int:
    """Fail-closed config resolution, before any write. An `int` return is the exit code."""
    config = load_config()
    if config.company_merge_similarity_threshold is None:
        print(
            "PS_COMPANYMERGE_SIMILARITY_THRESHOLD is not set -- resolve it via ServiceConfig "
            "before running a live merge.",
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
    return config.company_merge_similarity_threshold, config.llm_interface_embed_model


def _run_all_regulations(
    *,
    provider: GraphProvider,
    single_tenant_graph: GraphHandle,
    baseline_graphs: dict[str, GraphHandle],
    embed_model: str,
    similarity_threshold: float,
    emitter: LogEmitter,
    merge_fn: MergeFn,
    run_timestamp: str,
) -> tuple[list[str], list[str], list[dict[str, object]]]:
    """Call `merge_fn` for CRA-1.0, NIS2-1.0, GDPR-1.0 in order, stopping on the first raise.

    Returns `(run_ids, regulations_attempted, errors)`.
    """
    del provider  # graphs already selected by the caller; kept for signature symmetry/clarity
    run_ids: list[str] = []
    regulations_attempted: list[str] = []
    errors: list[dict[str, object]] = []

    for short_name, regulatory_instrument_id in _RUN_ORDER:
        run_id = f"issue28-live-merge-{short_name.lower()}-{run_timestamp}"
        run_ids.append(run_id)
        regulations_attempted.append(regulatory_instrument_id)
        try:
            with bind_run_context(run_id):
                merge_fn(
                    regulatory_instrument_id,
                    baseline_graph=baseline_graphs[short_name],
                    single_tenant_graph=single_tenant_graph,
                    embed_model=embed_model,
                    similarity_threshold=similarity_threshold,
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
) -> int:
    """Run the approval-gated, precondition-gated live merge for CRA/NIS2/GDPR.

    `graph_provider`/`merge_fn`/`clock` are test-only injection seams (no CLI flag exposes
    them): left at their defaults, a real invocation connects to real FalkorDB and calls the
    real `merge_baseline_graph`. Returns a process exit code: `0` on full success; `1` on a
    FalkorDB/query failure during the precondition check or an unresolved
    `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`/`PS_LLMINTERFACE_EMBED_MODEL`; `2` on an approval
    gate failure; `3` on an AC-BI-002 precondition violation; `4` if `merge_fn` raises for any
    regulation partway through the run.
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
    similarity_threshold, embed_model = resolved_config

    single_tenant_graph = provider(single_tenant_graph_name())
    baseline_graphs = {
        short_name: provider(baseline_graph_name(short_name))
        for short_name, _regulatory_instrument_id in _RUN_ORDER
    }

    before_snapshot = _snapshot_graph(single_tenant_graph)
    emitter: LogEmitter = configure_logging()
    started_at = clock_fn().isoformat()

    run_ids, regulations_attempted, errors = _run_all_regulations(
        provider=provider,
        single_tenant_graph=single_tenant_graph,
        baseline_graphs=baseline_graphs,
        embed_model=embed_model,
        similarity_threshold=similarity_threshold,
        emitter=emitter,
        merge_fn=merge_fn,
        run_timestamp=run_timestamp,
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
