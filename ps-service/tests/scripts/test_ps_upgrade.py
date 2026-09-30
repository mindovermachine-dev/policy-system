"""Regression tests for `scripts/ps-upgrade.sh` (issue #165 baseline fix).

The script upgraded the release without carrying over the values the release already has, so an
upgrade reset every non-LLM value on the evaluator (and the bootstrap-owner and Authentik URL
values on production) to chart defaults. It must upgrade with `--reset-then-reuse-values`: new
chart defaults still apply, the release's stored user values are kept, and only what the script
itself supplies is overridden.

The script runs for real against a fake `kubectl`/`helm` on PATH (the external CLIs are the
process boundary); the fake `helm` records its argv. `ps-cli/install.sh` is replaced by a no-op in
a copied script tree so the real client installer never runs.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ps-upgrade.sh"
PROD_VALUES = REPO_ROOT / "charts" / "policy-system" / "values-prod.yaml"

SUBPROCESS_TIMEOUT_SECONDS = 30.0

FAKE_KUBECTL = """#!/usr/bin/env bash
if [[ "$1 $2" == "config current-context" ]]; then
  printf '%s\\n' "$FAKE_CONTEXT"
fi
"""

FAKE_HELM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_HELM_LOG"
if [[ "$1 $2" == "get values" ]]; then
  printf '%s' "$FAKE_RELEASE_VALUES"
fi
"""

RELEASE_VALUES = (
    '{"psService":{"auth":{"issuer":"https://idp/","audience":"a","cliClientId":"c","scopes":"s"},'
    '"authzBootstrapOwner":{"subject":"sub","issuer":"https://idp/"}}}'
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_upgrade(
    tmp_path: Path, *, context: str, release_values: str
) -> tuple[int, list[str], str]:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "ps-cli").mkdir()
    (tree / "charts" / "policy-system").mkdir(parents=True)
    shutil.copy(SCRIPT, tree / "scripts" / "ps-upgrade.sh")
    shutil.copy(PROD_VALUES, tree / "charts" / "policy-system" / "values-prod.yaml")
    _write_executable(tree / "ps-cli" / "install.sh", "#!/usr/bin/env bash\nexit 0\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "kubectl", FAKE_KUBECTL)
    _write_executable(bin_dir / "helm", FAKE_HELM)
    log = tmp_path / "helm.log"
    log.write_text("")

    bash = shutil.which("bash")
    assert bash is not None
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CONTEXT": context,
        "FAKE_HELM_LOG": str(log),
        "FAKE_RELEASE_VALUES": release_values,
    }
    result = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; args are literals
        [bash, str(tree / "scripts" / "ps-upgrade.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    return result.returncode, log.read_text().splitlines(), result.stderr


def _upgrade_call(calls: list[str]) -> str:
    upgrades = [call for call in calls if call.startswith("upgrade ")]
    assert len(upgrades) == 1, calls
    return upgrades[0]


@pytest.mark.parametrize("context", ["kind-policy-system", "aks-prod"])
def test_upgrade_keeps_the_releases_stored_values(tmp_path: Path, context: str) -> None:
    code, calls, stderr = _run_upgrade(tmp_path, context=context, release_values=RELEASE_VALUES)

    assert code == 0, stderr
    upgrade = _upgrade_call(calls)
    assert "--reset-then-reuse-values" in upgrade.split()
    assert "--set llm.existingSecret=policy-system-llm-credentials" in upgrade


def test_production_upgrade_does_not_overwrite_stored_values_with_flags(tmp_path: Path) -> None:
    code, calls, stderr = _run_upgrade(tmp_path, context="aks-prod", release_values=RELEASE_VALUES)

    assert code == 0, stderr
    upgrade = _upgrade_call(calls)
    assert "-f " in upgrade
    assert "values-prod.yaml" in upgrade
    assert "psService.auth." not in upgrade
    assert "authzBootstrapOwner" not in upgrade


def test_production_upgrade_refuses_a_release_without_auth_values(tmp_path: Path) -> None:
    code, calls, stderr = _run_upgrade(tmp_path, context="aks-prod", release_values="{}")

    assert code == 1
    assert "psService.auth.issuer" in stderr
    assert not any(call.startswith("upgrade ") for call in calls)
