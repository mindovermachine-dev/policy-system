"""End-to-end proof of `release.sh`'s no-release path and its fail-fast boundary (S5/S6/S7).

`release.sh` reads the baseline release tag through `gh tt semver`, validates it (CHANGES X-01,
`lib/semver.sh`) *before any git command runs*, classifies the commits since that baseline
(`git log -z ... | bump-from-commits.sh`), and on `bump=none` writes a summary and exits 0 --
no commit, no tag, no version files touched. An invalid baseline must fail before any git
command or file write: the git-shim log (X-01) stays empty, `git status --porcelain` stays
empty, and `HEAD`/tags are unchanged. On `bump!=none` the driver also syncs and verifies the
version-lockstep fields (`sync-version-files.sh` / `verify-version-files.sh`, S7), then commits,
tags and atomically pushes the release (`publish-release.sh`, S8). A push rejected because `main`
moved upstream exits the driver non-zero with no retry; the next run against the new `main` tip
recomputes and self-heals (D-03, S9) -- proven here end-to-end alongside `publish-release.sh`'s
own unit tests.
"""

from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from conftest import ReleaseFixture

CHART_RELATIVE_PATH = "charts/policy-system/Chart.yaml"
VALUES_RELATIVE_PATH = "charts/policy-system/values.yaml"
PLUGIN_RELATIVE_PATH = "ps-skills/policy-system/.claude-plugin/plugin.json"


def test_docs_only_commits_produce_no_commit_no_tag_and_exit_zero(
    release_fixture: ReleaseFixture,
) -> None:
    release_fixture.commit("docs(readme): explain the release flow")
    release_fixture.commit("docs(contributing): add a releasing section")
    head_before = release_fixture.work.head()
    tags_before = release_fixture.work.tags()

    release_fixture.run_script("release.sh")

    assert release_fixture.work.head() == head_before, "the driver must not create a commit"
    assert release_fixture.work.tags() == tags_before, "the driver must not create a tag"
    assert release_fixture.read_gh_log() == ["tt semver"], "baseline is read exactly once"
    summary = release_fixture.read_summary().lower()
    assert "no release" in summary


def test_invalid_baseline_fails_before_any_file_write_or_git_command(
    release_fixture: ReleaseFixture,
) -> None:
    head_before = release_fixture.work.head()
    tags_before = release_fixture.work.tags()

    result = release_fixture.run_script(
        "release.sh",
        log_git=True,
        env_extra={"PS_RELEASE_GH_SEMVER_OUTPUT": "not-a-version"},
        expect=1,
    )

    assert release_fixture.read_git_log() == [], "no git command may run before validation"
    porcelain = release_fixture.work.run("status", "--porcelain").stdout
    assert porcelain == "", f"working tree must stay clean, got:\n{porcelain}"
    assert release_fixture.work.head() == head_before
    assert release_fixture.work.tags() == tags_before
    assert "not-a-version" in result.stderr
    summary = release_fixture.read_summary()
    assert "invalid_version" in summary


def test_feat_commit_leaves_five_fields_synced_to_next_minor(
    release_fixture: ReleaseFixture,
) -> None:
    work_dir = release_fixture.work.path

    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")
    expected_release_version = "0.12.0"  # SEED_TAG 0.11.0 + minor bump (feat)

    release_fixture.run_script("release.sh")

    document = tomllib.loads((work_dir / "ps-service" / "pyproject.toml").read_text("utf-8"))
    assert document["project"]["version"] == expected_release_version
    document = tomllib.loads((work_dir / "ps-cli" / "pyproject.toml").read_text("utf-8"))
    assert document["project"]["version"] == expected_release_version

    chart = yaml.safe_load((work_dir / CHART_RELATIVE_PATH).read_text("utf-8"))
    assert chart["version"] == expected_release_version
    assert chart["appVersion"] == expected_release_version

    values = yaml.safe_load((work_dir / VALUES_RELATIVE_PATH).read_text("utf-8"))
    assert values["psService"]["image"]["tag"] == expected_release_version
    assert values["falkordb"]["image"]["tag"] == "latest"

    plugin = json.loads((work_dir / PLUGIN_RELATIVE_PATH).read_text("utf-8"))
    assert plugin["version"] == expected_release_version

    # S8: release.sh now also commits, tags and pushes -- the release lands on origin.
    assert expected_release_version in release_fixture.work.tags()
    refs = release_fixture.origin_refs()
    assert refs["refs/heads/main"] == release_fixture.work.head()
    assert f"refs/tags/{expected_release_version}" in refs


def test_second_run_after_release_commit_creates_nothing(
    release_fixture: ReleaseFixture,
) -> None:
    """AC-BI-017/018: once a release lands, the very next run finds nothing to release."""
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")

    release_fixture.run_script("release.sh")

    head_after_release = release_fixture.work.head()
    tags_after_release = release_fixture.work.tags()
    assert "0.12.0" in tags_after_release

    release_fixture.run_script("release.sh")

    assert release_fixture.work.head() == head_after_release, "no new commit on the second run"
    assert release_fixture.work.tags() == tags_after_release, "no new tag on the second run"
    summary = release_fixture.read_summary().lower()
    assert "no release" in summary


def test_next_run_after_rejection_releases_successfully(
    release_fixture: ReleaseFixture,
) -> None:
    """D-03/AC-BI-019: a rejected push self-heals -- the next run, from a fresh checkout of the
    new `main` tip, recomputes and releases successfully.
    """
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")

    racer = release_fixture.second_clone()
    racer.commit("fix: racer")
    racer.push()

    release_fixture.run_script("release.sh", expect=1)
    refs_after_rejection = release_fixture.origin_refs()
    assert "refs/tags/0.12.0" not in refs_after_rejection, "the rejected push must not land"

    next_run_checkout = release_fixture.second_clone()  # a fresh checkout of the new `main` tip

    release_fixture.run_script("release.sh", cwd=next_run_checkout.path)

    refs = release_fixture.origin_refs()
    assert "refs/tags/0.11.1" in refs, "self-heal: the next run releases from the new tip"
    assert refs["refs/heads/main"] == next_run_checkout.head()
