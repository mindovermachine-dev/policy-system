"""The restore entry points document rejections without naming a base64 decode failure."""

from __future__ import annotations

import inspect
import re

from ps_service.api.errors import RestoreArtifactRejectedError
from ps_service.api.restore_orchestration import run_restoration
from ps_service.api.routes import create_restoration

_SECTION_HEADER = re.compile(r"^\w[\w ]*:$")


def _raises_section(obj: object) -> str:
    """Return the text of the ``Raises:`` section of ``obj``'s docstring, or ``""``."""
    lines = (inspect.getdoc(obj) or "").splitlines()
    collected: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == "Raises:":
            inside = True
            continue
        if inside and _SECTION_HEADER.match(line):
            break
        if inside:
            collected.append(line)
    return "\n".join(collected)


def test_restore_rejection_docstrings_do_not_mention_base64() -> None:
    run_raises = _raises_section(run_restoration)
    assert "RestoreArtifactRejectedError" in run_raises

    for documented in (run_restoration, create_restoration):
        assert "base64" not in _raises_section(documented).lower()

    rejected_doc = inspect.getdoc(RestoreArtifactRejectedError) or ""
    assert "base64" not in rejected_doc.lower()
