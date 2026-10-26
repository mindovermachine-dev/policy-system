"""Applies the chart's Authentik blueprint to the pinned Authentik image.

The structural tests in `test_authentik_blueprint.py` parse the YAML; only Authentik's own
importer can say whether it applies. `scripts/verify-authentik-blueprint.sh` runs that importer
in a disposable container, so every test here needs a container runtime and a public image pull
(about 1.4 GB the first time) and carries the `authentik_blueprint` marker.

The module starts one Postgres and one Authentik container (`shared_environment`) and removes
both when its last test ends, whether or not a test failed. Each test applies its blueprint to a
database cloned from a migrated template, so no test sees another's rows and the verdicts do not
depend on the order the tests run in.

With no container CLI the tests skip, except under `CI=true`, where a runner without one must
fail rather than pass unverified.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import IO

pytestmark = pytest.mark.authentik_blueprint

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-authentik-blueprint.sh"
CHART_BLUEPRINT = REPO_ROOT / "charts" / "policy-system" / "files" / "authentik-blueprint.yaml"

APPLY_TIMEOUT_SECONDS = 1500.0
SUBPROCESS_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 120.0
TEMPLATE_SESSIONS_QUERY = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = 'authentik_template'"
)

# The last revision before the passkey login flow was rewritten: an install upgraded from it
# holds the earlier blueprint's rows when the current one is applied.
PREVIOUS_REVISION = "e95dae75~1"

# A release whose enrolment flows end on deny confirmation stages: an install upgraded from it
# holds those stages and their bindings when the current blueprint is applied.
DENY_CONFIRMATION_RELEASE = "12.1.2"

# A row-deletion entry whose target does not exist: the importer builds an unsaved instance of a
# multi-table-inheritance model for it, and deleting that raises.
ABSENT_ENTRY_ON_MISSING_ROW = """\
version: 1
metadata:
  name: absent-entry-on-missing-row
entries:
  - model: authentik_flows.flowstagebinding
    state: absent
    identifiers:
      target: !Find [authentik_flows.flow, [slug, default-authentication-flow]]
      stage: !Find
        - authentik_stages_identification.identificationstage
        - [name, default-authentication-identification]
      order: 987
"""

# An expression policy whose body reads a name the policy evaluator does not provide.
EXPRESSION_POLICY_READING_AN_UNDEFINED_NAME = """\
version: 1
metadata:
  name: expression-policy-reading-an-undefined-name
entries:
  - model: authentik_policies_expression.expressionpolicy
    identifiers:
      name: reads-an-undefined-name
    attrs:
      expression: |
        return flow_plan.context.get("auth_method") == "auth_webauthn_pwl"
"""


def _forward_stderr(stream: IO[str], sink: list[str]) -> None:
    """Copy each stderr line to this process's stderr as it arrives, and keep it for assertions."""
    for line in stream:
        sink.append(line)
        sys.stderr.write(line)
        sys.stderr.flush()


