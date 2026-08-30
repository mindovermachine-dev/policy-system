"""ps-cli configuration: `CliConfig` and `load_config()`.

Resolution precedence (highest wins): the `PS_CLI_SERVICE_URL` environment
variable, then a project-root override file (`./ps-cli.toml`, resolved
relative to the current working directory), then the packaged default
config file shipped inside `ps_cli` (`default_config.toml`). See PLAN.md
§1 D3 for the full rationale, including the "fail closed on an explicit
but invalid value" convention this mirrors from `ps_service.config`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from ps_cli.errors import assert_contract

_OVERRIDE_FILE_NAME = "ps-cli.toml"
_ENV_VAR_NAME = "PS_CLI_SERVICE_URL"
_VALID_URL_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class CliConfig:
    """Fully-resolved ps-cli configuration."""

    service_url: str


def _read_packaged_default() -> dict[str, object]:
    """Read and parse the packaged `default_config.toml` resource.

    Lets any underlying `importlib.resources`/`tomllib` exception (e.g. a
    `FileNotFoundError` from a corrupted package install) propagate
    unhandled rather than wrapping it in `PsCliError` — a missing packaged
    resource is a bug in the package build, not something an operator did
    (L1 "let bugs crash"). See PLAN.md Increment 4.
    """
    resource = resources.files("ps_cli").joinpath("default_config.toml")
    with resource.open("rb") as handle:
        return tomllib.load(handle)


def _read_project_override(cwd: Path) -> dict[str, object]:
    """Read `<cwd>/ps-cli.toml` if it exists; return `{}` if it does not.

    A missing override file is not an error — it just means the operator
    has not opted into project-local config, and resolution falls through
    to the packaged default. See PLAN.md Increment 5.
    """
    override_path = cwd / _OVERRIDE_FILE_NAME
    if not override_path.is_file():
        return {}
    return tomllib.loads(override_path.read_text(encoding="utf-8"))


def _deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Recursively merge `override` over `base`; `override` wins on any scalar conflict.

    Generic on purpose — not special-cased to the single `service_url` key
    today — so the pattern already matches the "deep-merge, override wins"
    config convention as the config surface grows nested tables later, per
    PLAN.md D3.
    """
    merged = dict(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(override_value, dict) and isinstance(base_value, dict):
            merged[key] = _deep_merge(
                cast("dict[str, object]", base_value),
                cast("dict[str, object]", override_value),
            )
        else:
            merged[key] = override_value
    return merged


def _validate_service_url(url: str, *, source: str) -> None:
    """Raise `PsCliError` if `url` is not an http(s) URL with a non-empty hostname.

    `source` names which config layer (env var / override file / packaged
    default) produced `url`, so the raised error points an operator at the
    layer to fix rather than just the bad value. See PLAN.md D5.
    """
    parsed = urlparse(url)
    assert_contract(
        contract=parsed.scheme in _VALID_URL_SCHEMES and bool(parsed.hostname),
        msg=f"PS Service URL '{url}' is not a valid http(s) URL",
        hint=f"this value came from {source}",
    )


def load_config(*, cwd: Path | None = None) -> CliConfig:
    """Resolve ps-cli configuration.

    Precedence (highest wins): the `PS_CLI_SERVICE_URL` environment
    variable, then a project-root override file (`<cwd>/ps-cli.toml`) deep-
    merged over the packaged default. `cwd` defaults to the real current
    working directory; overriding it is the constructor-injection seam
    tests use to avoid touching the real filesystem's actual cwd.

    Fails closed (raises `PsCliError`) on an explicitly empty
    `PS_CLI_SERVICE_URL`, or on a resolved URL that is not a valid http(s)
    URL, from whichever layer produced it — never silently substitutes a
    different value. See PLAN.md D3.
    """
    resolved_cwd = cwd if cwd is not None else Path.cwd()
    override = _read_project_override(resolved_cwd)
    merged = _deep_merge(_read_packaged_default(), override)

    env_value = os.environ.get(_ENV_VAR_NAME)
    if env_value is not None:
        assert_contract(
            contract=env_value != "",
            msg=(
                f"{_ENV_VAR_NAME} is set but empty; unset it to use the default, or set a valid URL"
            ),
        )
        service_url = env_value
        source = f"the {_ENV_VAR_NAME} environment variable"
    else:
        # `merged` already deep-merged the override over the default, so
        # this same lookup returns the right value either way — only the
        # `source` label (for the error message below) depends on which
        # layer actually supplied it.
        source = (
            f"the project override file ({_OVERRIDE_FILE_NAME})"
            if "service_url" in override
            else "the packaged default config"
        )
        service_url_raw = merged.get("service_url")
        assert_contract(
            contract=isinstance(service_url_raw, str),
            msg=f"resolved config's 'service_url' is not a string, got {service_url_raw!r}",
        )
        service_url = cast("str", service_url_raw)

    _validate_service_url(service_url, source=source)
    return CliConfig(service_url=service_url)
