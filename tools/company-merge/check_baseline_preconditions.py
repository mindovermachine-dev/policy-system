#!/usr/bin/env python3
r"""Maintainer CLI shim: AC-BI-002/AC-BI-003 baseline precondition check (issue #28).

Thin wrapper over `ps_service.company_merge.preconditions.check_obligation_has_edge_cardinality`
-- the real check (Domain Mapper's #15 structural guarantee, reconfirmed as
Company Merge's own precondition) lives there; this script only connects to
FalkorDB, selects each currently-mapped regulation's `{short}_baseline` graph,
and prints a human- or machine-readable go/no-go report. Mirrors
`tools/curated-export/export_instrument.py`'s argparse/env-var/connection-guard
shape; unlike that precedent's `monkeypatch.setattr(cli_module, "FalkorDB", ...)`
substitution, this CLI uses a true `graph_provider` constructor-injection seam
(PLAN.md §2.2, corrected by CHANGES.md #7) -- a deliberate improvement, not a
literal mirror.

The list of currently-mapped regulations (`CRA`, `GDPR`, `NIS2`) is a
deliberate, hardcoded enumeration, never discovered via `db.list_graphs()`.
BASELINE.md flagged a stray, differently-cased `CRA_baseline` graph alongside
`cra_baseline` in real FalkorDB; a name-pattern-based discovery
(`name.endswith("_baseline")`) is exactly the kind of mechanism that could
accidentally match it (or any future non-regulation `*_baseline`-named graph).
Hardcoding the three short names and deriving each graph name via
`baseline_graph_name(short_name)` makes the stray `CRA_baseline` graph
structurally unreachable by this tool, by construction -- never queried at
all, not filtered post-hoc.

This tool is strictly read-only reporting: it never calls `merge_baseline_graph`
or any write path. It does not itself gate anything -- `tools/company-merge/
run_live_merge.py` (a separate slice) is what actually enforces AC-BI-003's
block on a live merge; this CLI exists so a human/orchestrator can check the
go/no-go signal independently, at any time.

    uv run tools/company-merge/check_baseline_preconditions.py [--format json]

Requires a running FalkorDB instance with `cra_baseline`/`gdpr_baseline`/
`nis2_baseline` already populated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ps_service.company_merge.falkordb_client import connect, select_graph
from ps_service.company_merge.preconditions import (
    ObligationHasEdgeViolation,
    check_obligation_has_edge_cardinality,
)
from ps_service.domain_mapper.falkordb_client import baseline_graph_name

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ps_service.company_merge.falkordb_client import GraphHandle

# Connection defaults, env-driven -- same PS_FALKORDB_HOST/PS_FALKORDB_PORT convention
# every other `tools/` script and ps-service itself reads (see .env.example).
DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))

# Deliberately hardcoded, not discovered via `db.list_graphs()` -- see module docstring.
_CURRENTLY_MAPPED_SHORT_NAMES = ("CRA", "GDPR", "NIS2")

# Identical, verbatim, to `preconditions._OBLIGATION_HAS_EDGE_CARDINALITY_QUERY` (private to
# that module -- not re-exported). Reused here, unchanged, ONLY to derive `obligation_count` for
# reporting: `check_obligation_has_edge_cardinality` returns violations only, not the total row
# count, and every Obligation produces exactly one row (`OPTIONAL MATCH` never drops a row), so
# `len(rows)` from this exact query equals the total Obligation count. This tool never issues any
# query against FalkorDB other than this one, exhaustive, read-only query.
_OBLIGATION_ROW_QUERY = (
    "MATCH (o:Obligation) OPTIONAL MATCH (:Role)-[h:HAS]->(o) RETURN o.id, count(h)"
)


@dataclass(frozen=True, slots=True)
class _BaselineReport:
    """One `{short}_baseline` graph's precondition-check outcome."""

    obligation_count: int
    violations: tuple[ObligationHasEdgeViolation, ...]

    @property
    def is_go(self) -> bool:
        """True when this graph has zero HAS-edge-cardinality violations."""
        return len(self.violations) == 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="FalkorDB host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="FalkorDB port")
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Report format: human-readable 'text' (default) or machine-readable 'json'",
    )
    return parser.parse_args(argv)


def _default_graph_provider(host: str, port: int) -> Callable[[str], GraphHandle]:
    """Real connection: one FalkorDB client, one `select_graph` per short name."""
    db = connect(host, port)

    def provider(short_name: str) -> GraphHandle:
        return select_graph(db, baseline_graph_name(short_name))

    return provider


def _check_one_graph(graph: GraphHandle) -> _BaselineReport:
    row_result = graph.query(_OBLIGATION_ROW_QUERY)
    rows = cast("list[list[object]]", row_result.result_set)
    obligation_count = len(rows)
    violations = check_obligation_has_edge_cardinality(graph)
    return _BaselineReport(obligation_count=obligation_count, violations=violations)


def _print_text_report(reports: dict[str, _BaselineReport]) -> None:
    for graph_name, report in reports.items():
        status = "GO" if report.is_go else "NO-GO"
        print(
            f"{graph_name}: {report.obligation_count} Obligations, "
            f"{len(report.violations)} violations -- {status}"
        )
        for violation in report.violations:
            print(
                f"  VIOLATION: Obligation '{violation.obligation_id}' has "
                f"{violation.has_edge_count} HAS edge(s) (expected exactly 1)"
            )


def _print_json_report(reports: dict[str, _BaselineReport]) -> None:
    payload = {
        graph_name: {
            "obligation_count": report.obligation_count,
            "violations": [
                {"obligation_id": v.obligation_id, "has_edge_count": v.has_edge_count}
                for v in report.violations
            ],
        }
        for graph_name, report in reports.items()
    }
    print(json.dumps(payload))


def main(
    argv: Sequence[str] | None = None,
    *,
    graph_provider: Callable[[str], GraphHandle] | None = None,
) -> int:
    """Check AC-BI-002/AC-BI-003's precondition against all three baseline graphs.

    `graph_provider` is a test-only injection seam (no CLI flag exposes it --
    there is no way to serialize a fake Python callable through argv): left
    `None`, a real invocation connects to real FalkorDB via `--host`/`--port`.
    Returns a process exit code: `0` only if all three currently-mapped
    baseline graphs report zero violations; `1` if any graph reports at least
    one violation, or if connecting to / querying FalkorDB fails.
    """
    args = _parse_args(argv)
    provider = (
        graph_provider
        if graph_provider is not None
        else _default_graph_provider(args.host, args.port)
    )

    reports: dict[str, _BaselineReport] = {}
    for short_name in _CURRENTLY_MAPPED_SHORT_NAMES:
        graph_name = baseline_graph_name(short_name)
        try:
            graph = provider(short_name)
            reports[graph_name] = _check_one_graph(graph)
        except Exception as exc:  # noqa: BLE001 -- top-level connection/query guard: friendly message, no traceback
            print(
                f"FalkorDB connection failed at {args.host}:{args.port} while checking "
                f"'{graph_name}'. Is FalkorDB running? Error: {exc}",
                file=sys.stderr,
            )
            return 1

    if args.format == "json":
        _print_json_report(reports)
    else:
        _print_text_report(reports)

    any_violation = any(not report.is_go for report in reports.values())
    return 1 if any_violation else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
