"""Shared typing helpers for the `company_merge` test package.

`company_merge` is the one `ps-service/tests` subdirectory that is an importable
package (it has an `__init__.py`), so its per-file test doubles can share a
single set of structural types from here instead of redeclaring them. Only the
call-shapes that basedpyright strict needs annotated live here; the concrete
hand-written doubles (`_ScriptedCallEmbedding`, `_FakeGraph`, ...) stay local to
each test module because they are already fully annotated and their scripted
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
