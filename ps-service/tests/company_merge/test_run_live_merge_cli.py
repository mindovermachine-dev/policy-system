"""Fake-wiring proof for `tools/company-merge/run_live_merge.py`.

Every collaborator `main()` needs beyond argv is dependency-injected here (`graph_provider`,
`merge_fn`, `clock`) -- NO live FalkorDB/LLM dependency, no `falkordb_live`/`llm_live` marker.
This suite proves the runner's own orchestration logic correct -- including "zero
merge_fn calls on a precondition violation" and "zero merge_fn calls without a
valid approval" -- entirely without a real approval file. The
real end-to-end wiring proof (real FalkorDB, real config, real logging, only `merge_fn` faked
to raise before any write) is `test_run_live_merge_cli_live.py`'s `falkordb_live` test.

Mirrors `test_export_instrument_cli.py`'s `importlib.util.spec_from_file_location` pattern to
load a non-package `tools/` script by path, and `test_check_baseline_preconditions_cli.py`'s
`sys.modules` pre-registration (needed for the script's own frozen/slotted-dataclass-adjacent
module-level constructs to resolve their defining module during `exec_module`).

No real `APPROVAL_LIVE_MERGE_*.md` file is ever written anywhere in this repo by these tests
-- every approval fixture lives under `tmp_path`, and every `--report-path` passed here also
lives under `tmp_path`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

from ps_service.logging import facade
from ps_service.logging.run_context import current_run_id

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "tools" / "company-merge" / "run_live_merge.py"
_MODULE_NAME = "_run_live_merge_cli_under_test"


@pytest.fixture(autouse=True)
def _preserve_atexit_registered_guard() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """`main()` calls the real `Logging.configure()` once it reaches the write phase -- see
    `test_export_instrument_cli.py`'s identical fixture docstring for why this guard is needed
    to avoid poisoning `tests/logging/test_facade_emit_log_entry.py`'s atexit-registration test.
    """
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


def _load_cli_module() -> ModuleType:
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


# --- Fakes -------------------------------------------------------------------------------


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeBaselineGraph:
    """Answers `check_obligation_has_edge_cardinality`'s one exhaustive query with a fixed,
    scripted row set -- mirrors `test_preconditions.py`'s `_FakeGraph` exactly.
    """

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del q, params
        return _FakeQueryResult(self._rows)


_CLEAN_OBLIGATION_ROWS: list[object] = [["obligation-1", 1], ["obligation-2", 1]]
_VIOLATING_OBLIGATION_ROWS: list[object] = [["obligation-1", 1], ["obligation-2", 0]]


class _FakeSingleTenantGraph:
    """A tiny in-memory graph engine answering exactly the query shapes
    `run_live_merge.py`'s `_snapshot_graph` issues -- one node-properties dict per label, one
    edge list per relationship type. `merge_fn` is fully faked in this suite (it never mutates
    this fake graph), so before/after snapshots are identical by construction; that is fine --
    this suite proves ORCHESTRATION (call order, gating, report shape), not FalkorDB's own
    write semantics (already proven live by `test_merge_baseline_graph.py`/
    `test_live_capstone.py`).
    """

    _NODE_LABELS: ClassVar[tuple[str, ...]] = (
        "RegulatoryInstrument",
        "Role",
        "Requirement",
        "Obligation",
        "Capability",
    )
    _EDGE_TYPES: ClassVar[tuple[str, ...]] = (
        "DEFINES",
        "EXPRESSES",
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
    )

    def __init__(self) -> None:
        self.node_properties: dict[str, dict[str, dict[str, object]]] = {
            label: {} for label in self._NODE_LABELS
        }
        self.edges: dict[str, list[tuple[str, str, dict[str, object]]]] = {
            rel: [] for rel in self._EDGE_TYPES
        }

    def seed_capability(self, node_id: str, *, name: str, embedding: list[float]) -> None:
        self.node_properties["Capability"][node_id] = {
            "name": name,
            "confidence": 0.9,
            "embedding": embedding,
        }

    def seed_defines_edge(self, source_id: str, target_id: str, *, source_ref: str) -> None:
        self.edges["DEFINES"].append((source_id, target_id, {"source_ref": source_ref}))

    def seed_has_edge(self, source_id: str, target_id: str) -> None:
        self.edges["HAS"].append((source_id, target_id, {}))

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        for label in self._NODE_LABELS:
            if q == f"MATCH (n:{label}) RETURN count(n)":
                return _FakeQueryResult([[len(self.node_properties[label])]])
            if q == f"MATCH (n:{label}) RETURN n.id AS id, properties(n) AS props":
                rows: list[object] = [
                    [node_id, dict(props)] for node_id, props in self.node_properties[label].items()
                ]
                return _FakeQueryResult(rows)
        for rel in self._EDGE_TYPES:
            if q == f"MATCH ()-[r:{rel}]->() RETURN count(r)":
                return _FakeQueryResult([[len(self.edges[rel])]])
            if q == f"MATCH (a)-[e:{rel}]->(b) RETURN a.id, b.id, properties(e)":
                edge_prop_rows: list[object] = [[s, t, dict(p)] for s, t, p in self.edges[rel]]
                return _FakeQueryResult(edge_prop_rows)
            if q == f"MATCH (a)-[:{rel}]->(b) RETURN a.id, b.id":
                edge_id_rows: list[object] = [[s, t] for s, t, _p in self.edges[rel]]
                return _FakeQueryResult(edge_id_rows)
        message = f"unexpected query issued by run_live_merge.py: {q!r}"
        raise AssertionError(message)


def _make_graph_provider(
    cli_module: ModuleType,
    *,
    baseline_rows_by_short: dict[str, list[object]],
    single_tenant_graph: _FakeSingleTenantGraph,
) -> tuple[object, list[str]]:
    calls: list[str] = []
    graphs: dict[str, object] = {
        cli_module.baseline_graph_name(short): _FakeBaselineGraph(rows)
        for short, rows in baseline_rows_by_short.items()
    }
    graphs[cli_module.single_tenant_graph_name()] = single_tenant_graph

    def provider(graph_name: str) -> object:
        calls.append(graph_name)
        return graphs[graph_name]

    return provider, calls


class _RecordingMergeFn:
    """Fake `merge_fn`: records every call's kwargs and the run_id bound at call time; can be
    scripted to raise on a given 1-indexed call number.
    """

    def __init__(self, *, raise_on_call_number: int | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.run_ids_seen: list[str | None] = []
        self._raise_on_call_number = raise_on_call_number

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: object,
        single_tenant_graph: object,
        submitter: object,
        single_tenant_graph_name: str,
        baseline_graph_name: str,
        audit_event_id: str,
        embed_model: str,
        merge_threshold: float,
        review_threshold: float,
        role_review_threshold: float,
        call_embedding: object | None = None,
        emitter: object | None = None,
    ) -> object:
        del call_embedding
        self.run_ids_seen.append(current_run_id())
        self.calls.append(
            {
                "regulatory_instrument_id": regulatory_instrument_id,
                "baseline_graph": baseline_graph,
                "single_tenant_graph": single_tenant_graph,
                "submitter": submitter,
                "single_tenant_graph_name": single_tenant_graph_name,
                "baseline_graph_name": baseline_graph_name,
                "audit_event_id": audit_event_id,
                "embed_model": embed_model,
                "merge_threshold": merge_threshold,
                "review_threshold": review_threshold,
                "emitter": emitter,
            }
        )
        if self._raise_on_call_number is not None and len(self.calls) == self._raise_on_call_number:
            message = f"boom on call #{len(self.calls)}"
            raise RuntimeError(message)
        return None


class _FakeGateway:
    """Stands in for the Graph Write Gateway: `merge_fn` is faked, so it is never submitted to."""


class _FakeWriteAccess:
    """The injected `write_access` builder: hands out one gateway and a recording audit recorder."""

    def __init__(self) -> None:
        self.gateway = _FakeGateway()
        self.audit_calls: list[dict[str, object]] = []
        self.build_calls = 0

    def __call__(self, config: object) -> tuple[object, object]:
        del config
        self.build_calls += 1
        return self.gateway, self._record

    def _record(
        self, *, action: str, resource_type: str, resource_id: str, details: Mapping[str, object]
    ) -> str:
        self.audit_calls.append(
            {
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": dict(details),
            }
        )
        return f"audit-{len(self.audit_calls)}"


_FIXED_CLOCK_VALUE = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return _FIXED_CLOCK_VALUE


_INITIAL_PHRASE = "I APPROVE THE LIVE MERGE INTO policy_system"

_VALID_APPROVAL_FIELDS: dict[str, str] = {
    "approved_by": "tete@cartman.dk",
    "approved_at": "2026-09-13T10:00:00Z",
    "scope": "CRA-1.0,GDPR-1.0,NIS2-1.0",
    "target_graph": "policy_system",
    "precondition_check_result": (
        "cra_baseline: 0 violations - GO; gdpr_baseline: 0 violations - GO; "
        "nis2_baseline: 0 violations - GO"
    ),
    "confirmation_phrase": _INITIAL_PHRASE,
}


def _write_approval_file(tmp_path: Path, **overrides: str | None) -> Path:
    fields = dict(_VALID_APPROVAL_FIELDS)
    for key, value in overrides.items():
        if value is None:
            fields.pop(key, None)
        else:
            fields[key] = value
    path = tmp_path / "approval.md"
    path.write_text(
        "\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n", encoding="utf-8"
    )
    return path


_CLEAN_BASELINE_ROWS: dict[str, list[object]] = {
    "CRA": _CLEAN_OBLIGATION_ROWS,
    "GDPR": _CLEAN_OBLIGATION_ROWS,
    "NIS2": _CLEAN_OBLIGATION_ROWS,
}


def _base_argv(*, approval_path: Path, report_path: Path, run_kind: str = "initial") -> list[str]:
    return [
        "--approval-file",
        str(approval_path),
        "--run-kind",
        run_kind,
        "--report-path",
        str(report_path),
    ]


# --- Case 1: no approval file at all ------------------------------------------------------


def test_no_approval_file_exits_2_with_zero_merge_calls(
    cli_module: ModuleType, tmp_path: Path
) -> None:
    missing_approval_path = tmp_path / "does_not_exist.md"
    single_tenant_graph = _FakeSingleTenantGraph()
    provider, provider_calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short=_CLEAN_BASELINE_ROWS,
        single_tenant_graph=single_tenant_graph,
    )
    merge_fn = _RecordingMergeFn()

    exit_code = cli_module.main(
        _base_argv(approval_path=missing_approval_path, report_path=tmp_path / "report.json"),
        graph_provider=provider,
        merge_fn=merge_fn,
        clock=_fixed_clock,
    )

    assert exit_code == 2
    assert merge_fn.calls == []
    assert provider_calls == []


# --- Case 2: approval file present but confirmation_phrase wrong for the given --run-kind --


def test_wrong_confirmation_phrase_for_run_kind_exits_2_with_zero_merge_calls(
    cli_module: ModuleType, tmp_path: Path
) -> None:
    approval_path = _write_approval_file(tmp_path, confirmation_phrase="I APPROVE THE WRONG THING")
    single_tenant_graph = _FakeSingleTenantGraph()
    provider, provider_calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short=_CLEAN_BASELINE_ROWS,
        single_tenant_graph=single_tenant_graph,
    )
    merge_fn = _RecordingMergeFn()

    exit_code = cli_module.main(
        _base_argv(approval_path=approval_path, report_path=tmp_path / "report.json"),
        graph_provider=provider,
        merge_fn=merge_fn,
        clock=_fixed_clock,
    )

    assert exit_code == 2
    assert merge_fn.calls == []
    assert provider_calls == []


def _unreachable_graph_provider(graph_name: str) -> object:
    message = f"graph_provider must never be called: unexpected graph_name={graph_name!r}"
    raise AssertionError(message)


def test_rerun_phrase_rejected_when_run_kind_is_initial(
    cli_module: ModuleType, tmp_path: Path
) -> None:
    """The phrase is looked up from `--run-kind`, never taken as a CLI value --
    a valid RERUN approval must not satisfy an `--run-kind initial` invocation.
    """
    approval_path = _write_approval_file(
        tmp_path, confirmation_phrase="I APPROVE THE LIVE MERGE RE-RUN INTO policy_system"
    )
    merge_fn = _RecordingMergeFn()

    exit_code = cli_module.main(
        _base_argv(
            approval_path=approval_path, report_path=tmp_path / "report.json", run_kind="initial"
        ),
        graph_provider=_unreachable_graph_provider,
        merge_fn=merge_fn,
        clock=_fixed_clock,
    )

    assert exit_code == 2
    assert merge_fn.calls == []


# --- Case 3: approval valid, a precondition violation blocks the merge --------


def test_precondition_violation_exits_3_with_zero_merge_calls(
    cli_module: ModuleType, tmp_path: Path
) -> None:
    approval_path = _write_approval_file(tmp_path)
    single_tenant_graph = _FakeSingleTenantGraph()
    provider, _provider_calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short={
            "CRA": _CLEAN_OBLIGATION_ROWS,
            "GDPR": _CLEAN_OBLIGATION_ROWS,
            "NIS2": _VIOLATING_OBLIGATION_ROWS,
        },
        single_tenant_graph=single_tenant_graph,
    )
    merge_fn = _RecordingMergeFn()

    exit_code = cli_module.main(
        _base_argv(approval_path=approval_path, report_path=tmp_path / "report.json"),
        graph_provider=provider,
        merge_fn=merge_fn,
        clock=_fixed_clock,
    )

    assert exit_code == 3
    assert merge_fn.calls == []
    # The approval file must not be consumed/renamed on a blocked run.
    assert approval_path.exists()


# --- Case 4: approval valid, preconditions clean, merge_threshold unset (fail closed) --


def test_merge_threshold_unset_exits_1_with_zero_merge_calls(
    cli_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", raising=False)
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")
    approval_path = _write_approval_file(tmp_path)
    single_tenant_graph = _FakeSingleTenantGraph()
    provider, _provider_calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short=_CLEAN_BASELINE_ROWS,
        single_tenant_graph=single_tenant_graph,
    )
    merge_fn = _RecordingMergeFn()

    exit_code = cli_module.main(
        _base_argv(approval_path=approval_path, report_path=tmp_path / "report.json"),
        graph_provider=provider,
        merge_fn=merge_fn,
        clock=_fixed_clock,
    )

    assert exit_code == 1
    assert merge_fn.calls == []
    assert approval_path.exists()


def test_review_threshold_unset_exits_1_naming_it_with_zero_merge_calls(
    cli_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.9")
    monkeypatch.delenv("PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD", raising=False)
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")
    approval_path = _write_approval_file(tmp_path)
    provider, _provider_calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short=_CLEAN_BASELINE_ROWS,
        single_tenant_graph=_FakeSingleTenantGraph(),
    )
    merge_fn = _RecordingMergeFn()

    exit_code = cli_module.main(
        _base_argv(approval_path=approval_path, report_path=tmp_path / "report.json"),
        graph_provider=provider,
        merge_fn=merge_fn,
        clock=_fixed_clock,
    )

    assert exit_code == 1
    assert merge_fn.calls == []
    assert "PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD" in capsys.readouterr().err
    assert approval_path.exists()


def test_role_review_threshold_unset_exits_1_naming_it_with_zero_merge_calls(
    cli_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.9")
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD", "0.5")
    monkeypatch.delenv("PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD", raising=False)
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")
    approval_path = _write_approval_file(tmp_path)
    provider, _provider_calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short=_CLEAN_BASELINE_ROWS,
        single_tenant_graph=_FakeSingleTenantGraph(),
    )
    merge_fn = _RecordingMergeFn()

    exit_code = cli_module.main(
        _base_argv(approval_path=approval_path, report_path=tmp_path / "report.json"),
        graph_provider=provider,
        merge_fn=merge_fn,
        clock=_fixed_clock,
    )

    assert exit_code == 1
    assert merge_fn.calls == []
    assert "PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD" in capsys.readouterr().err
    assert approval_path.exists()


# --- Case 5: full happy path -----------------------------------------------------------


def test_happy_path_calls_merge_fn_three_times_in_order_and_writes_report(
    cli_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.9")
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD", "0.5")
    monkeypatch.setenv("PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD", "0.8")
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")
    approval_path = _write_approval_file(tmp_path)
    report_path = tmp_path / "report.json"

    single_tenant_graph = _FakeSingleTenantGraph()
    # Seed a Capability WITH an embedding, and a property-bearing DEFINES edge plus a
    # propertyless HAS edge, so the before-snapshot's node_properties/edge_properties/
    # edge_ids shapes are exercised meaningfully.
    single_tenant_graph.seed_capability(
        "cap_existing", name="Existing Capability", embedding=[0.1, 0.2, 0.3]
    )
    single_tenant_graph.seed_defines_edge("CRA-1.0", "role_existing", source_ref="art-1")
    single_tenant_graph.seed_has_edge("role_existing", "obligation_existing")

    provider, _provider_calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short=_CLEAN_BASELINE_ROWS,
        single_tenant_graph=single_tenant_graph,
    )
    merge_fn = _RecordingMergeFn()

    exit_code = cli_module.main(
        _base_argv(approval_path=approval_path, report_path=report_path),
        graph_provider=provider,
        merge_fn=merge_fn,
        write_access=_FakeWriteAccess(),
        clock=_fixed_clock,
    )

    assert exit_code == 0
    assert [call["regulatory_instrument_id"] for call in merge_fn.calls] == [
        "CRA-1.0",
        "NIS2-1.0",
        "GDPR-1.0",
    ]
    # Each call ran under a distinct, non-None run_id.
    assert len(set(merge_fn.run_ids_seen)) == 3
    assert all(run_id is not None for run_id in merge_fn.run_ids_seen)
    # merge_threshold/embed_model reached merge_fn as real ServiceConfig-resolved values.
    for call in merge_fn.calls:
        assert call["merge_threshold"] == pytest.approx(0.9)
        assert call["review_threshold"] == pytest.approx(0.5)
        assert call["embed_model"] == "azure/text-embedding-3-large"

    # Approval file consumed with a `.consumed-<timestamp>` suffix; original path gone.
    assert not approval_path.exists()
    consumed_candidates = list(tmp_path.glob("approval.md.consumed-*"))
    assert len(consumed_candidates) == 1

    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["errors"] == []
    assert report["regulations_attempted"] == ["CRA-1.0", "NIS2-1.0", "GDPR-1.0"]
    assert len(set(report["run_ids"])) == 3

    for section in ("before", "after"):
        snapshot = report[section]
        assert set(snapshot) == {
            "node_counts",
            "edge_counts",
            "node_ids",
            "node_properties",
            "edge_properties",
            "edge_ids",
        }
        # `embedding` must be absent from the captured
        # Capability properties even though the fake graph carries one -- stripped at
        # CAPTURE time, not comparison time.
        assert "embedding" not in snapshot["node_properties"]["Capability"]["cap_existing"]
        assert snapshot["node_properties"]["Capability"]["cap_existing"]["name"] == (
            "Existing Capability"
        )
        # edge_properties is present and shaped correctly for the seeded DEFINES edge.
        assert snapshot["edge_properties"]["DEFINES"]["CRA-1.0|role_existing"] == {
            "source_ref": "art-1"
        }
        assert "EXPRESSES" in snapshot["edge_properties"]
        # edge_ids is present and shaped correctly for the seeded HAS edge.
        assert ["role_existing", "obligation_existing"] in snapshot["edge_ids"]["HAS"]
        assert set(snapshot["edge_ids"]) == {"HAS", "SATISFIED_BY", "REQUIRES"}


# --- Case 6: merge_fn raises on the second call (NIS2) -------------------------------------


def test_merge_fn_raises_on_second_call_stops_before_third_and_leaves_approval_untouched(
    cli_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.9")
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD", "0.5")
    monkeypatch.setenv("PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD", "0.8")
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")
    approval_path = _write_approval_file(tmp_path)
    report_path = tmp_path / "report.json"

    single_tenant_graph = _FakeSingleTenantGraph()
    provider, _provider_calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short=_CLEAN_BASELINE_ROWS,
        single_tenant_graph=single_tenant_graph,
    )
    merge_fn = _RecordingMergeFn(raise_on_call_number=2)

    exit_code = cli_module.main(
        _base_argv(approval_path=approval_path, report_path=report_path),
        graph_provider=provider,
        merge_fn=merge_fn,
        write_access=_FakeWriteAccess(),
        clock=_fixed_clock,
    )

    assert exit_code != 0
    # CRA (1st) and NIS2 (2nd, raises) were attempted; GDPR (3rd) never was.
    assert [call["regulatory_instrument_id"] for call in merge_fn.calls] == ["CRA-1.0", "NIS2-1.0"]

    assert approval_path.exists(), "a failed run must leave the approval file untouched"
    assert not list(tmp_path.glob("approval.md.consumed-*"))

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["errors"] != []
    assert report["errors"][0]["regulatory_instrument_id"] == "NIS2-1.0"


# --- The merge writes through the Graph Write Gateway ---------------------------


def _passing_run(
    cli_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_access: object,
    merge_fn: _RecordingMergeFn,
) -> tuple[int, Path]:
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.9")
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD", "0.5")
    monkeypatch.setenv("PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD", "0.8")
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")
    approval_path = _write_approval_file(tmp_path)
    provider, _calls = _make_graph_provider(
        cli_module,
        baseline_rows_by_short=_CLEAN_BASELINE_ROWS,
        single_tenant_graph=_FakeSingleTenantGraph(),
    )
    exit_code = cli_module.main(
        _base_argv(approval_path=approval_path, report_path=tmp_path / "report.json"),
        graph_provider=provider,
        merge_fn=merge_fn,
        clock=_fixed_clock,
        write_access=write_access,
    )
    return exit_code, approval_path


def test_the_cli_passes_the_gateway_graph_names_and_an_audit_event_to_the_merge_function(
    cli_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each regulation's merge gets the one gateway, both graph names and its own audit row id.

    The row is a system-actor `ingestion_run.submit` row whose resource is the run id (the same
    row an internal-seed run records), recorded once per regulation before its merge starts.
    """
    write_access = _FakeWriteAccess()
    merge_fn = _RecordingMergeFn()

    exit_code, _approval = _passing_run(
        cli_module, tmp_path, monkeypatch, write_access=write_access, merge_fn=merge_fn
    )

    assert exit_code == 0
    assert write_access.build_calls == 1
    assert [
        (
            call["baseline_graph_name"],
            call["single_tenant_graph_name"],
            call["audit_event_id"],
            call["submitter"] is write_access.gateway,
        )
        for call in merge_fn.calls
    ] == [
        ("cra_baseline", "policy_system", "audit-1", True),
        ("nis2_baseline", "policy_system", "audit-2", True),
        ("gdpr_baseline", "policy_system", "audit-3", True),
    ]
    assert [
        (c["action"], c["resource_type"], c["resource_id"]) for c in write_access.audit_calls
    ] == [
        ("ingestion_run.submit", "ingestion_run", run_id)
        for run_id in json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["run_ids"]
    ]
    assert all(c["details"]["trigger"] == "operator_cli" for c in write_access.audit_calls)  # type: ignore[index]


