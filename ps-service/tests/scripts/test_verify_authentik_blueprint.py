"""Tests for `scripts/verify-authentik-blueprint.sh` that need no container runtime.

The live apply is in `test_authentik_blueprint_apply_live.py`. Here the script's argument
handling, its image-tag derivation and its teardown wiring are checked by running it for real
up to the point where it would start a container.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from guardrails.chart_render import CHART_DIR, ChartGuardSetupError, require_subchart_archives

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-authentik-blueprint.sh"
IMAGE_REPOSITORY = "ghcr.io/goauthentik/server"

SUBPROCESS_TIMEOUT_SECONDS = 30.0
USAGE_EXIT_STATUS = 2


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # fixed argv: the script under test and literal options
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _authentik_dependency_version() -> str:
    chart = cast("dict[str, Any]", yaml.safe_load((CHART_DIR / "Chart.yaml").read_text("utf-8")))
    dependencies = cast("list[dict[str, str]]", chart["dependencies"])
    return next(d["version"] for d in dependencies if d["name"] == "authentik")


def test_image_tag_is_the_authentik_dependency_version_of_the_chart() -> None:
    completed = _run("--print-image")

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"{IMAGE_REPOSITORY}:{_authentik_dependency_version()}"


def _packaged_authentik_chart(chart_dir: Path) -> dict[str, Any]:
    """Return the `Chart.yaml` packaged in the authentik archive; fail when it is not packaged."""
    archive = require_subchart_archives(chart_dir)["authentik"]
    with tarfile.open(archive) as tar:
        member = tar.extractfile("authentik/Chart.yaml")
        assert member is not None
        return cast("dict[str, Any]", yaml.safe_load(member.read()))


def test_dependency_version_is_the_app_version_of_the_packaged_authentik_subchart() -> None:
    assert _packaged_authentik_chart(CHART_DIR)["appVersion"] == _authentik_dependency_version()


def test_an_unknown_option_is_refused() -> None:
    completed = _run("--no-such-option")

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "unknown option: --no-such-option" in completed.stderr


def test_upgrade_from_without_a_revision_is_refused() -> None:
    completed = _run("--upgrade-from")

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "--upgrade-from needs a git revision" in completed.stderr


def test_a_missing_blueprint_file_is_refused_before_any_container_starts(tmp_path: Path) -> None:
    completed = _run(str(tmp_path / "absent.yaml"))

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "blueprint not found" in completed.stderr


def test_a_second_blueprint_argument_is_refused(tmp_path: Path) -> None:
    first = tmp_path / "a.yaml"
    first.write_text("version: 1\n", encoding="utf-8")

    completed = _run(str(first), str(first))

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "only one BLUEPRINT" in completed.stderr


def test_the_postgres_container_and_network_are_removed_on_every_exit_path() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "trap cleanup EXIT" in script
    assert '"$CLI" rm -f "$PG"' in script
    assert '"$CLI" network rm "$NETWORK"' in script


def test_a_missing_authentik_archive_fails_with_the_build_hint(tmp_path: Path) -> None:
    for name in ("Chart.yaml", "Chart.lock"):
        shutil.copy(CHART_DIR / name, tmp_path / name)

    with pytest.raises(ChartGuardSetupError, match="helm dependency build charts/policy-system"):
        _packaged_authentik_chart(tmp_path)
