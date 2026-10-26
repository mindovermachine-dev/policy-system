"""Owner passkey reset support in `scripts/lib/authentik-owner.sh`.

The real library is sourced by a real bash; `curl` is the stateful Authentik fake. Covered: the
token check records whose token it is, the token never appears in a traced (`set -x`) run, and
the owner's devices of every type are deleted by ids from the owner's own listing only.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import LibHarness

EMAIL = "owner@example.com"
HOST = "https://ps.test:30443/auth"
ISSUER = f"{HOST}/application/o/ps-cli/"
OWNER: dict[str, object] = {
    "pk": 7,
    "username": EMAIL,
    "email": EMAIL,
    "type": "internal",
    "groups": [],
    "is_active": True,
}

PRELUDE = f"""
set -uo pipefail
print_error() {{ local f="$1"; shift; printf "$f" "$@" >&2; }}
source "$PS_TEST_LIB_DIR/authentik-owner.sh"
AUTHENTIK_API_BASE="{HOST}"
AUTHENTIK_LINK_BASE="{HOST}"
AUTHENTIK_CURL_ARGS=()
RESET_NAMESPACE=policy-system
RESET_PS_DEPLOYMENT=policy-system-ps-service
RESET_OWNER_SUBJECT="{EMAIL}"
RESET_OWNER_ISSUER="{ISSUER}"
"""


def test_the_token_check_records_the_user_the_token_belongs_to(lib_harness: LibHarness) -> None:
    lib_harness.seed_curl(users={"7": OWNER}, token_user_pk=7)

    proc = lib_harness.run(
        PRELUDE + 'AUTHENTIK_API_TOKEN="$PS_TEST_TOKEN"\n'
        'check_token_accepted || exit 1; printf "%s" "$TOKEN_USER_PK"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "7"


def test_a_token_of_the_default_admin_records_its_pk(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(
        PRELUDE + 'AUTHENTIK_API_TOKEN="$PS_TEST_TOKEN"\n'
        'check_token_accepted || exit 1; printf "%s" "$TOKEN_USER_PK"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "4"


def test_an_unreadable_token_user_fails_before_any_change(lib_harness: LibHarness) -> None:
    lib_harness.seed_curl(token_user_pk=None)

    proc = lib_harness.run(
        PRELUDE + 'AUTHENTIK_API_TOKEN="$PS_TEST_TOKEN"\ncheck_token_accepted; exit $?'
    )

    assert proc.returncode == 1
    assert "which user the shared API token belongs to" in proc.stderr
    assert [c["url"] for c in lib_harness.curl_calls()] == [f"{HOST}/api/v3/core/users/me/"]


def test_the_token_is_absent_from_an_xtrace_run(lib_harness: LibHarness) -> None:
    lib_harness.seed_curl(users={"7": OWNER})

    proc = lib_harness.run(
        PRELUDE + "set -x\n"
        "read_bootstrap_token default policy-system-authentik-api-token || exit 1\n"
        "check_token_accepted || exit 1\n"
        f'find_owner_user "{EMAIL}" || exit 1\n'
        'issue_owner_link "$OWNER_PK" || exit 1\n'
        "set +x\n"
        'printf "%s" "$OWNER_PK"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "7"
    assert "+ " in proc.stderr
    assert lib_harness.token not in proc.stderr
    assert lib_harness.token not in proc.stdout


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fakes" / "authentik"
OTHER_PK = 8
OTHER: dict[str, object] = {**OWNER, "pk": OTHER_PK, "username": "other@example.com"}
TYPE_PATHS = {
    "authentik_stages_authenticator_webauthn.webauthndevice": "/authenticators/admin/webauthn/",
    "authentik_stages_authenticator_totp.totpdevice": "/authenticators/admin/totp/",
    "authentik_stages_authenticator_static.staticdevice": "/authenticators/admin/static/",
    "authentik_stages_authenticator_duo.duodevice": "/authenticators/admin/duo/",
    "authentik_stages_authenticator_sms.smsdevice": "/authenticators/admin/sms/",
    "authentik_stages_authenticator_email.emaildevice": "/authenticators/admin/email/",
    "authentik_stages_authenticator_endpoint_gdtc.endpointdevice": (
        "/authenticators/admin/endpoint/"
    ),
    "authentik_endpoints_connectors_agent.appleindependentsecureenclave": (
        "/endpoints/agents/psso/ise/"
    ),
}
RESET_DEVICES = (
    'AUTHENTIK_API_TOKEN="$PS_TEST_TOKEN"\n'
    'OWNER_API_FAILURE_NOTE="nothing was changed"\n'
    "check_token_accepted || exit 1\n"
    f'find_owner_user "{EMAIL}" || exit 1\n'
    'plan_owner_devices "$OWNER_PK" || exit 1\n'
)


def _recorded_owner_devices() -> list[dict[str, str]]:
    """The owner's devices exactly as the per-user listing reported them, one of each type."""
    rows = json.loads((FIXTURES / "devices_all_owner_every_type.json").read_text())
    return [{"pk": r["pk"], "name": r["name"], "type": r["meta_model_name"]} for r in rows]


