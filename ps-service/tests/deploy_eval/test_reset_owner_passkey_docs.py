"""Content lint: the docs describe the owner passkey reset instead of an admin-UI device removal.

A lost or stolen owner device is recovered with `--reset-owner-passkey` in both deploy scripts.
These tests pin that every place a reader looks for that case names the flag and says the signing
passkey is enrolled again at the next approval, that no doc tells the owner to remove the device
in the Authentik admin UI (its passkey login fails on the port-forward), that the reset section
names the limits of what it records and revokes, and that the container and deployment
architecture docs describe the revoke operation and its `passkey.revoke` audit action. Anchors are
headings and quoted text, never line numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_ARTIFACTS = _REPO / "docs" / "artifacts"
_OPS = _ARTIFACTS / "operations-guide.md"
_INSTALL = _ARTIFACTS / "installation-guide.md"
_EVAL_INSTALL = _ARTIFACTS / "evaluator-installation-guide.md"
_CONTAINER = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_AZURE = _REPO / "docs" / "architecture" / "customer-azure-deployment.md"

_FLAG = "--reset-owner-passkey"
_RESET_HEADING = "### Resetting the owner's passkeys after a lost or stolen device"
_RESET_ANCHOR = "#resetting-the-owners-passkeys-after-a-lost-or-stolen-device"
_RE_ENROLMENT = "re-enrolled at the next approval"
_HEADING = re.compile(r"^(#{1,6}) ")


def _collapsed(text: str) -> str:
    """Return `text` with every run of whitespace replaced by one space."""
    return " ".join(text.split())


def _section(path: Path, heading: str) -> str:
    """Return the section of `path` under the exact `heading` line, up to the next peer heading.

    The heading must appear exactly once. Fenced code blocks are not treated as headings.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line == heading]
    assert len(starts) == 1, f"expected {heading!r} exactly once in {path.name}"
    match = _HEADING.match(heading)
    assert match is not None, f"{heading!r} is not a heading"
    level = len(match.group(1))
    body: list[str] = []
    in_fence = False
    for line in lines[starts[0] + 1 :]:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        found = None if in_fence else _HEADING.match(line)
        if found is not None and len(found.group(1)) <= level:
            break
        body.append(line)
    return "\n".join(body)


_LOST_DEVICE_LOCATIONS = {
    "ops-eval": (_OPS, "### Owner recovery, certificate renewal and re-running the script"),
    "ops-prod": (_OPS, "### Owner recovery and the Authentik admin UI"),
    "install-guide": (_INSTALL, "#### Recover the owner"),
    "evaluator-common-problems": (_EVAL_INSTALL, "### Common problems"),
}


@pytest.mark.parametrize("location", sorted(_LOST_DEVICE_LOCATIONS))
def test_lost_device_locations_name_the_reset_flag_and_re_enrolment(location: str) -> None:
    """Each lost-device location names the flag and says the signing passkey is re-enrolled."""
    path, heading = _LOST_DEVICE_LOCATIONS[location]
    section = _collapsed(_section(path, heading))
    assert _FLAG in section
    assert _RE_ENROLMENT in section
    assert re.search(r"lost or stolen", section)


def test_no_doc_tells_the_owner_to_remove_a_device_in_the_admin_ui() -> None:
    """No artifact or architecture doc tells the owner to remove a device in the admin UI."""
    removal = re.compile(
        r"owner's user page|device[^.]{0,60}remove it[^.]{0,80}admin UI"
        r"|remove the (?:lost )?device[^.]{0,80}admin UI",
        re.IGNORECASE,
    )
    paths = [*sorted(_ARTIFACTS.glob("*.md")), _CONTAINER, _AZURE]
    offenders = [
        path.name for path in paths if removal.search(_collapsed(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "heading",
    [
        "### Reaching the Authentik admin UI (evaluator)",
        "### Owner recovery and the Authentik admin UI",
    ],
)
def test_admin_ui_sections_say_passkey_login_fails_on_the_port_forward_and_link_the_flag(
    heading: str,
) -> None:
    """Both admin-UI sections say the owner's passkey cannot sign in there and link the reset."""
    section = _collapsed(_section(_OPS, heading))
    assert "Signing in there with the owner's passkey fails" in section
    assert f"]({_RESET_ANCHOR})" in section
    assert "Unverified" not in section
    assert "sign in as the owner" not in section.lower()
    assert "ak create_recovery_key 10 akadmin" in section
    assert "/auth/recovery/use-token/" in section
    assert "with no passkey" in section


def test_reset_section_says_the_authentik_log_is_partial() -> None:
    """The reset section says the printed record is the only complete record of the reset."""
    section = _collapsed(_section(_OPS, _RESET_HEADING))
    assert "the only complete record" in section
    assert "does not record session, OAuth-token or endpoint-device deletions" in section
    assert "`passkey.revoke`" in section
    assert "`system:owner-passkey-reset`" in section
    assert "core_tokens_kept=" in section


def test_reset_section_says_an_issued_access_token_stays_valid_until_it_expires() -> None:
    """The reset section says an access token already issued stays valid until it expires."""
    section = _collapsed(_section(_OPS, _RESET_HEADING))
    assert "refresh tokens are deleted first" in section
    assert "stays valid until it expires" in section


def test_reset_section_gives_both_commands_and_the_voided_approvals() -> None:
    """The reset section gives the eval and prod commands and says pending approvals are voided."""
    section = _collapsed(_section(_OPS, _RESET_HEADING))
    assert f"scripts/deploy-ps-eval.sh {_FLAG} --owner-email" in section
    assert f"scripts/deploy-ps-prod.sh {_FLAG} --owner-email" in section
    assert "voided" in section
    assert _RE_ENROLMENT in section


def test_container_architecture_documents_the_revoke_operation_and_audit_action() -> None:
    """The Passkey Signing section documents the revoke operation and the Audit section its row."""
    signing = _collapsed(_section(_CONTAINER, "### Passkey Signing"))
    for fact in (
        "python -m ps_service.passkey_signing.revoke",
        "`passkey.revoke`",
        "RevokeActorSigning",
        "passkey_signing/revocation.py",
        "passkey_signing/revoke.py",
        "`system:owner-passkey-reset`",
        "stays valid until it expires",
    ):
        assert fact in signing, fact
    audit = _collapsed(_section(_CONTAINER, "### Audit"))
    assert "`passkey.revoke`" in audit
    assert "`system:owner-passkey-reset`" in audit


def test_azure_deployment_documents_the_reset_mode() -> None:
    """The Azure deployment architecture has a reset-mode section and points recovery at it."""
    section = _collapsed(_section(_AZURE, f"### `{_FLAG}`"))
    assert "no Azure call" in section
    text = _collapsed(_AZURE.read_text(encoding="utf-8"))
    assert f"[`{_FLAG}`](#--reset-owner-passkey)" in text
