"""HTTP tests for `POST /change-checks` (issue #73, PLAN.md §4 Slices 1-2).

Slice 1's vertical-stub proof (`create_change_check` depended only on
`provide_run_id`) used the shared bare `client` fixture directly, since no
dependency needed overriding yet. Slice 2 wires `create_change_check` to a
real `ChangeCheckDependencies` (`config`/`dependencies`, via
`provide_change_check_dependencies`) exactly like `POST /ingestions`/
`POST /restorations` -- so every test here now builds its own `TestClient`
with `app.dependency_overrides[provide_change_check_dependencies]` set to a
fake bundle (mirrors `test_ingestions_catalog.py`'s/
`test_routes_restorations.py`'s own established pattern), rather than the
bare `client` fixture, which would otherwise reach the real, unmocked
`build_default_change_check_dependencies()` and attempt a live FalkorDB
call. AC-BI-001's "no auth dependency" is unaffected by this -- the route
still takes no auth dependency of any kind.

Slice 6 (D8) wires `run_change_check_sweep` to unconditionally emit
`change_check_sweep`/`change_check_instrument` log entries through the
process-default emitter, which none of these fast HTTP tests `configure()`s
-- so every test *except* the one dedicated run-id proof below explicitly
requests `_stub_run_log` (mirrors `test_ingestions_catalog.py`'s own
`_stub_run_log`/`_noop_emit` precedent for the identical situation).
`test_post_change_checks_response_run_id_matches_the_provide_run_id_binding`
is the one exception -- it needs the real facade, so it uses
`configured_logging`/`read_lines` instead (mirrors `test_run_context.py`'s
own established shape) and does not request the stub.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from api._audit_fakes import InMemoryAuditStore
from api._fakes import (
    FakeChangeCheckDependencies,
    build_fake_change_check_dependencies,
    compliance_officer_principal,
    install_compliance_officer_grant,
    install_no_principal,
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.dependencies import provide_audit_store, provide_change_check_dependencies
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)
from ps_service.config import ServiceConfig
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from api._fakes import ReadLines

_APP_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=True,
    authentik_api_token="test-authentik-token",
    authentik_base_url="https://authentik.example.com",
)


def _noop_emit(**_kwargs: object) -> None:
    """Discard a run-log entry (Logging boundary stub -- see `_stub_run_log`)."""


@pytest.fixture
def _stub_run_log(  # pyright: ignore[reportUnusedFunction]  # requested via usefixtures, not autouse
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the Logging boundary so `run_change_check_sweep` needs no `configure()`d facade.

    Mirrors `test_ingestions_catalog.py`'s own `_stub_run_log` fixture exactly,
    for the identical reason: `_emit_sweep`/`_emit_instrument` (D8) emit through
    the process-wide default emitter, which these fast HTTP tests deliberately
    do not `configure()`. Not autouse (unlike the ingestion precedent) because
    one test in this file -- the dedicated run-id proof -- needs the real
    facade instead; every other test requests this explicitly via
    `@pytest.mark.usefixtures("_stub_run_log")`.
    """
    # detroit-exception: process-wide atexit logging facade (AUDIT §2 case 12), not a business fake
    monkeypatch.setattr("ps_service.api.change_check_orchestration.emit_log_entry", _noop_emit)


