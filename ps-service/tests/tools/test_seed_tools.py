"""Tests for the `tools/backup-restore-verify` seed helpers.

The helpers run for real inside the ps-service pod; here only their pure parts are exercised,
with the access-role store seam and the restore callable injected.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

    from ps_service.authz.models import AccessRole

_TOOLS = Path(__file__).resolve().parents[3] / "tools" / "backup-restore-verify"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_{name}_under_test", _TOOLS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingStore:
    def __init__(self) -> None:
        self.grants: list[tuple[tuple[str, str], tuple[str, str], AccessRole]] = []

    def grant(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        self.grants.append((actor, target, access_role))


def test_seed_grants_grants_one_role_per_principal_through_the_store() -> None:
    module = _load("seed_grants")
    store = _RecordingStore()

    granted = module.seed_grants(store, issuer="https://idp.invalid", prefix="p")

    assert [role for _, role in granted] == list(module.SEED_ROLES)
    assert len(store.grants) == len(module.SEED_ROLES)
    assert {target for _, target, _ in store.grants} == {
        (subject, "https://idp.invalid") for subject, _ in granted
    }
    assert {actor for actor, _, _ in store.grants} == {
        (module.SEED_ACTOR_SUBJECT, "https://idp.invalid")
    }
    assert {role for _, _, role in store.grants} == set(module.SEED_ROLES)


def test_seed_instruments_restores_each_in_order_and_stops_on_failure() -> None:
    module = _load("seed_content")
    seen: list[str] = []

    assert module.seed_instruments(["A-1.0", "B-1.0"], seen.append) == ["A-1.0", "B-1.0"]
    assert seen == ["A-1.0", "B-1.0"]

    def failing(instrument_id: str) -> None:
        if instrument_id == "B-1.0":
            message = "source unreachable"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        module.seed_instruments(["A-1.0", "B-1.0", "C-1.0"], failing)


def test_seed_content_main_configures_logging_and_installs_a_gateway_before_the_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load("seed_content")
    events: list[str] = []

    def configure(*_args: object, **_kwargs: object) -> None:
        events.append("configure")

    def restore(*_args: object, **_kwargs: object) -> None:
        events.append("restore")

    # detroit-exception: seed CLI entrypoint wiring; the call order IS the spec
    monkeypatch.setattr(module, "configure_logging", configure)
    # detroit-exception: seed CLI entrypoint wiring; the call order IS the spec
    monkeypatch.setattr(module, "load_config", _FakeConfig)
    # detroit-exception: seed CLI entrypoint wiring; the call order IS the spec
    monkeypatch.setattr(module, "build_default_restore_from_catalog_dependencies", object)
    monkeypatch.setattr(module, "PsycopgAuditStore", _FakeAuditStore)
    # detroit-exception: seed CLI entrypoint wiring; the call order IS the spec
    monkeypatch.setattr(module, "run_restoration_from_catalog_source", restore)
    gateway = _FakeGateway(events)

    def build(_config: object) -> _FakeGateway:
        return gateway

    def install(installed: object) -> None:
        events.append(f"install:{installed is gateway}")

    # detroit-exception: seed CLI entrypoint wiring; the call order IS the spec
    monkeypatch.setattr(module, "build_default_graph_write_gateway", build)
    # detroit-exception: seed CLI entrypoint wiring; the call order IS the spec
    monkeypatch.setattr(module, "install_graph_write_gateway", install)

    assert module.main(["--instrument", "A-1.0"]) == 0
    assert events == ["configure", "install:True", "restore", "install:False", "stop"]


class _FakeConfig:
    authz_bootstrap_owner_issuer = "https://idp.invalid"


class _FakeAuditStore:
    def __init__(self, _config: object) -> None:
        pass


class _FakeGateway:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stop_reconciler(self) -> bool:
        self._events.append("stop")
        return True