def _other_users_devices() -> list[dict[str, str]]:
    """One device of each type for another user: the next pk of each numbered type."""
    return [
        {
            "pk": "2" if d["pk"].isdigit() else str(uuid.uuid5(uuid.NAMESPACE_URL, d["pk"])),
            "name": f"other-{d['name']}",
            "type": d["type"],
        }
        for d in _recorded_owner_devices()
    ]


def _seed_two_users_with_every_device_type(lib_harness: LibHarness) -> None:
    lib_harness.seed_curl(
        users={str(OWNER["pk"]): OWNER, str(OTHER_PK): OTHER},
        devices={
            str(OWNER["pk"]): _recorded_owner_devices(),
            str(OTHER_PK): _other_users_devices(),
        },
    )


def _devices(lib_harness: LibHarness) -> dict[str, list[dict[str, str]]]:
    devices: dict[str, list[dict[str, str]]] = json.loads(lib_harness.curl_state.read_text())[
        "devices"
    ]
    return devices


def _calls(lib_harness: LibHarness, method: str) -> list[str]:
    return [str(c["url"]) for c in lib_harness.curl_calls() if method in c["argv"]]


def test_device_delete_path_routes_every_recorded_type_and_rejects_unknown(
    lib_harness: LibHarness,
) -> None:
    types = [d["type"] for d in _recorded_owner_devices()]
    script = PRELUDE + "".join(f'device_delete_path "{t}" || exit 1; echo\n' for t in types)

    proc = lib_harness.run(script + 'device_delete_path "authentik_x.unknowndevice"; exit $?')

    assert proc.returncode == 1
    assert proc.stdout.splitlines() == [TYPE_PATHS[t] for t in types]


