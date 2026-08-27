from pathlib import Path

import pytest

from ps_service.logging import LoggingConfigurationError
from ps_service.logging.facade import resolve_default_log_path


def test_resolve_default_log_path_when_no_env_override_then_under_repo_root_logs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PS_LOGGING_DIR", raising=False)
    fake_repo_root = tmp_path / "repo"
    (fake_repo_root / ".git").mkdir(parents=True)

    resolved = resolve_default_log_path(repo_root=fake_repo_root)

    assert resolved == fake_repo_root / "logs" / "ps-service.jsonl"
    assert resolved.parent.is_dir()  # M12: directory actually created, not just computed


def test_resolve_default_log_path_when_env_override_set_then_uses_override_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override_dir = tmp_path / "custom-logs"
    monkeypatch.setenv("PS_LOGGING_DIR", str(override_dir))

    resolved = resolve_default_log_path(repo_root=tmp_path)

    assert resolved == (override_dir / "ps-service.jsonl").resolve()
    assert override_dir.is_dir()


def test_resolve_default_log_path_when_directory_cannot_be_created_then_raises_logging_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("x")
    monkeypatch.setenv("PS_LOGGING_DIR", str(blocking_file / "logs"))  # parent is a file -> mkdir fails

    with pytest.raises(LoggingConfigurationError):
        resolve_default_log_path(repo_root=tmp_path)
