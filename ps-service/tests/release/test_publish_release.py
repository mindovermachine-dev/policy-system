"""Commit + tag + atomic push, and the D-03 push-race contract (AC-BI-015/016/017/019).

`publish-release.sh` commits exactly the five version-file paths S7's `sync-version-files.sh`
writes as `chore(release): <release_version>`, tags HEAD through
`${PS_RELEASE_GH:-gh} tt semver bump --<bump_size>` (which runs the real `git tag -a`, PLAN A-03),
asserts the tag resolves to that commit, then pushes both refs to `origin` in one atomic
`git push --atomic origin main <release_version>`. A rejected atomic push (non-fast-forward
because `main` moved upstream, PLAN A-30) leaves origin untouched, exits non-zero, and appends a
summary line naming the race and stating that the next push to `main` recomputes and self-heals
(D-03) -- proven end-to-end in `test_release_driver.py`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import ReleaseFixture

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_SCRIPTS_DIR = REPO_ROOT / "scripts" / "release"

RELEASE_VERSION = "0.12.0"
BUMP_SIZE = "minor"
BOT_IDENTITY = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
EXPECTED_VERSION_FILE_PATHS = [
    "ps-service/pyproject.toml",
    "ps-cli/pyproject.toml",
    "uv.lock",
    "charts/policy-system/Chart.yaml",
    "ps-skills/policy-system/.claude-plugin/plugin.json",
]


def _sync_and_verify(release_fixture: ReleaseFixture, release_version: str) -> None:
    """Run S7's sync + verify scripts, the precondition `publish-release.sh` assumes."""
    release_fixture.run_script("sync-version-files.sh", release_version)
    release_fixture.run_script("verify-version-files.sh", release_version)


def test_single_release_commit_and_annotated_tag_land_on_origin_atomically(
    release_fixture: ReleaseFixture,
) -> None:
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")
    _sync_and_verify(release_fixture, RELEASE_VERSION)
    head_before = release_fixture.work.head()

    release_fixture.run_script("publish-release.sh", RELEASE_VERSION, BUMP_SIZE)

    head_after = release_fixture.work.head()
    assert head_after != head_before, "publish-release.sh must create the release commit"
    refs = release_fixture.origin_refs()
    assert refs["refs/heads/main"] == head_after
    assert f"refs/tags/{RELEASE_VERSION}" in refs, "the annotated tag must land on origin too"
    assert release_fixture.read_gh_log() == [f"tt semver bump --{BUMP_SIZE}"]


def test_commit_and_tag_identity_is_github_actions_bot(release_fixture: ReleaseFixture) -> None:
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")
    _sync_and_verify(release_fixture, RELEASE_VERSION)

    release_fixture.run_script("publish-release.sh", RELEASE_VERSION, BUMP_SIZE)

    commit_identity = (
        release_fixture.work.run("log", "-1", "--format=%an <%ae>%n%cn <%ce>")
        .stdout.strip()
        .splitlines()
    )
    assert commit_identity == [BOT_IDENTITY, BOT_IDENTITY]

    tagger_identity = release_fixture.work.run(
        "for-each-ref",
        "--format=%(taggername) %(taggeremail)",
        f"refs/tags/{RELEASE_VERSION}",
    ).stdout.strip()
    assert tagger_identity == BOT_IDENTITY


def test_tag_points_at_the_release_commit(release_fixture: ReleaseFixture) -> None:
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")
    _sync_and_verify(release_fixture, RELEASE_VERSION)

    release_fixture.run_script("publish-release.sh", RELEASE_VERSION, BUMP_SIZE)

    assert release_fixture.work.run("cat-file", "-t", RELEASE_VERSION).stdout.strip() == "tag"
    tag_commit = release_fixture.work.run("rev-list", "-n", "1", RELEASE_VERSION).stdout.strip()
    assert tag_commit == release_fixture.work.head()


def test_commit_touches_exactly_the_five_version_paths(release_fixture: ReleaseFixture) -> None:
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")
    _sync_and_verify(release_fixture, RELEASE_VERSION)

    release_fixture.run_script("publish-release.sh", RELEASE_VERSION, BUMP_SIZE)

    changed_paths = release_fixture.work.run(
        "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
    ).stdout.split()
    assert sorted(changed_paths) == sorted(EXPECTED_VERSION_FILE_PATHS)


def test_push_is_one_atomic_command_for_main_and_tag() -> None:
    script_text = (RELEASE_SCRIPTS_DIR / "publish-release.sh").read_text(encoding="utf-8")
    atomic_push_pattern = re.compile(r"(?<![\w-])git push --atomic origin main(?![\w-])")
    matching_lines = [
        line
        for line in script_text.splitlines()
        if not line.strip().startswith("#") and atomic_push_pattern.search(line)
    ]
    assert len(matching_lines) == 1, (
        f"expected exactly one `git push --atomic origin main` invocation, found: {matching_lines}"
    )


def test_rejected_atomic_push_leaves_no_commit_and_no_tag_on_origin(
    release_fixture: ReleaseFixture,
) -> None:
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")
    _sync_and_verify(release_fixture, RELEASE_VERSION)

    racer = release_fixture.second_clone()
    racer.commit("fix: racer")
    racer.push()
    refs_before = release_fixture.origin_refs()

    release_fixture.run_script("publish-release.sh", RELEASE_VERSION, BUMP_SIZE, expect=1)

    refs_after = release_fixture.origin_refs()
    assert refs_after == refs_before, "a rejected atomic push must leave origin untouched"
    assert f"refs/tags/{RELEASE_VERSION}" not in refs_after


def test_rejected_push_exits_nonzero_with_main_moved_summary(
    release_fixture: ReleaseFixture,
) -> None:
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")
    _sync_and_verify(release_fixture, RELEASE_VERSION)

    racer = release_fixture.second_clone()
    racer.commit("fix: racer")
    racer.push()

    result = release_fixture.run_script("publish-release.sh", RELEASE_VERSION, BUMP_SIZE, expect=1)

    assert result.returncode == 1
    assert "main moved" in result.stderr.lower()
    summary = release_fixture.read_summary().lower().replace("`", "")
    assert "main moved" in summary
    assert "next push to main" in summary
    assert "recomputes" in summary
