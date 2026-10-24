"""Content lint: the architecture and operations docs describe the six connection bounds.

Anchors are headings and the variable names, never line numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_OPS = _REPO / "docs" / "artifacts" / "operations-guide.md"
_CA = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_OPS_SECTION = "### Dependency connection and socket timeouts"

_DEFAULTS = {
    "PS_FALKORDB_CONNECT_TIMEOUT_SECONDS": "5",
    "PS_FALKORDB_SOCKET_TIMEOUT_SECONDS": "30",
    "PS_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS": "5",
    "PS_STATE_POSTGRES_STATEMENT_TIMEOUT_MS": "30000",
    "PS_PASSKEYSIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS": "5",
    "PS_PASSKEYSIGNING_POSTGRES_STATEMENT_TIMEOUT_MS": "30000",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    level = len(heading) - len(heading.lstrip("#"))
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(rf"^#{{1,{level}}} ", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _rows(text: str, variable: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|") and variable in line]


@pytest.mark.parametrize(("variable", "default"), sorted(_DEFAULTS.items()))
def test_the_architecture_doc_row_of_each_variable_names_its_default(
    variable: str, default: str
) -> None:
    rows = _rows(_read(_CA), variable)

    assert rows, f"no table row names {variable}"
    assert any(f"| {default} |" in row for row in rows)


def test_the_architecture_doc_states_the_socket_versus_query_timeout_constraint() -> None:
    rows = " ".join(_rows(_read(_CA), "PS_FALKORDB_SOCKET_TIMEOUT_SECONDS"))

    assert "PS_QUERY_TIMEOUT_MS" in rows
    assert "refused at load" in rows
    assert "GraphUnavailableError" in rows
    assert "falkordb" in rows


def test_the_architecture_doc_gives_each_failure_symptom() -> None:
    text = _read(_CA)

    assert "QueryCanceled" in text
    assert "state_postgres" in text
    assert "passkey_signing_postgres" in text
    assert "ambiguous" in text.lower()


def test_the_query_engine_paragraph_points_at_the_socket_timeout() -> None:
    line = next(
        line for line in _read(_CA).splitlines() if line.startswith("- Both a query timeout")
    )

    assert "PS_FALKORDB_SOCKET_TIMEOUT_SECONDS" in line


def test_the_operations_guide_section_names_every_variable_and_default() -> None:
    section = _section(_read(_OPS), _OPS_SECTION)

    for variable, default in _DEFAULTS.items():
        rows = _rows(section, variable)
        assert rows, f"the section has no row for {variable}"
        assert any(f"| {default} |" in row for row in rows)


def test_the_operations_guide_section_explains_the_symptoms() -> None:
    section = _section(_read(_OPS), _OPS_SECTION)

    for needle in (
        "unhealthy_dependencies",
        "/ready",
        "503",
        "GraphUnavailableError",
        "falkordb",
        "state_postgres",
        "passkey_signing_postgres",
        "verify_chain",
        "PS_QUERY_TIMEOUT_MS",
    ):
        assert needle in section, needle


def test_the_troubleshooting_table_has_the_connection_bound_rows() -> None:
    table = _section(_read(_OPS), "## Troubleshooting / FAQ")

    for needle in (
        "PS_FALKORDB_SOCKET_TIMEOUT_SECONDS",
        "PS_STATE_POSTGRES_STATEMENT_TIMEOUT_MS",
        "PS_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS",
        "PS_PASSKEYSIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS",
    ):
        assert any(line.startswith("|") and needle in line for line in table.splitlines()), needle


def test_the_operations_guide_says_the_chart_does_not_expose_the_variables() -> None:
    section = _section(_read(_OPS), _OPS_SECTION)

    assert "does not expose" in section
