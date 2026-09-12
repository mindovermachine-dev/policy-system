"""Arithmetic and boundary validation for `next-version.sh` (AC-BI-006, 007, 008, 013).

`next-version.sh` is the one place PLAN.md's `X` (CHANGES X-14: `release_version` in code) is
computed from a baseline semver and a bump size. It re-validates both its input and its output
against `lib/semver.sh`'s `validate_semver` -- the "second layer at sinks" L1 requires around
arithmetic that feeds a future `git tag`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import ReleaseFixture

BASELINE = "0.11.0"


@pytest.mark.parametrize(
    ("bump_size", "expected_release_version"),
    [
        ("patch", "0.11.1"),
        ("minor", "0.12.0"),
        ("major", "1.0.0"),
    ],
)
def test_patch_minor_major_arithmetic(
    release_fixture: ReleaseFixture, bump_size: str, expected_release_version: str
) -> None:
    result = release_fixture.run_script("next-version.sh", BASELINE, bump_size)

    assert result.stdout.strip() == expected_release_version


def test_rejects_non_semver_output(release_fixture: ReleaseFixture) -> None:
    result = release_fixture.run_script("next-version.sh", "not-a-version", "patch", expect=1)

    assert result.stdout == ""
    assert "not-a-version" in result.stderr
