"""Integration test: `ps-cli catalog restore <instrument_id>` against a real spawned
`ps-service`, real FalkorDB, and NO LLM provider configured at all (Slice 7.5, D5/D13).

Marked `@pytest.mark.integration` + `@pytest.mark.falkordb_live` -- unlike
`test_integration_regulations_ingest.py`, this test deliberately never sets
`PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL`/any provider credential on the spawned
`ps_service` subprocess (AC-BI-005's "no LLM provider configured" requirement) -- confirmed
safe by reading `ps_service/main.py`: `create_restoration`'s route never depends on
`app.state.ready` (only `/ready` itself reads that flag; restore's own dependency guard is
`api/restore_orchestration.py::_require_similarity_threshold`, which needs only
`PS_COMPANYMERGE_SIMILARITY_THRESHOLD`, not LLM config), and `llm_interface.connectivity.
check_connectivity` raises immediately without any network call when `llm_interface_model is
None` (`ps_service/llm_interface/connectivity.py:38-42`) -- so `_check_dependencies_at_startup`
(issue #22) logs a warning and moves on, never blocking process startup. This test therefore
polls `/health` only, never `/ready` (which would never flip true with no LLM configured).

`ps-cli` never builds its own curated-instrument fixture format by hand: this test calls the
REAL `ps_service.export.export_instrument()` (Batch 3) against a small, hand-authored
FalkorDB graph pair -- the exact same fixture-construction shape already proven live by
`ps-service/tests/restore/test_export_restore_roundtrip_live.py` (Slice 5.10) -- with a fake,
deterministic `EmbeddingCaller` (no real LLM call at export/fixture-build time either), so the
resulting `manifest.json`/`baseline.json`/`native.json` on disk are byte-for-byte what a real
curation run would produce, not a synthetic stand-in `ps-cli` invented. Cross-package imports
here (`ps_service.export.*`, `falkordb`) are test-only fixture-construction tooling, exactly
like `ps-cli/tests/test_integration_regulations_ingest.py`'s own `falkordb` import -- `ps-cli/
tests/test_architecture_boundary.py` only scans `ps-cli/src/ps_cli/**`, so neither is an
AC-BI-004 violation.

**A real finding, out of this slice's scope to fix**: `export_instrument()` writes the
instrument directory to `repo_root / "curated-content" / instrument_id / ...` but writes
`catalog.json` to `repo_root / "catalog.json"` (sibling to `curated-content/`, not inside it) --
confirmed by reading `ps_service/export/export_instrument.py` and `export/catalog_writer.py`
directly. This does not match D1's own ASCII layout diagram (PLAN.md §1 D1), which shows
`catalog.json` as a child of `curated-content/`, matching `ps_cli.config.CliConfig
.curated_repo_path`'s default (`"./curated-content"`) and `ps_cli.catalog_repo.read_catalog`'s
own `repo_path / "catalog.json"` lookup (Slice 7.1). `catalog restore` (this test's subject)
never calls `read_catalog` -- only `read_artifact`, which needs just the per-instrument
directory -- so this test sidesteps the mismatch entirely by pointing
`PS_CLI_CURATED_REPO_PATH` at `repo_root / "curated-content"` directly. `catalog list` against
a real, `export_instrument`-produced repo would NOT find its `catalog.json` today; this is
flagged for the orchestrator/a future slice, not silently worked around.
"""

from __future__ import annotations

import os
import random
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import cast

import httpx
import pytest
from falkordb import FalkorDB  # test-only: not a ps_cli production import, see module docstring
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_cli.cli import run
from ps_service.export.export_instrument import InstrumentDescriptor, export_instrument
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.logging import EmitterConfig, LogEmitter

if sys.platform == "win32":  # pragma: no cover - documented platform caveat, not exercised here
    pytest.skip("subprocess signal semantics differ on Windows", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOST = "127.0.0.1"
_HEALTH_POLL_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.2
_TERMINATE_WAIT_TIMEOUT_SECONDS = 10
_FALKORDB_HOST = os.environ.get("PS_FALKORDB_HOST", "127.0.0.1")
_FALKORDB_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))
_SIMILARITY_THRESHOLD = "0.85"

pytestmark = [pytest.mark.integration, pytest.mark.falkordb_live]


