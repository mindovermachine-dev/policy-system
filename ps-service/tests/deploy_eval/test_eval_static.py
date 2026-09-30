"""Static structure checks for `scripts/deploy-ps-eval.sh` (issue #165, AC-BI-013, AC-BI-008)."""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL = REPO_ROOT / "scripts" / "deploy-ps-eval.sh"
PROD = REPO_ROOT / "scripts" / "deploy-ps-prod.sh"


def test_both_scripts_source_the_same_owner_lib() -> None:
    assert "lib/authentik-owner.sh" in EVAL.read_text()
    assert "lib/authentik-owner.sh" in PROD.read_text()


def test_the_eval_script_is_executable_bash_in_strict_mode() -> None:
    source = EVAL.read_text()

    assert os.access(EVAL, os.X_OK)
    assert source.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in source


def test_no_script_or_lib_disables_certificate_verification() -> None:
    files = [EVAL, PROD, *sorted((REPO_ROOT / "scripts" / "lib").glob("*.sh"))]

    for path in files:
        offending = [
            line
            for line in path.read_text().splitlines()
            if re.search(r"(\bcurl\b.*\s-k\b|--insecure|verify=False)", line)
            and not line.lstrip().startswith("#")
        ]
        assert offending == [], f"{path}: {offending}"
