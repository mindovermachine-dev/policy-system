"""Full happy-path capstone: fresh run + no-op rerun (AC-BI-015, AC-BI-018 end-to-end,
AC-BI-019 full table; PLAN.md §5/S19).

One continuous fixture proves the entire `main()` flow (S1-S18) completes end to end against a
fully-mocked, fresh-subscription harness, then a second invocation is a *true* no-op -- mirrors
`ps-cli/tests/test_integration_auth_full_cycle.py`'s own "one continuous cycle" capstone pattern,
the house convention this repo already uses for this shape of proof.

Per PLAN.md §10 risk 5 and `IMPL_SLICE_18.md`'s own "S19 capstone fixture map" note, one fixture
call does the heavy lifting: `fill_tls_contact_email()` + `seed_subscription()` already bundle
every S5-S17 baseline (region/quota, VM-size allowlist/quota, ingress public IP, cert-manager
readiness) a fresh-subscription run needs -- no per-resource seeding beyond that was necessary,
so the sanctioned S19a/S19b fallback split was not needed.

The true no-op proof (test 2) is broader than every earlier slice's own narrow idempotency
check (S9/S13/S15/S16/S17/S18 each prove their own single call/field never re-fires): it scans
the *entire* second-run delta of all three call logs (`az`, `kubectl`, `helm`) for any call whose
own VERB is `create`/`register`/`regenerate`/`consent` (`admin-consent` is a single hyphenated
token in the real invocation, matched via its `consent` substring) -- never by command family.
This is the exact distinction `IMPL_SLICE_18.md` flags: `keyvault set-policy` and
`ad app permission add`/`list-grants` are NOT create calls and legitimately re-run every pass by
design; a naive "zero calls of any kind" assertion would false-fail on those. Helm's own
mutating verb is `upgrade --install`, not `create`, so it is checked as a separate, explicit
pattern rather than folded into the same substring set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own literals (S1/S9/S10/S11/S15/S17/S18) -- hardcoded here
# rather than parsed from the script, same precedent as every other deploy_ps test module.
RESOURCE_GROUP_NAME = "rg-policy-system"
CHAT_MODEL_NAME = "gpt-5.4-mini"
EMBED_MODEL_NAME = "text-embedding-3-large"
API_APP_NAME = "Policy System API"
CLI_APP_NAME = "Policy System CLI"
LLM_SECRET_NAME = "policy-system-llm-credentials"
HELM_RELEASE_NAME = "policy-system"
CERT_MANAGER_RELEASE_NAME = "cert-manager"
CLUSTER_ISSUER_NAME = "letsencrypt-prod"
PS_SERVICE_NAME = f"{HELM_RELEASE_NAME}-ps-service"

# A call's own VERB (not its command family) that marks it as a mutating, non-idempotent-by-
# rerun operation -- IMPL_SLICE_18's own resolved filter. `admin-consent` is a single hyphenated
# argv token in the real `az ad app permission admin-consent --id ...` call, so it is matched via
# its "consent" substring rather than the literal hyphenated string.
_DISALLOWED_VERB_SUBSTRINGS = ("create", "register", "regenerate", "consent")


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def fake_app_id(display_name: str) -> str:
    """Independently reproduce the fake `az ad app create`'s deterministic appId, same helper as
    every other deploy_ps test module's own `fake_app_id`.
    """
    return f"appid-{display_name.lower().replace(' ', '-')}"


def _lines_starting_with(lines: list[str], prefix: str) -> list[str]:
    return [line for line in lines if line.startswith(prefix)]


def _leading_positional_tokens(line: str) -> list[str]:
    """The command's own leading positional tokens (family/sub-family/verb), stopping at the
    first `--flag` -- e.g. an `ad app permission list-grants --id ... --query [?consentType==...]
    -o tsv` line yields `["ad", "app", "permission", "list-grants"]`, deliberately excluding the
    `--query` flag's *value*. Without this cut, `list-grants`' own `--query` value (which contains
    the literal text `consentType`) would false-positive the verb filter below on its `consent`
    substring, even though `list-grants` is the unprivileged READ AC-BI-005 itself depends on
    re-running every pass (it is what lets a rerun recognize consent a colleague already granted
    out of band).
    """
    tokens: list[str] = []
    for token in line.split():
        if token.startswith("-"):
            break
        tokens.append(token)
    return tokens


def _lines_with_disallowed_verb(lines: list[str]) -> list[str]:
    """Every line whose own leading positional tokens (never a flag's value) contain a disallowed
    VERB -- filtered by verb, not by command family (see this module's own docstring). A call
    like `keyvault set-policy` or `ad app permission add`/`list-grants` never matches: none of
    "set-policy"/"add"/"list-grants" contains any of `_DISALLOWED_VERB_SUBSTRINGS`.
    """
    violations: list[str] = []
    for line in lines:
        leading_tokens = _leading_positional_tokens(line)
        if any(verb in token for token in leading_tokens for verb in _DISALLOWED_VERB_SUBSTRINGS):
            violations.append(line)
    return violations


def _helm_upgrade_install_lines(lines: list[str]) -> list[str]:
    """Helm's own mutating verb is `upgrade --install`, not `create` -- checked separately from
    `_lines_with_disallowed_verb` since the literal substring "create" never appears in a helm
    invocation at all.
    """
    return [line for line in lines if line.startswith("upgrade --install")]


def test_fresh_subscription_completes_end_to_end_provisioning_everything(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-015: one continuous invocation against a freshly-seeded (nothing pre-existing)
    fixture provisions every resource type S1-S18 are responsible for, exiting 0.
    """
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    az_log = deploy_ps_fixture.read_az_log()

    # Resource-provider registration (S7, AC-BI-008) -- a fresh subscription has none
    # registered, so at least one `provider register` call must have fired.
    assert _lines_starting_with(az_log, "provider register"), "no provider was registered"

    # Resource group (S9).
    assert _lines_starting_with(az_log, f"group create --name {RESOURCE_GROUP_NAME}"), (
        "resource group was never created"
    )

    # AIServices account (S9).
    assert _lines_starting_with(az_log, "cognitiveservices account create"), (
        "AIServices account was never created"
    )

    # Both model deployments (S9) -- two distinct `deployment create` calls, one per model.
    deployment_creates = _lines_starting_with(az_log, "cognitiveservices account deployment create")
    assert any(CHAT_MODEL_NAME in line for line in deployment_creates), "chat deployment missing"
    assert any(EMBED_MODEL_NAME in line for line in deployment_creates), "embed deployment missing"

    # Key Vault + secrets (S9).
    assert _lines_starting_with(az_log, "keyvault create"), "Key Vault was never created"
    secret_sets = _lines_starting_with(az_log, "keyvault secret set")
    for secret_name in ("AZURE-API-BASE", "AZURE-API-KEY", "AZURE-API-VERSION"):
        assert any(secret_name in line for line in secret_sets), f"{secret_name} was never set"

    # Both Entra app registrations + their service principals (S10/S11).
    app_creates = _lines_starting_with(az_log, "ad app create")
    assert any(API_APP_NAME in line for line in app_creates), "API app registration missing"
    assert any(CLI_APP_NAME in line for line in app_creates), "CLI app registration missing"
    api_app_id = fake_app_id(API_APP_NAME)
    cli_app_id = fake_app_id(CLI_APP_NAME)
    sp_creates = _lines_starting_with(az_log, "ad sp create")
    assert any(api_app_id in line for line in sp_creates), "API app's service principal missing"
    assert any(cli_app_id in line for line in sp_creates), "CLI app's service principal missing"

    # AKS node VM-size allowlist + vCPU quota preflight (S12, AC-BI-011).
    assert _lines_starting_with(az_log, "vm list-skus"), "VM-size allowlist check never ran"
    assert _lines_starting_with(az_log, "vm list-usage"), "vCPU quota check never ran"

    # AKS cluster (S13).
    assert _lines_starting_with(az_log, "aks create"), "AKS cluster was never created"

    # LLM secret synced into the cluster (S14).
    assert deploy_ps_fixture.read_kubectl_applied("Secret", LLM_SECRET_NAME) is not None, (
        "LLM credentials Secret was never applied"
    )

    # Helm release (S15).
    assert deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME) is not None, (
        "policy-system Helm release was never installed"
    )

    # Application-routing add-on + public DNS label (S16).
    assert _lines_starting_with(az_log, "aks approuting enable"), "app-routing add-on never enabled"
    assert _lines_starting_with(az_log, "network public-ip update"), "DNS label was never set"

    # cert-manager install + ClusterIssuer (S17).
    assert deploy_ps_fixture.read_helm_release_values(CERT_MANAGER_RELEASE_NAME) is not None, (
        "cert-manager Helm release was never installed"
    )
    cluster_issuer_manifest = deploy_ps_fixture.read_kubectl_applied(
        "ClusterIssuer", CLUSTER_ISSUER_NAME
    )
    assert cluster_issuer_manifest is not None, "ClusterIssuer was never applied"

    # PS Service Ingress + closing summary (S18).
    assert deploy_ps_fixture.read_kubectl_applied("Ingress", PS_SERVICE_NAME) is not None, (
        "PS Service Ingress was never applied"
    )
    assert "PS Service: https://" in run.stdout, "summary line never printed the HTTPS URL"


def test_second_run_with_unchanged_inputs_reports_already_up_to_date_and_makes_zero_create_calls(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-018's literal, whole-flow claim: rerunning against unchanged inputs is a TRUE
    no-op -- exit 0, "already up to date", and the second run's own portion of every call log
    (`az`, `kubectl`, `helm`) contains zero calls whose VERB is `create`/`register`/`regenerate`/
    `consent`, nor any `upgrade --install`. Broader than S9/S13/S15/S16/S17/S18's own individual
    narrow idempotency checks -- this is the integration proof that all of them compose.
    """
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)

    az_before = len(deploy_ps_fixture.read_az_log())
    kubectl_before = len(deploy_ps_fixture.read_kubectl_log())
    helm_before = len(deploy_ps_fixture.read_helm_log())

    second_run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    new_az_lines = deploy_ps_fixture.read_az_log()[az_before:]
    new_kubectl_lines = deploy_ps_fixture.read_kubectl_log()[kubectl_before:]
    new_helm_lines = deploy_ps_fixture.read_helm_log()[helm_before:]

    # Sanity: the rerun still does real work (reads/idempotent re-applies) -- an empty delta
    # would make the assertions below vacuously true rather than a genuine proof.
    assert new_az_lines, "second run made no az calls at all -- fixture is likely mis-seeded"

    assert _lines_with_disallowed_verb(new_az_lines) == []
    assert _lines_with_disallowed_verb(new_kubectl_lines) == []
    assert _lines_with_disallowed_verb(new_helm_lines) == []
    assert _helm_upgrade_install_lines(new_helm_lines) == []

    # Idempotent-by-design re-applies (IMPL_SLICE_18's own named examples) DO still appear in
    # the second run's delta -- proving the filter above is discriminating, not just an empty log.
    assert _lines_starting_with(new_az_lines, "keyvault set-policy"), (
        "keyvault set-policy should still re-run every pass by design"
    )
    assert _lines_starting_with(new_az_lines, "ad app permission add"), (
        "ad app permission add should still re-run every pass by design"
    )

    assert second_run.returncode == 0
    assert "Policy System already up to date -- no changes made." in second_run.stdout


def test_confirmation_table_lists_every_resource_type_before_the_prompt(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-019's literal claim: every resource type `print_confirmation_table` names appears in
    stdout before the `[Y/n]` prompt.
    """
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    prompt_index = run.stdout.index("Proceed with these values? [Y/n]")
    for field_label in (
        "Region candidates (in order):",
        "Resource group:",
        "AIServices account:",
        "Chat deployment:",
        "Embedding deployment:",
        "Key Vault:",
        "AKS cluster:",
        "Public DNS label:",
    ):
        field_index = run.stdout.index(field_label)
        assert field_index < prompt_index, f"{field_label!r} did not appear before the prompt"
