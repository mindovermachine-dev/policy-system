"""Structural tests for the bundled Authentik blueprints.

Regexes on the rendered ConfigMap (see the chart's `authentik-blueprint-configmap_test.yaml`)
cannot express semantics such as "no password prompt is reachable from the enrollment flow" or
"a `!KeyOf` target is defined earlier in the file". These tests parse the two blueprint files with a
permissive PyYAML loader (Authentik's custom tags become opaque `Tag` values) and assert on the
structure. Whether Authentik itself accepts the schema is proven live (checkpoint LC1), not here.

Pure file parsing, no external process: hermetic, no marker needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
FILES_DIR = REPO_ROOT / "charts" / "policy-system" / "files"
MAIN_BLUEPRINT = FILES_DIR / "authentik-blueprint.yaml"

PASSWORD_MODELS = {"authentik_stages_password.passwordstage"}


@dataclass(frozen=True)
class Tag:
    """An Authentik custom YAML tag (`!KeyOf`, `!Find`, `!File`, ...) kept opaque."""

    tag: str
    value: Any


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that turns every `!Tag` into a `Tag` instead of failing."""


def _construct_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Tag:
    value: Any
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        assert isinstance(node, yaml.MappingNode)
        value = loader.construct_mapping(node, deep=True)
    return Tag(tag=tag_suffix, value=value)


_BlueprintLoader.add_multi_constructor(  # pyright: ignore[reportUnknownMemberType]  # PyYAML: untyped stub
    "!", _construct_tag
)


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        loaded: dict[str, Any] = yaml.load(f, Loader=_BlueprintLoader)  # noqa: S506  # safe subclass
    return loaded


def _entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = _load(path)["entries"]
    return entries


def _by_id(entries: list[dict[str, Any]], entry_id: str) -> dict[str, Any]:
    matches = [e for e in entries if e.get("id") == entry_id]
    assert len(matches) == 1, f"expected exactly one entry with id {entry_id!r}, got {len(matches)}"
    return matches[0]


def _keyof_target(value: object) -> str:
    assert isinstance(value, Tag)
    assert value.tag == "KeyOf"
    assert isinstance(value.value, str)
    return value.value


def _bindings_for(entries: list[dict[str, Any]], flow_id: str) -> list[dict[str, Any]]:
    """Flow stage bindings targeting the flow with entry id `flow_id`, ordered by `order`."""
    bindings = [
        e
        for e in entries
        if e["model"] == "authentik_flows.flowstagebinding"
        and isinstance(e["identifiers"].get("target"), Tag)
        and e["identifiers"]["target"].tag == "KeyOf"
        and e["identifiers"]["target"].value == flow_id
    ]
    return sorted(bindings, key=lambda b: b["identifiers"]["order"])


def _bound_stage_models(entries: list[dict[str, Any]], flow_id: str) -> list[str]:
    models: list[str] = []
    for binding in _bindings_for(entries, flow_id):
        stage_id = _keyof_target(binding["identifiers"]["stage"])
        models.append(_by_id(entries, stage_id)["model"])
    return models


def _walk(value: Any) -> list[Any]:  # noqa: ANN401 - arbitrary parsed YAML
    """Every scalar/Tag reachable from a parsed structure (for whole-file negative scans)."""
    found: list[Any] = []
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        for key, item in mapping.items():
            found.append(key)
            found.extend(_walk(item))
    elif isinstance(value, list):
        for item in cast("list[Any]", value):
            found.extend(_walk(item))
    elif isinstance(value, Tag):
        found.append(value)
        found.extend(_walk(value.value))
    else:
        found.append(value)
    return found


@pytest.fixture
def main_entries() -> list[dict[str, Any]]:
    return _entries(MAIN_BLUEPRINT)


# --- 2A: recovery flow + brand flow_recovery ----------------------------------------


def test_recovery_flow_is_passkey_only_recovery_designation(
    main_entries: list[dict[str, Any]],
) -> None:
    flow = _by_id(main_entries, "ps-recovery-flow")

    assert flow["model"] == "authentik_flows.flow"
    assert flow["attrs"]["designation"] == "recovery"
    assert flow["attrs"]["authentication"] == "none"


def test_recovery_flow_binds_webauthn_setup_then_user_login_only(
    main_entries: list[dict[str, Any]],
) -> None:
    models = _bound_stage_models(main_entries, "ps-recovery-flow")

    assert models == [
        "authentik_stages_authenticator_webauthn.authenticatorwebauthnstage",
        "authentik_stages_user_login.userloginstage",
    ]


def test_recovery_flow_has_no_password_or_identification_stage(
    main_entries: list[dict[str, Any]],
) -> None:
    models = set(_bound_stage_models(main_entries, "ps-recovery-flow"))

    assert models.isdisjoint(PASSWORD_MODELS)


