"""Shared helpers for tests of the evaluator proxy's rendered nginx configuration."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART = REPO_ROOT / "charts" / "policy-system"
PATHS_FILE = CHART / "files" / "authentik-public-paths.txt"

HELM_TIMEOUT_SECONDS = 60.0


@pytest.fixture(scope="session")
def allowlisted_paths() -> list[str]:
    """Return the path lines of the shared Authentik allowlist file."""
    lines = (line.strip() for line in PATHS_FILE.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


@pytest.fixture(scope="session")
def chart_dir() -> Path:
    """Return the chart directory."""
    return CHART


@pytest.fixture(scope="session")
def nginx_conf() -> str:
    """Render the chart with the evaluator proxy enabled and return its nginx.conf."""
    helm = shutil.which("helm")
    assert helm is not None, "helm must be on PATH (devcontainer/CI prerequisite)"
    result = subprocess.run(  # noqa: S603 - helm is a resolved absolute path; args are literals
        [
            helm,
            "template",
            "ps",
            str(CHART),
            "--set",
            "llm.provider=ollama",
            "--set",
            "psService.localTestBypass.enabled=true",
            "--set",
            "proxy.enabled=true",
            "--set",
            "proxy.tls.secretName=policy-system-local-tls",
            "--set",
            "psService.authentikHostname=ps.test",
            "--set",
            "authentik.enabled=true",
            "--show-only",
            "templates/proxy-configmap.yaml",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=HELM_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    document = yaml.safe_load(result.stdout)
    conf = document["data"]["nginx.conf"]
    assert isinstance(conf, str)
    return conf