class _FakeEmbeddingCaller:
    """A deterministic-per-text embedding stand-in -- no real LLM Provider call, ever.

    Mirrors `ps-service/tests/restore/test_export_restore_roundtrip_live.py`'s own
    `_FakeEmbeddingCaller` exactly (an 8-dim vector seeded from `random.Random(text)`, which
    reliably distinguishes unrelated Capability names well enough not to falsely converge
    two of them during restore's dedup replay, D6).
    """

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        """Return a deterministic embedding for `inputs[0]`, ignoring `model`/`timeout`."""
        del timeout
        assert len(inputs) == 1
        rng = random.Random(inputs[0])  # noqa: S311 -- deterministic test fixture, not security-sensitive
        vector = [rng.uniform(-1, 1) for _ in range(8)]
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def _find_free_port() -> int:
    """Bind a socket to port 0, read the OS-assigned port, close it, return the number."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _spawn_ps_service(
    port: int, log_dir: Path, single_tenant_graph: str
) -> subprocess.Popen[bytes]:
    """Spawn `python -m ps_service`, bound to `port`, with NO LLM provider configured at all.

    `PS_FALKORDB_GRAPH` points the single-tenant graph restore writes to at a disposable,
    test-scoped name -- this test never touches the real shared `policy_system` graph.
    Deliberately does not set `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL`/any
    provider credential (AC-BI-005) -- see the module docstring for why this is safe for
    `POST /restorations` specifically.
    """
    env = {
        **os.environ,
        "PS_SERVICE_PORT": str(port),
        "PS_LOGGING_DIR": str(log_dir),
        "PS_FALKORDB_HOST": _FALKORDB_HOST,
        "PS_FALKORDB_PORT": str(_FALKORDB_PORT),
        "PS_FALKORDB_GRAPH": single_tenant_graph,
        "PS_COMPANYMERGE_SIMILARITY_THRESHOLD": _SIMILARITY_THRESHOLD,
    }
    for llm_env_var in (
        "PS_LLMINTERFACE_MODEL",
        "PS_LLMINTERFACE_EMBED_MODEL",
        "AZURE_API_KEY",
        "AZURE_API_BASE",
    ):
        env.pop(llm_env_var, None)
    return subprocess.Popen(
        [sys.executable, "-m", "ps_service"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the subprocess, escalating to SIGKILL if it doesn't exit promptly."""
    if proc.poll() is not None:
        return  # already exited
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)


def _wait_until_healthy(base_url: str) -> None:
    """Poll `GET /health` until it responds at all, or raise `TimeoutError`.

    Never polls `/ready` -- with no LLM provider configured, `/ready` would never flip
    true (see module docstring); `/health` alone is sufficient since `POST /restorations`
    depends on neither flag.
    """
    deadline = time.monotonic() + _HEALTH_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=1.0)
        except httpx.HTTPError:
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue
        return
    msg = f"ps_service did not become healthy within {_HEALTH_POLL_TIMEOUT_SECONDS}s"
    raise TimeoutError(msg)


