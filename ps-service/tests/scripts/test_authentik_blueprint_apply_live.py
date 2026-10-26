"""Applies the chart's Authentik blueprint to the pinned Authentik image.

The structural tests in `test_authentik_blueprint.py` parse the YAML; only Authentik's own
importer can say whether it applies. `scripts/verify-authentik-blueprint.sh` runs that importer
in a disposable container, so every test here needs a container runtime and a public image pull
(about 1.4 GB the first time) and carries the `authentik_blueprint` marker.

With no container CLI the tests skip, except under `CI=true`, where a runner without one must
fail rather than pass unverified.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.authentik_blueprint

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-authentik-blueprint.sh"
CHART_BLUEPRINT = REPO_ROOT / "charts" / "policy-system" / "files" / "authentik-blueprint.yaml"

APPLY_TIMEOUT_SECONDS = 1500.0
SUBPROCESS_TIMEOUT_SECONDS = 30.0

# The last revision before the passkey login flow was rewritten: an install upgraded from it
# holds the earlier blueprint's rows when the current one is applied.
PREVIOUS_REVISION = "e95dae75~1"

# A row-deletion entry whose target does not exist: the importer builds an unsaved instance of a
# multi-table-inheritance model for it, and deleting that raises.
ABSENT_ENTRY_ON_MISSING_ROW = """\
version: 1
metadata:
  name: absent-entry-on-missing-row
entries:
  - model: authentik_flows.flowstagebinding
    state: absent
    identifiers:
      target: !Find [authentik_flows.flow, [slug, default-authentication-flow]]
      stage: !Find
        - authentik_stages_identification.identificationstage
        - [name, default-authentication-identification]
      order: 987
"""


@pytest.fixture(scope="module", autouse=True)
def _require_container_cli() -> None:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    cli = os.environ.get("PS_CONTAINER_CLI") or shutil.which("podman") or shutil.which("docker")
    if cli:
        return
    if os.environ.get("CI") == "true":
        pytest.fail("no container CLI on a CI runner; the blueprint apply guard cannot run")
    pytest.skip("no container CLI (podman or docker) available")


def _verify(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # fixed argv: the script under test and literal options
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=APPLY_TIMEOUT_SECONDS,
    )


def test_chart_blueprint_applies_and_leaves_the_passkey_login_chain() -> None:
    completed = _verify(str(CHART_BLUEPRINT))

    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    assert "PASS ps-login chain, brand pointers and ps-cli application" in completed.stdout
    assert "PASS recovery and enrollment flows end on the confirmation stage" in completed.stdout


def test_chart_blueprint_reapplies_with_unchanged_row_counts() -> None:
    completed = _verify("--twice", str(CHART_BLUEPRINT))

    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]


def test_chart_blueprint_applies_over_the_previous_revision() -> None:
    revision_present = subprocess.run(  # noqa: S603  # fixed argv
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{PREVIOUS_REVISION}^{{commit}}"],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    if revision_present.returncode != 0:
        pytest.skip(f"git history does not hold {PREVIOUS_REVISION} (shallow clone)")
    completed = _verify("--upgrade-from", PREVIOUS_REVISION, str(CHART_BLUEPRINT))

    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]


def test_guard_fails_on_a_blueprint_the_importer_cannot_apply(tmp_path: Path) -> None:
    repro = tmp_path / "repro.yaml"
    repro.write_text(ABSENT_ENTRY_ON_MISSING_ROW, encoding="utf-8")

    completed = _verify(str(repro))

    assert completed.returncode != 0
    assert "policybindingmodel_ptr" in completed.stdout + completed.stderr