def test_recovery_webauthn_stage_requires_resident_key_and_user_verification(
    main_entries: list[dict[str, Any]],
) -> None:
    stage = _by_id(main_entries, "ps-recovery-webauthn-stage")

    assert stage["attrs"]["user_verification"] == "required"
    assert stage["attrs"]["resident_key_requirement"] == "required"


def test_brand_sets_flow_recovery_after_the_recovery_flow_is_defined(
    main_entries: list[dict[str, Any]],
) -> None:
    flow_index = next(i for i, e in enumerate(main_entries) if e.get("id") == "ps-recovery-flow")
    brand_entries = [
        (i, e)
        for i, e in enumerate(main_entries)
        if e["model"] == "authentik_brands.brand" and "flow_recovery" in e["attrs"]
    ]

    assert len(brand_entries) == 1
    brand_index, brand = brand_entries[0]
    assert brand_index > flow_index, "`!KeyOf` needs its target defined earlier in the blueprint"
    assert brand["identifiers"] == {"domain": "authentik-default"}
    assert _keyof_target(brand["attrs"]["flow_recovery"]) == "ps-recovery-flow"


def test_brand_still_sets_flow_device_code(main_entries: list[dict[str, Any]]) -> None:
    brands = [e for e in main_entries if e["model"] == "authentik_brands.brand"]

    assert any("flow_device_code" in b["attrs"] for b in brands)


# --- 2B: passkey-only enrollment ----------------------------------------------------


def test_enrollment_prompt_stage_offers_only_username_name_email(
    main_entries: list[dict[str, Any]],
) -> None:
    stage = _by_id(main_entries, "ps-enrollment-prompt-stage")
    field_ids = [_keyof_target(f) for f in stage["attrs"]["fields"]]
    field_keys = [_by_id(main_entries, fid)["attrs"]["field_key"] for fid in field_ids]

    assert sorted(field_keys) == ["email", "name", "username"]


def test_no_password_prompt_exists_anywhere_in_the_blueprint(
    main_entries: list[dict[str, Any]],
) -> None:
    prompts = [e for e in main_entries if e["model"] == "authentik_stages_prompt.prompt"]

    assert prompts, "the enrollment prompts must still exist"
    assert all(p["attrs"]["type"] != "password" for p in prompts)
    assert not any(
        isinstance(v, str) and v.startswith("ps-enrollment-field-password") for v in _walk(prompts)
    )


def test_enrollment_binding_chain_is_invitation_prompt_write_webauthn_login(
    main_entries: list[dict[str, Any]],
) -> None:
    bindings = _bindings_for(main_entries, "ps-enrollment-flow")

    assert [b["identifiers"]["order"] for b in bindings] == [5, 10, 20, 30, 100]
    assert _bound_stage_models(main_entries, "ps-enrollment-flow") == [
        "authentik_stages_invitation.invitationstage",
        "authentik_stages_prompt.promptstage",
        "authentik_stages_user_write.userwritestage",
        "authentik_stages_authenticator_webauthn.authenticatorwebauthnstage",
        "authentik_stages_user_login.userloginstage",
    ]


def test_enrollment_webauthn_stage_requires_resident_key_and_user_verification(
    main_entries: list[dict[str, Any]],
) -> None:
    stage = _by_id(main_entries, "ps-enrollment-webauthn-stage")

    assert stage["attrs"]["user_verification"] == "required"
    assert stage["attrs"]["resident_key_requirement"] == "required"


def test_enrollment_invitation_stage_still_rejects_open_registration(
    main_entries: list[dict[str, Any]],
) -> None:
    stage = _by_id(main_entries, "ps-enrollment-invitation-stage")

    assert stage["attrs"]["continue_flow_without_invitation"] is False


# --- negative tests: no password stage in enrollment, recovery or login ------


@pytest.mark.parametrize("flow_id", ["ps-enrollment-flow", "ps-recovery-flow", "ps-login-flow"])
def test_enrollment_recovery_and_login_flows_have_no_password_stage(
    main_entries: list[dict[str, Any]], flow_id: str
) -> None:
    models = set(_bound_stage_models(main_entries, flow_id))

    assert models.isdisjoint(PASSWORD_MODELS)
    assert "authentik_stages_password.passwordstage" not in {e["model"] for e in main_entries}


def test_no_password_stage_is_defined_or_bound_anywhere_in_the_blueprint(
    main_entries: list[dict[str, Any]],
) -> None:
    assert not [e for e in main_entries if e["model"] in PASSWORD_MODELS]
    for binding in (e for e in main_entries if e["model"] == "authentik_flows.flowstagebinding"):
        stage = binding["identifiers"]["stage"]
        if isinstance(stage, Tag) and stage.tag == "KeyOf":
            assert _by_id(main_entries, str(stage.value))["model"] not in PASSWORD_MODELS
        else:
            assert "password" not in str(stage).lower()
    scalars = [v for v in _walk(main_entries) if isinstance(v, str)]
    assert not [v for v in scalars if "stages_password" in v or "authentication-password" in v]


