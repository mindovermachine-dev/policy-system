"""Only the two chained writers contain an `INSERT INTO ... audit_events` anywhere in the source.

The database chains every insert whichever code wrote it (the chain trigger), so this is not what
makes the trail tamper-evident. It is the structural statement reviewers can reason about: there
are exactly two places in `ps-service/src` that spell a write to the audit table, and a third
one appearing -- under any schema spelling -- fails here and must be justified.

Known limit: this reads source text, so a statement assembled from separate string fragments
could hide from it. The live chain tests cover the behaviour either way.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "ps_service"
_WRITERS = frozenset({"audit/store.py", "graph_gateway/system_audit.py"})
_TABLE_WRITE = re.compile(
    r"(?:INSERT\s+INTO|COPY)\s+(?:\"?\w+\"?\s*\.\s*)?\"?audit_events\b", re.IGNORECASE
)


def test_exactly_the_two_known_writers_spell_a_write_to_the_audit_table() -> None:
    writers = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if _TABLE_WRITE.search(path.read_text(encoding="utf-8"))
    }

    assert writers == _WRITERS


def test_both_writers_use_the_schema_qualified_name() -> None:
    for relative in sorted(_WRITERS):
        text = (_SRC / relative).read_text(encoding="utf-8")
        assert "INSERT INTO audit_log.audit_events" in text, relative


def test_the_pattern_catches_every_spelling_of_a_write() -> None:
    for spelling in (
        "INSERT INTO audit_events (id)",
        "insert   into audit_log.audit_events (id)",
        "INSERT INTO public.audit_events (id)",
        'INSERT INTO "audit_log"."audit_events" (id)',
        "INSERT INTO audit_log . audit_events (id)",
        "COPY audit_log.audit_events FROM STDIN",
    ):
        assert _TABLE_WRITE.search(spelling), spelling


def test_the_pattern_ignores_other_tables_and_reads() -> None:
    for text in (
        "INSERT INTO audit_events_archive (id)",
        "INSERT INTO audit_log.audit_chain_head (singleton)",
        "SELECT id FROM audit_log.audit_events",
        "-- the audit_events table",
    ):
        assert not _TABLE_WRITE.search(text), text
