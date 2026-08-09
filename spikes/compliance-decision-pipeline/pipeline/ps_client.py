# © 2026 Cartman ApS. All rights reserved.
"""Thin wrapper around spikes/cli-tool-semantics/ps.py -- Stage 4's graph
access. Reuses the CLI's proven deterministic query surface rather than
reimplementing query logic (README.md, "Other prior findings this design
reuses directly"). Every Stage 4 sub-check below independently re-queries
through here -- never reuses whatever query produced the original answer
(the lesson of SWE-M1/RM-E1's query-construction misses, see README.md
Stage 4).

Hardcoded to /usr/bin/python3, same reason ps.py itself is: the repo
.venv's python3 lacks the falkordb package.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

_PS_PY = (
    Path(__file__).resolve().parent.parent.parent / "cli-tool-semantics" / "ps.py"
)


class PsClientError(RuntimeError):
    pass


def _run(args: List[str]) -> Dict[str, Any]:
    proc = subprocess.run(
        ["/usr/bin/python3", str(_PS_PY)] + args + ["--format", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise PsClientError(f"ps {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def cypher(query: str) -> Dict[str, Any]:
    """Read-only escape hatch -- ps.py itself rejects write clauses."""
    return _run(["cypher", query])


def query_catalog(capability_id_or_name: str) -> Dict[str, Any]:
    return _run(["query", "catalog", capability_id_or_name])


def capabilities_list(filter_text: Optional[str] = None) -> List[Dict[str, Any]]:
    args = ["capabilities", "list"]
    if filter_text:
        args += ["--filter", filter_text]
    return _run(args)
