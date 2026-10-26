"""Tests for `scripts/verify-authentik-blueprint.sh` that need no container runtime.

The live apply is in `test_authentik_blueprint_apply_live.py`. Here the script's argument
handling, its image-tag derivation and its teardown wiring are checked by running it for real
up to the point where it would start a container. The environment phases (start, use, stop) are
run against a recording stand-in for the container CLI, which is the process boundary the script
drives.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from guardrails.chart_render import CHART_DIR, ChartGuardSetupError, require_subchart_archives

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify-authentik-blueprint.sh"
IMAGE_REPOSITORY = "ghcr.io/goauthentik/server"

SUBPROCESS_TIMEOUT_SECONDS = 30.0
USAGE_EXIT_STATUS = 2
GUARD_FAILURE_STATUS = 1

# Records every invocation, one line each; the exit status of the guard run, the migration, the
# shipped-blueprint apply and the removals is chosen per test through the environment.
FAKE_CONTAINER_CLI = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CLI_LOG"
case "$*" in
  *"GUARD_MODE=shipped"*) exit "${FAKE_SHIPPED_STATUS:-0}" ;;
  *"manage shell"*) exit "${FAKE_GUARD_STATUS:-0}" ;;
  *"lifecycle.migrate"*) exit "${FAKE_MIGRATE_STATUS:-0}" ;;
  "rm -f "* | "network rm "*) exit "${FAKE_REMOVE_STATUS:-0}" ;;
esac
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # fixed argv: the script under test and literal options
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


class _FakeCli:
    """A container CLI stand-in that logs its argv lines and exits as the test chooses."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "fake-container-cli"
        self.path.write_text(FAKE_CONTAINER_CLI, encoding="utf-8")
        self.path.chmod(0o755)
        self.log = directory / "fake-cli.log"
        self.log.touch()

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603  # fixed argv: the script under test and literal options
            [str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            env={
                **os.environ,
                "PS_CONTAINER_CLI": str(self.path),
                "FAKE_CLI_LOG": str(self.log),
                **env,
            },
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def calls_starting_with(self, prefix: str) -> list[str]:
        return [call for call in self.calls() if call.startswith(prefix)]


def _environment_names(stdout: str) -> dict[str, str]:
    return dict(re.findall(r"^(NETWORK|POSTGRES|AUTHENTIK)=(\S+)$", stdout, flags=re.MULTILINE))


@pytest.fixture
def fake_cli(tmp_path: Path) -> _FakeCli:
    return _FakeCli(tmp_path)


def _authentik_dependency_version() -> str:
    chart = cast("dict[str, Any]", yaml.safe_load((CHART_DIR / "Chart.yaml").read_text("utf-8")))
    dependencies = cast("list[dict[str, str]]", chart["dependencies"])
    return next(d["version"] for d in dependencies if d["name"] == "authentik")


def test_image_tag_is_the_authentik_dependency_version_of_the_chart() -> None:
    completed = _run("--print-image")

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"{IMAGE_REPOSITORY}:{_authentik_dependency_version()}"


def _packaged_authentik_chart(chart_dir: Path) -> dict[str, Any]:
    """Return the `Chart.yaml` packaged in the authentik archive; fail when it is not packaged."""
    archive = require_subchart_archives(chart_dir)["authentik"]
    with tarfile.open(archive) as tar:
        member = tar.extractfile("authentik/Chart.yaml")
        assert member is not None
        return cast("dict[str, Any]", yaml.safe_load(member.read()))


def test_dependency_version_is_the_app_version_of_the_packaged_authentik_subchart() -> None:
    assert _packaged_authentik_chart(CHART_DIR)["appVersion"] == _authentik_dependency_version()


def test_an_unknown_option_is_refused() -> None:
    completed = _run("--no-such-option")

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "unknown option: --no-such-option" in completed.stderr


def test_upgrade_from_without_a_revision_is_refused() -> None:
    completed = _run("--upgrade-from")

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "--upgrade-from needs a git revision" in completed.stderr


def test_a_missing_blueprint_file_is_refused_before_any_container_starts(tmp_path: Path) -> None:
    completed = _run(str(tmp_path / "absent.yaml"))

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "blueprint not found" in completed.stderr


def test_a_second_blueprint_argument_is_refused(tmp_path: Path) -> None:
    first = tmp_path / "a.yaml"
    first.write_text("version: 1\n", encoding="utf-8")

    completed = _run(str(first), str(first))

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "only one BLUEPRINT" in completed.stderr


def test_a_one_shot_run_starts_one_postgres_and_one_authentik_container(fake_cli: _FakeCli) -> None:
    completed = fake_cli.run()

    assert completed.returncode == 0, completed.stderr
    started = fake_cli.calls_starting_with("run ")
    assert len(started) == 2
    assert all(" -d " in call and "--rm" not in call for call in started)
    assert sum("postgres:" in call for call in started) == 1
    assert sum(IMAGE_REPOSITORY in call for call in started) == 1


def test_a_one_shot_run_removes_every_container_and_the_network_when_the_guard_fails(
    fake_cli: _FakeCli,
) -> None:
    completed = fake_cli.run(FAKE_GUARD_STATUS=str(GUARD_FAILURE_STATUS))

    assert completed.returncode == GUARD_FAILURE_STATUS
    started = [call.split("--name ")[1].split()[0] for call in fake_cli.calls_starting_with("run ")]
    network = fake_cli.calls_starting_with("network create ")[0].split()[-1]
    removed = [call.split()[-1] for call in fake_cli.calls_starting_with("rm -f ")]
    assert sorted(removed) == sorted(started)
    assert fake_cli.calls_starting_with("network rm ") == [f"network rm {network}"]


def test_a_failed_migration_removes_the_environment_it_started(fake_cli: _FakeCli) -> None:
    completed = fake_cli.run("--start-environment", FAKE_MIGRATE_STATUS="1")

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "database migration failed" in completed.stderr
    assert len(fake_cli.calls_starting_with("rm -f ")) == 2
    assert len(fake_cli.calls_starting_with("network rm ")) == 1


def test_start_environment_prints_the_names_that_stop_environment_removes(
    fake_cli: _FakeCli,
) -> None:
    started = fake_cli.run("--start-environment")
    names = _environment_names(started.stdout)

    assert started.returncode == 0, started.stderr
    assert set(names) == {"NETWORK", "POSTGRES", "AUTHENTIK"}
    assert fake_cli.calls_starting_with("rm -f ") == []
    assert fake_cli.calls_starting_with("network rm ") == []

    stopped = fake_cli.run("--stop-environment", names["NETWORK"])

    assert stopped.returncode == 0, stopped.stderr
    assert sorted(call.split()[-1] for call in fake_cli.calls_starting_with("rm -f ")) == sorted(
        [names["POSTGRES"], names["AUTHENTIK"]]
    )
    assert fake_cli.calls_starting_with("network rm ") == [f"network rm {names['NETWORK']}"]


def test_the_environment_migrates_once_before_any_database_is_cloned(fake_cli: _FakeCli) -> None:
    names = _environment_names(fake_cli.run("--start-environment").stdout)
    fake_cli.run("--use-environment", names["AUTHENTIK"])
    fake_cli.run("--use-environment", names["AUTHENTIK"])

    calls = fake_cli.calls()
    migrations = [i for i, call in enumerate(calls) if "lifecycle.migrate" in call]
    clones = [i for i, call in enumerate(calls) if "CREATE DATABASE" in call]
    assert len(migrations) == 1
    assert len(clones) == 2
    assert migrations[0] < clones[0]


def test_the_shipped_blueprints_are_applied_once_to_the_template_and_not_again_per_use(
    fake_cli: _FakeCli,
) -> None:
    names = _environment_names(fake_cli.run("--start-environment").stdout)
    fake_cli.run("--use-environment", names["AUTHENTIK"])
    fake_cli.run("--use-environment", names["AUTHENTIK"])

    calls = fake_cli.calls()
    shipped = [i for i, call in enumerate(calls) if "GUARD_MODE=shipped" in call]
    migrations = [i for i, call in enumerate(calls) if "lifecycle.migrate" in call]
    clones = [i for i, call in enumerate(calls) if "CREATE DATABASE" in call]
    assert len(shipped) == 1
    assert "AUTHENTIK_POSTGRESQL__NAME=authentik_template" in calls[shipped[0]]
    assert migrations[0] < shipped[0] < clones[0]
    assert sum("GUARD_MODE=verify" in call for call in calls) == 2


def test_a_failed_shipped_blueprint_apply_removes_the_environment_it_started(
    fake_cli: _FakeCli,
) -> None:
    completed = fake_cli.run("--start-environment", FAKE_SHIPPED_STATUS="1")

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "applying the shipped blueprints failed" in completed.stderr
    assert len(fake_cli.calls_starting_with("rm -f ")) == 2
    assert len(fake_cli.calls_starting_with("network rm ")) == 1


def test_the_image_is_pulled_in_the_background_while_postgres_starts(fake_cli: _FakeCli) -> None:
    completed = fake_cli.run("--start-environment")

    assert completed.returncode == 0, completed.stderr
    pulls = [call for call in fake_cli.calls() if call.startswith("pull ")]
    assert len(pulls) == 1
    assert pulls[0].endswith(IMAGE_REPOSITORY + ":" + _authentik_dependency_version())
    pull_index = fake_cli.calls().index(pulls[0])
    authentik_run = next(
        i
        for i, call in enumerate(fake_cli.calls())
        if call.startswith("run ") and IMAGE_REPOSITORY in call
    )
    assert pull_index < authentik_run


def test_every_environment_phase_logs_its_elapsed_seconds(fake_cli: _FakeCli) -> None:
    started = fake_cli.run("--start-environment")
    names = _environment_names(started.stdout)
    used = fake_cli.run("--use-environment", names["AUTHENTIK"])

    start_phases = set(re.findall(r"phase ([\w() -]+): \d+s", started.stderr))
    use_phases = set(re.findall(r"phase ([\w() -]+): \d+s", used.stderr))
    assert {"postgres ready", "migrate", "shipped blueprints", "environment total"} <= start_phases
    assert {"clone and stage", "guard"} <= use_phases


def test_use_environment_clones_the_template_database_and_drops_the_clone_afterwards(
    fake_cli: _FakeCli,
) -> None:
    names = _environment_names(fake_cli.run("--start-environment").stdout)

    used = fake_cli.run("--use-environment", names["AUTHENTIK"])

    assert used.returncode == 0, used.stderr
    (create,) = [call for call in fake_cli.calls() if "CREATE DATABASE" in call]
    clone = re.search(r"CREATE DATABASE (\w+) TEMPLATE authentik_template", create)
    assert clone is not None
    guard_run = next(call for call in fake_cli.calls() if "GUARD_MODE=verify" in call)
    assert f"AUTHENTIK_POSTGRESQL__NAME={clone.group(1)}" in guard_run
    assert any(f"DROP DATABASE IF EXISTS {clone.group(1)}" in call for call in fake_cli.calls())
    assert fake_cli.calls_starting_with("rm -f ") == []


def test_use_environment_drops_the_clone_and_keeps_the_environment_when_the_guard_fails(
    fake_cli: _FakeCli,
) -> None:
    names = _environment_names(fake_cli.run("--start-environment").stdout)

    used = fake_cli.run(
        "--use-environment", names["AUTHENTIK"], FAKE_GUARD_STATUS=str(GUARD_FAILURE_STATUS)
    )

    assert used.returncode == GUARD_FAILURE_STATUS
    assert any("DROP DATABASE IF EXISTS" in call for call in fake_cli.calls())
    assert fake_cli.calls_starting_with("rm -f ") == []
    assert fake_cli.calls_starting_with("network rm ") == []


def test_the_live_module_fixture_removes_its_containers_when_a_test_body_fails(
    fake_cli: _FakeCli, tmp_path: Path
) -> None:
    """Runs the live module's own fixture teardown, with a failing test body, on the fake CLI."""
    throwaway = tmp_path / "throwaway"
    throwaway.mkdir()
    (throwaway / "test_throwaway.py").write_text(
        textwrap.dedent(
            """\
            from test_authentik_blueprint_apply_live import shared_environment  # noqa: F401


            def test_fails_after_the_environment_is_up(shared_environment):
                assert False, "this test fails on purpose"
            """
        ),
        encoding="utf-8",
    )
    (throwaway / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n", "utf-8")

    nested = subprocess.run(  # fixed argv: this interpreter running pytest
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-n0", "test_throwaway.py"],
        cwd=throwaway,
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parent),
            "PS_CONTAINER_CLI": str(fake_cli.path),
            "FAKE_CLI_LOG": str(fake_cli.log),
        },
    )

    assert nested.returncode == 1, nested.stdout[-3000:] + nested.stderr[-2000:]
    assert "1 failed" in nested.stdout
    started = [call.split("--name ")[1].split()[0] for call in fake_cli.calls_starting_with("run ")]
    network = fake_cli.calls_starting_with("network create ")[0].split()[-1]
    removed = [call.split()[-1] for call in fake_cli.calls_starting_with("rm -f ")]
    assert len(started) == 2
    assert sorted(removed) == sorted(started)
    assert fake_cli.calls_starting_with("network rm ") == [f"network rm {network}"]


def test_stop_environment_succeeds_when_the_environment_is_already_gone(fake_cli: _FakeCli) -> None:
    first = fake_cli.run("--stop-environment", "ps-ak-gone", FAKE_REMOVE_STATUS="1")
    second = fake_cli.run("--stop-environment", "ps-ak-gone", FAKE_REMOVE_STATUS="1")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr


def test_two_environment_phases_in_one_call_are_refused() -> None:
    completed = _run("--start-environment", "--stop-environment", "ps-ak-1")

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "only one of --start-environment" in completed.stderr


def test_use_environment_without_a_container_name_is_refused() -> None:
    completed = _run("--use-environment")

    assert completed.returncode == USAGE_EXIT_STATUS
    assert "--use-environment needs the Authentik container name" in completed.stderr


def test_a_missing_authentik_archive_fails_with_the_build_hint(tmp_path: Path) -> None:
    for name in ("Chart.yaml", "Chart.lock"):
        shutil.copy(CHART_DIR / name, tmp_path / name)

    with pytest.raises(ChartGuardSetupError, match="helm dependency build charts/policy-system"):
        _packaged_authentik_chart(tmp_path)
