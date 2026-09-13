"""Fake-wiring proof for `tools/company-merge/check_baseline_preconditions.py`
(issue #28, PLAN.md §2.2).

Mirrors `test_export_instrument_cli.py`'s `importlib.util.spec_from_file_location`
pattern to load a non-package `tools/` script by path. Injects three fake
`GraphHandle`s via the CLI's `graph_provider` constructor-injection seam
(CHANGES.md #7's corrected description of PLAN.md §2.2 -- a true DI seam, never
`monkeypatch.setattr` on a `FalkorDB` class) -- no real FalkorDB connection
anywhere in this file. The real end-to-end proof (real FalkorDB, real baseline
graphs) is `test_check_baseline_preconditions_cli_live.py`'s `falkordb_live`
test.

`tools/` is outside pytest's `testpaths` (root `pyproject.toml`) and carries no
`__init__.py` (a standalone script, not a package member) -- the CLI module is
loaded by file path via `importlib.util`, the standard way to import a
non-package module by location.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "company-merge"
    / "check_baseline_preconditions.py"
)
_MODULE_NAME = "_check_baseline_preconditions_cli_under_test"


def _load_cli_module() -> ModuleType:
    """Import the CLI shim fresh, by file path -- see module docstring.

    Registered into `sys.modules` under its spec name before `exec_module`
    runs: the CLI defines a `from __future__ import annotations`-style
    frozen/slotted dataclass (`_BaselineReport`, mirroring
    `preconditions.ObligationHasEdgeViolation`'s own shape), and
    `dataclasses`' own field/`ClassVar` resolution looks the defining module
    up via `sys.modules[cls.__module__]` -- without this line that lookup
    returns `None` and dataclass construction raises `AttributeError`, purely
    an artifact of loading a standalone script outside the normal import
    machinery, not a defect in the CLI's own logic.
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MODULE_NAME]
        raise
    return module


@pytest.fixture
def cli_module() -> ModuleType:
    return _load_cli_module()


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    """Satisfies `GraphHandle` structurally.

    Returns the same scripted row set for every `.query()` call it receives --
    the CLI issues the identical exhaustive `MATCH (o:Obligation) OPTIONAL
    MATCH (:Role)-[h:HAS]->(o) RETURN o.id, count(h)` query twice per graph
    (once to derive the total obligation count, once inside
    `check_obligation_has_edge_cardinality` for violations), so no dispatch by
    query text is needed here.
    """

    query_texts: ClassVar[list[str]] = []

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        self.query_texts.append(q)
        return _FakeQueryResult(self._rows)


_CLEAN_ROWS: list[object] = [["obligation-1", 1], ["obligation-2", 1]]
_VIOLATING_ROWS: list[object] = [["obligation-1", 1], ["obligation-2", 0], ["obligation-3", 2]]


def _fake_provider(graphs: dict[str, _FakeGraph], cli_module: ModuleType) -> object:
    def provider(short_name: str) -> _FakeGraph:
        return graphs[cli_module.baseline_graph_name(short_name)]

    return provider


def test_exits_zero_when_all_three_baseline_graphs_are_clean(cli_module: ModuleType) -> None:
    graphs = {
        "cra_baseline": _FakeGraph(_CLEAN_ROWS),
        "gdpr_baseline": _FakeGraph(_CLEAN_ROWS),
        "nis2_baseline": _FakeGraph(_CLEAN_ROWS),
    }

    exit_code = cli_module.main([], graph_provider=_fake_provider(graphs, cli_module))

    assert exit_code == 0


def test_exits_one_and_prints_which_graph_obligation_and_count_on_a_violation(
    cli_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    graphs = {
        "cra_baseline": _FakeGraph(_CLEAN_ROWS),
        "gdpr_baseline": _FakeGraph(_CLEAN_ROWS),
        "nis2_baseline": _FakeGraph(_VIOLATING_ROWS),
    }

    exit_code = cli_module.main([], graph_provider=_fake_provider(graphs, cli_module))

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "nis2_baseline" in out
    assert "obligation-2" in out
    assert "obligation-3" in out
    # The two violating counts (0 and 2) must both be visible in the report.
    assert "0" in out
    assert "2" in out
    # The two clean graphs must still be reported as GO.
    assert "cra_baseline" in out
    assert "gdpr_baseline" in out


def test_exits_one_with_a_friendly_message_and_no_traceback_on_a_connection_failure(
    cli_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raising_provider(short_name: str) -> _FakeGraph:
        del short_name
        message = "connection refused"
        raise ConnectionError(message)

    exit_code = cli_module.main([], graph_provider=_raising_provider)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "connection failed" in err.lower()
    assert "traceback" not in err.lower()


def test_json_format_matches_the_documented_shape(
    cli_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    graphs = {
        "cra_baseline": _FakeGraph(_CLEAN_ROWS),
        "gdpr_baseline": _FakeGraph(_CLEAN_ROWS),
        "nis2_baseline": _FakeGraph(_VIOLATING_ROWS),
    }

    exit_code = cli_module.main(
        ["--format", "json"], graph_provider=_fake_provider(graphs, cli_module)
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"cra_baseline", "gdpr_baseline", "nis2_baseline"}
    assert payload["cra_baseline"] == {"obligation_count": 2, "violations": []}
    assert payload["gdpr_baseline"] == {"obligation_count": 2, "violations": []}
    assert payload["nis2_baseline"] == {
        "obligation_count": 3,
        "violations": [
            {"obligation_id": "obligation-2", "has_edge_count": 0},
            {"obligation_id": "obligation-3", "has_edge_count": 2},
        ],
    }
