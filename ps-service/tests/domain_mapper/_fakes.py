"""Shared typing helpers for the `domain_mapper` test package.

`domain_mapper` is an importable package (it has an `__init__.py`), so its
per-file test doubles can share a single set of structural types from here
instead of redeclaring them. Mirrors `tests/company_merge/_fakes.py`.

Only the call-shapes that basedpyright strict needs annotated live here — in
particular the root `tests/conftest.py` `make_emitter` / `read_lines` factory
fixtures, whose return types basedpyright cannot see through at the call site
(they return a nested closure). The concrete hand-written doubles
(`_FakeBaselineGraph`, `_scripted_sequential_call_completion`, ...) stay local
to each test module: they are already fully annotated and their scripted
behaviour differs per test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.logging import LogEmitter
    from ps_service.logging.emitter import TextSink


class MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(
        self, *, filename: str = ..., fallback: TextSink | None = ...
    ) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...
