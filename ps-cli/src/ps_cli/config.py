"""ps-cli configuration: `CliConfig` and `load_config()`.

Resolution precedence (highest wins): the `PS_CLI_SERVICE_URL` environment
variable; the `context` param (`--context` flag), validated against
`targets.toml`'s `[contexts]`; `targets.toml`'s `current_context` pointer,
if set; otherwise a project-root override file (`./ps-cli.toml`, resolved
relative to the current working directory) deep-merged over the packaged
default config file shipped inside `ps_cli` (`default_config.toml`). An explicit
but invalid value fails closed (raises `PsCliError`) rather than falling back,
mirroring the convention in `ps_service.config`.
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
_VALID_URL_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class CliConfig:
    """Fully-resolved ps-cli configuration.

    `service_url` is the validated http(s) PS Service base URL. `context_name` names
    the `targets.toml` context that supplied it, or is `None` when the URL came from
    the environment variable, the project override file or the packaged default.
    """

    service_url: str
    context_name: str | None = None


def _read_packaged_default() -> dict[str, object]:
    """Read and parse the packaged `default_config.toml` resource.

    Lets any underlying `importlib.resources`/`tomllib` exception (e.g. a
    `FileNotFoundError` from a corrupted package install) propagate
    unhandled rather than wrapping it in `PsCliError` — a missing packaged
    resource is a bug in the package build, not something an operator did
    (L1 "let bugs crash").
    """
    resource = resources.files("ps_cli").joinpath("default_config.toml")
    with resource.open("rb") as handle:
        return tomllib.load(handle)


def _read_project_override(cwd: Path) -> dict[str, object]:
    """Read `<cwd>/ps-cli.toml` if it exists; return `{}` if it does not.

    A missing override file is not an error — it just means the operator
    has not opted into project-local config, and resolution falls through
    to the packaged default.
    """
    override_path = cwd / _OVERRIDE_FILE_NAME
    if not override_path.is_file():
        return {}
    return tomllib.loads(override_path.read_text(encoding="utf-8"))


def _deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Recursively merge `override` over `base`; `override` wins on any scalar conflict.

    Generic on purpose — not special-cased to the single `service_url` key
    today — so the pattern already matches the "deep-merge, override wins"
    config convention as the config surface grows nested tables later.
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
    `type=` callback) without duplicating the same `urlparse` logic.

    Parameters:
        url: Candidate URL text; any string, including an empty or malformed one.

    Returns:
        `True` when the scheme is `http` or `https` and a hostname is present,
        otherwise `False`. Never raises and has no side effects.
    """
    parsed = urlparse(url)
    return parsed.scheme in _VALID_URL_SCHEMES and bool(parsed.hostname)


def _validate_service_url(url: str, *, source: str) -> None:
    """Raise `PsCliError` if `url` is not an http(s) URL with a non-empty hostname.

    `source` names which config layer (env var / override file / packaged
    default) produced `url`, so the raised error points an operator at the
    layer to fix rather than just the bad value.
    """
    assert_contract(
        contract=is_valid_service_url(url),
        msg=f"PS Service URL '{url}' is not a valid http(s) URL",
        hint=f"this value came from {source}",
    )


def load_config(
    *,
    cwd: Path | None = None,
    context: str | None = None,
    config_dir: Path | None = None,
) -> CliConfig:
    """Resolve ps-cli configuration into a validated `CliConfig`.

    Resolution precedence, highest wins:

    1. `PS_CLI_SERVICE_URL` env var.
    2. `context` param, if given -- validated against `targets.contexts`
       *unconditionally* whenever given, even when the env var will end up
       winning, so an invalid `--context` name never goes silently unvalidated.
    3. `targets.current_context`, if `targets.toml` exists and sets it; raises
       if it names a context missing from `[contexts]`.
    4. Otherwise, the project-root override file (`<cwd>/ps-cli.toml`) deep-merged
       over the packaged default -- used when there is no `targets.toml`, or one
       with no `current_context` set and no `--context` given.

    Fails closed on an explicitly empty `PS_CLI_SERVICE_URL`, an invalid/missing
    context name, or a resolved URL that is not a valid http(s) URL, from whichever
    layer produced it -- never silently substitutes a different value.

    Parameters:
        cwd: Directory searched for `ps-cli.toml`; defaults to the real current
            working directory. Overriding it is the seam tests use to avoid touching
            the real working directory.
        context: The `--context` flag's value for this invocation, or `None`.
        config_dir: Directory holding `targets.toml`; defaults to
            `resolve_config_dir()`.

    Returns:
        A `CliConfig` whose `service_url` is a valid http(s) URL and whose
        `context_name` is the context that supplied it (cases 2 and 3 when the env
        var does not win), else `None`.

    Raises:
        PsCliError: `PS_CLI_SERVICE_URL` is set but empty; `context` or
            `current_context` names a context not defined in `targets.toml` (the
            hint lists the valid names); `targets.toml` is invalid TOML or holds an
            old-format context; the merged `service_url` is not a string; or the
            resolved URL is not a valid http(s) URL (the hint names the layer).
        OSError: `ps-cli.toml` exists but cannot be read (propagated unwrapped).
        tomllib.TOMLDecodeError: `ps-cli.toml` is invalid TOML (propagated
            unwrapped).
        FileNotFoundError: The packaged `default_config.toml` is missing (a package
            build bug; propagated unwrapped).

    Side Effects:
        Reads `targets.toml`, the env var and, in case 4, `ps-cli.toml` and the
        packaged default. Writes nothing.
    """
    resolved_cwd = cwd if cwd is not None else Path.cwd()
    resolved_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    targets = load_targets(resolved_config_dir)
    contexts = targets.contexts if targets is not None else {}
    context_name: str | None = None

    # Case 2's validation happens unconditionally, ahead of the env-var check
    # below, so an invalid `--context` name always errors — even when
    # PS_CLI_SERVICE_URL is also set and would otherwise supply the
    # resolved URL.
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
        service_url = contexts[context].url
        source = f"the '{context}' context (targets.toml, via --context)"
        context_name = context
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
        service_url = contexts[current_context].url
        source = f"the current context '{current_context}' (targets.toml)"
        context_name = current_context
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
    return CliConfig(
        service_url=service_url,
        context_name=context_name,
    )
