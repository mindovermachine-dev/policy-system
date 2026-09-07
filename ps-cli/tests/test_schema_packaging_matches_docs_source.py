"""D7: `ps-cli`'s packaged schema copy must stay byte-identical to its docs source.

`docs/artifacts/schemas/internal-regulation-intake.v1.schema.json` is the
single authored source of truth (D7); `ps_cli/schemas/internal_regulation_
intake_v1.schema.json` is a vendored copy (the workspace's "no shared
internal package between ps-service and ps-cli" rule forbids importing the
docs copy at runtime). This test is the only thing that would catch the two
drifting apart -- there is no other mechanism keeping them in sync.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS_SOURCE = (
    _REPO_ROOT / "docs" / "artifacts" / "schemas" / "internal-regulation-intake.v1.schema.json"
)
_PACKAGED_COPY = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "ps_cli"
    / "schemas"
    / "internal_regulation_intake_v1.schema.json"
)


def test_packaged_schema_copy_is_byte_identical_to_docs_source() -> None:
    """`ps_cli/schemas/internal_regulation_intake_v1.schema.json` == docs source, byte-for-byte."""
    assert _PACKAGED_COPY.read_bytes() == _DOCS_SOURCE.read_bytes()
