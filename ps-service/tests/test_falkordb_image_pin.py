"""Every tracked reference to the FalkorDB image uses the tag the chart pins.

`charts/policy-system/values.yaml` is the single source of truth for the tag. This guard scans
the tracked tree so a copied-in unpinned or stale reference fails the suite instead of drifting.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from graph_gateway.falkordb_image import falkordb_image_tag

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT_SECONDS = 30
_TAG_REF = re.compile(r"falkordb/falkordb:(?P<tag>\w[\w.\-]*)")
_ROW = re.compile(
    r"^\|\s*`falkordb\.image\.repository`\s*/\s*`falkordb\.image\.tag`\s*\|"
    r"\s*`[^`]*`\s*/\s*`(?P<tag>[^`]*)`"
)
_EXCLUDED_PREFIXES = ("docs/audits/", "spikes/")
_MUST_BE_SCANNED = (
    ".devcontainer/docker-compose.yml",
    "CONTRIBUTING.md",
    "docs/artifacts/helm-chart-values-reference.md",
    "tools/graph-ingestion/load_all.sh",
    "tools/graph-ingestion/load_graph.py",
    "charts/policy-system/tests/falkordb-deployment_test.yaml",
    "ps-service/tests/test_container_image.py",
)

_IMAGE = "falkordb/falkordb"
_CHART_TAG = "v9.9.9"


def find_violations(files: Mapping[str, str], chart_tag: str) -> list[str]:
    """Return one `path:line: found X, chart pins Y` entry per reference off the chart tag.

    Args:
        files: Tracked text by repo-relative path.
        chart_tag: The tag pinned in the chart values.

    Returns:
        Violation descriptions; empty when every reference matches the chart tag.
    """
    violations: list[str] = []
    for path, text in files.items():
        for number, line in enumerate(text.splitlines(), start=1):
            found = [m.group("tag") for m in _TAG_REF.finditer(line)]
            row = _ROW.match(line)
            if row:
                found.append(row.group("tag"))
            violations.extend(
                f"{path}:{number}: found {tag}, chart pins {chart_tag}"
                for tag in found
                if tag != chart_tag
            )
    return violations


def read_tracked_text(paths: Iterable[str], root: Path) -> dict[str, str]:
    """Read tracked text files, skipping excluded, deleted, directory, and non-text paths.

    Args:
        paths: Repo-relative tracked paths.
        root: Directory the paths are relative to.

    Returns:
        Text by path for every readable, non-excluded file.
    """
    files: dict[str, str] = {}
    for path in paths:
        if path.startswith(_EXCLUDED_PREFIXES):
            continue
        try:
            files[path] = (root / path).read_text(encoding="utf-8")
        except FileNotFoundError, IsADirectoryError, UnicodeDecodeError:
            continue
    return files


def _visible_paths() -> list[str]:
    """List tracked paths plus untracked paths that are not ignored."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=True,
    ).stdout
    return [p.decode() for p in out.split(b"\0") if p]


def test_unpinned_latest_reference_is_reported_with_path_and_line() -> None:
    files = {"a.md": f"intro\nrun {_IMAGE}:" + "latest"}

    assert find_violations(files, _CHART_TAG) == [f"a.md:2: found latest, chart pins {_CHART_TAG}"]


def test_reference_to_a_different_tag_is_reported() -> None:
    files = {"a.md": f"run {_IMAGE}:" + "v0.0.1"}

    assert len(find_violations(files, _CHART_TAG)) == 1


def test_reference_matching_the_chart_tag_is_clean() -> None:
    files = {"a.md": f"run {_IMAGE}:" + _CHART_TAG}

    assert find_violations(files, _CHART_TAG) == []


def test_placeholder_and_untagged_references_are_clean() -> None:
    files = {"a.md": f"run {_IMAGE}:<new-tag> and {_IMAGE}"}

    assert find_violations(files, _CHART_TAG) == []


def test_values_reference_row_with_wrong_tag_is_reported() -> None:
    row = "| `falkordb.image.repository` / `falkordb.image.tag` | `" + _IMAGE + "` / `"
    row += "latest` | x |"

    assert len(find_violations({"ref.md": row}, _CHART_TAG)) == 1


def test_values_reference_row_with_chart_tag_is_clean() -> None:
    row = "| `falkordb.image.repository` / `falkordb.image.tag` | `" + _IMAGE + "` / `"
    row += _CHART_TAG + "` | x |"

    assert find_violations({"ref.md": row}, _CHART_TAG) == []


def test_deleted_and_excluded_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "docs" / "audits").mkdir(parents=True)
    (tmp_path / "docs" / "audits" / "old.md").write_text(f"{_IMAGE}:" + "latest")
    (tmp_path / "kept.md").write_text("plain")

    files = read_tracked_text(["gone.md", "docs/audits/old.md", "kept.md"], tmp_path)

    assert files == {"kept.md": "plain"}


def test_prod_values_do_not_override_the_falkordb_image() -> None:
    values_prod = _REPO_ROOT / "charts" / "policy-system" / "values-prod.yaml"
    prod = yaml.safe_load(values_prod.read_text())

    assert "image" not in (prod.get("falkordb") or {})


def test_every_tracked_falkordb_image_reference_matches_the_chart_tag() -> None:
    files = read_tracked_text(_visible_paths(), _REPO_ROOT)

    assert set(_MUST_BE_SCANNED) <= files.keys()
    assert find_violations(files, falkordb_image_tag()) == []


def test_chart_tag_is_a_bare_three_part_version() -> None:
    tag = falkordb_image_tag()

    assert re.fullmatch(r"\d+\.\d+\.\d+", tag), tag
    assert int(tag.split(".")[0]) >= 6


def test_bare_version_reference_is_parsed_as_the_tag() -> None:
    files = {"a.md": f"run {_IMAGE}:6.0.3"}

    assert find_violations(files, "6.0.3") == []
    assert find_violations(files, "6.0.4") == ["a.md:1: found 6.0.3, chart pins 6.0.4"]
