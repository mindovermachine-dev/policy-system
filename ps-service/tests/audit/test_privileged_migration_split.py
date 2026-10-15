"""The audit integrity migration survives `split_statements` intact (one file, one transaction).

`split_statements` splits on `;` and keeps `$$`/`$tag$` bodies whole, but it knows nothing about
`--` comments or single-quoted literals. A `;` or `$` in a comment, or a `;` inside a quoted
literal, would split a statement wrongly, and the damage would only show when the privileged
runner applies the file. These checks read the real file, so they fail the moment an edit
introduces such a hazard; they run unchanged in every later slice that grows the file. They also
guard the `CHECK (... ~ '^[0-9a-f]{64}$')` literals, which are safe only while no second `$`
follows them on the same dollar-quote scan.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ps_service.audit import PRIVILEGED_MIGRATIONS_DIR
from ps_service.persistence.migration_runner import discover_migration_files, split_statements

if TYPE_CHECKING:
    from pathlib import Path

_DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_]?[A-Za-z0-9_]*\$")
_QUOTED_LITERAL = re.compile(r"'(?:[^']|'')*'")
_BLOCK_TOKEN = re.compile(r"\bEND\s+(?:IF|LOOP|CASE)\b|\bBEGIN\b|\bCASE\b|\bEND\b", re.IGNORECASE)
_TOP_LEVEL_KEYWORDS = frozenset(
    {"ALTER", "COMMENT", "CREATE", "DO", "DROP", "GRANT", "INSERT", "REVOKE", "SELECT", "SET"}
)
_BODY_OPENERS = frozenset({"CREATE", "DO"})


def _migration_files() -> list[Path]:
    files = discover_migration_files(PRIVILEGED_MIGRATIONS_DIR)
    assert files, "the audit integrity migration file is missing"
    return files


def _statements(path: Path) -> list[str]:
    return list(split_statements(path.read_text(encoding="utf-8")))


def _first_word(statement: str) -> str:
    """First word of the statement's code, skipping the `--` comment lines before it."""
    code = "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--"))
    return code.split(None, 1)[0]


def test_every_dollar_quoted_body_is_exactly_one_statement_starting_with_create_or_do() -> None:
    for path in _migration_files():
        for statement in _statements(path):
            quotes = _DOLLAR_QUOTE.findall(statement)
            if not quotes:
                continue
            assert len(quotes) == 2, f"{path.name}: a body is split or two bodies share a statement"
            assert quotes[0] == quotes[1]
            assert _first_word(statement) in _BODY_OPENERS, statement[:80]


def test_every_statement_starts_with_an_uppercase_top_level_keyword() -> None:
    for path in _migration_files():
        for statement in _statements(path):
            word = _first_word(statement)
            assert word in _TOP_LEVEL_KEYWORDS, f"{path.name}: fragment starts with {word!r}"


def test_no_statement_ends_in_the_middle_of_a_begin_end_block() -> None:
    for path in _migration_files():
        for statement in _statements(path):
            depth = 0
            for token in _BLOCK_TOKEN.findall(statement):
                words = token.upper().split()
                if words[0] in {"BEGIN", "CASE"}:
                    depth += 1
                elif words in (["END"], ["END", "CASE"]):
                    depth -= 1
                assert depth >= 0, f"{path.name}: END without BEGIN in {statement[:80]!r}"
            assert depth == 0, f"{path.name}: unbalanced BEGIN/END in {statement[:80]!r}"


def test_comments_hold_no_semicolon_or_dollar_and_literals_hold_no_semicolon() -> None:
    for path in _migration_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for literal in _QUOTED_LITERAL.findall(line):
                assert ";" not in literal, f"{path.name}:{number}: ';' inside a quoted literal"
            code_and_comment = _QUOTED_LITERAL.sub("''", line)
            if "--" in code_and_comment:
                comment = code_and_comment.split("--", 1)[1]
                assert ";" not in comment, f"{path.name}:{number}: ';' inside a comment"
                assert "$" not in comment, f"{path.name}:{number}: '$' inside a comment"
