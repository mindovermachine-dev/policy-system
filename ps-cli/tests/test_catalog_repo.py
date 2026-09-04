"""Tests for ps_cli.catalog_repo: read_catalog() / read_artifact() (Slice 7.1).

Local filesystem only -- no PS Service, no network, no httpx -- matching D13's
"catalog list needs no PS Service connection at all" requirement and D5's
"ps-cli reads the artifact locally" design. `tmp_path` fixtures below mirror
the real curated-content layout (CHANGES2.md §3.7): a repo-root `catalog.json`
plus one folder per instrument holding `manifest.json`/`baseline.json`/
`native.json`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ps_cli.catalog_repo import read_artifact, read_catalog
from ps_cli.errors import PsCliError

if TYPE_CHECKING:
    from pathlib import Path

_MANIFEST_BODY: dict[str, object] = {
    "instrument_id": "CRA-1.0",
    "celex": "32024R2847",
    "title": "Cyber Resilience Act",
    "short_name": "CRA",
    "version": "1.0",
    "source_type": "external",
    "jurisdiction": "EU",
    "schema_version": "1.0.0",
    "exported_at": "2026-09-04T00:00:00Z",
    "baseline_sha256": "a" * 64,
    "native_sha256": "b" * 64,
}


def _write_catalog(repo_path: Path, entries: list[dict[str, object]]) -> None:
    (repo_path / "catalog.json").write_text(json.dumps(entries), encoding="utf-8")


def _write_instrument(
    repo_path: Path,
    instrument_id: str,
    *,
    manifest: dict[str, object],
    baseline: bytes = b'{"nodes": [], "edges": []}',
    native: bytes = b'{"nodes": [], "edges": []}',
) -> None:
    instrument_dir = repo_path / instrument_id
    instrument_dir.mkdir(parents=True)
    (instrument_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (instrument_dir / "baseline.json").write_bytes(baseline)
    (instrument_dir / "native.json").write_bytes(native)


class TestReadCatalog:
    """`read_catalog(repo_path)` parses `catalog.json` into `CuratedInstrumentEntry` objects."""

    def test_parses_every_entry_in_file_order(self, tmp_path: Path) -> None:
        """Every catalog.json row parses into a CuratedInstrumentEntry, in file order."""
        _write_catalog(
            tmp_path,
            [
                {
                    "instrument_id": "CRA-1.0",
                    "celex": "32024R2847",
                    "title": "Cyber Resilience Act",
                    "source_type": "external",
                    "jurisdiction": "EU",
                    "short_name": "CRA",
                    "version": "1.0",
                },
                {
                    "instrument_id": "ENGPRAC-1.0",
                    "celex": None,
                    "title": "Engineering Practices",
                    "source_type": "internal",
                    "jurisdiction": None,
                    "short_name": "ENGPRAC",
                    "version": "1.0",
                },
            ],
        )

        entries = read_catalog(tmp_path)

        assert len(entries) == 2
        assert entries[0].instrument_id == "CRA-1.0"
        assert entries[0].title == "Cyber Resilience Act"
        assert entries[0].source_type == "external"
        assert entries[0].jurisdiction == "EU"
        assert entries[1].instrument_id == "ENGPRAC-1.0"
        assert entries[1].source_type == "internal"
        assert entries[1].jurisdiction is None

    def test_missing_catalog_json_raises_ps_cli_error_naming_the_path(self, tmp_path: Path) -> None:
        """A repo_path with no catalog.json at all raises PsCliError naming the path."""
        with pytest.raises(PsCliError) as excinfo:
            read_catalog(tmp_path)

        assert str(tmp_path / "catalog.json") in excinfo.value.msg

    def test_non_list_catalog_json_raises_generic_ps_cli_error(self, tmp_path: Path) -> None:
        """A catalog.json that is not a JSON array raises a generic PsCliError."""
        (tmp_path / "catalog.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")

        with pytest.raises(PsCliError) as excinfo:
            read_catalog(tmp_path)

        assert "unexpected shape" in excinfo.value.msg

    def test_entry_missing_required_field_raises_generic_ps_cli_error(self, tmp_path: Path) -> None:
        """A catalog.json row missing 'title' raises a generic PsCliError."""
        _write_catalog(tmp_path, [{"instrument_id": "CRA-1.0", "source_type": "external"}])

        with pytest.raises(PsCliError) as excinfo:
            read_catalog(tmp_path)

        assert "unexpected shape" in excinfo.value.msg


class TestReadArtifact:
    """`read_artifact(repo_path, instrument_id)` reads the manifest + both blobs (raw bytes)."""

    def test_reads_manifest_and_both_blobs_verbatim(self, tmp_path: Path) -> None:
        """Manifest fields parse; baseline/native blobs come back as raw, unparsed bytes."""
        _write_instrument(
            tmp_path,
            "CRA-1.0",
            manifest=_MANIFEST_BODY,
            baseline=b'{"nodes": [{"label": "Role"}], "edges": []}',
            native=b'{"nodes": [{"label": "ARTICLE"}], "edges": []}',
        )

        artifact = read_artifact(tmp_path, "CRA-1.0")

        assert artifact.manifest.instrument_id == "CRA-1.0"
        assert artifact.manifest.celex == "32024R2847"
        assert artifact.manifest.short_name == "CRA"
        assert artifact.manifest.schema_version == "1.0.0"
        assert artifact.manifest.baseline_sha256 == "a" * 64
        assert artifact.manifest.native_sha256 == "b" * 64
        assert artifact.baseline_blob == b'{"nodes": [{"label": "Role"}], "edges": []}'
        assert artifact.native_blob == b'{"nodes": [{"label": "ARTICLE"}], "edges": []}'
        assert isinstance(artifact.baseline_blob, bytes)
        assert isinstance(artifact.native_blob, bytes)

    def test_internal_source_manifest_with_none_celex_and_jurisdiction(
        self, tmp_path: Path
    ) -> None:
        """An internal-source manifest's celex/jurisdiction parse as None, not missing."""
        manifest = {
            **_MANIFEST_BODY,
            "celex": None,
            "jurisdiction": None,
            "source_type": "internal",
        }
        _write_instrument(tmp_path, "ENGPRAC-1.0", manifest=manifest)

        artifact = read_artifact(tmp_path, "ENGPRAC-1.0")

        assert artifact.manifest.celex is None
        assert artifact.manifest.jurisdiction is None
        assert artifact.manifest.source_type == "internal"

    def test_missing_instrument_directory_raises_ps_cli_error_naming_the_path(
        self, tmp_path: Path
    ) -> None:
        """A nonexistent instrument directory raises PsCliError naming the missing path."""
        with pytest.raises(PsCliError) as excinfo:
            read_artifact(tmp_path, "MISSING-1.0")

        assert str(tmp_path / "MISSING-1.0") in excinfo.value.msg

    def test_malformed_manifest_raises_generic_ps_cli_error(self, tmp_path: Path) -> None:
        """A manifest.json missing a required field raises a generic PsCliError."""
        _write_instrument(tmp_path, "CRA-1.0", manifest={"instrument_id": "CRA-1.0"})

        with pytest.raises(PsCliError) as excinfo:
            read_artifact(tmp_path, "CRA-1.0")

        assert "unexpected shape" in excinfo.value.msg
