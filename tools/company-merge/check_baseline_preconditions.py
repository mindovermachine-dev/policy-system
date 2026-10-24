#!/usr/bin/env python3
"""Report whether each baseline graph satisfies Company Merge's HAS-edge precondition.

Connects to FalkorDB, opens the `cra_baseline`, `gdpr_baseline` and `nis2_baseline`
graphs and checks that every Obligation has exactly one incoming HAS edge from a Role
(the check is `check_obligation_has_edge_cardinality` in
`ps_service.company_merge.preconditions`). For each graph it prints the Obligation
count, the number of violations and a GO or NO-GO verdict, followed by one line per
offending Obligation.

The three regulations are a fixed, hardcoded list, never discovered through
`db.list_graphs()`. A name-pattern discovery (`name.endswith("_baseline")`) could match
a stray, differently-cased `CRA_baseline` graph or any future non-regulation graph with
a `_baseline` suffix; deriving each name with `baseline_graph_name(short_name)` makes
such graphs unreachable by construction, because they are never queried at all.

This tool is read-only: it issues only one Obligation-listing query plus the precondition
check, and never calls `merge_baseline_graph` or any other write path. It does not gate
anything itself; `tools/company-merge/run_live_merge.py` is the tool that blocks a live
merge on the same precondition. Run this one to read the go/no-go signal at any time.

    uv run tools/company-merge/check_baseline_preconditions.py [--host H] [--port P] [--format json]

Options:
    --host      FalkorDB host (default: env PS_FALKORDB_HOST, else localhost).
    --port      FalkorDB port (default: env PS_FALKORDB_PORT, else 6379).
    --format    `text` (default) for a human-readable report, `json` for one JSON object
                keyed by graph name with `obligation_count` and `violations`.

Exit codes:
    0   All three baseline graphs report zero violations.
    1   At least one graph reports a violation, or FalkorDB cannot be reached or queried
        (a one-line message goes to stderr, no traceback).

Requires a running FalkorDB with the three baseline graphs already populated.
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

# Bounds for the one-off connections these tools open; the service reads the same two
# values from `PS_FALKORDB_CONNECT_TIMEOUT_SECONDS` / `PS_FALKORDB_SOCKET_TIMEOUT_SECONDS`.
_CONNECT_TIMEOUT_SECONDS = 5.0
_SOCKET_TIMEOUT_SECONDS = 30.0

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ps_service.company_merge.falkordb_client import GraphHandle

# Connection defaults, env-driven -- same PS_FALKORDB_HOST/PS_FALKORDB_PORT convention
# every other `tools/` script and ps-service itself reads (see .env.example).
DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))

# Deliberately hardcoded, not discovered via `db.list_graphs()`: a name-pattern discovery
# could match a stray differently-cased graph.
_CURRENTLY_MAPPED_SHORT_NAMES = ("CRA", "GDPR", "NIS2")

# Same query as `preconditions._OBLIGATION_HAS_EDGE_CARDINALITY_QUERY` (private to that
# module, not re-exported). Used only to derive `obligation_count` for reporting:
# `check_obligation_has_edge_cardinality` returns violations only, not the total row
# count, and every Obligation produces exactly one row (`OPTIONAL MATCH` never drops a
# row), so `len(rows)` from this query equals the total Obligation count. This tool
# issues no other query against FalkorDB.
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
    db = connect(
        host,
        port,
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )

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
    """Check the HAS-edge precondition on all three baseline graphs and print a report.

    Args:
        argv: Command-line arguments (`--host`, `--port`, `--format`); `None` reads
            `sys.argv`. `--format` must be `text` or `json`; anything else makes
            argparse exit with status 2.
        graph_provider: Maps a short name (`CRA`, `GDPR`, `NIS2`) to its baseline graph
            handle. Test-only seam (no CLI flag exposes it, as a callable cannot pass
            through argv); `None` connects to the real FalkorDB at `--host`/`--port`.

    Returns:
        Process exit code: `0` only if every baseline graph has zero violations; `1` if
        any graph has at least one violation or if connecting to or querying FalkorDB
        fails.

    Raises:
        SystemExit: Raised by argparse on invalid arguments or `--help`.

    Side Effects:
        Opens a FalkorDB connection and runs read-only queries. Writes the report to
        stdout, or the failure message to stderr; never writes to any graph.
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
