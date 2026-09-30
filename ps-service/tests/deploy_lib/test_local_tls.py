"""Logic tests for `scripts/lib/local-tls.sh` (issue #165, Slice 3C) with a fake `openssl`.

Real-cryptography checks (CA/leaf extensions, strict TLS handshake, idempotence, permissions)
run real `openssl` and a local TLS server, so they live in `test_local_tls_integration.py`
under the `integration` marker (L2 Testing Patterns; FLAWS F-7). Here: tool preflight, the
Secret apply idiom and the certificate-refresh decision (D-C: apply the blueprint through the
API instead of restarting the server).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import LibHarness

LIB = "$PS_TEST_LIB_DIR"
PRELUDE = f"""
set -uo pipefail
EXIT_FAILURE=1
print_error() {{ local f="$1"; shift; printf "$f" "$@" >&2; }}
source "{LIB}/authentik-owner.sh"
source "{LIB}/local-tls.sh"
AUTHENTIK_API_BASE="https://authentik.local:30443"
AUTHENTIK_LINK_BASE="$AUTHENTIK_API_BASE"
AUTHENTIK_CURL_ARGS=(--cacert /tmp/ca.pem)
AUTHENTIK_API_TOKEN="$PS_TEST_TOKEN"
PS_TLS_REFRESH_ATTEMPTS=3
PS_TLS_REFRESH_INTERVAL_SECONDS=0
"""


def _seed_state_dir(harness: LibHarness, leaf_body: str) -> None:
    state = harness.root / "state"
    state.mkdir(exist_ok=True)
    (state / "leaf.pem").write_text(
        f"-----BEGIN CERTIFICATE-----\n{leaf_body}\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )


def _refresh(harness: LibHarness) -> tuple[int, str, str]:
    proc = harness.run(
        PRELUDE + "refresh_authentik_cert_if_differs authentik.local 30443 /tmp/ca.pem 127.0.0.1"
    )
    return proc.returncode, proc.stdout, proc.stderr


def _apply_calls(harness: LibHarness) -> list[str]:
    return [u for u in harness.curl_urls() if u.endswith("/apply/")]


def test_missing_openssl_is_reported_with_an_install_hint(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(
        f'source "{LIB}/local-tls.sh"; print_error() {{ printf "$@" >&2; }}; require_tls_tools',
        path_only=str(lib_harness.bin_dir),
    )

    assert proc.returncode != 0
    assert "openssl" in proc.stderr
    assert "install" in proc.stderr.lower()


def test_tools_present_passes_the_preflight(lib_harness: LibHarness) -> None:
    lib_harness.install_fake_openssl(["x"])

    proc = lib_harness.run(PRELUDE + "require_tls_tools")

    assert proc.returncode == 0, proc.stderr


def test_secret_is_applied_with_the_dry_run_pipe_and_no_key_material_on_stdout(
    lib_harness: LibHarness,
) -> None:
    state = lib_harness.root / "state"
    state.mkdir()
    for name in ("leaf.pem", "leaf.key", "ca.pem"):
        (state / name).write_text("PRIVATE-MATERIAL", encoding="utf-8")

    proc = lib_harness.run(PRELUDE + "apply_local_tls_secret policy-system ps-local-tls")

    assert proc.returncode == 0, proc.stderr
    calls = lib_harness.kubectl_calls()
    create = next(c for c in calls if "create secret generic ps-local-tls" in c)
    assert "--dry-run=client" in create
    for key in ("tls.crt=", "tls.key=", "ca.crt="):
        assert key in create
    assert any(re.search(r"\bapply\b", c) for c in calls)
    assert "PRIVATE-MATERIAL" not in proc.stdout + proc.stderr


def test_matching_served_certificate_makes_no_api_call(lib_harness: LibHarness) -> None:
    lib_harness.install_fake_openssl(["LEAF"])
    _seed_state_dir(lib_harness, "LEAF")

    code, _, stderr = _refresh(lib_harness)

    assert code == 0, stderr
    assert lib_harness.curl_calls() == []


def test_differing_certificate_applies_the_blueprint_once_and_waits_for_the_new_cert(
    lib_harness: LibHarness,
) -> None:
    lib_harness.install_fake_openssl(["OLD", "OLD", "LEAF"])
    _seed_state_dir(lib_harness, "LEAF")

    code, _, stderr = _refresh(lib_harness)

    assert code == 0, stderr
    assert len(_apply_calls(lib_harness)) == 1
    assert not any("rollout" in c for c in lib_harness.kubectl_calls())


def test_certificate_that_never_changes_fails_after_the_bounded_wait(
    lib_harness: LibHarness,
) -> None:
    lib_harness.install_fake_openssl(["OLD"])
    _seed_state_dir(lib_harness, "LEAF")

    code, _, stderr = _refresh(lib_harness)

    assert code != 0
    assert "certificate" in stderr.lower()
    assert len(_apply_calls(lib_harness)) == 1


def test_missing_blueprint_instance_fails_naming_the_blueprint(lib_harness: LibHarness) -> None:
    lib_harness.install_fake_openssl(["OLD"])
    _seed_state_dir(lib_harness, "LEAF")
    lib_harness.seed_curl(blueprints=[])

    code, _, stderr = _refresh(lib_harness)

    assert code != 0
    assert "Policy System local TLS" in stderr
    assert _apply_calls(lib_harness) == []


def test_api_token_never_reaches_output_or_argv_during_refresh(lib_harness: LibHarness) -> None:
    lib_harness.install_fake_openssl(["OLD", "LEAF"])
    _seed_state_dir(lib_harness, "LEAF")

    _, stdout, stderr = _refresh(lib_harness)

    everything = stdout + stderr + json.dumps(lib_harness.curl_calls())
    assert lib_harness.token not in everything


def test_lib_never_disables_certificate_verification() -> None:
    text = (Path(__file__).resolve().parents[3] / "scripts/lib/local-tls.sh").read_text()

    assert not re.search(r"(^|\s)-k(\s|$)|--insecure|verify=False", text)


def test_served_fingerprint_is_empty_not_fatal_when_nothing_answers_under_set_e(
    lib_harness: LibHarness,
) -> None:
    """Found live at LC2: before Authentik serves TLS at all the handshake yields no certificate;
    under `set -e -o pipefail` the failing `openssl x509` stage must not abort the caller.
    """
    lib_harness.install_fake_openssl(["NONE"])

    proc = lib_harness.run(
        PRELUDE.replace("set -uo pipefail", "set -euo pipefail")
        + 'have="$(served_fingerprint authentik.local 30443 /tmp/ca.pem 127.0.0.1)"\n'
        + 'echo "survived=[$have]"'
    )

    assert proc.returncode == 0, proc.stderr
    assert "survived=[]" in proc.stdout
