"""Static structure checks for `scripts/deploy-ps-eval.sh`."""

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


def _without_rejection_function(source: str) -> str:
    return re.sub(
        r"(?ms)^# reject_local_suffix_hostname:.*?^reject_local_suffix_hostname\(\) \{.*?^\}\n",
        "",
        source,
    )


def test_eval_script_has_no_mdns_or_five_second_delay_rationale_outside_the_rejection() -> None:
    source = EVAL.read_text()
    rest = _without_rejection_function(source)

    assert rest != source, "rejection function not found; update the extraction regex"
    assert "mDNS" not in rest
    assert "5 seconds" not in source
    assert "#224" not in source


_OLD_LOCAL = "authentik" + ".local"
_SCANNED_ROOTS = ("scripts", "charts", "docs", "ps-service/tests", "ps-cli/tests")
_SKIPPED_PARTS = frozenset({"__pycache__", ".venv", "node_modules", ".git"})


def test_the_old_default_hostname_literal_appears_in_no_tracked_file() -> None:
    """The needle is built by concatenation, so no file (this one included) holds it."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for root in _SCANNED_ROOTS
        for path in (REPO_ROOT / root).rglob("*")
        if path.is_file()
        and not _SKIPPED_PARTS & set(path.parts)
        and path.suffix not in {".pyc", ".png", ".gz", ".tgz"}
        and _OLD_LOCAL in path.read_text(errors="ignore")
    ]

    assert offenders == []
