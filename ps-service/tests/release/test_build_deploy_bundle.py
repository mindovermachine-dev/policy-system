"""The deploy bundle holds every file the deploy scripts need and pins the chart version.

Runs the real `scripts/release/build-deploy-bundle.sh` and unpacks the real archive; no fakes.
"""

from __future__ import annotations

import re
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "release" / "build-deploy-bundle.sh"
BUNDLE_DIR = "policy-system-deploy"
ENTRY_SCRIPTS = (
    "deploy-llm.sh",
    "sync-llm-secrets-to-kind.sh",
    "deploy-ps-eval.sh",
    "deploy-ps-prod.sh",
    "ps-aks.sh",
    "ps-upgrade.sh",
    "ps-reset-data.sh",
    "grant-access-role.sh",
)
SOURCE_LINE = re.compile(r'^\s*source "\$\{SCRIPT_DIR\}/([^"]+)"', re.MULTILINE)


@pytest.fixture(scope="module")
def unpacked(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("bundle-out")
    subprocess.run(  # noqa: S603 - the repo's own script and test literals
        [str(BUILD_SCRIPT), "9.9.9", str(out_dir)], cwd=REPO_ROOT, check=True, capture_output=True
    )
    target = tmp_path_factory.mktemp("bundle-unpacked")
    with tarfile.open(out_dir / "ps-deploy-bundle.tgz") as archive:
        archive.extractall(target, filter="data")
    return target / BUNDLE_DIR


def test_bundle_records_the_release_version(unpacked: Path) -> None:
    assert (unpacked / "VERSION").read_text().strip() == "9.9.9"


def test_bundle_has_the_cluster_config_the_guide_uses(unpacked: Path) -> None:
    assert (unpacked / "deploy" / "kind" / "cluster.yaml").is_file()


def test_bundle_has_the_ingress_controller_manifest_the_guide_applies(unpacked: Path) -> None:
    assert (unpacked / "deploy" / "kind" / "ingress-nginx.yaml").is_file()


def test_bundle_has_the_production_values_and_defaults(unpacked: Path) -> None:
    assert (unpacked / "charts" / "policy-system" / "values-prod.yaml").is_file()
    assert (unpacked / "scripts" / "ps-defaults.conf").is_file()


def test_every_file_the_entry_scripts_source_is_in_the_bundle(unpacked: Path) -> None:
    for name in ENTRY_SCRIPTS:
        script = unpacked / "scripts" / name
        assert script.is_file(), f"{name} missing from the bundle"
        for relative in SOURCE_LINE.findall(script.read_text()):
            assert (unpacked / "scripts" / relative).is_file(), f"{name} sources missing {relative}"


def test_bundled_scripts_keep_the_executable_bit(unpacked: Path) -> None:
    for name in ENTRY_SCRIPTS:
        assert (unpacked / "scripts" / name).stat().st_mode & 0o111, name


def test_build_script_rejects_missing_arguments() -> None:
    result = subprocess.run(  # noqa: S603 - the repo's own script
        [str(BUILD_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_bundle_pins_chart_and_script_versions_together(unpacked: Path) -> None:
    """The one VERSION file that says which release the scripts are from also selects the chart
    both deploy scripts install and read the shared path list from.
    """
    bundle_version = (unpacked / "VERSION").read_text().strip()
    probe = (
        f'source "{unpacked}/scripts/lib/chart-version.sh"; '
        f'set_chart_version_args "{unpacked}/scripts" oci://example.test/charts/policy-system; '
        'printf "%s " "${chart_version_args[@]}"'
    )
    result = subprocess.run(  # noqa: S603 - the repo's own script and test literals
        ["/bin/bash", "-c", probe], capture_output=True, text=True, check=True, timeout=30
    )
    assert result.stdout.split() == ["--version", bundle_version]
    for name in ("deploy-ps-prod.sh", "deploy-ps-eval.sh"):
        script = (unpacked / "scripts" / name).read_text()
        assert 'source "${SCRIPT_DIR}/lib/chart-version.sh"' in script, name
        assert "set_chart_version_args" in script, name
