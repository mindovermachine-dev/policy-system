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


_OLD_TEST = "authentik" + ".test"
_ROOT_ISSUER = re.compile(r":30443" + r"/application/o/")
_DEFAULT_HOSTNAME_POINTING_AT_OLD_NAME = re.compile(
    r"DEFAULT_HOSTNAME\s*=\s*[\"']?" + re.escape(_OLD_TEST)
)
_GUIDE = REPO_ROOT / "docs" / "artifacts" / "operations-guide.md"
_MIGRATION_HEADING_PREFIX = "### Moving an existing evaluator from "
_GUARDED_ROOTS = (*_SCANNED_ROOTS, "deploy")
_BINARY_SUFFIXES = frozenset({".pyc", ".png", ".gz", ".tgz"})


def _guarded_files() -> list[Path]:
    return [
        path
        for root in _GUARDED_ROOTS
        if (REPO_ROOT / root).exists()
        for path in (REPO_ROOT / root).rglob("*")
        if path.is_file()
        and not _SKIPPED_PARTS & set(path.parts)
        and path.suffix not in _BINARY_SUFFIXES
    ]


def _migration_section_lines() -> tuple[int, int]:
    """Return the guide's [start, end) line indexes of the single old-name migration section."""
    lines = _GUIDE.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(_MIGRATION_HEADING_PREFIX)]
    assert len(starts) == 1, "the guide must hold exactly one migration section"
    end = next(
        (i for i in range(starts[0] + 1, len(lines)) if re.match(r"#{2,3} ", lines[i])),
        len(lines),
    )
    return starts[0], end


def _text_outside_the_migration_section(path: Path) -> str:
    text = path.read_text(errors="ignore")
    if path != _GUIDE:
        return text
    start, end = _migration_section_lines()
    lines = text.splitlines()
    return "\n".join(lines[:start] + lines[end:])


def test_the_old_name_and_its_default_hostname_constant_appear_only_in_the_migration_section() -> (
    None
):
    offenders = sorted(
        {
            str(path.relative_to(REPO_ROOT))
            for path in _guarded_files()
            for text in [_text_outside_the_migration_section(path)]
            if _OLD_TEST in text or _DEFAULT_HOSTNAME_POINTING_AT_OLD_NAME.search(text)
        }
    )

    assert offenders == []


def test_no_root_path_evaluator_issuer_remains_outside_the_migration_section() -> None:
    offenders = sorted(
        {
            str(path.relative_to(REPO_ROOT))
            for path in _guarded_files()
            if _ROOT_ISSUER.search(_text_outside_the_migration_section(path))
        }
    )

    assert offenders == []


def test_the_script_writes_no_loopback_hosts_mapping() -> None:
    writes = [
        line
        for line in _code_lines(EVAL)
        if ("tee -a" in line or ">>" in line) and ("127.0.0.1" in line or "::1" in line)
    ]
    loopback_loops = [line for line in _code_lines(EVAL) if re.search(r"127\.0\.0\.1\s+::1", line)]

    assert writes == []
    assert loopback_loops == []


def test_every_loopback_address_in_the_script_belongs_to_a_local_connection() -> None:
    allowed = ("--resolve", "PF", "8000", "port-forward", "_PORT", "_LOCAL_URL", "psql")
    stray = [
        line
        for line in _code_lines(EVAL)
        if "127.0.0.1" in line and not any(marker in line for marker in allowed)
    ]

    assert stray == []


def test_the_eval_script_documents_the_exposure_flags_in_its_header() -> None:
    header = EVAL.read_text().split("set -euo pipefail", 1)[0]

    assert "--allow-public-host" in header
    assert "--check-exposure" in header


def _header_prose(path: Path) -> str:
    header = path.read_text().split("set -euo pipefail", 1)[0]
    return " ".join(line.lstrip("#").strip() for line in header.splitlines())


def test_eval_header_documents_reset_and_no_longer_claims_a_rerun_recovers_a_lost_device() -> None:
    header = _header_prose(EVAL)

    assert "--reset-owner-passkey" in header
    assert "Re-running is the owner-recovery path" not in header
    assert (
        "Re-running issues a fresh link to an owner with no passkey; a lost or stolen device is"
        " recovered with --reset-owner-passkey." in header
    )


def test_the_eval_release_values_set_the_passkey_signing_allowed_origins() -> None:
    source = EVAL.read_text()

    assert "passkeySigning" in source
    assert "allowedOrigins" in source


def _code_lines(path: Path) -> list[str]:
    """The script's lines that are neither blank nor comments."""
    return [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_issuer_is_built_with_the_auth_prefix_in_one_place() -> None:
    code = _code_lines(EVAL)
    literal_prefix = [line for line in code if re.search(r"/auth(?![A-Za-z0-9_])", line)]
    issuer_lines = [line for line in code if "application/o/" in line]

    assert len(literal_prefix) == 1
    assert "AUTHENTIK_WEB_PATH=" in literal_prefix[0]
    assert len(issuer_lines) == 1
    assert "authentik_base_url" in issuer_lines[0]


def test_the_script_and_its_libs_hold_no_old_name_literal() -> None:
    needle = "authentik" + ".test"
    files = [EVAL, *sorted((REPO_ROOT / "scripts" / "lib").glob("*.sh"))]

    offenders = [str(path) for path in files if needle in path.read_text()]

    assert offenders == []
