"""The repository's curated artifacts, parsed the way Restore parses them."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from ps_service.export.serialize import parse_serialized_graph_json

if TYPE_CHECKING:
    from ps_service.export.models import SerializedGraph

CURATED_DIR = Path(__file__).resolve().parents[3] / "curated-content"
CURATED_IDS: tuple[str, ...] = tuple(
    sorted(path.parent.name for path in CURATED_DIR.glob("*/manifest.json"))
)
INTERNAL_IDS = frozenset({"ENGPRAC-1.0"})
"""The only curated instrument whose baseline carries a Policy/Standard/Control tree."""


@dataclass(frozen=True)
class CuratedArtifact:
    """One curated instrument's parsed legs."""

    instrument_id: str
    short_name: str
    native: SerializedGraph
    baseline: SerializedGraph


@lru_cache(maxsize=2)
def load_curated(instrument_id: str) -> CuratedArtifact:
    """Parse `curated-content/<id>/{native,baseline}.json` (cached: CRA's baseline is 53 MB)."""
    directory = CURATED_DIR / instrument_id
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return CuratedArtifact(
        instrument_id=instrument_id,
        short_name=str(manifest["short_name"]).lower(),
        native=parse_serialized_graph_json((directory / "native.json").read_bytes()),
        baseline=parse_serialized_graph_json((directory / "baseline.json").read_bytes()),
    )
