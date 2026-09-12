"""Version-file sync + verify (AC-BI-012, AC-BI-014).

`sync-version-files.sh` writes `release_version` into the version-lockstep fields across four
files (`ps-service/pyproject.toml`, `ps-cli/pyproject.toml`, `charts/policy-system/Chart.yaml`
(`version` + `appVersion`), `charts/policy-system/values.yaml` (`psService.image.tag` only)) and
`ps-skills/policy-system/.claude-plugin/plugin.json` -- re-locking `uv.lock` for the two uv
workspace members along the way (PLAN A-13). `falkordb.image.tag`, a sibling top-level block in
`values.yaml`, must never change (PLAN A-20). `verify-version-files.sh` reads all of the above
back and fails, naming every offender, when any field does not equal `release_version`.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import ReleaseFixture

SUBPROCESS_TIMEOUT_SECONDS = 60.0

CHART_RELATIVE_PATH = "charts/policy-system/Chart.yaml"
VALUES_RELATIVE_PATH = "charts/policy-system/values.yaml"
PLUGIN_RELATIVE_PATH = "ps-skills/policy-system/.claude-plugin/plugin.json"


@pytest.fixture(autouse=True)
def _require_uv(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    release_fixture: ReleaseFixture,
) -> None:
    """`uv` is a devcontainer/CI prerequisite for the version-sync tests (conftest docstring)."""
    assert release_fixture.uv is not None, (
        "`uv` is a devcontainer/CI prerequisite but is not on PATH"
    )


def _pyproject_version(path: Path) -> str:
    """The `[project]` `version` value of a `pyproject.toml`."""
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(document["project"]["version"])


def _lock_versions(path: Path, package_names: tuple[str, ...]) -> dict[str, str]:
    """The `uv.lock` `version` of each named `[[package]]` entry."""
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        package["name"]: package["version"]
        for package in document["package"]
        if package["name"] in package_names
    }


def _parse_numstat(numstat_output: str) -> dict[str, tuple[int, int]]:
    r"""`git diff --numstat` lines (`added\tdeleted\tpath`) as `{path: (added, deleted)}`."""
    changed: dict[str, tuple[int, int]] = {}
    for line in numstat_output.splitlines():
        added, _, remainder = line.partition("\t")
        deleted, _, path = remainder.partition("\t")
        changed[path] = (int(added), int(deleted))
    return changed


def test_sync_writes_all_five_fields_and_uv_lock(release_fixture: ReleaseFixture) -> None:
    release_version = "0.12.0"
    work_dir = release_fixture.work.path

    release_fixture.run_script("sync-version-files.sh", release_version)

    assert _pyproject_version(work_dir / "ps-service" / "pyproject.toml") == release_version
    assert _pyproject_version(work_dir / "ps-cli" / "pyproject.toml") == release_version

    chart = yaml.safe_load((work_dir / CHART_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert chart["version"] == release_version
    assert chart["appVersion"] == release_version

    values = yaml.safe_load((work_dir / VALUES_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert values["psService"]["image"]["tag"] == release_version

    plugin = json.loads((work_dir / PLUGIN_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert plugin["version"] == release_version

    lock_versions = _lock_versions(work_dir / "uv.lock", ("ps-service", "ps-cli"))
    assert lock_versions == {"ps-service": release_version, "ps-cli": release_version}


def test_falkordb_image_tag_is_untouched(release_fixture: ReleaseFixture) -> None:
    values_path = release_fixture.work.path / VALUES_RELATIVE_PATH
    original_falkordb_tag = yaml.safe_load(values_path.read_text(encoding="utf-8"))["falkordb"][
        "image"
    ]["tag"]

    release_fixture.run_script("sync-version-files.sh", "0.12.0")

    updated_falkordb_tag = yaml.safe_load(values_path.read_text(encoding="utf-8"))["falkordb"][
        "image"
    ]["tag"]
    assert updated_falkordb_tag == original_falkordb_tag


def test_each_file_changes_exactly_one_line(release_fixture: ReleaseFixture) -> None:
    release_fixture.run_script("sync-version-files.sh", "0.12.0")

    changed_lines = _parse_numstat(release_fixture.work.run("diff", "--numstat").stdout)

    expected_lines_changed = {
        "ps-service/pyproject.toml": 1,
        "ps-cli/pyproject.toml": 1,
        CHART_RELATIVE_PATH: 2,  # `version:` + `appVersion:` -- two distinct fields, PLAN A-20
        VALUES_RELATIVE_PATH: 1,
        PLUGIN_RELATIVE_PATH: 1,
    }
    for path, expected in expected_lines_changed.items():
        added, deleted = changed_lines[path]
        assert (added, deleted) == (expected, expected), (
            f"{path}: expected exactly {expected} changed line(s), got +{added}/-{deleted}"
        )
    assert "uv.lock" in changed_lines, (
        "uv.lock must be re-locked alongside the pyproject.toml edits"
    )


def test_uv_lock_check_passes_after_sync(release_fixture: ReleaseFixture) -> None:
    release_fixture.run_script("sync-version-files.sh", "0.12.0")

    assert release_fixture.uv is not None
    result = subprocess.run(  # noqa: S603 - `uv` is a shutil.which-resolved absolute path; args are literals
        [str(release_fixture.uv), "lock", "--check", "--offline"],
        cwd=release_fixture.work.path,
        env=release_fixture.base_environment(),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_verify_fails_when_a_field_is_stale(release_fixture: ReleaseFixture) -> None:
    release_version = "0.12.0"
    release_fixture.run_script("sync-version-files.sh", release_version)

    plugin_path = release_fixture.work.path / PLUGIN_RELATIVE_PATH
    stale_text = re.sub(
        r'"version": "[^"]*"', '"version": "0.11.0"', plugin_path.read_text(encoding="utf-8")
    )
    plugin_path.write_text(stale_text, encoding="utf-8")

    result = release_fixture.run_script("verify-version-files.sh", release_version, expect=1)

    assert PLUGIN_RELATIVE_PATH in result.stderr
    assert "0.11.0" in result.stderr