def _client_with_fake(
    fake: FakeChangeCheckDependencies, *, audit_store: InMemoryAuditStore | None = None
) -> TestClient:
    """A `TestClient` over `fake`; audit rows (issue #195) go to memory, never Postgres."""
    app = create_app(_APP_CONFIG)
    app.dependency_overrides[provide_change_check_dependencies] = lambda: fake.dependencies
    store = audit_store if audit_store is not None else InMemoryAuditStore()
    app.dependency_overrides[provide_audit_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _grant_compliance_officer(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test below drives a caller holding `ComplianceOfficer` by default (issue #145).

    `POST /change-checks` is now gated behind `require_access_role`, which has
    no local-test-bypass carve-out (PLAN.md §3.2). The dedicated denial-proof
    tests below re-monkeypatch this away for their own scenario (the same
    `monkeypatch` fixture instance, so the later call simply wins).
    """
    install_compliance_officer_grant(monkeypatch, granted=True)


@pytest.mark.usefixtures("_stub_run_log")
def test_post_change_checks_returns_200_with_run_id_and_empty_instruments() -> None:
    """A bare `POST /change-checks` (no body), no tracked instruments -> 200 with a
    non-empty run id and an empty `instruments` list (AC-BI-001 trivial, AC-BI-008
    trivial).
    """
    client = _client_with_fake(build_fake_change_check_dependencies(tracked=()))

    response = client.post("/change-checks")

    assert response.status_code == 200
    body = response.json()
    assert body["instruments"] == []
    assert body["run_id"]


@pytest.mark.usefixtures("_stub_run_log")
def test_post_change_checks_succeeds_with_compliance_officer_grant() -> None:
    """Issue #145: renamed from the pre-#145 `..._has_no_auth_dependency` -- the route is
    now gated behind `require_access_role(AccessRole.COMPLIANCE_OFFICER)`, so "no auth
    header of any kind set" alone no longer implies success; it succeeds here because
    the file's own autouse `_grant_compliance_officer` fixture presents a principal
    holding the role, not because no gate exists.
    """
    client = _client_with_fake(build_fake_change_check_dependencies(tracked=()))

    response = client.post("/change-checks")

    assert response.status_code == 200


@pytest.mark.usefixtures("_stub_run_log")
def test_post_change_checks_reports_poll_stage_buckets_via_dependency_override() -> None:
    """`app.dependency_overrides[provide_change_check_dependencies]` (mirrors
    `test_routes_restorations.py:104`'s exact override pattern): the three
    poll-stage buckets appear correctly in the JSON response (issue #73,
    PLAN.md §4 Slice 2).
    """
    tracked = (
        TrackedInstrumentNode(
            regulatory_instrument_id="id_a",
            celex="32024R2847",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
        TrackedInstrumentNode(
            regulatory_instrument_id="id_b",
            celex="32024R2848",
            instrument_type="directive",
            effective_date="2024-01-01",
        ),
        TrackedInstrumentNode(
            regulatory_instrument_id="id_c",
            celex="32024R2849",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(), polled_count=3, failed_ids=("id_a",), unconfigured_ids=("id_b",)
        ),
    )
    client = _client_with_fake(fake)

    response = client.post("/change-checks")

    assert response.status_code == 200
    outcomes = {i["instrument_id"]: i["outcome"] for i in response.json()["instruments"]}
    assert outcomes == {"id_a": "poll_failed", "id_b": "not_configured", "id_c": "current"}


@pytest.mark.usefixtures("_stub_run_log")
def test_post_change_checks_reingests_a_detected_amendment_via_dependency_override() -> None:
    """A detected amendment, resolved through a curated catalog entry, is re-ingested
    end to end through a real `TestClient` round trip (issue #73, PLAN.md §4 Slice 3):
    the response's `instruments[0]` reports `amendment_reingested` with the fresh
    re-ingest's `run_id` and a `detail` naming the outcome.
    """
    tracked = (
        TrackedInstrumentNode(
            regulatory_instrument_id="CRA-1.0",
            celex="32024R2847",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
    )
    finding = AmendmentFinding(
        regulatory_instrument_id="CRA-1.0",
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R2847C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    reingestion_result = ReingestionOutcome(
        prior_regulatory_instrument_id="CRA-0.9",
        new_regulatory_instrument_id="CRA-1.0",
        run_id="ingest-run-1",
        outcome="superseded",
        ingest_counts={},
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(finding,), polled_count=1, failed_ids=(), unconfigured_ids=()
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_result=reingestion_result,
    )
    client = _client_with_fake(fake)

    response = client.post("/change-checks")

    assert response.status_code == 200
    (instrument,) = response.json()["instruments"]
    assert instrument["instrument_id"] == "CRA-1.0"
    assert instrument["outcome"] == "amendment_reingested"
    assert instrument["reingest_run_id"] == "ingest-run-1"
    assert instrument["detail"] is not None
    assert "superseded" in instrument["detail"]


class NationalTranspositionNotSupportedError(Exception):
    """Locally-defined test double proving D10's name-matching (issue #73,
    PLAN.md §4 Slice 4) -- not an import of the real
    `ps_service.change_monitor.errors` type.
    """


@pytest.mark.usefixtures("_stub_run_log")
def test_post_change_checks_reports_skipped_via_dependency_override() -> None:
    """`trigger_reingestion` raising an exception literally named
    `NationalTranspositionNotSupportedError` -> `skipped`, through a real
    `TestClient` round trip (issue #73, PLAN.md §4 Slice 4).
    """
    tracked = (
        TrackedInstrumentNode(
            regulatory_instrument_id="CRA-1.0",
            celex="32024R2847",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
    )
    finding = AmendmentFinding(
        regulatory_instrument_id="CRA-1.0",
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R2847C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
    entry = CatalogEntry(
        celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
    )
    exc = NationalTranspositionNotSupportedError(
        "Re-ingestion of a national_transposition instrument is not supported..."
    )
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=PollReport(
            findings=(finding,), polled_count=1, failed_ids=(), unconfigured_ids=()
        ),
        catalog_entries={"32024R2847": entry},
        reingestion_results=(exc,),
    )
    client = _client_with_fake(fake)

    response = client.post("/change-checks")

    assert response.status_code == 200
    (instrument,) = response.json()["instruments"]
    assert instrument["instrument_id"] == "CRA-1.0"
    assert instrument["outcome"] == "skipped"
    assert instrument["detail"] == str(exc)


def test_post_change_checks_response_run_id_matches_the_provide_run_id_binding(
    configured_logging: Path, read_lines: ReadLines
) -> None:
    """AC-BI-008 (full, D8): the response `run_id` is the one actually bound
    on the request -- `create_change_check` passes no explicit `emitter`, so
    `run_change_check_sweep`'s `_emit_sweep`/`_emit_instrument` entries flow
    through the process-default emitter `configured_logging` installs (the
    same precedent as `test_run_context.py`'s
    `test_log_lines_emitted_during_a_request_carry_the_returned_run_id`).
    Combined with Slice 2's already-passing ps-cli-level
    `test_handle_check_prints_run_id_first_then_one_line_per_instrument`
    (run_id printed first), AC-BI-008's three clauses ("minted," "returned
    in the REST response," "printed by ps-cli") each have a passing,
    executed test.
    """
    client = _client_with_fake(build_fake_change_check_dependencies(tracked=()))

    response = client.post("/change-checks")

    assert response.status_code == 200
    returned_run_id = response.json()["run_id"]
    assert returned_run_id

    facade.reset_for_tests()  # drain + join the writer thread so the file is complete
    lines = read_lines(configured_logging)

    sweep_lines = [line for line in lines if line.get("action") == "change_check_sweep"]
    assert sweep_lines, "run_change_check_sweep emitted no change_check_sweep lines"
    assert all(line.get("run_id") == returned_run_id for line in sweep_lines)


def test_no_principal_at_all_is_denied_with_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #145, AC-BI-003/006: no verified principal -- 403, sweep never starts."""
    install_no_principal(monkeypatch)
    fake = build_fake_change_check_dependencies(tracked=())
    client = _client_with_fake(fake)

    response = client.post("/change-checks")

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "access_denied"
    assert body["error"]["message"] == "You do not have the required access role for this action."
    assert fake.read_tracked_instruments_graphs == []


def test_authenticated_user_without_compliance_officer_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified caller holding no elevated role at all -- still 403, sweep never starts."""
    install_compliance_officer_grant(monkeypatch, granted=False)
    fake = build_fake_change_check_dependencies(tracked=())
    client = _client_with_fake(fake)

    response = client.post("/change-checks")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert fake.read_tracked_instruments_graphs == []


def test_system_admin_without_explicit_grant_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: `SystemAdmin` alone does not implicitly satisfy `ComplianceOfficer`."""
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_ADMIN})
    )
    fake = build_fake_change_check_dependencies(tracked=())
    client = _client_with_fake(fake)

    response = client.post("/change-checks")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert fake.read_tracked_instruments_graphs == []


def test_system_owner_without_explicit_grant_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: `SystemOwner` alone does not implicitly satisfy `ComplianceOfficer` either."""
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_OWNER})
    )
    fake = build_fake_change_check_dependencies(tracked=())
    client = _client_with_fake(fake)

    response = client.post("/change-checks")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert fake.read_tracked_instruments_graphs == []


def _amended_fake() -> FakeChangeCheckDependencies:
    node = TrackedInstrumentNode(
        regulatory_instrument_id="CRA-1.0",
        celex="32024R2847",
        instrument_type="regulation",
        effective_date="2024-01-01",
    )
    finding = AmendmentFinding(
        regulatory_instrument_id="CRA-1.0",
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R2847C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
    return build_fake_change_check_dependencies(
        tracked=(node,),
        poll_report=PollReport(
            findings=(finding,), polled_count=1, failed_ids=(), unconfigured_ids=()
        ),
        catalog_entries={
            "32024R2847": CatalogEntry(
                celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
            )
        },
        reingestion_result=ReingestionOutcome(
            prior_regulatory_instrument_id="CRA-0.9",
            new_regulatory_instrument_id="CRA-1.0",
            run_id="ingest-run-1",
            outcome="superseded",
            ingest_counts={},
        ),
    )


@pytest.mark.usefixtures("_stub_run_log")
def test_post_change_checks_audits_the_principal() -> None:
    """Issue #195 (AC-BI-001/007): the pair's actor is the verified principal, not the bypass."""
    store = InMemoryAuditStore()
    client = _client_with_fake(_amended_fake(), audit_store=store)
    principal = compliance_officer_principal()

    response = client.post("/change-checks")

    assert response.status_code == 200
    assert [(r.action, r.outcome) for r in store.rows] == [
        ("ingestion_run.submit", "applied"),
        ("ingestion_run.complete", "applied"),
    ]
    assert {(r.actor_subject, r.actor_issuer) for r in store.rows} == {
        (principal.sub, principal.iss)
    }
    assert store.rows[0].details["trigger"] == "amendment_check"
    assert store.rows[0].resource_id != response.json()["run_id"]


@pytest.mark.usefixtures("_stub_run_log")
def test_post_change_checks_returns_503_when_audit_unavailable() -> None:
    """AC-BI-011: an unwritable opening row aborts the sweep with a 503; nothing re-ingests."""
    store = InMemoryAuditStore(
        fail_on_outcome={"applied": AuditPostgresUnavailableError("db down")}
    )
    fake = _amended_fake()
    client = _client_with_fake(fake, audit_store=store)

    response = client.post("/change-checks")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_trail_unavailable"
    assert "db down" not in response.text
    assert fake.trigger_reingestion_calls == []
