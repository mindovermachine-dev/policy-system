"""`python -m ps_service.domain_schema measure` reports the before/after size (AC-BI-011)."""

from __future__ import annotations

import re
from importlib.resources import files
from typing import TYPE_CHECKING

from ps_service.domain_schema import DOMAIN_SCHEMA, render_slim_schema
from ps_service.domain_schema.__main__ import main

if TYPE_CHECKING:
    import pytest


def test_measure_prints_document_and_slim_character_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = (
        files("ps_service.mcp_interface")
        .joinpath("ps-domain-concepts.md")
        .read_text(encoding="utf-8")
    )

    exit_code = main(["measure"])

    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    assert re.fullmatch(r"before=\d+ after=\d+", out)
    assert out == f"before={len(document)} after={len(render_slim_schema(DOMAIN_SCHEMA))}"