def _seed_curated_graphs(db: FalkorDB, instrument_id: str, short: str, token: str) -> None:
    """Hand-author a small `{short}_baseline`/`{short}_native` graph pair via real Cypher.

    Mirrors `ps-service/tests/restore/test_export_restore_roundtrip_live.py`'s own
    `_seed_curated_graphs` shape (2 Role, 2 Requirement, 3 Obligation, 3 Capability, full
    `DEFINES`/`EXPRESSES`/`HAS`/`SATISFIED_BY`/`REQUIRES` edge set for baseline;
    `RegulatoryInstrument`/`TITLE`/`ARTICLE` with `HAS` edges for native) -- proven, real
    curated-instrument-shaped content, not an ad hoc minimal graph invented for this test.
    """
    baseline = db.select_graph(f"{short}_baseline")
    baseline.query(
        "CREATE (:RegulatoryInstrument {id: $id, title: 'Slice 7.5 Regulation', "
        "source_type: 'external'})",
        {"id": instrument_id},
    )
    for role_id, name in ((f"{token}-role-1", "Role One"), (f"{token}-role-2", "Role Two")):
        baseline.query(
            "CREATE (:Role {id: $id, name: $name, confidence: 0.9})", {"id": role_id, "name": name}
        )
    for req_id, text in (
        (f"{token}-req-1", "Requirement One"),
        (f"{token}-req-2", "Requirement Two"),
    ):
        baseline.query(
            "CREATE (:Requirement {id: $id, text: $text, type: 'obligation', confidence: 0.9})",
            {"id": req_id, "text": text},
        )
    for obl_id, text in (
        (f"{token}-obl-1", "Obligation One"),
        (f"{token}-obl-2", "Obligation Two"),
        (f"{token}-obl-3", "Obligation Three"),
    ):
        baseline.query(
            "CREATE (:Obligation {id: $id, text: $text, confidence: 0.9})",
            {"id": obl_id, "text": text},
        )
    for cap_id, name in (
        (f"{token}-cap-1", "Capability One"),
        (f"{token}-cap-2", "Capability Two"),
        (f"{token}-cap-3", "Capability Three"),
    ):
        baseline.query(
            "CREATE (:Capability {id: $id, name: $name, confidence: 0.9})",
            {"id": cap_id, "name": name},
        )
    for role_id in (f"{token}-role-1", f"{token}-role-2"):
        baseline.query(
            "MATCH (ri:RegulatoryInstrument {id: $id}), (r:Role {id: $role_id}) "
            "CREATE (ri)-[:DEFINES {source_ref: 'art. 1'}]->(r)",
            {"id": instrument_id, "role_id": role_id},
        )
    for req_id in (f"{token}-req-1", f"{token}-req-2"):
        baseline.query(
            "MATCH (ri:RegulatoryInstrument {id: $id}), (req:Requirement {id: $req_id}) "
            "CREATE (ri)-[:EXPRESSES {source_ref: 'art. 1'}]->(req)",
            {"id": instrument_id, "req_id": req_id},
        )
    for role_id, obl_id in (
        (f"{token}-role-1", f"{token}-obl-1"),
        (f"{token}-role-2", f"{token}-obl-2"),
        (f"{token}-role-1", f"{token}-obl-3"),
    ):
        baseline.query(
            "MATCH (r:Role {id: $role_id}), (o:Obligation {id: $obl_id}) CREATE (r)-[:HAS]->(o)",
            {"role_id": role_id, "obl_id": obl_id},
        )
    for req_id, obl_id in (
        (f"{token}-req-1", f"{token}-obl-1"),
        (f"{token}-req-2", f"{token}-obl-2"),
        (f"{token}-req-1", f"{token}-obl-3"),
    ):
        baseline.query(
            "MATCH (req:Requirement {id: $req_id}), (o:Obligation {id: $obl_id}) "
            "CREATE (req)-[:SATISFIED_BY]->(o)",
            {"req_id": req_id, "obl_id": obl_id},
        )
    for obl_id, cap_id in (
        (f"{token}-obl-1", f"{token}-cap-1"),
        (f"{token}-obl-2", f"{token}-cap-2"),
        (f"{token}-obl-3", f"{token}-cap-3"),
    ):
        baseline.query(
            "MATCH (o:Obligation {id: $obl_id}), (c:Capability {id: $cap_id}) "
            "CREATE (o)-[:REQUIRES]->(c)",
            {"obl_id": obl_id, "cap_id": cap_id},
        )

    native = db.select_graph(f"{short}_native")
    native.query(
        "CREATE (:RegulatoryInstrument {id: $id, title: 'Slice 7.5 Regulation'})",
        {"id": instrument_id},
    )
    native.query(f"CREATE (:TITLE {{id: '{token}-title-1', text: 'Title I'}})")
    native.query(f"CREATE (:ARTICLE {{id: '{token}-art-1', text: 'Article 1 text'}})")
    native.query(
        f"MATCH (ri:RegulatoryInstrument {{id: $id}}), (t:TITLE {{id: '{token}-title-1'}}) "
        "CREATE (ri)-[:HAS]->(t)",
        {"id": instrument_id},
    )
    native.query(
        f"MATCH (t:TITLE {{id: '{token}-title-1'}}), (a:ARTICLE {{id: '{token}-art-1'}}) "
        "CREATE (t)-[:HAS]->(a)"
    )


def _query_rows(db: FalkorDB, graph_name: str, query: str) -> list[list[object]]:
    """Run `query` against `graph_name` and return its raw result rows."""
    result = db.select_graph(graph_name).query(query)
    return cast("list[list[object]]", result.result_set)


@pytest.fixture
def live_falkordb() -> FalkorDB:
    """A real `FalkorDB` client against this sandbox's real, reachable FalkorDB instance."""
    return FalkorDB(host=_FALKORDB_HOST, port=_FALKORDB_PORT)


