"""Tests for `tools/backup-restore-verify/graph_digests.py` (issue #216, slice V3).

The tool is loaded by file path (`tools/` is not a package). Its only seam is the FalkorDB
driver connection (`open_read_only_database(connect=...)`): the fake driver below answers the
digest scans from the gateway rig's in-memory graphs, so every digest asserted here is the real
`canonical_digest` of real graph content.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import GatewayRig, InMemoryGraph

from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_TOOL_PATH = (
    Path(__file__).resolve().parents[3] / "tools" / "backup-restore-verify" / "graph_digests.py"
)
_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_graph_digests_under_test", _TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool() -> ModuleType:
    return _load_tool()


@pytest.fixture
def rig() -> Iterator[GatewayRig]:
    gateway_rig = GatewayRig()
    try:
        yield gateway_rig
    finally:
        gateway_rig.close()


class _DriverResult:
    def __init__(self, result_set: list[object], header: list[list[object]]) -> None:
        self.result_set = result_set
        self.header = header


class _DriverGraph:
    def __init__(self, graph: InMemoryGraph, log: list[str]) -> None:
        self._graph = graph
        self._log = log

    def ro_query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _DriverResult:
        del timeout
        self._log.append(q)
        result = self._graph.query(q, params)
        return _DriverResult(result.result_set, [])


class _Driver:
    def __init__(self, rig: GatewayRig) -> None:
        self._rig = rig
        self.log: list[str] = []
        self.connected: list[tuple[str, int]] = []

    def connect(self, host: str, port: int) -> _Driver:
        self.connected.append((host, port))
        return self

    def select_graph(self, graph_id: str, /) -> _DriverGraph:
        return _DriverGraph(self._rig.graphs.open(graph_id), self.log)

    def list_graphs(self) -> list[str]:
        return sorted(self._rig.graphs.graphs)


def _write(rig: GatewayRig, graph: str, *names: str) -> None:
    rig.gateway.submit_group(
        MutationGroup(
            graph=graph,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=tuple(
                UpsertNode(label="Policy", id=f"{graph}-{name}", properties={"name": name})
                for name in names
            ),
        )
    )


def test_snapshot_digests_each_named_graph_like_canonical_digest(
    tool: ModuleType, rig: GatewayRig
) -> None:
    _write(rig, "g1", "a", "b")
    _write(rig, "g2", "c")
    driver = _Driver(rig)
    database = tool.open_database("falkordb.invalid", 6379, connect=driver.connect)

    snapshot = tool.snapshot_digests(database, ["g1", "g2"])

    assert snapshot == {
        "g1": canonical_digest(rig.graphs.open("g1")),
        "g2": canonical_digest(rig.graphs.open("g2")),
    }
    assert snapshot["g1"] != snapshot["g2"]
    assert driver.connected == [("falkordb.invalid", 6379)]


def test_snapshot_reads_through_read_only_queries_only(tool: ModuleType, rig: GatewayRig) -> None:
    _write(rig, "g1", "a")
    driver = _Driver(rig)

    tool.snapshot_digests(tool.open_database("h", 1, connect=driver.connect), ["g1"])

    assert driver.log
    assert all("CREATE" not in q.upper() and "DELETE" not in q.upper() for q in driver.log)


def test_snapshot_of_an_absent_graph_is_the_empty_graph_digest(
    tool: ModuleType, rig: GatewayRig
) -> None:
    driver = _Driver(rig)
    database = tool.open_database("h", 1, connect=driver.connect)

    snapshot = tool.snapshot_digests(database, ["nothing"])

    assert snapshot == {"nothing": canonical_digest(rig.graphs.open("nothing"))}


def test_compare_of_identical_snapshots_reports_no_difference(tool: ModuleType) -> None:
    snapshot = {"g1": "sha256:aa", "g2": "sha256:bb"}

    assert tool.compare_snapshots(snapshot, dict(snapshot)) == []


def test_compare_names_a_changed_and_a_missing_graph(tool: ModuleType) -> None:
    before = {"g1": "sha256:aa", "g2": "sha256:bb", "g3": "sha256:cc"}
    after = {"g1": "sha256:aa", "g2": "sha256:xx", "g4": "sha256:dd"}

    differences = tool.compare_snapshots(before, after)

    text = "\n".join(differences)
    assert "g2" in text
    assert "g3" in text
    assert "g4" in text
    assert "g1" not in text


def test_cli_snapshot_writes_json_then_compare_exits_zero_for_equal_files(
    tool: ModuleType, rig: GatewayRig, tmp_path: Path
) -> None:
    _write(rig, "g1", "a")
    driver = _Driver(rig)
    first, second = tmp_path / "a.json", tmp_path / "b.json"

    for target in (first, second):
        code = tool.main(
            ["snapshot", "--graph", "g1", "--out", str(target), "--host", "h", "--port", "1"],
            connect=driver.connect,
        )
        assert code == 0

    assert json.loads(first.read_text()) == {"g1": canonical_digest(rig.graphs.open("g1"))}
    assert tool.main(["compare", str(first), str(second)]) == 0


def test_cli_compare_exits_one_and_names_the_graph_when_content_changed(
    tool: ModuleType,
    rig: GatewayRig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(rig, "g1", "a")
    driver = _Driver(rig)
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    args = ["--graph", "g1", "--host", "h", "--port", "1"]
    assert tool.main(["snapshot", *args, "--out", str(first)], connect=driver.connect) == 0
    _write(rig, "g1", "extra")
    assert tool.main(["snapshot", *args, "--out", str(second)], connect=driver.connect) == 0

    code = tool.main(["compare", str(first), str(second)])

    assert code == 1
    assert "g1" in capsys.readouterr().err


def test_cli_snapshot_requires_a_graph(tool: ModuleType, tmp_path: Path) -> None:
    code = tool.main(["snapshot", "--out", str(tmp_path / "x.json")])

    assert code == 1
