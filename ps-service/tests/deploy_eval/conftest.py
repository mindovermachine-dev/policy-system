"""Shared fixtures for `scripts/deploy-ps-eval.sh`.


Same pattern as `deploy_ps/conftest.py`: the real script and the real `scripts/lib/*.sh` files are
copied into an isolated `tmp_path/scripts` tree and run there; only the process boundary is faked.
`curl` is the stateful Authentik API fake from `tests/fixtures/fakes/`; `kubectl`, `helm`,
`podman`/`docker`, `ip`/`route`/`ipconfig`, `uname`, `security` and `openssl` (network `s_client`;
certificate generation and parsing run the real binary) are small recording fakes written below
and put first on a minimal `PATH` that contains nothing else but the coreutils the script needs,
so "tool missing" is real.
"""

from __future__ import annotations

import base64
import json
import os
import pty
import shlex
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
    Path("scripts/lib/chart-version.sh"),
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
  "config current-context")
    [[ -z "${PS_TEST_NO_KUBE_CONTEXT:-}" ]] || { echo 'error: no current-context' >&2; exit 1; }
    printf '%s\n' "${PS_TEST_KUBE_CONTEXT:-kind-policy-system}"
    ;;
  "get deployments -o name"*)
    [[ -z "${PS_TEST_CLUSTER_UNREACHABLE:-}" ]] || { echo 'Unable to connect' >&2; exit 1; }
    printf 'deployment.apps/policy-system-ps-service\n'
    ;;
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
    [[ -z "${PS_TEST_APPLY_FAIL:-}" ]] || { echo 'error: apply failed' >&2; exit 1; }
    cat >> "$PS_TEST_KUBECTL_APPLIED"
    printf 'secret/applied configured\n'
    ;;
  "get service policy-system-proxy -o name")
    [[ -n "${PS_TEST_PROXY_SERVICE:-}" ]] || { echo 'NotFound' >&2; exit 1; }
    printf 'service/policy-system-proxy\n'
    ;;
  "get deployment ingress-nginx-controller -o name")
    [[ -z "${PS_TEST_INGRESS_MISSING:-}" ]] || { echo 'NotFound' >&2; exit 1; }
    ;;
  "rollout status deployment/ingress-nginx-controller"*)
    [[ -z "${PS_TEST_INGRESS_NOT_READY:-}" ]] || exit 1
    ;;
  "get endpointslices"*)
    [[ -n "${PS_TEST_INGRESS_NO_ENDPOINTS:-}" ]] || printf '10.244.0.7'
    ;;
  "rollout status"*) exit "${PS_TEST_ROLLOUT_EXIT:-0}" ;;
  exec*ps_service.passkey_signing.revoke*) exec "$PS_TEST_SIGNING_FAKE" "${args[@]}" ;;
  exec*) [[ -z "${PS_TEST_EXEC_FAIL:-}" ]] || { echo 'urlopen error' >&2; exit 1; } ;;
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
# Fake openssl. When $PS_TEST_REAL_OPENSSL names the real binary (the fixture default), every
# other subcommand runs it. Without it, `req`/`x509`/`verify` write and read tiny stand-in files
# (a PEM-looking body plus the SAN line) instead of real cryptography.
printf '%s\n' "$*" >> "$PS_TEST_OPENSSL_LOG"
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


FAKE_IP = r"""#!/usr/bin/env bash
# Fake `ip`: `ip route ...` prints the route the test wrote to $PS_TEST_IP_ROUTE_OUTPUT (and fails
# when there is none); anything else prints the address lines in $PS_TEST_IP_OUTPUT.
if [[ "${1:-}" == route ]]; then
  [[ -s "$PS_TEST_IP_ROUTE_OUTPUT" ]] || exit 1
  cat "$PS_TEST_IP_ROUTE_OUTPUT"
  exit 0
fi
cat "$PS_TEST_IP_OUTPUT"
"""

