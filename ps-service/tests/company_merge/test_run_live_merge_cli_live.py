"""Live wiring smoke test for `tools/company-merge/run_live_merge.py` (issue #28,
CHANGES.md #2).

`@pytest.mark.falkordb_live`, **ZERO writes**: `test_run_live_merge_cli.py`'s own unit suite
fakes every collaborator (`graph_provider`, `merge_fn`, `clock`), so nothing before this test
ever exercised the runner's REAL `select_graph`/`load_config`/`bind_run_context`/
`Logging.configure()` wiring against a real FalkorDB instance -- CHANGES.md #2's "integration
wiring never exercised live" gap. This test closes it: real `graph_provider` (real
`select_graph` against real FalkorDB), real `ps_service.config.load_config()`-resolved
`similarity_threshold`/`embed_model`, real `bind_run_context`/emitter (`Logging.configure()`)
-- and ONLY `merge_fn` faked, raising immediately on its FIRST call (recording the exact real
args it was invoked with) BEFORE any write of any kind occurs. No `--approval-file` points at
either of the two human-authored `APPROVAL_LIVE_MERGE_*.md` files this issue's real slice 6/8
runs will consume -- a fresh, valid approval fixture lives under `tmp_path` instead, so the
approval-gate code path is exercised too without touching anything under this issue's own
tracker directory.

Mirrors `test_check_baseline_preconditions_cli_live.py`'s `importlib`-by-path loading and
`test_run_live_merge_cli.py`'s `_preserve_atexit_registered_guard` fixture (this test's own
`main()` call reaches the real `Logging.configure()` before `merge_fn` raises).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.config import load_config
from ps_service.logging import facade
from ps_service.logging.run_context import current_run_id

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from ps_service.company_merge.falkordb_client import GraphHandle

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "tools" / "company-merge" / "run_live_merge.py"
_MODULE_NAME = "_run_live_merge_cli_under_test_live"

# A fresh, valid approval fixture -- lives under tmp_path only, never under this issue's own
# tracker directory, and is NOT one of the two human-authored
# APPROVAL_LIVE_MERGE_INITIAL.md/APPROVAL_LIVE_MERGE_RERUN.md files (this suite never creates
# those, per issue #28's own approval-gate design: only a human authors those, by hand).
_VALID_APPROVAL_FIELDS: dict[str, str] = {
    "approved_by": "tete@cartman.dk",
    "approved_at": "2026-09-13T10:00:00Z",
    "scope": "CRA-1.0,GDPR-1.0,NIS2-1.0",
    "target_graph": "policy_system",
    "precondition_check_result": "wiring-smoke-test fixture -- not a real precondition run",
    "confirmation_phrase": "I APPROVE THE LIVE MERGE INTO policy_system",
}


def _node_count(graph: GraphHandle) -> int:
    rows = cast("list[list[object]]", graph.query("MATCH (n) RETURN count(n)").result_set)
    return cast("int", rows[0][0])


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


@pytest.fixture(autouse=True)
def _preserve_atexit_registered_guard() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """See `test_run_live_merge_cli.py`'s identical fixture docstring."""
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


class _RaisingOnFirstCallMergeFn:
    """The ONE faked collaborator: records the exact real args its first call receives, then
    raises immediately -- BEFORE `merge_baseline_graph`'s own first real action would ever run,
    so this test issues literally zero writes anywhere (not to any `{short}_baseline` graph,
    not to `policy_system`).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: object,
        single_tenant_graph: object,
        embed_model: str,
        similarity_threshold: float,
        call_embedding: object | None = None,
        emitter: object | None = None,
    ) -> object:
        del call_embedding, emitter
        self.calls.append(
            {
                "regulatory_instrument_id": regulatory_instrument_id,
                "baseline_graph": baseline_graph,
                "single_tenant_graph": single_tenant_graph,
                "embed_model": embed_model,
                "similarity_threshold": similarity_threshold,
                "run_id": current_run_id(),
            }
        )
        message = "wiring smoke test: refuse before any write occurs"
        raise RuntimeError(message)


@pytest.mark.falkordb_live
def test_real_wiring_reaches_merge_fn_with_real_args_and_issues_zero_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real `load_config()` resolution is exercised (never a fake ServiceConfig injected) --
    # these values are set here only so the resolution has something real to resolve to,
    # regardless of the ambient shell/.env state; `merge_fn` never uses them for a real
    # embedding/write call, since it raises before doing anything with them.
    monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.85")
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")

    approval_path = tmp_path / "approval.md"
    approval_path.write_text(
        "\n".join(f"{key}: {value}" for key, value in _VALID_APPROVAL_FIELDS.items()) + "\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    cli_module = _load_cli_module()
    merge_fn = _RaisingOnFirstCallMergeFn()

    # --- Safety check (BEFORE): real policy_system's total node count. ---
    config = load_config()
    db = connect_from_config(config)
    real_single_tenant_graph = select_graph(db, "policy_system")
    node_count_before = _node_count(real_single_tenant_graph)

    exit_code = cli_module.main(
        [
            "--approval-file",
            str(approval_path),
            "--run-kind",
            "initial",
            "--report-path",
            str(report_path),
        ],
        merge_fn=merge_fn,
        # graph_provider/clock deliberately left at their REAL defaults -- this is the whole
        # point of this test, per CHANGES.md #2.
    )

    # --- Safety check (AFTER): must be byte-for-byte the same. ---
    node_count_after = _node_count(real_single_tenant_graph)
    assert node_count_after == node_count_before, (
        "this wiring smoke test must never write into the real policy_system graph"
    )

    assert exit_code != 0, "merge_fn always raises in this test -- the run must not succeed"
    assert len(merge_fn.calls) == 1, (
        f"merge_fn must be called exactly once (raising on its FIRST call, before any second "
        f"call), got {len(merge_fn.calls)} call(s)"
    )
    first_call = merge_fn.calls[0]
    assert first_call["regulatory_instrument_id"] == "CRA-1.0", (
        "CRA-1.0 is first in run_live_merge.py's fixed run order"
    )
    assert first_call["embed_model"] == "azure/text-embedding-3-large"
    assert first_call["similarity_threshold"] == pytest.approx(0.85)
    assert first_call["run_id"] is not None, "bind_run_context must have bound a real run_id"
    assert first_call["baseline_graph"] is not None
    assert first_call["single_tenant_graph"] is not None

    # A failed run must never consume the approval file.
    assert approval_path.exists()
    assert not list(tmp_path.glob("approval.md.consumed-*"))
