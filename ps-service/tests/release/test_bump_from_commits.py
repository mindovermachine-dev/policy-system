"""`scripts/release/bump-from-commits.sh` -- the conventional-commit classifier.

Contract (PLAN B.1): stdin is the NUL-separated `sha<LF>header<LF>body` stream that
`git log -z --format='%H%n%s%n%b' <baseline>..HEAD` yields (PLAN A-33); stdout is exactly one
line `bump=<major|minor|patch|none>`; structured logs go to stderr and a table with a warning
row per unparsed SHA goes to `$GITHUB_STEP_SUMMARY`.

Rules under test: type is read from the header only; `!` after type/scope or a body line
`BREAKING CHANGE: ` yields major (AC-BI-008); `feat` minor (AC-BI-007); `fix`/`perf` patch
(AC-BI-006); the other seven types none (AC-BI-005); the highest wins (AC-BI-009); a header
that does not parse counts as none and the summary names its SHA (AC-BI-010). Fixtures a-d of
AC-BI-011 use the real `main` SHAs and headers recorded in PLAN A-32.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import ReleaseFixture

BUMP_SCRIPT = "bump-from-commits.sh"
SEED_TAG = "0.11.0"
NUL = "\0"

# Real `main` SHAs and headers (PLAN A-32): (b) slash-separated pseudo-type, (c) untyped prose.
FIXTURE_B_SHA = "3b3c19da5f52f17bcd31321c13a0e38fb3208722"
FIXTURE_B_HEADER = "ps-service/ps-cli: FalkorDB-only readiness gate + LLM fail-fast - resolves #75"
FIXTURE_C_SHA = "72764ca1b3231372d090ae3b0499e86178e5fd86"
FIXTURE_C_HEADER = (
    "Accept authored Policy/Standard/Control in internal ingestion; "
    "remove LLM derivation - resolves #76"
)
# Excerpt of fixture (c)'s real body: the squashed branch log carries typed lines that must
# never be mistaken for the commit's own type.
FIXTURE_C_BODY = (
    "12cf95c: test(internal-seed): real-infra capstone for authored governance spine\n"
    "\n"
    "Retargets test_live_capstone_internal.py at new fixtures.\n"
    "\n"
    "25103fa: feat(internal-seed): accept authored Control + IMPLEMENTED_BY; fix control_id\n"
    "\n"
    "Fixes control_id(standard_node_id, title) to be content-derived.\n"
    "\n"
    "9e1d2c3: fix(internal-seed): keep IMPLEMENTED_BY cardinality 1:0..*\n"
)
FIXTURE_A_HEADER = "feat(x): title - resolves #79"
FIXTURE_D_HEADER = f"chore(release): {SEED_TAG}"
COMMA_SCOPE_HEADER = "fix(ps-service, scripts): report FalkorDB readiness before serving"

NO_RELEASE_HEADERS = (
    "docs(release): explain the flow",
    "chore: tidy the workspace",
    "ci: pin the runner image",
    "test(release): cover the classifier",
    "refactor(ps-service): extract the readiness gate",
    "style: apply ruff format",
    "build: bump hatchling",
)


def _sha(index: int) -> str:
    """A distinct, valid-looking 40-hex SHA for synthesised record `index`."""
    return f"{index:040x}"


def _stream(*records: tuple[str, str, str]) -> str:
    """Synthesise the `git log -z --format='%H%n%s%n%b'` stream (PLAN A-33).

    Each record is `sha<LF>header<LF>body` followed by NUL; git emits the body with its own
    trailing newline and nothing at all when the body is empty.
    """
    return "".join(
        f"{sha}\n{header}\n{body}\n{NUL}" if body else f"{sha}\n{header}\n{NUL}"
        for sha, header, body in records
    )


def _headers_stream(*headers: str) -> str:
    """A stream of body-less records, one per header, with synthetic SHAs."""
    return _stream(*((_sha(index), header, "") for index, header in enumerate(headers, start=1)))


def _bump(release_fixture: ReleaseFixture, stream: str) -> str:
    """Run the classifier over `stream` and return its single stdout line."""
    run = release_fixture.run_script(BUMP_SCRIPT, stdin=stream, expect=0)
    lines = run.stdout.splitlines()
    assert len(lines) == 1, f"stdout must be exactly one line, got {lines!r}"
    return lines[0]


def _summary_rows_naming(release_fixture: ReleaseFixture, sha: str) -> list[str]:
    return [line for line in release_fixture.read_summary().splitlines() if sha in line]


# ---------------------------------------------------------------------------------------------
# S3 -- classifier core (AC-BI-005, 006, 007, 009)
# ---------------------------------------------------------------------------------------------


def test_all_no_release_types_yield_none(release_fixture: ReleaseFixture) -> None:
    assert _bump(release_fixture, _headers_stream(*NO_RELEASE_HEADERS)) == "bump=none"


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("fix(release): correct the arithmetic", id="fix"),
        pytest.param("perf(query): cache the readiness probe", id="perf"),
    ],
)
def test_fix_or_perf_yields_patch(release_fixture: ReleaseFixture, header: str) -> None:
    assert _bump(release_fixture, _headers_stream(header)) == "bump=patch"


def test_feat_yields_minor(release_fixture: ReleaseFixture) -> None:
    assert _bump(release_fixture, _headers_stream("feat(release): automate it")) == "bump=minor"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(("docs: a", "fix: b", "chore: c"), "bump=patch", id="patch-over-none"),
        pytest.param(("fix: a", "feat: b", "perf: c"), "bump=minor", id="minor-over-patch"),
        pytest.param(("feat: a", "fix!: b", "docs: c"), "bump=major", id="major-over-minor"),
    ],
)
def test_mixed_types_yield_the_highest_bump(
    release_fixture: ReleaseFixture, headers: tuple[str, ...], expected: str
) -> None:
    assert _bump(release_fixture, _headers_stream(*headers)) == expected


def test_reads_real_git_log_z_stream_from_fixture_repo(release_fixture: ReleaseFixture) -> None:
    release_fixture.commit("docs(release): describe the release flow")
    fix_sha = release_fixture.commit("fix(release): correct the patch arithmetic", "Closes #1.")
    release_fixture.commit(FIXTURE_D_HEADER)
    stream = release_fixture.work.run(
        "log", "-z", "--format=%H%n%s%n%b", f"{SEED_TAG}..HEAD"
    ).stdout

    assert _bump(release_fixture, stream) == "bump=patch"
    assert f"sha={fix_sha}" in release_fixture.run_script(BUMP_SCRIPT, stdin=stream).stderr


# ---------------------------------------------------------------------------------------------
# S4 -- breaking changes, warnings, summary (AC-BI-008, 010, 011)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        pytest.param((_sha(1), "feat(api)!: drop the v1 routes", ""), id="bang-after-scope"),
        pytest.param((_sha(2), "fix!: reject empty ids", ""), id="bang-after-type"),
        pytest.param(
            (_sha(3), "refactor(api): fold the routers", "BREAKING CHANGE: v1 routes removed"),
            id="breaking-change-footer",
        ),
        pytest.param(
            (_sha(4), "docs: explain", "Body text.\n\nBREAKING-CHANGE: config key renamed"),
            id="breaking-change-footer-with-hyphen",
        ),
    ],
)
def test_bang_or_breaking_change_footer_yields_major(
    release_fixture: ReleaseFixture, record: tuple[str, str, str]
) -> None:
    assert _bump(release_fixture, _stream(record)) == "bump=major"


def test_unparsed_header_counts_as_none_and_summary_names_its_sha(
    release_fixture: ReleaseFixture,
) -> None:
    offender_sha = _sha(0xBAD)
    stream = _stream((_sha(1), "docs: fine", ""), (offender_sha, "wip stuff without a type", ""))

    run = release_fixture.run_script(BUMP_SCRIPT, stdin=stream, expect=0)

    assert run.stdout == "bump=none\n"
    assert f"event=header_unparsed sha={offender_sha}" in run.stderr
    warning_rows = _summary_rows_naming(release_fixture, offender_sha)
    assert warning_rows, "the step summary must name the unparsed SHA"
    assert all("does not parse" in row for row in warning_rows)
    assert len(_summary_rows_naming(release_fixture, "does not parse")) == 1, (
        "only the unparsed header gets a warning row"
    )


def test_fixture_a_deliver_suffix_parses_as_feat(release_fixture: ReleaseFixture) -> None:
    run = release_fixture.run_script(BUMP_SCRIPT, stdin=_headers_stream(FIXTURE_A_HEADER))

    assert run.stdout == "bump=minor\n"
    assert "type=feat" in run.stderr
    assert "header_unparsed" not in run.stderr


def test_fixture_b_slash_type_does_not_parse_and_is_warned(
    release_fixture: ReleaseFixture,
) -> None:
    run = release_fixture.run_script(
        BUMP_SCRIPT, stdin=_stream((FIXTURE_B_SHA, FIXTURE_B_HEADER, ""))
    )

    assert run.stdout == "bump=none\n"
    assert f"event=header_unparsed sha={FIXTURE_B_SHA}" in run.stderr
    assert _summary_rows_naming(release_fixture, FIXTURE_B_SHA)


def test_fixture_c_untyped_does_not_parse_and_is_warned(release_fixture: ReleaseFixture) -> None:
    run = release_fixture.run_script(
        BUMP_SCRIPT, stdin=_stream((FIXTURE_C_SHA, FIXTURE_C_HEADER, FIXTURE_C_BODY))
    )

    assert run.stdout == "bump=none\n", "typed lines in the body must not be read as the type"
    assert f"event=header_unparsed sha={FIXTURE_C_SHA}" in run.stderr
    assert _summary_rows_naming(release_fixture, FIXTURE_C_SHA)


def test_fixture_d_chore_release_is_no_bump(release_fixture: ReleaseFixture) -> None:
    run = release_fixture.run_script(BUMP_SCRIPT, stdin=_headers_stream(FIXTURE_D_HEADER))

    assert run.stdout == "bump=none\n"
    assert "header_unparsed" not in run.stderr, "chore(release) parses; it is simply no-bump"


@pytest.mark.parametrize(
    ("header", "body", "expected"),
    [
        pytest.param(
            "chore: squash of a branch", "feat(a): added\n\nfix(b): repaired", "bump=none", id="c"
        ),
        pytest.param("fix: one repair", "feat(a): from the branch log", "bump=patch", id="fix"),
    ],
)
def test_body_types_are_ignored_only_header_type_counts(
    release_fixture: ReleaseFixture, header: str, body: str, expected: str
) -> None:
    assert _bump(release_fixture, _stream((_sha(1), header, body))) == expected


def test_comma_scope_fix_yields_patch(release_fixture: ReleaseFixture) -> None:
    run = release_fixture.run_script(BUMP_SCRIPT, stdin=_headers_stream(COMMA_SCOPE_HEADER))

    assert run.stdout == "bump=patch\n"
    assert "header_unparsed" not in run.stderr


def test_empty_input_yields_none(release_fixture: ReleaseFixture) -> None:
    run = release_fixture.run_script(BUMP_SCRIPT, stdin="", expect=0)

    assert run.stdout == "bump=none\n"
    assert "event=bump_decided" in run.stderr