def _run_script(
    cli: str, *args: str, timeout: float = APPLY_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run the script, echoing its stderr live so its phase timing survives a killed run."""
    with subprocess.Popen(  # noqa: S603  # fixed argv: the script under test and literal options
        [str(SCRIPT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PS_CONTAINER_CLI": cli},
    ) as process:
        stdout, stderr = process.stdout, process.stderr
        assert stdout is not None
        assert stderr is not None
        output: list[str] = []
        error_lines: list[str] = []
        readers = [
            threading.Thread(target=lambda: output.append(stdout.read())),
            threading.Thread(target=_forward_stderr, args=(stderr, error_lines)),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        finally:
            for reader in readers:
                reader.join()
    return subprocess.CompletedProcess(
        process.args, returncode, "".join(output), "".join(error_lines)
    )


def _container_cli() -> str:
    cli = os.environ.get("PS_CONTAINER_CLI") or shutil.which("podman") or shutil.which("docker")
    if cli:
        return cli
    if os.environ.get("CI") == "true":
        pytest.fail("no container CLI on a CI runner; the blueprint apply guard cannot run")
    pytest.skip("no container CLI (podman or docker) available")


@dataclass(frozen=True)
class SharedEnvironment:
    """The Postgres and Authentik containers every test in the module applies its blueprint in."""

    cli: str
    network: str
    postgres: str
    authentik: str

    def verify(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run_script(self.cli, "--use-environment", self.authentik, *args)

    def container_names(self) -> list[str]:
        listed = subprocess.run(  # noqa: S603  # fixed argv: the container CLI and literal options
            [self.cli, "ps", "-a", "--filter", f"name={self.network}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        return sorted(listed.stdout.split())

    def template_sessions(self) -> int:
        counted = subprocess.run(  # noqa: S603  # fixed argv: the container CLI and a literal query
            [
                self.cli,
                "exec",
                self.postgres,
                "psql",
                "-U",
                "authentik",
                "-d",
                "postgres",
                "-At",
                "-c",
                TEMPLATE_SESSIONS_QUERY,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        return int(counted.stdout.strip())


def _start_environment(cli: str) -> SharedEnvironment:
    started = _run_script(cli, "--start-environment")
    assert started.returncode == 0, started.stdout[-2000:] + started.stderr[-4000:]
    names = dict(line.split("=", 1) for line in started.stdout.splitlines() if "=" in line)
    return SharedEnvironment(cli, names["NETWORK"], names["POSTGRES"], names["AUTHENTIK"])


def _stop_environment(environment: SharedEnvironment) -> None:
    stopped = _run_script(
        environment.cli, "--stop-environment", environment.network, timeout=STOP_TIMEOUT_SECONDS
    )
    remaining = environment.container_names()
    assert stopped.returncode == 0 and not remaining, (
        f"environment {environment.network} still has {remaining}: {stopped.stderr[-2000:]}"
    )


@pytest.fixture(scope="module")
def shared_environment() -> Iterator[SharedEnvironment]:
    environment = _start_environment(_container_cli())
    try:
        yield environment
    finally:
        _stop_environment(environment)


def test_chart_blueprint_applies_and_passes_every_guard_check(
    shared_environment: SharedEnvironment,
) -> None:
    completed = shared_environment.verify(str(CHART_BLUEPRINT))

    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    assert "PASS shipped expression policies read only names authentik provides" in completed.stdout
    assert "PASS expression policies read only names authentik provides" in completed.stdout
    assert "PASS ps-login chain, brand pointers and ps-cli application" in completed.stdout
    assert (
        "PASS ps-login validate stage is skipped only after a passkey identification"
        in completed.stdout
    )
    assert "PASS ps-passkey-recovery ends on the done-page redirect" in completed.stdout
    assert "PASS ps-invite-enrollment ends on the done-page redirect" in completed.stdout
    assert "PASS brand css hides flow cancel links" in completed.stdout
    assert shared_environment.template_sessions() == 0


def test_chart_blueprint_reapplies_with_unchanged_row_counts(
    shared_environment: SharedEnvironment,
) -> None:
    completed = shared_environment.verify("--twice", str(CHART_BLUEPRINT))

    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]


def test_chart_blueprint_applies_over_the_previous_revision(
    shared_environment: SharedEnvironment,
) -> None:
    revision_present = subprocess.run(  # noqa: S603  # fixed argv
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{PREVIOUS_REVISION}^{{commit}}"],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    if revision_present.returncode != 0:
        pytest.skip(f"git history does not hold {PREVIOUS_REVISION} (shallow clone)")
    completed = shared_environment.verify("--upgrade-from", PREVIOUS_REVISION, str(CHART_BLUEPRINT))

    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]


def test_chart_blueprint_applies_over_a_release_that_ends_on_the_deny_confirmation(
    shared_environment: SharedEnvironment,
) -> None:
    revision_present = subprocess.run(  # noqa: S603  # fixed argv
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{DENY_CONFIRMATION_RELEASE}^{{commit}}"],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    if revision_present.returncode != 0:
        pytest.skip(f"git history does not hold {DENY_CONFIRMATION_RELEASE} (shallow clone)")
    completed = shared_environment.verify(
        "--upgrade-from", DENY_CONFIRMATION_RELEASE, str(CHART_BLUEPRINT)
    )

    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    assert "PASS ps-passkey-recovery ends on the done-page redirect" in completed.stdout
    assert "PASS ps-invite-enrollment ends on the done-page redirect" in completed.stdout


def test_guard_fails_on_a_blueprint_the_importer_cannot_apply(
    shared_environment: SharedEnvironment, tmp_path: Path
) -> None:
    repro = tmp_path / "repro.yaml"
    repro.write_text(ABSENT_ENTRY_ON_MISSING_ROW, encoding="utf-8")

    completed = shared_environment.verify(str(repro))

    assert completed.returncode != 0
    assert "policybindingmodel_ptr" in completed.stdout + completed.stderr


def test_guard_fails_on_an_expression_policy_that_reads_an_undefined_name(
    shared_environment: SharedEnvironment, tmp_path: Path
) -> None:
    blueprint = tmp_path / "undefined-name.yaml"
    blueprint.write_text(EXPRESSION_POLICY_READING_AN_UNDEFINED_NAME, encoding="utf-8")

    completed = shared_environment.verify(str(blueprint))

    output_tail = completed.stdout[-4000:] + completed.stderr[-2000:]
    fail_lines = [line for line in completed.stdout.splitlines() if line.startswith("FAIL ")]
    naming_lines = [line for line in fail_lines if "reads-an-undefined-name" in line]
    assert completed.returncode == 1, output_tail
    assert any("flow_plan" in line for line in naming_lines), output_tail
    assert "PASS shipped expression policies read only names authentik provides" in completed.stdout


def test_module_environment_runs_one_postgres_and_one_authentik_container(
    shared_environment: SharedEnvironment,
) -> None:
    assert shared_environment.container_names() == sorted(
        [shared_environment.postgres, shared_environment.authentik]
    )
