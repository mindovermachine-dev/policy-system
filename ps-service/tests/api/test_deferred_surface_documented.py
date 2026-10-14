"""The api package docstring documents the interim access posture and the deferred remote surface.

The runtime posture (loopback bind default, non-loopback startup warning) is covered by
`tests/test_main.py`; `create_app` never binds a socket, so there is nothing runtime to assert
here. This test pins the in-code note instead.
"""

from __future__ import annotations

import inspect

import ps_service.api


def test_api_package_docstring_documents_loopback_only_and_defers_remote_and_auth() -> None:
    """Package docstring names the loopback-only posture and the deferred remote surface."""
    docstring = inspect.getdoc(ps_service.api)

    assert docstring is not None
    lowered = docstring.lower()
    assert "loopback" in lowered
    assert "unauthenticated" in lowered or "authentication" in lowered
    assert "remote" in lowered
