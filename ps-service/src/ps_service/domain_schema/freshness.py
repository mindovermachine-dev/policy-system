"""Keep the generated artifacts in step with `DOMAIN_SCHEMA`.

`check_artifacts` regenerates every artifact in memory and reports each one that differs from
the committed bytes. `write_docs` regenerates the marked regions of the canonical
domain-concepts document and writes the packaged copy with the same bytes. Nothing here ever
writes the intake JSON Schema files (D13): they are only compared, never written.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.domain_schema.definition import DOMAIN_SCHEMA
from ps_service.domain_schema.errors import DomainSchemaError, MissingArtifactError
from ps_service.domain_schema.intake import generate_intake_schema
from ps_service.domain_schema.render_doc import find_region_ids, render_regions, replace_regions

if TYPE_CHECKING:
    from pathlib import Path

CANONICAL_DOC = "docs/artifacts/ps-domain-concepts.md"
PACKAGED_DOC = "ps-service/src/ps_service/mcp_interface/ps-domain-concepts.md"
INTAKE_COPIES = (
    "docs/artifacts/schemas/internal-regulation-intake.v1.schema.json",
    "ps-service/src/ps_service/api/schemas/internal_regulation_intake_v1.schema.json",
    "ps-cli/src/ps_cli/schemas/internal_regulation_intake_v1.schema.json",
)


@dataclass(frozen=True, slots=True)
class Drift:
    """One artifact that no longer matches what the schema generates."""

    artifact: str
    diff: str


def render_canonical_doc(canonical_text: str) -> str:
    """Return `canonical_text` with every generated region regenerated from the schema."""
    return replace_regions(canonical_text, render_regions(DOMAIN_SCHEMA))


def write_docs(repo_root: Path) -> None:
    """Regenerate the canonical document, then write the packaged copy with the same bytes."""
    canonical = repo_root / CANONICAL_DOC
    text = render_canonical_doc(canonical.read_bytes().decode("utf-8"))
    for path in (canonical, repo_root / PACKAGED_DOC):
        path.write_bytes(text.encode("utf-8"))


def _read(repo_root: Path, artifact: str) -> str:
    try:
        return (repo_root / artifact).read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError as error:
        raise MissingArtifactError(artifact) from error


def _diff(artifact: str, committed: str, expected: str) -> Drift | None:
    if committed == expected:
        return None
    lines = difflib.unified_diff(
        committed.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=f"{artifact} (committed)",
        tofile=f"{artifact} (generated)",
    )
    return Drift(artifact, "".join(lines))


def _canonical_doc_drift(committed: str) -> Drift | None:
    expected_regions = tuple(render_regions(DOMAIN_SCHEMA))
    try:
        if set(find_region_ids(committed)) != set(expected_regions):
            return Drift(CANONICAL_DOC, "generated regions differ from the schema's regions\n")
        return _diff(CANONICAL_DOC, committed, render_canonical_doc(committed))
    except DomainSchemaError as error:
        return Drift(CANONICAL_DOC, f"{error}\n")


def check_artifacts(repo_root: Path) -> tuple[Drift, ...]:
    """Return one `Drift` per artifact that differs from its regeneration (empty when fresh).

    Raises:
        MissingArtifactError: when any compared artifact is absent under `repo_root`.
    """
    canonical = _read(repo_root, CANONICAL_DOC)
    packaged = _read(repo_root, PACKAGED_DOC)
    intake = generate_intake_schema(DOMAIN_SCHEMA)
    drifts = [
        _canonical_doc_drift(canonical),
        _diff(PACKAGED_DOC, packaged, canonical),
        *(_diff(copy, _read(repo_root, copy), intake) for copy in INTAKE_COPIES),
    ]
    return tuple(drift for drift in drifts if drift is not None)
