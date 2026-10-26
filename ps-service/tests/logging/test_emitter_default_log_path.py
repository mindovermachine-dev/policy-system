import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ps_service import logging as ps_logging
from ps_service.logging import LoggingConfigurationError
from ps_service.logging.facade import resolve_default_log_path

# Resolves the default path with no `repo_root`, the way `configure()` does in the image, and
# prints where the package was imported from so the test can prove it ran the copy.
_RESOLVE_SNIPPET = """
import json
from ps_service.logging import LoggingConfigurationError
from ps_service.logging import facade
try:
    result = {"path": str(facade.resolve_default_log_path())}
except LoggingConfigurationError as exc:
    result = {"error": type(exc).__name__}
print(json.dumps({"module": facade.__file__, **result}))
"""


def test_resolve_default_log_path_when_no_env_override_then_under_repo_root_logs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PS_LOGGING_DIR", raising=False)
    fake_repo_root = tmp_path / "repo"
    (fake_repo_root / ".git").mkdir(parents=True)

    resolved = resolve_default_log_path(repo_root=fake_repo_root)

    assert resolved == fake_repo_root / "logs" / "ps-service.jsonl"
    assert resolved.parent.is_dir()  # directory actually created, not just computed


def test_resolve_default_log_path_when_env_override_set_then_uses_override_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override_dir = tmp_path / "custom-logs"
    monkeypatch.setenv("PS_LOGGING_DIR", str(override_dir))

    resolved = resolve_default_log_path(repo_root=tmp_path)

    assert resolved == (override_dir / "ps-service.jsonl").resolve()
    assert override_dir.is_dir()


def test_resolve_default_log_path_raises_when_directory_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("x")
    monkeypatch.setenv(
        "PS_LOGGING_DIR", str(blocking_file / "logs")
    )  # parent is a file -> mkdir fails

    with pytest.raises(LoggingConfigurationError):
        resolve_default_log_path(repo_root=tmp_path)


def _logging_package_outside_a_repo(tmp_path: Path) -> Path:
    """Copy `ps_service.logging` to a tree with no `.git` ancestor, as the image installs it."""
    source = Path(ps_logging.__file__).resolve().parent
    site = tmp_path / "site"
    shutil.copytree(
        source, site / "ps_service" / "logging", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(source.parent / "__init__.py", site / "ps_service" / "__init__.py")
    assert not any((candidate / ".git").is_dir() for candidate in (site, *site.parents))
    return site


def _resolve_outside_a_repo(tmp_path: Path, logging_dir: str | None) -> dict[str, str]:
    site = _logging_package_outside_a_repo(tmp_path)
    environ = {name: value for name, value in os.environ.items() if name != "PS_LOGGING_DIR"}
    environ["PYTHONPATH"] = str(site)
    if logging_dir is not None:
        environ["PS_LOGGING_DIR"] = logging_dir
    completed = subprocess.run(  # noqa: S603 -- fixed interpreter, literal snippet
        [sys.executable, "-c", _RESOLVE_SNIPPET],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result: dict[str, str] = json.loads(completed.stdout)
    assert Path(result["module"]).is_relative_to(site), result
    return result


def test_resolve_default_log_path_when_env_override_set_then_needs_no_repo_root(
    tmp_path: Path,
) -> None:
    logging_dir = tmp_path / "pod-logs"

    result = _resolve_outside_a_repo(tmp_path, str(logging_dir))

    assert result.get("path") == str((logging_dir / "ps-service.jsonl").resolve()), result
    assert logging_dir.is_dir()


@pytest.mark.parametrize("logging_dir", [None, ""], ids=["unset", "empty"])
def test_resolve_default_log_path_when_no_env_override_and_no_repo_root_then_raises(
    tmp_path: Path, logging_dir: str | None
) -> None:
    result = _resolve_outside_a_repo(tmp_path, logging_dir)

    assert result.get("error") == "LoggingConfigurationError", result
