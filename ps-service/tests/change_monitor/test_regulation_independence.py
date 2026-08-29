"""AC-011 — the regulation-independence AST scan, extended to cover the
`ps_service.change_monitor` package (PLAN_REVIEWED.md §1.6, §3 test 20).

Uses the shared `scan_package_for_forbidden_conditionals` helper — the same
one `tests/ingestion/test_pipeline.py` runs over the ingestion package — so
there is exactly one scan implementation exercised over both packages. The
second test is an anti-narrowing guard: it asserts the scanner's
`rglob('*.py')` walk actually reaches every `change_monitor/*.py` module,
so a future module cannot carry a regulation-name conditional the scan
never looks at.
"""

from __future__ import annotations

from pathlib import Path

import ps_service.change_monitor as change_monitor_package
from change_monitor._regulation_independence import (
    FORBIDDEN_LITERALS,
    scan_package_for_forbidden_conditionals,
)

_EXPECTED_MODULES = {
    "__init__.py",
    "errors.py",
    "models.py",
    "cellar_consolidated.py",
    "falkordb_client.py",
    "graph_reader.py",
    "succession.py",
    "poll.py",
    "trigger.py",
}


def test_no_forbidden_literal_conditionals_in_change_monitor_package() -> None:
    violations = scan_package_for_forbidden_conditionals(change_monitor_package)
    assert not violations, (
        "forbidden-literal conditional(s) found in change_monitor: "
        f"{[(str(path), node.value, node.lineno) for path, node in violations]}"
    )


def test_scan_covers_every_change_monitor_module() -> None:
    """Anti-narrowing: the scanner's `rglob('*.py')` walk must reach every
    module the package ships (listed explicitly), and the forbidden set it
    keys on must still name every literal AC-011 forbids.
    """
    package_file = change_monitor_package.__file__
    assert package_file is not None
    root = Path(package_file).parent

    scanned_relative_paths = {str(path.relative_to(root)) for path in root.rglob("*.py")}
    assert scanned_relative_paths == _EXPECTED_MODULES

    assert (
        frozenset({"CRA", "GDPR", "NIS2", "32024R2847", "32016R0679", "32022L2555"})
        == FORBIDDEN_LITERALS
    )
