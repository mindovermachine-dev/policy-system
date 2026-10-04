"""Tests for `scripts/lib/authentik-owner.sh` (issue #165, Slice 3B).

The real library is sourced by a real bash; `curl` is the stateful Authentik fake serving
response shapes recorded from a live Authentik 2026.8.3, `kubectl` records its argv.
Acceptance: AC-BI-002 (owner user + link), AC-BI-004 (re-run semantics), AC-BI-016 (fail
cleanly, no half-created user), D-21 (token never in argv/output).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import subprocess

    from conftest import LibHarness

EMAIL = "owner@example.com"
HOST = "https://authentik.local:30443"

PRELUDE = f"""
set -uo pipefail
EXIT_FAILURE=1
print_error() {{ local f="$1"; shift; printf "$f" "$@" >&2; }}
source "$PS_TEST_LIB_DIR/authentik-owner.sh"
AUTHENTIK_API_BASE="${{AUTHENTIK_API_BASE:-{HOST}}}"
AUTHENTIK_LINK_BASE="${{AUTHENTIK_LINK_BASE:-{HOST}}}"
AUTHENTIK_CURL_ARGS=(--cacert /tmp/ca.pem --resolve authentik.local:30443:127.0.0.1)
AUTHENTIK_API_TOKEN="$PS_TEST_TOKEN"
"""

EXISTING_USER: dict[str, Any] = {
    "pk": 7,
    "username": EMAIL,
    "email": EMAIL,
    "type": "internal",
    "groups": [],
    "is_active": True,
}


def _provision(
    harness: LibHarness, email: str = EMAIL, **env: str
) -> subprocess.CompletedProcess[str]:
    return harness.run(
        PRELUDE + f'provision_owner "{email}"; rc=$?; print_owner_link_message; exit $rc',
        **env,
    )


def _all_output(harness: LibHarness, proc: subprocess.CompletedProcess[str]) -> str:
    return (
        proc.stdout
        + proc.stderr
        + json.dumps(harness.curl_calls())
        + "\n".join(harness.kubectl_calls())
    )


@pytest.mark.parametrize("bad", ["foo", "a@", "@b.c", "with space@x.com", ""])
def test_invalid_email_is_rejected_before_any_api_call(lib_harness: LibHarness, bad: str) -> None:
    proc = lib_harness.run(PRELUDE + f'validate_owner_email "{bad}"')

    assert proc.returncode != 0
    assert "--owner-email" in proc.stderr
    assert lib_harness.curl_calls() == []


def test_valid_email_is_accepted(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(PRELUDE + f'validate_owner_email "{EMAIL}"')

    assert proc.returncode == 0


def test_new_owner_is_created_in_admin_group_without_password_and_link_is_printed(
    lib_harness: LibHarness,
) -> None:
    proc = _provision(lib_harness)

    assert proc.returncode == 0, proc.stderr
    users = list(lib_harness.curl_state_users().values())
    assert len(users) == 1
    assert users[0]["username"] == EMAIL
    assert users[0]["email"] == EMAIL
    assert users[0]["is_superuser"] is True  # member of the admin group
    posted = [
        c["argv"][c["argv"].index("--data") + 1]
        for c in lib_harness.curl_calls()
        if "--data" in c["argv"]
    ]
    assert posted
    assert all("password" not in body for body in posted)
    recovery = [u for u in lib_harness.curl_urls() if "/recovery/" in u]
    assert len(recovery) == 1
    links = [line for line in proc.stdout.splitlines() if line.startswith(f"{HOST}/if/flow/")]
    assert len(links) == 1


def test_recovery_call_carries_token_duration_default_30_minutes(lib_harness: LibHarness) -> None:
    _provision(lib_harness)

    recovery = next(u for u in lib_harness.curl_urls() if "/recovery/" in u)
    assert "token_duration=minutes=30" in recovery


def test_recovery_call_honours_ttl_override(lib_harness: LibHarness) -> None:
    _provision(lib_harness, PS_OWNER_LINK_TTL="5")

    recovery = next(u for u in lib_harness.curl_urls() if "/recovery/" in u)
    assert "token_duration=minutes=5" in recovery


def test_token_never_appears_in_output_or_argv(lib_harness: LibHarness) -> None:
    proc = _provision(lib_harness)

    assert proc.returncode == 0
    assert lib_harness.token not in _all_output(lib_harness, proc)


def test_token_is_read_from_the_cluster_secret_and_never_echoed(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(
        PRELUDE.replace('AUTHENTIK_API_TOKEN="$PS_TEST_TOKEN"', 'AUTHENTIK_API_TOKEN=""')
        + "read_bootstrap_token policy-system policy-system-authentik-api-token || exit 9\n"
        + "check_token_accepted; rc=$?\n"
        + '[[ "$AUTHENTIK_API_TOKEN" == "$EXPECTED" ]] || exit 8; exit $rc',
        EXPECTED=lib_harness.token,
    )

    assert proc.returncode == 0, proc.stderr
    assert any(
        "get secret" in c and "policy-system-authentik-api-token" in c
        for c in lib_harness.kubectl_calls()
    )
    assert lib_harness.token not in _all_output(lib_harness, proc)


def test_missing_token_secret_fails_with_actionable_message(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(
        PRELUDE + "read_bootstrap_token policy-system policy-system-authentik-api-token",
        PS_TEST_SECRET_MISSING="1",
    )

    assert proc.returncode != 0
    assert "policy-system-authentik-api-token" in proc.stderr


@pytest.mark.parametrize("wrong_token_status", [401, 403])
def test_rejected_token_stops_before_any_user_is_created(
    lib_harness: LibHarness, wrong_token_status: int
) -> None:
    lib_harness.seed_curl(token="a-different-token", token_reject_status=wrong_token_status)

    proc = _provision(lib_harness)

    assert proc.returncode != 0
    assert "existingSecret" in proc.stderr
    assert "bootstrap" in proc.stderr.lower()
    assert lib_harness.curl_state_users() == {}
    assert all("/core/users/me/" in u for u in lib_harness.curl_urls())
    assert lib_harness.token not in _all_output(lib_harness, proc)


def test_rerun_with_zero_devices_issues_a_new_link_without_duplicating_the_user(
    lib_harness: LibHarness,
) -> None:
    lib_harness.seed_curl(users={"7": EXISTING_USER}, devices={})

    proc = _provision(lib_harness)

    assert proc.returncode == 0, proc.stderr
    assert len(lib_harness.curl_state_users()) == 1
    assert any("/recovery/" in u for u in lib_harness.curl_urls())
    assert f"{HOST}/if/flow/" in proc.stdout


def test_rerun_with_an_enrolled_device_issues_no_link(lib_harness: LibHarness) -> None:
    lib_harness.seed_curl(users={"7": EXISTING_USER}, devices={"7": [{"pk": 1, "name": "key"}]})

    proc = _provision(lib_harness)

    assert proc.returncode == 0, proc.stderr
    assert not any("/recovery/" in u for u in lib_harness.curl_urls())
    assert "already" in proc.stdout.lower()
    assert "flow_token" not in proc.stdout


def test_device_check_uses_the_per_user_endpoint_not_the_unfiltered_webauthn_list(
    lib_harness: LibHarness,
) -> None:
    # Another user's device must not make the owner look enrolled (DECISIONS D-B).
    other: dict[str, Any] = {
        **EXISTING_USER,
        "pk": 9,
        "username": "other@example.com",
        "email": "other@example.com",
    }
    lib_harness.seed_curl(
        users={"7": EXISTING_USER, "9": other}, devices={"9": [{"pk": 2, "name": "other"}]}
    )

    proc = _provision(lib_harness)

    assert proc.returncode == 0, proc.stderr
    assert any("/authenticators/admin/all/?user=7" in u for u in lib_harness.curl_urls())
    assert not any("/authenticators/admin/webauthn/" in u for u in lib_harness.curl_urls())
    assert any("/recovery/" in u for u in lib_harness.curl_urls())


def test_unreachable_api_fails_naming_the_fix_and_creates_nothing(lib_harness: LibHarness) -> None:
    lib_harness.seed_curl(unreachable=True)

    proc = _provision(lib_harness)

    assert proc.returncode != 0
    assert "reach" in proc.stderr.lower()
    assert lib_harness.curl_state_users() == {}


def test_failure_after_create_deletes_the_user_and_a_later_run_completes(
    lib_harness: LibHarness,
) -> None:
    lib_harness.seed_curl(recovery_flow=False)

    failed = _provision(lib_harness)

    assert failed.returncode != 0
    assert lib_harness.curl_state_users() == {}
    assert any(
        call["argv"][call["argv"].index("-X") + 1] == "DELETE"
        for call in lib_harness.curl_calls()
        if "-X" in call["argv"]
    )

    lib_harness.seed_curl(recovery_flow=True)
    retried = _provision(lib_harness)

    assert retried.returncode == 0, retried.stderr
    assert len(lib_harness.curl_state_users()) == 1
    assert f"{HOST}/if/flow/" in retried.stdout


def test_a_pre_existing_user_is_never_deleted_on_failure(lib_harness: LibHarness) -> None:
    lib_harness.seed_curl(users={"7": EXISTING_USER}, devices={}, recovery_flow=False)

    proc = _provision(lib_harness)

    assert proc.returncode != 0
    assert "7" in lib_harness.curl_state_users()
    assert not any(
        "-X" in call["argv"] and call["argv"][call["argv"].index("-X") + 1] == "DELETE"
        for call in lib_harness.curl_calls()
    )


def test_link_authority_is_rewritten_to_the_public_base(lib_harness: LibHarness) -> None:
    proc = _provision(
        lib_harness,
        AUTHENTIK_API_BASE="http://127.0.0.1:9443/auth",
        AUTHENTIK_LINK_BASE="https://ps.example.com/auth",
    )

    assert proc.returncode == 0, proc.stderr
    assert "https://ps.example.com/auth/if/flow/ps-passkey-recovery/?flow_token=" in proc.stdout
    assert "127.0.0.1" not in proc.stdout


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        (
            "http://127.0.0.1:9000/if/flow/x/?flow_token=t",
            "https://h.example/auth/if/flow/x/?flow_token=t",
        ),
        (
            "http://127.0.0.1:9000/auth/if/flow/x/?flow_token=t",
            "https://h.example/auth/if/flow/x/?flow_token=t",
        ),
    ],
)
def test_rewrite_never_duplicates_the_path_prefix(
    lib_harness: LibHarness, returned: str, expected: str
) -> None:
    proc = lib_harness.run(
        PRELUDE + 'AUTHENTIK_LINK_BASE="https://h.example/auth"; rewrite_owner_link "$RETURNED"',
        RETURNED=returned,
    )

    assert proc.stdout.strip() == expected


def test_no_curl_insecure_flags_anywhere_in_the_lib() -> None:
    lib = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "authentik-owner.sh"
    text = lib.read_text(encoding="utf-8")

    assert " -k " not in text
    assert "--insecure" not in text


def test_prompt_reads_the_email_from_stdin_when_blank(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(
        PRELUDE + 'OWNER_EMAIL=""; prompt_owner_email <<< "typed@example.com" || exit 1; '
        'printf "%s" "$OWNER_EMAIL"'
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "typed@example.com"


def test_prompt_rejects_an_empty_answer(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(PRELUDE + 'OWNER_EMAIL=""; prompt_owner_email <<< ""')

    assert proc.returncode != 0
    assert "--owner-email" in proc.stderr


def test_prompt_is_disabled_for_non_interactive_runs(lib_harness: LibHarness) -> None:
    proc = lib_harness.run(
        PRELUDE + 'OWNER_EMAIL=""; SKIP_OWNER_PROMPT=true; prompt_owner_email <<< "x@y.z"'
    )

    assert proc.returncode != 0
    assert "--owner-email" in proc.stderr


# --- waiting for the bundled blueprint (found live at LC3, issue #165) ------------------------

MAIN_BLUEPRINT = "PS Service — bundled Authentik setup (issue #129)"
WAIT_ENV = {"PS_BLUEPRINT_WAIT_ATTEMPTS": "4", "PS_BLUEPRINT_WAIT_INTERVAL_SECONDS": "0"}


def _blueprint(status: str) -> list[dict[str, Any]]:
    return [
        {
            "pk": "66666666-7777-8888-9999-000000000000",
            "name": MAIN_BLUEPRINT,
            "status": status,
        }
    ]


def test_owner_is_not_created_before_the_bundled_blueprint_has_applied(
    lib_harness: LibHarness,
) -> None:
    """On a fresh install the API answers as soon as the server is Ready, but the worker applies
    the blueprint (recovery flow, brand) about a minute later; a recovery link requested earlier
    fails with `No recovery flow set.` Wait for the blueprint instead of racing it.
    """
    lib_harness.seed_curl(blueprint_list_empty_calls=2)

    proc = _provision(lib_harness, **WAIT_ENV)

    assert proc.returncode == 0, proc.stderr
    assert "/if/flow/" in proc.stdout
    urls = lib_harness.curl_urls()
    blueprint_polls = [i for i, u in enumerate(urls) if "/managed/blueprints/" in u]
    assert len(blueprint_polls) == 3
    assert max(blueprint_polls) < next(i for i, u in enumerate(urls) if "/recovery/" in u)


def test_a_blueprint_that_never_reaches_successful_fails_naming_the_fix_and_creates_nothing(
    lib_harness: LibHarness,
) -> None:
    lib_harness.seed_curl(blueprints=_blueprint("error"))

    proc = _provision(lib_harness, **WAIT_ENV)

    assert proc.returncode != 0
    assert MAIN_BLUEPRINT in proc.stderr
    assert "kubectl logs" in proc.stderr
    assert lib_harness.curl_state_users() == {}


def test_a_blueprint_in_error_is_reapplied_a_bounded_number_of_times(
    lib_harness: LibHarness,
) -> None:
    """Parallel blueprint applies on a fresh install can deadlock in Postgres and leave the bundled
    blueprint in `error`; Authentik re-applies only on a file change, so the script re-applies it.
    """
    lib_harness.seed_curl(blueprints=_blueprint("error"))

    proc = _provision(lib_harness, **WAIT_ENV, PS_BLUEPRINT_REAPPLY_ATTEMPTS="2")

    assert proc.returncode != 0
    applies = [u for u in lib_harness.curl_urls() if u.endswith("/apply/")]
    assert len(applies) == 2


def test_an_already_applied_blueprint_costs_a_single_poll(lib_harness: LibHarness) -> None:
    proc = _provision(lib_harness, **WAIT_ENV)

    assert proc.returncode == 0, proc.stderr
    polls = [u for u in lib_harness.curl_urls() if "/managed/blueprints/" in u]
    assert len(polls) == 1
