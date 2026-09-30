"""Structural tests for the bundled Authentik blueprints (issue #165, Slice 2).

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
LOCAL_TLS_BLUEPRINT = FILES_DIR / "authentik-local-tls-blueprint.yaml"

PASSWORD_MODELS = {
    "authentik_stages_password.passwordstage",
    "authentik_stages_identification.identificationstage",
}


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


# --- 2A: recovery flow + brand flow_recovery (AC-BI-021) ----------------------------------------


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


# --- 2B: passkey-only enrollment (AC-BI-003) ----------------------------------------------------


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


# --- OD-6 negative tests: password stage stays in LOGIN only, never in enrollment/recovery ------


@pytest.mark.parametrize("flow_id", ["ps-enrollment-flow", "ps-recovery-flow"])
def test_enrollment_and_recovery_flows_have_no_password_stage(
    main_entries: list[dict[str, Any]], flow_id: str
) -> None:
    models = set(_bound_stage_models(main_entries, flow_id))

    assert models.isdisjoint(PASSWORD_MODELS)
    assert "authentik_stages_password.passwordstage" not in {e["model"] for e in main_entries}


def test_blueprint_does_not_remove_the_shipped_login_password_stage(
    main_entries: list[dict[str, Any]],
) -> None:
    """OD-6: the password stage stays in the shipped login flow (documented lockout precedent)."""
    login_flow_edits = [
        e
        for e in main_entries
        if e["model"] == "authentik_flows.flowstagebinding"
        and isinstance(e["identifiers"].get("target"), Tag)
        and e["identifiers"]["target"].tag == "Find"
        and "default-authentication-flow" in _walk(e["identifiers"]["target"])
    ]

    # Only the reputation Deny stage is added to the shipped login flow; nothing deletes a binding.
    assert all(e.get("state", "present") == "present" for e in login_flow_edits)
    assert all(
        _keyof_target(e["identifiers"]["stage"]) == "ps-login-reputation-deny-stage"
        for e in login_flow_edits
    )


# --- 2C: sub_mode (AC-BI-022) -------------------------------------------------------------------


def test_ps_cli_provider_uses_username_as_the_token_subject(
    main_entries: list[dict[str, Any]],
) -> None:
    provider = _by_id(main_entries, "ps-cli-provider")

    assert provider["attrs"]["sub_mode"] == "user_username"


# --- blueprint hygiene ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [MAIN_BLUEPRINT, LOCAL_TLS_BLUEPRINT], ids=lambda p: p.name)
def test_keyof_targets_are_defined_earlier_in_the_same_blueprint(path: Path) -> None:
    entries = _entries(path)
    seen_ids: set[str] = set()
    for entry in entries:
        for value in _walk(entry):
            if isinstance(value, Tag) and value.tag == "KeyOf":
                assert value.value in seen_ids, f"{path.name}: !KeyOf {value.value} not defined yet"
        if "id" in entry:
            seen_ids.add(entry["id"])


@pytest.mark.parametrize("path", [MAIN_BLUEPRINT, LOCAL_TLS_BLUEPRINT], ids=lambda p: p.name)
def test_blueprint_declares_version_1_and_a_name(path: Path) -> None:
    doc = _load(path)

    assert doc["version"] == 1
    assert doc["metadata"]["name"]


# --- 2D: local certificate blueprint (AC-BI-007, AC-BI-023) ---------------------------------------


def test_local_tls_blueprint_registers_the_mounted_certificate_from_files() -> None:
    entries = _entries(LOCAL_TLS_BLUEPRINT)
    keypair = _by_id(entries, "ps-local-tls-keypair")

    assert keypair["model"] == "authentik_crypto.certificatekeypair"
    assert keypair["identifiers"] == {"name": "ps-local-tls"}
    cert = keypair["attrs"]["certificate_data"]
    key = keypair["attrs"]["key_data"]
    # Two-element form: a missing mount only warns and never aborts on an absent file (D-05).
    assert cert == Tag(tag="File", value=["/ps-tls/tls.crt", ""])
    assert key == Tag(tag="File", value=["/ps-tls/tls.key", ""])


def test_local_tls_blueprint_sets_the_brand_web_certificate_to_that_keypair() -> None:
    entries = _entries(LOCAL_TLS_BLUEPRINT)
    brands = [e for e in entries if e["model"] == "authentik_brands.brand"]

    assert len(brands) == 1
    assert brands[0]["identifiers"] == {"domain": "authentik-default"}
    assert _keyof_target(brands[0]["attrs"]["web_certificate"]) == "ps-local-tls-keypair"


def test_local_tls_blueprint_never_embeds_key_material() -> None:
    text = LOCAL_TLS_BLUEPRINT.read_text(encoding="utf-8")

    assert "BEGIN" not in text


# --- default-brand race (found live at LC2, issue #165) -----------------------------------------


def _first_brand_index(entries: list[dict[str, Any]]) -> int:
    return next(i for i, e in enumerate(entries) if e["model"] == "authentik_brands.brand")


@pytest.mark.parametrize("path", [MAIN_BLUEPRINT, LOCAL_TLS_BLUEPRINT], ids=["main", "local-tls"])
def test_blueprints_that_patch_the_default_brand_apply_the_shipped_default_brand_first(
    path: Path,
) -> None:
    """Authentik's `Default - Brand` blueprint creates the `authentik-default` brand (with
    `default: true` and the four default flows) only when no default brand exists. Blueprint
    tasks run concurrently, so a patch keyed on `domain: authentik-default` that wins the race
    CREATES the brand itself: `default: false`, no authentication/invalidation flow. The
    listener then finds no brand for an unknown SNI name and serves its self-signed
    certificate. Applying `Default - Brand` through a MetaApplyBlueprint entry before the first
    brand entry makes the order deterministic.
    """
    entries = _entries(path)

    meta_indexes = [
        i
        for i, e in enumerate(entries)
        if e["model"] == "authentik_blueprints.metaapplyblueprint"
        and e["attrs"]["identifiers"] == {"name": "Default - Brand"}
    ]

    assert len(meta_indexes) == 1
    assert meta_indexes[0] < _first_brand_index(entries)
