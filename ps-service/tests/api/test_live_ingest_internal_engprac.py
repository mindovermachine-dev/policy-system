"""Live proof that `POST /ingestions` imports the engineering-practices tree as caller-owned drafts.

`@pytest.mark.falkordb_live @pytest.mark.llm_live` (issue #227): the real `ENGPRAC-1.0` seed is
POSTed through the real route, real `internal_ingestion` and real `merge` stages against real
FalkorDB (the merge's Capability dedup embeds, hence the LLM credentials), into a **disposable**
single-tenant graph -- never the shared `policy_system` graph. Proves, against real
infrastructure:

1. the response is 200 and every stage succeeded (AC-BI-003);
2. the disposable graph holds 11 Policies, 21 Standards and 39 Controls, all `draft`, every
   Policy owned by the caller's `(sub, iss)`, with the 12/21/39/11/24 governance and
   classification edges (AC-BI-002/004);
3. restoring the same curated instrument into a second scratch graph produces the same
   Policy/Standard/Control ids and edge counts (AC-BI-005);
4. after one Policy is approved through the real lifecycle service by a different manager, a
   second ingest leaves that approved tree, its owner and every count untouched (AC-BI-006).

Unverified in the development environment that wrote it (no FalkorDB with the graph module was
available): run it with `-m falkordb_live` against a real instance.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore
from fastapi.testclient import TestClient

from ps_service.api.dependencies import get_principal
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.audit.store import PsycopgAuditStore
from ps_service.auth.models import Principal
from ps_service.authz.models import AccessRole
from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.config import load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.export import catalog_writer
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.logging import facade
from ps_service.main import create_app
from ps_service.policy_lifecycle.graph_submission import lifecycle_submitter
from ps_service.policy_lifecycle.service import approve_policy, propose_policy
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.logging import LogEmitter

# Captured at import time, before the autouse `_isolate_logging` fixture strips them.
_CHAT_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")
_EMBED_MODEL = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")

pytestmark = [
    pytest.mark.falkordb_live,
    pytest.mark.llm_live,
    pytest.mark.skipif(
        not _CHAT_MODEL or not _EMBED_MODEL,
        reason="requires .env sourced (PS_LLMINTERFACE_MODEL/_EMBED_MODEL, AZURE_*)",
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED: dict[str, object] = json.loads(
    (
        _REPO_ROOT
        / "internal-sources"
        / "engineering-practices"
        / "engineering-practices-seed.json"
    ).read_text(encoding="utf-8")
)
_INSTRUMENT_DIR = _REPO_ROOT / "curated-content" / "ENGPRAC-1.0"
_SHORT_NAME = "ENGPRAC"
_OWNER = ("ingest-owner", "https://idp.example")
_MANAGER = ("ingest-manager", "https://idp.example")
_GOVERNANCE_EDGES = ("GOVERNED_BY", "SUPPORTED_BY", "IMPLEMENTED_BY", "OWNS", "VERIFIED_BY")
_EXPECTED_EDGES = {
    "GOVERNED_BY": 12,
    "SUPPORTED_BY": 21,
    "IMPLEMENTED_BY": 39,
    "OWNS": 11,
    "VERIFIED_BY": 24,
}


def _rows(graph: GraphHandle, query: str) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query).result_set)


def _governance_state(graph: GraphHandle) -> dict[str, object]:
    """Ids and status/owner of every governance node, plus the governance edge counts."""
    state: dict[str, object] = {
        label: sorted(
            (cast("str", r[0]), cast("str", r[1]), cast("str | None", r[2]))
            for r in _rows(graph, f"MATCH (n:{label}) RETURN n.id, n.status, n.owner_subject")
        )
        for label in ("Policy", "Standard", "Control")
    }
    for edge in _GOVERNANCE_EDGES:
        state[edge] = cast("int", _rows(graph, f"MATCH ()-[r:{edge}]->() RETURN count(r)")[0][0])
    return state


@pytest.fixture
def ingest_env(tmp_path: Path) -> Iterator[tuple[TestClient, FalkorDB, str]]:
    """A real app over a disposable single-tenant graph; yields the client, db and graph name."""
    assert _CHAT_MODEL is not None  # narrowed by the module skipif
    assert _EMBED_MODEL is not None
    token = uuid.uuid4().hex[:12]
    graph_name = f"policy_system_gh227_{token}"
    monkeypatch = pytest.MonkeyPatch()
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", _CHAT_MODEL)
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", _EMBED_MODEL)
    if not os.environ.get("PS_COMPANYMERGE_SIMILARITY_THRESHOLD"):
        monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.85")
    monkeypatch.setenv("PS_FALKORDB_GRAPH", graph_name)
    db = connect_from_config(load_config())
    scratch = (
        graph_name,
        native_graph_name(_SHORT_NAME),
        baseline_graph_name(_SHORT_NAME),
    )
    for name in scratch:
        if name in db.list_graphs():
            db.select_graph(name).delete()
    facade.configure(log_path=tmp_path / "ingest.jsonl")
    app = create_app(load_config())
    app.dependency_overrides[get_principal] = lambda: Principal(sub=_OWNER[0], iss=_OWNER[1])
    # Every pipeline stage writes through the Graph Write Gateway (#208-#210). `TestClient` is not
    # entered, so the app lifespan never installs it: install the real one the way a deployed
    # service does. Not run in the implementation sandbox.
    install_graph_write_gateway(build_default_graph_write_gateway(load_config()))
    try:
        yield TestClient(app, raise_server_exceptions=False), db, graph_name
    finally:
        install_graph_write_gateway(None)
        monkeypatch.undo()
        for name in scratch:
            with contextlib.suppress(Exception):
                db.select_graph(name).delete()
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


def _restore_into(db: FalkorDB, graph_name: str, emitter: LogEmitter) -> None:
    """Restore the shipped curated `ENGPRAC-1.0` artifact into the scratch graph `graph_name`."""
    short_name = f"ENGPRAC_P{uuid.uuid4().hex[:8]}"
    manifest = replace(catalog_writer.read_manifest(_INSTRUMENT_DIR), short_name=short_name)
    # The restore writes through the real gateway, its groups anchored to a real
    # `instrument.restore` audit row (the graph log's audit link is a foreign key into
    # `audit_events`). Not run in the implementation sandbox.
    config = load_config()
    audit_event_id = PsycopgAuditStore(config).record_standalone_with_event_id(
        actor_subject=_OWNER[0],
        actor_issuer=_OWNER[1],
        action="instrument.restore",
        resource_type="instrument",
        resource_id="ENGPRAC-1.0",
        outcome="applied",
        details={"instrument_id": "ENGPRAC-1.0", "status": "started", "source": "catalog"},
    )
    gateway = build_default_graph_write_gateway(config)
    restore_instrument(
        RestoreArtifact(
            manifest=manifest,
            baseline_blob=(_INSTRUMENT_DIR / "baseline.json").read_bytes(),
            native_blob=(_INSTRUMENT_DIR / "native.json").read_bytes(),
        ),
        db=db,
        single_tenant_graph_name=graph_name,
        similarity_threshold=0.9,
        actor="test-actor",
        open_gateway=lambda: gateway,
        audit_event_id=audit_event_id,
        emitter=emitter,
        owner=_OWNER,
    )


def test_ingest_imports_the_tree_as_caller_owned_drafts(
    ingest_env: tuple[TestClient, FalkorDB, str],
) -> None:
    """AC-BI-002/003/004: 200, all stages succeeded, 11/21/39 drafts owned by the caller."""
    client, db, graph_name = ingest_env

    response = client.post("/ingestions", json={"source": "internal", "content": _SEED})

    assert response.status_code == 200, response.text
    body = cast("dict[str, object]", response.json())
    stages = cast("list[dict[str, object]]", body["stages"])
    assert [s["status"] for s in stages] == ["succeeded", "succeeded"]
    graph = select_graph(db, graph_name)
    state = _governance_state(graph)
    assert [
        len(cast("list[object]", state[label])) for label in ("Policy", "Standard", "Control")
    ] == [
        11,
        21,
        39,
    ]
    for label in ("Policy", "Standard", "Control"):
        assert {row[1] for row in cast("list[tuple[str, str, str | None]]", state[label])} == {
            "draft"
        }
    assert {row[2] for row in cast("list[tuple[str, str, str | None]]", state["Policy"])} == {
        _OWNER[0]
    }
    assert {edge: state[edge] for edge in _GOVERNANCE_EDGES} == _EXPECTED_EDGES


def test_ingest_and_restore_produce_the_same_governance_tree(
    ingest_env: tuple[TestClient, FalkorDB, str], make_emitter: MakeEmitter
) -> None:
    """AC-BI-005: one shared write function, so ingest and restore agree node for node."""
    client, db, graph_name = ingest_env
    restored_graph_name = f"policy_system_gh227_restore_{uuid.uuid4().hex[:8]}"
    emitter, _log_path = make_emitter()
    try:
        assert (
            client.post("/ingestions", json={"source": "internal", "content": _SEED}).status_code
            == 200
        )
        _restore_into(db, restored_graph_name, emitter)

        assert _governance_state(select_graph(db, graph_name)) == _governance_state(
            select_graph(db, restored_graph_name)
        )
    finally:
        with contextlib.suppress(Exception):
            db.select_graph(restored_graph_name).delete()


def test_reingest_after_approval_leaves_the_approved_tree_untouched(
    ingest_env: tuple[TestClient, FalkorDB, str],
) -> None:
    """AC-BI-006: a second ingest changes neither the approved tree, its owner, nor any count."""
    client, db, graph_name = ingest_env
    assert (
        client.post("/ingestions", json={"source": "internal", "content": _SEED}).status_code == 200
    )
    graph = select_graph(db, graph_name)
    target = cast(
        "str",
        _rows(
            graph,
            "MATCH (p:Policy)-[:SUPPORTED_BY]->(:Standard) RETURN DISTINCT p.id ORDER BY p.id",
        )[0][0],
    )
    store = FakeAccessRoleStore()
    store.grant(actor=_OWNER, target=_OWNER, access_role=AccessRole.POLICY_MANAGER)
    store.grant(actor=_OWNER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)
    # The lifecycle writes through the real gateway, and its `applied` rows are real audit rows
    # (the graph log's audit link is a foreign key into `audit_events`). Not run in the
    # implementation sandbox.
    config = load_config()
    audit = PsycopgAuditStore(config)
    submitter = lifecycle_submitter(build_default_graph_write_gateway(config))
    propose_policy(
        actor=_OWNER, policy_id=target, graph=graph, audit_store=audit, submitter=submitter
    )
    approve_policy(
        actor=_MANAGER,
        policy_id=target,
        graph=graph,
        audit_store=audit,
        access_role_store=store,
        submitter=submitter,
    )
    before = _governance_state(graph)

    second = client.post("/ingestions", json={"source": "internal", "content": _SEED})

    assert second.status_code == 200, second.text
    assert _governance_state(graph) == before
