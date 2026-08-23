"""AC#4/D8: emit() must never block on the sink, even while the sink is stalled."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ps_service.logging import EmitterConfig, LogEmitter
from ps_service.logging.emitter import TextSink
from ps_service.logging.models import LogEntry


class _GatedSink:
    """A `TextSink` whose `write` blocks until the test opens the gate.

    Lives in the sink, not a faked `Thread` (D8 design note) — this exercises
    the real writer thread stalled on a real write, avoiding any typing
    trickery around faking `threading.Thread` itself (M9).
    """

    def __init__(self, gate: threading.Event) -> None:
        self._gate = gate
        self._buffer: list[str] = []

    def write(self, s: str, /) -> int:
        self._gate.wait()
        self._buffer.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    @property
    def lines(self) -> list[str]:
        return self._buffer


def test_emit_when_writer_sink_blocked_then_returns_immediately(tmp_path: Path) -> None:
    gate = threading.Event()  # closed: writer's first write() call blocks
    sink = _GatedSink(gate)

    def gated_writer_factory(log_path: Path) -> TextSink:
        return sink

    emitter = LogEmitter(
        EmitterConfig(log_path=tmp_path / "test.jsonl"),
        writer_factory=gated_writer_factory,
    )

    entry_count = 20
    start = time.monotonic()
    for i in range(entry_count):
        emitter.emit(LogEntry(component="ac4", action=f"entry-{i}"))
    elapsed = time.monotonic() - start

    assert elapsed < 0.01, f"emit() blocked on the sink: {entry_count} calls took {elapsed}s"

    gate.set()  # release the writer; let it drain the queue
    emitter.flush()

    assert len(sink.lines) == entry_count
