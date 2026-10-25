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


class _FakeRuntimeConfigStore:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.writes: list[tuple[str, object, tuple[str, str]]] = []

    def get(self, key: str) -> object | None:
        return self.values.get(key)

    def set(self, key: str, value: object, *, actor: tuple[str, str]) -> None:
        self.values[key] = value
        self.writes.append((key, value, actor))


class _FakeRunStore:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, object]] = {}
        self.completions: list[tuple[str, str, dict[str, object] | None]] = []

    def get_run(self, run_id: str) -> object | None:
        return self.runs.get(run_id)

    def create_run(
        self, *, run_id: str, celex: str, short_name: str, actor: tuple[str, str]
    ) -> object:
        self.runs[run_id] = {"celex": celex, "short_name": short_name, "actor": actor}
        return self.runs[run_id]

    def complete_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, object] | None,
        error: str | None,
        reason_code: object | None = None,
        audit_actor: tuple[str, str] | None = None,
    ) -> bool:
        assert error is None
        assert reason_code is None
        assert audit_actor is None
        self.completions.append((run_id, status, result))
        return True


class _FakeApprovalStore:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    def create_pending_approval(
        self,
        *,
        tool_name: str,
        normalized_args: dict[str, object],
        actor_subject: str,
        actor_issuer: str,
        display_summary: dict[str, object],
    ) -> tuple[object, str]:
        self.created.append(
            {
                "tool_name": tool_name,
                "normalized_args": normalized_args,
                "actor": (actor_subject, actor_issuer),
                "display_summary": display_summary,
            }
        )
        return object(), "raw-code"


class _FakeCredentialStore:
    def __init__(self) -> None:
        self.rows: dict[bytes, dict[str, object]] = {}

    def get_by_credential_id(self, credential_id: bytes) -> object | None:
        return self.rows.get(credential_id)

    def create_signing_credential(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        rp_id: str | None,
    ) -> object:
        self.rows[credential_id] = {
            "actor": (actor_subject, actor_issuer),
            "public_key": public_key,
            "sign_count": sign_count,
            "rp_id": rp_id,
        }
        return self.rows[credential_id]


class _StateStores:
    def __init__(self) -> None:
        self.runtime_config = _FakeRuntimeConfigStore()
        self.runs = _FakeRunStore()
        self.approvals = _FakeApprovalStore()
        self.credentials = _FakeCredentialStore()

    def seed(self, module: ModuleType, *, issuer: str = "https://idp.invalid") -> None:
        module.seed_state(
            self.runtime_config,
            self.runs,
            self.approvals,
            self.credentials,
            issuer=issuer,
            catalog_url="https://catalog.example/effective",
        )


def test_seed_state_writes_one_synthetic_row_to_each_state_table() -> None:
    module = _load("seed_state")
    stores = _StateStores()

    stores.seed(module)

    assert stores.runtime_config.values == {
        "curated_source.base_url": "https://catalog.example/effective"
    }
    assert list(stores.runs.runs) == [module.SEED_RUN_ID]
    assert stores.runs.runs[module.SEED_RUN_ID]["celex"] == "32099R0000"
    assert [(run_id, status) for run_id, status, _ in stores.runs.completions] == [
        (module.SEED_RUN_ID, "succeeded")
    ]
    assert len(stores.approvals.created) == 1
    assert stores.approvals.created[0]["tool_name"] == "backup-restore-verify"
    assert list(stores.credentials.rows) == [module.SEED_CREDENTIAL_ID]


def test_seed_state_attributes_every_row_to_a_synthetic_actor_of_the_given_issuer() -> None:
    module = _load("seed_state")
    stores = _StateStores()

    stores.seed(module, issuer="https://idp.example")

    actor = (module.SEED_ACTOR_SUBJECT, "https://idp.example")
    assert module.SEED_ACTOR_SUBJECT.startswith("system:backup-restore-verify")
    assert [written_actor for _, _, written_actor in stores.runtime_config.writes] == [actor]
    assert stores.runs.runs[module.SEED_RUN_ID]["actor"] == actor
    assert stores.approvals.created[0]["actor"] == actor
    assert stores.credentials.rows[module.SEED_CREDENTIAL_ID]["actor"] == actor


def test_seed_state_twice_adds_no_duplicate_config_run_or_credential() -> None:
    module = _load("seed_state")
    stores = _StateStores()

    stores.seed(module)
    stores.seed(module)

    assert len(stores.runtime_config.writes) == 1
    assert len(stores.runs.completions) == 1
    assert len(stores.credentials.rows) == 1


def test_seed_state_adds_one_pending_approval_per_call() -> None:
    module = _load("seed_state")
    stores = _StateStores()

    stores.seed(module)
    stores.seed(module)

    assert len(stores.approvals.created) == 2


def test_seed_state_keeps_an_operator_configured_catalog_source() -> None:
    module = _load("seed_state")
    stores = _StateStores()
    stores.runtime_config.values["curated_source.base_url"] = "https://catalog.example/c"

    stores.seed(module)

    assert stores.runtime_config.writes == []
    assert stores.runtime_config.values["curated_source.base_url"] == "https://catalog.example/c"
