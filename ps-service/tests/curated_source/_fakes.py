"""Shared test doubles for `ps_service.curated_source` and its consumers (issue #130).

`tests/curated_source/` is an importable package, so `tests/mcp_interface/` imports these
instead of redeclaring them, mirroring `tests/authz/_fakes.py`. (They live here, not under
`tests/runtime_config/`, because pytest's importlib mode makes cross-package imports depend on
collection order, and `curated_source` and `mcp_interface` collect before `runtime_config`.)

`InMemoryRuntimeConfigStore` is a hand-written `RuntimeConfigStore` at the persistence
boundary. It prepares every value through the same public registry function the real store
uses (`prepare_runtime_config_value`), on write and again on read, so a test using it still
exercises each key's real type check and validator, never a bypass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ps_service.runtime_config import (
    RuntimeConfigPersistenceError,
    RuntimeConfigUnavailableError,
    prepare_runtime_config_value,
    require_runtime_config_key,
)

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig


@dataclass(frozen=True, slots=True)
class RecordedWrite:
    """One successful `set`/`reset` an `InMemoryRuntimeConfigStore` observed."""

    action: str
    key: str
    actor: tuple[str, str]
    value: object | None


@dataclass
class InMemoryRuntimeConfigStore:
    """In-memory `RuntimeConfigStore`; `rows` may be shared across instances to model restarts."""

    config: ServiceConfig
    rows: dict[str, object] = field(default_factory=dict)
    writes: list[RecordedWrite] = field(default_factory=list)
    fail_reads: bool = False
    fail_writes: bool = False

    def get(self, key: str) -> object | None:
        """Return the stored value for `key` re-validated, `None` when absent."""
        registered = require_runtime_config_key(key)
        if self.fail_reads:
            raise RuntimeConfigUnavailableError
        if key not in self.rows:
            return None
        return prepare_runtime_config_value(self.config, registered, self.rows[key])

    def set(self, key: str, value: object, *, actor: tuple[str, str]) -> None:
        """Validate `value` for `key` through the real registry, then store it."""
        validated = prepare_runtime_config_value(
            self.config, require_runtime_config_key(key), value
        )
        if self.fail_writes:
            raise RuntimeConfigPersistenceError
        self.rows[key] = validated
        self.writes.append(RecordedWrite("set", key, actor, validated))

    def reset(self, key: str, *, actor: tuple[str, str]) -> None:
        """Remove `key`'s row (a no-op when absent)."""
        require_runtime_config_key(key)
        if self.fail_writes:
            raise RuntimeConfigPersistenceError
        self.rows.pop(key, None)
        self.writes.append(RecordedWrite("reset", key, actor, None))
