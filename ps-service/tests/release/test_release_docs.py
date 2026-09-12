"""Retirement of the manual release flow (GH issue #79, PLAN S11/S12/S13, AC-BI-027..031).

`scripts/release-ps-service.sh` is the manual script the automated `release` job
(`scripts/release/release.sh` + friends) replaces. S11 deleted the script itself. S12
rewrites `CONTRIBUTING.md`'s Releasing/Getting Started/Delivery Process sections to
describe the new automated, lockstep flow instead of the retired manual one. S13
updates the two docs that reference the `ps-service` image tag (the user guide's
"Updating to the latest version" tip and the Helm values reference table) so neither
implies a human maintains that pin by hand anymore.

Section-extraction helpers below operate on the raw Markdown text rather than a
Markdown parser -- the repo has no Markdown-AST dependency today and these documents'
`##`/`###` heading structure is a stable, simple enough contract for substring/regex
assertions (consistent with this package's existing style of asserting on script/
workflow text directly).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRIBUTING_PATH = REPO_ROOT / "CONTRIBUTING.md"
USER_GUIDE_PATH = REPO_ROOT / "docs" / "artifacts" / "user-guide.md"
VALUES_REFERENCE_PATH = REPO_ROOT / "docs" / "artifacts" / "helm-chart-values-reference.md"

TEN_ALLOWED_TYPES = (
    "feat",
    "fix",
    "perf",
    "docs",
    "chore",
    "ci",
    "test",
    "refactor",
    "style",
    "build",
)


def _section(markdown_text: str, heading: str) -> str:
    """Return the body of the first `##`/`###` section whose heading text matches.

    Slices from the matched heading line up to (but not including) the next heading
    of the same or shallower level, so a `##` section's slice stops at the next `##`
    (its own `###` subsections stay included).
    """
    pattern = re.compile(
        rf"^(#{{2,3}})\s+{re.escape(heading)}\s*$",
        re.MULTILINE,
    )
    match = pattern.search(markdown_text)
    assert match is not None, f"heading {heading!r} not found"
    level = len(match.group(1))
    next_heading = re.compile(rf"^#{{1,{level}}}\s+\S", re.MULTILINE)
    next_match = next_heading.search(markdown_text, match.end())
    end = next_match.start() if next_match else len(markdown_text)
    return markdown_text[match.start() : end]


def _fenced_code_blocks(markdown_text: str) -> list[str]:
    return re.findall(r"```[a-zA-Z]*\n(.*?)```", markdown_text, re.DOTALL)


def test_manual_release_script_is_retired() -> None:
    assert (REPO_ROOT / "scripts" / "release-ps-service.sh").exists() is False


def test_no_ps_cli_v_or_release_ps_service_references_remain() -> None:
    """AC-BI-031's literal grep, run verbatim against the real repo tree.

    Was expected-red after S11 alone (only `scripts/release-ps-service.sh` deleted;
    `CONTRIBUTING.md` still had several `ps-cli-v` references from the retired tagging
    convention section). S12 rewrote those sections to drop the literal `ps-cli-v`
    string entirely (replaced with prose describing the procedure as retired), so this
    now asserts the AC directly: zero matches anywhere in the grepped paths.
    """
    result = subprocess.run(
        [  # noqa: S607 - `grep` is resolved via PATH; args are test literals
            "grep",
            "-rn",
            r"ps-cli-v\|release-ps-service",
            "README.md",
            "CONTRIBUTING.md",
            "docs",
            ".github",
            "scripts",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    # grep exit code 1 == no matches found == the AC is satisfied.
    assert result.returncode == 1, (
        "AC-BI-031 grep found matches (expected to still be red until S12 removes "
        f"CONTRIBUTING.md's ps-cli-v prose):\n{result.stdout}"
    )


def test_contributing_releasing_describes_automated_flow() -> None:
    """AC-BI-027: Releasing describes the automated flow.

    Names the trigger, bump rules, the synced fields, and D-03 push-race self-healing behaviour.
    """
    releasing = _section(CONTRIBUTING_PATH.read_text(encoding="utf-8"), "Releasing")

    assert "release" in releasing.lower()
    assert "push" in releasing.lower() and "main" in releasing
    assert "github-actions[bot]" in releasing

    # The synced version fields (PLAN S7/AC-BI-012, amended by issue #80: `values.yaml` is no
    # longer synced -- its `psService.image.tag` falls back to `Chart.yaml`'s `appVersion`).
    for synced_field in (
        "ps-service/pyproject.toml",
        "ps-cli/pyproject.toml",
        "Chart.yaml",
        "appVersion",
        "plugin.json",
    ):
        assert synced_field in releasing, f"missing synced field {synced_field!r}"

    # D-03 push-race behaviour: a rejected push self-heals on the next push, no
    # manual intervention required.
    assert "race" in releasing.lower()
    assert "self-heal" in releasing.lower() or "self heal" in releasing.lower()


def test_contributing_has_no_manual_release_instructions() -> None:
    """AC-BI-027 / CHANGES X-11.

    No fenced code block may instruct a human to run `gh tt semver bump` or
    `git push origin <tag>` (the retired manual flow) -- prose MAY still explain that
    the automated job runs `gh tt semver bump --<size>` internally. Nowhere in the file
    may it claim there is no version file / nothing to keep in sync -- that was true of
    the old tag-only scheme and is false now that four files/five fields are synced.
    """
    text = CONTRIBUTING_PATH.read_text(encoding="utf-8")

    for code_block in _fenced_code_blocks(text):
        assert "gh tt semver bump" not in code_block, (
            f"manual release instruction survives in a code block:\n{code_block}"
        )
        assert "git push origin <" not in code_block and "git push <tag>" not in code_block, (
            f"manual tag-push instruction survives in a code block:\n{code_block}"
        )

    lowered = text.lower()
    assert "no version file" not in lowered
    assert "nothing to keep in sync" not in lowered
    assert "ps-cli-v" not in text


def test_getting_started_step2_requires_typed_issue_title() -> None:
    """AC-BI-028: step 2 states the issue title must carry a conventional-commit type.

    `gh tt deliver` uses it as the squash commit header, which the release classifier then reads.
    """
    getting_started = _section(CONTRIBUTING_PATH.read_text(encoding="utf-8"), "Getting Started")
    step2 = getting_started.split("\n2.", 1)[1].split("\n3.", 1)[0]

    assert "gh tt workon" in step2
    assert "conventional" in step2.lower()
    assert "squash" in step2.lower()


def test_getting_started_step5_no_longer_says_gh_tt_cuts_releases() -> None:
    """AC-BI-028: step 5 no longer says `gh tt` is used to cut releases."""
    getting_started = _section(CONTRIBUTING_PATH.read_text(encoding="utf-8"), "Getting Started")
    step5 = getting_started.split("\n5.", 1)[1]

    assert "used to cut" not in step5.lower()
    assert "automatic" in step5.lower()
    assert "release" in step5.lower()


def test_contributing_releasing_states_breaking_change_in_body_ships_major() -> None:
    """DECISIONS F-05: a `BREAKING CHANGE` footer/body (not only `feat!`) ships major."""
    releasing = _section(CONTRIBUTING_PATH.read_text(encoding="utf-8"), "Releasing")

    assert "BREAKING CHANGE" in releasing
    assert "major" in releasing.lower()


def test_contributing_releasing_lists_the_ten_allowed_types() -> None:
    """DECISIONS F-06: all ten allowed conventional-commit types are named.

    States other/unparseable types are warned about and do not bump.
    """
    releasing = _section(CONTRIBUTING_PATH.read_text(encoding="utf-8"), "Releasing")

    for commit_type in TEN_ALLOWED_TYPES:
        assert re.search(rf"[`(]{commit_type}[`)!:]", releasing), (
            f"allowed type {commit_type!r} not listed in Releasing"
        )
    assert "warn" in releasing.lower()
    assert "no bump" in releasing.lower() or "no-bump" in releasing.lower()


def test_contributing_releasing_says_dispatch_accepts_bare_or_v_prefixed_tag() -> None:
    """DECISIONS F-11 (superseded by X-05): `workflow_dispatch` accepts a tag input.

    Either bare (`0.12.0`) or `v`-prefixed (`v0.12.0`).
    """
    releasing = _section(CONTRIBUTING_PATH.read_text(encoding="utf-8"), "Releasing")

    assert "workflow_dispatch" in releasing
    assert "0.12.0" in releasing
    assert "v0.12.0" in releasing
    assert "bare" in releasing.lower()


def test_contributing_delivery_process_mentions_rebase_after_release_commit() -> None:
    """DECISIONS F-12: every release adds a `chore(release)` commit to `main`.

    An in-flight `ready/**` branch may need a rebase before its own `gh tt deliver`.
    """
    delivery_process = _section(CONTRIBUTING_PATH.read_text(encoding="utf-8"), "Delivery Process")

    assert "chore(release)" in delivery_process
    assert "rebase" in delivery_process.lower()


def test_user_guide_tip_uses_oci_install_with_no_git_pull() -> None:
    """AC-BI-010 (GH issue #80, supersedes #79's AC-BI-029 for this tip).

    §5's install command and the "Updating to the latest version" tip both use the
    `oci://` form with `--version`; neither contains a `git pull` step, a
    `--reset-values` flag, nor the retired "pinned by hand" hand-pin caveat -- the
    chart's `psService.image.tag` now falls back to `Chart.appVersion` on its own, so
    there is nothing left to hand-pin or reset.
    """
    deploy_section = _section(
        USER_GUIDE_PATH.read_text(encoding="utf-8"), "5. Deploy Policy System"
    )

    assert "oci://ghcr.io/mindovermachine-dev/charts/policy-system" in deploy_section
    assert "--version" in deploy_section
    assert "git pull" not in deploy_section
    assert "--reset-values" not in deploy_section
    assert "pinned by hand" not in deploy_section.lower()

    tip_text = deploy_section[deploy_section.index("Updating to the latest version") :]

    assert "oci://ghcr.io/mindovermachine-dev/charts/policy-system" in tip_text
    assert "--version" in tip_text


def test_user_guide_section_6_documents_pinning_and_rerun_upgrade_path() -> None:
    """AC-BI-010 (GH issue #81).

    §6 states the installer resolves the latest release, shows `PS_CLI_VERSION=X`
    for pinning a specific version, and names re-running the script as the
    documented upgrade path.
    """
    section = _section(USER_GUIDE_PATH.read_text(encoding="utf-8"), "6. Install ps-cli")

    assert "PS_CLI_VERSION" in section
    assert any(re.search(r"PS_CLI_VERSION\s*=", block) for block in _fenced_code_blocks(section)), (
        "no fenced code block shows a PS_CLI_VERSION=... assignment example"
    )

    lowered = section.lower()
    assert "re-run" in lowered or "rerun" in lowered
    assert "upgrade" in lowered


def test_contributing_has_no_commit_sha_git_plus_install_instruction() -> None:
    """AC-BI-011 (GH issue #81).

    No fenced code block instructs a human to install against a raw
    `<commit-sha>` via `git+...#subdirectory=ps-cli` -- that path is retired in
    favor of `PS_CLI_REF`.
    """
    text = CONTRIBUTING_PATH.read_text(encoding="utf-8")

    for code_block in _fenced_code_blocks(text):
        assert re.search(r"git\+.*@<commit-sha>#subdirectory=ps-cli", code_block) is None, (
            f"retired <commit-sha> git+ install instruction survives in a code block:\n{code_block}"
        )


def test_contributing_documents_ps_cli_ref_dev_path() -> None:
    """AC-BI-011 (GH issue #81).

    The section that used to hold the `<commit-sha>` instruction now documents
    `PS_CLI_REF` as the developer install path, so the removal isn't a silent
    deletion with nothing replacing it.
    """
    section = _section(
        CONTRIBUTING_PATH.read_text(encoding="utf-8"), "ps-cli is released the same way"
    )

    assert "PS_CLI_REF" in section


def test_values_reference_image_tag_row_documents_the_appversion_fallback() -> None:
    """AC-BI-011 (GH issue #80, supersedes #79's AC-BI-030 for this row).

    The `psService.image.tag` row states the default is empty and falls back to
    `Chart.appVersion` -- the release job no longer writes this field at all (it only
    keeps `Chart.yaml`'s `appVersion` in lockstep), so the row must not claim the
    field is "maintained" by anything, and must never show a literal version number.
    """
    lines = VALUES_REFERENCE_PATH.read_text(encoding="utf-8").splitlines()
    matching_rows = [line for line in lines if "psService.image.tag" in line]

    assert len(matching_rows) == 1, matching_rows
    row = matching_rows[0]

    assert re.search(r"\d+\.\d+\.\d+", row) is None, row
    assert "appVersion" in row
    assert '""' in row or "empty" in row.lower()
