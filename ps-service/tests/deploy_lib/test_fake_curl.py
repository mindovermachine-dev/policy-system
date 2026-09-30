"""Smoke tests for the shared test fakes (issue #165 Slice 0).

Later script tests (owner lib, eval script) put these fakes on PATH. These tests exist so a
failure there is attributable to the script under test, not to the fake's behaviour.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FAKES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fakes"
API = "https://authentik.local:30443/api/v3"
TOKEN = "s3cr3t-token-value"


@dataclass
class FakeCurl:
    state_path: Path
    log_path: Path

    def seed(self, **state: object) -> None:
        current: dict[str, object] = (
            json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        )
        current.update(state)
        self.state_path.write_text(json.dumps(current))

    def call(self, *args: str, token: str | None = TOKEN) -> tuple[int, int, Any]:
        cmd = [sys.executable, str(FAKES_DIR / "curl"), "-s", "-w", "%{http_code}", *args]
        stdin = None
        if token is not None:
            cmd.extend(["--config", "-"])
            stdin = f'header = "Authorization: Bearer {token}"\n'
        env = {
            "PS_TEST_CURL_STATE": str(self.state_path),
            "PS_TEST_CURL_LOG": str(self.log_path),
        }
        proc = subprocess.run(  # noqa: S603 - repo-local fake under sys.executable, literal args
            cmd, input=stdin, capture_output=True, text=True, env=env, check=False
        )
        out = proc.stdout
        status = int(out[-3:]) if len(out) >= 3 and out[-3:].isdigit() else 0
        body = json.loads(out[:-3]) if out[:-3] else None
        return proc.returncode, status, body


@pytest.fixture
def curl(tmp_path: Path) -> FakeCurl:
    fake = FakeCurl(tmp_path / "state.json", tmp_path / "curl.log")
    fake.seed(token=TOKEN, recovery_flow=True)
    return fake


FIXTURES_DIR = FAKES_DIR / "authentik"


def _create_owner(curl: FakeCurl, username: str = "owner@example.com") -> dict[str, Any]:
    _, status, body = curl.call(
        "-X",
        "POST",
        "-d",
        json.dumps({"username": username, "name": username, "email": username, "type": "internal"}),
        f"{API}/core/users/",
    )
    assert status == 201
    return body


def test_users_me_accepts_the_seeded_token_and_rejects_another_with_403(curl: FakeCurl) -> None:
    _, status, body = curl.call(f"{API}/core/users/me/")
    assert status == 200
    assert body["user"]["username"] == "akadmin"
    assert body["user"]["is_superuser"] is True

    _, status, body = curl.call(f"{API}/core/users/me/", token="wrong")
    # Recorded against Authentik 2026.8.3: an invalid API token is a 403, not a 401.
    assert status == 403
    assert body == {"detail": "Token invalid/expired"}


def test_admins_group_is_found_by_name(curl: FakeCurl) -> None:
    _, status, body = curl.call(f"{API}/core/groups/?name=authentik%20Admins")
    assert status == 200
    assert body["results"][0]["name"] == "authentik Admins"


def test_user_creation_enforces_unique_usernames(curl: FakeCurl) -> None:
    first = _create_owner(curl)
    assert first["username"] == "owner@example.com"
    _, status, body = curl.call(
        "-X",
        "POST",
        "-d",
        json.dumps({"username": "owner@example.com", "name": "x"}),
        f"{API}/core/users/",
    )
    assert status == 400
    assert body == {"username": ["This field must be unique."]}


def test_user_creation_requires_a_name(curl: FakeCurl) -> None:
    _, status, body = curl.call(
        "-X", "POST", "-d", json.dumps({"username": "a@example.com"}), f"{API}/core/users/"
    )
    assert status == 400
    assert body == {"name": ["This field is required."]}


def test_created_admin_group_member_has_the_recorded_user_shape(curl: FakeCurl) -> None:
    _, _, groups = curl.call(f"{API}/core/groups/?name=authentik%20Admins")
    admins_pk = groups["results"][0]["pk"]
    _, status, body = curl.call(
        "-X",
        "POST",
        "-d",
        json.dumps({"username": "o@example.com", "name": "o", "groups": [admins_pk]}),
        f"{API}/core/users/",
    )
    recorded = json.loads((FIXTURES_DIR / "user_created.json").read_text())
    assert status == 201
    assert set(body) == set(recorded)
    assert body["groups"] == [admins_pk]
    assert body["is_superuser"] is True
    assert isinstance(body["pk"], int)


def test_users_list_uses_the_recorded_paginated_envelope(curl: FakeCurl) -> None:
    _create_owner(curl)
    _, _, body = curl.call(f"{API}/core/users/?username=owner@example.com")
    recorded = json.loads((FIXTURES_DIR / "users_list.json").read_text())
    assert set(body) == set(recorded)
    assert set(body["pagination"]) == set(recorded["pagination"])
    assert len(body["results"]) == 1


def test_recovery_link_carries_the_request_authority(curl: FakeCurl) -> None:
    user = _create_owner(curl)
    _, status, body = curl.call(
        "-X", "POST", f"{API}/core/users/{user['pk']}/recovery/?token_duration=minutes=30"
    )
    assert status == 200
    assert body["link"].startswith("https://authentik.local:30443/if/flow/ps-passkey-recovery/")


def test_recovery_without_a_recovery_flow_is_a_400(curl: FakeCurl) -> None:
    curl.seed(recovery_flow=False)
    user = _create_owner(curl)
    _, status, body = curl.call("-X", "POST", f"{API}/core/users/{user['pk']}/recovery/")
    assert status == 400
    assert body == {"non_field_errors": "No recovery flow set."}


def test_recovery_for_an_unknown_user_is_the_recorded_404(curl: FakeCurl) -> None:
    _, status, body = curl.call("-X", "POST", f"{API}/core/users/99999/recovery/")
    assert status == 404
    assert body == {"detail": "No User matches the given query."}


def test_devices_all_filters_by_user_and_returns_a_bare_array(curl: FakeCurl) -> None:
    enrolled = _create_owner(curl, "a@example.com")
    other = _create_owner(curl, "b@example.com")
    curl.seed(devices={str(enrolled["pk"]): [{"pk": 7, "name": "WebAuthn Device"}]})

    _, _, found = curl.call(f"{API}/authenticators/admin/all/?user={enrolled['pk']}")
    _, _, none = curl.call(f"{API}/authenticators/admin/all/?user={other['pk']}")

    recorded = json.loads((FIXTURES_DIR / "devices_all_one.json").read_text())
    assert [d["pk"] for d in found] == ["7"]
    assert set(found[0]) == set(recorded[0])
    assert none == []


def test_devices_all_for_an_unknown_user_is_the_recorded_400(curl: FakeCurl) -> None:
    _, status, body = curl.call(f"{API}/authenticators/admin/all/?user=99999")
    assert status == 400
    assert body == {"user": ['Invalid pk "99999" - object does not exist.']}


def test_webauthn_admin_list_ignores_the_user_query_like_real_authentik(curl: FakeCurl) -> None:
    """Authentik 2026.8.3 has no `user` filter on this endpoint (it lists every device)."""
    first = _create_owner(curl, "a@example.com")
    second = _create_owner(curl, "b@example.com")
    curl.seed(
        devices={
            str(first["pk"]): [{"pk": 1, "name": "WebAuthn Device"}],
            str(second["pk"]): [{"pk": 2, "name": "WebAuthn Device"}],
        }
    )

    _, _, body = curl.call(f"{API}/authenticators/admin/webauthn/?user={first['pk']}")

    assert body["pagination"]["count"] == 2
    assert {d["user"]["pk"] for d in body["results"]} == {first["pk"], second["pk"]}


def test_invitation_creation_returns_the_recorded_shape(curl: FakeCurl) -> None:
    _, status, body = curl.call(
        "-X",
        "POST",
        "-d",
        json.dumps({"name": "ps-invite-x", "single_use": True, "fixed_data": {"email": "i@e.com"}}),
        f"{API}/stages/invitation/invitations/",
    )
    recorded = json.loads((FIXTURES_DIR / "invitation_created.json").read_text())
    assert status == 201
    assert set(body) == set(recorded)
    assert body["fixed_data"] == {"email": "i@e.com"}


@pytest.mark.parametrize("fixture", sorted(FIXTURES_DIR.glob("*.json")), ids=lambda p: p.name)
def test_recorded_fixtures_carry_no_secrets(fixture: Path) -> None:
    text = fixture.read_text()
    json.loads(text)
    assert "eyJ" not in text
    assert "BEGIN" not in text
    assert "Bearer" not in text
    assert not re.search(r"flow_token=(?!FLOW_TOKEN)", text)


def test_user_delete_removes_the_user(curl: FakeCurl) -> None:
    user = _create_owner(curl)
    assert curl.call("-X", "DELETE", f"{API}/core/users/{user['pk']}/")[1] == 204
    _, _, body = curl.call(f"{API}/core/users/?username=owner@example.com")
    assert body["results"] == []


def test_unreachable_switch_exits_7(curl: FakeCurl) -> None:
    curl.seed(unreachable=True)
    assert curl.call(f"{API}/core/users/me/")[0] == 7


def test_token_from_stdin_config_never_reaches_the_argv_log(curl: FakeCurl) -> None:
    curl.call(f"{API}/core/users/me/")
    assert TOKEN not in curl.log_path.read_text()


def test_fake_podman_reports_machine_state_and_logs_argv(tmp_path: Path) -> None:
    log = tmp_path / "podman.log"
    env = {"PS_TEST_PODMAN_LOG": str(log), "PS_TEST_PODMAN_MACHINE_STATE": "stopped"}
    proc = subprocess.run(  # noqa: S603 - repo-local fake under sys.executable, literal args
        [sys.executable, str(FAKES_DIR / "podman"), "machine", "list"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert "stopped" in proc.stdout
    assert json.loads(log.read_text().splitlines()[0]) == ["machine", "list"]


def test_token_reject_status_switch_changes_the_status_for_a_wrong_token(curl: FakeCurl) -> None:
    curl.seed(token_reject_status=401)
    _, status, _ = curl.call(f"{API}/core/users/me/", token="wrong")
    assert status == 401


def test_blueprint_apply_is_recorded_for_a_known_instance(curl: FakeCurl) -> None:
    _, status, body = curl.call(f"{API}/managed/blueprints/?name=Policy%20System%20local%20TLS")
    assert status == 200
    pk = body["results"][0]["pk"]
    _, applied, _ = curl.call("-X", "POST", f"{API}/managed/blueprints/{pk}/apply/")
    assert applied == 200
    assert json.loads(curl.state_path.read_text())["applied_blueprints"] == [pk]


def test_blueprint_list_and_apply_serve_the_shape_recorded_from_a_live_authentik(
    curl: FakeCurl,
) -> None:
    """LC2 (issue #165) recorded `GET /managed/blueprints/?name=` and `POST .../apply/` against a
    real Authentik 2026.8.3; the shapes were assumed before that.
    """
    _, status, listing = curl.call(f"{API}/managed/blueprints/?name=Policy%20System%20local%20TLS")

    assert status == 200
    assert sorted(listing) == ["autocomplete", "pagination", "results"]
    instance = listing["results"][0]
    assert sorted(instance) == [
        "content",
        "context",
        "enabled",
        "last_applied",
        "last_applied_hash",
        "managed_models",
        "metadata",
        "name",
        "path",
        "pk",
        "status",
    ]
    assert instance["status"] == "successful"

    _, applied_status, applied = curl.call(
        "-X", "POST", f"{API}/managed/blueprints/{instance['pk']}/apply/"
    )
    assert applied_status == 200
    assert sorted(applied) == sorted(instance)


def test_blueprint_list_filters_by_name_like_the_live_api(curl: FakeCurl) -> None:
    _, _, listing = curl.call(f"{API}/managed/blueprints/?name=nonexistent")

    assert listing["results"] == []