def test_the_cli_fails_closed_before_any_merge_when_no_gateway_can_be_built(
    cli_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PS state database / gateway: exit 1, no merge call, no graph selected, approval kept."""

    def _no_gateway(config: object) -> tuple[object, object]:
        del config
        message = "the PS state database is not configured"
        raise RuntimeError(message)

    merge_fn = _RecordingMergeFn()

    exit_code, approval_path = _passing_run(
        cli_module, tmp_path, monkeypatch, write_access=_no_gateway, merge_fn=merge_fn
    )

    assert exit_code == 1
    assert merge_fn.calls == []
    assert approval_path.exists()
    assert not (tmp_path / "report.json").exists()  # nothing ran, so no report of a run


def test_an_unavailable_audit_trail_stops_the_run_before_that_regulations_merge(
    cli_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit row is the group's anchor: if it cannot be written nothing may be merged."""

    class _AuditDown(_FakeWriteAccess):
        def _record(
            self,
            *,
            action: str,
            resource_type: str,
            resource_id: str,
            details: Mapping[str, object],
        ) -> str:
            del action, resource_type, resource_id, details
            message = "The audit trail is temporarily unavailable."
            raise RuntimeError(message)

    merge_fn = _RecordingMergeFn()

    exit_code, approval_path = _passing_run(
        cli_module, tmp_path, monkeypatch, write_access=_AuditDown(), merge_fn=merge_fn
    )

    assert exit_code == 4
    assert merge_fn.calls == []
    assert approval_path.exists()
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["errors"][0]["regulatory_instrument_id"] == "CRA-1.0"


def test_snapshot_of_a_graph_key_that_does_not_exist_yet_counts_zero(
    cli_module: ModuleType,
) -> None:
    """A read-only handle answers every query on a never-created key with no rows (not `[[0]]`).

    The first merge into a new `policy_system` snapshots exactly that, so the snapshot must read
    zero nodes and zero edges instead of failing on the missing count row.
    """
    snapshot = cli_module._snapshot_graph(_FakeBaselineGraph([]))  # pyright: ignore[reportPrivateUsage]

    assert set(snapshot["node_counts"].values()) == {0}  # pyright: ignore[reportAttributeAccessIssue]
    assert set(snapshot["edge_counts"].values()) == {0}  # pyright: ignore[reportAttributeAccessIssue]