def test_blueprint_does_not_patch_the_shipped_identification_stage(
    main_entries: list[dict[str, Any]],
) -> None:
    names = [
        e["identifiers"].get("name")
        for e in main_entries
        if e["model"] == "authentik_stages_identification.identificationstage"
    ]

    assert names
    assert all(isinstance(n, str) and n.startswith("ps-") for n in names)


# --- passkey-only login flow ----------------------------------------------------------------


LOGIN_VALIDATE_BINDING_ID = "ps-login-validate-binding"


def _brand_entry_with(entries: list[dict[str, Any]], attr: str) -> tuple[int, dict[str, Any]]:
    matches = [
        (i, e)
        for i, e in enumerate(entries)
        if e["model"] == "authentik_brands.brand" and attr in e["attrs"]
    ]
    assert len(matches) == 1, f"expected exactly one brand entry setting {attr}"
    return matches[0]


def _login_flow_id(entries: list[dict[str, Any]]) -> str:
    """Entry id of the flow the brand serves as its authentication flow."""
    _, brand = _brand_entry_with(entries, "flow_authentication")
    return _keyof_target(brand["attrs"]["flow_authentication"])


def test_login_passkey_validate_stage_is_passkey_only_and_denies_unconfigured_users(
    main_entries: list[dict[str, Any]],
) -> None:
    stage = _by_id(main_entries, "ps-login-passkey-validate")

    assert stage["model"] == "authentik_stages_authenticator_validate.authenticatorvalidatestage"
    assert stage["attrs"]["device_classes"] == ["webauthn"]
    assert stage["attrs"]["not_configured_action"] == "deny"
    assert stage["attrs"]["webauthn_user_verification"] == "required"
    assert stage["attrs"]["last_auth_threshold"] == "seconds=0"


def test_login_flow_chain_is_identification_reputation_deny_passkey_validate_user_login(
    main_entries: list[dict[str, Any]],
) -> None:
    flow_id = _login_flow_id(main_entries)
    flow = _by_id(main_entries, flow_id)

    assert flow["model"] == "authentik_flows.flow"
    assert flow["attrs"]["designation"] == "authentication"
    assert [b["identifiers"]["order"] for b in _bindings_for(main_entries, flow_id)] == [
        10,
        25,
        30,
        100,
    ]
    assert _bound_stage_models(main_entries, flow_id) == [
        "authentik_stages_identification.identificationstage",
        "authentik_stages_deny.denystage",
        "authentik_stages_authenticator_validate.authenticatorvalidatestage",
        "authentik_stages_user_login.userloginstage",
    ]


def test_brand_authentication_flow_is_defined_before_it_is_referenced(
    main_entries: list[dict[str, Any]],
) -> None:
    flow_id = _login_flow_id(main_entries)
    flow_index = next(i for i, e in enumerate(main_entries) if e.get("id") == flow_id)
    brand_index, brand = _brand_entry_with(main_entries, "flow_authentication")

    assert brand_index > flow_index
    assert brand["identifiers"] == {"domain": "authentik-default"}


def test_device_code_flow_is_the_passkey_login_flow(main_entries: list[dict[str, Any]]) -> None:
    _, brand = _brand_entry_with(main_entries, "flow_device_code")

    assert _keyof_target(brand["attrs"]["flow_device_code"]) == _login_flow_id(main_entries)


def test_identification_webauthn_stage_is_the_bound_passkey_validate_stage(
    main_entries: list[dict[str, Any]],
) -> None:
    flow_id = _login_flow_id(main_entries)
    by_order = {b["identifiers"]["order"]: b for b in _bindings_for(main_entries, flow_id)}
    identification = _by_id(main_entries, _keyof_target(by_order[10]["identifiers"]["stage"]))
    validate_id = _keyof_target(by_order[30]["identifiers"]["stage"])

    assert validate_id == "ps-login-passkey-validate"
    assert _keyof_target(identification["attrs"]["webauthn_stage"]) == validate_id
    assert identification["attrs"]["user_fields"] == ["email", "username"]


def test_no_entry_deletes_a_row_with_state_absent(
    main_entries: list[dict[str, Any]],
) -> None:
    """Authentik's importer deletes through an instance it builds unsaved when no row matches.

    For the multi-table-inheritance models (a flow-stage binding and a policy binding both
    inherit from the policy-binding model) that unsaved instance has no parent pointer, so the
    delete raises and the whole apply fails on any install where the row does not exist. A
    blueprint cannot condition a deletion on the row existing, so none may use `state: absent`.
    """
    removals = [e["model"] for e in main_entries if e.get("state") == "absent"]

    assert removals == []


