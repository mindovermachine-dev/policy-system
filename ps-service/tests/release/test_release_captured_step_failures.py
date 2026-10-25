"""A failing sub-command whose output a release script captures is never silent.

`release.sh`, `publish-release.sh` and `create-github-release.sh` capture sub-command output
(`gh tt semver`, the commit classifier, `next-version.sh`, `previous-release-tag.sh`,
`git rev-list`/`rev-parse`) or discard it (`gh tt semver bump|note`, `gh release create|edit`).
When such a sub-command fails, the script must exit non-zero AND say on stderr which step
failed, with the sub-command's own message -- even when that message went to stdout, as gh-tt's
launcher does ("python3 not found in PATH").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import ReleaseFixture

FAILURE_MESSAGE = "stub gh: interpreter not found in PATH"

# Fails `tt semver` (no sub-command) with its message on STDOUT, like the real gh-tt launcher.
FAILING_SEMVER_GH = f"""#!/usr/bin/env bash
if [[ "$1 $2" == "tt semver" && -z "${{3:-}}" ]]; then
  echo "{FAILURE_MESSAGE}"
  exit 1
fi
echo "stub gh: unexpected invocation '$*'" >&2
exit 2
"""

# Fails `tt semver bump` with its message on STDOUT.
FAILING_BUMP_GH = f"""#!/usr/bin/env bash
if [[ "$1 $2 ${{3:-}}" == "tt semver bump" ]]; then
  echo "{FAILURE_MESSAGE}"
  exit 1
fi
echo "stub gh: unexpected invocation '$*'" >&2
exit 2
"""

# Fails the one invocation whose argv starts with `$STUB_GH_FAIL_ON`, message on STDOUT. Every
# other invocation succeeds; `release view` succeeds only when `$STUB_GH_RELEASE_EXISTS` is set.
SELECTIVE_FAILING_GH = f"""#!/usr/bin/env bash
if [[ "$*" == "$STUB_GH_FAIL_ON"* ]]; then
  echo "{FAILURE_MESSAGE}"
  exit 1
fi
if [[ "$1 $2" == "release view" ]]; then
  [[ -n "${{STUB_GH_RELEASE_EXISTS:-}}" ]]
fi
"""


def _write_stub_gh(directory: Path, body: str) -> Path:
    stub = directory / "failing-gh"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_release_reports_failed_baseline_read_on_stderr(
    release_fixture: ReleaseFixture,
) -> None:
    stub = _write_stub_gh(release_fixture.root, FAILING_SEMVER_GH)

    result = release_fixture.run_script(
        "release.sh", env_extra={"PS_RELEASE_GH": str(stub)}, expect=None
    )

    assert result.returncode != 0
    assert "event=step_failed" in result.stderr
    assert "step=read_baseline_tag" in result.stderr
    assert FAILURE_MESSAGE in result.stderr


def test_publish_release_reports_failed_tag_creation_on_stderr(
    release_fixture: ReleaseFixture,
) -> None:
    release_fixture.commit("feat(release): automate lockstep versioning - resolves #79")
    release_fixture.run_script("sync-version-files.sh", "0.12.0")
    stub = _write_stub_gh(release_fixture.root, FAILING_BUMP_GH)

    result = release_fixture.run_script(
        "publish-release.sh",
        "0.12.0",
        "minor",
        env_extra={"PS_RELEASE_GH": str(stub)},
        expect=None,
    )

    assert result.returncode != 0
    assert "event=step_failed" in result.stderr
    assert "step=create_release_tag" in result.stderr
    assert FAILURE_MESSAGE in result.stderr


@pytest.mark.parametrize(
    ("fail_on", "release_exists", "step"),
    [
        ("tt semver note", "", "write_release_notes"),
        ("release create", "", "publish_release"),
        ("release edit", "1", "update_release"),
    ],
)
def test_create_github_release_reports_failed_gh_step_on_stderr(
    release_fixture: ReleaseFixture, fail_on: str, release_exists: str, step: str
) -> None:
    release_fixture.work.run("tag", "0.12.0")
    stub = _write_stub_gh(release_fixture.root, SELECTIVE_FAILING_GH)
    environment = {"PS_RELEASE_GH": str(stub), "STUB_GH_FAIL_ON": fail_on}
    if release_exists:
        environment["STUB_GH_RELEASE_EXISTS"] = release_exists

    result = release_fixture.run_script(
        "create-github-release.sh", "0.12.0", env_extra=environment, expect=None
    )

    assert result.returncode != 0
    assert "event=step_failed" in result.stderr
    assert f"step={step}" in result.stderr
    assert FAILURE_MESSAGE in result.stderr


def test_create_github_release_names_failed_baseline_lookup(
    release_fixture: ReleaseFixture,
) -> None:
    result = release_fixture.run_script("create-github-release.sh", "0.11.0", expect=None)

    assert result.returncode != 0
    assert "step=find_baseline" in result.stderr
