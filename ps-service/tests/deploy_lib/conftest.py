"""Shared harness for the `scripts/lib/*.sh` tests (issue #165).

Detroit style: the real library is sourced by a real `bash`; only the process boundary is faked
(`curl` -> the stateful Authentik fake, `kubectl`/`podman` -> tiny recording fakes on PATH).
Nothing is imported from the fakes: they are copied into a per-test `bin/` directory.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_DIR = REPO_ROOT / "scripts" / "lib"
FAKES_DIR = REPO_ROOT / "ps-service" / "tests" / "fixtures" / "fakes"
TOKEN = "s3cr3t-authentik-token-value"
_BASH_TIMEOUT_SECONDS = 60

FAKE_KUBECTL = r"""#!/usr/bin/env bash
# Recording fake kubectl: logs argv; `get secret` answers with $PS_TEST_SECRET_B64.
printf '%s\n' "$*" >> "$PS_TEST_KUBECTL_LOG"
if [[ "$*" == *"get secret"* ]]; then
  if [[ -n "${PS_TEST_SECRET_MISSING:-}" ]]; then
    echo 'Error from server (NotFound): secrets not found' >&2
    exit 1
  fi
  printf '%s' "$PS_TEST_SECRET_B64"
  exit 0
fi
if [[ "$*" == *"config view"* ]]; then
  printf '%s' "${PS_TEST_KUBE_NAMESPACE:-}"
  exit 0
fi
if [[ "$*" == *"port-forward"* ]]; then
  if [[ -n "${PS_TEST_PF_FAIL:-}" ]]; then
    echo 'error: unable to forward port because pod is not running' >&2
    exit 1
  fi
  if [[ -z "${PS_TEST_PF_SILENT:-}" ]]; then
    printf 'Forwarding from 127.0.0.1:%s -> 9000\n' "${PS_TEST_PF_PORT:-45678}"
  fi
  exec sleep 60
fi
if [[ "$*" == *"apply"* ]]; then
  cat >> "$PS_TEST_KUBECTL_APPLIED"
  exit 0
fi
if [[ "$*" == *"create secret"* ]]; then
  echo "apiVersion: v1"
  exit 0
