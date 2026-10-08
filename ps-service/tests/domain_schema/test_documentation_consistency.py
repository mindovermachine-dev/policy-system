"""AC-BI-018 / AC-BI-023: documentation no longer says the `domain_concepts`
tool returns the same text as the `psdomain://concepts` resource.

The tool renders a slim schema from code; only the RESOURCE is served verbatim.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ps_service.mcp_interface import mcp_server

_REPO = Path(__file__).resolve().parents[3]
_ARCH = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_SOLUTION_ARCH = _REPO / "docs" / "architecture" / "ps-solution-architecture.md"
_SKILL = _REPO / "ps-skills" / "ps-plugin" / "skills" / "ps-qna" / "SKILL.md"
_DOC = _REPO / "docs" / "artifacts" / "ps-domain-concepts.md"
_SEARCH_ROOTS = ("ps-service/src", "ps-service/tests", "docs", "ps-skills")
_SUFFIXES = {".py", ".md"}
_MCP_SERVER_SRC = Path(mcp_server.__file__).read_text(encoding="utf-8")

# Sentences that legitimately say "verbatim" about the RESOURCE (never the tool).
# key: (path relative to repo, exact sentence fragment) -> justification.
_RESOURCE_VERBATIM_WHITELIST = {
    (
        "ps-service/src/ps_service/mcp_interface/mcp_server.py",
        'description="The canonical PS compliance-graph vocabulary and schema, served verbatim."',
    ): "MCP resource description: the RESOURCE is the verbatim document",
    (
        "ps-service/tests/mcp_interface/test_domain_concepts_merged_tombstone.py",
        "served verbatim by the `psdomain://concepts` resource",
    ): "module docstring: these assertions read the RESOURCE text, not the tool",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tool_docstring() -> str:
    tree = ast.parse(_MCP_SERVER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "domain_concepts":
            return ast.get_docstring(node) or ""
    raise AssertionError("domain_concepts not found")


def _instructions() -> str:
    tree = ast.parse(_MCP_SERVER_SRC)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.keyword)
            and node.arg == "instructions"
            and isinstance(node.value, ast.Constant | ast.JoinedStr)
        ):
            return ast.unparse(node.value)
    raise AssertionError("instructions not found")


def test_architecture_no_longer_says_tool_returns_the_doc_verbatim() -> None:
    text = _read(_ARCH)

    assert "verbatim, deliberately with no derived" not in text
    assert "slim schema" in text
    assert "ps_service.domain_schema" in text


def test_architecture_has_a_domain_schema_component_row() -> None:
    rows = [
        line for line in _read(_SOLUTION_ARCH).splitlines() if line.startswith("| Domain Schema |")
    ]

    assert len(rows) == 1
    assert rows[0].rstrip().endswith("| ps.service.domainschema |")


def test_ps_qna_skill_does_not_claim_tool_is_the_resource_text() -> None:
    assert "the same text its `psdomain://concepts`" not in _read(_SKILL)


def test_server_instructions_and_tool_docstring_do_not_claim_sameness() -> None:
    for text in (_instructions(), _tool_docstring()):
        assert "verbatim" not in text.lower()
        assert "same text" not in text.lower()


def test_every_generated_marker_says_generated_and_do_not_edit() -> None:
    markers = [line for line in _read(_DOC).splitlines() if line.startswith("<!-- BEGIN GENERATED")]

    assert len(markers) == 21
    assert all("Generated" in m and "do not edit" in m for m in markers)


def _text_files() -> list[Path]:
    return sorted(
        path
        for root in _SEARCH_ROOTS
        for path in (_REPO / root).rglob("*")
        if path.is_file()
        and path.suffix in _SUFFIXES
        and "__pycache__" not in path.parts
        and path != Path(__file__).resolve()
    )


def _sentences(text: str, suffix: str) -> list[str]:
    if suffix == ".py":  # code has no sentence structure: a 3-line window is "near"
        lines = text.splitlines()
        return ["\n".join(lines[i : i + 3]) for i in range(len(lines))]
    return [s for s in re.split(r"(?<=[.;!?])\s+|\n\s*\n|\n(?=\|)", text) if s.strip()]


def _is_whitelisted(relative: str, sentence: str) -> bool:
    return any(
        relative == path and fragment in sentence for path, fragment in _RESOURCE_VERBATIM_WHITELIST
    )


def test_no_text_describes_the_tool_as_verbatim_or_the_same_text_as_the_resource() -> None:
    offenders: list[str] = []
    for path in _text_files():
        relative = path.relative_to(_REPO).as_posix()
        for sentence in _sentences(_read(path), path.suffix):
            flat = " ".join(sentence.split())
            lowered = flat.lower()
            tool_verbatim = "verbatim" in lowered and re.search(r"domain[_ ]concepts", lowered)
            same_text = "same text" in lowered and "psdomain://concepts" in lowered
            if (tool_verbatim or same_text) and not _is_whitelisted(relative, flat):
                offenders.append(f"{relative}: {flat[:120]}")

    assert not offenders, "\n".join(offenders)
