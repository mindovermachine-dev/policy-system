"""`write-docs` regenerates the canonical document and its packaged copy (S12, F4)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ps_service.domain_schema.__main__ import build_parser, main
from ps_service.domain_schema.freshness import (
    CANONICAL_DOC,
    PACKAGED_DOC,
    write_docs,
)
from ps_service.domain_schema.render_doc import begin_marker

_REPO = Path(__file__).resolve().parents[3]


def _copy_tree(tmp_path: Path) -> Path:
    for relative in (CANONICAL_DOC, PACKAGED_DOC):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REPO / relative, target)
    return tmp_path


def _stale_catalog(root: Path) -> None:
    path = root / CANONICAL_DOC
    path.write_text(
        path.read_text(encoding="utf-8").replace("| `DEFINES` |", "| `DEFINES_STALE` |", 1),
        encoding="utf-8",
    )


def test_write_docs_repairs_a_stale_region_and_writes_identical_copies(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    _stale_catalog(root)

    write_docs(root)

    canonical = (root / CANONICAL_DOC).read_bytes()
    assert b"DEFINES_STALE" not in canonical
    assert (root / PACKAGED_DOC).read_bytes() == canonical


def test_write_docs_on_the_committed_tree_changes_nothing(tmp_path: Path) -> None:
    """The committed document is already fresh, so regeneration is a no-op."""
    root = _copy_tree(tmp_path)
    before = (root / CANONICAL_DOC).read_bytes()

    write_docs(root)

    assert (root / CANONICAL_DOC).read_bytes() == before
    assert before.count(begin_marker("edge-catalog").encode()) == 1


def test_write_docs_subcommand_runs_against_repo_root(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    _stale_catalog(root)

    assert main(["write-docs", "--repo-root", str(root)]) == 0

    assert b"DEFINES_STALE" not in (root / CANONICAL_DOC).read_bytes()


def test_write_docs_is_a_known_subcommand_and_write_intake_is_not() -> None:
    parser = build_parser()

    assert parser.parse_args(["write-docs"]).command == "write-docs"
    with pytest.raises(SystemExit):
        parser.parse_args(["write-intake"])
