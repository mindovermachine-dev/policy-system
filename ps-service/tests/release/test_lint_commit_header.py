"""`scripts/release/lint-commit-header.sh` -- the conventional-header guard rail.

AC-BI-024 (header mode names the offending header and the expected form), AC-BI-025's lint
half (range mode names the offending SHA; HEAD fallback when `before` is unknown) and
AC-BI-026 (the `deliver` suffix ` - resolves #N` is part of the description). The regex is
the single shared one from `lib/conventional-header.sh` (PLAN B.6 as amended by CHANGES X-04).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import ReleaseFixture

LINT_SCRIPT = "lint-commit-header.sh"
EXPECTED_FORM = "type(scope)!: description"
ZERO_SHA = "0" * 40
UNKNOWN_SHA = "deadbeef" * 5

# Real `main` headers (PLAN A-32): (b) slash-separated pseudo-type, (c) untyped prose.
FIXTURE_B_HEADER = "ps-service/ps-cli: FalkorDB-only readiness gate + LLM fail-fast - resolves #75"
FIXTURE_C_HEADER = (
    "Accept authored Policy/Standard/Control in internal ingestion; "
    "remove LLM derivation - resolves #76"
)
DELIVER_SUFFIX_HEADER = "feat(x): title - resolves #79"
COMMA_SCOPE_HEADER = "fix(ps-service, scripts): report FalkorDB readiness before serving"


def test_rejects_untyped_header_and_names_expected_form(release_fixture: ReleaseFixture) -> None:
    run = release_fixture.run_script(LINT_SCRIPT, "--header", FIXTURE_C_HEADER, expect=1)

    assert FIXTURE_C_HEADER in run.stdout, "the failure output must show the offending header"
    assert EXPECTED_FORM in run.stdout, "the failure output must show the expected form"
    assert FIXTURE_C_HEADER in release_fixture.read_summary()


def test_rejects_fixture_b_slash_separated_pseudo_type(release_fixture: ReleaseFixture) -> None:
    run = release_fixture.run_script(LINT_SCRIPT, "--header", FIXTURE_B_HEADER, expect=1)

    assert FIXTURE_B_HEADER in run.stdout
    assert EXPECTED_FORM in run.stdout


def test_accepts_typed_header_with_deliver_suffix(release_fixture: ReleaseFixture) -> None:
    run = release_fixture.run_script(LINT_SCRIPT, "--header", DELIVER_SUFFIX_HEADER, expect=0)

    assert EXPECTED_FORM not in run.stdout


def test_accepts_comma_separated_multi_word_scope(release_fixture: ReleaseFixture) -> None:
    release_fixture.run_script(LINT_SCRIPT, "--header", COMMA_SCOPE_HEADER, expect=0)


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("revert: undo the last feature", id="revert-is-not-an-allowed-type"),
        pytest.param("feat(a(b)): nested scope", id="nested-parentheses-in-scope"),
        pytest.param("feat(add). create the thing", id="missing-colon-separator"),
        pytest.param("", id="empty-header"),
    ],
)
def test_rejects_headers_outside_the_ten_allowed_types_or_form(
    release_fixture: ReleaseFixture, header: str
) -> None:
    run = release_fixture.run_script(LINT_SCRIPT, "--header", header, expect=1)

    assert EXPECTED_FORM in run.stdout


def test_range_mode_fails_naming_the_offending_sha(release_fixture: ReleaseFixture) -> None:
    baseline_sha = release_fixture.work.head()
    good_before = release_fixture.commit("feat(release): typed before the offender")
    offender = release_fixture.commit("wip stuff without a type")
    good_after = release_fixture.commit("fix(release): typed after the offender")

    run = release_fixture.run_script(
        LINT_SCRIPT, "--range", f"{baseline_sha}..{good_after}", expect=1
    )

    assert f"{offender} wip stuff without a type" in run.stdout
    assert EXPECTED_FORM in run.stdout
    assert good_before not in run.stdout
    assert good_after not in run.stdout
    assert offender in release_fixture.read_summary()


def test_range_mode_passes_when_every_commit_in_the_range_is_typed(
    release_fixture: ReleaseFixture,
) -> None:
    baseline_sha = release_fixture.work.head()
    release_fixture.commit("docs(release): explain the flow")
    head = release_fixture.commit(DELIVER_SUFFIX_HEADER)

    run = release_fixture.run_script(LINT_SCRIPT, "--range", f"{baseline_sha}..{head}", expect=0)

    assert EXPECTED_FORM not in run.stdout


@pytest.mark.parametrize(
    "before",
    [
        pytest.param("", id="empty-before-as-on-workflow-dispatch"),
        pytest.param(ZERO_SHA, id="all-zero-before-as-on-first-push"),
        pytest.param(UNKNOWN_SHA, id="before-unknown-to-this-checkout"),
    ],
)
def test_range_mode_falls_back_to_head_when_before_is_unknown(
    release_fixture: ReleaseFixture, before: str
) -> None:
    older_offender = release_fixture.commit("older untyped commit")
    head = release_fixture.commit("fix(release): typed HEAD")

    run = release_fixture.run_script(LINT_SCRIPT, "--range", f"{before}..{head}", expect=0)

    assert older_offender not in run.output, "only HEAD is linted under the fallback"


def test_range_mode_fallback_still_fails_on_an_untyped_head(
    release_fixture: ReleaseFixture,
) -> None:
    offender = release_fixture.commit("untyped HEAD after a force push")

    run = release_fixture.run_script(LINT_SCRIPT, "--range", f"{ZERO_SHA}..{offender}", expect=1)

    assert f"{offender} untyped HEAD after a force push" in run.stdout
    assert EXPECTED_FORM in run.stdout


def test_range_mode_defaults_after_to_head_when_omitted(release_fixture: ReleaseFixture) -> None:
    baseline_sha = release_fixture.work.head()
    offender = release_fixture.commit("no type here either")

    run = release_fixture.run_script(LINT_SCRIPT, "--range", f"{baseline_sha}..", expect=1)

    assert offender in run.stdout


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param((), id="no-arguments"),
        pytest.param(("--header",), id="header-flag-without-value"),
        pytest.param(("--range", "abc"), id="range-without-dotdot"),
        pytest.param(("--bogus", "x"), id="unknown-flag"),
    ],
)
def test_usage_errors_exit_two_and_print_usage(
    release_fixture: ReleaseFixture, argv: tuple[str, ...]
) -> None:
    run = release_fixture.run_script(LINT_SCRIPT, *argv, expect=2)

    assert "usage:" in run.stderr
