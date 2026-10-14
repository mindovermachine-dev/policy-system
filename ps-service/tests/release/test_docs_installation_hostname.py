"""The evaluator hostname documentation after issue #239 (`authentik.test`, `.local` rejected).

Reads the real markdown files off disk; no fakes. The old hostname needle is built by
concatenation so the repo-wide AC-BI-008 grep stays empty. Deliberately limited to absence
rules, the option row, the surface-table rule and section existence; the wording of the
upgrade and evidence sections is reviewed by hand, not pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_OLD_LOCAL = "authentik" + ".local"

_REPO = Path(__file__).resolve().parents[3]
_ARTIFACTS = _REPO / "docs" / "artifacts"
_INSTALL = _ARTIFACTS / "installation-guide.md"
_DOCS = (
    _INSTALL,
    _ARTIFACTS / "operations-guide.md",
    _ARTIFACTS / "idp-configuration-contract.md",
    _ARTIFACTS / "helm-chart-values-reference.md",
)
_VERSION = re.compile(r"\d+\.\d+")


def _text() -> str:
    return _INSTALL.read_text()


def test_install_guide_has_no_mdns_five_second_delay_guidance() -> None:
    text = _text()

    for stale in ("mDNS", "5 seconds", "pauses for about 10"):
        assert stale not in text, stale


@pytest.mark.parametrize("path", _DOCS, ids=lambda p: p.name)
def test_docs_never_name_the_old_default_hostname(path: Path) -> None:
    assert _OLD_LOCAL not in path.read_text()


def test_hostname_option_row_names_the_new_default_and_the_rejection() -> None:
    rows = [line for line in _text().splitlines() if line.startswith("| `--hostname <name>`")]

    assert len(rows) == 1
    assert "Default: `authentik.test`" in rows[0]
    assert "`.local`" in rows[0]
    assert "rejected" in rows[0]


@pytest.mark.parametrize("surface", ["| Claude Code (CLI) |", "| Claude Desktop - Code tab |"])
def test_surface_table_rows_point_at_issue_239_as_pending(surface: str) -> None:
    rows = [line for line in _text().splitlines() if line.startswith(surface)]

    assert len(rows) == 1
    assert "#239" in rows[0]
    assert "pending" in rows[0]


def test_nothing_claims_239_verified_without_a_version() -> None:
    claims = [
        line
        for line in _text().splitlines()
        if "#239" in line
        and re.search(r"\bverified\b", line, re.IGNORECASE)
        and not _VERSION.search(line)
    ]

    assert claims == []


@pytest.mark.parametrize(
    "heading",
    [
        "### Moving an existing evaluator off a .local hostname",
        "### Recording verification evidence",
    ],
)
def test_install_guide_has_the_upgrade_and_evidence_sections(heading: str) -> None:
    assert heading in _text().splitlines()


def test_values_reference_row_states_the_script_default_and_the_local_rejection() -> None:
    rows = [
        line
        for line in (_ARTIFACTS / "helm-chart-values-reference.md").read_text().splitlines()
        if line.startswith("| `psService.authentikHostname`")
    ]

    assert len(rows) == 1
    assert "`authentik.test`" in rows[0]
    assert "`.local`" in rows[0]


def test_idp_contract_says_the_redirect_uris_hold_no_hostname() -> None:
    text = (_ARTIFACTS / "idp-configuration-contract.md").read_text()
    section = text.split("### Grants and redirect URIs of the `ps-cli` provider", 1)[1]
    section = section.split("\n## ", 1)[0].split("\n### ", 1)[0]

    assert "no hostname" in section
