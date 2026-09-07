"""F5: `ps-cli`'s default `fixtures_root` and `ps-service`'s own `_FIXTURES_ROOT`
must agree on the same absolute path (`<repo>/test-data`).

Both are `uv` workspace members sharing one venv, run under one root `pytest`
invocation (`testpaths = ["ps-service/tests", "ps-cli/tests"]`) -- this same-
process import proves the two independently-derived values agree without
either package importing the other in production code (mirrors D7's
packaged-schema drift guard, `test_schema_packaging_matches_docs_source.py`).
"""

from __future__ import annotations

from pathlib import Path

from ps_cli.config import load_config

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_default_fixtures_root_matches_ps_service_default(tmp_path: Path) -> None:
    """`ps-cli`'s resolved default `fixtures_root`, anchored at the repo root (D3's
    "same machine" deployment assumption), resolves to the same absolute path as
    `ps-service`'s own `_FIXTURES_ROOT` constant.
    """
    from ps_service.api.fixtures import (
        _FIXTURES_ROOT,  # pyright: ignore[reportPrivateUsage] -- drift guard needs the resolved constant itself
    )

    cli_config = load_config(cwd=_REPO_ROOT, config_dir=tmp_path)
    resolved_cli_fixtures_root = (_REPO_ROOT / cli_config.fixtures_root).resolve()

    assert resolved_cli_fixtures_root == _FIXTURES_ROOT.resolve()
