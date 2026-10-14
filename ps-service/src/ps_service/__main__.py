"""Thin process entrypoint: `uv run python -m ps_service`.

This module contains no logic of its own: it only imports and calls the single `main()` of
`ps_service.main`, which resolves configuration, builds the app and runs it under uvicorn.
"""

from __future__ import annotations

from ps_service.main import main

if __name__ == "__main__":
    main()