FAKE_ROUTE = r"""#!/usr/bin/env bash
# Fake macOS `route -n get default`: prints the interface the test chose in $PS_TEST_ROUTE_OUTPUT.
[[ -s "$PS_TEST_ROUTE_OUTPUT" ]] || exit 1
cat "$PS_TEST_ROUTE_OUTPUT"
"""

FAKE_IPCONFIG = r"""#!/usr/bin/env bash
# Fake macOS `ipconfig getifaddr <interface>`: prints $PS_TEST_IPCONFIG_ADDRESS.
[[ "${1:-}" == getifaddr && -s "$PS_TEST_IPCONFIG_ADDRESS" ]] || exit 1
cat "$PS_TEST_IPCONFIG_ADDRESS"
"""

FAKE_UNAME = r"""#!/usr/bin/env bash
# Fake `uname`: prints the platform the test chose in $PS_TEST_PLATFORM.
printf '%s\n' "$PS_TEST_PLATFORM"
"""

FAKE_SECURITY = r"""#!/usr/bin/env bash
# Fake macOS `security`: logs its arguments; `verify-cert` succeeds only when the test marked the
# CA as trusted ($PS_TEST_SECURITY_TRUSTED).
printf '%s\n' "$*" >> "$PS_TEST_SECURITY_LOG"
[[ "${1:-}" == verify-cert && -n "${PS_TEST_SECURITY_TRUSTED:-}" ]]
"""

FAKE_IFCONFIG = r"""#!/usr/bin/env bash
# Fake `ifconfig`: prints the text the test wrote to $PS_TEST_IFCONFIG_OUTPUT.
cat "$PS_TEST_IFCONFIG_OUTPUT"
"""

DEFAULT_HOST_ADDRESSES = ("127.0.0.1", "::1", "192.168.1.5", "fe80::1")


