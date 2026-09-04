"""ps-cli configuration: `CliConfig` and `load_config()`.

Resolution precedence (highest wins): the `PS_CLI_SERVICE_URL` environment
variable; the `context` param (`--context` flag), validated against
`targets.toml`'s `[contexts]`; `targets.toml`'s `current_context` pointer,
if set; otherwise a project-root override file (`./ps-cli.toml`, resolved
relative to the current working directory) deep-merged over the packaged
default config file shipped inside `ps_cli` (`default_config.toml`). See
PLAN.md (issue #56) §1 D3 for the full rationale, including the "fail
closed on an explicit but invalid value" convention this mirrors from
`ps_service.config`.
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
from ps_cli.targets import load_targets, resolve_config_dir

_OVERRIDE_FILE_NAME = "ps-cli.toml"
_ENV_VAR_NAME = "PS_CLI_SERVICE_URL"
_CURATED_REPO_ENV_VAR_NAME = "PS_CLI_CURATED_REPO_PATH"
_VALID_URL_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class CliConfig:
    """Fully-resolved ps-cli configuration."""

    service_url: str
    curated_repo_path: Path = Path("./curated-content")


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


def is_valid_service_url(url: str) -> bool:
    """Return whether `url` is an http(s) URL with a non-empty hostname.

    A pure, shared format check, extracted out of `_validate_service_url` so it
    can be reused elsewhere (e.g. the `config set-context --url` argparse
    `type=` callback) without duplicating the same `urlparse` logic. See
    PLAN.md D5.
    """
    parsed = urlparse(url)
    return parsed.scheme in _VALID_URL_SCHEMES and bool(parsed.hostname)


def _validate_service_url(url: str, *, source: str) -> None:
    """Raise `PsCliError` if `url` is not an http(s) URL with a non-empty hostname.

    `source` names which config layer (env var / override file / packaged
    default) produced `url`, so the raised error points an operator at the
    layer to fix rather than just the bad value. See PLAN.md D5.
    """
    assert_contract(
        contract=is_valid_service_url(url),
        msg=f"PS Service URL '{url}' is not a valid http(s) URL",
        hint=f"this value came from {source}",
    )


def _resolve_curated_repo_path(cwd: Path) -> Path:
    """Resolve `curated_repo_path`, independently of `service_url`'s resolution.

    Precedence (highest wins): the `PS_CLI_CURATED_REPO_PATH` environment
    variable; otherwise the project-root override file (`<cwd>/ps-cli.toml`)
    deep-merged over the packaged default — the same env-var/override-file/
    packaged-default chain `service_url` uses in `load_config`'s `else`
    branch, but resolved by its own standalone function so it applies
    unconditionally, regardless of which `service_url` case fires (env var /
    `--context` / `targets.current_context` / the override-file chain). See
    PLAN.md D3 / CHANGES.md MI2.
    """
    env_value = os.environ.get(_CURATED_REPO_ENV_VAR_NAME)
    if env_value is not None:
        assert_contract(
            contract=env_value != "",
            msg=(
                f"{_CURATED_REPO_ENV_VAR_NAME} is set but empty; "
                "unset it to use the default, or set a path"
            ),
        )
        return Path(env_value)
    override = _read_project_override(cwd)
    merged = _deep_merge(_read_packaged_default(), override)
    raw = merged.get("curated_repo_path")
    assert_contract(
        contract=isinstance(raw, str),
        msg=f"resolved config's 'curated_repo_path' is not a string, got {raw!r}",
    )
    return Path(cast("str", raw))


def load_config(
    *,
    cwd: Path | None = None,
    context: str | None = None,
    config_dir: Path | None = None,
) -> CliConfig:
    """Resolve ps-cli configuration.

    Precedence (highest wins): the `PS_CLI_SERVICE_URL` environment
    variable, then a project-root override file (`<cwd>/ps-cli.toml`) deep-
    merged over the packaged default. `cwd` defaults to the real current
    working directory; overriding it is the constructor-injection seam
    tests use to avoid touching the real filesystem's actual cwd.

    `context` (the `--context` CLI flag's resolved value for this
    invocation) and `config_dir` (defaulting to `resolve_config_dir()`) are
    the seam for `targets.toml`-driven named-context resolution. Resolution
    precedence, highest wins (PLAN.md D3):

    1. `PS_CLI_SERVICE_URL` env var (AC-BI-002).
    2. `context` param, if given — validated against `targets.contexts`
       *unconditionally* whenever given, even when the env var will end up
       winning (AC-BI-009; CHANGES.md F5 closes a gap where an invalid
       `--context` name would otherwise go silently unvalidated whenever
       the env var was also set).
    3. `targets.current_context`, if `targets.toml` exists and sets it
       (AC-BI-001); raises if it names a context missing from `[contexts]`
       (AC-BI-008).
    4. Otherwise, falls through unchanged to today's project-override-file/
       packaged-default chain (AC-BI-004) — no `targets.toml`, or one with
       no `current_context` set and no `--context` given.

    Fails closed (raises `PsCliError`) on an explicitly empty
    `PS_CLI_SERVICE_URL`, an invalid/missing context name, or a resolved
    URL that is not a valid http(s) URL, from whichever layer produced it —
    never silently substitutes a different value. See PLAN.md D3.
    """
    resolved_cwd = cwd if cwd is not None else Path.cwd()
    resolved_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    targets = load_targets(resolved_config_dir)
    contexts = targets.contexts if targets is not None else {}

    # Case 2's validation happens unconditionally, ahead of the env-var check
    # below, so an invalid `--context` name always errors — even when
    # PS_CLI_SERVICE_URL is also set and would otherwise supply the
    # resolved URL (CHANGES.md F5 / AC-BI-009's unconditional wording).
    if context is not None:
        assert_contract(
            contract=context in contexts,
            msg=f"context '{context}' is not defined in targets.toml",
            hint=(
                f"valid contexts: {', '.join(sorted(contexts))}"
                if contexts
                else "no contexts are defined in targets.toml"
            ),
        )

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
    elif context is not None:
        # Already validated as a member of `contexts` above.
        service_url = contexts[context]
        source = f"the '{context}' context (targets.toml, via --context)"
    elif targets is not None and targets.current_context is not None:
        current_context = targets.current_context
        assert_contract(
            contract=current_context in contexts,
            msg=(
                f"current_context '{current_context}' (targets.toml) is not defined in [contexts]"
            ),
            hint=(
                f"valid contexts: {', '.join(sorted(contexts))}"
                if contexts
                else "no contexts are defined in targets.toml"
            ),
        )
        service_url = contexts[current_context]
        source = f"the current context '{current_context}' (targets.toml)"
    else:
        override = _read_project_override(resolved_cwd)
        merged = _deep_merge(_read_packaged_default(), override)

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
    curated_repo_path = _resolve_curated_repo_path(resolved_cwd)
    return CliConfig(service_url=service_url, curated_repo_path=curated_repo_path)
