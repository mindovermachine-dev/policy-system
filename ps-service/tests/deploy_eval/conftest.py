"""Shared fixtures for `scripts/deploy-ps-eval.sh` (GH issue #165).

Same pattern as `deploy_ps/conftest.py`: the real script and the real `scripts/lib/*.sh` files are
copied into an isolated `tmp_path/scripts` tree and run there; only the process boundary is faked.
`curl` is the stateful Authentik API fake from `tests/fixtures/fakes/`; `kubectl`, `helm`,
`podman`/`docker` and `openssl` (network `s_client` and, unless a test asks for the real
binary, certificate generation) are small recording fakes written below and put first on a
minimal `PATH` that contains nothing else but the coreutils the script needs, so "tool missing"
is real.
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
FAKES_DIR = REPO_ROOT / "ps-service" / "tests" / "fixtures" / "fakes"
SCRIPT_TIMEOUT_SECONDS = 60
TOKEN = "s3cr3t-authentik-token-value"
OWNER_EMAIL = "owner@example.com"
NODE_IP = "10.89.0.7"

COPIED_FILES = (
    Path("scripts/deploy-ps-eval.sh"),
    Path("scripts/lib/authentik-owner.sh"),
    Path("scripts/lib/local-tls.sh"),
)

# Coreutils and friends the scripts and libs call. Everything else (kubectl, helm, jq, podman,
# docker, openssl, curl) is placed explicitly so a test can leave one out.
SYSTEM_TOOLS = (
    "bash",
    "sh",
    "env",
    "dirname",
    "basename",
    "cat",
    "chmod",
    "mkdir",
    "mktemp",
    "rm",
    "mv",
    "cp",
    "sed",
    "grep",
    "head",
    "tail",
    "tr",
    "wc",
    "cut",
    "sleep",
    "date",
    "base64",
    "uname",
    "sort",
    "printf",
    "shasum",
    "awk",
    "touch",
    "id",
    "ls",
    "tee",
)

FAKE_KUBECTL = r"""#!/usr/bin/env bash
# Recording fake kubectl for deploy-ps-eval.sh tests.
args=("$@")
if [[ "${args[0]:-}" == "-n" ]]; then args=("${args[@]:2}"); fi
line="${args[*]}"
printf '%s\n' "$*" >> "$PS_TEST_KUBECTL_LOG"
case "$line" in
  "config current-context") printf '%s\n' "${PS_TEST_KUBE_CONTEXT:-kind-policy-system}" ;;
  "config view"*) printf '%s' "${PS_TEST_KUBE_NAMESPACE:-}" ;;
  "get secret policy-system-llm-credentials"*)
    [[ -z "${PS_TEST_LLM_SECRET_MISSING:-}" ]] || { echo 'NotFound' >&2; exit 1; }
    ;;
  "get secret policy-system-authentik-api-token"*)
    printf '%s' "$PS_TEST_SECRET_B64"
    ;;
  "create secret generic"*)
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n' "${args[3]}"
    ;;
  "apply -f -")
    cat >> "$PS_TEST_KUBECTL_APPLIED"
    printf 'secret/applied configured\n'
    ;;
  "rollout status"*) exit "${PS_TEST_ROLLOUT_EXIT:-0}" ;;
  port-forward*)
    if [[ -n "${PS_TEST_PF_FAIL:-}" ]]; then echo 'error: unable to forward port' >&2; exit 1; fi
    printf 'Forwarding from 127.0.0.1:%s -> 9000\n' "${PS_TEST_PF_PORT:-45678}"
    exec sleep 60
    ;;
  *) ;;
esac
exit 0
"""

FAKE_HELM = r"""#!/usr/bin/env bash
# Recording fake helm: `upgrade --install <release> <chart> -f <values.json>` stores the values
# file; `status`/`get values` answer from it. The script hands helm one JSON values file.
printf '%s\n' "$*" >> "$PS_TEST_HELM_LOG"
state="$PS_TEST_HELM_STATE_DIR"
mkdir -p "$state"
case "${1:-}" in
  upgrade)
    shift
    file=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -f|--values) file="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    [[ -n "$file" ]] && cp "$file" "$state/values.json"
    ;;
  status) [[ -f "$state/values.json" ]] || exit 1 ;;
  get) cat "$state/values.json" ;;