def _run_on_tty(
    argv: list[str], cwd: Path, env: dict[str, str], tty_input: str
) -> subprocess.CompletedProcess[str]:
    """Run `argv` with a terminal as stdin, typing `tty_input` (an interactive run)."""
    master, slave = pty.openpty()
    try:
        os.write(master, tty_input.encode())
        completed = subprocess.run(  # noqa: S603 - fixture-owned script copy, hermetic PATH
            argv,
            cwd=cwd,
            env=env,
            stdin=slave,
            capture_output=True,
            text=True,
            check=False,
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
    finally:
        os.close(master)
        os.close(slave)
    return completed


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

    def remove_tool(self, name: str) -> None:
        (self.bin_dir / name).unlink()

    @property
    def argv_log(self) -> Path:
        return self.root / "argv.log"

    def log_every_tool_argv(self) -> None:
        """Put the argv-logging shim in front of every tool on the script's PATH (`bin/`).

        Each run then appends every external command line to `argv_log`, so a test can prove a
        secret reached no argv of any tool, the fakes included.
        """
        real_dir = self.root / "real-bin"
        real_dir.mkdir(exist_ok=True)
        shim = (FAKES_DIR / "argv_shim").read_text(encoding="utf-8")
        for tool in sorted(self.bin_dir.iterdir()):
            if tool.is_symlink():
                real = tool.resolve()
                tool.unlink()
            else:
                real = real_dir / tool.name
                tool.rename(real)
            _install_text_fake(shim.replace("@REAL@", shlex.quote(str(real))), tool)
        self.env_extra["PS_TEST_ARGV_LOG"] = str(self.argv_log)

    def logged_argv(self) -> str:
        return self.argv_log.read_text(encoding="utf-8") if self.argv_log.exists() else ""

    def add_container_engine(self, name: str) -> None:
        _install_python_fake(FAKES_DIR / "podman", self.bin_dir / name)

    def set_addresses(self, *addresses: str, tool: str = "ip") -> None:
        """Make the fake address tool (`ip` or `ifconfig`) report exactly these addresses."""
        for name in ("ip", "ifconfig"):
            (self.bin_dir / name).unlink(missing_ok=True)
        if tool == "ip":
            lines = [
                f"2: eth0    {'inet6' if ':' in a else 'inet'} {a}/64 scope global"
                for a in addresses
            ]
            (self.root / "ip-output.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            _install_text_fake(FAKE_IP, self.bin_dir / "ip")
        else:
            lines = ["en0: flags=8863<UP> mtu 1500"]
            for a in addresses:
                family, detail = (
                    ("inet6", "prefixlen 64") if ":" in a else ("inet", "netmask 0xffffff00")
                )
                lines.append(f"\t{family} {a} {detail}")
            (self.root / "ifconfig-output.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            _install_text_fake(FAKE_IFCONFIG, self.bin_dir / "ifconfig")

    def set_default_route(self, address: str, *, tool: str = "ip") -> None:
        """Make the default route leave through the interface that holds `address`."""
        if tool == "ip":
            route = f"1.1.1.1 via 192.168.1.1 dev eth0 src {address} uid 1000\n"
            (self.root / "ip-route-output.txt").write_text(route, encoding="utf-8")
            return
        (self.root / "route-output.txt").write_text(
            "   route to: default\ndestination: default\n  gateway: 192.168.1.1\n"
            "  interface: en0\n",
            encoding="utf-8",
        )
        (self.root / "ipconfig-address.txt").write_text(f"{address}\n", encoding="utf-8")
        _install_text_fake(FAKE_ROUTE, self.bin_dir / "route")
        _install_text_fake(FAKE_IPCONFIG, self.bin_dir / "ipconfig")

    @property
    def debian_anchor(self) -> Path:
        return self.root / "trust" / "ps-local-ca.crt"

    @property
    def fedora_anchor(self) -> Path:
        return self.root / "trust" / "ps-local-ca.pem"

    def set_platform(self, name: str) -> None:
        """Make `uname -s` report `name` (`Darwin` or `Linux`)."""
        (self.bin_dir / "uname").unlink(missing_ok=True)
        _install_text_fake(FAKE_UNAME, self.bin_dir / "uname")
        self.env_extra["PS_TEST_PLATFORM"] = name

    def add_security(self, *, trusted: bool) -> Path:
        """Provide a macOS `security`; returns the log of its arguments."""
        _install_text_fake(FAKE_SECURITY, self.bin_dir / "security")
        if trusted:
            self.env_extra["PS_TEST_SECURITY_TRUSTED"] = "1"
        return self.root / "security.log"

    def use_real_openssl(self) -> None:
        """Run the real `openssl` behind the recording fake (the fixture default)."""
        real = shutil.which("openssl")
        assert real, "the script tests need a real openssl"
        self.env_extra["PS_TEST_REAL_OPENSSL"] = real

    def use_stand_in_openssl(self) -> None:
        """Replace certificate generation with tiny stand-in files (invocation-level tests)."""
        self.env_extra.pop("PS_TEST_REAL_OPENSSL", None)

    def curl_calls(self) -> list[dict[str, Any]]:
        if not self.curl_log.exists():
            return []
        return [json.loads(line) for line in self.curl_log.read_text().splitlines()]

    def curl_urls(self) -> list[str]:
        return [call["url"] for call in self.curl_calls()]

    def curl_users(self) -> dict[str, Any]:
        users: dict[str, Any] = json.loads(self.curl_state.read_text()).get("users", {})
        return users

    def curl_devices(self) -> dict[str, Any]:
        devices: dict[str, Any] = json.loads(self.curl_state.read_text()).get("devices", {})
        return devices

    def kubectl_calls(self) -> list[str]:
        return self.kubectl_log.read_text().splitlines() if self.kubectl_log.exists() else []

    def helm_calls(self) -> list[str]:
        return self.helm_log.read_text().splitlines() if self.helm_log.exists() else []

    def helm_upgrades(self) -> list[str]:
        return [c for c in self.helm_calls() if c.startswith("upgrade")]

    def seed_release(self, values: dict[str, Any]) -> None:
        """Make the fake helm report an installed release whose `get values` is `values`."""
        self.helm_state.mkdir(parents=True, exist_ok=True)
        (self.helm_state / "values.json").write_text(json.dumps(values), encoding="utf-8")

    @property
    def signing_state_path(self) -> Path:
        return self.root / "signing-state.json"

    def seed_signing(self, **state: Any) -> None:  # noqa: ANN401 - JSON-shaped state values
        """Merge `state` into the state of the fake signing revoke command (`ps_signing_revoke`)."""
        current: dict[str, Any] = {}
        if self.signing_state_path.exists():
            current = json.loads(self.signing_state_path.read_text())
        current.update(state)
        self.signing_state_path.write_text(json.dumps(current))

    def signing_state(self) -> dict[str, Any]:
        if not self.signing_state_path.exists():
            return {}
        state: dict[str, Any] = json.loads(self.signing_state_path.read_text())
        return state

    def deployed_values(self) -> dict[str, Any]:
        values: dict[str, Any] = json.loads((self.helm_state / "values.json").read_text())
        return values

    def everything_observable(self, run: ScriptRun) -> str:
        return run.output + json.dumps(self.curl_calls()) + "\n".join(self.kubectl_calls())

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        tty_input: str | None = None,
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
            "PS_TEST_IP_OUTPUT": str(self.root / "ip-output.txt"),
            "PS_TEST_IFCONFIG_OUTPUT": str(self.root / "ifconfig-output.txt"),
            "PS_TEST_IP_ROUTE_OUTPUT": str(self.root / "ip-route-output.txt"),
            "PS_TEST_ROUTE_OUTPUT": str(self.root / "route-output.txt"),
            "PS_TEST_IPCONFIG_ADDRESS": str(self.root / "ipconfig-address.txt"),
            "PS_TEST_SECURITY_LOG": str(self.root / "security.log"),
            "PS_DEBIAN_CA_ANCHOR": str(self.debian_anchor),
            "PS_FEDORA_CA_ANCHOR": str(self.fedora_anchor),
            "PS_TEST_OPENSSL_LOG": str(self.root / "openssl.log"),
            "PS_TEST_SECRET_B64": base64.b64encode(TOKEN.encode()).decode(),
            "PS_TEST_SIGNING_FAKE": str(self.bin_dir / "ps-signing-revoke"),
            "PS_TEST_SIGNING_STATE": str(self.signing_state_path),
            "PS_INGRESS_PROBE_ATTEMPTS": "1",
            "PS_TLS_REFRESH_ATTEMPTS": "5",
            "PS_TLS_REFRESH_INTERVAL_SECONDS": "0.05",
            "PS_PORT_FORWARD_INTERVAL_SECONDS": "0.05",
            **self.env_extra,
            **env,
        }
        bash = shutil.which("bash") or "/bin/bash"
        if tty_input is None:
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
        else:
            completed = _run_on_tty([bash, str(script), *args], self.root, full_env, tty_input)
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
    _install_python_fake(FAKES_DIR / "ps_signing_revoke", fixture.bin_dir / "ps-signing-revoke")
    _install_text_fake(FAKE_KUBECTL, fixture.bin_dir / "kubectl")
    _install_text_fake(FAKE_HELM, fixture.bin_dir / "helm")
    _install_text_fake(FAKE_OPENSSL, fixture.bin_dir / "openssl")
    fixture.add_container_engine("podman")
    fixture.set_addresses(*DEFAULT_HOST_ADDRESSES)
    fixture.seed_curl()
    fixture.use_real_openssl()
    fixture.env_extra["PS_TEST_PODMAN_NODE_IP"] = NODE_IP
    return fixture
