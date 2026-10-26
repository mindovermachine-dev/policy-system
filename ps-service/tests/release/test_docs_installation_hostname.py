"""The evaluator hostname documentation (`ps.test`, `.local` rejected, migration sections).

Reads the real markdown files off disk; no fakes. The evaluator guide holds the install and
exposure text, the operations guide holds the migration and evidence sections, and the production
installation guide keeps the surface table. Needles for earlier names are built by
concatenation so a repo-wide search for them stays empty.
Deliberately limited to absence rules, the option row, the surface-table rule and section
existence; the wording of the upgrade and evidence sections is reviewed by hand, not pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_OLD_LOCAL = "authentik" + ".local"

_REPO = Path(__file__).resolve().parents[3]
_ARTIFACTS = _REPO / "docs" / "artifacts"
_INSTALL = _ARTIFACTS / "installation-guide.md"
_EVAL = _ARTIFACTS / "evaluator-installation-guide.md"
_OPS = _ARTIFACTS / "operations-guide.md"
_DOCS = (
    _INSTALL,
    _OPS,
    _ARTIFACTS / "idp-configuration-contract.md",
    _ARTIFACTS / "helm-chart-values-reference.md",
    _EVAL,
)
_VERSION = re.compile(r"\d+\.\d+")
_OLD_NAME = "authentik" + ".test"
_MIGRATION_HEADING = f"### Moving an existing evaluator from {_OLD_NAME} to ps.test"
_LOCAL_HEADING = "### Moving an existing evaluator off a .local hostname"


def _text() -> str:
    return _INSTALL.read_text()


def _eval_text() -> str:
    return _EVAL.read_text()


def _ops_text() -> str:
    return _OPS.read_text()


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize("path", [_INSTALL, _EVAL], ids=lambda p: p.name)
def test_install_guides_have_no_mdns_five_second_delay_guidance(path: Path) -> None:
    for stale in ("mDNS", "5 seconds", "pauses for about 10"):
        assert stale not in path.read_text(), stale


@pytest.mark.parametrize("path", _DOCS, ids=lambda p: p.name)
def test_docs_never_name_the_old_default_hostname(path: Path) -> None:
    assert _OLD_LOCAL not in path.read_text()


def test_guide_names_the_default_hostname_and_the_local_rejection() -> None:
    assert "https://ps.test:30443/auth/application/o/ps-cli/" in _squash(_text())
    assert "rejects any `--hostname` that ends in `.local`" in _squash(_eval_text())


@pytest.mark.parametrize("surface", ["| Claude Code (CLI) |", "| Claude Desktop - Code tab |"])
def test_surface_table_rows_state_the_evaluator_hostname_status(surface: str) -> None:
    rows = [line for line in _text().splitlines() if line.startswith(surface)]

    assert len(rows) == 1
    assert "ps.test" in rows[0]
    assert "pending" in rows[0] or re.search(
        r"verified \((?:Claude Code|Desktop app) \d[\d.]*\)", rows[0], re.IGNORECASE
    )


def test_nothing_claims_the_evaluator_hostname_verified_without_a_version() -> None:
    claims = [
        line
        for line in _text().splitlines()
        if "evaluator on `ps.test`" in line
        and re.search(r"\bverified\b", line, re.IGNORECASE)
        and "pending" not in line
        and not _VERSION.search(line)
    ]

    assert claims == []


@pytest.mark.parametrize(
    "heading",
    [
        "### Moving an existing evaluator off a .local hostname",
        _MIGRATION_HEADING,
        "### Recording verification evidence",
    ],
)
def test_operations_guide_has_the_upgrade_and_evidence_sections(heading: str) -> None:
    assert heading in _ops_text().splitlines()


def test_values_reference_row_states_the_script_default_and_the_local_rejection() -> None:
    rows = [
        line
        for line in (_ARTIFACTS / "helm-chart-values-reference.md").read_text().splitlines()
        if line.startswith("| `psService.authentikHostname`")
    ]

    assert len(rows) == 1
    assert "`ps.test`" in rows[0]
    assert "`.local`" in rows[0]


def test_idp_contract_says_the_redirect_uris_hold_no_hostname() -> None:
    text = (_ARTIFACTS / "idp-configuration-contract.md").read_text()
    section = text.split("### Grants and redirect URIs of the `ps-cli` provider", 1)[1]
    section = section.split("\n## ", 1)[0].split("\n### ", 1)[0]

    assert "no hostname" in section


_CHECK = "scripts/deploy-ps-eval.sh --check-exposure"
_KIND_CREATE = "kind create cluster --config"


_EXPOSURE_STEP = "#### Step 2: Check that the machine is safe to expose"


def test_evaluator_guide_runs_the_exposure_check_before_creating_the_cluster() -> None:
    text = _eval_text()
    step = text.split(_EXPOSURE_STEP, 1)[1].split("\n#### ", 1)[0]

    assert _CHECK in step
    assert text.index(_CHECK) < text.index(_KIND_CREATE)
    assert "--allow-public-host" in step


def test_evaluator_guide_recreate_path_points_back_to_the_exposure_check() -> None:
    warning = _eval_text().split("> If a `policy-system` kind cluster already exists", 1)[1]
    recreate = warning.split("\n#### ", 1)[0]

    assert "exposure check" in _squash(recreate)


def test_operations_guide_recreate_path_runs_the_exposure_check_first() -> None:
    text = _squash((_ARTIFACTS / "operations-guide.md").read_text())
    section = text.split("### Recreating the kind cluster", 1)[1].split("### Cleanup", 1)[0]

    assert _CHECK in section
    assert section.index(_CHECK) < section.index("kind create cluster")


def _exposed_section() -> str:
    text = _squash(_eval_text())
    return text.split("### What is exposed", 1)[1].split("### After the install", 1)[0]


def test_exposed_section_describes_the_preflight_and_the_flag() -> None:
    section = _exposed_section()

    assert "--check-exposure" in section
    assert "--allow-public-host" in section
    assert "100.64" in section


def test_guide_says_a_multi_homed_rerun_needs_the_flag_on_both_calls() -> None:
    assert "on both calls" in _exposed_section()


def test_exposed_section_explains_the_configured_approval_origins() -> None:
    section = _exposed_section()

    assert "psService.passkeySigning.allowedOrigins" in section
    assert "421" in section
    assert "https://<hostname>:30443" in section
    assert "localhost:8000" not in section


def test_operations_guide_documents_the_allowed_approval_origins() -> None:
    text = _squash((_ARTIFACTS / "operations-guide.md").read_text())
    section = text.split("### Allowed approval origins", 1)[1].split("\n### ", 1)[0]

    assert "psService.passkeySigning.allowedOrigins" in section
    assert "PS_PASSKEYSIGNING_ALLOWED_ORIGINS" in section
    assert "421" in section
    assert "rp_id" in section
    assert "first" in section


def test_architecture_doc_states_the_configured_origins_and_host_rejection() -> None:
    text = _squash(
        (_REPO / "docs" / "architecture" / "ps-service-container-architecture.md").read_text()
    )

    assert "PS_PASSKEYSIGNING_ALLOWED_ORIGINS" in text
    assert "421" in text


def test_install_guide_describes_the_constrained_short_lived_ca() -> None:
    text = _squash(_eval_text())
    flow = text.split("It creates a local certificate authority", 1)[1].split(" 5. It runs", 1)[0]
    key_bullet = text.split("The local CA private key (`ca.key`", 1)[1].split(
        "The Authentik API token", 1
    )[0]

    assert "name constraint" in flow
    assert "730 days" in flow
    assert "TLS server authentication" in flow
    assert "replaces" in flow
    assert "only for the evaluator hostname" in key_bullet
    assert "Share `ca.pem` only" in key_bullet


def test_architecture_doc_says_the_evaluator_ca_is_name_constrained_and_tls_only() -> None:
    text = _squash((_REPO / "docs" / "architecture" / "customer-azure-deployment.md").read_text())
    counterpart = text.split("**The evaluator counterpart.**", 1)[1].split(
        "**Secret provisioning", 1
    )[0]

    assert "name-constrained" in counterpart
    assert "TLS server authentication" in counterpart


_ROOT_ISSUER = ":30443" + "/application/o/"
_ROOT_ADMIN_UI = ":30443" + "/if/admin/"
_ROOT_PORT_FORWARD_ADMIN_UI = "127.0.0.1:9000" + "/if/admin/"


@pytest.mark.parametrize("path", _DOCS, ids=lambda p: p.name)
def test_docs_give_no_evaluator_issuer_or_admin_ui_without_the_auth_prefix(path: Path) -> None:
    text = path.read_text()
    if path == _OPS:
        text = text.replace(_section(_MIGRATION_HEADING), "")

    assert _ROOT_ISSUER not in text
    assert _ROOT_ADMIN_UI not in text
    assert _ROOT_PORT_FORWARD_ADMIN_UI not in text


@pytest.mark.parametrize("path", _DOCS[:1] + _DOCS[2:], ids=lambda p: p.name)
def test_docs_state_the_evaluator_issuer_under_the_auth_prefix(path: Path) -> None:
    text = _squash(path.read_text())

    assert "https://<hostname>:30443/auth/application/o/ps-cli/" in text or (
        "https://ps.test:30443/auth/application/o/ps-cli/" in text
    )


@pytest.mark.parametrize("path", _DOCS[:2], ids=lambda p: p.name)
def test_guides_open_the_evaluator_admin_ui_through_a_port_forward_under_the_auth_prefix(
    path: Path,
) -> None:
    text = _squash(path.read_text())

    assert (
        "http://127.0.0.1:9000/auth/if/admin/" in text
        or "http://localhost:9000/auth/if/admin/" in text
    )
    assert "https://ps.test:30443/auth/if/admin/" not in text


def test_values_reference_gives_the_evaluator_authentik_base_url_under_the_auth_prefix() -> None:
    text = _squash((_ARTIFACTS / "helm-chart-values-reference.md").read_text())

    assert "evaluator `https://<hostname>:30443/auth`" in text
    assert "`AUTHENTIK_WEB__PATH=/auth/`" in text


def test_evaluator_guide_maps_the_name_to_the_lan_address_and_trusts_for_tls_only() -> None:
    text = _squash(_eval_text())
    client = text.split("### Client on macOS", 1)[1].split("## Part 3", 1)[0]
    reference = text.split("### What the deploy script does", 1)[1]

    assert "LAN address" in reference or "LAN" in reference
    assert "--lan-address" in reference
    assert "default route" in reference
    assert "never edited" in reference
    assert "-r trustRoot -p ssl -k /Library/Keychains/System.keychain" in client
    assert "TLS server authentication" in client
    assert "`ca.pem`" in client
    assert "Never copy `ca.key`" in client
    assert "Reserve this machine's address" in text


def _section(heading: str) -> str:
    """The operations guide text from `heading` to the next `##` or `###` heading."""
    lines = _ops_text().splitlines()
    start = lines.index(heading)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(("## ", "### "))),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_operations_guide_has_the_migration_section_for_the_old_name() -> None:
    headings = [
        line for line in _ops_text().splitlines() if line.startswith("### Moving an existing")
    ]

    assert headings.count(_MIGRATION_HEADING) == 1


def test_the_guide_names_the_old_name_only_inside_the_migration_section() -> None:
    outside = _ops_text().replace(_section(_MIGRATION_HEADING), "")

    assert _OLD_NAME not in outside
    assert _OLD_NAME in _section(_MIGRATION_HEADING)


def test_migration_section_says_all_passkeys_must_be_registered_again() -> None:
    section = _squash(_section(_MIGRATION_HEADING))

    assert "All passkeys must be registered again" in section
    assert "owner" in section
    assert "invitee" in section


def test_migration_section_names_the_old_ca_removal_commands() -> None:
    section = _squash(_section(_MIGRATION_HEADING))

    assert "sudo security remove-trusted-cert -d" in section
    assert "admin trust settings" in section
    assert "sudo security delete-certificate -Z" in section
    assert "-t /Library/Keychains/System.keychain" in section
    assert "sudo rm /usr/local/share/ca-certificates/ps-local-ca.crt" in section
    assert "sudo update-ca-certificates --fresh" in section
    assert "sudo rm /etc/pki/ca-trust/source/anchors/ps-local-ca.pem" in section
    assert "sudo update-ca-trust" in section
    assert "SHA-1" in section
    assert "SHA-256" in section


def test_migration_section_covers_hosts_link_role_reset_colleagues_and_permanence() -> None:
    section = _squash(_section(_MIGRATION_HEADING))

    assert f"remove the lines that map `{_OLD_NAME}`" in section
    assert "`<LAN address> ps.test`" in section
    assert "never edits" in section
    assert "fresh single-use enrolment link" in section
    assert "DELETE FROM access_role_assignments" in section
    assert "LAN colleague" in section
    assert "reserved DHCP address" in section
    assert "permanent" in section
    assert "signing passkey" in section
    assert "ps-cli auth login" in section
    assert "https://ps.test:30443/auth/application/o/ps-cli/" in section
    assert "unverified" in section


def test_migration_section_covers_an_issuer_only_change() -> None:
    section = _squash(_section(_MIGRATION_HEADING))

    assert "same host" in section
    assert "does not issue a new enrolment link" in section


def test_local_hostname_section_moves_to_ps_test_and_never_names_the_old_name() -> None:
    section = _section(_LOCAL_HEADING)

    assert _OLD_NAME not in section
    assert "ps.test" in section
    assert "127.0.0.1 ps.test" not in section
    assert "<LAN address> ps.test" in section


def test_evidence_section_records_ps_test_and_rehearses_the_migration() -> None:
    section = _section("### Recording verification evidence")

    assert _OLD_NAME not in section
    assert "Evaluator hostname:               ps.test" in section
    assert "evaluator on `ps.test` has not yet been run" in _squash(section)
    assert "#moving-an-existing-evaluator-from-" in section


def test_verification_history_records_what_is_and_is_not_verified_on_the_new_name() -> None:
    section = _squash(
        _ops_text().split("### Evaluator verification history", 1)[1].split("\n### ", 1)[0]
    )

    assert "Evaluator on `ps.test`, macOS, same machine as the server: partly verified" in section
    assert "Not verified: an owner-only call after the migration" in section
    assert _OLD_NAME not in section
