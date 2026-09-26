"""Tests for ps_cli.modules.handlers.

`handle_ingest_document`'s full happy-path wiring (PLAN.md §3 Increment 15) is
deliberately not unit-tested at this layer -- its coverage there is exactly two
`cli.run()`-level tests in `ps-cli/tests/test_cli.py`, which prove the full
wiring (parser -> dispatch -> handler -> client) end to end. Issue #54's S1
slice adds exactly one test at this layer for the new local-validation-before-
any-network-call behavior (`validate_local_seed_file`, D3/B4) -- distinct from
happy-path wiring, and the one place a fake client can assert zero calls were
made.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ps_cli.errors import PsCliError
from ps_cli.models import (
    ExportResult,
    IngestionResult,
    ReadinessResult,
    StageOutcome,
)
from ps_cli.modules.handlers import (
    handle_get_health,
    handle_ingest_document,
)

if TYPE_CHECKING:
    from pathlib import Path


class _UnusedPsServiceClientMethods:
    """Base for hand-written `PsServiceClientProtocol` fakes below (PLAN.md §1 D10).

    Every method raises unless a subclass overrides it -- a fake overrides
    only the method its own test actually exercises, so an unexpected call
    to any other method fails loudly and immediately, the same guarantee
    the old `cast()`-narrowed partial fakes gave for free, now that the
    type checker requires each fake to structurally satisfy all three
    `PsServiceClientProtocol` methods rather than just the one under test.
    """

    def check_health(self) -> str:
        """Fail: this test's fake does not expect `check_health()` to be called."""
        raise AssertionError("check_health must not be called in this test")

    def get_service_version(self) -> str:
        """Fail: this test's fake does not expect `get_service_version()` to be called."""
        raise AssertionError("get_service_version must not be called in this test")

    def check_readiness(self) -> ReadinessResult:
        """Fail: this test's fake does not expect `check_readiness()` to be called."""
        raise AssertionError("check_readiness must not be called in this test")

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """Fail: this test's fake does not expect `ingest_internal()` to be called."""
        msg = f"ingest_internal must not be called in this test (content={content!r})"
        raise AssertionError(msg)

    def export_instrument(self, instrument_id: str) -> ExportResult:
        """Fail: this test's fake does not expect `export_instrument()` to be called."""
        msg = f"export_instrument must not be called in this test (instrument_id={instrument_id!r})"
        raise AssertionError(msg)


def test_handle_ingest_document_validates_locally_before_any_http_call(
    tmp_path: Path,
) -> None:
    """A schema-invalid local fixture is rejected before `client.ingest_internal()` is called.

    `client` here is a bare `_UnusedPsServiceClientMethods` instance --
    its `ingest_internal` raises `AssertionError` if ever called, so the
    absence of that failure (only `PsCliError` is raised, from local
    validation) is itself the proof of zero HTTP calls (issue #54 D3/B4,
    AC-BI-019).
    """
    invalid_document: dict[str, object] = {
        "nodes": [],
        "edges": [],
        "graph_name": "policy_system",
    }
    document_path = tmp_path / "bad-seed.json"
    document_path.write_text(json.dumps(invalid_document), encoding="utf-8")
    client = _UnusedPsServiceClientMethods()

    with pytest.raises(PsCliError) as excinfo:
        handle_ingest_document(document_path, client)

    assert "graph_name" in excinfo.value.msg or "additional" in excinfo.value.msg.lower()


_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT: dict[str, object] = {
    "nodes": [
        {
            "label": "RegulatoryInstrument",
            "id": "ENGPRAC-3.0",
            "properties": {
                "title": "Engineering Practices Policy",
                "source_type": "internal",
                "effective_date": "2026-08-01",
                "version": "3.0",
                "status": "active",
            },
        }
    ],
    "edges": [],
}


class _FakeInternalIngestClient(_UnusedPsServiceClientMethods):
    """Hand-written fake implementing `ingest_internal()`'s signature.

    Scripted to return a fixed `IngestionResult`, used only for issue #35
    Slice 5's pending_reviews stage-line tests, which need a specific
    `IngestionResult` to exercise `handle_ingest_document`'s conditional-append
    idiom for `pending_reviews`.
    """

    def __init__(self, *, result: IngestionResult) -> None:
        """Script this fake's ingest_internal() outcome."""
        self._result = result

    def check_readiness(self) -> ReadinessResult:
        """Report a fully-healthy target -- the pre-flight check must let this through."""
        return ReadinessResult(status="ready", unhealthy_dependencies=[])

    def ingest_internal(self, content: dict[str, object]) -> IngestionResult:
        """Return the scripted result, ignoring `content`."""
        del content
        return self._result


