"""Tests for the loopback `kubectl port-forward` helper in `scripts/lib/authentik-owner.sh`
(issue #165, Slices 3D/3E): both deploy scripts reach Authentik's admin API through it when the
external address is not usable (the evaluator before its certificate is served, production
where the admin API is deliberately not on the public Ingress).

The real library is sourced by a real bash; `kubectl` is a recording fake whose `port-forward`
prints kubectl's own `Forwarding from ...` line and then blocks until it is killed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess

    from conftest import LibHarness

PRELUDE = """
set -uo pipefail
EXIT_FAILURE=1
print_error() { local f="$1"; shift; printf "$f" "$@" >&2; }
source "$PS_TEST_LIB_DIR/authentik-owner.sh"
"""


def _run(harness: LibHarness, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    return harness.run(PRELUDE + body, **env)


def test_start_reports_the_local_port_kubectl_chose_and_targets_the_service(
    lib_harness: LibHarness,
) -> None:
    proc = _run(
        lib_harness,
        "start_authentik_port_forward default policy-system-authentik-server 80 || exit 1\n"
        'printf "port=%s\\n" "$AUTHENTIK_PF_LOCAL_PORT"; stop_authentik_port_forward',
        PS_TEST_PF_PORT="41234",
    )

    assert proc.returncode == 0, proc.stderr
    assert "port=41234" in proc.stdout
    forwards = [c for c in lib_harness.kubectl_calls() if "port-forward" in c]
    assert forwards == [
        "-n default port-forward svc/policy-system-authentik-server :80 --address 127.0.0.1"
    ]


def test_stop_terminates_the_background_process(lib_harness: LibHarness) -> None:
    proc = _run(
        lib_harness,
        "start_authentik_port_forward default svc 80 || exit 1\n"
        'pid="$AUTHENTIK_PF_PID"; stop_authentik_port_forward\n'
        'if kill -0 "$pid" 2>/dev/null; then echo alive; else echo gone; fi\n'
        'printf "pid_var=[%s]\\n" "$AUTHENTIK_PF_PID"',
    )

    assert "gone" in proc.stdout
    assert "pid_var=[]" in proc.stdout


def test_a_failing_port_forward_exits_non_zero_with_an_actionable_message(
    lib_harness: LibHarness,
) -> None:
    proc = _run(
        lib_harness,
        "start_authentik_port_forward default policy-system-authentik-server 80",
        PS_TEST_PF_FAIL="1",
    )

    assert proc.returncode != 0
    assert "port-forward" in proc.stderr
    assert "kubectl get pods" in proc.stderr


def test_a_port_forward_that_never_reports_a_port_times_out_and_is_cleaned_up(
    lib_harness: LibHarness,
) -> None:
    proc = _run(
        lib_harness,
        "start_authentik_port_forward default svc 80; rc=$?\n"
        'if [[ -n "$AUTHENTIK_PF_PID" ]]; then echo leaked; else echo clean; fi; exit $rc',
        PS_TEST_PF_SILENT="1",
        PS_PORT_FORWARD_ATTEMPTS="3",
        PS_PORT_FORWARD_INTERVAL_SECONDS="0.05",
    )

    assert proc.returncode != 0
    assert "clean" in proc.stdout
    assert "port-forward" in proc.stderr


def test_current_kube_namespace_defaults_to_default(lib_harness: LibHarness) -> None:
    proc = _run(lib_harness, "current_kube_namespace")

    assert proc.stdout == "default"


def test_current_kube_namespace_uses_the_contexts_namespace(lib_harness: LibHarness) -> None:
    proc = _run(lib_harness, "current_kube_namespace", PS_TEST_KUBE_NAMESPACE="policy")

    assert proc.stdout == "policy"
