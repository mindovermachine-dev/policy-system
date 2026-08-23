"""Thin process entrypoint: `uv run python -m ps_service`.

Per the L2 coding standard's `ps-cli` precedent ("`__main__.py` only imports
and calls a single `main()` from the package's top-level CLI module — no
logic lives in `__main__.py` itself"), this module contains no logic of its
own; it only dispatches to `ps_service.main.main()`.
"""

from __future__ import annotations

from ps_service.main import main

if __name__ == "__main__":
    main()
