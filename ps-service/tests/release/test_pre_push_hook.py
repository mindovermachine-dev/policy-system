"""`.githooks/pre-push` -- the local twin of two CI gates.

1. on_ready.yml's `lint-commit-header` job: `gh tt deliver` builds the squeezed commit with
   `git commit-tree` (no commit-msg hook) and then pushes, so pre-push is the only local hook
   that sees a `ready/*` header before CI does.
2. trunk-worthy's `ruff-check`/`ruff-format` on the Python files a push adds or changes: git's
   rebase merge backend never invokes `.githooks/pre-commit`, so a conflict resolved under
   `git rebase --continue` reaches origin unchecked (wrapup run 34879493653). The hook checks
   the pushed commit's tree, not the working tree, and only the pushed range.

Each test drives a real `git push` from the fixture clone with `core.hooksPath` pointed at
the repository's own `.githooks` (scoped to the push via `-c`, so the fixture's seed commits
never run the pre-commit hook), proving the hook through git itself rather than by feeding
it stdin by hand. The fixture's mini-workspace `pyproject.toml` carries no `[tool.ruff]`, so
the ruff tests rely only on ruff's default rule set (F401) and default formatting.
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
ISSUE_REF = "refs/heads/32-issue-branch"
PYTHON_FILE = "ps_cli/src/ps_cli/pushed.py"
CLEAN_PYTHON = "X = 1\n"
UNFORMATTED_PYTHON = "X=1\n"
UNUSED_IMPORT_PYTHON = "import os\n"


def _push_with_repo_hooks(
    release_fixture: ReleaseFixture, repo_root: Path, refspec: str, *, expect: int = 0
) -> ScriptRun:
    hooks_path = f"core.hooksPath={repo_root / '.githooks'}"
    return release_fixture.work.run("-c", hooks_path, "push", "origin", refspec, expect=expect)


def _commit_python(release_fixture: ReleaseFixture, content: str, header: str = "wip") -> str:
    """Commit `content` at `PYTHON_FILE` in the work clone (no hooks run); return the SHA."""
    path = release_fixture.work.path / PYTHON_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    release_fixture.work.run("add", PYTHON_FILE)
    release_fixture.work.run("commit", "-q", "-m", header)
    return release_fixture.work.head()


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


# --------------------------------------------------------------------------------------
# ruff on the pushed range (wrapup run 34879493653's gap)
# --------------------------------------------------------------------------------------


def test_rejects_a_push_whose_new_commit_adds_unformatted_python(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    """A brand-new issue branch (no remote tip yet) is checked against its merge-base with main."""
    _commit_python(release_fixture, UNFORMATTED_PYTHON)

    run = _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{ISSUE_REF}", expect=1)

    assert "would be reformatted" in run.output, "ruff format's own report must reach the user"
    assert PYTHON_FILE in run.output
    assert ISSUE_REF in run.stderr
    assert ISSUE_REF not in release_fixture.origin_refs(), "nothing must land on origin"


def test_rejects_a_push_whose_new_commit_fails_ruff_check(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    _commit_python(release_fixture, UNUSED_IMPORT_PYTHON)

    run = _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{ISSUE_REF}", expect=1)

    assert "F401" in run.output, "ruff check's own report must reach the user"
    assert ISSUE_REF not in release_fixture.origin_refs()


def test_accepts_a_push_whose_python_is_clean(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    sha = _commit_python(release_fixture, CLEAN_PYTHON)

    _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{ISSUE_REF}")

    assert release_fixture.origin_refs()[ISSUE_REF] == sha


def test_checks_only_the_range_ahead_of_the_remote_tip(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    """Python already on the remote (however it got there) is out of scope; only new commits count.

    First push lands unformatted Python bypassing the hook (as a pre-hook history would have);
    the second push adds a clean commit on top and must pass, while a third push adding a bad
    commit on top of that must fail -- proving the range starts at the remote's current tip.
    """
    release_fixture.work.run(
        "-c",
        "core.hooksPath=/dev/null",
        "push",
        "-q",
        "origin",
        f"{_commit_python(release_fixture, UNFORMATTED_PYTHON)}:{ISSUE_REF}",
    )
    (release_fixture.work.path / "other.py").write_text(CLEAN_PYTHON, encoding="utf-8")
    release_fixture.work.run("add", "other.py")
    release_fixture.work.run("commit", "-q", "-m", "wip: clean file")
    clean_sha = release_fixture.work.head()

    _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{ISSUE_REF}")
    assert release_fixture.origin_refs()[ISSUE_REF] == clean_sha

    _commit_python(release_fixture, UNUSED_IMPORT_PYTHON, header="wip: bad again")
    _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{ISSUE_REF}", expect=1)
    assert release_fixture.origin_refs()[ISSUE_REF] == clean_sha, "the bad push must not land"


def test_checks_the_pushed_commit_not_the_working_tree(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    """Fixing the file in the working tree without amending must not mask the pushed commit."""
    _commit_python(release_fixture, UNFORMATTED_PYTHON)
    (release_fixture.work.path / PYTHON_FILE).write_text(CLEAN_PYTHON, encoding="utf-8")

    run = _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{ISSUE_REF}", expect=1)

    assert "would be reformatted" in run.output
    assert ISSUE_REF not in release_fixture.origin_refs()


def test_ruff_gate_applies_to_ready_pushes_alongside_the_header_lint(
    release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    """A `ready/*` push with a conventional header still fails on unformatted Python."""
    _commit_python(release_fixture, UNFORMATTED_PYTHON, header=TYPED_HEADER)

    run = _push_with_repo_hooks(release_fixture, repo_root, f"HEAD:{READY_REF}", expect=1)

    assert "would be reformatted" in run.output
    assert EXPECTED_FORM not in run.output, "the conventional header must not be blamed"
    assert READY_REF not in release_fixture.origin_refs()