def test_handle_ingest_document_surfaces_nonzero_pending_reviews(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #35, Slice 5 (AC-BI-010): `handle_ingest_document`'s own
    stage-print loop appends `" (pending_reviews: {n})"` when the merge
    stage's summary reports a nonzero count (CHANGES.md C2).
    """
    document_path = tmp_path / "seed.json"
    document_path.write_text(json.dumps(_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT), encoding="utf-8")
    result = IngestionResult(
        run_id="run-internal-004",
        regulatory_instrument_id="ri-engprac",
        source="internal",
        stages=[
            StageOutcome(
                stage="merge",
                status="succeeded",
                summary={
                    "obligations": 1,
                    "canonical_capabilities": 1,
                    "near_misses": 1,
                    "pending_reviews": 1,
                },
            ),
        ],
    )
    client = _FakeInternalIngestClient(result=result)

    handle_ingest_document(document_path, client)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert "merge: succeeded (pending_reviews: 1)" in lines


def test_handle_ingest_document_stage_line_byte_identical_when_no_pending_reviews(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-010 corollary for the internal-seed pipeline: pending_reviews ==
    0 (or absent) prints exactly the pre-existing line, unchanged.
    """
    document_path = tmp_path / "seed.json"
    document_path.write_text(json.dumps(_MINIMAL_VALID_INTERNAL_SEED_DOCUMENT), encoding="utf-8")
    result = IngestionResult(
        run_id="run-internal-005",
        regulatory_instrument_id="ri-engprac",
        source="internal",
        stages=[
            StageOutcome(stage="internal_ingestion", status="succeeded", summary={"roles": 1}),
            StageOutcome(
                stage="merge",
                status="succeeded",
                summary={
                    "obligations": 1,
                    "canonical_capabilities": 1,
                    "near_misses": 0,
                    "pending_reviews": 0,
                },
            ),
        ],
    )
    client = _FakeInternalIngestClient(result=result)

    handle_ingest_document(document_path, client)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert "internal_ingestion: succeeded" in lines
    assert "merge: succeeded" in lines


class _FakeHealthClient(_UnusedPsServiceClientMethods):
    """Hand-written fake implementing `check_health()`/`check_readiness()`'s signatures."""

    def __init__(self, *, health_status: str, readiness: ReadinessResult) -> None:
        """Script this fake's `check_health()`/`check_readiness()` return values."""
        self._health_status = health_status
        self._readiness = readiness

    def check_health(self) -> str:
        """Return the scripted health status."""
        return self._health_status

    def check_readiness(self) -> ReadinessResult:
        """Return the scripted readiness result."""
        return self._readiness


def test_handle_get_health_prints_reachable_alive_ready_on_happy_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fully healthy, fully ready target prints exactly the three summary lines (D10/D11)."""
    fake = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="ready", unhealthy_dependencies=[]),
    )

    handle_get_health(fake)

    captured = capsys.readouterr()
    assert captured.out == "reachable: yes\nhealth: alive\nready: ready\n"
    assert captured.err == ""


def test_handle_get_health_raises_ps_cli_error_naming_unhealthy_dependencies_when_not_ready() -> (
    None
):
    """A not-ready target with a named unhealthy dependency raises PsCliError with that name
    in the hint (AC-BI-001 handler half, AC-BI-007).
    """
    fake = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="not_ready", unhealthy_dependencies=["falkordb"]),
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_get_health(fake)

    assert "not ready" in excinfo.value.msg
    assert excinfo.value.hint is not None
    assert "falkordb" in excinfo.value.hint


def test_handle_get_health_raises_ps_cli_error_with_no_hint_when_no_dependency_named() -> None:
    """A not-ready target with no unhealthy dependency named (§0.3's line-970 nuance) still
    raises, but with no hint -- proving the hint is genuinely conditional, not always-present.
    """
    fake = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="not_ready", unhealthy_dependencies=[]),
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_get_health(fake)

    assert excinfo.value.hint is None


def test_handle_get_health_not_ready_message_never_says_could_not_reach() -> None:
    """The not-ready error's rendered text is textually distinct from the unreachable-target
    error's wording (AC-BI-008), proven by an executed assertion, not just inspection.
    """
    fake = _FakeHealthClient(
        health_status="alive",
        readiness=ReadinessResult(status="not_ready", unhealthy_dependencies=["falkordb"]),
    )

    with pytest.raises(PsCliError) as excinfo:
        handle_get_health(fake)

    assert "Could not reach" not in str(excinfo.value)
