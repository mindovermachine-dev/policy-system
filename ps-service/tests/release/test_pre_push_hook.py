"""`.githooks/pre-push` -- the local twin of on_ready.yml's `lint-commit-header` job.

`gh tt deliver` builds the squeezed commit with `git commit-tree` (no commit-msg hook) and
then pushes, so pre-push is the only local hook that sees a `ready/*` header before CI does.
Each test drives a real `git push` from the fixture clone with `core.hooksPath` pointed at
the repository's own `.githooks` (scoped to the push via `-c`, so the fixture's seed commits
never run the pre-commit hook), proving the hook through git itself rather than by feeding
it stdin by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import ReleaseFixture, ScriptRun

EXPECTED_FORM = "type(scope)!: description"
UNTYPED_HEADER = "company_merge: confirm carry-forward is intended - resolves #32"
TYPED_HEADER = "docs(company_merge): confirm carry-forward is intended - resolves #32"
READY_REF = "refs/heads/ready/32-x"


def _push_with_repo_hooks(
    release_fixture: ReleaseFixture, repo_root: Path, refspec: str, *, expect: int = 0
) -> ScriptRun:
    hooks_path = f"core.hooksPath={repo_root / '.githooks'}"
    return release_fixture.work.run("-c", hooks_path, "push", "origin", refspec, expect=expect)


def test_rejects_a_ready_push_whose_head_header_is_not_conventional(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    release_fixture.commit(UNTYPED_HEADER)

    run = _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{READY_REF}", expect=1)

    assert UNTYPED_HEADER in run.output, "the rejection must name the offending header"
    assert EXPECTED_FORM in run.output
    assert "hint: 'company_merge' is not a type" in run.output
    assert READY_REF in run.stderr
    assert READY_REF not in release_fixture.origin_refs(), "nothing must land on origin"


def test_accepts_a_ready_push_with_a_conventional_head_header(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    sha = release_fixture.commit(TYPED_HEADER)

    _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{READY_REF}")

    assert release_fixture.origin_refs()[READY_REF] == sha


def test_ignores_pushes_to_non_ready_branches(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    """Issue-branch (`wrapup`) pushes carry free-form headers; the hook must not touch them."""
    sha = release_fixture.commit("wip: nothing conventional here")

    _push_with_repo_hooks(release_fixture, repo_root, "HEAD:refs/heads/32-issue-branch")

    assert release_fixture.origin_refs()["refs/heads/32-issue-branch"] == sha


def test_ignores_ready_branch_deletions(release_fixture: ReleaseFixture, repo_root: Path) -> None:
    release_fixture.commit(TYPED_HEADER)
    _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{READY_REF}")

    _push_with_repo_hooks(release_fixture, repo_root, f":{READY_REF}")

    assert READY_REF not in release_fixture.origin_refs()