esac
exit 0
"""

FAKE_OPENSSL = r"""#!/usr/bin/env bash
# Fake openssl. `s_client` answers from $PS_TEST_SERVED_SEQUENCE (local|other|none, one per call,
# the last repeats). Unless $PS_TEST_REAL_OPENSSL names the real binary, `req`/`x509`/`verify`
# write and read tiny stand-in files (a PEM-looking body plus the SAN line) instead of real
# cryptography.
printf '%s\n' "$*" >> "$PS_TEST_OPENSSL_LOG"
if [[ "${1:-}" == "s_client" ]]; then
  seq="$PS_TEST_SERVED_SEQUENCE"
  line="$(head -n 1 "$seq")"
  if [[ "$(wc -l < "$seq")" -gt 1 ]]; then
    tail -n +2 "$seq" > "$seq.next" && mv "$seq.next" "$seq"
  fi
  case "$line" in
    local) cat "$PS_EVAL_STATE_DIR/leaf.pem" ;;
    other) cat "$PS_EVAL_STATE_DIR/ca.pem" ;;
  esac
  exit 0
fi
if [[ -n "${PS_TEST_REAL_OPENSSL:-}" ]]; then exec "$PS_TEST_REAL_OPENSSL" "$@"; fi
cmd="$1"; shift
# val <flag> args... : the argument following <flag>
val() {
  local f="$1"; shift
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "$f" ]]; then printf '%s' "$2"; return; fi
    shift
  done
}
pem() { printf -- '-----BEGIN CERTIFICATE-----\n%s\n-----END CERTIFICATE-----\n' "$1"; }
case "$cmd" in
  req)
    key="$(val -keyout "$@")"; out="$(val -out "$@")"; cnf="$(val -config "$@")"
    printf 'FAKE-PRIVATE-KEY-%s\n' "$RANDOM$RANDOM" > "$key"
    { pem "CERT$RANDOM$RANDOM"; grep '^CN' "$cnf" | sed 's/^CN = /# cn: /'; } > "$out"
    if grep -q 'CA:TRUE' "$cnf"; then printf '# ca\n' >> "$out"; fi
    ;;
  x509)
    if [[ "$1" == "-req" ]]; then
      out="$(val -out "$@")"; ext="$(val -extfile "$@")"
      {
        pem "LEAF$RANDOM$RANDOM"
        grep '^subjectAltName' "$ext" | sed 's/^subjectAltName = /# SAN: /'
      } > "$out"
      exit 0
    fi
    infile="$(val -in "$@")"
    if [[ -n "$infile" ]]; then src="$(cat "$infile")"; else src="$(cat)"; fi
    case " $* " in
      *" -text "*)
        printf '%s\n' "$src" | sed 's/^# SAN: /Subject Alternative Name: /'
        exit 0
        ;;
      *" -checkend "*) exit 0 ;;
    esac
    body="$(printf '%s\n' "$src" | sed -n '/BEGIN CERT/,/END CERT/p' | grep -v -- '-----')"
    printf 'sha256 Fingerprint=%s\n' "$(printf '%s' "$body" | shasum -a 256 | cut -d' ' -f1)"
    ;;
  *) exit 0 ;;