def test_catalog_restore_against_real_spawned_ps_service_with_no_llm_configured(
    live_falkordb: FalkorDB,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-002/AC-BI-005/AC-BI-011 end to end: real `ps-cli catalog restore`, real
    `ps-service`, real FalkorDB, NO LLM provider configured anywhere -- restoring one
    curated instrument and confirming a follow-up direct-FalkorDB query sees the seeded
    content (mirrors `ps-cli regulations list`-style follow-up verification; `ps-cli` has
    no query command of its own, so the verification queries FalkorDB directly, exactly as
    `test_integration_regulations_ingest.py`'s own cleanup step already does).
    """
    token = uuid.uuid4().hex[:10]
    short = f"s75{token}"
    instrument_id = f"SLICE75-{token}"
    single_tenant_graph = f"__ac66_slice75_single_tenant_{token}__"
    baseline_graph_name = f"{short}_baseline"
    native_graph_name = f"{short}_native"

    live_falkordb.connection.delete(baseline_graph_name, native_graph_name, single_tenant_graph)
    proc: subprocess.Popen[bytes] | None = None
    try:
        _seed_curated_graphs(live_falkordb, instrument_id, short, token)

        descriptor = InstrumentDescriptor(
            short_name=short,
            instrument_id=instrument_id,
            version="1.0",
            celex=None,
            title="Slice 7.5 Regulation",
            source_type="external",
            jurisdiction=None,
        )
        repo_root = tmp_path / "repo"
        packaged_copy_path = tmp_path / "packaged" / "catalog.json"
        # `export_instrument`'s embedding backfill emits an audit log entry per node
        # (`emit_log_entry`) -- this test process never calls `ps_service.logging.facade.
        # configure()` (only the spawned subprocess does its own, independent setup), so a
        # `LogEmitter` is passed explicitly here, exactly like `ps-service/tests/conftest.
        # py::make_emitter`'s own fixture shape.
        fixture_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "export-fixture-log.jsonl"))
        export_instrument(
            descriptor,
            baseline_graph=graph_query_handle(live_falkordb, baseline_graph_name),
            native_graph=graph_query_handle(live_falkordb, native_graph_name),
            embed_model="fake-embed-model",
            repo_root=repo_root,
            packaged_copy_path=packaged_copy_path,
            call_embedding=_FakeEmbeddingCaller(),
            emitter=fixture_emitter,
        )
        curated_repo_path = repo_root / "curated-content"
        assert (curated_repo_path / instrument_id / "manifest.json").is_file()

        port = _find_free_port()
        log_dir = tmp_path / "ps-service-logs"
        log_dir.mkdir()
        proc = _spawn_ps_service(port, log_dir, single_tenant_graph)
        base_url = f"http://{_HOST}:{port}"
        _wait_until_healthy(base_url)

        monkeypatch.setenv("PS_CLI_SERVICE_URL", base_url)
        monkeypatch.setenv("PS_CLI_CURATED_REPO_PATH", str(curated_repo_path))

        exit_code = run(["catalog", "restore", instrument_id], client=None)

        captured = capsys.readouterr()
        assert exit_code == 0, f"stdout={captured.out!r} stderr={captured.err!r}"
        assert f"instrument_id: {instrument_id}" in captured.out
        assert "verified: succeeded" in captured.out
        assert "staged: succeeded" in captured.out
        assert "merged_and_finalized: succeeded" in captured.out

        # Follow-up: confirm the seeded content genuinely landed in the single-tenant
        # graph, restored via the real ps-service subprocess -- no LLM call anywhere.
        role_rows = _query_rows(
            live_falkordb, single_tenant_graph, "MATCH (n:Role) RETURN n.id, n.name ORDER BY n.id"
        )
        assert role_rows == [
            [f"{token}-role-1", "Role One"],
            [f"{token}-role-2", "Role Two"],
        ]

        capability_rows = _query_rows(
            live_falkordb,
            single_tenant_graph,
            "MATCH (n:Capability) RETURN n.id, n.name, n.embedding IS NOT NULL ORDER BY n.id",
        )
        assert capability_rows == [
            [f"{token}-cap-1", "Capability One", True],
            [f"{token}-cap-2", "Capability Two", True],
            [f"{token}-cap-3", "Capability Three", True],
        ]

        requires_rows = _query_rows(
            live_falkordb,
            single_tenant_graph,
            "MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) RETURN o.id, c.id ORDER BY o.id",
        )
        assert requires_rows == [
            [f"{token}-obl-1", f"{token}-cap-1"],
            [f"{token}-obl-2", f"{token}-cap-2"],
            [f"{token}-obl-3", f"{token}-cap-3"],
        ]
    finally:
        if proc is not None:
            _terminate(proc)
        live_falkordb.connection.delete(baseline_graph_name, native_graph_name, single_tenant_graph)
        assert live_falkordb.connection.exists(baseline_graph_name) == 0
        assert live_falkordb.connection.exists(native_graph_name) == 0
        assert live_falkordb.connection.exists(single_tenant_graph) == 0
