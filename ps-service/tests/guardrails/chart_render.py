"""Render `charts/policy-system` for the chart guards, failing loudly when that is impossible.

The guards render with the real `helm` binary and the packaged subchart archives that
`helm dependency build` leaves in `charts/policy-system/charts`. Nothing here builds or fetches:
building would reach `https://charts.goauthentik.io` and write into the working tree from inside
a test. When `helm`, `Chart.lock` or an archive is missing, or an archive holds another version
than `Chart.lock` pins, the helpers raise `ChartGuardSetupError` with how to fix it, so the test
fails instead of passing or skipping on a setup that cannot prove anything.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from typing import TYPE_CHECKING, Any, cast

import yaml

from guardrails.migration_sources import REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

CHART_DIR = REPO_ROOT / "charts" / "policy-system"
RELEASE = "ps"
SUBPROCESS_TIMEOUT_SECONDS = 120.0

BUILD_HINT = (
    "run `helm repo add authentik https://charts.goauthentik.io && "
    "helm dependency build charts/policy-system`, or `gh insitu run post-create-deps`"
)


class ChartGuardSetupError(AssertionError):
    """The chart cannot be rendered here; raised so the test fails, never skips."""


def require_helm(search_path: str | None = None) -> str:
    """Return the absolute path of `helm` on `search_path` (default: `PATH`)."""
    helm = shutil.which("helm", path=search_path)
    if helm is None:
        msg = "helm is not on PATH. How to fix: install helm (gh insitu run install-helm)"
        raise ChartGuardSetupError(msg)
    return helm


def _packaged_version(archive: Path, name: str) -> str:
    """Return the `version` the archive's `<name>/Chart.yaml` declares, or what it holds instead."""
    try:
        with tarfile.open(archive) as tar:
            member = tar.extractfile(f"{name}/Chart.yaml")
            if member is None:
                return f"no {name}/Chart.yaml"
            # A Chart.yaml is a mapping; yaml.safe_load is untyped and only `.get` is used on it.
            packaged = cast("dict[str, object]", yaml.safe_load(member.read()))
    except KeyError, tarfile.TarError:
        return f"no readable {name}/Chart.yaml"
    return str(packaged.get("version"))


def require_subchart_archives(chart_dir: Path = CHART_DIR) -> dict[str, Path]:
    """Map each Chart.lock dependency name to its verified `charts/<name>-<version>.tgz`."""
    lock_file = chart_dir / "Chart.lock"
    if not lock_file.is_file():
        msg = f"{lock_file} is missing. How to fix: {BUILD_HINT}"
        raise ChartGuardSetupError(msg)
    # `helm dependency build` writes Chart.lock as a mapping whose `dependencies` entries carry
    # string `name` and `version`; yaml.safe_load is untyped, so the cast states that shape.
    lock = cast("dict[str, list[dict[str, str]]]", yaml.safe_load(lock_file.read_text("utf-8")))
    archives: dict[str, Path] = {}
    for dependency in lock.get("dependencies", []):
        name, version = dependency["name"], dependency["version"]
        archive = chart_dir / "charts" / f"{name}-{version}.tgz"
        if not archive.is_file():
            msg = f"{archive} is missing. How to fix: {BUILD_HINT}"
            raise ChartGuardSetupError(msg)
        found = _packaged_version(archive, name)
        if found != version:
            msg = (
                f"{archive} holds {name} {found}, Chart.lock pins {version}. "
                f"How to fix: {BUILD_HINT}"
            )
            raise ChartGuardSetupError(msg)
        archives[name] = archive
    return archives


def render(helm: str, chart_dir: Path, values: Sequence[str]) -> list[dict[str, Any]]:
    """Return the non-empty documents `helm template` renders for `values` (`key=value`).

    Never passes `--dependency-update`, so helm reads the packaged archives and makes no network
    call.
    """
    settings = [argument for value in values for argument in ("--set", value)]
    result = subprocess.run(  # noqa: S603 - helm is a shutil.which-resolved absolute path; args are literals and test-owned paths
        [helm, "template", RELEASE, str(chart_dir), *settings],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        msg = f"helm template exited {result.returncode}: {result.stderr.strip()}"
        raise ChartGuardSetupError(msg)
    documents: list[object] = list(yaml.safe_load_all(result.stdout))
    # `isinstance` narrows only to `dict[Unknown, Unknown]`; a rendered manifest has string keys.
    return [cast("dict[str, Any]", d) for d in documents if isinstance(d, dict)]