esac
"""


def _install_python_fake(source: Path, target: Path) -> None:
    lines = source.read_text(encoding="utf-8").splitlines()
    lines[0] = f"#!{sys.executable}"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o755)


def _install_text_fake(text: str, target: Path) -> None:
    target.write_text(text, encoding="utf-8")
    target.chmod(0o755)


@dataclass(frozen=True)
class ScriptRun:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


@dataclass
class EvalFixture:
    """`scripts/deploy-ps-eval.sh` plus its libs in an isolated tree, and the fake toolchain."""

    root: Path
    env_extra: dict[str, str] = field(default_factory=dict)

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def state_dir(self) -> Path:
        return self.root / "eval-tls"

    @property
    def curl_state(self) -> Path:
        return self.root / "curl-state.json"

    @property
    def curl_log(self) -> Path:
        return self.root / "curl.log"

    @property
    def kubectl_log(self) -> Path:
        return self.root / "kubectl.log"

    @property
    def helm_log(self) -> Path:
        return self.root / "helm.log"

    @property
    def helm_state(self) -> Path:
        return self.root / "helm-state"

    def seed_curl(self, **state: Any) -> None:  # noqa: ANN401 - JSON-shaped state values
        current: dict[str, Any] = {"token": TOKEN, "recovery_flow": True}
        if self.curl_state.exists():
            current = json.loads(self.curl_state.read_text())
        current.update(state)
        self.curl_state.write_text(json.dumps(current))

    def served(self, *sequence: str) -> None:
        """Script what a TLS handshake against Authentik's NodePort returns, call by call."""
        (self.root / "served-sequence.txt").write_text("\n".join(sequence) + "\n", encoding="utf-8")

    def remove_tool(self, name: str) -> None:
        (self.bin_dir / name).unlink()

    def add_container_engine(self, name: str) -> None:
        _install_python_fake(FAKES_DIR / "podman", self.bin_dir / name)

    def use_real_openssl(self) -> None:
        real = shutil.which("openssl")
        assert real, "the integration test needs a real openssl"
        self.env_extra["PS_TEST_REAL_OPENSSL"] = real

    def curl_calls(self) -> list[dict[str, Any]]:
        if not self.curl_log.exists():
            return []
        return [json.loads(line) for line in self.curl_log.read_text().splitlines()]

    def curl_urls(self) -> list[str]:
        return [call["url"] for call in self.curl_calls()]

    def curl_users(self) -> dict[str, Any]:
        users: dict[str, Any] = json.loads(self.curl_state.read_text()).get("users", {})
        return users

    def kubectl_calls(self) -> list[str]:
        return self.kubectl_log.read_text().splitlines() if self.kubectl_log.exists() else []

    def helm_calls(self) -> list[str]:
        return self.helm_log.read_text().splitlines() if self.helm_log.exists() else []

    def helm_upgrades(self) -> list[str]:
        return [c for c in self.helm_calls() if c.startswith("upgrade")]

    def deployed_values(self) -> dict[str, Any]:
        values: dict[str, Any] = json.loads((self.helm_state / "values.json").read_text())
        return values

    def everything_observable(self, run: ScriptRun) -> str:
        return run.output + json.dumps(self.curl_calls()) + "\n".join(self.kubectl_calls())

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        expect: int | None = 0,
        **env: str,
    ) -> ScriptRun:
        script = self.root / "scripts" / "deploy-ps-eval.sh"
        full_env = {
            "PATH": str(self.bin_dir),
            "HOME": str(self.root / "home"),
            "PS_CHART_REF": "./charts/policy-system",
            "PS_EVAL_STATE_DIR": str(self.state_dir),
            "PS_TEST_CURL_STATE": str(self.curl_state),
            "PS_TEST_CURL_LOG": str(self.curl_log),
            "PS_TEST_CURL_FIXTURES": str(self.root / "authentik-fixtures"),
            "PS_TEST_KUBECTL_LOG": str(self.kubectl_log),
            "PS_TEST_KUBECTL_APPLIED": str(self.root / "kubectl-applied.yaml"),
            "PS_TEST_HELM_LOG": str(self.helm_log),
            "PS_TEST_HELM_STATE_DIR": str(self.helm_state),
            "PS_TEST_OPENSSL_LOG": str(self.root / "openssl.log"),
            "PS_TEST_SERVED_SEQUENCE": str(self.root / "served-sequence.txt"),
            "PS_TEST_SECRET_B64": base64.b64encode(TOKEN.encode()).decode(),
            "PS_TLS_REFRESH_ATTEMPTS": "5",
            "PS_TLS_REFRESH_INTERVAL_SECONDS": "0.05",
            "PS_PORT_FORWARD_INTERVAL_SECONDS": "0.05",
            **self.env_extra,
            **env,
        }
        bash = shutil.which("bash") or "/bin/bash"
        completed = subprocess.run(  # noqa: S603 - fixture-owned script copy, hermetic PATH
            [bash, str(script), *args],
            cwd=self.root,
            env=full_env,
            capture_output=True,
            text=True,
            input=stdin if stdin is not None else "",
            check=False,
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
        result = ScriptRun(completed.returncode, completed.stdout, completed.stderr)
        if expect is not None:
            assert result.returncode == expect, (
                f"deploy-ps-eval.sh {' '.join(args)} -> {result.returncode}, expected {expect}\n"
                f"--- stdout ---\n{result.stdout}--- stderr ---\n{result.stderr}"
            )
        return result


@pytest.fixture
def eval_fixture(tmp_path: Path) -> EvalFixture:
    fixture = EvalFixture(tmp_path)
    fixture.bin_dir.mkdir()
    (tmp_path / "home").mkdir()
    for relative in COPIED_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, target)
    shutil.copytree(FAKES_DIR / "authentik", tmp_path / "authentik-fixtures")
    for tool in (*SYSTEM_TOOLS, "jq"):
        found = shutil.which(tool)
        if found:
            (fixture.bin_dir / tool).symlink_to(found)
    _install_python_fake(FAKES_DIR / "curl", fixture.bin_dir / "curl")
    _install_text_fake(FAKE_KUBECTL, fixture.bin_dir / "kubectl")
    _install_text_fake(FAKE_HELM, fixture.bin_dir / "helm")
    _install_text_fake(FAKE_OPENSSL, fixture.bin_dir / "openssl")
    fixture.add_container_engine("podman")
    fixture.seed_curl()
    fixture.served("local")
    fixture.env_extra["PS_TEST_PODMAN_NODE_IP"] = NODE_IP
    return fixture
