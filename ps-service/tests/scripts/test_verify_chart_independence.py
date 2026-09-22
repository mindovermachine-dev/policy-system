"""Regression test for `scripts/verify-chart-independence.sh` (issue #111 baseline fix).

The script's own `helm template` invocations must always pass
`psService.localTestBypass.enabled=true` (or otherwise satisfy the chart's #58 fail-closed OIDC
guard on `templates/ps-service-deployment.yaml`), or every `helm template` call in the script
fails before its independence/credential-leak checks ever run. This was found as a pre-existing
bug on unmodified `main` (reproduced independently by two implement agents against clean
checkouts) and fixed by adding the bypass flag to each `helm template` call in the script.

Marked `integration` (spawns a real `helm` subprocess against the real chart, per the
`integration` marker's own definition) so the default hermetic `pytest -q` suite -- which does
not otherwise depend on `helm` being installed -- is unaffected; `helm` is a devcontainer/CI
prerequisite (`.insitu.yml`'s `install-helm` check), so its absence here is a failure, not a
skip.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-chart-independence.sh"

SUBPROCESS_TIMEOUT_SECONDS = 60.0


def test_verify_chart_independence_passes_against_the_real_chart() -> None:
    """Guards against a future `helm template` call being added without the bypass flag."""
    bash = shutil.which("bash")
    assert bash is not None, "bash must be on PATH (devcontainer/CI prerequisite)"

    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, (
        f"verify-chart-independence.sh failed (exit {result.returncode}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "PASS — falkordb.* and psService.* value overrides are structurally decoupled" in (
        result.stdout
    )
    assert "PASS — credential value renders only inside templates/secret.yaml" in result.stdout