def test_login_flow_keeps_the_reputation_deny_then_passkey_validate_then_login_order(
    main_entries: list[dict[str, Any]],
) -> None:
    flow_id = _login_flow_id(main_entries)
    orders = sorted(b["identifiers"]["order"] for b in _bindings_for(main_entries, flow_id))

    assert orders == [10, 25, 30, 100]


def test_login_validate_binding_is_skipped_only_after_a_verified_passkey_identification(
    main_entries: list[dict[str, Any]],
) -> None:
    binding = _by_id(main_entries, LOGIN_VALIDATE_BINDING_ID)
    policy_bindings = [
        e
        for e in main_entries
        if e["model"] == "authentik_policies.policybinding"
        and isinstance(e["identifiers"].get("target"), Tag)
        and e["identifiers"]["target"].value == LOGIN_VALIDATE_BINDING_ID
    ]

    assert binding["attrs"]["re_evaluate_policies"] is True
    assert len(policy_bindings) == 1
    attrs = policy_bindings[0]["attrs"]
    assert attrs["failure_result"] is True
    assert attrs.get("negate", False) is False
    policy = _by_id(main_entries, _keyof_target(attrs["policy"]))
    assert policy["model"] == "authentik_policies_expression.expressionpolicy"
    expression = policy["attrs"]["expression"]
    assert "auth_webauthn_pwl" in expression
    assert "not" in expression


# --- 2C: sub_mode -------------------------------------------------------------------


def test_ps_cli_provider_uses_username_as_the_token_subject(
    main_entries: list[dict[str, Any]],
) -> None:
    provider = _by_id(main_entries, "ps-cli-provider")

    assert provider["attrs"]["sub_mode"] == "user_username"


# --- authorization_code grant + Claude redirect URIs ---------------------------------------


def test_ps_cli_provider_accepts_authorization_code_with_claude_redirect_uris(
    main_entries: list[dict[str, Any]],
) -> None:
    attrs = _by_id(main_entries, "ps-cli-provider")["attrs"]

    assert attrs["client_type"] == "public"
    assert set(attrs["grant_types"]) == {
        "urn:ietf:params:oauth:grant-type:device_code",
        "refresh_token",
        "authorization_code",
    }
    assert len(attrs["grant_types"]) == 3  # no duplicates, no implicit/client_credentials/password
    assert attrs["redirect_uris"] == [
        {"matching_mode": "strict", "url": "https://claude.ai/api/mcp/auth_callback"},
        {"matching_mode": "regex", "url": r"^http://(localhost|127\.0\.0\.1):[0-9]+/callback$"},
    ]


# --- blueprint hygiene ---------------------------------------------------------------------------


def test_keyof_targets_are_defined_earlier_in_the_same_blueprint() -> None:
    path = MAIN_BLUEPRINT
    entries = _entries(path)
    seen_ids: set[str] = set()
    for entry in entries:
        for value in _walk(entry):
            if isinstance(value, Tag) and value.tag == "KeyOf":
                assert value.value in seen_ids, f"{path.name}: !KeyOf {value.value} not defined yet"
        if "id" in entry:
            seen_ids.add(entry["id"])


def test_blueprint_declares_version_1_and_a_name() -> None:
    doc = _load(MAIN_BLUEPRINT)

    assert doc["version"] == 1
    assert doc["metadata"]["name"]


# --- default-brand race (found live) -----------------------------------------


def _first_brand_index(entries: list[dict[str, Any]]) -> int:
    return next(i for i, e in enumerate(entries) if e["model"] == "authentik_brands.brand")


def test_blueprints_that_patch_the_default_brand_apply_the_shipped_default_brand_first() -> None:
    """Authentik's `Default - Brand` blueprint creates the `authentik-default` brand (with
    `default: true` and the four default flows) only when no default brand exists. Blueprint
    tasks run concurrently, so a patch keyed on `domain: authentik-default` that wins the race
    CREATES the brand itself: `default: false`, no authentication/invalidation flow. The
    listener then finds no brand for an unknown SNI name and serves its self-signed
    certificate. Applying `Default - Brand` through a MetaApplyBlueprint entry before the first
    brand entry makes the order deterministic.
    """
    entries = _entries(MAIN_BLUEPRINT)

    meta_indexes = [
        i
        for i, e in enumerate(entries)
        if e["model"] == "authentik_blueprints.metaapplyblueprint"
        and e["attrs"]["identifiers"] == {"name": "Default - Brand"}
    ]

    assert len(meta_indexes) == 1
    assert meta_indexes[0] < _first_brand_index(entries)
