"""Tests for `mcp_server.main()` -- the stdio entry point (PLAN_REVIEWED.md
§6, Batch 6; Q3, AC-015).

`main()` must install the process-wide default `LogEmitter` via `configure()`
and only then hand control to `server.run()`, which is left at its `"stdio"`
default -- no positional transport arg, no `transport=` kwarg (AC-015: no
network listener).

Hand-written spies, per repo convention -- no `unittest.mock`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    import pytest


def test_main_calls_configure_then_run_with_no_transport_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    run_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def configure_spy() -> None:
        order.append("configure")

    def run_spy(*args: object, **kwargs: object) -> None:
        order.append("run")
        run_calls.append((args, kwargs))

    monkeypatch.setattr(mcp_server, "configure", configure_spy)
    monkeypatch.setattr(mcp_server.server, "run", run_spy)

    mcp_server.main()

    assert order == ["configure", "run"]
    assert len(run_calls) == 1
    positional, keyword = run_calls[0]
    assert positional == ()
    assert "transport" not in keyword
