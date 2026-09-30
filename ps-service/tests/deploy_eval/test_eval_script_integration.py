"""Real-`openssl` run of `scripts/deploy-ps-eval.sh` (issue #165, AC-BI-007).

Marked `integration` (spawns the real `openssl`, L2 Testing Patterns): the whole script runs with
only the network `s_client` faked, so the certificate the script generates and hands to the
cluster Secret is a real one that must chain to the real local CA and name the hostname.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import EvalFixture

pytestmark = pytest.mark.integration

_OPENSSL_TIMEOUT_SECONDS = 30


def _openssl(*args: str) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv, test-owned files
        ["openssl", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=_OPENSSL_TIMEOUT_SECONDS,
    )
    return out.stdout


def test_script_generated_leaf_chains_to_the_local_ca_and_names_the_host(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.use_real_openssl()
    eval_fixture.served("other", "other", "local")

    run = eval_fixture.run("--owner-email", "owner@example.com")

    state = eval_fixture.state_dir
    assert "OK" in _openssl("verify", "-CAfile", str(state / "ca.pem"), str(state / "leaf.pem"))
    text = _openssl("x509", "-in", str(state / "leaf.pem"), "-noout", "-text")
    assert "DNS:authentik.local" in text
    assert "Authority Key Identifier" in text
    assert "Subject Key Identifier" in text
    assert f"export SSL_CERT_FILE={state}/ca.pem" in run.stdout
