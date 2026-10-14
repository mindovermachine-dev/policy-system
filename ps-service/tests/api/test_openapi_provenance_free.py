"""The served OpenAPI document carries no provenance in any description, summary or title.

The provenance patterns are the single definition kept by `scripts/check_docstring_provenance.py`;
they are loaded from that script by path. Route and model docstrings become OpenAPI descriptions,
so a reference to an internal planning artefact there would reach every REST client.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    import types
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "check_docstring_provenance.py"
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_PROSE_KEYS = frozenset({"description", "summary", "title"})


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_docstring_provenance", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _strings(value: object, path: str) -> Iterator[tuple[str, str]]:
    """Yield `(path, text)` for every string under a prose key at any depth of `value`."""
    if isinstance(value, dict):
        for key, item in cast("dict[str, object]", value).items():
            if key in _PROSE_KEYS and isinstance(item, str):
                yield f"{path}.{key}", item
            else:
                yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(cast("list[object]", value)):
            yield from _strings(item, f"{path}[{index}]")


def _violations(texts: list[tuple[str, str]]) -> list[str]:
    patterns = _load_script().PATTERNS
    found: list[str] = []
    for path, text in texts:
        cleaned = _URL.sub("", text)
        for pattern in patterns:
            match = pattern.regex.search(cleaned)
            if match is not None:
                found.append(f"{path}: {pattern.pattern_id}: {match.group(0)}")
    return found


def _openapi() -> dict[str, object]:
    app = create_app(
        ServiceConfig(
            host="127.0.0.1",
            port=8000,
            graceful_shutdown_seconds=10,
            logging_dir=None,
            is_local_test_bypass_active=True,
            authentik_api_token="test-authentik-token",
            authentik_base_url="https://authentik.example.com",
        )
    )
    return cast("dict[str, object]", app.openapi())


def test_openapi_document_is_provenance_free() -> None:
    document = _openapi()

    paths = cast("dict[str, object]", document["paths"])
    texts = list(_strings(document, "openapi"))
    assert paths
    assert texts
    assert _violations(texts) == []


def test_matcher_flags_known_violating_descriptions() -> None:
    ac_id = "AC" + "-BI-" + "003"
    issue = "issue " + "#" + "12"
    plan = "PLA" + "N.md"
    spike = "spi" + "ke-3"
    samples = [
        (f"sample{i}", f"Returns the run per {text}.")
        for i, text in enumerate([ac_id, issue, plan, spike])
    ]

    flagged = _violations(samples)

    assert {line.split(":")[0] for line in flagged} == {name for name, _ in samples}


def test_matcher_ignores_urls() -> None:
    clean = [("clean", "Reference at https://example.com/issues/#12 for the contract.")]

    assert _violations(clean) == []