fi
exit "${PS_TEST_KUBECTL_EXIT:-0}"
"""


@dataclass
class LibHarness:
    """One isolated `bin/` + state area, and a `run` that executes bash against the real libs."""

    root: Path
    env_extra: dict[str, str] = field(default_factory=dict)

    token: str = TOKEN

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def curl_state(self) -> Path:
        return self.root / "curl-state.json"

    @property
    def curl_log(self) -> Path:
        return self.root / "curl.log"

    @property
    def kubectl_log(self) -> Path:
        return self.root / "kubectl.log"

    def seed_curl(self, **state: Any) -> None:  # noqa: ANN401 - JSON-shaped state values
        current: dict[str, Any] = {"token": TOKEN, "recovery_flow": True}
        if self.curl_state.exists():
            current = json.loads(self.curl_state.read_text())
        current.update(state)
        self.curl_state.write_text(json.dumps(current))

    def curl_calls(self) -> list[dict[str, Any]]:
        if not self.curl_log.exists():
            return []
        return [json.loads(line) for line in self.curl_log.read_text().splitlines()]

    def curl_urls(self) -> list[str]:
        return [call["url"] for call in self.curl_calls()]

    def curl_state_users(self) -> dict[str, Any]:
        users: dict[str, Any] = json.loads(self.curl_state.read_text()).get("users", {})
        return users

    def kubectl_calls(self) -> list[str]:
        if not self.kubectl_log.exists():
            return []
        return self.kubectl_log.read_text().splitlines()

    def install_fake_openssl(self, served: list[str]) -> None:
        """Put the fake `openssl` on PATH; `served` scripts what `s_client` returns in turn."""
        target = self.bin_dir / "openssl"
        target.write_text(FAKE_OPENSSL, encoding="utf-8")
        target.chmod(0o755)
        sequence = self.root / "served-sequence.txt"
        sequence.write_text("\n".join(served) + "\n", encoding="utf-8")
        self.env_extra["PS_TEST_SERVED_SEQUENCE"] = str(sequence)
        self.env_extra["PS_TEST_OPENSSL_LOG"] = str(self.root / "openssl.log")

    def run(
        self, script: str, *, path_only: str | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        """Run `script` in `bash -c` with the harness `bin/` first on PATH."""
        full_env = {
            "PATH": path_only if path_only is not None else f"{self.bin_dir}:/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "PS_TEST_LIB_DIR": str(LIB_DIR),
            "PS_TEST_TOKEN": TOKEN,
            "PS_TEST_CURL_STATE": str(self.curl_state),
            "PS_TEST_CURL_LOG": str(self.curl_log),
            "PS_TEST_CURL_FIXTURES": str(self.root / "authentik-fixtures"),
            "PS_TEST_KUBECTL_LOG": str(self.kubectl_log),
            "PS_TEST_KUBECTL_APPLIED": str(self.root / "kubectl-applied.yaml"),
            "PS_TEST_SECRET_B64": base64.b64encode(TOKEN.encode()).decode(),
            "PS_EVAL_STATE_DIR": str(self.root / "state"),
            **self.env_extra,
            **env,
        }
        return subprocess.run(  # noqa: S603 - fixed argv, hermetic PATH, real lib under test
            [shutil.which("bash") or "/bin/bash", "-c", script],
            capture_output=True,
            text=True,
            env=full_env,
            check=False,
            timeout=_BASH_TIMEOUT_SECONDS,
        )


def _install_python_fake(source: Path, target: Path) -> None:
    """Copy a python fake into `bin/` with a shebang pinned to the running interpreter."""
    lines = source.read_text(encoding="utf-8").splitlines()
    lines[0] = f"#!{sys.executable}"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o755)


@pytest.fixture
def lib_harness(tmp_path: Path) -> LibHarness:
    harness = LibHarness(tmp_path)
    harness.bin_dir.mkdir()
    (tmp_path / "home").mkdir()
    _install_python_fake(FAKES_DIR / "curl", harness.bin_dir / "curl")
    _install_python_fake(FAKES_DIR / "podman", harness.bin_dir / "podman")
    kubectl = harness.bin_dir / "kubectl"
    kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
    kubectl.chmod(0o755)
    shutil.copytree(FAKES_DIR / "authentik", tmp_path / "authentik-fixtures")
    for tool in ("jq",):
        found = shutil.which(tool)
        if found:
            (harness.bin_dir / tool).symlink_to(found)
    harness.seed_curl()
    return harness


FAKE_OPENSSL = r"""#!/usr/bin/env bash
# Fake openssl for logic tests of scripts/lib/local-tls.sh (no real cryptography).
#   s_client  -> prints a fake PEM whose body is the next line of $PS_TEST_SERVED_SEQUENCE
#                (the last line repeats), so a test scripts "old cert, then new cert".
#   x509 ... -fingerprint -> prints `sha256 Fingerprint=<sha256 of the PEM body>` for -in FILE
#                or stdin.
printf '%s\n' "$*" >> "$PS_TEST_OPENSSL_LOG"
case "$1" in
  s_client)
    sequence="$PS_TEST_SERVED_SEQUENCE"
    line="$(head -n 1 "$sequence")"
    if [[ "$(wc -l < "$sequence")" -gt 1 ]]; then
      tail -n +2 "$sequence" > "$sequence.next" && mv "$sequence.next" "$sequence"
    fi
    # A scripted NONE means nothing answered the handshake: no certificate on stdout.
    if [[ "$line" != "NONE" ]]; then
      printf -- '-----BEGIN CERTIFICATE-----\n%s\n-----END CERTIFICATE-----\n' "$line"
    fi
    ;;
  x509)
    infile=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "-in" ]]; then infile="$2"; fi
      shift
    done
    if [[ -n "$infile" ]]; then
      body="$(grep -v -- '-----' "$infile")"
    else
      body="$(grep -v -- '-----')"
    fi
    if [[ -z "$body" ]]; then echo 'unable to load certificate' >&2; exit 1; fi
    printf 'sha256 Fingerprint=%s\n' "$(printf '%s' "$body" | shasum -a 256 | cut -d' ' -f1)"
    ;;
  *) exit 0 ;;
esac
"""
