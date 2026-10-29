"""Static guards that CI pulls nothing from a moving ref.

A tag or branch can be re-pointed upstream, and whatever it points at then runs inside the
pipeline that builds and publishes the release images. Every non-local `uses:` in the workflows
and composite actions therefore names a full commit SHA with a trailing `# vX.Y.Z` comment (the
form Dependabot's `github-actions` ecosystem keeps current), and the dev-environment inventory
installs its tools from immutable refs. The checks read the repository files only: no network
access and no `gh`, so they cannot tell whether a SHA resolves (`verify-action-refs.sh` does).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

CI_DEFINITION_GLOBS = (".github/workflows/*.yml", ".github/actions/**/action.yml")
INSITU_INVENTORY = ".insitu.yml"

USES_LINE = re.compile(r"(?:^|[\s-])uses:[ \t]*(?P<ref>\S+)(?P<rest>[^\n]*)")
PINNED_REF = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s*#\s*v\d+(\.\d+)*(\s|$)")
MOVING_BRANCH_REF = re.compile(r"@(experimental|main|master|HEAD)\b")
RAW_SCRIPT_ON_BRANCH = re.compile(
    r"raw\.githubusercontent\.com/[^/\s]+/[^/\s]+/(?:refs/heads/)?(?:main|master|HEAD)/"
)
PLUGIN_INSTALL = "helm plugin install"
VERSION_FLAG = "--version"
LOCAL_ACTION_PREFIX = "./"

PINNED_USES = "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"


def unpinned_actions(label: str, text: str) -> list[str]:
    """A `label:line: ...` problem per non-local `uses:` lacking a SHA and version comment."""
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = USES_LINE.search(line)
        if match is None or match["ref"].startswith(LOCAL_ACTION_PREFIX):
            continue
        if PINNED_REF.match(match["ref"]) is None:
            problems.append(f"{label}:{number}: {match['ref']} is not pinned to a full commit SHA")
        elif VERSION_COMMENT.match(match["rest"]) is None:
            problems.append(f"{label}:{number}: {match['ref']} lacks a trailing '# vX.Y.Z' comment")
    return problems


def mutable_installers(label: str, text: str) -> list[str]:
    """A `label:line: ...` problem per installer read from a branch or an unversioned plugin."""
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if RAW_SCRIPT_ON_BRANCH.search(line):
            problems.append(f"{label}:{number}: installer script is read from a branch")
        if PLUGIN_INSTALL in line and VERSION_FLAG not in line:
            problems.append(f"{label}:{number}: '{PLUGIN_INSTALL}' passes no {VERSION_FLAG}")
    return problems


def _ci_definitions(repo_root: Path) -> list[Path]:
    paths = sorted(path for pattern in CI_DEFINITION_GLOBS for path in repo_root.glob(pattern))
    assert paths, "no workflow or composite action found"
    return paths


@pytest.mark.parametrize(
    ("uses", "reason"),
    [
        pytest.param("actions/checkout@v7.0.1", "tag", id="tag"),
        pytest.param("actions/checkout@v7", "major tag", id="major-tag"),
        pytest.param("devx-cafe/takt-actions/pr-to-ready@experimental", "branch", id="branch"),
        pytest.param("actions/checkout@3d3c42e", "short SHA", id="short-sha"),
        pytest.param("actions/checkout@" + "A" * 40, "upper-case SHA", id="upper-case-sha"),
        pytest.param("actions/checkout", "no ref", id="no-ref"),
    ],
)
def test_an_unpinned_action_ref_fails_naming_the_file_and_line(uses: str, reason: str) -> None:
    text = f"steps:\n  - {PINNED_USES}\n  - uses: {uses} # v7.0.1\n"

    problems = unpinned_actions("workflow.yml", text)

    assert len(problems) == 1, reason
    assert problems[0].startswith("workflow.yml:3: ")


def test_a_pinned_action_without_a_version_comment_fails() -> None:
    sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"

    problems = unpinned_actions("workflow.yml", f"      - uses: actions/checkout@{sha}\n")

    assert problems == [
        f"workflow.yml:1: actions/checkout@{sha} lacks a trailing '# vX.Y.Z' comment"
    ]


def test_a_sha_with_a_version_comment_and_a_local_action_pass() -> None:
    text = f"steps:\n  - {PINNED_USES}\n  - uses: ./.github/actions/prep-runner\n"

    assert unpinned_actions("workflow.yml", text) == []


@pytest.mark.parametrize(
    "command",
    [
        "curl -sSL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/x.bash | bash",
        "curl -sSL https://raw.githubusercontent.com/helm/helm/master/scripts/get-helm-4 | bash",
        "curl -sSL https://raw.githubusercontent.com/helm/helm/HEAD/scripts/get-helm-4 | bash",
        "helm plugin install https://github.com/helm-unittest/helm-unittest.git --verify=false",
    ],
    ids=["main", "master", "head", "plugin-without-version"],
)
def test_a_mutable_installer_fails_naming_the_line(command: str) -> None:
    problems = mutable_installers(INSITU_INVENTORY, f"    command: |\n      {command}\n")

    assert len(problems) == 1
    assert problems[0].startswith(f"{INSITU_INVENTORY}:2: ")


def test_an_immutable_installer_passes() -> None:
    text = (
        "curl -sSL https://raw.githubusercontent.com/rhysd/actionlint/v1.7.12/scripts/x.bash\n"
        "helm plugin install https://github.com/helm-unittest/helm-unittest.git --version v1.2.1\n"
    )

    assert mutable_installers(INSITU_INVENTORY, text) == []


def test_every_action_in_the_repository_is_pinned_to_a_sha_with_a_version_comment(
    repo_root: Path,
) -> None:
    problems = [
        problem
        for path in _ci_definitions(repo_root)
        for problem in unpinned_actions(
            str(path.relative_to(repo_root)), path.read_text(encoding="utf-8")
        )
    ]

    assert problems == []


def test_no_workflow_or_action_follows_a_moving_branch(repo_root: Path) -> None:
    offenders = [
        f"{path.relative_to(repo_root)}:{number}"
        for path in _ci_definitions(repo_root)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if MOVING_BRANCH_REF.search(line)
    ]

    assert offenders == []


def test_the_dev_environment_inventory_installs_from_immutable_refs(repo_root: Path) -> None:
    text = (repo_root / INSITU_INVENTORY).read_text(encoding="utf-8")

    assert mutable_installers(INSITU_INVENTORY, text) == []
