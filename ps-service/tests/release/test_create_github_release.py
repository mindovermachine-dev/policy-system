"""`previous-release-tag.sh` and `create-github-release.sh` (S10, PLAN B.1, CHANGES X-02).

`previous-release-tag.sh <release_version>` finds the highest bare-semver git tag strictly
below `<release_version>` (by `sort -V`), for use as the note baseline; it exits 1 when none
exists. `create-github-release.sh <release_version>` calls it for the baseline, writes release
notes to a file via `${PS_RELEASE_GH:-gh} tt semver note --from <baseline> --to <release_version>
--filename <file>`, then either `gh release edit` (release already exists) or `gh release create
--verify-tag --title <release_version> --notes-file <file>` (AC-BI-021).

The fake `gh` (`conftest.py`'s `FAKE_GH_SCRIPT`) answers `tt semver note` by writing a real note
from `git log` and answers `release view` by exit 1 unless the test creates the marker file
named by `PS_RELEASE_GH_RELEASE_EXISTS` -- exactly the seam these tests drive.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import ReleaseFixture

RELEASE_VERSION = "0.12.0"
SEED_TAG = "0.11.0"


def _gh_log_lines(release_fixture: ReleaseFixture, prefix: str) -> list[str]:
    """Every fake-`gh` argv line starting with `prefix` (e.g. `"release create"`)."""
    return [line for line in release_fixture.read_gh_log() if line.startswith(prefix)]


def _seed_release_tag(release_fixture: ReleaseFixture) -> None:
    """Tag `HEAD` as `RELEASE_VERSION`, mirroring the real workflow: `create-github-release.sh`
    only ever runs for a tag that already exists (it is what triggered `on_semver.yml`).
    """
    release_fixture.work.run("tag", RELEASE_VERSION)


# --------------------------------------------------------------------------------------
# previous-release-tag.sh
# --------------------------------------------------------------------------------------


def test_previous_release_tag_is_highest_below_x(release_fixture: ReleaseFixture) -> None:
    release_fixture.work.run("tag", "0.10.0")

    result = release_fixture.run_script("previous-release-tag.sh", RELEASE_VERSION)

    assert result.stdout.strip() == SEED_TAG


def test_previous_release_tag_fails_naming_none_when_no_tag_is_below_x(
    release_fixture: ReleaseFixture,
) -> None:
    result = release_fixture.run_script("previous-release-tag.sh", SEED_TAG, expect=1)

    assert result.returncode == 1
    summary = release_fixture.read_summary()
    assert SEED_TAG in summary


# --------------------------------------------------------------------------------------
# create-github-release.sh -- note generation
# --------------------------------------------------------------------------------------


def test_note_is_written_once_via_filename_and_names_the_baseline(
    release_fixture: ReleaseFixture,
) -> None:
    _seed_release_tag(release_fixture)
    release_fixture.run_script("create-github-release.sh", RELEASE_VERSION)

    note_lines = _gh_log_lines(release_fixture, "tt semver note")
    assert len(note_lines) == 1, f"expected exactly one note invocation, found {note_lines}"

    tokens = shlex.split(note_lines[0])
    assert tokens[:3] == ["tt", "semver", "note"]
    assert tokens[tokens.index("--from") + 1] == SEED_TAG
    assert tokens[tokens.index("--to") + 1] == RELEASE_VERSION

    notes_path = Path(tokens[tokens.index("--filename") + 1])
    assert notes_path.is_file(), "the note must be written to the named file"
    assert SEED_TAG in notes_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# create-github-release.sh -- gh release create/edit
# --------------------------------------------------------------------------------------


def test_gh_release_create_receives_tag_verify_tag_and_notes_file(
    release_fixture: ReleaseFixture,
) -> None:
    _seed_release_tag(release_fixture)
    release_fixture.run_script("create-github-release.sh", RELEASE_VERSION)

    create_lines = _gh_log_lines(release_fixture, "release create")
    assert len(create_lines) == 1, f"expected exactly one release create, found {create_lines}"
    assert not _gh_log_lines(release_fixture, "release edit"), "must not also edit"

    tokens = shlex.split(create_lines[0])
    assert tokens[2] == RELEASE_VERSION, "the release must be created for the resolved tag"
    assert "--verify-tag" in tokens
    assert tokens[tokens.index("--title") + 1] == RELEASE_VERSION

    notes_path = Path(tokens[tokens.index("--notes-file") + 1])
    assert notes_path.is_file()


def test_existing_release_is_edited_not_recreated(release_fixture: ReleaseFixture) -> None:
    _seed_release_tag(release_fixture)
    release_marker = release_fixture.root / "release-exists"
    release_marker.touch()

    release_fixture.run_script(
        "create-github-release.sh",
        RELEASE_VERSION,
        env_extra={"PS_RELEASE_GH_RELEASE_EXISTS": str(release_marker)},
    )

    assert not _gh_log_lines(release_fixture, "release create"), "must not create a new release"
    edit_lines = _gh_log_lines(release_fixture, "release edit")
    assert len(edit_lines) == 1, f"expected exactly one release edit, found {edit_lines}"

    tokens = shlex.split(edit_lines[0])
    assert tokens[2] == RELEASE_VERSION
    notes_path = Path(tokens[tokens.index("--notes-file") + 1])
    assert notes_path.is_file()
