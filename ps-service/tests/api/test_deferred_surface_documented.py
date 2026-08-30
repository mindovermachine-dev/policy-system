"""AC-BI-012: the deferred remote/auth surface is documented in code.

The runtime posture (loopback bind default, non-loopback startup warning) is
already covered by `tests/test_main.py`; `create_app` itself never binds a
socket, so there is nothing runtime to assert here (PLAN_REVIEWED.md §1.8 Q-D).
This test pins the in-code deferral note instead.
"""

from __future__ import annotations

import inspect

import ps_service.api


def test_api_package_docstring_defers_remote_and_auth_to_issue_39() -> None:
    """Package docstring records the interim posture and defers remote/auth to issue #39.

    It must name the loopback-only, unauthenticated interim access control and point
    remote transport + authentication at issue #39.
    """
    docstring = inspect.getdoc(ps_service.api)

    assert docstring is not None
    lowered = docstring.lower()
    assert "loopback" in lowered
    assert "unauthenticated" in lowered or "authentication" in lowered
    assert "#39" in docstring
