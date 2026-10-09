"""Static contract test: every policy-system `helm upgrade` waits for Jobs (issue #205).

The chart renders a provisioning Job (`ps-state-provision`) that must finish before ps-service can
start (it fails closed until the immutable graph log tables exist). `helm --wait` does not wait
for Jobs without `--wait-for-jobs`, so a failed Job would otherwise surface only as a crash-looping
ps-service until the timeout. These scripts are the deploy paths that upgrade the release.

Placement: mirrors no source module (it asserts facts about repository-root scripts), like
`test_dockerfile.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_RELEASE_UPGRADE = re.compile(r"helm upgrade --install \"\$(?:HELM_)?RELEASE_NAME\"")
_SCRIPTS = ("ps-upgrade.sh", "deploy-ps-prod.sh")


def _helm_upgrade_commands(script_text: str) -> list[str]:
    """Return each policy-system `helm upgrade` command with its backslash continuations joined."""
    joined = script_text.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if _RELEASE_UPGRADE.search(line)]


@pytest.mark.parametrize("script_name", _SCRIPTS)
def test_every_policy_system_helm_upgrade_waits_for_jobs(script_name: str) -> None:
    commands = _helm_upgrade_commands((_SCRIPTS_DIR / script_name).read_text(encoding="utf-8"))

    assert commands, f"{script_name} has no policy-system helm upgrade to check"
    for command in commands:
        assert "--wait " in f"{command} ", command
        assert "--wait-for-jobs" in command, command


def test_ps_upgrade_script_covers_both_the_local_and_production_upgrades() -> None:
    commands = _helm_upgrade_commands((_SCRIPTS_DIR / "ps-upgrade.sh").read_text(encoding="utf-8"))

    assert len(commands) == 2


def test_production_deploy_gives_the_provisioning_job_a_bounded_wait() -> None:
    commands = _helm_upgrade_commands(
        (_SCRIPTS_DIR / "deploy-ps-prod.sh").read_text(encoding="utf-8")
    )

    assert all("--timeout 10m" in command for command in commands)