def test_owner_devices_of_every_type_are_deleted_and_the_other_users_survive(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users_with_every_device_type(lib_harness)

    proc = lib_harness.run(
        PRELUDE
        + RESET_DEVICES
        + 'delete_owner_devices || exit 1; printf "%s" "$RESET_DEVICES_DELETED"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "8"
    devices = _devices(lib_harness)
    assert devices[str(OWNER["pk"])] == []
    assert devices[str(OTHER_PK)] == _other_users_devices()


def test_every_device_delete_targets_an_id_from_the_owners_listing(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users_with_every_device_type(lib_harness)

    proc = lib_harness.run(PRELUDE + RESET_DEVICES + "delete_owner_devices || exit 1")

    assert proc.returncode == 0, proc.stderr
    expected = {
        f"{HOST}/api/v3{TYPE_PATHS[d['type']]}{d['pk']}/" for d in _recorded_owner_devices()
    }
    deletes = _calls(lib_harness, "DELETE")
    assert sorted(deletes) == sorted(expected)


def test_never_uses_the_per_type_device_lists(lib_harness: LibHarness) -> None:
    _seed_two_users_with_every_device_type(lib_harness)

    proc = lib_harness.run(PRELUDE + RESET_DEVICES + "delete_owner_devices || exit 1")

    assert proc.returncode == 0, proc.stderr
    lists = {f"{HOST}/api/v3{path}" for path in TYPE_PATHS.values()}
    assert not [url for url in lib_harness.curl_urls() if url.split("?")[0] in lists]


def test_a_retrieved_device_owned_by_another_user_aborts_before_any_delete(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users_with_every_device_type(lib_harness)
    lib_harness.seed_curl(ignore_user_filters=["devices"])

    proc = lib_harness.run(PRELUDE + RESET_DEVICES + "delete_owner_devices; exit $?")

    assert proc.returncode == 1
    assert "another user's device" in proc.stderr
    assert "nothing was changed" in proc.stderr
    assert _calls(lib_harness, "DELETE") == []
    assert len(_devices(lib_harness)[str(OWNER["pk"])]) == len(TYPE_PATHS)


def test_unknown_device_type_aborts_before_any_delete(lib_harness: LibHarness) -> None:
    lib_harness.seed_curl(
        users={str(OWNER["pk"]): OWNER},
        devices={
            str(OWNER["pk"]): [
                {"pk": "1", "name": "passkey"},
                {"pk": "1", "name": "new", "type": "authentik_x.unknowndevice"},
            ]
        },
    )

    proc = lib_harness.run(PRELUDE + RESET_DEVICES + "delete_owner_devices; exit $?")

    assert proc.returncode == 1
    assert "authentik_x.unknowndevice" in proc.stderr
    assert "nothing was changed" in proc.stderr
    assert _calls(lib_harness, "DELETE") == []


def test_already_deleted_device_404_is_not_counted_and_not_an_error(
    lib_harness: LibHarness,
) -> None:
    lib_harness.seed_curl(
        users={str(OWNER["pk"]): OWNER},
        devices={str(OWNER["pk"]): [{"pk": "1", "name": "a"}, {"pk": "2", "name": "b"}]},
    )

    proc = lib_harness.run(
        PRELUDE + RESET_DEVICES + 'authentik_api DELETE "/authenticators/admin/webauthn/1/"\n'
        'delete_owner_devices || exit 1; printf "%s" "$RESET_DEVICES_DELETED"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "1"
    assert _devices(lib_harness)[str(OWNER["pk"])] == []


OWNER_PK = int(str(OWNER["pk"]))
OWNER_FILTER = "user__username=owner%40example.com"
FIND_OWNER = (
    'AUTHENTIK_API_TOKEN="$PS_TEST_TOKEN"\n'
    'OWNER_API_FAILURE_NOTE="the remaining counts follow"\n'
    "check_token_accepted || exit 1\n"
    f'find_owner_user "{EMAIL}" || exit 1\n'
)


def _state(lib_harness: LibHarness, key: str) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = json.loads(lib_harness.curl_state.read_text())[key]
    return rows


def _seed_two_users(lib_harness: LibHarness, **state: object) -> None:
    lib_harness.seed_curl(users={str(OWNER_PK): OWNER, str(OTHER_PK): OTHER}, **state)


def _sessions(owned: int, other: int = 1) -> dict[str, dict[str, int]]:
    rows = {f"owner-{i:03}": {"user": OWNER_PK} for i in range(owned)}
    rows.update({f"other-{i:03}": {"user": OTHER_PK} for i in range(other)})
    return rows


def test_list_pages_follows_next_until_zero_at_page_size_100(lib_harness: LibHarness) -> None:
    _seed_two_users(lib_harness, sessions=_sessions(150))

    proc = lib_harness.run(
        PRELUDE
        + FIND_OWNER
        + f'authentik_list_pages "/core/authenticated_sessions/?{OWNER_FILTER}"'
        ' "listing the owner\'s sessions" || exit 1\n'
        'printf "%s" "$AUTHENTIK_LIST_JSON" | jq -r length'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "150"
    lists = [u for u in lib_harness.curl_urls() if "/core/authenticated_sessions/" in u]
    assert lists == [
        f"{HOST}/api/v3/core/authenticated_sessions/?{OWNER_FILTER}&page_size=100&page=1",
        f"{HOST}/api/v3/core/authenticated_sessions/?{OWNER_FILTER}&page_size=100&page=2",
    ]


def test_list_pages_stops_on_a_non_advancing_next(lib_harness: LibHarness) -> None:
    _seed_two_users(lib_harness, sessions=_sessions(3), stuck_next_page=1)

    proc = lib_harness.run(
        PRELUDE
        + FIND_OWNER
        + f'authentik_list_pages "/core/authenticated_sessions/?{OWNER_FILTER}"'
        ' "listing the owner\'s sessions"; exit $?'
    )

    assert proc.returncode == 1
    assert "did not advance past page 1" in proc.stderr
    assert "the remaining counts follow" in proc.stderr
    assert len([u for u in lib_harness.curl_urls() if "/authenticated_sessions/" in u]) == 1


def test_owner_sessions_are_deleted_by_username_filter_and_the_other_users_survive(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(lib_harness, sessions=_sessions(3, other=2))

    proc = lib_harness.run(
        PRELUDE + FIND_OWNER + f'delete_owner_sessions "{EMAIL}" || exit 1\n'
        'printf "%s" "$RESET_SESSIONS_DELETED"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "3"
    assert _state(lib_harness, "sessions") == {
        "other-000": {"user": OTHER_PK},
        "other-001": {"user": OTHER_PK},
    }
    deletes = _calls(lib_harness, "DELETE")
    assert sorted(deletes) == [
        f"{HOST}/api/v3/core/authenticated_sessions/owner-00{i}/" for i in range(3)
    ]


def test_sessions_are_never_listed_with_the_ignored_user_pk_filter_or_bulk_deleted(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(lib_harness, sessions=_sessions(2))

    proc = lib_harness.run(PRELUDE + FIND_OWNER + f'delete_owner_sessions "{EMAIL}" || exit 1')

    assert proc.returncode == 0, proc.stderr
    sessions = [u for u in lib_harness.curl_urls() if "/authenticated_sessions/" in u]
    assert sessions
    assert not [u for u in sessions if "?user=" in u or "&user=" in u]
    assert not [u for u in sessions if "bulk_delete" in u]


def test_a_listing_that_returns_another_users_session_aborts_with_no_session_deleted(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(lib_harness, sessions=_sessions(2), ignore_user_filters=["sessions"])

    proc = lib_harness.run(PRELUDE + FIND_OWNER + f'delete_owner_sessions "{EMAIL}"; exit $?')

    assert proc.returncode == 1
    assert "another user's session" in proc.stderr
    assert _calls(lib_harness, "DELETE") == []
    assert len(_state(lib_harness, "sessions")) == 3


def _tokens(owned: list[str], other: list[str]) -> dict[str, dict[str, int]]:
    rows = {pk: {"user": OWNER_PK} for pk in owned}
    rows.update({pk: {"user": OTHER_PK} for pk in other})
    return rows


def test_owner_access_and_refresh_tokens_are_deleted_and_the_other_users_survive(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(
        lib_harness,
        access_tokens=_tokens(["1", "2"], ["3"]),
        refresh_tokens=_tokens(["4"], ["5"]),
    )

    proc = lib_harness.run(
        PRELUDE + FIND_OWNER + 'revoke_owner_oauth_tokens "$OWNER_PK" || exit 1\n'
        'printf "%s" "$RESET_OAUTH_TOKENS_REVOKED"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "3"
    assert _state(lib_harness, "access_tokens") == {"3": {"user": OTHER_PK}}
    assert _state(lib_harness, "refresh_tokens") == {"5": {"user": OTHER_PK}}


def test_oauth_tokens_are_listed_by_user_pk_never_by_username(lib_harness: LibHarness) -> None:
    _seed_two_users(lib_harness, access_tokens=_tokens(["1"], ["2"]))

    proc = lib_harness.run(PRELUDE + FIND_OWNER + 'revoke_owner_oauth_tokens "$OWNER_PK" || exit 1')

    assert proc.returncode == 0, proc.stderr
    lists = [u for u in _calls(lib_harness, "GET") if "/oauth2/" in u]
    assert {u.split("?")[0] for u in lists} == {
        f"{HOST}/api/v3/oauth2/refresh_tokens/",
        f"{HOST}/api/v3/oauth2/access_tokens/",
    }
    assert all(f"?user={OWNER_PK}&" in u for u in lists)
    assert not [u for u in lists if "user__username" in u]


def test_an_oauth_row_owned_by_another_user_aborts_before_any_token_delete(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(
        lib_harness,
        access_tokens=_tokens(["1"], ["2"]),
        refresh_tokens=_tokens(["3"], ["4"]),
        ignore_user_filters=["oauth"],
    )

    proc = lib_harness.run(PRELUDE + FIND_OWNER + 'revoke_owner_oauth_tokens "$OWNER_PK"; exit $?')

    assert proc.returncode == 1
    assert "another user's OAuth refresh token" in proc.stderr
    assert _calls(lib_harness, "DELETE") == []


def test_refresh_tokens_are_deleted_before_any_access_token(lib_harness: LibHarness) -> None:
    _seed_two_users(
        lib_harness, access_tokens=_tokens(["1", "2"], []), refresh_tokens=_tokens(["3", "4"], [])
    )

    proc = lib_harness.run(PRELUDE + FIND_OWNER + 'revoke_owner_oauth_tokens "$OWNER_PK" || exit 1')

    assert proc.returncode == 0, proc.stderr
    kinds = [u.split("/oauth2/")[1].split("/")[0] for u in _calls(lib_harness, "DELETE")]
    assert kinds == ["refresh_tokens", "refresh_tokens", "access_tokens", "access_tokens"]


EVERY_INTENT = {
    "owner-api": {"user": OWNER_PK, "intent": "api"},
    "owner-app-password": {"user": OWNER_PK, "intent": "app_password"},
    "owner-old-recovery": {"user": OWNER_PK, "intent": "recovery"},
    "owner-verification": {"user": OWNER_PK, "intent": "verification"},
    "other-api": {"user": OTHER_PK, "intent": "api"},
}
DELETE_CORE_TOKENS = (
    f'delete_owner_core_tokens "{EMAIL}" || exit 1\n'
    'printf "%s %s" "$RESET_CORE_TOKENS_DELETED" "$RESET_CORE_TOKENS_KEPT"'
)


def test_owner_core_tokens_of_every_intent_are_deleted_and_the_other_users_survive(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(lib_harness, core_tokens=EVERY_INTENT)

    proc = lib_harness.run(PRELUDE + FIND_OWNER + DELETE_CORE_TOKENS)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "4 0"
    assert _state(lib_harness, "core_tokens") == {"other-api": EVERY_INTENT["other-api"]}


def test_core_tokens_are_listed_by_username_never_by_user_pk(lib_harness: LibHarness) -> None:
    _seed_two_users(lib_harness, core_tokens=EVERY_INTENT)

    proc = lib_harness.run(PRELUDE + FIND_OWNER + DELETE_CORE_TOKENS)

    assert proc.returncode == 0, proc.stderr
    lists = [u for u in _calls(lib_harness, "GET") if "/core/tokens/" in u]
    assert lists
    assert all(f"?{OWNER_FILTER}&" in u for u in lists)
    assert not [u for u in lists if "?user=" in u or "&user=" in u]


def test_a_core_token_row_owned_by_another_user_aborts_before_any_delete(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(lib_harness, core_tokens=EVERY_INTENT, ignore_user_filters=["core_tokens"])

    proc = lib_harness.run(PRELUDE + FIND_OWNER + DELETE_CORE_TOKENS)

    assert proc.returncode == 1
    assert "another user's token" in proc.stderr
    assert _calls(lib_harness, "DELETE") == []


def test_a_token_owned_by_another_user_keeps_no_core_token(lib_harness: LibHarness) -> None:
    _seed_two_users(lib_harness, core_tokens=EVERY_INTENT, token_user_pk=OTHER_PK)

    proc = lib_harness.run(PRELUDE + FIND_OWNER + DELETE_CORE_TOKENS)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "4 0"
    assert "owner-api" not in _state(lib_harness, "core_tokens")


def test_an_owner_owned_token_keeps_only_the_owners_api_tokens_and_deletes_every_other_intent(
    lib_harness: LibHarness,
) -> None:
    tokens = {**EVERY_INTENT, "owner-api-2": {"user": OWNER_PK, "intent": "api"}}
    _seed_two_users(lib_harness, core_tokens=tokens, token_user_pk=OWNER_PK)

    proc = lib_harness.run(PRELUDE + FIND_OWNER + DELETE_CORE_TOKENS)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "3 2"
    assert set(_state(lib_harness, "core_tokens")) == {"owner-api", "owner-api-2", "other-api"}


def test_kept_api_tokens_do_not_fail_the_postcondition(lib_harness: LibHarness) -> None:
    _seed_two_users(lib_harness, core_tokens=EVERY_INTENT, token_user_pk=OWNER_PK)

    proc = lib_harness.run(
        PRELUDE + FIND_OWNER + f'delete_owner_core_tokens "{EMAIL}" || exit 1\n'
        f'owner_reset_postcondition "{EMAIL}" || exit 1\n'
        f'owner_remaining_counts "{EMAIL}"'
    )

    assert proc.returncode == 0, proc.stderr
    assert "devices=0 sessions=0 oauth_tokens=0 core_tokens=0 " in proc.stdout


def test_a_remaining_session_fails_the_postcondition_and_names_every_source(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(
        lib_harness,
        sessions=_sessions(1),
        refresh_tokens=_tokens(["1"], []),
        core_tokens={"owner-api": {"user": OWNER_PK, "intent": "api"}},
    )

    proc = lib_harness.run(PRELUDE + FIND_OWNER + f'owner_reset_postcondition "{EMAIL}"; exit $?')

    assert proc.returncode == 1
    assert (
        f"Remaining for {EMAIL}: devices=0 sessions=1 oauth_tokens=1 core_tokens=1 " in proc.stderr
    )


def _seed_signing(lib_harness: LibHarness, credentials: int, pending: list[str]) -> None:
    lib_harness.seed_signing(
        actors={f"{EMAIL}\n{ISSUER}": {"credentials": credentials, "pending": pending}}
    )


def test_remaining_counts_read_the_signing_state_from_the_ps_service_pod(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(lib_harness)
    _seed_signing(lib_harness, 2, ["a-1", "a-2"])

    proc = lib_harness.run(PRELUDE + FIND_OWNER + f'owner_remaining_counts "{EMAIL}"')

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.endswith(" signing_credentials=2 pending_approvals=2")
    assert lib_harness.signing_state().get("audit", []) == []


def test_an_unreadable_signing_state_shows_unknown_and_fails_the_postcondition(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(lib_harness)
    lib_harness.seed_signing(fail="count")

    proc = lib_harness.run(PRELUDE + FIND_OWNER + f'owner_reset_postcondition "{EMAIL}"; exit $?')

    assert proc.returncode == 1
    assert "signing_credentials=unknown pending_approvals=unknown" in proc.stderr


def test_a_remaining_signing_credential_fails_the_postcondition(lib_harness: LibHarness) -> None:
    _seed_two_users(lib_harness)
    _seed_signing(lib_harness, 1, [])

    proc = lib_harness.run(PRELUDE + FIND_OWNER + f'owner_reset_postcondition "{EMAIL}"; exit $?')

    assert proc.returncode == 1
    assert "(signing_credentials=1)" in proc.stderr


def test_a_remaining_pending_approval_fails_the_postcondition(lib_harness: LibHarness) -> None:
    _seed_two_users(lib_harness)
    _seed_signing(lib_harness, 0, ["a-1"])

    proc = lib_harness.run(PRELUDE + FIND_OWNER + f'owner_reset_postcondition "{EMAIL}"; exit $?')

    assert proc.returncode == 1
    assert "(pending_approvals=1)" in proc.stderr
    assert " signing_credentials=0 pending_approvals=1. No link was issued." in proc.stderr


def test_revoking_the_owners_signing_passkeys_records_the_counts_of_the_json_line(
    lib_harness: LibHarness,
) -> None:
    _seed_signing(lib_harness, 3, ["a-1", "a-2"])

    proc = lib_harness.run(
        PRELUDE + "revoke_owner_signing || exit 1\n"
        'printf "%s %s" "$RESET_CREDENTIALS_DELETED" "$RESET_APPROVALS_VOIDED"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "3 2"
    assert lib_harness.signing_state()["audit"] == [
        {
            "subject": EMAIL,
            "issuer": ISSUER,
            "credentials_deleted": 3,
            "voided_approval_ids": ["a-1", "a-2"],
        }
    ]
    assert lib_harness.signing_state()["actors"][f"{EMAIL}\n{ISSUER}"]["pending"] == []
    (call,) = [c for c in lib_harness.kubectl_calls() if "passkey_signing.revoke" in c]
    assert call == (
        "-n policy-system exec deploy/policy-system-ps-service -c ps-service"
        " --pod-running-timeout=60s -- python -m ps_service.passkey_signing.revoke"
        f" --subject {EMAIL} --issuer {ISSUER}"
    )
    assert lib_harness.token not in call


def test_a_failed_signing_revoke_names_its_reason_and_the_pod(lib_harness: LibHarness) -> None:
    lib_harness.seed_signing(fail="revoke")

    proc = lib_harness.run(
        PRELUDE + 'OWNER_API_FAILURE_NOTE="the remaining counts follow"\n'
        "revoke_owner_signing; exit $?"
    )

    assert proc.returncode == 1
    assert (
        "The signing passkey step in the PS Service pod failed (signing_store_unavailable)."
        in proc.stderr
    )
    assert "kubectl -n policy-system logs deploy/policy-system-ps-service" in proc.stderr
    assert "the remaining counts follow" in proc.stderr


def test_the_remaining_report_names_every_count_it_could_not_read(
    lib_harness: LibHarness,
) -> None:
    _seed_two_users(lib_harness, sessions=_sessions(1))
    lib_harness.seed_curl(fail_on={"GET /oauth2/refresh_tokens/": 500, "GET /core/tokens/": 500})
    lib_harness.seed_signing(fail="count")

    proc = lib_harness.run(PRELUDE + FIND_OWNER + f'owner_remaining_report "{EMAIL}"')

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == (
        f"Remaining for {EMAIL}: devices=0 sessions=1 oauth_tokens=unknown core_tokens=unknown"
        " signing_credentials=unknown pending_approvals=unknown. Could not recount: oauth_tokens,"
        " core_tokens, signing_credentials, pending_approvals. No link was issued. Re-running"
        " --reset-owner-passkey is safe and finishes the reset.\n"
    )


TOKEN_FUNCTIONS = frozenset({"authentik_api", "read_bootstrap_token"})
LIB_FILE = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "authentik-owner.sh"
DEPLOY_SCRIPTS = tuple(
    Path(__file__).resolve().parents[3] / "scripts" / name
    for name in ("deploy-ps-eval.sh", "deploy-ps-prod.sh")
)
TOKEN_VARIABLE = re.compile(r"\bAUTHENTIK_API_TOKEN\b(?!_)")
FUNCTION_START = re.compile(r"^([a-z_][a-z0-9_]*)\(\) \{")


def _token_uses_by_function(text: str) -> list[tuple[int, str | None]]:
    """(line number, enclosing function or None) of every code use of the token variable."""
    uses: list[tuple[int, str | None]] = []
    function: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        start = FUNCTION_START.match(line)
        if start:
            function = start.group(1)
        if not line.lstrip().startswith("#") and TOKEN_VARIABLE.search(line):
            uses.append((number, function))
        if line == "}":
            function = None
    return uses


def test_only_the_token_functions_read_the_api_token_variable() -> None:
    uses = _token_uses_by_function(LIB_FILE.read_text(encoding="utf-8"))

    assert {function for _, function in uses} == TOKEN_FUNCTIONS
    for script in DEPLOY_SCRIPTS:
        assert _token_uses_by_function(script.read_text(encoding="utf-8")) == [], script.name
