"""DB-free proof that every shipped ``curated-content/*/{baseline,native}.json`` passes
``restore.schema_allowlist.validate_serialized_graph`` (GH #104, AC-BI-005) and matches its
manifest checksum. Discovery is cross-checked against ``catalog.json`` so a moved directory can
never degrade this module to zero parametrised cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ps_service.api.catalog import load_regulation_catalog
from ps_service.export import catalog_writer
from ps_service.export.serialize import checksum_bytes, parse_serialized_graph_json
from ps_service.restore.schema_allowlist import (
    BASELINE_ALLOWED_LABELS,
    BASELINE_ALLOWED_RELATIONSHIP_TYPES,
    NATIVE_ALLOWED_LABELS,
    NATIVE_ALLOWED_RELATIONSHIP_TYPES,
    validate_serialized_graph,
)

_CURATED_CONTENT_ROOT = Path(__file__).resolve().parents[3] / "curated-content"
_INSTRUMENT_DIRS: tuple[Path, ...] = tuple(
    sorted(p.parent for p in _CURATED_CONTENT_ROOT.glob("*-*/manifest.json"))
)
_LEGS: tuple[str, ...] = ("baseline", "native")
_ALLOW_LISTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "baseline": (BASELINE_ALLOWED_LABELS, BASELINE_ALLOWED_RELATIONSHIP_TYPES),
    "native": (NATIVE_ALLOWED_LABELS, NATIVE_ALLOWED_RELATIONSHIP_TYPES),
}
_LEG_CASES = [
    pytest.param(instrument_dir, leg, id=f"{instrument_dir.name}/{leg}")
    for instrument_dir in _INSTRUMENT_DIRS
    for leg in _LEGS
]
_EXPECTED_INSTRUMENT_COUNT = 10


def test_curated_content_directories_match_the_catalog() -> None:
    catalog = load_regulation_catalog(_CURATED_CONTENT_ROOT / "catalog.json")

    assert {d.name for d in _INSTRUMENT_DIRS} == {entry.instrument_id for entry in catalog}
    assert len(_INSTRUMENT_DIRS) == _EXPECTED_INSTRUMENT_COUNT


@pytest.mark.parametrize(("instrument_dir", "leg"), _LEG_CASES)
def test_curated_artifact_leg_passes_restore_content_validation(
    instrument_dir: Path, leg: str
) -> None:
    allowed_labels, allowed_relationship_types = _ALLOW_LISTS[leg]
    graph = parse_serialized_graph_json((instrument_dir / f"{leg}.json").read_bytes())

    validate_serialized_graph(
        graph, allowed_labels=allowed_labels, allowed_relationship_types=allowed_relationship_types
    )


@pytest.mark.parametrize("instrument_dir", _INSTRUMENT_DIRS, ids=[d.name for d in _INSTRUMENT_DIRS])
def test_curated_artifact_legs_match_their_manifest_checksums(instrument_dir: Path) -> None:
    manifest = catalog_writer.read_manifest(instrument_dir)

    assert (
        checksum_bytes((instrument_dir / "baseline.json").read_bytes()) == manifest.baseline_sha256
    )
    assert checksum_bytes((instrument_dir / "native.json").read_bytes()) == manifest.native_sha256
