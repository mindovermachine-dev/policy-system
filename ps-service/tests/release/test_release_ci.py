"""`.insitu.yml` runs the default pytest suite in the `trunk-worthy` wave (CHANGES X-03).

AC-BI-011 requires the classifier's unit tests to "pass in CI"; until slice S4b nothing in CI
ran pytest at all (PLAN A-24). The default suite is hermetic (DECISIONS F-03), so the whole of
`uv run pytest -q` -- not only `ps-service/tests/release` -- becomes a trunk-worthy check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

INSITU_FILE = ".insitu.yml"
PYTEST_CHECK_ID = "pytest"
PYTEST_COMMAND = "uv run pytest -q"
TRUNK_WORTHY_WAVE = "trunk-worthy"


def _insitu_document(repo_root: Path) -> dict[str, object]:
    document: dict[str, object] = yaml.safe_load(
        (repo_root / INSITU_FILE).read_text(encoding="utf-8")
    )
    return document


def _entries(document: dict[str, object], key: str) -> dict[str, dict[str, object]]:
    """The `inventory:` or `waves:` list keyed by entry id."""
    entries = document.get(key)
    assert isinstance(entries, list), f"{INSITU_FILE} has no `{key}:` list"
    return {
        str(entry["id"]): entry
        for entry in cast("list[dict[str, object]]", entries)
        if "id" in entry
    }


def test_trunk_worthy_wave_runs_the_default_pytest_suite(repo_root: Path) -> None:
    document = _insitu_document(repo_root)
    checks = _entries(document, "inventory")
    waves = _entries(document, "waves")

    assert PYTEST_CHECK_ID in checks, f"no `{PYTEST_CHECK_ID}` check in {INSITU_FILE}"
    assert str(checks[PYTEST_CHECK_ID]["command"]).strip() == PYTEST_COMMAND
    assert TRUNK_WORTHY_WAVE in waves, f"no `{TRUNK_WORTHY_WAVE}` wave in {INSITU_FILE}"
    wave_checks = cast("list[str]", waves[TRUNK_WORTHY_WAVE]["checks"])
    assert PYTEST_CHECK_ID in wave_checks, f"`{TRUNK_WORTHY_WAVE}` does not run `{PYTEST_CHECK_ID}`"
