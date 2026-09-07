"""AC-BI-010 layer 2: resolve a `POST /ingestions` internal request's `fixture_path`.

`InternalIngestionRequest.fixture_path`'s own Pydantic `field_validator`
already rejects a leading `/`, a backslash, or a `..` segment on the raw
string (layer 1, `ps_service/api/models.py`). This module is the second,
independent layer AC-BI-010 requires: it resolves the string against PS
Service's own fixtures root and verifies the *result* is still contained
within it -- catching anything the string-only layer-1 check cannot (a
`Path.resolve()` normalization it didn't model), before any pipeline stage
runs, and independent of `ps-cli`'s own local validation (D3's "in addition
to," not "instead of").
"""

from __future__ import annotations

from pathlib import Path

from ps_service.api.errors import FixturePathError

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES_ROOT = _REPO_ROOT / "test-data"
"""PS Service's own fixtures root -- `<repo>/test-data`, mirroring `ps-cli`'s
own default `fixtures_root` resolution (D3: both point at the same physical
location in local dev, independently derived, no shared code). See
`ps-cli/tests/test_fixtures_root_agrees_with_ps_service.py` for the drift
guard proving the two stay in agreement."""


def resolve_fixture_path(fixture_path: str) -> Path:
    """Resolve `fixture_path` against `_FIXTURES_ROOT`, rejecting anything unsafe.

    Args:
        fixture_path: The relative path submitted on the request body,
            already layer-1-validated by `InternalIngestionRequest`.

    Returns:
        The resolved, existing `.json` file's absolute path.

    Raises:
        FixturePathError: `fixture_path` resolves outside `_FIXTURES_ROOT`,
            does not end in `.json`, or does not exist as a regular file.
    """
    candidate = (_FIXTURES_ROOT / fixture_path).resolve()
    fixtures_root = _FIXTURES_ROOT.resolve()
    try:
        candidate.relative_to(fixtures_root)
    except ValueError:
        raise FixturePathError(
            f"fixture_path {fixture_path!r} resolves outside the fixtures root"
        ) from None
    if candidate.suffix != ".json":
        raise FixturePathError(f"fixture_path {fixture_path!r} must reference a '.json' file")
    if not candidate.is_file():
        raise FixturePathError(f"fixture_path {fixture_path!r} does not exist")
    return candidate
